"""Bounded state reasoning: deterministic rules over the structured state (§15).

This is not an agent and not a model. It is a small table of named rules, each of
which reads the state and returns one conclusion with the evidence behind it:

    spatial_relation      is A near/inside/left of B, according to the geometry
    last_seen             where and when an entity was last observed
    stale                 which facts the evidence stopped supporting
    conflict              which observations disagree with each other
    uncertainty           what the state does not know, and why
    provisional_identity  which identities are guesses the state admits to
    evidence              why a particular claim is believed

The rule table is the single source of truth: the detector, the answer builders
and the generated tests all walk it, so a new rule is one row and cannot be
half-added. Every conclusion carries ``rule`` (which rule spoke), ``evidence``
(the observation ids), ``confidence`` (``None`` when nothing measured it) and
``limitations`` (what the rule could not check) — which is what makes a conclusion
reviewable instead of authoritative.

There is deliberately no field for private reasoning, no chain-of-thought and no
free-form model output. Nothing here decides to act: a conclusion is a statement
about what is known, and acting on it is another phase's business.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.perception.models import DetectedObject
from novacontrol.perception.spatial import derive_relationships
from novacontrol.world.memory import TemporalMemory
from novacontrol.world.models import (
    EntityStatus,
    ReasoningConclusion,
    RelationStatus,
    UncertaintyKind,
    WorldEntity,
    WorldRelationship,
    WorldState,
    as_text,
)

__all__ = [
    "REASONING_ACTIONS",
    "ReasoningRule",
    "answer",
    "reasoning_rules",
]

#: The actions a caller may name explicitly instead of phrasing a question. This
#: is the setting the API validates against, so an unknown action is refused by
#: name rather than silently becoming the default rule.
REASONING_ACTIONS: tuple[str, ...] = (
    "spatial_relation",
    "last_seen",
    "stale",
    "conflict",
    "uncertainty",
    "provisional_identity",
    "evidence",
)


@dataclass(frozen=True, slots=True)
class _RuleContext:
    """Everything one rule may read — passed in, never looked up."""

    question: str
    state: WorldState
    memory: TemporalMemory
    payload: Mapping[str, Any] = field(default_factory=dict)

    def subject(self) -> str:
        return as_text(self.payload.get("subject") or self.payload.get("entity_id"))

    def object(self) -> str:
        return as_text(self.payload.get("object") or self.payload.get("target"))

    def mentions(self) -> tuple[WorldEntity, ...]:
        return mentioned_entities(self.question, self.state)


@dataclass(frozen=True, slots=True)
class ReasoningRule:
    """One deterministic rule: how to recognise it, and what it concludes.

    ``examples`` are the phrases this rule answers — the same contract the
    scratch-intent registry uses, so the generated tests exercise the rule through
    the real detector.
    """

    name: str
    category: str
    patterns: tuple[str, ...]
    build: Callable[[_RuleContext], ReasoningConclusion]
    examples: tuple[str, ...] = ()
    description: str = ""
    limitations: tuple[str, ...] = ()


def reasoning_rules() -> tuple[ReasoningRule, ...]:
    """The rule table, in the order detection tries them."""
    return _RULES


def answer(
    question: Any,
    *,
    memory: TemporalMemory,
    payload: Mapping[str, Any] | None = None,
) -> tuple[ReasoningConclusion, ...]:
    """Every rule that recognises the request, or one honest "I cannot place this".

    A question no rule recognises is NOT answered with a guess: the single
    conclusion says which rules exist and that none of them matched, so a caller
    learns the vocabulary instead of receiving an invented fact.
    """
    state = memory.state
    if state is None:
        return (
            ReasoningConclusion(
                rule="no_state",
                conclusion=(
                    "this world has not been observed yet, so there is nothing to reason about"
                ),
                category="state",
                limitations=("no state exists",),
            ),
        )
    text = as_text(question)
    if isinstance(payload, Mapping) and payload.get("action"):
        wanted = as_text(payload.get("action"))
        for rule in _RULES:
            if rule.name == wanted:
                return (rule.build(_RuleContext(text, state, memory, payload)),)
        return (
            ReasoningConclusion(
                rule="unknown_action",
                conclusion=(
                    f"there is no reasoning rule named {wanted!r}; the rules are "
                    + ", ".join(item.name for item in _RULES)
                ),
                category="request",
                state_id=state.state_id,
                limitations=("the request named an action this build does not have",),
            ),
        )
    context = _RuleContext(text, state, memory, payload or {})
    lowered = text.lower()
    matched = [
        rule.build(context)
        for rule in _RULES
        if any(re.search(pattern, lowered) for pattern in rule.patterns)
    ]
    if matched:
        return tuple(matched)
    return (
        ReasoningConclusion(
            rule="unmatched",
            conclusion=(
                "no state rule recognises that question; this layer answers questions "
                "about what is known, what changed, what is uncertain and what the "
                "evidence is — it does not reason about anything else"
            ),
            category="request",
            state_id=state.state_id,
            limitations=(
                "the known question shapes are: "
                + "; ".join(rule.examples[0] for rule in _RULES if rule.examples),
            ),
        ),
    )


def mentioned_entities(question: str, state: WorldState) -> tuple[WorldEntity, ...]:
    """Entities the question names, in the order they appear in it.

    Deterministic and literal: a label must occur as a word in the question. There
    is no fuzzy matching here, because "which entity did the person mean" is a
    question this layer cannot answer and must not pretend to.
    """
    lowered = question.lower()
    found: list[tuple[int, WorldEntity]] = []
    for entity in state.entities:
        if entity.status is EntityStatus.EXPIRED:
            continue
        for label in entity.labels:
            name = label.lower().strip()
            if not name:
                continue
            position = _word_position(lowered, name)
            if position >= 0:
                found.append((position, entity))
                break
    found.sort(key=lambda row: (row[0], row[1].entity_id))
    ordered: list[WorldEntity] = []
    for _, entity in found:
        if entity.entity_id not in [item.entity_id for item in ordered]:
            ordered.append(entity)
    return tuple(ordered)


def _word_position(haystack: str, needle: str) -> int:
    """Where a label occurs as a whole word, or -1."""
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")
    match = pattern.search(haystack)
    return match.start() if match else -1


# ── the rules ────────────────────────────────────────────────────────────────


def _spatial_relation(context: _RuleContext) -> ReasoningConclusion:
    """What the geometry says about two named entities — derived, not remembered."""
    mentions = context.mentions()
    subject = _find(context, context.subject()) or (mentions[0] if mentions else None)
    target = _find(context, context.object()) or _second(mentions, subject)
    if subject is None or target is None:
        return _conclusion(
            "spatial_relation",
            context,
            "spatial",
            "no two known entities were named, so no relation between them can be checked",
            limitations=(
                "the rule needs two entity labels that the current state knows",
            ),
        )
    relations = _between(context.state, subject, target)
    if not relations:
        return _conclusion(
            "spatial_relation",
            context,
            "spatial",
            (
                f"the geometry supports no relation between "
                f"{subject.label or subject.entity_id} and {target.label or target.entity_id}"
            ),
            evidence=(*subject.evidence, *target.evidence),
            confidence=None,
            limitations=(
                "no stored relationship covers this pair and their boxes do not support one",
            ),
        )
    described = "; ".join(
        f"{item.kind.value} ({item.detail or 'geometry'})" for item in relations
    )
    return _conclusion(
        "spatial_relation",
        context,
        "spatial",
        (
            f"{subject.label or subject.entity_id} and {target.label or target.entity_id}: "
            f"{described}"
        ),
        evidence=tuple(
            dict.fromkeys(
                (
                    *subject.evidence,
                    *target.evidence,
                    *(item for row in relations for item in row.evidence),
                )
            )
        ),
        confidence=_weakest(relations),
        limitations=(
            "a spatial relation is a statement about a moment, not about the world in general",
        ),
    )


def _last_seen(context: _RuleContext) -> ReasoningConclusion:
    """Where and when an entity was last observed — from the record, not from now."""
    mentions = context.mentions()
    entity = _find(context, context.subject()) or (mentions[0] if mentions else None)
    if entity is None:
        return _conclusion(
            "last_seen",
            context,
            "state",
            "no entity was named that this state knows, so nothing was last seen",
            limitations=("the rule needs an entity label or id the current state holds",),
        )
    position = (
        entity.bbox.to_dict() if entity.bbox is not None else None
    )
    observed_at = entity.last_seen or "an unrecorded time"
    answer_text = (
        f"{entity.label or entity.entity_id} was last observed at {observed_at}"
        + (f" at {position}" if position else " without a recorded position")
    )
    if entity.status is not EntityStatus.PRESENT:
        answer_text += (
            f"; the state currently holds it as {entity.status.value} "
            f"after {entity.missed_observations} missed observation(s)"
        )
    return _conclusion(
        "last_seen",
        context,
        "state",
        answer_text,
        evidence=tuple(entity.evidence),
        confidence=entity.identity_confidence,
        limitations=(
            ("the entity's identity is provisional",) if entity.provisional else ()
        )
        + (
            ("the entity's facts are stale: the latest observations do not include it",)
            if entity.status is not EntityStatus.PRESENT
            else ()
        ),
    )


def _stale(context: _RuleContext) -> ReasoningConclusion:
    """Facts the evidence stopped supporting — absence or a lapsed relation."""
    stale_entities = [
        item
        for item in context.state.entities
        if item.status in {EntityStatus.NOT_OBSERVED, EntityStatus.MISSING, EntityStatus.EXPIRED}
    ]
    stale_relations = [
        item for item in context.state.relationships if item.status is not RelationStatus.ACTIVE
    ]
    if not stale_entities and not stale_relations:
        return _conclusion(
            "stale",
            context,
            "state",
            "nothing in the current state has gone stale",
            limitations=("a fact is only stale once the evidence stops supporting it",),
        )
    parts: list[str] = []
    for item in stale_entities[:8]:
        parts.append(
            f"{item.label or item.entity_id} is {item.status.value} "
            f"(last seen {item.last_seen or 'unknown'})"
        )
    for relation in stale_relations[:8]:
        parts.append(
            f"{relation.kind.value} between {relation.source_entity_id} and "
            f"{relation.target_entity_id} is {relation.status.value}"
        )
    return _conclusion(
        "stale",
        context,
        "state",
        "; ".join(parts),
        evidence=tuple(
            dict.fromkeys(
                [
                    *(item for row in stale_entities for item in row.evidence),
                    *(item for row in stale_relations for item in row.evidence),
                ]
            )
        ),
        confidence=None,
        limitations=(
            "stale is not false: it means the most recent evidence did not repeat the fact",
        ),
    )


def _conflict(context: _RuleContext) -> ReasoningConclusion:
    """Observations that disagree — kept as conflicts rather than resolved by fiat."""
    rows = [
        item
        for item in context.state.uncertainty
        if item.kind in {UncertaintyKind.CONFLICTING, UncertaintyKind.OUT_OF_ORDER}
    ]
    if not rows:
        return _conclusion(
            "conflict",
            context,
            "uncertainty",
            "no observations in the current state disagree with each other",
        )
    return _conclusion(
        "conflict",
        context,
        "uncertainty",
        "; ".join(item.detail for item in rows[:6]),
        evidence=tuple(item for row in rows for item in row.evidence),
        confidence=None,
        limitations=(
            "the state keeps both readings and resolves neither: a newer observation "
            "wins the stored value, and the disagreement stays recorded",
        ),
    )


def _uncertainty(context: _RuleContext) -> ReasoningConclusion:
    """Everything the state knows it does not know, with the kind of doubt named."""
    rows = list(context.state.uncertainty)
    if not rows:
        return _conclusion(
            "uncertainty",
            context,
            "uncertainty",
            "the current state records no open uncertainty",
            limitations=(
                "an empty list means nothing was recorded as uncertain, not that every "
                "fact is certain",
            ),
        )
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
    described = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    return _conclusion(
        "uncertainty",
        context,
        "uncertainty",
        f"{len(rows)} open uncertainty record(s): {described}",
        evidence=tuple(item for row in rows for item in row.evidence),
        confidence=None,
        limitations=(
            "each record names the kind of doubt; none of them is a judgement about truth",
        ),
    )


def _provisional_identity(context: _RuleContext) -> ReasoningConclusion:
    """Entities whose continuity the state admits it is guessing."""
    rows = [
        item
        for item in context.state.entities
        if item.provisional and item.status is not EntityStatus.EXPIRED
    ]
    if not rows:
        return _conclusion(
            "provisional_identity",
            context,
            "identity",
            "no entity in the current state carries a provisional identity",
        )
    return _conclusion(
        "provisional_identity",
        context,
        "identity",
        "; ".join(
            f"{item.label or item.entity_id} is provisional"
            + (
                f" (identity confidence {item.identity_confidence:.2f})"
                if item.identity_confidence is not None
                else " (identity confidence was not measured)"
            )
            for item in rows[:8]
        ),
        evidence=tuple(item for row in rows for item in row.evidence),
        confidence=None,
        limitations=(
            "a provisional identity may be the same thing seen twice or two things that "
            "look alike; the state keeps the question open instead of choosing",
        ),
    )


def _evidence(context: _RuleContext) -> ReasoningConclusion:
    """Why a claim is believed: the references, the basis and the limits."""
    mentions = context.mentions()
    wanted = _find(context, context.subject()) or (mentions[0] if mentions else None)
    if wanted is None:
        return _conclusion(
            "evidence",
            context,
            "evidence",
            "no entity was named whose evidence could be shown",
            limitations=("the rule needs an entity label or id the current state holds",),
        )
    lines: list[str] = []
    for attribute in wanted.attributes[:6]:
        lines.append(f"{attribute.name}={attribute.value!r} ({attribute.basis.value})")
    relations = [
        item
        for item in context.state.relationships
        if wanted.entity_id in item.pair() and item.status is RelationStatus.ACTIVE
    ]
    for relation in relations[:4]:
        other = (
            relation.target_entity_id
            if relation.source_entity_id == wanted.entity_id
            else relation.source_entity_id
        )
        lines.append(f"{relation.kind.value} with {other} ({relation.relation_source.value})")
    return _conclusion(
        "evidence",
        context,
        "evidence",
        (
            f"{wanted.label or wanted.entity_id} is held from {len(wanted.evidence)} "
            f"observation(s): " + ("; ".join(lines) if lines else "no attributes are recorded")
        ),
        evidence=(*wanted.evidence, *(item for row in relations for item in row.evidence)),
        confidence=wanted.identity_confidence,
        limitations=(
            "evidence references name observations; their content lives with the "
            "subsystem that produced them",
        )
        + (("the entity's identity is provisional",) if wanted.provisional else ()),
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _conclusion(
    rule: str,
    context: _RuleContext,
    category: str,
    conclusion: str,
    *,
    evidence: Sequence[str] = (),
    confidence: float | None = None,
    limitations: Sequence[str] = (),
) -> ReasoningConclusion:
    return ReasoningConclusion(
        rule=rule,
        conclusion=conclusion,
        category=category,
        evidence=tuple(dict.fromkeys(item for item in evidence if item))[:16],
        confidence=confidence,
        limitations=tuple(limitations),
        state_id=context.state.state_id,
        subject=context.subject(),
    )


def _find(context: _RuleContext, wanted: str) -> WorldEntity | None:
    """An entity by id or label, from a request field — never fuzzy."""
    text = as_text(wanted)
    if not text:
        return None
    direct = context.state.entity(text)
    if direct is not None:
        return direct
    matches = context.state.find(text)
    if matches:
        return sorted(matches, key=lambda item: item.entity_id)[0]
    return None


def _second(mentions: Sequence[WorldEntity], first: WorldEntity | None) -> WorldEntity | None:
    for item in mentions:
        if first is None or item.entity_id != first.entity_id:
            return item
    return None


def _between(
    state: WorldState, subject: WorldEntity, target: WorldEntity
) -> tuple[WorldRelationship, ...]:
    """Stored relations for a pair, or the geometry derived for it right now.

    The fallback is Phase 21's own relationship derivation applied to the two
    entity boxes — the arithmetic is reused rather than rewritten here, which is
    what keeps this rule and the perception layer from disagreeing about "left of".
    """
    stored = tuple(
        item
        for item in state.relationships
        if item.status is RelationStatus.ACTIVE
        and (
            (item.source_entity_id, item.target_entity_id)
            in {
                (subject.entity_id, target.entity_id),
                (target.entity_id, subject.entity_id),
            }
        )
    )
    if stored:
        return stored
    if subject.bbox is None or target.bbox is None:
        return ()
    if not subject.bbox.has_extent or not target.bbox.has_extent:
        return ()
    report = derive_relationships(
        [
            DetectedObject(
                label=subject.label, bbox=subject.bbox, object_id=subject.entity_id
            ),
            DetectedObject(
                label=target.label, bbox=target.bbox, object_id=target.entity_id
            ),
        ]
    )
    from novacontrol.world.relationships import _PERCEPTION_KINDS

    rows: list[WorldRelationship] = []
    for item in report.relationships:
        kind = _PERCEPTION_KINDS.get(item.kind.value)
        if kind is None:
            continue
        rows.append(
            WorldRelationship(
                kind=kind,
                source_entity_id=item.subject_id,
                target_entity_id=item.object_id,
                confidence=item.confidence,
                detail=item.evidence,
                evidence=(),
            )
        )
    return tuple(rows)


def _weakest(relations: Sequence[WorldRelationship]) -> float | None:
    """The confidence of the least certain relation reported, or ``None``."""
    measured = [item.confidence for item in relations if item.confidence is not None]
    if not measured:
        return None
    return min(measured)


_RULES: tuple[ReasoningRule, ...] = (
    ReasoningRule(
        name="spatial_relation",
        category="spatial",
        patterns=(
            r"\b(left of|right of|above|below|near|next to|inside|contains|relative to)\b",
            r"\bwhere is\b.+\bcompared\b",
        ),
        build=_spatial_relation,
        examples=(
            "is the phone left of the laptop",
            "where is the phone relative to the laptop",
        ),
        description="What the geometry says about the relation between two entities.",
        limitations=("a spatial relation is a claim about a moment, not about the world",),
    ),
    ReasoningRule(
        name="last_seen",
        category="state",
        patterns=(
            r"\bwhere (was|is)\b.*\blast\b",
            r"\bwhen was\b.*\b(last|seen)\b",
            r"\blast (observed|seen)\b",
        ),
        build=_last_seen,
        examples=(
            "where was the phone last observed",
            "when was the laptop last seen",
        ),
        description="Where and when an entity was last observed, with its staleness.",
        limitations=("the record is only as fresh as the last observation",),
    ),
    ReasoningRule(
        name="stale",
        category="state",
        patterns=(
            r"\b(stale|expired|no longer|out of date)\b",
            r"\bwhat (is|has) gone\b",
        ),
        build=_stale,
        examples=("what is stale", "which relationships are no longer supported"),
        description="Facts the latest evidence stopped supporting.",
        limitations=("a stale fact was true when it was recorded; stale is not false",),
    ),
    ReasoningRule(
        name="conflict",
        category="uncertainty",
        patterns=(r"\b(conflict|disagree|contradict)\w*\b",),
        build=_conflict,
        examples=("do any observations conflict", "what observations disagree"),
        description="Observations that disagree about a fact.",
        limitations=("the state records the disagreement rather than resolving it",),
    ),
    ReasoningRule(
        name="uncertainty",
        category="uncertainty",
        patterns=(
            r"\b(uncertain|unsure|not sure|unknown)\w*\b",
            r"\bwhat do (you|we) not know\b",
            r"\bhow confident\b",
        ),
        build=_uncertainty,
        examples=("what is uncertain", "what do you not know"),
        description="Everything the state knows it does not know, by kind.",
        limitations=("only recorded doubts appear here",),
    ),
    ReasoningRule(
        name="provisional_identity",
        category="identity",
        patterns=(
            r"\b(provisional|ambiguous|guess)\w*\b",
            r"\bwhich identities\b",
            r"\bmight be the same\b",
        ),
        build=_provisional_identity,
        examples=("which identities are provisional", "is any identity ambiguous"),
        description="Entities whose identity continuity is a stated guess.",
        limitations=("a provisional identity may be one thing or two",),
    ),
    ReasoningRule(
        name="evidence",
        category="evidence",
        patterns=(
            r"\bwhy (do|does|is|are)\b",
            r"\bwhat evidence\b",
            r"\bhow (do|can) (you|we) know\b",
        ),
        build=_evidence,
        examples=("why is the laptop believed to be here", "what evidence supports the laptop"),
        description="The references, basis and limits behind a claim.",
        limitations=("evidence points at observations; their content is not copied here",),
    ),
)
