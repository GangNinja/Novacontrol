"""Entity identity: continuity when the evidence supports it, uncertainty when it does not (§6).

The central refusal of this module is that **a label is not an identity**. Two
things called "laptop" are two laptops until something else says otherwise, and
"something else" means one of exactly three things, in order of strength:

1. **The source said so.** A Phase 21 track id, an object id from a tool — an
   identifier that a source reported and that this layer has already seen. This is
   the only match that is treated as confident, and even it is a HINT: a source
   that reuses ids is a source whose ids are not identity.
2. **The geometry agrees, AND the label agrees.** Overlapping boxes with the same
   label is one thing seen twice. Overlapping boxes with different labels is two
   things, and proximity with the same label is only ever a *provisional* match.
3. **Nothing agrees** — which produces a NEW identity, not a guess.

When two candidates are equally good, the estimator does not pick the first: it
records an ``ambiguous`` decision, keeps the entity ``provisional``, and lists the
candidates. A provisional identity is a real entity with honest continuity — it
may later be merged, and it is never silently treated as certain.

Merging is where the out-of-order policy lives. An attribute from an observation
that is OLDER than the attribute already stored does not overwrite it: the newer
value survives and the disagreement is returned so the caller can record it as a
conflict. That is the difference between "this fact was corrected" and "an old
file was replayed".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.perception.models import BBox
from novacontrol.world.models import (
    EntityAttribute,
    EntityStatus,
    FactBasis,
    ObservedEntity,
    StateEstimate,
    UncertaintyKind,
    UncertaintyRecord,
    WorldEntity,
    as_text,
)
from novacontrol.world.timeutil import is_newer

__all__ = [
    "IdentityPolicy",
    "active_entities",
    "confirm_absence",
    "expire_stale",
    "merge_entity",
    "resolve_identity",
]

#: How much history one entity carries. Bounded because a state that grows with
#: every observation is a state that eventually cannot be saved.
MAX_EVIDENCE_PER_ENTITY = 20
MAX_SOURCE_IDS_PER_ENTITY = 10
MAX_LABELS_PER_ENTITY = 6


@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    """The thresholds identity resolution uses — one place, stated in numbers.

    ``ambiguous_margin`` is the field that makes the difference between a
    confident match and a guess: when the runner-up candidate scores within this
    margin of the best, the match is recorded as ambiguous and the identity
    becomes provisional instead of being asserted.
    """

    iou_threshold: float = 0.35
    distance_px: int = 40
    ambiguous_margin: float = 0.25
    require_label_agreement: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> IdentityPolicy:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def number(key: str, default: float, *, low: float, high: float) -> float:
            try:
                value = float(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        distance = data.get("identity_distance_px", defaults.distance_px)
        try:
            distance_value = int(distance)
        except (TypeError, ValueError):
            distance_value = defaults.distance_px
        return cls(
            iou_threshold=number(
                "identity_iou_threshold", defaults.iou_threshold, low=0.0, high=1.0
            ),
            distance_px=max(0, distance_value),
            ambiguous_margin=number(
                "identity_ambiguous_margin", defaults.ambiguous_margin, low=0.0, high=1.0
            ),
            require_label_agreement=bool(
                data.get("identity_require_label", defaults.require_label_agreement)
            ),
        )


def active_entities(entities: Iterable[WorldEntity]) -> tuple[WorldEntity, ...]:
    """Entities still asserted as part of the world (not expired)."""
    return tuple(item for item in entities if item.status is not EntityStatus.EXPIRED)


def resolve_identity(
    observed: ObservedEntity,
    existing: Sequence[WorldEntity],
    *,
    policy: IdentityPolicy | None = None,
    timestamp: str = "",
) -> StateEstimate:
    """Which known entity (if any) this observation is about — and how sure we are."""
    settings = policy or IdentityPolicy()
    candidates = [item for item in existing if item.status is not EntityStatus.EXPIRED]

    hint = as_text(observed.entity_id) or as_text(observed.track_id)
    if hint:
        for item in candidates:
            if item.entity_id == hint or hint in item.source_ids:
                return StateEstimate(
                    decision="matched",
                    entity_id=item.entity_id,
                    matched_entity_id=item.entity_id,
                    label=observed.label or item.label,
                    reason="the source reported an identity this state has already seen",
                    confidence=0.9,
                )

    scored: list[tuple[float, WorldEntity, str]] = []
    for item in candidates:
        labels_disagree = (
            settings.require_label_agreement
            and observed.label
            and item.label
            and observed.label.lower() != item.label.lower()
        )
        types_disagree = (
            settings.require_label_agreement
            and observed.entity_type
            and item.entity_type
            and observed.entity_type != item.entity_type
        )
        if labels_disagree or types_disagree:
            # A different label is a different thing, even in the same place.
            # (A re-labelled object is a new entity; the state says so rather
            # than silently rewriting what a past observation meant.)
            continue
        score, reason = _geometry_score(observed.bbox, item.bbox, settings)
        if score > 0.0:
            scored.append((score, item, reason))

    if not scored:
        return StateEstimate(
            decision="new_entity",
            label=observed.label,
            reason=(
                "no known entity agrees with this one's identity hints or geometry"
                if hint or observed.label
                else "the observation carried no label, id or geometry to match on"
            ),
            confidence=None,
        )

    scored.sort(key=lambda row: (-row[0], row[1].entity_id))
    best_score, best, best_reason = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    if runner_up is not None and (best_score - runner_up[0]) < settings.ambiguous_margin:
        return StateEstimate(
            decision="ambiguous",
            entity_id=best.entity_id,
            matched_entity_id=best.entity_id,
            label=observed.label or best.label,
            reason=(
                f"{len(scored)} known entities match about as well "
                f"({best.entity_id} at {best_score:.2f}, {runner_up[1].entity_id} at "
                f"{runner_up[0]:.2f}); the identity is kept provisional"
            ),
            confidence=max(0.2, min(0.5, best_score)),
            candidates=tuple(item.entity_id for _, item, _ in scored),
        )
    confidence = 0.5 + 0.4 * min(1.0, best_score)
    if best_score < 0.6:
        confidence = min(confidence, 0.65)
    return StateEstimate(
        decision="matched",
        entity_id=best.entity_id,
        matched_entity_id=best.entity_id,
        label=observed.label or best.label,
        reason=best_reason,
        confidence=min(0.9, confidence),
        candidates=tuple(item.entity_id for _, item, _ in scored),
    )


def _geometry_score(
    observed_box: BBox | None, known_box: BBox | None, policy: IdentityPolicy
) -> tuple[float, str]:
    """How much two boxes agree, in [0, 1]. Zero means "no agreement at all".

    Two boxes that cannot be measured score zero — not 0.5 — because "I have no
    geometry" is not weak evidence for a match, it is the absence of evidence,
    and a default would let a label alone resolve an identity.
    """
    if observed_box is None or known_box is None:
        return 0.0, ""
    if not observed_box.has_extent or not known_box.has_extent:
        return 0.0, ""
    overlap = observed_box.iou(known_box)
    if overlap >= policy.iou_threshold:
        return overlap, (
            f"the boxes overlap ({observed_box.to_dict()} vs {known_box.to_dict()}, "
            f"IoU {overlap:.2f})"
        )
    gap = observed_box.gap_to(known_box)
    if gap <= policy.distance_px and policy.distance_px > 0:
        closeness = 1.0 - (gap / float(policy.distance_px))
        return max(0.0, closeness) * 0.7, (
            f"the boxes are {gap:.0f}px apart, within the {policy.distance_px}px "
            "continuity window"
        )
    return 0.0, ""


def merge_entity(
    existing: WorldEntity,
    observed: ObservedEntity,
    *,
    estimate: StateEstimate,
    timestamp: str,
    evidence_id: str,
    scope: str = "",
) -> tuple[WorldEntity, tuple[UncertaintyRecord, ...]]:
    """The next version of one entity after an observation agreed it is the same thing.

    Returns the entity AND the uncertainty rows the merge itself produced: an
    out-of-order attribute is a real disagreement, and the caller records it rather
    than the merge silently choosing a winner.
    """
    conflicts: list[UncertaintyRecord] = []
    attributes, attribute_conflicts = _merge_attributes(
        existing.attributes, observed.attributes, timestamp=timestamp, evidence_id=evidence_id
    )
    conflicts.extend(attribute_conflicts)

    comparable_newer = is_newer(timestamp, existing.last_seen)
    newer = comparable_newer is not False
    unordered = (
        comparable_newer is None
        and bool(existing.last_seen)
        and bool(timestamp)
        and timestamp != existing.last_seen
    )
    if unordered:
        # Two stamps that cannot be ordered (a missing or non-ISO time). The fact
        # is applied — it was just delivered — but the state says the ORDER is
        # unknown rather than implying it knows which came first.
        conflicts.append(
            UncertaintyRecord(
                subject=existing.entity_id,
                kind=UncertaintyKind.OUT_OF_ORDER,
                detail=(
                    "the observation's time could not be compared with the stored one, "
                    "so it was applied without establishing which came first"
                ),
                confidence=None,
                evidence=(evidence_id,),
                recorded_at=timestamp,
            )
        )

    bbox = existing.bbox
    if observed.bbox is not None and newer:
        bbox = observed.bbox

    labels = _merge_labels(existing.labels, observed.label)
    last_seen = _latest(existing.last_seen, timestamp)
    last_confirmed = last_seen if bbox is not None or labels else existing.last_confirmed

    return (
        WorldEntity(
            entity_id=existing.entity_id,
            entity_type=observed.entity_type or existing.entity_type,
            labels=labels,
            attributes=attributes,
            bbox=bbox,
            status=EntityStatus.PRESENT,
            first_seen=existing.first_seen or timestamp,
            last_seen=last_seen,
            last_confirmed=last_confirmed,
            missed_observations=0,
            observed_count=existing.observed_count + 1,
            evidence=_append_bounded(existing.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY),
            identity_confidence=estimate.confidence
            if estimate.confidence is not None
            else existing.identity_confidence,
            provisional=estimate.decision == "ambiguous",
            provenance=_append_bounded(
                existing.provenance, observed.source, MAX_SOURCE_IDS_PER_ENTITY
            ),
            source_ids=_append_bounded(
                existing.source_ids,
                observed.entity_id or observed.track_id,
                MAX_SOURCE_IDS_PER_ENTITY,
            ),
            # An entity created unscoped adopts the scope it is first seen in;
            # one that already has a scope keeps it, so a region-scoped entity is
            # never silently re-scoped by an unrelated observation.
            scope=existing.scope or scope,
        ),
        tuple(conflicts),
    )


def _merge_attributes(
    existing: Sequence[EntityAttribute],
    observed: Sequence[EntityAttribute],
    *,
    timestamp: str,
    evidence_id: str,
) -> tuple[tuple[EntityAttribute, ...], tuple[UncertaintyRecord, ...]]:
    """Attribute-by-attribute reconciliation, newer facts winning and conflicts reported."""
    by_name: dict[str, EntityAttribute] = {item.name: item for item in existing}
    order: list[str] = [item.name for item in existing]
    conflicts: list[UncertaintyRecord] = []
    for item in observed:
        name = item.name
        if not name:
            continue
        previous = by_name.get(name)
        incoming = EntityAttribute(
            name=name,
            value=item.value,
            basis=item.basis,
            confidence=item.confidence,
            evidence=_append_bounded(item.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY),
            observed_at=item.observed_at or timestamp,
            source=item.source,
        )
        if previous is None:
            by_name[name] = incoming
            order.append(name)
            continue
        newer = is_newer(incoming.observed_at, previous.observed_at)
        if newer is False:
            # The stored value is the more recent one. Keep it, keep the incoming
            # fact as evidence on the stored row, and REPORT the disagreement.
            by_name[name] = EntityAttribute(
                name=previous.name,
                value=previous.value,
                basis=previous.basis,
                confidence=previous.confidence,
                evidence=_append_bounded(
                    previous.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY
                ),
                observed_at=previous.observed_at,
                source=previous.source,
            )
            conflicts.append(
                UncertaintyRecord(
                    subject=previous.name,
                    kind=UncertaintyKind.CONFLICTING,
                    detail=(
                        f"an observation from {incoming.observed_at or 'unknown time'} "
                        f"reported {name}={incoming.value!r}, but the state already holds "
                        f"{previous.value!r} from a later observation"
                    ),
                    confidence=incoming.confidence,
                    evidence=(evidence_id,),
                    recorded_at=timestamp,
                )
            )
            continue
        if previous.value != incoming.value and previous.basis is not incoming.basis:
            # Same time, different provenance: the stored fact was inferred and
            # the incoming one was observed, so the observed one wins — and the
            # change of basis is stated rather than implied.
            by_name[name] = incoming
            continue
        by_name[name] = incoming
    return tuple(by_name[name] for name in dict.fromkeys(order)), tuple(conflicts)


def _merge_labels(existing: Sequence[str], label: str) -> tuple[str, ...]:
    """Labels in a stable order, bounded — the first is the entity's own name."""
    names = [item for item in existing if item]
    incoming = as_text(label)
    if incoming and incoming not in names:
        names.append(incoming)
    return tuple(names[:MAX_LABELS_PER_ENTITY])


