"""Global Input Intelligence: ONE language layer for all of NovaControl.

Every capability (JARVIS, research, automation, browser, desktop, phone,
agentcore, future agents) consumes the same StructuredIntent produced here —
no subsystem parses raw user strings on its own.

    raw input -> normalize -> correct typos -> match intent registry
        -> resolve references with context -> confidence + clarification
        -> StructuredIntent(s)
"""

from novacontrol.intelligence.complexity import (
    Complexity,
    ComplexityAssessment,
    ComplexitySignals,
    assess,
    signals_for,
)
from novacontrol.intelligence.context import InteractionContext
from novacontrol.intelligence.engine import GlobalInputIntelligence, UnderstandResult
from novacontrol.intelligence.entities import EntityExtractorRegistry, default_extractors
from novacontrol.intelligence.exemplars import default_exemplars
from novacontrol.intelligence.intent import (
    INTENT_ALIASES,
    CapabilityRegistry,
    IntentName,
    IntentRegistry,
    RiskLevel,
    StructuredIntent,
    resolve_intent,
)
from novacontrol.intelligence.lexical import LexicalMatch, LexicalMatcher
from novacontrol.intelligence.model_manager import ModelManager, ModelPlan, ModelStatus
from novacontrol.intelligence.registry import (
    ExecutionCategory,
    IntentCatalog,
    IntentDefinition,
    default_catalog,
)
from novacontrol.intelligence.results import ResultStatus, StandardResult
from novacontrol.intelligence.scoring import (
    ConfidenceScore,
    ConfidenceSignals,
    ambiguity_from,
    score_confidence,
)
from novacontrol.intelligence.semantic import (
    Embedder,
    EmbeddingIndex,
    HashingEmbedder,
    SemanticMatch,
)
from novacontrol.intelligence.thresholds import NluThresholds, Route, RoutingDecision
from novacontrol.intelligence.understanding import UserIntent, parse_llm_output

__all__ = [
    "INTENT_ALIASES",
    "CapabilityRegistry",
    "Complexity",
    "ComplexityAssessment",
    "ComplexitySignals",
    "ConfidenceScore",
    "ConfidenceSignals",
    "Embedder",
    "EmbeddingIndex",
    "EntityExtractorRegistry",
    "ExecutionCategory",
    "GlobalInputIntelligence",
    "HashingEmbedder",
    "InteractionContext",
    "IntentCatalog",
    "IntentDefinition",
    "IntentName",
    "IntentRegistry",
    "LexicalMatch",
    "LexicalMatcher",
    "ModelManager",
    "ModelPlan",
    "ModelStatus",
    "NluThresholds",
    "ResultStatus",
    "RiskLevel",
    "Route",
    "RoutingDecision",
    "SemanticMatch",
    "StandardResult",
    "StructuredIntent",
    "UnderstandResult",
    "UserIntent",
    "ambiguity_from",
    "assess",
    "default_catalog",
    "default_exemplars",
    "default_extractors",
    "parse_llm_output",
    "resolve_intent",
    "score_confidence",
    "signals_for",
]
