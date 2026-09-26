"""Phase 3: the Decision Engine.

Sits between the understanding layers (NLU + context/memory) and everything
that executes, and answers one question: *what should NovaControl do with this
request?* It chooses among the machinery NovaControl already has — deterministic
capabilities, subsystem handlers, the planner, the agentic loop, the vision
pipeline, the local model, the cloud — and reports what the request therefore
needs. It executes nothing itself, and it never exposes reasoning: the fields it
produces are the same safe operational metadata the rest of the status surface
already carries.

The default provider is local, deterministic and offline. An external provider
(Jev) can be configured behind the same interface, is consulted only when
remote decisions are explicitly allowed, receives a redacted routing summary
and can only ever advise a route — never name an executor.
"""

from novacontrol.decision.engine import DecisionEngine
from novacontrol.decision.models import (
    ESCALATING_ROUTES,
    Decision,
    DecisionEnvironment,
    DecisionRequest,
    DecisionRoute,
    DecisionType,
    ReasonCode,
    redacted_intent_payload,
)
from novacontrol.decision.providers import (
    DecisionProvider,
    JevDecisionProvider,
    LocalDecisionProvider,
    build_decision_provider,
)
from novacontrol.decision.routing import (
    AGENTIC_INTENTS,
    CONVERSATION_INTENTS,
    INTENT_HANDLERS,
    KNOWLEDGE_INTENTS,
    PLANNING_INTENTS,
    SEQUENCING_HANDLERS,
    SYSTEM_READING_INTENTS,
    VISION_INTENTS,
    handler_for,
)

__all__ = [
    "AGENTIC_INTENTS",
    "CONVERSATION_INTENTS",
    "ESCALATING_ROUTES",
    "INTENT_HANDLERS",
    "KNOWLEDGE_INTENTS",
    "PLANNING_INTENTS",
    "SEQUENCING_HANDLERS",
    "SYSTEM_READING_INTENTS",
    "VISION_INTENTS",
    "build_decision_provider",
    "Decision",
    "DecisionEngine",
    "DecisionEnvironment",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionRoute",
    "DecisionType",
    "JevDecisionProvider",
    "LocalDecisionProvider",
    "ReasonCode",
    "handler_for",
    "redacted_intent_payload",
]
