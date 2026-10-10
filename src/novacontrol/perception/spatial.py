"""Spatial relationships: what the geometry can prove, stated as geometry.

"How is the text placed relative to the panel?" is a question a screenshot
pipeline can answer without a model, provided it answers only what the boxes
support. This module derives exactly that: containment, overlap, the four
directions, and proximity — each from the arithmetic that decided it, and each
carrying that arithmetic in ``evidence`` so the claim can be checked rather than
believed.

Two rules keep it honest:

**No relation is inferred that the data cannot support.** A pair with no clear
directional gap and no overlap is NEAR at most, and a pair whose boxes lack
extent produces nothing at all. There is no "probably to the left" here.

**A relation is never surer than the boxes it came from.** Confidence is the
weaker of the two objects' own confidences, and it is ``None`` when either is
unmeasured — the relation inherits the upstream uncertainty instead of
laundering it into a fresh number.

The engine bounds its own work: pairs are capped, and the cap is REPORTED rather
than silently truncating the relationship list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.perception.models import BBox, DetectedObject, Relationship, RelationshipKind

__all__ = ["RelationshipReport", "derive_relationships"]


@dataclass(frozen=True, slots=True)
class RelationshipReport:
    """The relations found, plus how much of the pair space was actually examined."""

    relationships: tuple[Relationship, ...] = ()
    pairs_considered: int = 0
    pairs_skipped: int = 0
    objects: int = 0

    @property
    def capped(self) -> bool:
        return self.pairs_skipped > 0

    def by_kind(self, kind: RelationshipKind) -> tuple[Relationship, ...]:
        return tuple(item for item in self.relationships if item.kind is kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationships": [item.to_dict() for item in self.relationships],
            "pairs_considered": self.pairs_considered,
            "pairs_skipped": self.pairs_skipped,
            "objects": self.objects,
            "capped": self.capped,
        }


@dataclass(frozen=True, slots=True)
class _Options:
    near_ratio: float = 0.25
    overlap_ratio: float = 0.5
    max_pairs: int = 120


def derive_relationships(
    objects: Sequence[DetectedObject],
    *,
    near_ratio: float = 0.25,
    overlap_ratio: float = 0.5,
    max_pairs: int = 120,
) -> RelationshipReport:
    """Every spatial relation these boxes support, with its arithmetic.

    Pairs are visited in a stable order (the order the objects arrived in), so
    the same scene always produces the same relationship list — which is what a
    temporal diff and a test both need. Objects without extent are skipped
    entirely: a box with no area cannot support a claim about position.
    """
    options = _Options(
        near_ratio=max(0.0, near_ratio),
        overlap_ratio=max(0.0, min(1.0, overlap_ratio)),
        max_pairs=max(0, max_pairs),
    )
    located = [item for item in objects if item.located]
    found: list[Relationship] = []
    considered = 0
    skipped = 0
    for left_index in range(len(located)):
        for right_index in range(left_index + 1, len(located)):
            if considered >= options.max_pairs:
                skipped += 1
                continue
            considered += 1
            first = located[left_index]
            second = located[right_index]
            found.extend(_relations_for(first, second, options))
    return RelationshipReport(
        relationships=tuple(found),
        pairs_considered=considered,
        pairs_skipped=skipped,
        objects=len(located),
    )


def _relations_for(
    first: DetectedObject, second: DetectedObject, options: _Options
) -> list[Relationship]:
    """Every relation one pair supports, in a fixed order.

    Containment is checked before overlap and overlap before direction, because
    the three are mutually exclusive readings of the same two boxes: a box inside
    another also "overlaps" it arithmetically, and reporting both would let a
    caller who reads the first relation it finds reach the wrong conclusion.
    """
    a, b = first.bbox, second.bbox
    subject, other = first, second
    if b.contains(a) and b.area > a.area:
        return [
            _relation(RelationshipKind.INSIDE, subject, other, f"{_box(a)} lies within {_box(b)}"),
            _relation(RelationshipKind.CONTAINS, other, subject, f"{_box(b)} encloses {_box(a)}"),
        ]
    if a.contains(b) and a.area > b.area:
        return [
            _relation(RelationshipKind.INSIDE, other, subject, f"{_box(b)} lies within {_box(a)}"),
            _relation(RelationshipKind.CONTAINS, subject, other, f"{_box(a)} encloses {_box(b)}"),
        ]
    shared = a.intersection(b)
    smaller = min(a.area, b.area)
    if shared and smaller and shared / smaller >= options.overlap_ratio:
        subject, other = _ordered_pair(first, second)
        return [_relation(RelationshipKind.OVERLAPS, subject, other, f"{shared}px shared")]
    if a.right <= b.x:
        return [
            _relation(RelationshipKind.LEFT_OF, subject, other, f"a.right={a.right} <= b.x={b.x}"),
            _relation(RelationshipKind.RIGHT_OF, other, subject, f"b.right={b.right} > a.x={a.x}"),
        ]
    if b.right <= a.x:
        return [
            _relation(RelationshipKind.RIGHT_OF, subject, other, f"a.x={a.x} >= b.right={b.right}"),
            _relation(RelationshipKind.LEFT_OF, other, subject, f"b.right={b.right} <= a.x={a.x}"),
        ]
    if a.bottom <= b.y:
        return [
            _relation(RelationshipKind.ABOVE, subject, other, f"a.bottom={a.bottom} <= b.y={b.y}"),
            _relation(RelationshipKind.BELOW, other, subject, f"b.bottom={b.bottom} > a.y={a.y}"),
        ]
    if b.bottom <= a.y:
        return [
            _relation(RelationshipKind.BELOW, subject, other, f"a.y={a.y} >= b.bottom={b.bottom}"),
            _relation(RelationshipKind.ABOVE, other, subject, f"b.bottom={b.bottom} <= a.y={a.y}"),
        ]
    nearest = max(a.width, a.height, b.width, b.height)
    if options.near_ratio and nearest and a.gap_to(b) <= options.near_ratio * nearest:
        subject, other = _ordered_pair(first, second)
        return [
            _relation(
                RelationshipKind.NEAR,
                subject,
                other,
                f"edge gap {a.gap_to(b):.1f}px <= {options.near_ratio * nearest:.1f}px",
            )
        ]
    return []


def _ordered_pair(
    first: DetectedObject, second: DetectedObject
) -> tuple[DetectedObject, DetectedObject]:
    """A symmetric pair in a stable order, so it is reported exactly once."""
    if (first.object_id, first.label) <= (second.object_id, second.label):
        return first, second
    return second, first


def _relation(
    kind: RelationshipKind, subject: DetectedObject, other: DetectedObject, evidence: str
) -> Relationship:
    """One relation, with the provenance and the inherited confidence."""
    return Relationship(
        kind=kind,
        subject_id=subject.object_id,
        object_id=other.object_id,
        confidence=_shared_confidence(subject.confidence, other.confidence),
        provenance=f"geometry:{subject.source or 'unknown'}+{other.source or 'unknown'}",
        evidence=evidence,
    )


def _shared_confidence(left: float | None, right: float | None) -> float | None:
    """The weaker of two measured confidences, or nothing when either is unmeasured.

    Not the average: a relation cannot be more certain than the less certain box
    it was computed from, and an unmeasured input makes the result unmeasured
    rather than zero.
    """
    if left is None or right is None:
        return None
    return min(left, right)


def _box(box: BBox) -> str:
    """A box as a short arithmetic-friendly string, for evidence text."""
    return f"({box.x},{box.y} {box.width}x{box.height})"