def _append_bounded(values: Sequence[str], value: str, ceiling: int) -> tuple[str, ...]:
    """Append a non-empty string, keeping the newest ``ceiling`` entries."""
    text = as_text(value)
    if not text:
        return tuple(values)
    merged = [*values, text]
    return tuple(merged[-ceiling:])


def _latest(current: str, candidate: str) -> str:
    """The later of two timestamps, falling back to the candidate when unsure."""
    if not candidate:
        return current
    if not current:
        return candidate
    newer = is_newer(candidate, current)
    return candidate if newer is not False else current


def confirm_absence(
    entity: WorldEntity,
    *,
    complete: bool,
    scope: str,
    missing_after: int,
    timestamp: str,
    evidence_id: str,
) -> tuple[WorldEntity, str | None, UncertaintyRecord | None]:
    """What an observation that did NOT include this entity means for it (§5/§10).

    Three-valued on purpose:

    * the observation was not complete, or covers a different scope — nothing is
      concluded, and the entity is left exactly as it was (a screenshot of one
      window is not evidence about another);
    * the observation was complete but this is the first miss — ``NOT_OBSERVED``,
      which is the ABSENCE OF A DETECTION and not a conclusion;
    * the observation was complete and the miss threshold was reached —
      ``MISSING``, which IS a conclusion, and therefore its own transition kind.

    Returns ``(entity, transition_kind, uncertainty)`` with kinds as strings so the
    caller names them in one place.
    """
    if not complete or (scope and entity.scope and scope != entity.scope):
        return entity, None, None
    missed = entity.missed_observations + 1
    if missed < max(1, missing_after):
        return (
            WorldEntity(
                entity_id=entity.entity_id,
                entity_type=entity.entity_type,
                labels=entity.labels,
                attributes=entity.attributes,
                bbox=entity.bbox,
                status=EntityStatus.NOT_OBSERVED,
                first_seen=entity.first_seen,
                last_seen=entity.last_seen,
                last_confirmed=entity.last_confirmed,
                missed_observations=missed,
                observed_count=entity.observed_count,
                evidence=_append_bounded(
                    entity.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY
                ),
                identity_confidence=entity.identity_confidence,
                provisional=entity.provisional,
                provenance=entity.provenance,
                source_ids=entity.source_ids,
                scope=entity.scope,
            ),
            "entity_not_observed",
            None,
        )
    already_concluded = entity.status is EntityStatus.MISSING
    return (
        WorldEntity(
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            labels=entity.labels,
            attributes=entity.attributes,
            bbox=entity.bbox,
            status=EntityStatus.MISSING,
            first_seen=entity.first_seen,
            last_seen=entity.last_seen,
            last_confirmed=entity.last_confirmed,
            missed_observations=missed,
            observed_count=entity.observed_count,
            evidence=_append_bounded(entity.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY),
            identity_confidence=entity.identity_confidence,
            provisional=entity.provisional,
            provenance=entity.provenance,
            source_ids=entity.source_ids,
            scope=entity.scope,
        ),
        None if already_concluded else "entity_confirmed_missing",
        None,
    )


