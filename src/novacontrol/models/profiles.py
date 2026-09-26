"""What a model CAN DO, declared once and selected by — never by its name.

Phase 7 starts from one refusal: NovaControl must not assume that one model
handles everything, and it must not decide which model to use by recognising a
NAME. A name is a string someone typed into a config file; capabilities are the
things a request actually needs. So every model NovaControl will use declares
what it supports:

    text  reasoning  coding  vision  tool_calling  structured_output  embeddings

and the manager selects by matching a requirement against those declarations.
``qwen3:8b`` appears in exactly one place in this codebase — the declaration
table below, as DATA — and never in a branch. A vision request asks for
``{VISION}``; whichever profile declares it is the answer, whether it is a local
build, a cloud model, or something installed next year.

Three rules keep the declarations honest:

**Unknown is not a yes.** A capability that was not declared is absent, so a
model whose abilities nobody wrote down fails a requirement instead of being
guessed at. That is the opposite of convenient and the only safe direction: a
text model trusted with an image invents coordinates, and a model trusted with
tool calls emits prose that no executor can run.

**A declaration says where it came from.** ``declared`` profiles are written
down here (or supplied by configuration); ``inferred`` ones exist because the
runtime lists a model nobody declared, and were guessed from its name. Both are
usable, and the distinction travels with the profile — because "we know this
model can see" and "its name ends in ``-vl``, so it probably can" are different
claims, and a caller is entitled to see which one it is acting on.

**Context length is a number, not a boolean.** The specification lists it beside
the capabilities; it belongs in the profile as a figure, and a model with an
unknown context window reports ``None`` rather than a fabricated one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

#: The token/parameter suffix in a model name (``qwen3:8b``, ``:3b-q4_K_M``).
_PARAM_SUFFIX = re.compile(r"[:/-](\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


class ModelCapability(StrEnum):
    """One thing a model can do, as a thing a request can require."""

    TEXT = "text"
    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    EMBEDDINGS = "embeddings"


#: What every model that is a language model at all must support. Kept separate
#: from the full set because "can hold a conversation" is the floor a caller
#: rarely has to ask for, while the rest are choices.
BASELINE: frozenset[ModelCapability] = frozenset({ModelCapability.TEXT})

#: The capabilities that mean "this is a language model", and therefore imply
#: text. Embeddings is deliberately absent: an embedder is not a chat model, and
#: "declare text too" would be a requirement only a general model could meet.
_LANGUAGE_CAPABILITIES: frozenset[ModelCapability] = frozenset(
    {
        ModelCapability.TEXT,
        ModelCapability.REASONING,
        ModelCapability.CODING,
        ModelCapability.VISION,
        ModelCapability.TOOL_CALLING,
        ModelCapability.STRUCTURED_OUTPUT,
    }
)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One model's declared abilities, size and provenance.

    ``declared`` is the honest half of this type. A profile written down (here,
    or by configuration, or by the operator) is a statement; a profile inferred
    from a model name is a guess, and ``inferred_from`` records the guess so no
    caller can mistake one for the other.
    """

    name: str
    capabilities: frozenset[ModelCapability] = BASELINE
    provider: str = ""
    context_length: int | None = None
    parameters_b: float | None = None
    #: Memory the model needs resident, when it is known. ``None`` means "the
    #: runtime has not said" — never a zero that would read as "free".
    memory_bytes: int | None = None
    declared: bool = True
    inferred_from: str = ""
    #: The runtime's OWN words about this model (``/api/show`` capabilities),
    #: empty when the runtime was never asked or had nothing to say.
    reported: frozenset[str] = frozenset()
    notes: str = ""

    def __post_init__(self) -> None:
        # A name is the key a caller looks a profile up by, so an empty one is a
        # profile nothing can select rather than a harmless omission.
        if not str(self.name).strip():
            raise ValueError("a model profile needs a name")

    # -- what it can do ------------------------------------------------------
    def supports(self, *capabilities: ModelCapability) -> bool:
        """Whether EVERY named capability is declared (the empty set is ``True``)."""
        return all(capability in self.capabilities for capability in capabilities)

    def missing(self, required: Iterable[ModelCapability]) -> tuple[ModelCapability, ...]:
        """The required capabilities this profile does NOT declare, in order."""
        return tuple(capability for capability in required if capability not in self.capabilities)

    def lacks(self, required: Iterable[ModelCapability | str]) -> tuple[ModelCapability, ...]:
        """``missing`` for a requirement that may name capabilities as strings."""
        return self.missing(sorted(as_capabilities(required), key=lambda c: c.value))

    @property
    def runtime_confirmed(self) -> bool:
        """Whether a runtime REPORTED this model's capabilities (not a guess)."""
        return bool(self.reported)

    @property
    def is_local(self) -> bool:
        """Whether this profile describes something running on this machine.

        Deliberately not a heuristic on the name: the provider says where the
        model lives, and an empty provider means nobody said — which is reported
        as unknown rather than assumed local.
        """
        return self.provider.strip().lower() in _LOCAL_PROVIDERS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "provider": self.provider,
            "context_length": self.context_length,
            "parameters_b": self.parameters_b,
            "memory_bytes": self.memory_bytes,
            "declared": self.declared,
            "inferred_from": self.inferred_from,
            "reported": sorted(self.reported),
            "runtime_confirmed": self.runtime_confirmed,
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ModelProfile:
        """A profile from configuration, tolerating partial declarations.

        An unknown capability name is DROPPED rather than raising: a config typo
        must not be the reason the model layer cannot start. It is also not
        silently widened into something else — the profile simply does not claim
        an ability nobody could spell.
        """
        known = {capability.value: capability for capability in ModelCapability}
        raw = data.get("capabilities", ())
        if isinstance(raw, str):
            raw = [raw]
        capabilities = frozenset(
            known[str(item).strip().lower()]
            for item in raw
            if str(item).strip().lower() in known
        )
        if not capabilities:
            capabilities = BASELINE
        return cls(
            name=str(data.get("name", "") or "").strip(),
            capabilities=capabilities,
            provider=str(data.get("provider", "") or "").strip(),
            context_length=_positive_int(data.get("context_length")),
            parameters_b=_positive_float(data.get("parameters_b")),
            memory_bytes=_positive_int(data.get("memory_bytes")),
            declared=True,
            notes=str(data.get("notes", "") or "").strip(),
        )


