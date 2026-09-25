"""The structured answer to "what should NovaControl do with this request?".

The decision layer sits between the understanding layers (NLU + context/memory)
and everything that executes. It decides WHICH machinery a request deserves —
a deterministic tool, a local capability, the planner, the agentic loop, a
language model, the vision pipeline or the cloud — and reports WHAT it needs.
It never executes anything: authorization stays with the approval layer, and a
decision is advice to the orchestrator, not a permission.

Two vocabularies, deliberately separate:

    decision_type   what KIND of work this is — deterministic, capability,
                    planning, reasoning, vision, conversation, research,
                    clarification;
    route           WHERE it goes — direct_tool, system_tools, local_capability,
                    planner, agent, local_llm, cloud, vision, chat, clarify.

They are not the same question: ``direct_tool`` and ``system_tools`` are both
deterministic work, and a status surface wants to say "understood and handled
locally" while an orchestrator wants to know which executor to call.

Every decision also carries a stable ``reason_code``, so a caller can branch on
the WHY without matching prose, and a ``reason`` written for humans — assembled
from a template, never model output, and never chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.intelligence.intent import StructuredIntent


class DecisionRoute(StrEnum):
    """Where a request goes once it has been understood and assessed."""

    #: A deterministic executor runs immediately; nothing to plan, no model.
    DIRECT_TOOL = "direct_tool"
    #: The machine's own readings, answered from telemetry rather than a model.
    SYSTEM_TOOLS = "system_tools"
    #: A subsystem handler carries it out in one step (desktop, browser, …).
    LOCAL_CAPABILITY = "local_capability"
    #: More than one step must be sequenced by the planner.
    PLANNER = "planner"
    #: The agentic loop: perceive, act, verify, recover.
    AGENT = "agent"
    #: A language model is required — local by preference.
    LOCAL_LLM = "local_llm"
    #: The vision pipeline (a dedicated VLM, never a text-only model).
    VISION = "vision"
    #: The configured cloud provider.
    CLOUD = "cloud"
    #: Conversation and language-only answers.
    CHAT = "chat"
    #: One precise question instead of a guess.
    CLARIFY = "clarify"


class DecisionType(StrEnum):
    """What kind of work the request is — the coarser, user-facing half."""

    DETERMINISTIC = "deterministic"
    CAPABILITY = "capability"
    PLANNING = "planning"
    REASONING = "reasoning"
    VISION = "vision"
    CONVERSATION = "conversation"
    RESEARCH = "research"
    CLARIFICATION = "clarification"


class ReasonCode(StrEnum):
    """Stable, machine-readable WHY for a decision (never prose to parse)."""

    DETERMINISTIC_CAPABILITY = "deterministic_capability"
    SYSTEM_READING = "system_reading"
    LOCAL_CAPABILITY = "local_capability"
    PLANNING_REQUIRED = "planning_required"
    AGENTIC_TASK = "agentic_task"
    REASONING_REQUIRED = "reasoning_required"
    CLOUD_ESCALATION = "cloud_escalation"
    VISION_REQUIRED = "vision_required"
    CONVERSATION = "conversation"
    KNOWLEDGE_RESEARCH = "knowledge_research"
    CLARIFICATION_NEEDED = "clarification_needed"


#: Routes that need something beyond deterministic local execution — a model or
#: the agentic loop. Used for the ``escalation_required`` flag and for the
#: telemetry count of how often NovaControl leaves the cheap path.
ESCALATING_ROUTES: frozenset[DecisionRoute] = frozenset(
    {
        DecisionRoute.LOCAL_LLM,
        DecisionRoute.CLOUD,
        DecisionRoute.VISION,
        DecisionRoute.AGENT,
    }
)


@dataclass(frozen=True, slots=True)
class DecisionEnvironment:
    """What is actually available to route TO, on this machine, right now.

    The decision engine must not reach into the application to ask "is there a
    model?" — a decision made against assumed hardware is how a request ends up
    queued behind a model that was never installed. The application describes
    its providers here, so the engine stays a pure function of its inputs and
    remains testable without a running model.
    """

    local_model: str = ""
    local_model_loaded: bool = False
    cloud_model: str = ""
    cloud_configured: bool = False
    vision_model: str = ""
    vision_available: bool = False
    planner_available: bool = True
    agent_available: bool = True
    mode: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_model": self.local_model,
            "local_model_loaded": self.local_model_loaded,
            "cloud_model": self.cloud_model,
            "cloud_configured": self.cloud_configured,
            "vision_model": self.vision_model,
            "vision_available": self.vision_available,
            "planner_available": self.planner_available,
            "agent_available": self.agent_available,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """What NovaControl should do, and what it will therefore need.

    Fields are the specification's surface, plus the two that make the decision
    actionable: ``handler`` is the executor key the application already routes
    on, and ``provider`` names who decided (so a fallback is visible rather
    than silent).
    """

    decision_type: DecisionType
    route: DecisionRoute
    selected_capability: str = ""
    #: The TOOL the selected capability would use — resolved locally, from the
    #: intent catalog, so a decision names what runs rather than only where it
    #: goes. Empty when nothing is registered yet, which is reported as such.
    selected_tool: str = ""
    selected_model: str = ""
    actions: tuple[str, ...] = ()
    confidence: float = 0.0
    requires_planning: bool = False
    requires_vision: bool = False
    requires_web: bool = False
    requires_confirmation: bool = False
    escalation_required: bool = False
    reason_code: ReasonCode = ReasonCode.LOCAL_CAPABILITY
    #: Which executor carries it out — the same keys the handler table uses.
    handler: str = ""
    #: Who decided: "local" or the configured provider's name.
    provider: str = "local"
    #: A short human sentence. Templated, never model output.
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Derived rather than set by hand: a decision that escalates while
        # claiming no escalation, or that claims to plan with one action, is a
        # bug that would otherwise only show up as a strange-looking UI.
        object.__setattr__(
            self, "escalation_required", self.escalation_required or self.route in ESCALATING_ROUTES
        )

    def with_(self, **changes: Any) -> Decision:
        from dataclasses import replace

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Safe operational metadata only — no reasoning, no prompts, no text."""
        return {
            "decision_type": self.decision_type.value,
            "route": self.route.value,
            "selected_capability": self.selected_capability,
            "selected_tool": self.selected_tool,
            "selected_model": self.selected_model,
            "actions": list(self.actions),
            "confidence": round(self.confidence, 3),
            "requires_planning": self.requires_planning,
            "requires_vision": self.requires_vision,
            "requires_web": self.requires_web,
            "requires_confirmation": self.requires_confirmation,
            "escalation_required": self.escalation_required,
            "reason_code": self.reason_code.value,
            "handler": self.handler,
            "provider": self.provider,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Everything a provider may consider, in one immutable bundle.

    Assembled once by the engine so that every provider — local or external —
    sees exactly the same evidence, and so an external provider can be handed a
    REDACTED view of it without the call site having to know what is sensitive.
    """

    intent: StructuredIntent
    environment: DecisionEnvironment
    strategy: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    complexity: str = ""
    #: The complexity assessment's own conclusions, carried separately from the
    #: level it reported: "COMPLEX" is a summary, while these two say WHAT kind
    #: of machinery that summary is asking for — a model, a planner, or both.
    needs_model: bool = False
    needs_planner: bool = False
    #: Clause material the reading did not account for. A request understood in
    #: part is not a simple request, however confident the part it read was.
    unresolved: int = 0
    ambiguity: float = 0.0

    @property
    def route_hint(self) -> str:
        """The NLU's own routing hint (fast | verify | llm | vision | clarify)."""
        return str(self.intent.decision.get("route", ""))


def redacted_intent_payload(request: DecisionRequest) -> dict[str, Any]:
    """The minimum an EXTERNAL provider needs to advise on routing.

    Nothing the user said and nothing NovaControl knows about them: no raw or
    normalized text, no entities (they carry filenames, projects and contact
    names), no conversation history. Only the resolved intent, its action and
    requirement flags, the complexity level and what the machine has available.
    A provider that cannot decide from this is not being asked the right
    question.
    """
    intent = request.intent
    return {
        "intent": intent.intent.value,
        "action": intent.action,
        "target_kind": intent.target_kind,
        "confidence": round(intent.confidence, 3),
        "actions": list(intent.actions),
        "requires_vision": intent.requires_vision,
        "requires_web": intent.requires_web,
        "requires_tools": intent.requires_tools,
        "requires_confirmation": intent.requires_confirmation,
        "needs_clarification": intent.needs_clarification,
        "complexity": request.complexity,
        "strategy": request.strategy,
        "environment": request.environment.to_dict(),
    }