def expire_stale(
    entities: Sequence[WorldEntity],
    *,
    ttl_seconds: int,
    now: str,
    evidence_id: str,
) -> tuple[tuple[WorldEntity, ...], tuple[str, ...]]:
    """Retire entities nobody has seen for longer than the retention window.

    Retirement is a STATUS, not a deletion: the entity stays in the state with
    its history and evidence, because "we watched a laptop here for an hour" is
    something a later phase may legitimately ask about. Removing it would make the
    state claim it was never there.
    """
    if ttl_seconds <= 0:
        return tuple(entities), ()
    expired: list[str] = []
    result: list[WorldEntity] = []
    for entity in entities:
        if entity.status is EntityStatus.EXPIRED:
            result.append(entity)
            continue
        reference = entity.last_seen or entity.first_seen
        age = None
        if reference:
            from novacontrol.world.timeutil import seconds_between

            age = seconds_between(reference, now)
        if age is not None and age > ttl_seconds:
            expired.append(entity.entity_id)
            result.append(
                WorldEntity(
                    entity_id=entity.entity_id,
                    entity_type=entity.entity_type,
                    labels=entity.labels,
                    attributes=entity.attributes,
                    bbox=entity.bbox,
                    status=EntityStatus.EXPIRED,
                    first_seen=entity.first_seen,
                    last_seen=entity.last_seen,
                    last_confirmed=entity.last_confirmed,
                    missed_observations=entity.missed_observations,
                    observed_count=entity.observed_count,
                    evidence=_append_bounded(
                        entity.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY
                    ),
                    identity_confidence=entity.identity_confidence,
                    provisional=entity.provisional,
                    provenance=entity.provenance,
                    source_ids=entity.source_ids,
                    scope=entity.scope,
                )
            )
            continue
        result.append(entity)
    return tuple(result), tuple(expired)


