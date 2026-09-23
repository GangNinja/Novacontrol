"""Complexity detection: SIMPLE, MODERATE or COMPLEX.

The router has to decide how much machinery a request deserves before paying
for any of it. Sentence length is the tempting signal and the wrong one::

    "Open Chrome."                                        short, but trivial
    "Continue the project I was working on yesterday."     short, but hard
    "Open Chrome and search YouTube for the Python docs."  long, but easy

So complexity here is counted, not measured in characters. The signals are the
ones that actually change the cost of getting it right:

  * **actions** — how many things must happen;
  * **dependencies** — whether one action needs another's result ("then",
    "after that", "and use that");
  * **context requirements** — references to the past ("the one I was on");
  * **reasoning** — verbs that ask for judgement ("figure out", "decide",
    "explain why"), which no deterministic layer can answer;
  * **ambiguity** — from the confidence scorer: a request that could mean two
    things is complex even when it is one short clause.

The mapping is deliberately blunt, and each conclusion carries its reasons so
the pipeline (and the UI) can explain WHY a request went to a model:

    SIMPLE    -> the lightweight NLU result, straight to a handler
    MODERATE  -> the lightweight NLU result plus the planner
    COMPLEX   -> language understanding first (the model), then the planner
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from novacontrol.intelligence.registry import ExecutionCategory


class Complexity(StrEnum):
    """How much machinery a request needs."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


# Sequencing words: the second action consumes the first one's outcome, so the
# clauses cannot be planned independently.
_DEPENDENCY = re.compile(
    r"\b(?:then|and then|after that|afterwards|next|and use (?:that|it)|"
    r"using (?:that|it)|based on (?:that|it)|and (?:check|fix|report) (?:that|it))\b",
    re.IGNORECASE,
)
# Requests for judgement rather than execution.
_REASONING = re.compile(
    r"\b(?:why|figure out|work out|decide|determine|diagnose|investigate|"
    r"explain (?:why|what|how)|suggest|recommend|prioriti[sz]e|compare and|"
    r"should i|what should|best way|root cause)\b",
    re.IGNORECASE,
)
# References to something earlier, which only context (or a model) can resolve.
_CONTEXT_REFERENCE = re.compile(
    r"\b(?:yesterday|earlier|before|last (?:time|night|week)|we (?:were|was) "
    r"(?:working|doing)|i was (?:working|doing)|continue|resume|pick up where|"
    r"where i (?:left off|stopped)|the one i|what i was|that project|my progress)\b",
    re.IGNORECASE,
)
# An explicit instruction to look at the world before acting.
_RESEARCH = re.compile(
    r"\b(?:latest|current|today|news|prices?|weather|search|look up|find out)\b", re.IGNORECASE
)
# A BARE request to carry on with earlier work: it names no target at all, so
# only memory can say what to continue. Kept deliberately narrow — "continue the
# project I was working on yesterday" names something and must keep flowing
# through the pipeline, while "continue from where I stopped" must not be read
# as a status query just because some exemplar sits near it in vector space.
_CONTINUATION = re.compile(
    r"^(?:hey |ok |okay |please |now )*"
    r"(?:"
    r"(?:continue|resume|carry on|keep going|go on)(?: please)?"
    r"|(?:continue|resume|pick up)(?: from| right)? where (?:i|we) (?:left off|stopped)"
    r"|(?:continue|resume) (?:what|whatever) (?:i|we) (?:was|were) (?:doing|working on)"
    r"|where (?:was|were) (?:i|we)(?: at)?"
    r"|what was i (?:doing|working on)"
    r")(?: again)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ComplexitySignals:
    """Everything the assessment is allowed to consider."""

    actions: int = 1
    dependencies: int = 0
    context_references: int = 0
    reasoning_markers: int = 0
    research_markers: int = 0
    ambiguity: float = 0.0
    unresolved: int = 0
    words: int = 0
    categories: tuple[ExecutionCategory, ...] = ()


@dataclass(frozen=True, slots=True)
class ComplexityAssessment:
    """The level, a 0..1 score, and the reasons that produced them."""

    level: Complexity
    score: float
    reasons: tuple[str, ...] = ()

    @property
    def needs_model(self) -> bool:
        """True when understanding this request needs a language model."""
        return self.level is Complexity.COMPLEX

    @property
    def needs_planner(self) -> bool:
        """True when more than one step must be sequenced."""
        return self.level in (Complexity.MODERATE, Complexity.COMPLEX)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "needs_model": self.needs_model,
            "needs_planner": self.needs_planner,
        }


