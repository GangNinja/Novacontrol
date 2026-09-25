"""The decision engine: one place that answers "what should we do with this?".

It is a thin, deterministic shell around a provider. The shell's whole job is
to assemble the evidence ONCE (so every provider — local or external — reasons
over exactly the same request), to consult the configured provider, and to fall
back to the local one when the configured provider declines, is unreachable or
is switched off. A fallback is never silent: the decision records which
provider was preferred and why the local one answered instead.

The engine holds no conversation state and executes nothing. Context enters as
a snapshot of what the context/memory layer already knows, which keeps a
decision explainable after the fact — the same intent plus the same environment
produces the same decision, and that is what makes the fast path testable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from novacontrol.decision.models import (
    Decision,
    DecisionEnvironment,
    DecisionRequest,
)
from novacontrol.decision.providers import DecisionProvider, LocalDecisionProvider
from novacontrol.intelligence.intent import CapabilityRegistry, IntentName, StructuredIntent
from novacontrol.tools.selection import ToolSelection, ToolSelector


class DecisionEngine:
    """Turns an understood request into a structured decision about it."""

    def __init__(
        self,
        *,
        capabilities: CapabilityRegistry | None = None,
        provider: DecisionProvider | None = None,
        local: LocalDecisionProvider | None = None,
        requested_provider: str = "",
        tool_lookup: Callable[[IntentName], str] | None = None,
        selector: ToolSelector | None = None,
    ) -> None:
        #: Phase 5: "which tool would carry this intent out?" resolved with
        #: evidence (declared order, entity completeness, registration, risk)
        #: rather than a single lookup — and never by a provider, so an external
        #: advisor can suggest a route but not name what executes (see ``decide``).
        self._selector = selector
        #: The narrower lookup, kept for callers that only need a name. Used
        #: when no selector was supplied, so nothing that built an engine before
        #: the selection layer existed changes behaviour.
        self._tool_lookup = tool_lookup
        self.local = local or LocalDecisionProvider(capabilities=capabilities)
        #: The configured provider. ``None`` means the local one IS the answer,
        #: which is the default install: nothing external, nothing optional.
        self._primary = provider
        #: What configuration ASKED for, which can differ from what is active
        #: (an unknown name, or an endpoint that was never set). Reported so a
        #: configuration mistake is visible rather than silent.
        self._requested = requested_provider.strip().lower()

    # -- identity --------------------------------------------------------------

    @property
    def provider(self) -> DecisionProvider:
        """Who will be asked first."""
        return self._primary if self._primary is not None else self.local

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def remote(self) -> bool:
        """True when a non-local provider is configured (not necessarily usable)."""
        return self._primary is not None and self._primary is not self.local

    def status(self) -> dict[str, Any]:
        """Safe metadata for a status surface: who decides, and is it usable."""
        primary = self.provider
        enabled = bool(getattr(primary, "enabled", True))
        return {
            "provider": primary.name,
            "requested": self._requested or self.local.name,
            "local_provider": self.local.name,
            "remote": self.remote,
            "enabled": enabled,
            "fallback": self.local.name,
        }

    # -- deciding --------------------------------------------------------------

    def decide(
        self,
        intent: StructuredIntent,
        *,
        context: object | None = None,
        environment: DecisionEnvironment | None = None,
        strategy: str = "",
    ) -> Decision:
        """Decide what to do with one understood request.

        Deterministic and offline in the default configuration. A configured
        provider is consulted first and may decline; whichever way that goes,
        the caller always receives a decision — never ``None``, never an
        exception from a provider that is having a bad day.
        """
        request = self._build_request(
            intent, context=context, environment=environment, strategy=strategy
        )
        preferred = self.provider
        if preferred is self.local:
            return self._with_tool(self.local.decide(request), intent)
        decision = self._ask(preferred, request)
        if decision is not None:
            return self._with_tool(decision, intent)
        # The configured provider had nothing usable for us. Answer locally.
        local = self._with_tool(self.local.decide(request), intent)
        if not bool(getattr(preferred, "enabled", True)):
            # Switched off, not failing: the local answer is simply the answer,
            # and reporting a fallback would make a default install look like it
            # was recovering from something on every single request.
            return local
        # Consulted and it declined, timed out or answered nonsense: answer
        # locally AND say so — an invisible fallback is how a broken provider
        # goes unnoticed for a month.
        return local.with_(
            metadata={
                **local.metadata,
                "preferred_provider": preferred.name,
                "provider_fallback": True,
            }
        )

    async def decide_async(
        self,
        intent: StructuredIntent,
        *,
        context: object | None = None,
        environment: DecisionEnvironment | None = None,
        strategy: str = "",
    ) -> Decision:
        """``decide`` for event-loop callers.

        A local decision is a few dictionary lookups and stays on the loop. A
        configured remote provider is a network call, so it runs in a thread:
        the decision layer must never be the reason an async request stalls.
        """
        if not self.remote:
            return self.decide(
                intent, context=context, environment=environment, strategy=strategy
            )
        return await asyncio.to_thread(
            self.decide, intent, context=context, environment=environment, strategy=strategy
        )

    # -- internals -------------------------------------------------------------

    def _with_tool(self, decision: Decision, intent: StructuredIntent) -> Decision:
        """Name the TOOL the decision's intent would reach.

        Resolved here, from the local catalog and registry, and overwritten on
        every decision — including one an external provider produced. Which tool
        runs is execution, and execution is not a provider's to choose: a remote
        answer that named a tool would be choosing what executes on this
        machine.

        Selection can also RAISE the confirmation requirement (a capability that
        calls itself medium-risk is telling the pipeline a human should look at
        the step), and it can never lower it — the same monotone-caution rule the
        external provider is held to.
        """
        selection = self._select(intent)
        if selection is None:
            return decision
        return decision.with_(
            selected_tool=selection.tool,
            selected_capability=decision.selected_capability or selection.capability,
            requires_confirmation=bool(
                decision.requires_confirmation or selection.requires_confirmation
            ),
            metadata={
                **decision.metadata,
                "tool_source": selection.source.value if selection.source is not None else "",
                "tool_confidence": round(selection.confidence, 3),
                "tool_reason": selection.reason.value,
                "tool_dispatchable": selection.dispatchable,
                "tool_missing_entities": list(selection.missing_entities),
                "tool_candidates": [
                    candidate.name for candidate in selection.candidates
                ],
            },
        )

    def _select(self, intent: StructuredIntent) -> ToolSelection | None:
        """Ask the selection layer, or fall back to the narrow lookup.

        Neither path may fail a decision: a registry that is having a bad day
        costs a decision its tool name, not the request its routing.
        """
        if self._selector is not None:
            try:
                return self._selector.select(intent)
            except Exception:
                return None
        if self._tool_lookup is None:
            return None
        try:
            tool = str(self._tool_lookup(intent.intent) or "")
        except Exception:
            return None
        return ToolSelection(tool=tool, confidence=1.0 if tool else 0.0)

    def _build_request(
        self,
        intent: StructuredIntent,
        *,
        context: object | None,
        environment: DecisionEnvironment | None,
        strategy: str,
    ) -> DecisionRequest:
        parameters = intent.parameters if isinstance(intent.parameters, Mapping) else {}
        complexity = parameters.get("complexity")
        level = ""
        needs_model = False
        needs_planner = False
        if isinstance(complexity, Mapping):
            level = str(complexity.get("level", ""))
            # The assessment's own conclusions, not a re-reading of its summary:
            # a COMPLEX request can need a planner without needing a model, and
            # the decision layer must not have to guess which from the level.
            needs_model = bool(complexity.get("needs_model", False))
            needs_planner = bool(complexity.get("needs_planner", False))
        ambiguity = parameters.get("ambiguity", 0.0)
        return DecisionRequest(
            intent=intent,
            environment=environment or DecisionEnvironment(),
            strategy=strategy or intent.source,
            context=self._context_snapshot(context),
            complexity=level,
            needs_model=needs_model,
            needs_planner=needs_planner,
            unresolved=len(intent.unresolved_steps),
            ambiguity=float(ambiguity) if isinstance(ambiguity, int | float) else 0.0,
        )

    @staticmethod
    def _context_snapshot(context: object | None) -> dict[str, Any]:
        """Ask the context layer what it knows, without depending on its type.

        The context/memory layer already exposes a resolved snapshot; asking for
        it keeps a decision explainable ("this was chosen while a project was
        active") without the decision engine reaching into memory itself.
        """
        if context is None:
            return {}
        resolve = getattr(context, "resolve_snapshot", None)
        if not callable(resolve):
            return {}
        try:
            snapshot = resolve()
        except Exception:
            # Same rule as every other optional dependency here: a context layer
            # that is having a bad day costs the decision its context, not the
            # request its route. Reported as no context available rather than as
            # an error, because the decision that follows is still a real one.
            return {}
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
        return {}

    @staticmethod
    def _ask(provider: DecisionProvider, request: DecisionRequest) -> Decision | None:
        """Consult a provider, treating any failure as "declined"."""
        try:
            return provider.decide(request)
        except Exception:
            # A provider is optional machinery. Its failure mode is the local
            # decision, never a broken request — so nothing is re-raised here and
            # the reason is reported through ``provider_fallback``.
            return None
