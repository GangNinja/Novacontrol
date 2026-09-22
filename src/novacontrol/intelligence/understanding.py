"""The structured understanding contract, and the boundary the LLM answers in.

``StructuredIntent`` (a frozen dataclass) is NovaControl's live internal
contract — every subsystem and handler already consumes it. This module adds the
STRICT, validating, serializable form of the same idea, used at the two places
where untrusted or external data arrives:

  1. LANGUAGE MODEL OUTPUT — a model asked to classify a request returns JSON.
     That JSON is data from outside the program: it is parsed here, validated
     against the intent taxonomy, and converted into a ``StructuredIntent``.
     A model that answers with an invented intent, an out-of-range confidence,
     or prose instead of JSON produces ``None`` — never a half-built intent.
  2. API SERIALIZATION — the request-understanding block a client renders.

Two invariants are worth stating plainly, because they are security-relevant:

  * a ``UserIntent`` carries NO executable payload. It names an intent and
    entities; it cannot name a tool, a shell command, or a path to write to.
    The model understands and requests — NovaControl validates, authorizes,
    and executes (see tools/executor.py, core/security.py).
  * the ``requires_*`` flags are DEMANDS, not permissions. ``requires_tools``
    means "this will need a tool to succeed", and the approval layer is what
    decides whether the tool may run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novacontrol.intelligence.intent import IntentName, StructuredIntent

# Words a model (or a human) uses when nothing fits. They are not new intents —
# they are the taxonomy's own "ask one precise question" state.
_INTENT_ALIASES: dict[str, IntentName] = {
    "": IntentName.CLARIFY,
    "unknown": IntentName.CLARIFY,
    "none": IntentName.CLARIFY,
    "null": IntentName.CLARIFY,
    "other": IntentName.CLARIFY,
    "ambiguous": IntentName.CLARIFY,
    "clarify": IntentName.CLARIFY,
}

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class UserIntent(BaseModel):
    """Strict, validated understanding of one user request.

    Field groups mirror ``StructuredIntent`` exactly, so the two never drift:
    what was understood (intent/goal/entities/actions/target), what it needs
    (the requires_* flags), and how it was understood (confidence/source).
    """

    model_config = ConfigDict(frozen=True, extra="ignore", str_strip_whitespace=True)

    intent: IntentName
    goal: str = ""
    entities: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_llm: bool = False
    requires_vision: bool = False
    requires_web: bool = False
    requires_tools: bool = False
    requires_confirmation: bool = False
    target: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    source: str = "fast_path"
    reasoning_level: str = "none"

    @field_validator("intent", mode="before")
    @classmethod
    def _coerce_intent(cls, value: Any) -> Any:
        """Accept the taxonomy's names, and map "unknown" onto clarification."""
        if isinstance(value, IntentName):
            return value
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text in _INTENT_ALIASES:
            return _INTENT_ALIASES[text]
        return text

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> Any:
        """Read a confidence expressed either as a fraction or a percentage.

        Small local models emit ``97`` for "97%". Treating that as invalid would
        throw away an otherwise usable answer, so a value in (1, 100] is read as
        a percentage; anything else out of range is clamped.
        """
        if value is None:
            return 0.5
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.5
        if 1.0 < number <= 100.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))

    @field_validator("entities", "parameters", mode="before")
    @classmethod
    def _coerce_mapping(cls, value: Any) -> Any:
        """A model that omits entities, or sends a list of pairs, still validates."""
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            pairs: dict[str, Any] = {}
            for item in value:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    pairs[str(item[0])] = item[1]
            return pairs
        return {}

    @field_validator("actions", mode="before")
    @classmethod
    def _coerce_actions(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        return []

    def to_structured(self, *, raw_input: str, normalized_input: str, **overrides: Any) -> StructuredIntent:
        """Convert to the internal contract (the one direction that matters)."""
        fields: dict[str, Any] = {
            "raw_input": raw_input,
            "normalized_input": normalized_input,
            "intent": self.intent,
            "action": self.intent.value.split("_", 1)[0],
            "target_kind": self.target,
            "entities": dict(self.entities),
            "parameters": dict(self.parameters),
            "confidence": self.confidence,
            "source": self.source,
            "goal": self.goal,
            "actions": tuple(self.actions),
            "requires_llm": self.requires_llm,
            "requires_vision": self.requires_vision,
            "requires_web": self.requires_web,
            "requires_tools": self.requires_tools,
            "requires_confirmation": self.requires_confirmation,
            "reasoning_level": self.reasoning_level,
        }
        fields.update(overrides)
        return StructuredIntent(**fields)

    @classmethod
    def from_structured(cls, intent: StructuredIntent) -> UserIntent:
        """Adapt the internal contract to the validated/serializable form."""
        return cls(
            intent=intent.intent,
            goal=intent.goal,
            entities=dict(intent.entities),
            actions=list(intent.actions),
            confidence=intent.confidence,
            requires_llm=intent.requires_llm,
            requires_vision=intent.requires_vision,
            requires_web=intent.requires_web,
            requires_tools=intent.requires_tools,
            requires_confirmation=intent.requires_confirmation,
            target=intent.target_kind,
            parameters=dict(intent.parameters),
            source=intent.source,
            reasoning_level=intent.reasoning_level,
        )


def parse_llm_output(text: str) -> UserIntent | None:
    """Parse a language model's reply into a validated ``UserIntent``.

    Returns ``None`` for anything that is not a valid intent object — prose, an
    invented intent name, a malformed confidence, truncated JSON. ``None`` means
    "the model did not answer usably", and the caller must fall back to asking
    a question rather than guessing at a partial parse.
    """
    payload = extract_json_object(text)
    if payload is None:
        return None
    try:
        return UserIntent.model_validate(payload)
    except Exception:
        return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first complete JSON object out of a model reply.

    Models wrap JSON in prose and code fences, so a naive ``json.loads`` fails
    on otherwise perfect answers. This scans for the first balanced object,
    respecting strings and escapes, so a ``}`` inside a string value cannot
    truncate the parse.
    """
    if not text:
        return None
    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return {str(key): value for key, value in parsed[0].items()}
    return None


def _candidates(text: str) -> list[str]:
    found: list[str] = []
    balanced = _first_balanced_object(text)
    if balanced is not None:
        found.append(balanced)
    match = _JSON_BLOCK.search(text)
    if match:
        found.append(match.group(0))
    stripped = text.strip()
    if stripped.startswith("{"):
        found.append(stripped)
    return found


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
