"""Confidence scoring: several pieces of evidence, not one similarity number.

A single score is easy to produce and easy to fool. TF-IDF similarity says
"which programs are eating my memory" is close to *memory_status* — but it says
the same thing about a request that is one word away from two different
intents, and it says nothing at all about whether the entity the tool needs was
actually found. Trusting it alone is how "open it" gets executed as confidently
as "open chrome".

This module combines the evidence the pipeline already has:

    rule_strength        a deterministic rule matched, and how strongly
    lexical_score        TF-IDF/character similarity of the best exemplar
    entity_completeness  required entities found / required
    ambiguity            0 = one clear candidate, 1 = a tie between intents
    candidate_margin     best candidate minus the runner-up
    context_available    the context layer can back a reference
    reference            the request names a KIND ("that file"), not a target

Two behaviours are deliberate and worth stating:

  * **A rule outranks similarity.** Exact phrasing is evidence of intent;
    similarity is evidence of topic. They are blended 65/35 rather than summed,
    so a strong feel-alike can never outvote a real match.
  * **A reference is capped, never boosted.** "open it" is only ever as certain
    as the remembered target — so with context it returns the reference ceiling
    and without context it returns something low, which is what routes it to a
    question or to the model instead of to an invented application.

The result carries its own parts, so the UI and the logs can say WHY the number
is what it is without exposing any model reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Blend weights: a deterministic rule is exact evidence of intent, similarity
# is fuzzier evidence of topic.
_RULE_WEIGHT = 0.65
_LEXICAL_WEIGHT = 0.35
# Missing a required entity should lower confidence without zeroing it: the
# intent may still be right, and the pipeline asks for the missing piece.
_ENTITY_FLOOR = 0.80
# A tie between two intents is real doubt, not a rounding error.
_AMBIGUITY_PENALTY = 0.40
# Ceiling for a context-resolved reference and for one with no context at all.
_REFERENCE_CEILING = 0.74
_UNRESOLVED_REFERENCE = 0.35
# Ambiguity above this counts as a genuine tie.
_AMBIGUITY_KNEE = 0.55


@dataclass(frozen=True, slots=True)
class ConfidenceSignals:
    """Evidence available at scoring time. All optional; defaults are neutral."""

    rule_strength: float = 0.0
    lexical_score: float = 0.0
    # Embedding similarity. Unlike ``lexical_score`` — one hint among several —
    # this is the WHOLE evidence a semantic layer has, so it is weighted as
    # evidence rather than as 35% of one.
    semantic_score: float = 0.0
    required_entities: int = 0
    found_entities: int = 0
    ambiguity: float = 0.0
    candidate_margin: float = 1.0
    context_available: bool = False
    reference: bool = False
    strategy: str = ""


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """A confidence and the arithmetic behind it."""

    confidence: float
    parts: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": round(self.confidence, 3),
            "parts": {key: round(value, 4) for key, value in self.parts.items()},
            "notes": list(self.notes),
        }


def entity_completeness(required: int, found: int) -> float:
    """Fraction of required entities present (1.0 when nothing is required)."""
    if required <= 0:
        return 1.0
    return max(0.0, min(1.0, found / required))


def score_confidence(signals: ConfidenceSignals) -> ConfidenceScore:
    """Blend the signals into one explainable confidence."""
    notes: list[str] = []
    rule = _unit(signals.rule_strength)
    lexical = _unit(signals.lexical_score)

    if signals.reference:
        ceiling = _REFERENCE_CEILING if signals.context_available else _UNRESOLVED_REFERENCE
        notes.append(
            "reference resolved from context"
            if signals.context_available
            else "reference with no context to resolve it"
        )
        return ConfidenceScore(
            confidence=ceiling,
            parts={
                "reference_ceiling": ceiling,
                "rule_strength": rule,
                "lexical_score": lexical,
                "ambiguity": _unit(signals.ambiguity),
            },
            notes=tuple(notes),
        )

    semantic = _unit(signals.semantic_score)
    if semantic and not rule:
        # An embedding match stands alone: there is no rule behind it to
        # corroborate, so it enters at full weight. Blending it into the lexical
        # slot would have made every genuine paraphrase read near 0.2 — an
        # artefact of the blend, not a statement about the match.
        evidence = semantic
        notes.append("matched by embedding similarity")
    else:
        evidence = _RULE_WEIGHT * rule + _LEXICAL_WEIGHT * lexical
        if semantic:
            notes.append("matched by embedding similarity")
        if rule and lexical:
            notes.append("matched by rule and by exemplar similarity")
        elif rule:
            notes.append("matched by a deterministic rule")
        elif lexical:
            notes.append("matched by exemplar similarity only")

    completeness = entity_completeness(signals.required_entities, signals.found_entities)
    entity_factor = _ENTITY_FLOOR + (1.0 - _ENTITY_FLOOR) * completeness
    missing = signals.required_entities - signals.found_entities
    if missing > 0:
        notes.append(f"{missing} required entity(ies) missing")

    ambiguity = _unit(signals.ambiguity)
    ambiguity_factor = 1.0 - _AMBIGUITY_PENALTY * ambiguity
    if ambiguity >= _AMBIGUITY_KNEE:
        notes.append("two candidate intents are close together")

    margin = max(0.0, min(1.0, signals.candidate_margin))
    margin_bonus = 0.06 * margin
    context_bonus = 0.03 if signals.context_available else 0.0

    confidence = evidence * entity_factor * ambiguity_factor + margin_bonus + context_bonus
    return ConfidenceScore(
        confidence=_unit(confidence),
        parts={
            "rule_strength": rule,
            "lexical_score": lexical,
            "semantic_score": semantic,
            "evidence": evidence,
            "entity_factor": entity_factor,
            "ambiguity_factor": ambiguity_factor,
            "margin_bonus": margin_bonus,
            "context_bonus": context_bonus,
        },
        notes=tuple(notes),
    )


def ambiguity_from(candidates: tuple[float, ...]) -> tuple[float, float]:
    """Ambiguity and margin from ranked candidate scores (best first).

    Ambiguity is how close the runner-up is: a 0.9/0.89 pair is a genuine tie
    (high ambiguity), while 0.9/0.3 is not. Margin is the normalized gap. Two
    candidates are the minimum for either to mean anything.
    """
    if len(candidates) < 2:
        return 0.0, 1.0
    best, second = candidates[0], candidates[1]
    if best <= 0.0:
        return 0.0, 1.0
    margin = max(0.0, (best - second) / best)
    # Two nearly identical candidates => ambiguity near 1.
    ambiguity = 1.0 - min(1.0, margin / 0.25)
    return ambiguity, margin


def _unit(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