#: Provider names that mean "on this machine". Anything else — including an
#: empty string — is not claimed to be local.
_LOCAL_PROVIDERS = frozenset({"ollama", "llama.cpp", "llamacpp", "local", "lmstudio", "vllm"})


#: The runtime's OWN words for a capability, and what each one confirms. This
#: is the only vocabulary in this module that comes from a measurement rather
#: than from a person or a name.
REPORTED_CAPABILITIES: dict[str, ModelCapability] = {
    "completion": ModelCapability.TEXT,
    "text": ModelCapability.TEXT,
    "vision": ModelCapability.VISION,
    "tools": ModelCapability.TOOL_CALLING,
    "tool_calling": ModelCapability.TOOL_CALLING,
    "thinking": ModelCapability.REASONING,
    "reasoning": ModelCapability.REASONING,
    "embedding": ModelCapability.EMBEDDINGS,
    "embeddings": ModelCapability.EMBEDDINGS,
}

#: The capabilities a runtime CAN deny by not listing them, because its own
#: vocabulary has a word for each. Coding and structured output are deliberately
#: absent: no runtime here reports "coding", so a runtime silence about it is not
#: evidence and must never remove a declared ability. Denying what a runtime
#: cannot express is how a capability table starts lying in the other direction.
_DENIABLE: frozenset[ModelCapability] = frozenset(
    {
        ModelCapability.TEXT,
        ModelCapability.VISION,
        ModelCapability.TOOL_CALLING,
        ModelCapability.REASONING,
        ModelCapability.EMBEDDINGS,
    }
)


