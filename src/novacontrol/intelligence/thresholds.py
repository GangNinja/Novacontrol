"""Confidence thresholds and the routing policy for request understanding.

A fast NLU layer is only useful if the system can tell WHEN it is not
confident. This module owns that judgement, in one place, so no caller
re-invents it:

    confidence >= fast_confidence        -> answer from the fast NLU
    verify_confidence <= conf < fast     -> extra verification (lexical/semantic)
    confidence < verify_confidence       -> escalate to the language model
    vision required                      -> the vision pipeline, never the text LLM
    multi-step / unresolved clauses      -> the language model directly

Every threshold is configurable, by environment variable or through the
central ``NovaControlConfig`` (``nlu`` section), because the right numbers are
machine-specific: a CPU-only box pays tens of seconds for an LLM round trip
while a GPU box pays one, so the same numbers are not correct everywhere.

This module holds POLICY only. It never calls a model and never touches the
filesystem — callers hand it a confidence and a shape, and it returns a
decision they can explain to a user.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

# Defaults. `fast` is what a deterministic rule match already earns (rules
# register 0.8-0.92), so an exact rule hit stays on the fast path; anything
# derived rather than matched (fuzzy, lexical, contextual) lands in the verify
# band and gets a second look before a model is considered.
_DEFAULT_FAST = 0.90
_DEFAULT_VERIFY = 0.70
_DEFAULT_MULTI_STEP = 0.88
_DEFAULT_LEXICAL = 0.62
_DEFAULT_REFERENCE = 0.74
# Calibrated floor for acting on an embedding match — the confidence AFTER the
# multi-signal arithmetic, not the raw similarity. Measured leave-one-out over
# the shipped exemplar corpus (200 phrases, 46 intents, index rebuilt without
# the query): 41% precision at 0.50, 48% at 0.55, 62% at 0.60 (8% of phrases),
# 67% at 0.70, 80% at 0.80. The knee is at 0.60, so that is the default: a
# wrong intent is expensive (it selects a tool), so the layer acts only where
# the arithmetic says it is right more often than not. Raise it for stricter
# behaviour, lower it when a real embedding backend is configured — this number
# describes the local hashing space, not embeddings in general.
_DEFAULT_SEMANTIC = 0.60


class Route(StrEnum):
    """Where a request goes once it has been (partially) understood."""

    FAST = "fast"
    VERIFY = "verify"
    LLM = "llm"
    VISION = "vision"
    CLARIFY = "clarify"


class UnderstandingSource(StrEnum):
    """Human-facing description of what understood the request.

    Deliberately about the COMPONENT, not the model: the UI shows
    "Fast NLU / Qwen3 8B" without naming an implementation here.
    """

    FAST = "Fast NLU"
    CONTEXT = "Context"
    LEXICAL = "Lexical match"
    SEMANTIC = "Language model"
    VISION = "Vision pipeline"
    CLARIFY = "Clarification"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The outcome of applying the policy to one understood request."""

    route: Route
    reason: str
    confidence: float
    requires_llm: bool = False
    reasoning_level: str = "none"

    @property
    def source(self) -> UnderstandingSource:
        if self.route is Route.VISION:
            return UnderstandingSource.VISION
        if self.route is Route.LLM:
            return UnderstandingSource.SEMANTIC
        if self.route is Route.CLARIFY:
            return UnderstandingSource.CLARIFY
        return UnderstandingSource.FAST

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.value,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "requires_llm": self.requires_llm,
            "reasoning_level": self.reasoning_level,
            "understanding": self.source.value,
        }


class _SettingsLike(Protocol):
    """Structural type for ``novacontrol.core.config.NluSettings``.

    Declared here (rather than imported) so this module never depends on the
    configuration module — the config module may import this one.
    """

    fast_confidence: float
    verify_confidence: float
    multi_step_confidence: float
    lexical_confidence: float
    reference_confidence: float
    semantic_confidence: float
    allow_llm: bool
    lexical_matching: bool
    semantic_matching: bool


