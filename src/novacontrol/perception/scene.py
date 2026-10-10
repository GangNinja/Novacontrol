"""Scene assembly and the abstraction layer built on top of it.

Two things live here, in the order the pipeline uses them.

**The scene** is assembled from the parts the run actually produced — objects,
text, relationships, tracks, segmentation references, temporal events, the
providers involved — and carries the confidence that was measured across them.
Assembling is not interpreting: every field is a direct reference to evidence
some provider produced, and the scene's own metadata records which of those
sources answered.

**The abstraction** is the compact form a later phase wants to read: counts, the
salient relations, how much text was read, and a one-line summary. It is built by
counting and quoting what the scene contains — never by asking a model what the
scene MEANS, because that inference is exactly what this phase must not
manufacture. ``basis`` says which it was: ``deterministic`` for the fast path,
``deterministic+vlm`` when a model contributed objects.

The summary is therefore reproducible: the same scene produces the same
abstraction on any machine, and a test can pin the sentence. That is the property
that makes it a foundation rather than an opinion.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.perception.frames import Frame
from novacontrol.perception.models import (
    DetectedObject,
    OcrText,
    Relationship,
    RelationshipKind,
    SceneAbstraction,
    SceneRepresentation,
    SegmentationResult,
    TemporalEvent,
    TemporalEventKind,
)
from novacontrol.perception.tracking import TrackUpdate

__all__ = [
    "abstraction_for",
    "build_scene",
    "focus_for",
    "scene_confidence",
]


def scene_confidence(
    objects: Sequence[DetectedObject] = (),
    text: Sequence[OcrText] = (),
    *,
    extra: Sequence[float | None] = (),
) -> float | None:
    """The mean of the confidences that were MEASURED, or ``None`` if none were.

    A mean is not truth, and this one is only reported when at least one provider
    gave a number — the alternative (treating unmeasured as zero) would make a
    scene full of unquantified detections look confident about having found
    nothing. Which values went in is recoverable from the objects themselves.
    """
    measured = [
        item
        for item in (
            *(obj.confidence for obj in objects),
            *(line.confidence for line in text),
            *extra,
        )
        if item is not None
    ]
    if not measured:
        return None
    return sum(measured) / len(measured)


def _uncertainty_of(confidence: float | None) -> float | None:
    """1 - confidence, and nothing at all when nothing was measured."""
    if confidence is None:
        return None
    return max(0.0, min(1.0, 1.0 - confidence))


def build_scene(
    frame: Frame,
    *,
    objects: Sequence[DetectedObject] = (),
    text: Sequence[OcrText] = (),
    relationships: Sequence[Relationship] = (),
    track_update: TrackUpdate | None = None,
    segmentation: Sequence[SegmentationResult] = (),
    temporal_events: Sequence[TemporalEvent] = (),
    providers: Mapping[str, str] | None = None,
    capability_states: Mapping[str, str] | None = None,
    focus: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    abstraction: bool = True,
) -> SceneRepresentation:
    """One scene from the parts of one observation, plus its abstraction.

    ``abstraction=False`` exists for a caller that wants the raw scene (a test
    comparing structure, or a run that was told to skip the layer); the default is
    on, because a scene without its compact form is the representation a
    downstream phase would have to rebuild.
    """
    tracks = tuple(track_update.tracks) if track_update is not None else ()
    confidence = scene_confidence(objects, text)
    scene = SceneRepresentation(
        timestamp=frame.timestamp,
        frame_id=frame.frame_id,
        source_id=frame.source_id,
        sequence=frame.sequence,
        width=frame.width,
        height=frame.height,
        objects=tuple(objects),
        text=tuple(text),
        relationships=tuple(relationships),
        tracks=tracks,
        segmentation=tuple(segmentation),
        temporal_events=tuple(temporal_events),
        focus=dict(focus or {}),
        confidence=confidence,
        uncertainty=_uncertainty_of(confidence),
        providers=dict(providers or {}),
        capability_states=dict(capability_states or {}),
        abstraction=None,
        metadata=dict(metadata or {}),
    )
    if not abstraction:
        return scene
    return replace(scene, abstraction=abstraction_for(scene))


def _counts_for(scene: SceneRepresentation) -> dict[str, int]:
    """How many of each label the scene contains, in a stable order."""
    counts: Counter[str] = Counter(item.label for item in scene.objects if item.label)
    return dict(sorted(counts.items()))


def _plural(count: int, word: str) -> str:
    """A count and its noun, pluralized for anything but one."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _summary_sentence(scene: SceneRepresentation, counts: Mapping[str, int]) -> str:
    """One deterministic sentence describing what was perceived.

    Every clause is a count or a quotation of something in the scene, and each is
    omitted when its evidence is absent — a blank frame reads as a blank frame
    rather than as a scene with zeroes in it.
    """
    parts: list[str] = []
    total = sum(counts.values())
    if total:
        breakdown = ", ".join(f"{count} {label}" for label, count in counts.items())
        parts.append(f"{_plural(total, 'object')} detected ({breakdown})")
    lines = [line.text.strip() for line in scene.text if line.text.strip()]
    if lines:
        parts.append(f"{_plural(len(lines), 'line')} of text read")
    moved = sum(
        1 for event in scene.temporal_events if event.kind is TemporalEventKind.OBJECT_MOVED
    )
    if moved:
        parts.append(f"{_plural(moved, 'object')} moved since the previous frame")
    if not parts:
        return "Nothing was detected in this frame and no text could be read."
    return ". ".join(parts) + "."


