"""What a tool IS — the description a search can look through, and a model can be asked.

A tool has always had a name, a schema and a callable. That is enough to RUN
one and not enough to FIND one: a planner asked to choose between twenty
executors cannot do it from names alone, and a retriever cannot rank tools whose
descriptions do not exist. This module is the missing half — the fields that
make a tool *findable* and *judgeable* before anything runs:

    description     one sentence, in the words a request would use
    category        what family of work it does (system, files, browser, ...)
    capabilities    the named capabilities it serves, as the registry knows them
    input schema    what a valid call looks like (validated before execution)
    output schema   what a caller may expect back
    risk            how bad a mistake would be
    permissions     what it touches — the SAME scopes the approval layer gates on
    examples        requests this tool is the answer to ("which programs are
                    eating my memory?") — the phrases the retriever matches
    tags            the operator-facing vocabulary (cpu, memory, hardware, os)

Two rules keep this honest rather than decorative:

* **Permissions are the executor's own rule, not a second copy of it.**
  :meth:`ToolMetadata.requires_approval` derives from the same permission scopes
  ``ToolExecutor`` checks, so metadata cannot claim a tool is safe while the
  executor asks a person to approve it.
* **Caching is declared, not assumed.** ``cache_ttl_s`` is a tool saying "this
  result may be reused for N seconds"; ``volatile_values`` is the same tool
  saying WHICH of its operations change by the second (CPU load does, total RAM
  does not), and ``is_cacheable`` refuses to cache those. A read-only tool is
  not automatically cacheable — a read-only reading of a moving number is the
  clearest example of a cache that lies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.tools.models import ToolSchema


class ToolCategory(StrEnum):
    """The family a tool belongs to — what a caller filters on first."""

    SYSTEM = "system"
    FILES = "files"
    DESKTOP = "desktop"
    BROWSER = "browser"
    PHONE = "phone"
    CODE = "code"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    AUTOMATION = "automation"
    VISION = "vision"
    PLANNING = "planning"
    CONVERSATION = "conversation"
    GENERIC = "generic"


#: Risk ordering, so "the riskiest capability this tool serves" has an answer.
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def highest_risk(*levels: RiskLevel) -> RiskLevel:
    """The most serious of the levels given (LOW when there are none)."""
    return max(levels, key=lambda level: _RISK_ORDER[level]) if levels else RiskLevel.LOW


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """One tool, described well enough to be searched, ranked and gated."""

    name: str
    description: str = ""
    category: ToolCategory = ToolCategory.GENERIC
    capabilities: tuple[str, ...] = ()
    input_schema: ToolSchema | None = None
    output_schema: ToolSchema | None = None
    risk: RiskLevel = RiskLevel.LOW
    permissions: tuple[PermissionScope, ...] = ()
    examples: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    #: The registered tool this describes, when one is registered. Empty means
    #: "a capability this build can dispatch but has no runtime tool behind yet"
    #: — reported, never rounded up to available.
    registered: bool = False
    #: Intents this tool carries out (filled in from the intent catalogue).
    intents: tuple[str, ...] = ()
    #: Safe to run without changing anything? Defaults to False: a tool that
    #: has not said so is assumed to do something, which is the safe direction.
    read_only: bool = False
    #: How long a result may be reused. 0 means never cached.
    cache_ttl_s: float = 0.0
    #: Argument name -> the values of it that change by the second. An operation
    #: named here is never cached, however read-only it is.
    volatile_values: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # ── the risk declaration (Phase 8.5) ────────────────────────────────────
    #: The ONE scope this tool's sensitivity is about, when a caller needs a
    #: single answer (``permissions`` above remains the full set the approval
    #: layer gates on). None means "not stated", never "no permission needed".
    required_permission: PermissionScope | None = None
    #: Whether a person must say yes before it runs, when this build's policy
    #: asks. Derived for a tool that declares nothing from its risk level, so
    #: an undeclared tool cannot quietly become a high-risk action.
    requires_confirmation: bool = False
    #: Whether the action can be undone. False is what stops an automatic retry
    #: of a half-finished operation — running it again completes something that
    #: was never finished, not undoes it.
    reversible: bool = True
    #: Whether it can delete or overwrite something a person would miss.
    destructive: bool = False
    #: Whether it changes something outside this machine (a message sent, an
    #: order placed). Never auto-retried, and never assumed to have failed
    #: safely: an external action that errored may still have happened.
    external_side_effect: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Tool metadata needs a name.")
        if self.cache_ttl_s < 0:
            raise ValueError("cache_ttl_s must not be negative.")

    # -- derived ----------------------------------------------------------------

    @property
    def risk_level(self) -> RiskLevel:
        """The specification's name for :attr:`risk` — the same value."""
        return self.risk

    def requires_approval(self) -> bool:
        """Whether the executor would ask a person before running this.

        The same rule ``ToolExecutor`` applies: a tool that declares permission
        scopes is gated when the policy requires approval for sensitive tools.
        Derived here so a search result cannot advertise a tool as harmless
        while the executor treats it as sensitive.
        """
        return bool(self.permissions)

    def is_cacheable(self, arguments: Mapping[str, Any] | None = None) -> bool:
        """Whether THIS call's result may be cached — and never a maybe."""
        return not self.cache_refusal(arguments)

    def cache_refusal(self, arguments: Mapping[str, Any] | None = None) -> str:
        """Why this call must not be cached (empty string when it may be).

        Returned rather than logged, so the caller that skipped the cache can
        say which operation it skipped and why instead of looking permanently
        slow for no visible reason.
        """
        if not self.read_only:
            return f"{self.name} is not read-only, so its result is not reused."
        if self.cache_ttl_s <= 0:
            return f"{self.name} declares no cache lifetime."
        for name, volatile in self.volatile_values.items():
            value = str((arguments or {}).get(name, "")).strip().lower()
            if value and value in volatile:
                return (
                    f"{name}={value!r} is volatile (it changes between calls), "
                    "so it is measured rather than remembered."
                )
        return ""

    @property
    def cacheable(self) -> bool:
        """Whether the tool's DEFAULT operation may be cached."""
        return self.is_cacheable({})

    def searchable_text(self) -> str:
        """Everything a retriever may match against, as one block of text.

        Deliberately includes the examples and the tags: the words a person
        would USE ("chewing up my ram") are what a query has in common with a
        tool, and the name of a tool is usually the least useful part of it.
        """
        parts = [
            self.name,
            self.name.replace("_", " "),
            self.description,
            self.category.value,
            " ".join(self.capabilities),
            " ".join(self.tags),
            " ".join(self.intents),
            " ".join(self.examples),
        ]
        return "\n".join(part for part in parts if part)

    def with_(self, **changes: Any) -> ToolMetadata:
        return replace(self, **changes)

    def merged(self, other: ToolMetadata) -> ToolMetadata:
        """This metadata with ``other``'s contributions folded in.

        Used when several sources describe one tool (a declaration, the
        capabilities that name it as their executor, the intents that reach it):
        the fields accumulate, the risk is the HIGHEST any source claims, and a
        read-only claim is only kept when no source contradicts it.
        """
        return ToolMetadata(
            name=self.name,
            description=self.description or other.description,
            category=(
                self.category
                if self.category is not ToolCategory.GENERIC
                else other.category
            ),
            capabilities=_union(self.capabilities, other.capabilities),
            input_schema=self.input_schema or other.input_schema,
            output_schema=self.output_schema or other.output_schema,
            risk=highest_risk(self.risk, other.risk),
            permissions=_union(self.permissions, other.permissions),
            examples=_union(self.examples, other.examples),
            tags=_union(self.tags, other.tags),
            registered=self.registered or other.registered,
            intents=_union(self.intents, other.intents),
            read_only=self.read_only and other.read_only,
            cache_ttl_s=max(self.cache_ttl_s, other.cache_ttl_s),
            volatile_values=_merge_volatile(self.volatile_values, other.volatile_values),
            required_permission=self.required_permission or other.required_permission,
            requires_confirmation=self.requires_confirmation or other.requires_confirmation,
            # Reversibility is the one field that gets STRICTER when sources
            # disagree: a single source calling an action irreversible is
            # enough, because the other sources only ever saw part of it.
            reversible=self.reversible and other.reversible,
            destructive=self.destructive or other.destructive,
            external_side_effect=self.external_side_effect or other.external_side_effect,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "capabilities": list(self.capabilities),
            "input_schema": _schema_dict(self.input_schema),
            "output_schema": _schema_dict(self.output_schema),
            "risk": self.risk.value,
            "permissions": [scope.value for scope in self.permissions],
            "examples": list(self.examples),
            "tags": list(self.tags),
            "registered": self.registered,
            "intents": list(self.intents),
            "read_only": self.read_only,
            "cache_ttl_s": self.cache_ttl_s,
            "volatile_values": {key: list(value) for key, value in self.volatile_values.items()},
            "requires_approval": self.requires_approval(),
            "risk_level": self.risk.value,
            "required_permission": (
                self.required_permission.value if self.required_permission else None
            ),
            "requires_confirmation": self.requires_confirmation,
            "reversible": self.reversible,
            "destructive": self.destructive,
            "external_side_effect": self.external_side_effect,
        }


def _union(left: tuple[Any, ...], right: tuple[Any, ...]) -> tuple[Any, ...]:
    """Both tuples, order preserved, duplicates dropped."""
    seen: list[Any] = []
    for item in (*left, *right):
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _merge_volatile(
    left: Mapping[str, tuple[str, ...]], right: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for source in (left, right):
        for key, values in source.items():
            merged[key] = _union(merged.get(key, ()), tuple(values))
    return merged


def _schema_dict(schema: ToolSchema | None) -> dict[str, Any] | None:
    if schema is None:
        return None
    return {
        "name": schema.name,
        "description": schema.description,
        "parameters": [
            {
                "name": parameter.name,
                "type": parameter.type,
                "required": parameter.required,
                "description": parameter.description,
            }
            for parameter in schema.parameters
        ],
    }