def as_capabilities(values: Iterable[Any]) -> frozenset[ModelCapability]:
    """Normalise a capability requirement into enum members.

    Public entry points are handed capabilities in whatever form reads well at
    the call site — ``{"vision"}`` is how a person writes it, and
    ``{ModelCapability.VISION}`` is how a type checker likes it. Both must mean
    the same thing, and until this existed only the second one survived: because
    a ``StrEnum`` member equals its value, a set of strings matched profiles
    correctly and then CRASHED when a rejection reason formatted ``.value`` — so
    the bug appeared exactly when the manager was explaining why it could not
    help.

    An unrecognised name RAISES rather than being dropped. Dropping is right for
    a config file (see :meth:`ModelProfile.from_mapping`) because a typo there
    must not stop the process; dropping is wrong for a REQUIREMENT, because
    ``{"visoin"}`` would quietly become "nothing in particular" and be answered
    by a model that cannot see. This is a programming error and it says so.
    """
    normalised: set[ModelCapability] = set()
    for value in values:
        if isinstance(value, ModelCapability):
            normalised.add(value)
            continue
        candidate = str(value).strip().lower().replace("-", "_")
        member = next(
            (known for known in ModelCapability if known.value == candidate), None
        )
        if member is None:
            raise ValueError(
                f"unknown capability {value!r}; expected one of "
                + ", ".join(sorted(known.value for known in ModelCapability))
            )
        normalised.add(member)
    return frozenset(normalised)


def apply_reported(
    profile: ModelProfile, reported: Iterable[str]
) -> ModelProfile:
    """Reconcile a profile with what the RUNTIME says the model can do.

    A declaration is a claim and a name is a guess; ``/api/show`` is a
    measurement, and a measurement outranks both. Two directions, because they
    are not symmetric:

    * a capability the runtime CONFIRMS is added — including one nobody declared
      and no name mentioned, which is how a model pulled five minutes ago
      becomes selectable for the job it is actually good at;
    * a capability the runtime can express and did NOT list is removed — this is
      the case the whole function exists for, because a text-only build named
      ``llava`` is exactly what a name guess gets wrong, and the runtime is the
      one party that knows.

    Silence changes nothing. An empty or unreadable report means nobody measured
    anything, so the declaration (or the name guess) stands as the best evidence
    available, still marked as what it is.
    """
    words = {str(word).strip().lower() for word in reported if str(word).strip()}
    if not words:
        return profile
    confirmed = {
        REPORTED_CAPABILITIES[word] for word in words if word in REPORTED_CAPABILITIES
    }
    capabilities = (set(profile.capabilities) | confirmed) - (_DENIABLE - confirmed)
    return replace(
        profile,
        capabilities=frozenset(capabilities),
        reported=frozenset(words),
    )


