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
from novacontrol.intelligence.intent import (
    CapabilityRegistry,
    IntentName,
    IntentRegistry,
    StructuredIntent,
    RiskLevel,
)
from novacontrol.intelligence.results import ResultStatus, StandardResult

__all__ = [
    "CapabilityRegistry",
    "GlobalInputIntelligence",
    "InteractionContext",
    "IntentName",
    "IntentRegistry",
    "ResultStatus",
    "RiskLevel",
    "StandardResult",
    "StructuredIntent",
    "UnderstandResult",
]
