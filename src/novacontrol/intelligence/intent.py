"""Structured intent model, global intent registry, and capability registry.

This is THE contract between language and execution: every subsystem consumes
StructuredIntent, every capability registers itself here — feature modules
never parse raw user language again.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4


class IntentName(StrEnum):
    """Global intent vocabulary, derived from existing NovaControl capabilities.

    Specced request names map onto this vocabulary rather than duplicating it:

        open_application      -> OPEN_APPLICATION
        close_application     -> CLOSE_APPLICATION
        launch_website        -> NAVIGATE
        search_web            -> SEARCH_WEB
        find_file             -> FIND_FILE
        read_file             -> READ_FILE
        create_file           -> WRITE_FILE
        modify_file           -> MODIFY_FILE
        delete_file           -> DELETE_FILE
        move_file             -> MOVE_FILE
        copy_file             -> COPY_FILE
        execute_command       -> RUN_COMMAND
        system_status         -> SYSTEM_STATUS (and the per-device *_STATUS)
        volume_control        -> VOLUME_CONTROL
        brightness_control    -> BRIGHTNESS_CONTROL
        screenshot            -> TAKE_SCREENSHOT
        screenshot_analysis   -> SCREENSHOT_ANALYSIS
        browser_action        -> BROWSER_ACTION
        code_generation       -> CODE_GENERATION
        code_explanation      -> CODE_EXPLANATION
        code_debugging        -> CODE_DEBUGGING
        project_analysis      -> PROJECT_ANALYSIS
        summarize             -> SUMMARIZE
        explain               -> ANSWER_QUESTION
        calculate             -> CALCULATE
        media_control         -> MEDIA_CONTROL
        general_question      -> GENERAL_QUESTION
        conversation          -> CONVERSATION
        unknown               -> CLARIFY

    One name per real capability: synonyms are handled by exemplars and rules,
    not by a second intent that routes to the same handler.

    The names above the taxonomy that differ from ours live in
    :data:`INTENT_ALIASES`, so the mapping is data a caller can act on rather
    than prose only a reader can use.
    """

    OPEN_APPLICATION = "open_application"
    CLOSE_APPLICATION = "close_application"
    OPEN_FOLDER = "open_folder"
    TAKE_SCREENSHOT = "take_screenshot"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    RUN_COMMAND = "run_command"
    LIST_FILES = "list_files"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    ORGANIZE_FILES = "organize_files"
    SYSTEM_INFO = "system_info"
    NAVIGATE = "navigate"
    SEARCH_WEB = "search_web"
    EXTRACT_PAGE = "extract_page"
    FILL_FORM = "fill_form"
    PHONE_OPEN_APP = "phone_open_app"
    PHONE_SEND_TEXT = "phone_send_text"
    PHONE_CALL = "phone_call"
    PHONE_SCREENSHOT = "phone_screenshot"
    PHONE_CONNECT = "phone_connect"
    PHONE_STATUS = "phone_status"
    RESEARCH = "research"
    ANSWER_QUESTION = "answer_question"
    SUMMARIZE = "summarize"
    COMPARE = "compare"
    GENERATE_REPORT = "generate_report"
    CREATE_AUTOMATION = "create_automation"
    RUN_AUTOMATION = "run_automation"
    SCHEDULE_TASK = "schedule_task"
    CHECK_STATUS = "check_status"
    REMEMBER = "remember"
    RECALL = "recall"
    PLAN_TASK = "plan_task"
    AGENTIC_TASK = "agentic_task"
    CREATE_PROJECT = "create_project"
    IMPROVE_SELF = "improve_self"
    # -- System status: answered deterministically, never via a language model.
    SYSTEM_STATUS = "system_status"
    CPU_STATUS = "cpu_status"
    MEMORY_STATUS = "memory_status"
    GPU_STATUS = "gpu_status"
    BATTERY_STATUS = "battery_status"
    NETWORK_STATUS = "network_status"
    # -- Device controls.
    VOLUME_CONTROL = "volume_control"
    BRIGHTNESS_CONTROL = "brightness_control"
    MEDIA_CONTROL = "media_control"
    # -- Vision.
    SCREENSHOT_ANALYSIS = "screenshot_analysis"
    # -- Filesystem.
    FIND_FILE = "find_file"
    MODIFY_FILE = "modify_file"
    DELETE_FILE = "delete_file"
    MOVE_FILE = "move_file"
    COPY_FILE = "copy_file"
    # -- Browser / web.
    BROWSER_ACTION = "browser_action"
    # -- Code / project work.
    CODE_GENERATION = "code_generation"
    CODE_EXPLANATION = "code_explanation"
    CODE_DEBUGGING = "code_debugging"
    PROJECT_ANALYSIS = "project_analysis"
    # -- Language-only intents.
    CALCULATE = "calculate"
    GENERAL_QUESTION = "general_question"
    CONVERSATION = "conversation"
    CHAT = "chat"
    CLARIFY = "clarify"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# The specification's names for capabilities this taxonomy already has. A
# second intent per synonym would route to the same handler twice; an alias is
# the same name under a different spelling, so it resolves instead.
INTENT_ALIASES: dict[str, IntentName] = {
    "launch_website": IntentName.NAVIGATE,
    "create_file": IntentName.WRITE_FILE,
    "execute_command": IntentName.RUN_COMMAND,
    "screenshot": IntentName.TAKE_SCREENSHOT,
    "explain": IntentName.ANSWER_QUESTION,
    "unknown": IntentName.CLARIFY,
}


def resolve_intent(name: str) -> IntentName | None:
    """The intent for a name from this taxonomy OR its synonym set.

    Returns ``None`` for a name this system does not have: the caller decides
    whether that is an error, rather than a guess being made here.
    """
    candidate = str(name or "").strip().lower().replace("-", "_")
    if not candidate:
        return None
    try:
        return IntentName(candidate)
    except ValueError:
        return INTENT_ALIASES.get(candidate)


def _ensure_tuple(value: "str | tuple[str, ...]") -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True, slots=True)
class IntentRule:
    """One deterministic fast-path rule: phrase patterns -> intent."""
    intent: IntentName
    patterns: tuple[str, ...] = ()            # substring needles (lowercased)
    prefixes: tuple[str, ...] = ()            # startswith needles
    exact: tuple[str, ...] = ()               # whole normalized input
    regex: tuple[str, ...] = ()               # regex patterns
    entity: str = ""                          # which entity the remainder names
    confidence: float = 0.9

    def __post_init__(self) -> None:
        # Tolerate a bare string where a tuple was intended (the classic
        # `regex=(r"...")` missing-comma typo, which would otherwise be
        # iterated CHARACTER BY CHARACTER and match everything).
        object.__setattr__(self, "patterns", _ensure_tuple(self.patterns))
        object.__setattr__(self, "prefixes", _ensure_tuple(self.prefixes))
        object.__setattr__(self, "exact", _ensure_tuple(self.exact))
        regex = _ensure_tuple(self.regex)
        for pattern in regex:
            re.compile(pattern)  # fail fast at registration, not at match time
        object.__setattr__(self, "regex", regex)

    def matches(self, normalized: str) -> bool:
        if any(item == normalized for item in self.exact):
            return True
        if any(normalized.startswith(prefix) for prefix in self.prefixes):
            return True
        if any(needle in normalized for needle in self.patterns):
            return True
        return any(re.search(pattern, normalized) for pattern in self.regex)


class CapabilitySource(StrEnum):
    """Where a capability came from — which is also what it claims.

    A DECLARED capability is a subsystem saying "I implement this verb". A TOOL
    one is projected from the tool catalogue, and an ACTION one is a step an
    executor can actually carry out. Reporting the source is what keeps the
    three from being read as one kind of statement.
    """

    DECLARED = "declared"
    TOOL = "tool"
    ACTION = "action"


class CapabilityAvailability(StrEnum):
    """Whether this installation can do it right now, and if not, why not.

    UNKNOWN is a real answer: a capability that needs a model nobody has probed
    for is not "available", and calling it unavailable would be a claim the
    registry cannot support.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