def is_continuation(text: str) -> bool:
    """True when the request asks to carry on with work already underway.

    Such a request has no target of its own, which makes it a hazard for any
    layer that answers from wording rather than from memory: a similarity
    reading of *"continue from where I stopped"* lands within 0.57 of a GPU
    status exemplar, and acting on that answers a request to resume work with
    hardware telemetry.
    """
    return bool(_CONTINUATION.match(text.strip()))


def signals_for(
    text: str,
    *,
    actions: int = 1,
    unresolved: int = 0,
    ambiguity: float = 0.0,
    categories: tuple[ExecutionCategory, ...] = (),
) -> ComplexitySignals:
    """Count the textual signals in ``text`` (pure; no model, no state)."""
    return ComplexitySignals(
        actions=max(1, actions),
        dependencies=len(_DEPENDENCY.findall(text)),
        context_references=len(_CONTEXT_REFERENCE.findall(text)),
        reasoning_markers=len(_REASONING.findall(text)),
        research_markers=len(_RESEARCH.findall(text)),
        ambiguity=max(0.0, min(1.0, ambiguity)),
        unresolved=max(0, unresolved),
        words=len(text.split()),
        categories=categories,
    )


def assess(signals: ComplexitySignals) -> ComplexityAssessment:
    """Turn counted signals into a level, with the reasons that justify it.

    The scoring is additive and each term is small; what decides the level is
    the presence of a *qualitative* blocker (unresolved clauses, reasoning,
    context that only memory can supply), because those cannot be worked around
    by trying harder in the lightweight layers.
    """
    reasons: list[str] = []
    score = 0.0
    complex_blocker = False

    if signals.unresolved:
        score += 0.45 * signals.unresolved
        complex_blocker = True
        reasons.append(f"{signals.unresolved} clause(s) could not be understood")

    if signals.context_references:
        score += 0.30 * signals.context_references
        complex_blocker = True
        reasons.append("refers to earlier work, which needs remembered context")

    if signals.reasoning_markers:
        score += 0.28 * signals.reasoning_markers
        complex_blocker = True
        reasons.append("asks for judgement, not just execution")

    if signals.dependencies:
        score += 0.22 * signals.dependencies
        reasons.append("later steps depend on earlier results")

    if signals.actions > 1:
        score += 0.18 * (signals.actions - 1)
        reasons.append(f"{signals.actions} actions to sequence")

    if signals.ambiguity >= 0.5:
        score += 0.22
        complex_blocker = True
        reasons.append("genuinely ambiguous between candidate intents")
    elif signals.ambiguity > 0.0:
        score += 0.10 * signals.ambiguity
        reasons.append("some ambiguity between candidate intents")

    if signals.research_markers:
        score += 0.12
        reasons.append("needs current information from outside this machine")

    # A model-category intent is complex on its own: no deterministic layer can
    # carry it out, so it must not be sent down the local path merely because
    # the sentence happens to be short.
    if ExecutionCategory.MODEL in signals.categories:
        score += 0.30
        complex_blocker = True
        reasons.append("needs language understanding or reasoning to answer")

    if ExecutionCategory.PLANNER in signals.categories and signals.actions > 1:
        score += 0.10

    score = min(1.0, score)
    if complex_blocker or score >= 0.55:
        level = Complexity.COMPLEX
    elif signals.actions > 1 or ExecutionCategory.PLANNER in signals.categories or score >= 0.25:
        level = Complexity.MODERATE
    else:
        level = Complexity.SIMPLE

    if not reasons:
        reasons.append("one clear action, nothing to resolve")
    return ComplexityAssessment(level=level, score=score, reasons=tuple(reasons))