#: The labels a human reads on the relationship kinds, for highlight sentences.
_RELATION_WORDS: Mapping[RelationshipKind, str] = {
    RelationshipKind.LEFT_OF: "is left of",
    RelationshipKind.RIGHT_OF: "is right of",
    RelationshipKind.ABOVE: "is above",
    RelationshipKind.BELOW: "is below",
    RelationshipKind.INSIDE: "is inside",
    RelationshipKind.CONTAINS: "contains",
    RelationshipKind.OVERLAPS: "overlaps",
    RelationshipKind.NEAR: "is near",
}


def _highlights(
    scene: SceneRepresentation,
    counts: Mapping[str, int],
    *,
    max_highlights: int,
) -> tuple[str, ...]:
    """The salient lines, each traceable to evidence in the scene.

    Relationships come first because they are the genuinely structural fact (a
    label inside a box says more than either label alone), then text, then the
    relationship cap, which is a limitation a reader needs to know about rather
    than a detail to hide.
    """
    labels = {item.object_id: item.label for item in scene.objects}
    highlights: list[str] = []
    for relation in scene.relationships:
        subject = labels.get(relation.subject_id, "an object")
        other = labels.get(relation.object_id, "another object")
        verb = _RELATION_WORDS.get(relation.kind, "relates to")
        highlights.append(f"{subject!r} {verb} {other!r}")
        if len(highlights) >= max_highlights:
            break
    if scene.text_block and len(highlights) < max_highlights:
        first = next((line for line in scene.text if line.text.strip()), None)
        if first is not None:
            highlights.append(f"text read: {first.text.strip()[:80]!r}")
    if counts and len(highlights) < max_highlights:
        top = max(counts.items(), key=lambda row: (row[1], row[0]))
        highlights.append(f"most common label: {top[0]!r} ({top[1]})")
    if len(highlights) < max_highlights:
        capped = scene.metadata.get("relationships_capped")
        if capped:
            highlights.append(f"{capped} object pair(s) were not compared for relationships")
    return tuple(highlights[:max_highlights])


def _provenance_names() -> tuple[str, ...]:
    """The deterministic layers every abstraction is built from."""
    return ("counting", "geometry")


def _provenance(scene: SceneRepresentation, *, escalated: bool) -> tuple[str, ...]:
    """The providers that contributed, plus the deterministic layers used.

    A bare set of provider names would leave the geometry and counting invisible
    even though both shaped the answer, so they are named alongside.
    """
    names = {str(value) for value in scene.providers.values() if str(value).strip()}
    if scene.relationships:
        names.add("geometry")
    if scene.objects or scene.text:
        names.add("counting")
    if escalated:
        names.add("vlm-escalation")
    return tuple(sorted(names))


def abstraction_for(
    scene: SceneRepresentation,
    *,
    max_highlights: int = 6,
    text_excerpt_chars: int = 240,
) -> SceneAbstraction:
    """The compact form of one scene — counts, relations, and a summary.

    ``basis`` distinguishes the two ways objects arrive (deterministic providers
    versus a vision model's own answer), because a summary that mixed them
    without saying so would read as more certain than it is.
    """
    counts = _counts_for(scene)
    semantic = any(
        str(item.source).startswith("vlm") or str(item.source).startswith("model")
        for item in scene.objects
    )
    escalated = bool(scene.metadata.get("escalated")) or semantic
    excerpt = " ".join(scene.text_block.split())[: max(0, text_excerpt_chars)]
    return SceneAbstraction(
        summary=_summary_sentence(scene, counts),
        counts=counts,
        highlights=_highlights(scene, counts, max_highlights=max_highlights),
        provenance=_provenance(scene, escalated=escalated),
        basis="deterministic+vlm" if escalated else "deterministic",
        confidence=scene.confidence,
        uncertainty=scene.uncertainty,
        text_excerpt=excerpt,
    )


def focus_for(
    question: str, scene: SceneRepresentation, *, max_items: int = 5
) -> dict[str, Any]:
    """Which objects and lines the question was about — lexically, and honestly.

    The match is the same content-word test the vision layer already uses to
    decide whether OCR text answers a question: lowercased content words of the
    question, matched as whole words against object labels and text lines. A
    question with no match reports ``match="none"`` rather than pointing at
    whichever object happened to be nearest, because an invented focus is worse
    than an empty one for anything that reads it.
    """
    from novacontrol.intelligence.semantic import content_words

    terms = tuple(
        word
        for word in content_words(question.lower())
        if len(word) > 1
    )
    focus: dict[str, Any] = {"question": question.strip(), "terms": list(terms)}
    if not terms:
        focus["match"] = "none"
        focus["reason"] = "the question names nothing to look for"
        return focus
    matched_objects: list[str] = []
    for item in scene.objects:
        label = item.label.casefold()
        if any(term in label for term in terms):
            matched_objects.append(item.object_id)
            if len(matched_objects) >= max_items:
                break
    matched_lines: list[int] = []
    for index, line in enumerate(scene.text):
        text = line.text.casefold()
        if any(term in text for term in terms):
            matched_lines.append(index)
            if len(matched_lines) >= max_items:
                break
    if matched_objects:
        focus["match"] = "object"
    elif matched_lines:
        focus["match"] = "text"
    else:
        focus["match"] = "none"
    focus["matched_objects"] = matched_objects
    focus["matched_lines"] = matched_lines
    return focus

