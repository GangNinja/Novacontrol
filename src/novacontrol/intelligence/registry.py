"""The intent catalog: ONE declarative definition per intent.

Intent knowledge used to live in four places — phrasing in ``rules.py``,
exemplars in ``exemplars.py``, risk/verifier in ``capabilities.py``, and
required entities in the engine. They agree today, but nothing *made* them
agree, so a new intent could be half-added: understood, described nowhere, and
dispatched by nobody.

This module gives every intent a single definition that answers the questions
the rest of the pipeline asks about it:

    what is it?            description
    how is it said?        examples          (taken from the exemplar corpus)
    what does it need?     required/optional entities
    what carries it out?   tools + handler   ("" handler = not dispatchable yet)
    how careful must we be? requires_confirmation / risk
    what does it need next? requires_web / requires_vision
    how sure must we be?   confidence_floor
    how much machinery?    category          (local / planner / model / conversation)

Two design rules keep it honest:

  * **Complete by construction.** Any intent with no hand-written definition is
    filled from the capability registry (executor -> tools/category) with a
    conservative default. A new ``IntentName`` therefore cannot be invisible —
    it appears in the catalog the moment it exists.
  * **Declared, not inferred.** ``handler`` names the route that dispatches the
    intent. An entry with ``handler == ""`` is *understood but not yet
    dispatchable*, which is the one gap worth naming out loud; a drift test
    compares this field against the application's real route table so the two
    can never disagree silently.

This module holds DATA and lookup only. It never calls a model, never touches
the filesystem, and never imports the application layer (which imports this).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from novacontrol.intelligence.intent import IntentName

# The global fast band. An intent that does not override ``confidence_floor``
# inherits the confidence its own deterministic rule registers, so an exact
# rule match always clears the bar — a floor ABOVE the rule's own confidence
# would silently demote every one of its matches to the verify band.
_DEFAULT_FLOOR = 0.90
# Sentinel for "no floor was declared": derive it (see default_catalog).
_DERIVED_FLOOR = 0.0
# A conversational reply is fine at a lower bar: being slightly unsure which of
# two chat intents applies costs a marginally different answer, not a wrong
# action, so demanding 0.90 would push obvious chat into the model.
_CONVERSATION_FLOOR = 0.55


class ExecutionCategory(StrEnum):
    """How much machinery an intent needs to be carried out.

    This is the field the complexity detector and the router share: it is what
    decides whether a request can be executed locally, whether it needs the
    planner, or whether it needs language understanding at all.
    """

    LOCAL = "local"  # one deterministic local tool; no planner, no model
    PLANNER = "planner"  # multi-step workflow through the planner
    MODEL = "model"  # needs language-model understanding or reasoning
    CONVERSATION = "conversation"  # answered by the chat brain


@dataclass(frozen=True, slots=True)
class IntentDefinition:
    """Everything the pipeline knows about one intent."""

    intent: IntentName
    description: str
    category: ExecutionCategory = ExecutionCategory.LOCAL
    examples: tuple[str, ...] = ()
    required_entities: tuple[str, ...] = ()
    optional_entities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    handler: str = ""
    requires_confirmation: bool = False
    requires_web: bool = False
    requires_vision: bool = False
    confidence_floor: float = _DEFAULT_FLOOR
    # Opt-in: when this intent arrives with no target, ask for the first
    # required entity instead of acting on nothing. Opt-in because a
    # declaration is a *dispatch* requirement — ``find_file`` declares ``file``
    # yet "find my NovaControl project" is a complete request — so only an
    # intent whose meaning genuinely depends on the entity should say so.
    clarify_when_missing: bool = False
    # True for the intents that intentionally have no executor: clarification
    # asks a question, it does not run anything, so it must not be reported as
    # a capability gap or a new intent would be indistinguishable from it.
    tool_free: bool = False

    @property
    def dispatchable(self) -> bool:
        """True when some handler in the application actually carries this out."""
        return bool(self.handler)

    @property
    def needs_model(self) -> bool:
        """True when the intent cannot be served without language understanding."""
        return self.category is ExecutionCategory.MODEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "description": self.description,
            "category": self.category.value,
            "examples": list(self.examples),
            "required_entities": list(self.required_entities),
            "optional_entities": list(self.optional_entities),
            "tools": list(self.tools),
            "handler": self.handler,
            "dispatchable": self.dispatchable,
            "requires_confirmation": self.requires_confirmation,
            "requires_web": self.requires_web,
            "requires_vision": self.requires_vision,
            "confidence_floor": self.confidence_floor,
            "clarify_when_missing": self.clarify_when_missing,
            "tool_free": self.tool_free,
        }


@dataclass(frozen=True, slots=True)
class IntentCatalog:
    """Ordered, extensible collection of intent definitions.

    Registration returns a NEW catalog (frozen dataclass), so a subsystem can
    extend the catalog without mutating the one the rest of the process holds.
    """

    definitions: tuple[IntentDefinition, ...] = ()
    _index: Mapping[IntentName, IntentDefinition] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self._index:
            object.__setattr__(
                self, "_index", {definition.intent: definition for definition in self.definitions}
            )

    def register(self, definition: IntentDefinition) -> IntentCatalog:
        """Return a catalog with ``definition`` added (replacing any same-intent entry)."""
        kept = tuple(item for item in self.definitions if item.intent is not definition.intent)
        return IntentCatalog((*kept, definition))

    def get(self, intent: IntentName) -> IntentDefinition | None:
        return self._index.get(intent)

    def require(self, intent: IntentName) -> IntentDefinition:
        """The definition for ``intent``, failing loudly if the catalog is incomplete."""
        definition = self._index.get(intent)
        if definition is None:
            raise KeyError(f"no intent definition for {intent.value!r}")
        return definition

    def all(self) -> tuple[IntentDefinition, ...]:
        return self.definitions

    def by_category(self, category: ExecutionCategory) -> tuple[IntentDefinition, ...]:
        return tuple(item for item in self.definitions if item.category is category)

    def dispatchable(self) -> tuple[IntentDefinition, ...]:
        return tuple(item for item in self.definitions if item.dispatchable)

    def not_dispatchable(self) -> tuple[IntentDefinition, ...]:
        """Understood intents that no handler serves yet — the honest gap list.

        Excludes the tool-free intents (clarification never runs anything by
        design), so this list means exactly one thing: a capability the rules
        can recognise and the application cannot yet carry out.
        """
        return tuple(
            item for item in self.definitions if not item.dispatchable and not item.tool_free
        )

    def missing_entities(self, intent: IntentName, entities: Mapping[str, Any]) -> tuple[str, ...]:
        """Required entity kinds absent from ``entities`` (the clarification question)."""
        definition = self._index.get(intent)
        if definition is None:
            return ()
        return tuple(
            kind
            for kind in definition.required_entities
            if entities.get(kind) in (None, "", (), [], {})
        )

    def confidence_floor(self, intent: IntentName) -> float:
        definition = self._index.get(intent)
        return definition.confidence_floor if definition is not None else _DEFAULT_FLOOR

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.definitions),
            "dispatchable": len(self.dispatchable()),
            "not_dispatchable": [d.intent.value for d in self.not_dispatchable()],
            "by_category": {
                category.value: len(self.by_category(category)) for category in ExecutionCategory
            },
            "intents": [definition.to_dict() for definition in self.definitions],
        }


def _def(
    intent: IntentName,
    description: str,
    category: ExecutionCategory = ExecutionCategory.LOCAL,
    *,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    handler: str = "",
    confirm: bool = False,
    web: bool = False,
    vision: bool = False,
    floor: float = _DERIVED_FLOOR,
    tool_free: bool = False,
) -> IntentDefinition:
    """Terse constructor — the table below reads as data, not as boilerplate."""
    return IntentDefinition(
        intent=intent,
        description=description,
        category=category,
        required_entities=required,
        optional_entities=optional,
        tools=tools,
        handler=handler,
        requires_confirmation=confirm,
        requires_web=web,
        requires_vision=vision,
        confidence_floor=floor,
        tool_free=tool_free,
    )


# The hand-written table. Everything here is a deliberate choice; anything not
# listed is filled from the capability registry so the catalog stays complete.
_DEFINITIONS: tuple[IntentDefinition, ...] = (
    # ── Applications and windows ──────────────────────────────────────────
    _def(IntentName.OPEN_APPLICATION, "Start an application.", required=("application",),
         tools=("desktop_controller",), handler="desktop"),
    _def(IntentName.CLOSE_APPLICATION, "Close a running application or window.",
         required=("application",), tools=("desktop_controller",), handler="desktop",
         confirm=True, floor=0.92),
    _def(IntentName.OPEN_FOLDER, "Open a folder in the file manager.",
         required=("folder",), tools=("desktop_controller",), handler="desktop"),
    _def(IntentName.TYPE_TEXT, "Type literal text into the focused window.",
         required=("text",), tools=("desktop_controller",), handler="desktop", floor=0.92),
    _def(IntentName.PRESS_KEY, "Press a key or keyboard shortcut.",
         required=("key",), optional=("keys",), tools=("desktop_controller",), handler="desktop",
         confirm=True),
    _def(IntentName.TAKE_SCREENSHOT, "Capture the screen to an image.",
         optional=("path", "save_path"), tools=("desktop_controller",), handler="desktop"),

    # ── Files ─────────────────────────────────────────────────────────────
    _def(IntentName.FIND_FILE, "Locate a file or a named project on disk.",
         required=("file",), optional=("folder", "project", "query"), handler=""),
    _def(IntentName.READ_FILE, "Read and show the contents of a file.",
         required=("file",), handler=""),
    _def(IntentName.WRITE_FILE, "Create or overwrite a file with given content.",
         required=("file",), optional=("text", "content"), handler="", confirm=True, floor=0.92),
    _def(IntentName.MODIFY_FILE, "Change part of an existing file.",
         required=("file",), handler="", confirm=True),
    _def(IntentName.DELETE_FILE, "Delete a file.", required=("file",), handler="",
         confirm=True, floor=0.95),
    _def(IntentName.MOVE_FILE, "Move or rename a file.", required=("file",), handler="",
         confirm=True),
    _def(IntentName.COPY_FILE, "Copy a file.", required=("file",), handler="", confirm=True),
    _def(IntentName.LIST_FILES, "List the files in a folder.", optional=("folder",), handler=""),
    _def(IntentName.ORGANIZE_FILES, "Sort a folder's files into subfolders by type.",
         required=("folder",), handler="", confirm=True, floor=0.92),

    # ── Web and browsing ──────────────────────────────────────────────────
    _def(IntentName.SEARCH_WEB, "Search the web in a real browser.",
         required=("query",), optional=("website",), tools=("browser_controller",),
         handler="browser", web=True),
    _def(IntentName.NAVIGATE, "Open a specific site or URL.",
         optional=("url", "website"), tools=("browser_controller",), handler="browser",
         web=True),
    _def(IntentName.EXTRACT_PAGE, "Read a page's content and report what it says.",
         optional=("url",), tools=("browser_controller",), handler="browser", web=True),
    _def(IntentName.FILL_FORM, "Fill in and submit a web form.",
         optional=("url", "text"), tools=("browser_controller",), handler="browser",
         web=True, confirm=True),
    _def(IntentName.BROWSER_ACTION, "A browser action described in words (click, scroll, go back).",
         optional=("query", "url"), tools=("browser_controller",), handler="browser",
         web=True, floor=0.80),

    # ── Knowledge and research ────────────────────────────────────────────
    _def(IntentName.RESEARCH, "Research a topic and answer from sources.",
         category=ExecutionCategory.MODEL, required=("query",), tools=("explore_service",),
         handler="explore", web=True, floor=0.80),
    _def(IntentName.ANSWER_QUESTION, "Answer a direct question.",
         category=ExecutionCategory.CONVERSATION, required=("query",), tools=("chat_brain",),
         handler="chat", floor=_CONVERSATION_FLOOR),
    _def(IntentName.SUMMARIZE, "Summarize a page, document or topic.",
         category=ExecutionCategory.MODEL, optional=("url", "query"), tools=("explore_service",),
         handler="explore", floor=0.80),
    _def(IntentName.COMPARE, "Compare two or more things.",
         category=ExecutionCategory.MODEL, required=("query",), tools=("explore_service",),
         handler="explore", floor=0.80),
    _def(IntentName.GENERATE_REPORT, "Produce a written report on a topic.",
         category=ExecutionCategory.MODEL, required=("query",), tools=("explore_service",),
         handler="explore", web=True, floor=0.80),

    # ── Conversation ──────────────────────────────────────────────────────
    _def(IntentName.CHAT, "Plain conversation with no action attached.",
         category=ExecutionCategory.CONVERSATION, tools=("chat_brain",), handler="chat",
         floor=_CONVERSATION_FLOOR),
    _def(IntentName.CONVERSATION, "Social turn (greeting, thanks, small talk).",
         category=ExecutionCategory.CONVERSATION, tools=("chat_brain",), handler="chat",
         floor=_CONVERSATION_FLOOR),
    _def(IntentName.GENERAL_QUESTION, "A question that is not a system or research request.",
         category=ExecutionCategory.CONVERSATION, tools=("chat_brain",), handler="chat",
         floor=_CONVERSATION_FLOOR),
    _def(IntentName.CALCULATE, "Arithmetic, computed locally.",
         category=ExecutionCategory.CONVERSATION, required=("expression",),
         optional=("numbers",), tools=("scratch_brain",), handler="chat",
         floor=_CONVERSATION_FLOOR),

    # ── System status (measured, never guessed) ───────────────────────────
    _def(IntentName.SYSTEM_STATUS, "Report a combined system health snapshot.",
         tools=("system_monitor",), handler="system"),
    _def(IntentName.CPU_STATUS, "Report live CPU load and temperature.",
         tools=("system_monitor",), handler="system"),
    _def(IntentName.MEMORY_STATUS, "Report live memory usage.", tools=("system_monitor",),
         handler="system"),
    _def(IntentName.GPU_STATUS, "Report GPU and video-memory state.", tools=("system_monitor",),
         handler="system"),
    _def(IntentName.BATTERY_STATUS, "Report battery charge and power source.",
         tools=("system_monitor",), handler="system"),
    _def(IntentName.NETWORK_STATUS, "Report network connectivity.", tools=("system_monitor",),
         handler="system"),
    _def(IntentName.SYSTEM_INFO, "Report machine specifications.", tools=("system_monitor",),
         handler=""),

    # ── Device controls ───────────────────────────────────────────────────
    _def(IntentName.VOLUME_CONTROL, "Change or mute the output volume.",
         required=("level",), handler=""),
    _def(IntentName.BRIGHTNESS_CONTROL, "Change the display brightness.",
         required=("level",), handler=""),
    _def(IntentName.MEDIA_CONTROL, "Play, pause, skip or stop media.", handler=""),

    # ── Commands and automation ───────────────────────────────────────────
    _def(IntentName.RUN_COMMAND, "Run a shell command.", required=("command",), handler="",
         confirm=True, floor=0.95),
    _def(IntentName.PLAN_TASK, "Break a goal into planned steps.",
         category=ExecutionCategory.PLANNER, tools=("planning_engine",), handler="plan"),
    _def(IntentName.CREATE_AUTOMATION, "Save a request as a reusable automation.",
         category=ExecutionCategory.PLANNER, tools=("automation_manager",), handler="plan",
         confirm=True),
    _def(IntentName.RUN_AUTOMATION, "Run a saved automation.",
         category=ExecutionCategory.PLANNER, tools=("automation_manager",), handler="plan"),
    _def(IntentName.SCHEDULE_TASK, "Schedule something to run later.",
         category=ExecutionCategory.PLANNER, tools=("scheduler",), handler="plan",
         optional=("time", "date")),

    # ── Memory and projects ───────────────────────────────────────────────
    _def(IntentName.REMEMBER, "Store a fact for later recall.", tools=("memory_manager",),
         handler="memory"),
    _def(IntentName.RECALL, "Recall something previously stored.", tools=("memory_manager",),
         handler="memory"),
    _def(IntentName.CREATE_PROJECT, "Create a project record.",
         tools=("project_manager",), handler="project"),
    _def(IntentName.PROJECT_ANALYSIS, "Inspect and explain a project's code.",
         category=ExecutionCategory.MODEL, optional=("project",), tools=("code_agent",),
         handler="", floor=0.80),

    # ── Code ──────────────────────────────────────────────────────────────
    _def(IntentName.CODE_GENERATION, "Write code.", category=ExecutionCategory.MODEL,
         tools=("code_agent",), handler="", floor=0.80),
    _def(IntentName.CODE_EXPLANATION, "Explain existing code.",
         category=ExecutionCategory.MODEL, tools=("code_agent",), handler="", floor=0.80),
    _def(IntentName.CODE_DEBUGGING, "Find and fix a defect.",
         category=ExecutionCategory.MODEL, tools=("code_agent",), handler="", floor=0.80),

    # ── Phone ─────────────────────────────────────────────────────────────
    _def(IntentName.PHONE_OPEN_APP, "Open an app on the paired phone.",
         required=("application",), tools=("phone_controller",), handler="phone"),
    _def(IntentName.PHONE_SEND_TEXT, "Send a text from the paired phone.",
         required=("message",), optional=("contact",), tools=("phone_controller",),
         handler="phone", confirm=True),
    _def(IntentName.PHONE_CALL, "Place a call from the paired phone.",
         required=("contact",), tools=("phone_controller",), handler="phone", confirm=True),
    _def(IntentName.PHONE_SCREENSHOT, "Capture the paired phone's screen.",
         tools=("phone_controller",), handler="phone"),
    _def(IntentName.PHONE_CONNECT, "Connect or check the phone link.",
         tools=("phone_controller",), handler="phone"),
    _def(IntentName.PHONE_STATUS, "Report paired-phone status.", tools=("phone_controller",),
         handler="phone"),

    # ── Vision (detected now, executed by a VLM later) ────────────────────
    _def(IntentName.SCREENSHOT_ANALYSIS, "Interpret a screenshot or image.",
         tools=("vision_pipeline",), handler="vision", vision=True),

    # ── Self-improvement and agents ───────────────────────────────────────
    _def(IntentName.IMPROVE_SELF, "Inspect and improve NovaControl's own code.",
         category=ExecutionCategory.MODEL, tools=("self_improvement_engine",),
         handler="self_improvement", confirm=True, floor=0.85),
    _def(IntentName.AGENTIC_TASK, "Run a multi-step task through the agent runtime.",
         category=ExecutionCategory.PLANNER, tools=("agent_coordinator",), handler="agent",
         confirm=True, floor=0.85),

    # ── Clarification (the honest non-answer) ─────────────────────────────
    _def(IntentName.CLARIFY, "The request could not be understood; ask one precise question.",
         category=ExecutionCategory.CONVERSATION, floor=0.0, tool_free=True),
)

# Filling an undescribed intent needs a category; these executors are
# conversational, so their intents are conversation rather than tooling.
_CONVERSATIONAL_TOOLS = frozenset({"chat_brain", "scratch_brain"})


def default_catalog(
    *,
    capabilities: Iterable[Any] | None = None,
    exemplars: Iterable[tuple[IntentName, Iterable[str]]] | None = None,
) -> IntentCatalog:
    """The catalog the NLU engine uses: hand-written entries, then filled gaps.

    Filling uses the capability registry (executor -> tools and category) and
    the exemplar corpus (examples), so an intent added in either place shows up
    here without a second edit. Hand-written entries always win.
    """
    examples = _examples_by_intent(exemplars)
    rule_confidence = _rule_confidences()
    defined: list[IntentDefinition] = []
    for definition in _DEFINITIONS:
        phrases = definition.examples or examples.get(definition.intent, ())
        floor = definition.confidence_floor
        if floor <= _DERIVED_FLOOR:
            # No floor declared: inherit the rule's own confidence. An exact
            # deterministic match must keep its fast path; intents that need a
            # higher bar declare one explicitly (see the destructive entries).
            floor = rule_confidence.get(definition.intent, _DEFAULT_FLOOR)
        updated = replace(definition, examples=phrases, confidence_floor=floor)
        defined.append(updated if (phrases or floor != definition.confidence_floor) else definition)

    described = {definition.intent for definition in defined}
    for capability in _capabilities(capabilities):
        intent = getattr(capability, "intent", None)
        if intent is None or intent in described:
            continue
        entry = _from_capability(capability, examples.get(intent, ()))
        if entry.confidence_floor <= _DERIVED_FLOOR:
            entry = replace(
                entry, confidence_floor=rule_confidence.get(intent, _DEFAULT_FLOOR)
            )
        defined.append(entry)
        described.add(intent)

    # Nothing may be missing: an intent the rules can produce but the catalog
    # cannot describe is exactly the drift this module exists to prevent.
    for intent in IntentName:
        if intent not in described:
            defined.append(
                IntentDefinition(
                    intent=intent,
                    description="Undescribed intent (no catalog entry yet).",
                    examples=examples.get(intent, ()),
                    confidence_floor=rule_confidence.get(intent, _DEFAULT_FLOOR),
                )
            )
    return IntentCatalog(tuple(defined))


def _rule_confidences() -> dict[IntentName, float]:
    """The strongest confidence each intent's deterministic rules register."""
    from novacontrol.intelligence.rules import default_rules

    strongest: dict[IntentName, float] = {}
    for rule in default_rules():
        current = strongest.get(rule.intent, 0.0)
        if rule.confidence > current:
            strongest[rule.intent] = rule.confidence
    return strongest