@dataclass(frozen=True, slots=True)
class NluThresholds:
    """Tunable confidence bands.

    ``allow_llm`` is an escape hatch, not a mode: with it off the pipeline
    still understands everything it can deterministically and asks a question
    instead of escalating, which is what a user wants on a machine with no
    model (or when chat must never block).
    """

    fast_confidence: float = _DEFAULT_FAST
    verify_confidence: float = _DEFAULT_VERIFY
    multi_step_confidence: float = _DEFAULT_MULTI_STEP
    lexical_confidence: float = _DEFAULT_LEXICAL
    reference_confidence: float = _DEFAULT_REFERENCE
    semantic_confidence: float = _DEFAULT_SEMANTIC
    allow_llm: bool = True
    lexical_matching: bool = True
    semantic_matching: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.verify_confidence <= self.fast_confidence <= 1.0:
            raise ValueError(
                "NLU thresholds must satisfy 0 <= verify_confidence <= "
                f"fast_confidence <= 1, got verify={self.verify_confidence} "
                f"fast={self.fast_confidence}"
            )

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "NluThresholds":
        """Read ``NOVACONTROL_NLU_*`` overrides, ignoring unusable values.

        A malformed override must never take the app down: it falls back to the
        default for that single field and keeps the rest.
        """
        env = os.environ if environ is None else environ
        defaults = cls()
        return cls(
            fast_confidence=_float_env(env, "NOVACONTROL_NLU_FAST_CONFIDENCE", defaults.fast_confidence),
            verify_confidence=_float_env(env, "NOVACONTROL_NLU_VERIFY_CONFIDENCE", defaults.verify_confidence),
            multi_step_confidence=_float_env(
                env, "NOVACONTROL_NLU_MULTI_STEP_CONFIDENCE", defaults.multi_step_confidence
            ),
            lexical_confidence=_float_env(
                env, "NOVACONTROL_NLU_LEXICAL_CONFIDENCE", defaults.lexical_confidence
            ),
            reference_confidence=_float_env(
                env, "NOVACONTROL_NLU_REFERENCE_CONFIDENCE", defaults.reference_confidence
            ),
            semantic_confidence=_float_env(
                env, "NOVACONTROL_NLU_SEMANTIC_CONFIDENCE", defaults.semantic_confidence
            ),
            allow_llm=_bool_env(env, "NOVACONTROL_NLU_ALLOW_LLM", defaults.allow_llm),
            lexical_matching=_bool_env(
                env, "NOVACONTROL_NLU_LEXICAL_MATCHING", defaults.lexical_matching
            ),
            semantic_matching=_bool_env(
                env, "NOVACONTROL_NLU_SEMANTIC_MATCHING", defaults.semantic_matching
            ),
        )

    @classmethod
    def from_settings(cls, settings: _SettingsLike) -> "NluThresholds":
        """Adapt the central configuration's ``nlu`` section."""
        return cls(
            fast_confidence=settings.fast_confidence,
            verify_confidence=settings.verify_confidence,
            multi_step_confidence=settings.multi_step_confidence,
            lexical_confidence=settings.lexical_confidence,
            reference_confidence=settings.reference_confidence,
            semantic_confidence=settings.semantic_confidence,
            allow_llm=settings.allow_llm,
            lexical_matching=settings.lexical_matching,
            semantic_matching=settings.semantic_matching,
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NluThresholds":
        """Build from a plain mapping (the config file's ``nlu`` section)."""
        defaults = cls()
        return cls(
            fast_confidence=_float_value(data, "fast_confidence", defaults.fast_confidence),
            verify_confidence=_float_value(data, "verify_confidence", defaults.verify_confidence),
            multi_step_confidence=_float_value(
                data, "multi_step_confidence", defaults.multi_step_confidence
            ),
            lexical_confidence=_float_value(data, "lexical_confidence", defaults.lexical_confidence),
            reference_confidence=_float_value(
                data, "reference_confidence", defaults.reference_confidence
            ),
            semantic_confidence=_float_value(
                data, "semantic_confidence", defaults.semantic_confidence
            ),
            allow_llm=_bool_value(data, "allow_llm", defaults.allow_llm),
            lexical_matching=_bool_value(data, "lexical_matching", defaults.lexical_matching),
            semantic_matching=_bool_value(data, "semantic_matching", defaults.semantic_matching),
        )

    # -- policy ---------------------------------------------------------------

    def decide(
        self,
        *,
        confidence: float,
        requires_vision: bool = False,
        steps: int = 1,
        unresolved_steps: int = 0,
        llm_available: bool = True,
        reference_resolved: bool = False,
    ) -> RoutingDecision:
        """Apply the policy to one candidate understanding.

        Order matters and is deliberate:

        1. vision wins outright — a screenshot must never be described by a
           text-only model, no matter how confident the text parse was;
        2. unresolved clauses escalate, because a half-understood multi-step
           request executed confidently is worse than a slow correct one;
        3. a derived (reference/context) reading is verified before it is
           trusted, since "open it" is only as good as the remembered target;
        4. then the confidence bands.
        """
        can_escalate = llm_available and self.allow_llm

        if requires_vision:
            return RoutingDecision(
                route=Route.VISION,
                reason="The request needs visual understanding of an image or screen.",
                confidence=max(confidence, 0.5),
                reasoning_level="vision",
            )

        if unresolved_steps > 0:
            if can_escalate:
                return RoutingDecision(
                    route=Route.LLM,
                    reason=(
                        f"{unresolved_steps} clause(s) of a multi-step request could not be "
                        "understood without a language model."
                    ),
                    confidence=min(confidence, self.verify_confidence),
                    requires_llm=True,
                    reasoning_level="high",
                )
            return RoutingDecision(
                route=Route.CLARIFY,
                reason="Part of a multi-step request could not be understood.",
                confidence=confidence,
            )

        if steps > 1 and confidence < max(self.multi_step_confidence, self.verify_confidence):
            if can_escalate:
                return RoutingDecision(
                    route=Route.LLM,
                    reason=f"Multi-step request ({steps} steps) needs language understanding.",
                    confidence=confidence,
                    requires_llm=True,
                    reasoning_level="high",
                )
            return RoutingDecision(
                route=Route.VERIFY,
                reason=f"Multi-step request ({steps} steps) verified clause by clause.",
                confidence=confidence,
            )

        if reference_resolved and confidence < self.fast_confidence:
            if confidence >= self.reference_confidence:
                return RoutingDecision(
                    route=Route.VERIFY,
                    reason="A reference was resolved from context and verified.",
                    confidence=confidence,
                )

        if confidence >= self.fast_confidence:
            return RoutingDecision(
                route=Route.FAST,
                reason="Confidently understood without a language model.",
                confidence=confidence,
            )
        if confidence >= self.verify_confidence:
            return RoutingDecision(
                route=Route.VERIFY,
                reason="Understood, but below the fast-path confidence band.",
                confidence=confidence,
            )
        if can_escalate:
            return RoutingDecision(
                route=Route.LLM,
                reason="Low confidence — escalated to the language model.",
                confidence=confidence,
                requires_llm=True,
                reasoning_level="high",
            )
        return RoutingDecision(
            route=Route.CLARIFY,
            reason="Low confidence and no language model is configured.",
            confidence=confidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fast_confidence": self.fast_confidence,
            "verify_confidence": self.verify_confidence,
            "multi_step_confidence": self.multi_step_confidence,
            "lexical_confidence": self.lexical_confidence,
            "reference_confidence": self.reference_confidence,
            "semantic_confidence": self.semantic_confidence,
            "allow_llm": self.allow_llm,
            "lexical_matching": self.lexical_matching,
            "semantic_matching": self.semantic_matching,
        }


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if 0.0 <= value <= 1.0 else default


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _float_value(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


def _bool_value(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
