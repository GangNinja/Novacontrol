"""Structured intent model, global intent registry, and capability registry.

This is THE contract between language and execution: every subsystem consumes
StructuredIntent, every capability registers itself here — feature modules
never parse raw user language again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4


class IntentName(StrEnum):
    """Global intent vocabulary, derived from existing NovaControl capabilities."""

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
    CHAT = "chat"
    CLARIFY = "clarify"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


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


@dataclass(frozen=True, slots=True)
class Capability:
    """A capability registered by a subsystem with the global layer."""

    capability: str
    intent: IntentName
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    executor: str = "orchestrator"
    verifier: str = ""
    supported_environments: tuple[str, ...] = ("desktop",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "intent": self.intent.value,
            "description": self.description,
            "required": list(self.required),
            "optional": list(self.optional),
            "risk": self.risk.value,
            "executor": self.executor,
            "verifier": self.verifier,
            "supported_environments": list(self.supported_environments),
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


class CapabilityRegistry:
    """Central registry of what NovaControl can do, by intent."""

    def __init__(self) -> None:
        self._capabilities: dict[IntentName, list[Capability]] = {}

    def register(self, capability: Capability) -> None:
        self._capabilities.setdefault(capability.intent, []).append(capability)

    def for_intent(self, intent: IntentName) -> tuple[Capability, ...]:
        return tuple(self._capabilities.get(intent, ()))

    def best(self, intent: IntentName) -> Capability | None:
        options = self._capabilities.get(intent)
        return options[0] if options else None

    def all(self) -> tuple[Capability, ...]:
        flat: list[Capability] = []
        for items in self._capabilities.values():
            flat.extend(items)
        return tuple(flat)

    def to_dict(self) -> dict[str, Any]:
        return {"capabilities": [cap.to_dict() for cap in self.all()]}


@dataclass(frozen=True, slots=True)
class StructuredIntent:
    """The standardized output of the global intelligence layer."""

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
    source: str = "fast_path"  # fast_path | learned_variant | contextual | semantic | clarification
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
        }