#: The namespace a capability's id takes when it declares none of its own, by
#: the subsystem that implements it. ``open_application`` (desktop_controller)
#: therefore reads as ``desktop.open_application``.
_EXECUTOR_NAMESPACES: dict[str, str] = {
    "agent_coordinator": "agents",
    "automation_manager": "automation",
    "browser_controller": "browser",
    "chat_brain": "chat",
    "code_agent": "developer",
    "desktop_controller": "desktop",
    "explore_service": "research",
    "file_manager": "filesystem",
    "memory_manager": "memory",
    "orchestrator": "system",
    "phone_controller": "phone",
    "planning_engine": "planning",
    "project_manager": "project",
    "scheduler": "scheduler",
    "scratch_brain": "compute",
    "self_improvement_engine": "self_improvement",
    "system_monitor": "system",
    "vision_pipeline": "vision",
}


def _slug(value: str) -> str:
    """A dotted-id segment: lowercase words joined by underscores."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())
    return cleaned.strip("_")


@dataclass(frozen=True, slots=True)
class Capability:
    """A capability registered by a subsystem with the global layer.

    The first ten fields are the original registration contract and are
    unchanged. The rest is what the specification asks a capability to publish
    about itself so that the decision layer, the tool selector, a UI and a test
    can all ask ONE registry what this installation can do — with the fields
    that cannot be known at registration time (a capability's tools, examples
    and category) left empty and resolved from the catalogues the registry is
    attached to, rather than duplicated here.
    """

    capability: str
    intent: IntentName | None = None
    description: str = ""
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    executor: str = "orchestrator"
    verifier: str = ""
    supported_environments: tuple[str, ...] = ("desktop",)
    # -- what the specification asks a capability to declare ---------------
    capability_id: str = ""
    name: str = ""
    category: str = ""
    tools: tuple[str, ...] = ()
    required_models: tuple[str, ...] = ()
    supported_inputs: tuple[str, ...] = ()
    supported_outputs: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    availability_reason: str = ""
    examples: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source: CapabilitySource = CapabilitySource.DECLARED

    def __post_init__(self) -> None:
        if not str(self.capability).strip():
            raise ValueError("Capability name is required.")
        object.__setattr__(self, "required", _ensure_tuple(self.required))
        object.__setattr__(self, "optional", _ensure_tuple(self.optional))

    @property
    def risk_level(self) -> RiskLevel:
        """The specification's name for ``risk``; one value, two spellings."""
        return self.risk

    @property
    def id(self) -> str:
        """The dotted id: ``system.get_ram``, ``filesystem.read``.

        Declared when the capability knows it, otherwise built from the
        namespace of the subsystem that implements it — so every capability has
        a name a person can ask for, without a second table to keep in step.
        """
        if self.capability_id.strip():
            return self.capability_id.strip()
        namespace = _EXECUTOR_NAMESPACES.get(str(self.executor).strip(), "system")
        return f"{namespace}.{_slug(self.capability)}"

    @property
    def display_name(self) -> str:
        """A human name for the capability, defaulting to its id's last part."""
        if self.name.strip():
            return self.name.strip()
        return self.id.split(".", 1)[-1].replace("_", " ")

    @property
    def category_name(self) -> str:
        """The declared category, or the id's namespace when none was given."""
        if self.category.strip():
            return self.category.strip()
        return self.id.split(".", 1)[0]

    @property
    def requires_confirmation(self) -> bool:
        """Risk at or above MEDIUM is asked about before it runs."""
        return self.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "intent": self.intent.value if self.intent is not None else "",
            "description": self.description,
            "required": list(self.required),
            "optional": list(self.optional),
            "risk": self.risk.value,
            "executor": self.executor,
            "verifier": self.verifier,
            "supported_environments": list(self.supported_environments),
            # -- Phase 9: the same capability, described for discovery -------
            "capability_id": self.id,
            "name": self.display_name,
            "category": self.category_name,
            "tools": list(self.tools),
            "required_models": list(self.required_models),
            "supported_inputs": list(self.supported_inputs),
            "supported_outputs": list(self.supported_outputs),
            "risk_level": self.risk.value,
            "permissions": list(self.permissions),
            "availability": self.availability.value,
            "availability_reason": self.availability_reason,
            "examples": list(self.examples),
            "tags": list(self.tags),
            "source": self.source.value,
        }


