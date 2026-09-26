"""Phase 5: intelligent tool selection.

Between the plan and the approval gate sits one question the pipeline used to
answer by accident: *which tool actually carries this step out?* The intent
catalog declares the tools an intent reaches (``IntentDefinition.tools``), the
capability registry declares the executor (``Capability.executor``), and the
runtime tool registry holds whatever has been registered with callables and
schemas. Those three answers can disagree, and until they are resolved in ONE
place the readout, the routing and the approval prompt are each free to name a
different tool.

This module resolves them, and it does exactly three things:

    candidates    every tool that could carry the intent, each with the
                  evidence for and against it;
    selection     the chosen one, with a confidence earned from that evidence
                  — declared order, entity completeness, whether it is actually
                  registered, and how risky the capability says it is;
    caution       whether the choice needs confirmation, which can only ever be
                  RAISED here (risk and the intent's own flag), never lowered.

It executes nothing, holds no state, and reaches for nothing: every input is
passed in, so the same intent and the same registry produce the same selection
and the choice is explainable after the fact. Running the tool remains the
application's job, and authorizing it remains the approval layer's — the
selection is advice, and the reason it cannot be more than advice is that the
alternative is a selector that can approve its own choice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.intelligence.intent import (
    Capability,
    CapabilityRegistry,
    IntentName,
    RiskLevel,
    StructuredIntent,
)
from novacontrol.intelligence.registry import IntentCatalog

_logger = logging.getLogger(__name__)


#: Told about a completed selection. The selector stays stateless and bus-free
#: and simply hands the answer to whoever asked to see it.
SelectionObserver = Callable[["ToolSelection"], None]


#: Where a candidate came from — reported so a surprising choice is traceable
#: to the surface that suggested it rather than to "the selector".
class ToolSource(StrEnum):
    DECLARED = "declared"  # the intent catalog names it, in preference order
    REGISTERED = "registered"  # a runtime tool registered for this intent
    EXECUTOR = "executor"  # the capability's declared executor


class SelectionReason(StrEnum):
    """Stable, machine-readable WHY for a selection (never prose to parse)."""

    DECLARED_TOOL = "declared_tool"
    REGISTERED_TOOL = "registered_tool"
    CAPABILITY_EXECUTOR = "capability_executor"
    NO_TOOL = "no_tool"


#: Risk levels that make an action worth confirming even when the intent does
#: not say so. A capability that describes itself as medium-risk is telling the
#: pipeline that a human should see the step before it runs.
_CONFIRMING_RISK: frozenset[RiskLevel] = frozenset(
    {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}
)

#: How much a later entry in the catalog's own preference order loses. Small on
#: purpose: "second choice" is a tiebreak, not a reason to distrust the tool.
_ORDER_PENALTY = 0.05

#: A tool that is confirmed registered is a *known* quantity and wins ties; the
#: bonus is smaller than one preference step so it never overrides the catalog.
_REGISTERED_BONUS = 0.02

#: Missing required entities do not make a tool wrong — they make the step
#: incomplete, and an incomplete step is exactly what the approval prompt and
#: the clarification question exist for. Applied as a multiplier so it scales
#: with how sure the rest of the evidence was.
_MISSING_ENTITY_FACTOR = 0.6


@dataclass(frozen=True, slots=True)
class ToolCandidate:
    """One tool that could carry the intent out, with the evidence about it."""

    name: str
    source: ToolSource
    score: float
    #: Position in the catalog's own preference order (0 = most preferred).
    order: int = 0
    #: ``True``/``False`` when the runtime registry could answer, ``None`` when
    #: no registry was supplied — unknown, which is not the same as missing.
    registered: bool | None = None
    executor: str = ""
    capability: str = ""
    risk: RiskLevel = RiskLevel.LOW
    requires_confirmation: bool = False
    missing_entities: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    #: What the CAPABILITY registry says about the capability behind this
    #: candidate (Phase 9.2). Reported, never used to reorder: the choice of
    #: tool must not depend on whether this machine happens to have a model
    #: installed, and a caller that must know is told rather than left to guess.
    availability: str = ""
    availability_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source.value,
            "score": round(self.score, 3),
            "order": self.order,
            "registered": self.registered,
            "executor": self.executor,
            "capability": self.capability,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "missing_entities": list(self.missing_entities),
            "reasons": list(self.reasons),
            "availability": self.availability,
            "availability_reason": self.availability_reason,
        }


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """Which tool would carry this out — and what that choice therefore needs.

    ``tool`` is empty when nothing declared or registered can carry the intent,
    which is reported as such (``reason`` ``no_tool``, ``dispatchable`` false)
    rather than papered over with a plausible-looking name.
    """

    tool: str = ""
    capability: str = ""
    executor: str = ""
    source: ToolSource | None = None
    confidence: float = 0.0
    requires_confirmation: bool = False
    missing_entities: tuple[str, ...] = ()
    reason: SelectionReason = SelectionReason.NO_TOOL
    candidates: tuple[ToolCandidate, ...] = ()
    #: The intent this selection was made for — carried so a caller can tell a
    #: stale selection from a fresh one without keeping its own bookkeeping.
    intent: IntentName | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    #: What the capability registry says about the chosen tool's capability, and
    #: whether that makes the choice degradable rather than wrong: a tool this
    #: machine cannot run yet is still the right choice to REPORT, and the caller
    #: needs to know before it asks.
    availability: str = ""
    availability_reason: str = ""
    degraded: bool = False

    @property
    def dispatchable(self) -> bool:
        """True when a tool would actually run for this step."""
        return bool(self.tool)

    def to_dict(self) -> dict[str, Any]:
        """Safe operational metadata only — the choice, never the reasoning."""
        return {
            "tool": self.tool,
            "capability": self.capability,
            "executor": self.executor,
            "source": self.source.value if self.source is not None else "",
            "confidence": round(self.confidence, 3),
            "requires_confirmation": self.requires_confirmation,
            "missing_entities": list(self.missing_entities),
            "reason": self.reason.value,
            "dispatchable": self.dispatchable,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
            "degraded": self.degraded,
            "intent": self.intent.value if self.intent is not None else "",
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "evidence": dict(self.evidence),
        }


class ToolSelector:
    """Ranks the tools an intent reaches and reports the choice with evidence.

    Every dependency is optional, because the selector is asked from tests, from
    the decision layer and from a running application whose registries differ:

        catalog       the intent's declared tools, in preference order
        capabilities  the declared executor and risk for the intent
        registered    the names the runtime registry actually holds

    Availability is reported, never invented: with no registry supplied every
    candidate records ``registered=None`` (unknown) and takes no penalty from
    it, so a selector used in a test does not silently claim the tool is
    missing.
    """

    def __init__(
        self,
        *,
        catalog: IntentCatalog | None = None,
        capabilities: CapabilityRegistry | None = None,
        registered: Sequence[str] | Callable[[], Sequence[str]] | None = None,
        observer: SelectionObserver | None = None,
    ) -> None:
        self._catalog = catalog
        self._capabilities = capabilities
        #: A callable is accepted because a live registry's contents change
        #: between requests, and a snapshot taken at construction would report
        #: yesterday's tools.
        self._registered = registered
        #: Told about every selection this selector makes (Phase 9.1's
        #: "tool.selected"). ONE seam rather than one per call site: the decision
        #: layer and the planning path both go through ``select``, so both are
        #: announced without either of them knowing this exists.
        self._observer = observer

    # -- the one public question -------------------------------------------------

    def select(
        self,
        intent: StructuredIntent,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> ToolSelection:
        """Choose the tool for one understood intent. Deterministic, offline.

        ``context`` is accepted and recorded as evidence rather than read: what
        a caller knows about the surrounding conversation is useful to explain a
        selection and must not silently change which tool runs.
        """
        candidates = self._candidates(intent)
        if not candidates:
            return self._announce(
                ToolSelection(
                    intent=intent.intent,
                    reason=SelectionReason.NO_TOOL,
                    evidence={
                        "declared": 0,
                        "registered": 0,
                        "context_available": bool(context),
                    },
                )
            )
        # Highest score wins; a tie goes to the catalog's own preference order,
        # then to the name, so the answer never depends on dict ordering.
        chosen = min(
            candidates,
            key=lambda item: (-item.score, item.order, item.name),
        )
        return self._announce(
            ToolSelection(
                tool=chosen.name,
                capability=chosen.capability,
                executor=chosen.executor,
                source=chosen.source,
                confidence=chosen.score,
                # Availability travels WITH the choice: the caller that is about
                # to ask for approval is the one that needs to know the
                # capability it is approving cannot run here yet.
                availability=chosen.availability,
                availability_reason=(
                    chosen.availability_reason
                    if chosen.availability not in ("", "available")
                    else ""
                ),
                degraded=chosen.availability not in ("", "available"),
                # Caution is monotone: the intent's own flag and the capability's
                # risk both raise it, and nothing in this module can clear it.
                requires_confirmation=bool(chosen.requires_confirmation),
                missing_entities=chosen.missing_entities,
                reason=self._reason_for(chosen),
                candidates=tuple(candidates),
                intent=intent.intent,
                evidence={
                    "declared": sum(
                        1 for item in candidates if item.source is ToolSource.DECLARED
                    ),
                    "registered": sum(1 for item in candidates if item.registered),
                    "candidate_count": len(candidates),
                    "context_available": bool(context),
                },
            )
        )

    def _announce(self, selection: ToolSelection) -> ToolSelection:
        """Show the selection to the observer, then hand it straight back.

        Returned rather than rebuilt, so what a watcher hears about IS the choice
        the caller acts on. An observer that raises cannot take a selection with
        it — the answer was already made — but the failure is LOGGED rather than
        swallowed: a publisher that names a field the bus reserves, or forgets
        one it promises, must be findable instead of mysteriously silent.
        """
        if self._observer is None:
            return selection
        try:
            self._observer(selection)
        except Exception as exc:  # noqa: BLE001 - announcing is not deciding
            _logger.warning(
                "Tool-selection observer failed for %r: %s: %s",
                selection.tool or "(no tool)",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
        return selection

    # -- assembling the candidates ----------------------------------------------

    def _candidates(self, intent: StructuredIntent) -> list[ToolCandidate]:
        definition = self._catalog.get(intent.intent) if self._catalog is not None else None
        capability = self._capability(intent.intent)
        availability, availability_reason = self._availability(capability)
        missing = self._missing_entities(intent, definition)
        risk = capability.risk if capability is not None else RiskLevel.LOW
        confirmation = bool(
            intent.requires_confirmation
            or (definition is not None and definition.requires_confirmation)
            or risk in _CONFIRMING_RISK
        )
        names = self._registry_names()
        candidates: list[ToolCandidate] = []
        seen: set[str] = set()

        declared = definition.tools if definition is not None else ()
        for order, name in enumerate(declared):
            token = str(name).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            registered = None if names is None else token in names
            score = 1.0 - (order * _ORDER_PENALTY)
            reasons: list[str] = [f"declared by {intent.intent.value} at position {order}"]
            if registered:
                score += _REGISTERED_BONUS
                reasons.append("registered at runtime")
            elif registered is False:
                reasons.append("declared but not registered at runtime")
            score, entity_reason = self._apply_entities(score, missing)
            if entity_reason:
                reasons.append(entity_reason)
            if availability_reason:
                reasons.append(availability_reason)
            candidates.append(
                ToolCandidate(
                    name=token,
                    source=ToolSource.DECLARED,
                    score=_clamp(score),
                    order=order,
                    registered=registered,
                    executor=capability.executor if capability is not None else "",
                    capability=capability.capability if capability is not None else "",
                    risk=risk,
                    requires_confirmation=confirmation,
                    missing_entities=missing,
                    reasons=tuple(reasons),
                    availability=availability,
                    availability_reason=availability_reason,
                )
            )

        # A tool registered for THIS intent is evidence the runtime agrees with
        # the catalog: named by its own schema rather than by name coincidence,
        # so it is offered alongside the declared tools instead of replacing
        # them. Only registered tools whose name equals the intent, or one the
        # catalog already names, qualify — guessing from a tool's name is how a
        # selector starts running the wrong thing.
        for name in sorted(names or ()):
            token = str(name).strip()
            if not token or token in seen:
                continue
            if not self._serves(name, intent.intent, declared):
                continue
            seen.add(token)
            score, entity_reason = self._apply_entities(1.0 + _REGISTERED_BONUS / 2, missing)
            reasons = [f"registered at runtime for {intent.intent.value}"]
            if entity_reason:
                reasons.append(entity_reason)
            if availability_reason:
                reasons.append(availability_reason)
            candidates.append(
                ToolCandidate(
                    name=token,
                    source=ToolSource.REGISTERED,
                    score=_clamp(score),
                    order=len(declared),
                    registered=True,
                    executor=capability.executor if capability is not None else "",
                    capability=capability.capability if capability is not None else "",
                    risk=risk,
                    requires_confirmation=confirmation,
                    missing_entities=missing,
                    reasons=tuple(reasons),
                    availability=availability,
                    availability_reason=availability_reason,
                )
            )

        # The capability's executor is the weakest candidate on purpose: it is
        # the subsystem, not the tool, so it is offered only when the catalog
        # named nothing — an executor name is a real answer to "what runs this"
        # but a coarser one than the tool the intent declared.
        executor = capability.executor if capability is not None else ""
        if executor and not candidates and executor not in seen:
            seen.add(executor)
            registered = None if names is None else executor in names
            score, entity_reason = self._apply_entities(0.75, missing)
            reasons = ["the capability's declared executor; the catalog names no tool"]
            if entity_reason:
                reasons.append(entity_reason)
            if availability_reason:
                reasons.append(availability_reason)
            candidates.append(
                ToolCandidate(
                    name=executor,
                    source=ToolSource.EXECUTOR,
                    score=_clamp(score),
                    order=0,
                    registered=registered,
                    executor=executor,
                    capability=capability.capability if capability is not None else "",
                    risk=risk,
                    requires_confirmation=confirmation,
                    missing_entities=missing,
                    reasons=tuple(reasons),
                    availability=availability,
                    availability_reason=availability_reason,
                )
            )
        return candidates

    # -- pieces -----------------------------------------------------------------

    def _capability(self, intent: IntentName) -> Capability | None:
        if self._capabilities is None:
            return None
        return self._capabilities.best(intent)

    def _availability(self, capability: Capability | None) -> tuple[str, str]:
        """What the registry says about the capability behind the candidates.

        Absent evidence is reported as absent (""), never as availability: with
        no registry wired there is nothing to say, and "unknown" dressed up as
        "available" is exactly the rounding-up this phase exists to prevent.
        """
        if capability is None or self._capabilities is None:
            return "", ""
        try:
            availability, reason = self._capabilities.availability_of(capability)
        except Exception:  # pragma: no cover - a selector must never throw here
            return "", ""
        state = getattr(availability, "value", str(availability))
        if state == "available":
            return state, ""
        return state, f"capability {capability.id} is {state}" + (f": {reason}" if reason else "")

    @staticmethod
    def _missing_entities(
        intent: StructuredIntent, definition: Any
    ) -> tuple[str, ...]:
        """Required entity kinds the reading did not fill.

        Read from the catalog definition when there is one, and from the
        intent's own declared requirements otherwise — an intent built by hand
        still knows what it needs.
        """
        required: tuple[str, ...] = ()
        if definition is not None:
            required = tuple(getattr(definition, "required_entities", ()) or ())
        entities = intent.entities if isinstance(intent.entities, Mapping) else {}
        missing = tuple(name for name in required if not _filled(entities.get(name)))
        if missing or not intent.unresolved_steps:
            return missing
        # Part of the request was never read at all. That is an incomplete step
        # even when every declared entity happens to be present, and it is
        # reported the same way so one prompt covers both cases.
        return missing

    @staticmethod
    def _apply_entities(score: float, missing: tuple[str, ...]) -> tuple[float, str]:
        if not missing:
            return score, ""
        return score * _MISSING_ENTITY_FACTOR, f"missing required entity: {', '.join(missing)}"

    def _registry_names(self) -> tuple[str, ...] | None:
        """The registered tool names, live when the caller supplied a callable."""
        source = self._registered
        if source is None:
            return None
        try:
            names = source() if callable(source) else source
        except Exception:
            # A registry that cannot be read is treated as unknown availability,
            # never as "nothing is registered": the difference matters, because
            # one loses confidence and the other looks like a missing tool.
            return None
        return tuple(str(name) for name in names)

    @staticmethod
    def _serves(name: str, intent: IntentName, declared: tuple[str, ...]) -> bool:
        """True when a registered tool says it carries this intent out."""
        token = str(name).strip().lower()
        if not token:
            return False
        if token in {str(item).strip().lower() for item in declared}:
            return False  # already offered as a declared candidate
        return token == intent.value or token == intent.value.replace("_", "")

    @staticmethod
    def _reason_for(candidate: ToolCandidate) -> SelectionReason:
        if candidate.source is ToolSource.REGISTERED:
            return SelectionReason.REGISTERED_TOOL
        if candidate.source is ToolSource.EXECUTOR:
            return SelectionReason.CAPABILITY_EXECUTOR
        return SelectionReason.DECLARED_TOOL


def _filled(value: Any) -> bool:
    """True when an entity is actually present (not empty, not a blank string)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return bool(value)
    if isinstance(value, Mapping):
        return bool(value)
    return True


def _clamp(score: float) -> float:
    return max(0.0, min(1.0, float(score)))
