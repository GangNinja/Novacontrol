"""Which executor carries out which intent — ONE table, in the decision layer.

This mapping used to live in the application as ``_GIL_ROUTES``, next to the
adapter that translates handler keys into the legacy brain's vocabulary. It is
not application policy, it is a decision: "this intent is carried out by that
subsystem". Moving it here makes the decision engine's answer directly
actionable — ``Decision.handler`` is literally the key the application routes
on — and leaves the application holding only the adapter, not the table.

``NovaControlApplication._GIL_ROUTES`` remains as a view of this mapping, so
existing callers and tests keep working against one source of truth.
"""

from __future__ import annotations

from novacontrol.intelligence.intent import IntentName

#: Intent -> executor key. The executor keys are the application's handler
#: vocabulary (see ``_GIL_HANDLER_KEYS`` for their brain equivalents).
INTENT_HANDLERS: dict[IntentName, str] = {
    # Device / J.A.R.V.I.S surface.
    IntentName.OPEN_APPLICATION: "desktop",
    IntentName.CLOSE_APPLICATION: "desktop",
    IntentName.OPEN_FOLDER: "desktop",
    IntentName.TAKE_SCREENSHOT: "desktop",
    IntentName.TYPE_TEXT: "desktop",
    IntentName.PRESS_KEY: "desktop",
    IntentName.PHONE_OPEN_APP: "phone",
    IntentName.PHONE_SEND_TEXT: "phone",
    IntentName.PHONE_CALL: "phone",
    IntentName.PHONE_SCREENSHOT: "phone",
    IntentName.PHONE_CONNECT: "phone",
    IntentName.PHONE_STATUS: "phone",
    IntentName.NAVIGATE: "browser",
    IntentName.SEARCH_WEB: "browser",
    IntentName.EXTRACT_PAGE: "browser",
    IntentName.FILL_FORM: "browser",
    # Knowledge / research surface.
    IntentName.RESEARCH: "explore",
    IntentName.ANSWER_QUESTION: "chat",
    IntentName.SUMMARIZE: "explore",
    IntentName.COMPARE: "explore",
    IntentName.GENERATE_REPORT: "explore",
    IntentName.CHAT: "chat",
    # Planning / automation surface.
    IntentName.PLAN_TASK: "plan",
    IntentName.CREATE_AUTOMATION: "plan",
    IntentName.RUN_AUTOMATION: "plan",
    IntentName.SCHEDULE_TASK: "plan",
    # Memory / project / improvement surface (system status stays with the
    # brain, whose chat already reports status with full context).
    IntentName.REMEMBER: "memory",
    IntentName.RECALL: "memory",
    IntentName.CREATE_PROJECT: "project",
    IntentName.IMPROVE_SELF: "self_improvement",
    IntentName.AGENTIC_TASK: "agent",
    # System status: measured on this machine and answered without a model.
    IntentName.SYSTEM_STATUS: "system",
    IntentName.CPU_STATUS: "system",
    IntentName.MEMORY_STATUS: "system",
    IntentName.GPU_STATUS: "system",
    IntentName.BATTERY_STATUS: "system",
    IntentName.NETWORK_STATUS: "system",
    # Composite browser work routes to the real browser controller.
    IntentName.BROWSER_ACTION: "browser",
    # Visual understanding goes to the vision pipeline (a dedicated VLM),
    # never to a text-only chat model.
    IntentName.SCREENSHOT_ANALYSIS: "vision",
    # Language-only intents stay with the chat handler, which already knows
    # how to answer them locally (scratch) or through the configured model.
    IntentName.CONVERSATION: "chat",
    IntentName.GENERAL_QUESTION: "chat",
    IntentName.CALCULATE: "chat",
}

#: Answered from this machine's own telemetry — never worth a model call.
SYSTEM_READING_INTENTS: frozenset[IntentName] = frozenset(
    {
        IntentName.SYSTEM_STATUS,
        IntentName.CPU_STATUS,
        IntentName.MEMORY_STATUS,
        IntentName.GPU_STATUS,
        IntentName.BATTERY_STATUS,
        IntentName.NETWORK_STATUS,
    }
)

#: Needs sources from outside this machine, so the web flags apply.
KNOWLEDGE_INTENTS: frozenset[IntentName] = frozenset(
    {
        IntentName.RESEARCH,
        IntentName.SUMMARIZE,
        IntentName.COMPARE,
        IntentName.GENERATE_REPORT,
        IntentName.ANSWER_QUESTION,
        IntentName.GENERAL_QUESTION,
    }
)

#: Language-only work that the chat handler already answers.
CONVERSATION_INTENTS: frozenset[IntentName] = frozenset(
    {IntentName.CONVERSATION, IntentName.CHAT, IntentName.CALCULATE}
)

#: Work that is planned and sequenced rather than executed in one step.
PLANNING_INTENTS: frozenset[IntentName] = frozenset(
    {
        IntentName.PLAN_TASK,
        IntentName.CREATE_AUTOMATION,
        IntentName.RUN_AUTOMATION,
        IntentName.SCHEDULE_TASK,
        IntentName.ORGANIZE_FILES,
        IntentName.CREATE_PROJECT,
        IntentName.IMPROVE_SELF,
    }
)

#: Work that needs the agentic loop: perception, action, verification, recovery.
AGENTIC_INTENTS: frozenset[IntentName] = frozenset({IntentName.AGENTIC_TASK})

#: Visual understanding — routed to the vision pipeline, never to chat.
VISION_INTENTS: frozenset[IntentName] = frozenset({IntentName.SCREENSHOT_ANALYSIS})


def handler_for(intent: IntentName) -> str:
    """The executor key for an intent, or "" when nothing carries it out yet."""
    return INTENT_HANDLERS.get(intent, "")