class IntentRegistry:
    """Central registry of deterministic intent rules."""

    def __init__(self) -> None:
        self._rules: list[IntentRule] = []
        self._variants: dict[str, set[str]] = {}  # learned linguistic variations

    def register(self, rule: IntentRule) -> None:
        self._rules.append(rule)

    def register_variation(self, phrase: str, intent: IntentName) -> None:
        """Learn that a phrase maps to an intent (from successful resolutions)."""
        key = phrase.strip().lower()
        if key:
            self._variants.setdefault(key, set()).add(intent.value)

    def variant_intent(self, normalized: str) -> IntentName | None:
        intents = self._variants.get(normalized)
        if intents and len(intents) == 1:
            return IntentName(next(iter(intents)))
        return None

    def match(self, normalized: str) -> IntentRule | None:
        for rule in self._rules:
            if rule.matches(normalized):
                return rule
        return None

    def rules(self) -> tuple[IntentRule, ...]:
        return tuple(self._rules)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": len(self._rules),
            "learned_variants": {k: sorted(v) for k, v in self._variants.items()},
        }


#: Words that carry no capability meaning, so they never score a match.
_TASK_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "by", "can",
        "could", "do", "does", "for", "from", "how", "i", "in", "is", "it", "me",
        "my", "of", "on", "or", "please", "should", "that", "the", "then", "this",
        "to", "us", "was", "we", "what", "when", "where", "which", "why", "with",
        "would", "you",
    }
)