#: The models this project actually uses, declared with what they can do.
#:
#: This table is DATA. Nothing branches on these names: selection is a set
#: comparison against ``capabilities``, so a model that is not listed here is
#: still selectable (as an inferred profile) the moment the runtime reports it,
#: and a listed model that stops being installed simply stops being found.
KNOWN_MODELS: tuple[ModelProfile, ...] = (
    ModelProfile(
        name="qwen3:8b",
        capabilities=frozenset(
            {
                ModelCapability.TEXT,
                ModelCapability.REASONING,
                ModelCapability.CODING,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
        provider="ollama",
        context_length=32768,
        parameters_b=8.0,
        notes="the general-purpose local brain: text, reasoning, coding, tool calls",
    ),
    ModelProfile(
        name="llava",
        capabilities=frozenset(
            {
                ModelCapability.TEXT,
                ModelCapability.VISION,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
        provider="ollama",
        context_length=4096,
        parameters_b=7.0,
        notes="a non-reasoning VLM: answers about an image without spending its budget thinking",
    ),
    ModelProfile(
        name="moondream",
        capabilities=frozenset(
            {ModelCapability.TEXT, ModelCapability.VISION, ModelCapability.STRUCTURED_OUTPUT}
        ),
        provider="ollama",
        context_length=2048,
        parameters_b=1.8,
        notes="a small, fast VLM — the cheapest thing that can look at a screenshot",
    ),
    ModelProfile(
        name="qwen2.5vl",
        capabilities=frozenset(
            {
                ModelCapability.TEXT,
                ModelCapability.VISION,
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.TOOL_CALLING,
            }
        ),
        provider="ollama",
        context_length=32768,
        parameters_b=3.0,
        notes="a vision model that also calls tools",
    ),
    ModelProfile(
        name="llama3.2",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.TOOL_CALLING}),
        provider="ollama",
        context_length=8192,
        parameters_b=3.0,
        notes="a small text model",
    ),
    ModelProfile(
        name="nomic-embed-text",
        capabilities=frozenset({ModelCapability.EMBEDDINGS}),
        provider="ollama",
        context_length=8192,
        parameters_b=0.14,
        notes="embeddings only — it cannot hold a conversation and is not offered as if it could",
    ),
)


def infer_profile(name: str, *, provider: str = "") -> ModelProfile:
    """A profile guessed from a model name, MARKED as a guess.

    The runtime lists models nobody declared — a user pulls a new build and it
    appears. Refusing to consider it would make the manager useless exactly when
    it is most needed, and claiming its abilities would be a fabrication, so
    this reads the conventions that are actually reliable (an embedding model
    embeds; a name with a vision marker sees) and marks the result
    ``inferred_from`` so every caller can tell it was guessed.

    Deliberately conservative: a name it cannot read produces TEXT and nothing
    else. Guessing "reasoning" or "tool_calling" from a name is how a model that
    cannot call tools gets asked to.
    """
    lowered = f" {str(name).strip().lower()} "
    capabilities = {ModelCapability.TEXT}
    inferred: list[str] = []
    if any(marker in lowered for marker in ("embed", "nomic-embed", "bge-", "e5-")):
        return ModelProfile(
            name=name,
            capabilities=frozenset({ModelCapability.EMBEDDINGS}),
            provider=provider,
            parameters_b=_parameters_from_name(name),
            declared=False,
            inferred_from="name says embeddings",
            notes="guessed from the name: not offered as a chat model",
        )
    if any(
        marker in lowered
        for marker in ("-vl", "vl:", "vision", "llava", "moondream", "minicpm-v")
    ):
        capabilities.add(ModelCapability.VISION)
        inferred.append("a vision marker is in the name")
    if any(
        marker in lowered
        for marker in ("qwen", "llama", "mistral", "gemma", "phi", "deepseek")
    ):
        # These families ship tool-capable and instruct-tuned builds by default.
        capabilities.update({ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT})
        inferred.append("an instruct-family name")
    return ModelProfile(
        name=name,
        capabilities=frozenset(capabilities),
        provider=provider,
        parameters_b=_parameters_from_name(name),
        declared=False,
        inferred_from="; ".join(inferred) or "the name carried no readable marker",
        notes="guessed from the name; it was not declared anywhere",
    )


class ModelRegistry:
    """Every model NovaControl knows about, declared and discovered.

    Two sources, one lookup. Declarations (the table above, plus configuration)
    carry real claims; discovery merges in whatever the runtime actually offers,
    inferring a profile only for names nothing declared. The registry never
    reaches for a runtime itself — discovery is handed a list — so it is a pure
    data structure that can be tested without a model installed.
    """

    def __init__(
        self,
        profiles: Sequence[ModelProfile] = (),
        *,
        with_defaults: bool = True,
    ) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        if with_defaults:
            for profile in KNOWN_MODELS:
                self.register(profile)
        for profile in profiles:
            self.register(profile)

    # -- registering ---------------------------------------------------------
    def register(self, profile: ModelProfile) -> ModelProfile:
        """Add a declaration. A later declaration of the same name replaces it.

        Replacement is the useful behaviour: configuration declaring ``llava``
        with a measured memory figure should win over the built-in note, and an
        operator's ``vision: false`` must be able to correct a guess.
        """
        self._profiles[_key(profile.name)] = profile
        return profile

    def register_mapping(self, data: Mapping[str, Any]) -> ModelProfile:
        return self.register(ModelProfile.from_mapping(data))

    def _resolve(self, name: str) -> ModelProfile | None:
        return self._profiles.get(_key(name))

    def discover(
        self,
        names: Iterable[str],
        *,
        provider: str = "",
        reported: Mapping[str, Iterable[str]] | None = None,
    ) -> tuple[ModelProfile, ...]:
        """Merge what the runtime offers; return the profiles now available.

        Names already known keep their declared profile (with the provider
        filled in when it was blank — the discovery source just told us where
        the model really lives). Unknown names get an INFERRED profile, added to
        the registry so the next lookup finds the same answer.

        ``reported`` is the runtime's own capability words per model, when it was
        asked: those are applied LAST, so a measurement corrects a declaration or
        a guess rather than being overwritten by either.
        """
        reported = reported or {}
        discovered: list[ModelProfile] = []
        for raw in names:
            name = str(raw).strip()
            if not name:
                continue
            known = self._resolve(name)
            if known is None:
                known = self.register(infer_profile(name, provider=provider))
            elif provider and not known.provider:
                known = self.register(replace(known, provider=provider))
            words = reported.get(_key(name)) or reported.get(name)
            if words:
                known = self.register(apply_reported(known, words))
            discovered.append(known)
        return tuple(discovered)

    # -- looking up ----------------------------------------------------------
    def get(self, name: str) -> ModelProfile | None:
        """The profile for ``name``, or ``None`` — never a guess by accident."""
        return self._resolve(name)

    def profile_for(self, name: str, *, provider: str = "") -> ModelProfile:
        """The profile for ``name``, inferring one when nothing declared it."""
        known = self._resolve(name)
        if known is not None:
            return known
        return self.register(infer_profile(name, provider=provider))

    def all(self) -> tuple[ModelProfile, ...]:
        return tuple(self._profiles.values())

    def declared(self) -> tuple[ModelProfile, ...]:
        return tuple(profile for profile in self._profiles.values() if profile.declared)

    def supporting(self, *required: ModelCapability | str) -> tuple[ModelProfile, ...]:
        """Every profile that declares ALL of ``required``, in registry order."""
        wanted = as_capabilities(required)
        return tuple(
            profile
            for profile in self._profiles.values()
            if profile.capabilities >= wanted
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "known": len(self._profiles),
            "declared": len(self.declared()),
            "capabilities": sorted(capability.value for capability in ModelCapability),
            "models": [profile.to_dict() for profile in self._profiles.values()],
        }


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    """The outcome of matching a requirement against what is available.

    ``profile`` is the winner and is ``None`` when nothing could satisfy the
    requirement — which is a RESULT, not an error: a caller that cannot be
    served locally is exactly the caller the cloud fallback exists for, and
    inventing a runner-up would hide that.
    """

    required: frozenset[ModelCapability]
    profile: ModelProfile | None
    reason: str
    considered: tuple[str, ...] = ()
    rejected: Mapping[str, str] = field(default_factory=dict)
    available_memory_bytes: int | None = None

    @property
    def found(self) -> bool:
        return self.profile is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": sorted(capability.value for capability in self.required),
            "selected": self.profile.name if self.profile else "",
            "reason": self.reason,
            "considered": list(self.considered),
            "rejected": dict(self.rejected),
            "available_memory_bytes": self.available_memory_bytes,
        }


def select_profile(
    required: Iterable[ModelCapability],
    candidates: Sequence[ModelProfile],
    *,
    prefer_loaded: str = "",
    preferred: str = "",
    available_memory_bytes: int | None = None,
    headroom_bytes: int = 0,
    latency_preference: str = "balanced",
) -> CapabilityMatch:
    """Pick the model that best satisfies ``required`` out of ``candidates``.

    The rules, in order, and none of them is a model name:

    1. **Capability first.** A candidate that cannot do what was asked is not a
       candidate. This is the whole point of the registry: a request for
       ``{VISION}`` is satisfied by whatever declares vision, and nothing else
       is even considered. A candidate whose measured footprint does not fit in
       the memory left (plus the caller's headroom) is rejected too, with that
       reason — "not measured" is not "too big", but "too big" is final.
    2. **An explicit choice wins.** When configuration names a model and it
       satisfies the requirement, it is the answer. An operator's decision is
       not a preference to be out-voted by a size heuristic.
    3. **A loaded model that satisfies wins next.** Switching costs a load and
       an unload — seconds on a CPU-only machine — so a model already resident
       is chosen over a marginal upgrade that has to be fetched first. This is
       the "avoid unnecessary switching" rule, enforced here so no caller has to
       remember it.
    4. **Then the smallest that fits, unless latency says otherwise.** On a
       machine this size a smaller model is a faster one, and the default is
       RAM-conscious; ``latency_preference="quality"`` prefers the largest
       instead, which is a caller's choice rather than an assumption. When the
       ability that makes a model large is NOT needed (a vision model answering
       a text request), the specialist wins the tie: extra capability that the
       request did not ask for is a cost, not a feature.
    """
    asked = as_capabilities(required)
    # Text is implied by the capabilities that ARE conversation, and NOT by
    # embeddings: an embedding model is not a chat model, and requiring text of
    # one would reject every purpose-built embedder on the machine.
    wanted = asked | BASELINE if not asked or asked & _LANGUAGE_CAPABILITIES else asked
    considered: list[str] = []
    rejected: dict[str, str] = {}
    viable: list[ModelProfile] = []
    for profile in candidates:
        considered.append(profile.name)
        missing = profile.lacks(wanted)
        if missing:
            rejected[profile.name] = "declares no " + ", ".join(
                capability.value for capability in missing
            )
            continue
        if (
            available_memory_bytes is not None
            and profile.memory_bytes
            and profile.memory_bytes + headroom_bytes > available_memory_bytes
        ):
            rejected[profile.name] = "needs more memory than is free"
            continue
        viable.append(profile)

    if not viable:
        return CapabilityMatch(
            required=wanted,
            profile=None,
            reason=(
                "no available model declares "
                + ", ".join(sorted(capability.value for capability in wanted))
            ),
            considered=tuple(considered),
            rejected=rejected,
            available_memory_bytes=available_memory_bytes,
        )

    for label, name, reason in (
        ("preferred", preferred, "the model configuration names, and it satisfies the requirement"),
        ("resident", prefer_loaded, "already resident and satisfies the requirement"),
    ):
        if not name:
            continue
        chosen = [profile for profile in viable if _key(profile.name) == _key(name)]
        if chosen:
            del label
            return CapabilityMatch(
                required=wanted,
                profile=chosen[0],
                reason=reason,
                considered=tuple(considered),
                rejected=rejected,
                available_memory_bytes=available_memory_bytes,
            )

    def footprint(profile: ModelProfile) -> tuple[float, float]:
        # An unmeasured model sorts ahead of one we KNOW is large — not knowing a
        # size is not a reason to prefer paying the biggest price available — and
        # the declared parameter count breaks the tie between two unmeasured
        # models, so the answer is the cheapest model rather than the first
        # alphabetically. A model with neither figure sorts last within its group.
        measured = float(profile.memory_bytes) if profile.memory_bytes else 0.0
        parameters = float(profile.parameters_b) if profile.parameters_b else float("inf")
        return measured, parameters

    largest_first = str(latency_preference).strip().lower() in {"quality", "accuracy", "best"}
    # Whether the request asked for eyes. "No" is a reason to prefer a model that
    # does not claim them: a general text model is better at text than a tiny
    # vision model is, and its window is usually larger.
    wants_vision = ModelCapability.VISION in wanted

    def sort_key(profile: ModelProfile) -> tuple[Any, ...]:
        # Ability the request did NOT ask for is a cost, so it ranks above size:
        # a text request is served by a text model with a larger window rather
        # than by the smallest vision model, which would be worse at the job.
        overshoot = 0 if wants_vision or not profile.supports(ModelCapability.VISION) else 1
        size = footprint(profile)
        size_key = tuple(-value for value in size) if largest_first else size
        return (overshoot, *size_key, profile.name)

    ordered = sorted(viable, key=sort_key)
    winner = ordered[0]
    return CapabilityMatch(
        required=wanted,
        profile=winner,
        reason=(
            "the largest fitting model that satisfies the requirement"
            if largest_first
            else "the smallest fitting model that satisfies the requirement"
        ),
        considered=tuple(considered),
        rejected=rejected,
        available_memory_bytes=available_memory_bytes,
    )


def _key(name: str) -> str:
    """Model names compared the way a runtime compares them (case-folded)."""
    return str(name).strip().casefold()


def _parameters_from_name(name: str) -> float | None:
    """The parameter count a name states (``qwen3:8b`` -> 8.0), or nothing."""
    match = _PARAM_SUFFIX.search(str(name))
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches numbers
        return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