def entity_from_observation(
    observed: ObservedEntity, *, timestamp: str, evidence_id: str, scope: str
) -> WorldEntity:
    """A brand-new entity built from the observation that introduced it.

    ``identity_confidence`` is ``None``: nothing was matched, so nothing was
    measured. A new identity is not a confident identity, and saying 1.0 here
    would make a fresh entity look better established than a matched one.
    """
    attributes = [
        EntityAttribute(
            name=item.name,
            value=item.value,
            basis=item.basis if item.basis is not FactBasis.UNKNOWN else FactBasis.OBSERVED,
            confidence=item.confidence,
            evidence=_append_bounded(item.evidence, evidence_id, MAX_EVIDENCE_PER_ENTITY),
            observed_at=item.observed_at or timestamp,
            source=item.source,
        )
        for item in observed.attributes
        if item.name
    ]
    return WorldEntity(
        entity_type=observed.entity_type or "object",
        labels=(observed.label,) if observed.label else (),
        attributes=tuple(attributes),
        bbox=observed.bbox,
        status=EntityStatus.PRESENT,
        first_seen=timestamp,
        last_seen=timestamp,
        last_confirmed=timestamp,
        missed_observations=0,
        observed_count=1,
        evidence=(evidence_id,) if evidence_id else (),
        identity_confidence=None,
        provisional=False,
        provenance=(observed.source,) if observed.source else (),
        source_ids=tuple(
            item for item in (observed.entity_id or observed.track_id,) if item
        ),
        scope=scope,
    )