def _task_tokens(text: str) -> tuple[str, ...]:
    """The meaningful words of a task or a description, lowercased and deduped."""
    words = re.findall(r"[a-z0-9_]+", str(text or "").lower())
    seen: dict[str, None] = {}
    for word in words:
        if len(word) < 2 or word in _TASK_STOPWORDS:
            continue
        seen.setdefault(word, None)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    """One capability that could serve a task, with the evidence for choosing it.

    A match is a *candidate*, never a decision: discovery answers what could
    carry a task and why, and running anything remains the decision layer's
    call. ``unavailable_reason`` is populated when the capability exists on
    paper and this installation cannot do it — the answer worth having.
    """

    capability_id: str
    name: str
    description: str
    score: float
    reasons: tuple[str, ...] = ()
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    unavailable_reason: str = ""
    intent: str = ""
    category: str = ""
    tools: tuple[str, ...] = ()
    risk_level: str = ""
    requires_confirmation: bool = False
    source: CapabilitySource = CapabilitySource.DECLARED

    @property
    def available(self) -> bool:
        return self.availability is CapabilityAvailability.AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "availability": self.availability.value,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "intent": self.intent,
            "category": self.category,
            "tools": list(self.tools),
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "source": self.source.value,
        }


class CapabilityRegistry:
    """What this installation can do — one index over three declarations.

    The registry is the single place that answers "what can NovaControl do?",
    and it is deliberately a *query point* rather than a second copy of the
    facts. A capability declares what only it knows (its name, description, the
    risk, the verifier) and the registry fills the rest from the catalogues it
    is attached to:

        intent capabilities   a subsystem says "I implement this verb"
        tool capabilities     projected from the tool catalogue
        action capabilities   a step an executor says it can carry out

    Tools, examples and entities come from the intent catalog when a capability
    does not declare them, so the two layers cannot drift apart; ``best`` and
    ``for_intent`` keep answering about DECLARED capabilities only, which is
    what the routing layers have always asked.
    """

    def __init__(
        self,
        *,
        catalog: Any | None = None,
        tools: Any | None = None,
        tool_names: Callable[[], Sequence[str]] | None = None,
        model_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self._by_intent: dict[IntentName, list[Capability]] = {}
        self._by_id: dict[str, Capability] = {}
        self._catalog = catalog
        self._tools = tools
        self._tool_names = tool_names
        self._model_probe = model_probe

    # -- wiring ---------------------------------------------------------------

    def attach(
        self,
        *,
        catalog: Any | None = None,
        tools: Any | None = None,
        tool_names: Callable[[], Sequence[str]] | None = None,
        model_probe: Callable[[str], bool] | None = None,
    ) -> CapabilityRegistry:
        """Point the registry at the catalogues and probes it resolves against.

        Anything left as None stays unknown rather than being guessed: with no
        tool probe a capability is not "unavailable" for lack of one, and with
        no model probe a capability that needs a model reports UNKNOWN.
        """
        if catalog is not None:
            self._catalog = catalog
        if tools is not None:
            self._tools = tools
        if tool_names is not None:
            self._tool_names = tool_names
        if model_probe is not None:
            self._model_probe = model_probe
        return self

    # -- registration ---------------------------------------------------------

    def register(self, capability: Capability) -> None:
        """Add a capability. Two entries for one id is a bug, not a preference."""
        self._insert(capability)

    def register_many(self, capabilities: Iterable[Capability]) -> int:
        count = 0
        for capability in capabilities:
            self.register(capability)
            count += 1
        return count

    def register_tools(self, entries: Iterable[Any]) -> int:
        """Project tool metadata into capability entries (one per tool).

        A tool is a fact about this installation, so its capability says what
        the catalogue says — its risk, its permissions, its tags and examples —
        and declares no intent: it is machinery, not a user-facing verb.
        """
        count = 0
        for metadata in entries:
            projected = capability_from_tool(metadata)
            if projected is None:
                continue
            self._insert(projected)
            count += 1
        return count

    def register_action(
        self,
        action: str,
        *,
        description: str = "",
        capability_id: str = "",
        intent: IntentName | None = None,
        executor: str = "application",
        verifier: str = "",
        category: str = "",
        tools: Sequence[str] = (),
        required: Sequence[str] = (),
        optional: Sequence[str] = (),
        risk: RiskLevel = RiskLevel.LOW,
        inputs: Sequence[str] = (),
        outputs: Sequence[str] = (),
        permissions: Sequence[str] = (),
        required_models: Sequence[str] = (),
        tags: Sequence[str] = (),
        examples: Sequence[str] = (),
        availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
        availability_reason: str = "",
    ) -> Capability:
        """Declare a step an executor can actually carry out.

        This is the door for the "can this installation run tests?" kind of
        fact: the code that carries the action out declares it, once, where it
        lives. A capability declared unavailable is a real answer — "git status"
        with no git here is something the registry knows and says.
        """
        capability = Capability(
            capability=action,
            intent=intent,
            description=description or f"Carry out the {action!r} step.",
            required=tuple(required),
            optional=tuple(optional),
            risk=risk,
            executor=executor,
            verifier=verifier,
            capability_id=capability_id,
            name="",
            category=category,
            tools=tuple(tools),
            required_models=tuple(required_models),
            supported_inputs=tuple(inputs),
            supported_outputs=tuple(outputs),
            permissions=tuple(permissions),
            availability=availability,
            availability_reason=availability_reason,
            examples=tuple(examples),
            tags=tuple(tags),
            source=CapabilitySource.ACTION,
        )
        self._insert(capability)
        return capability

    def _insert(self, capability: Capability) -> None:
        existing = self._by_id.get(capability.id)
        if existing is not None:
            if existing == capability:
                return  # idempotent: the same declaration twice is not a conflict
            raise ValueError(
                f"capability id {capability.id!r} is already registered by "
                f"{existing.capability!r} (source {existing.source.value})"
            )
        self._by_id[capability.id] = capability
        if capability.intent is not None and capability.source is CapabilitySource.DECLARED:
            self._by_intent.setdefault(capability.intent, []).append(capability)

    # -- reading ---------------------------------------------------------------

    def for_intent(self, intent: IntentName) -> tuple[Capability, ...]:
        return tuple(self._by_intent.get(intent, ()))

    def best(self, intent: IntentName) -> Capability | None:
        options = self._by_intent.get(intent)
        return options[0] if options else None

    def all(self) -> tuple[Capability, ...]:
        """The DECLARED capabilities, in registration order (the original contract)."""
        flat: list[Capability] = []
        for items in self._by_intent.values():
            flat.extend(items)
        return tuple(flat)

    def everything(self) -> tuple[Capability, ...]:
        """Every capability of every source, in a stable order.

        Tools are projected HERE, from the live catalogue, rather than being
        snapshotted at some earlier moment: a tool registered after boot (a
        plugin, a restored artifact) must appear without a restart, exactly as
        ``tools_for`` resolves the tools of a declared capability at query time.
        A stored entry always wins over a projection of the same id.
        """
        merged: dict[str, Capability] = dict(self._by_id)
        for capability in self._projected_tools():
            merged.setdefault(capability.id, capability)
        return tuple(merged[key] for key in sorted(merged))

    def _projected_tools(self) -> tuple[Capability, ...]:
        """The tools this installation really has, as capabilities.

        "Really has" means a callable behind the declaration (``registered()``),
        which is the same test the tool catalogue itself uses — a capability
        that cannot run is not an answer to "what can you do?".
        """
        if self._tools is None:
            return ()
        listed = getattr(self._tools, "registered", None)
        if not callable(listed):
            return ()
        projected: list[Capability] = []
        for metadata in listed():
            capability = capability_from_tool(metadata)
            if capability is not None:
                projected.append(capability)
        return tuple(projected)

    def get(self, capability_id: str) -> Capability | None:
        """The capability with this id, or None. Accepts a bare name too.

        A bare name is resolved against the id's last segment, so ``"run_tests"``
        finds ``developer.run_tests`` — the id a person typed and the id the
        registry holds should not have to match character for character. Every
        registered id answers, projected tools included: an inventory that lists
        something this cannot then fetch would be two answers to one question.
        """
        wanted = str(capability_id or "").strip()
        if not wanted:
            return None
        candidates = (*self._by_id.values(), *self._projected_tools())
        for capability in candidates:
            if capability.id == wanted:
                return capability
        for capability in candidates:
            if capability.id.split(".", 1)[-1] == wanted:
                return capability
        return None

    def require(self, capability_id: str) -> Capability:
        found = self.get(capability_id)
        if found is None:
            raise KeyError(f"no capability {capability_id!r} in this registry")
        return found

    def capability_ids(self, *, source: CapabilitySource | None = None) -> tuple[str, ...]:
        return tuple(
            capability.id
            for capability in self.everything()
            if source is None or capability.source is source
        )

    def by_source(self, source: CapabilitySource) -> tuple[Capability, ...]:
        return tuple(item for item in self.everything() if item.source is source)

    def by_category(self, category: str) -> tuple[Capability, ...]:
        wanted = str(category or "").strip().lower()
        return tuple(item for item in self.everything() if item.category_name == wanted)

    def by_tag(self, tag: str) -> tuple[Capability, ...]:
        wanted = _slug(tag)
        return tuple(
            item for item in self.everything() if wanted in {_slug(value) for value in item.tags}
        )

    def for_tool(self, tool: str) -> tuple[Capability, ...]:
        """Which capabilities this installation reaches through a given tool."""
        wanted = str(tool or "").strip()
        return tuple(item for item in self.everything() if wanted in self.tools_for(item))

    def tools_for(self, capability: Capability | str) -> tuple[str, ...]:
        """The tools a capability uses: declared, else the catalog's, else none."""
        resolved = self.get(capability) if isinstance(capability, str) else capability
        if resolved is None:
            return ()
        if resolved.tools:
            return tuple(resolved.tools)
        if self._catalog is not None and resolved.intent is not None:
            return tuple(self._catalog.tools_for(resolved.intent))
        return ()

    def examples_for(self, capability: Capability | str) -> tuple[str, ...]:
        resolved = self.get(capability) if isinstance(capability, str) else capability
        if resolved is None:
            return ()
        if resolved.examples:
            return tuple(resolved.examples)
        if self._catalog is not None and resolved.intent is not None:
            definition = self._catalog.get(resolved.intent)
            return tuple(getattr(definition, "examples", ()) or ())
        return ()

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for capability in self.everything():
            counts[capability.category_name] = counts.get(capability.category_name, 0) + 1
        return dict(sorted(counts.items()))

    # -- availability ----------------------------------------------------------

    def _known_tools(self) -> frozenset[str] | None:
        """Tools this installation declares, or None when that cannot be known.

        The catalogue is what counts: a live registry holding only the tools a
        route has already touched would call a lazily-registered tool missing.
        """
        names: set[str] = set()
        known = False
        if self._tools is not None:
            listed = getattr(self._tools, "names", None)
            if callable(listed):
                names.update(str(name) for name in listed())
                known = True
        if self._tool_names is not None:
            names.update(str(name) for name in self._tool_names())
            known = True
        return frozenset(names) if known else None

    def availability_of(self, capability: Capability | str) -> tuple[CapabilityAvailability, str]:
        """Whether this installation can do it now, and the reason when it cannot."""
        resolved = self.get(capability) if isinstance(capability, str) else capability
        if resolved is None:
            return CapabilityAvailability.UNKNOWN, "no such capability is registered"
        if resolved.availability is CapabilityAvailability.UNAVAILABLE:
            return CapabilityAvailability.UNAVAILABLE, (
                resolved.availability_reason
                or "declared unavailable by the subsystem that registers it"
            )
        if resolved.required_models:
            needed = ", ".join(resolved.required_models)
            if self._model_probe is None:
                return CapabilityAvailability.UNKNOWN, (
                    f"needs a model ({needed}) and no model probe is wired here, so "
                    "this cannot be confirmed"
                )
            missing = tuple(
                name for name in resolved.required_models if not self._model_probe(name)
            )
            if missing:
                return CapabilityAvailability.UNAVAILABLE, (
                    "needs a model this installation does not have: " + ", ".join(missing)
                )
        declared_tools = self.tools_for(resolved)
        known_tools = self._known_tools()
        if (
            declared_tools
            and known_tools is not None
            and not any(name in known_tools for name in declared_tools)
        ):
            return CapabilityAvailability.UNAVAILABLE, (
                "no tool in this installation carries it: " + ", ".join(declared_tools)
            )
        if resolved.availability is CapabilityAvailability.UNKNOWN:
            return CapabilityAvailability.UNKNOWN, (
                resolved.availability_reason or "declared unknown by its own subsystem"
            )
        return CapabilityAvailability.AVAILABLE, ""

    def available(self) -> tuple[Capability, ...]:
        """Capabilities this installation can do now, in id order."""
        return tuple(
            item
            for item in self.everything()
            if self.availability_of(item)[0] is CapabilityAvailability.AVAILABLE
        )

    def unavailable(self) -> tuple[Capability, ...]:
        """Capabilities that exist on paper and cannot run here (the honest gaps)."""
        return tuple(
            item
            for item in self.everything()
            if self.availability_of(item)[0] is CapabilityAvailability.UNAVAILABLE
        )

    # -- discovery -------------------------------------------------------------

    def discover(
        self,
        query: str,
        *,
        intent: IntentName | str | None = None,
        tools: Sequence[str] = (),
        models: Sequence[str] = (),
        categories: Sequence[str] = (),
        include_unavailable: bool = False,
        limit: int = 8,
    ) -> tuple[CapabilityMatch, ...]:
        """Which capabilities could serve this task, and why. Executes nothing.

        Ranking is by evidence, and every match says what that evidence was:

            the task names the id            4.0
            a declared tag matches a word    3.0 per word
            the id or name matches a word    2.5 per word
            an example's words match         1.5 per word
            the description matches a word   1.0 per word
            the caller can supply a tool     1.5
            the caller can supply a model    1.5

        ``intent`` restricts the answer to capabilities declared for that intent
        (an action that names its intent still appears); unavailable capabilities
        are left out unless asked for, and always carry the reason. A capability
        exists on paper, so finding one is never permission to run it.
        """
        tokens = _task_tokens(query)
        # The id rule reads the task as written (lowercased, whitespace folded),
        # NOT as tokens: see ``_score``.
        query_text = " ".join(str(query).lower().split())
        wanted_intent = (
            resolve_intent(intent) if isinstance(intent, str) else intent
        )
        wanted_categories = {_slug(value) for value in categories if str(value).strip()}
        wanted_tools = {str(name).strip() for name in tools if str(name).strip()}
        wanted_models = {str(name).strip() for name in models if str(name).strip()}

        matches: list[CapabilityMatch] = []
        for capability in self.everything():
            availability, reason = self.availability_of(capability)
            if availability is CapabilityAvailability.UNAVAILABLE and not include_unavailable:
                continue
            if wanted_categories and capability.category_name not in wanted_categories:
                continue
            if wanted_intent is not None and capability.intent is not wanted_intent:
                continue
            score, reasons = self._score(
                capability, tokens, wanted_tools, wanted_models, query_text
            )
            if score <= 0.0:
                continue
            matches.append(
                CapabilityMatch(
                    capability_id=capability.id,
                    name=capability.display_name,
                    description=capability.description,
                    score=score,
                    reasons=reasons,
                    availability=availability,
                    unavailable_reason=reason,
                    intent=capability.intent.value if capability.intent else "",
                    category=capability.category_name,
                    tools=self.tools_for(capability),
                    risk_level=capability.risk.value,
                    requires_confirmation=capability.requires_confirmation,
                    source=capability.source,
                )
            )

        matches.sort(key=lambda match: (-match.score, match.capability_id))
        return tuple(matches[: max(0, int(limit))]) if limit else tuple(matches)

    def _score(
        self,
        capability: Capability,
        tokens: Sequence[str],
        wanted_tools: set[str],
        wanted_models: set[str],
        query: str = "",
    ) -> tuple[float, tuple[str, ...]]:
        """Rank one capability against a task, with the evidence for the score.

        ``query`` is the task as TEXT, because "the task names this capability"
        is a statement about how it was written: a dotted id is not a word, so
        tokenizing first destroyed the one signal that says the caller meant
        THIS capability and not one that merely shares a word with it.
        """
        if not tokens and not wanted_tools and not wanted_models:
            return 0.0, ()
        score = 0.0
        reasons: list[str] = []
        task = set(tokens)
        identifier = capability.id.lower()
        if identifier and identifier in query:
            score += 4.0
            reasons.append(f"the task names {identifier}")
        tag_hits = sorted(task & {_slug(tag) for tag in capability.tags})
        if tag_hits:
            score += 3.0 * len(tag_hits)
            reasons.append("tag(s) matched: " + ", ".join(tag_hits))
        name_hits = sorted(task & set(_task_tokens(identifier.replace(".", " "))))
        if name_hits:
            score += 2.5 * len(name_hits)
            reasons.append("name matched: " + ", ".join(name_hits))
        example_hits = sorted(
            task
            & {
                word
                for example in self.examples_for(capability)[:8]
                for word in _task_tokens(example)
            }
        )
        if example_hits:
            score += 1.5 * len(example_hits)
            reasons.append("example(s) matched: " + ", ".join(example_hits))
        description_hits = sorted(task & set(_task_tokens(capability.description)))
        if description_hits:
            score += 1.0 * len(description_hits)
            reasons.append("description matched: " + ", ".join(description_hits))
        declared_tools = set(self.tools_for(capability))
        if wanted_tools & declared_tools:
            score += 1.5
            reasons.append(
                "the caller can supply: " + ", ".join(sorted(wanted_tools & declared_tools))
            )
        if wanted_models & set(capability.required_models):
            score += 1.5
            reasons.append(
                "the caller can supply the model: "
                + ", ".join(sorted(wanted_models & set(capability.required_models)))
            )
        return score, tuple(reasons)

    # -- reporting -------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Counts for a status readout: what exists, what can run, what cannot."""
        everything = self.everything()
        by_source = {source.value: 0 for source in CapabilitySource}
        for capability in everything:
            by_source[capability.source.value] += 1
        unavailable = self.unavailable()
        unknown = tuple(
            item
            for item in everything
            if self.availability_of(item)[0] is CapabilityAvailability.UNKNOWN
        )
        return {
            "total": len(everything),
            "by_source": by_source,
            "categories": self.categories(),
            "available": len(everything) - len(unavailable) - len(unknown),
            "unavailable": len(unavailable),
            "unknown": len(unknown),
            "unavailable_ids": [item.id for item in unavailable],
            "model_probe": self._model_probe is not None,
            "tool_probe": self._tool_names is not None or self._tools is not None,
        }

    def to_dict(self, *, include_projected: bool = False) -> dict[str, Any]:
        """The declared capabilities, as the API has always published them.

        ``include_projected`` adds the tool and action projections, which is what
        a capability browser wants and what the routing layers do not: they ask
        ``for_intent``/``best`` and have always meant the declared set.
        """
        items = self.everything() if include_projected else self.all()
        return {
            "capabilities": [cap.to_dict() for cap in items],
            "count": len(items),
            "registry": self.report(),
        }


def capability_from_tool(metadata: Any) -> Capability | None:
    """Project one tool's metadata into a capability, or None if it is unusable."""
    name = str(getattr(metadata, "name", "") or "").strip()
    if not name:
        return None
    category = getattr(metadata, "category", None)
    category_name = str(getattr(category, "value", category) or "").strip().lower()
    declared_permissions = getattr(metadata, "permissions", ()) or ()
    permissions = tuple(str(getattr(scope, "value", scope)) for scope in declared_permissions)
    required = getattr(metadata, "required_permission", None)
    if required is not None:
        value = str(getattr(required, "value", required))
        if value and value not in permissions:
            permissions = (*permissions, value)
    risk = getattr(metadata, "risk", None)
    risk_level = risk if isinstance(risk, RiskLevel) else RiskLevel.LOW
    schema = getattr(metadata, "output_schema", None)
    outputs = tuple(
        str(getattr(parameter, "name", ""))
        for parameter in (getattr(schema, "parameters", ()) or ())
        if str(getattr(parameter, "name", "")).strip()
    )
    input_schema = getattr(metadata, "input_schema", None)
    inputs = tuple(
        str(getattr(parameter, "name", ""))
        for parameter in (getattr(input_schema, "parameters", ()) or ())
        if str(getattr(parameter, "name", "")).strip()
    )
    return Capability(
        capability=name,
        intent=None,
        description=str(getattr(metadata, "description", "") or ""),
        risk=risk_level,
        executor=name,
        category=category_name or "tool",
        tools=(name,),
        permissions=permissions,
        supported_inputs=inputs,
        supported_outputs=outputs,
        examples=tuple(str(item) for item in (getattr(metadata, "examples", ()) or ())),
        tags=tuple(str(item) for item in (getattr(metadata, "tags", ()) or ())),
        source=CapabilitySource.TOOL,
    )


@dataclass(frozen=True, slots=True)
class StructuredIntent:
    """The standardized output of the global intelligence layer.

    This is the live contract between language and execution, so its fields are
    strict and its responsibilities are separated:

      understanding  goal / entities / actions / confidence / source
      requirements   requires_llm / vision / web / tools / confirmation
      evidence       decision (how the route was chosen), latency_ms

    The flags are ADVISORY to the planner — they describe what the request
    needs, never what may be executed. Authorization stays with the approval
    layer, which is why nothing here can request its own execution.
    """

    raw_input: str
    normalized_input: str
    intent: IntentName
    action: str = ""
    target_kind: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    needs_clarification: bool = False
    clarification_question: str = ""
    ambiguity: tuple[str, ...] = ()
    source: str = "fast_path"  # fast_path | fuzzy | learned_variant | contextual | lexical | semantic | multi_intent | clarification
    # -- what the request is FOR (the spec's UserIntent surface) ---------------
    goal: str = ""
    actions: tuple[str, ...] = ()
    # -- what the request NEEDS (advisory; never an authorization) -------------
    requires_llm: bool = False
    requires_vision: bool = False
    requires_web: bool = False
    requires_tools: bool = False
    requires_confirmation: bool = False
    reasoning_level: str = "none"  # none | low | high | vision
    # -- how it was understood, and what it cost -----------------------------
    decision: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    unresolved_steps: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)

    def with_(self, **changes: Any) -> "StructuredIntent":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "raw_input": self.raw_input,
            "normalized_input": self.normalized_input,
            "intent": self.intent.value,
            "action": self.action,
            "target_kind": self.target_kind,
            "entities": dict(self.entities),
            "parameters": dict(self.parameters),
            "constraints": list(self.constraints),
            "references": list(self.references),
            "context": dict(self.context),
            "confidence": round(self.confidence, 3),
            "needs_clarification": self.needs_clarification,
            "clarification_question": self.clarification_question,
            "ambiguity": list(self.ambiguity),
            "source": self.source,
            "goal": self.goal,
            "actions": list(self.actions),
            "requires_llm": self.requires_llm,
            "requires_vision": self.requires_vision,
            "requires_web": self.requires_web,
            "requires_tools": self.requires_tools,
            "requires_confirmation": self.requires_confirmation,
            "reasoning_level": self.reasoning_level,
            "decision": dict(self.decision),
            "latency_ms": round(self.latency_ms, 3),
            "unresolved_steps": list(self.unresolved_steps),
        }
