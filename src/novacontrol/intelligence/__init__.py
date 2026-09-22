"""Global Input Intelligence: ONE language layer for all of NovaControl.

Every capability (JARVIS, research, automation, browser, desktop, phone,
agentcore, future agents) consumes the same StructuredIntent produced here —
no subsystem parses raw user strings on its own.

    raw input -> normalize -> correct typos -> match intent registry
        -> resolve references with context -> confidence + clarification
        -> StructuredIntent(s)
"""

from novacontrol.intelligence.context import InteractionContext
from novacontrol.intelligence.engine import GlobalInputIntelligence, UnderstandResult
from novacontrol.intelligence.entities import EntityExtractorRegistry, default_extractors
from novacontrol.intelligence.exemplars import default_exemplars
from novacontrol.intelligence.intent import (
    CapabilityRegistry,
    IntentName,
    IntentRegistry,
    RiskLevel,
    StructuredIntent,
)
from novacontrol.intelligence.lexical import LexicalMatch, LexicalMatcher
from novacontrol.intelligence.model_manager import ModelManager, ModelPlan, ModelStatus
from novacontrol.intelligence.results import ResultStatus, StandardResult
from novacontrol.intelligence.thresholds import NluThresholds, Route, RoutingDecision
from novacontrol.intelligence.understanding import UserIntent, parse_llm_output

__all__ = [
    "CapabilityRegistry",
    "EntityExtractorRegistry",
    "GlobalInputIntelligence",
    "InteractionContext",
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
    "StandardResult",
    "StructuredIntent",
    "UnderstandResult",
    "UserIntent",
    "default_exemplars",
    "default_extractors",
    "parse_llm_output",
]