def _capabilities(capabilities: Iterable[Any] | None) -> tuple[Any, ...]:
    if capabilities is not None:
        return tuple(capabilities)
    from novacontrol.intelligence.capabilities import default_capabilities

    return tuple(default_capabilities())


def _examples_by_intent(
    exemplars: Iterable[tuple[IntentName, Iterable[str]]] | None,
) -> dict[IntentName, tuple[str, ...]]:
    if exemplars is None:
        from novacontrol.intelligence.exemplars import default_exemplars

        exemplars = default_exemplars()
    grouped: dict[IntentName, tuple[str, ...]] = {}
    for intent, phrases in exemplars:
        grouped.setdefault(intent, tuple(str(phrase) for phrase in phrases))
    return grouped


def _from_capability(capability: Any, examples: tuple[str, ...]) -> IntentDefinition:
    """Derive a definition from a registered capability.

    Only the fields a capability can actually prove are taken from it; risk
    becomes ``requires_confirmation`` for anything above LOW, and the executor
    decides the category. Everything else keeps a conservative default so a
    derived entry never claims more than it knows.
    """
    executor = str(getattr(capability, "executor", "") or "")
    risk = getattr(capability, "risk", None)
    risk_value = str(getattr(risk, "value", risk or "")).lower()
    return IntentDefinition(
        intent=capability.intent,
        description=str(getattr(capability, "description", "") or ""),
        category=(
            ExecutionCategory.CONVERSATION
            if executor in _CONVERSATIONAL_TOOLS
            else ExecutionCategory.LOCAL
        ),
        examples=examples,
        required_entities=tuple(getattr(capability, "required", ()) or ()),
        optional_entities=tuple(getattr(capability, "optional", ()) or ()),
        tools=(executor,) if executor else (),
        requires_confirmation=risk_value in {"medium", "high", "critical"},
        confidence_floor=_DERIVED_FLOOR,
    )
