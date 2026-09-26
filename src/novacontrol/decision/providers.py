"""Decision providers: the local router, and the optional external one.

``LocalDecisionProvider`` is the default and the floor. It is a pure function of
its inputs — a structured intent, the complexity assessment the NLU already
made, and an environment describing which providers exist on this machine — so
it needs no model, no network and no running application, and it never fails.

``JevDecisionProvider`` is the optional one. It exists behind the same
interface, sends nothing but a redacted routing summary, validates every field
it receives, and returns ``None`` on any doubt — so an unreachable, slow or
malformed external service cannot change what NovaControl does beyond failing
back to the local decision, which is what it would have made anyway.

The routing hierarchy, cheapest first:

    1. deterministic / direct tool     a capability with a known executor
    2. lightweight local capability    a subsystem that does its own work
    3. planner / agent                 several steps, or a loop
    4. vision pipeline                 the request needs an image LOOKED at
    5. local language model            understanding or reasoning needs one
    6. cloud                           when no local model can carry it

Two rules are load-bearing. **A model is never consulted when determinism is
enough**: "what is my RAM usage?" and "open chrome" are answered from telemetry
and the desktop controller, and neither decision mentions a model. And **the
handler is always resolved locally**, never taken from an external provider —
remote advice can suggest a route, but only the local table can name an
executor, so an external service cannot direct NovaControl at an arbitrary
subsystem.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from novacontrol.decision.models import (
    Decision,
    DecisionEnvironment,
    DecisionRequest,
    DecisionRoute,
    DecisionType,
    ReasonCode,
    redacted_intent_payload,
)
from novacontrol.decision.routing import (
    AGENTIC_INTENTS,
    CONVERSATION_INTENTS,
    KNOWLEDGE_INTENTS,
    PLANNING_INTENTS,
    SEQUENCING_HANDLERS,
    SYSTEM_READING_INTENTS,
    VISION_INTENTS,
    handler_for,
)
from novacontrol.intelligence.intent import CapabilityRegistry, IntentName

#: Executors that mean "a subsystem does its own internal work" rather than
#: "one deterministic action runs". Used only to describe a decision, never to
#: authorize one.
_COMPOSITE_HANDLERS = frozenset({"agent", "plan", "explore", "chat", "vision"})

#: Handlers whose work can need sequencing. A single action under one of these
#: is still a direct call; two or more is planning. The set itself lives in
#: ``routing.py`` beside the table that names the handlers, because the
#: application needs the same list to decide whether the executor it was given
#: can honour a sequencing requirement at all.
_PLANNER_HANDLERS = SEQUENCING_HANDLERS

#: The user-facing grouping, which is coarser than the capability registry on
#: purpose: the registry names one capability per intent, while a status
#: surface wants to say "system monitoring" once.
_GROUP_BY_HANDLER: dict[str, str] = {
    "desktop": "application_control",
    "phone": "phone_control",
    "browser": "browser_automation",
    "explore": "research",
    "chat": "conversation",
    "plan": "planning",
    "agent": "agentic",
    "memory": "memory",
    "project": "project",
    "self_improvement": "self_improvement",
    "system": "system_monitoring",
    "vision": "vision",
}

_GROUP_BY_EXECUTOR: dict[str, str] = {
    "system_monitor": "system_monitoring",
    "desktop_controller": "application_control",
    "phone_controller": "phone_control",
    "browser_controller": "browser_automation",
    "file_manager": "file_management",
    "code_agent": "code_assistance",
    "explore_service": "research",
    "chat_brain": "conversation",
    "scratch_brain": "conversation",
    "planning_engine": "planning",
    "agent_coordinator": "agentic",
    "vision_pipeline": "vision",
    "memory_manager": "memory",
    "automation_manager": "planning",
    "scheduler": "planning",
    "project_manager": "project",
    "self_improvement_engine": "self_improvement",
}


@runtime_checkable
class DecisionProvider(Protocol):
    """Anything that can answer "what should NovaControl do with this?".

    Returns a :class:`Decision`, or ``None`` to decline. Declining is a normal
    answer: the engine falls back to the local provider, so a provider that is
    unavailable, unsure or malfunctioning degrades rather than breaks.
    """

    name: str

    def decide(self, request: DecisionRequest) -> Decision | None:  # pragma: no cover - protocol
        ...


class LocalDecisionProvider:
    """The default provider: deterministic, offline, and never wrong by accident.

    Every branch is a rule over evidence the NLU already produced — the intent,
    its requirement flags, the actions it decomposed into, the complexity
    assessment and what the machine has available. Nothing here is learned,
    sampled or guessed, which is what makes the fast path reproducible run to
    run.
    """

    name = "local"

    def __init__(self, *, capabilities: CapabilityRegistry | None = None) -> None:
        self._capabilities = capabilities

    # -- the hierarchy ---------------------------------------------------------

    def decide(self, request: DecisionRequest) -> Decision:
        intent = request.intent
        actions = self._actions(request)
        handler = handler_for(intent.intent)
        capability = self._capability(intent.intent)
        capability_name = capability.capability if capability is not None else ""
        executor = capability.executor if capability is not None else ""
        group = self._group(handler, executor)
        metadata: dict[str, Any] = {
            "strategy": request.strategy,
            "complexity": request.complexity,
            "needs_model": request.needs_model,
            "needs_planner": request.needs_planner,
            "unresolved_clauses": request.unresolved,
            "actions": list(actions),
            "capability_group": group,
            "capability_registered": capability is not None,
            "dispatchable": bool(handler),
            # Context as an INPUT to the decision rather than decoration beside
            # it: whether a remembered snapshot was available, and whether this
            # reading's target actually came from it. The names inside the
            # snapshot are deliberately not copied here — a decision says that it
            # was made while a project was active, not which files are in it.
            "context_available": bool(request.context),
            "context_resolved": bool(intent.references),
            "context_references": list(intent.references),
        }

        # 1. Vision wins outright: an image must be SEEN, not described by text.
        #    It outranks clarification deliberately — a request about a picture
        #    cannot be answered by asking a better question, and the pipeline
        #    reports what it could actually determine when no VLM is wired.
        if (
            intent.requires_vision
            or intent.intent in VISION_INTENTS
            or request.route_hint == DecisionRoute.VISION.value
        ):
            return self._decision(
                request,
                decision_type=DecisionType.VISION,
                route=DecisionRoute.VISION,
                handler=handler or "vision",
                selected_capability=capability_name,
                selected_model=request.environment.vision_model,
                requires_vision=True,
                requires_planning=self._needs_sequencing(request, actions),
                metadata={**metadata, "vision_available": request.environment.vision_available},
                reason_code=ReasonCode.VISION_REQUIRED,
                reason=(
                    "The request needs an image understood, so it goes to the vision "
                    "pipeline — never to a text-only model."
                ),
            )

        # 2. Nothing was understood well enough to act on: one question —
        #    unless the request is WORK, in which case a question about the part
        #    this layer happened to read answers none of it.
        if (
            intent.intent is IntentName.CLARIFY
            or intent.needs_clarification
            or request.route_hint == DecisionRoute.CLARIFY.value
        ) and not self._shape_demands_more(request, actions):
            return self._decision(
                request,
                decision_type=DecisionType.CLARIFICATION,
                route=DecisionRoute.CLARIFY,
                reason_code=ReasonCode.CLARIFICATION_NEEDED,
                metadata=metadata,
                reason=(
                    "The request was not understood confidently enough to act on, so "
                    "NovaControl asks one precise question instead of guessing."
                ),
            )

        # 3. Reasoning: the lightweight layers, including every embedding, already
        #    declined, or the request is shaped like judgement rather than action,
        #    or part of it was never read at all (unresolved clause material).
        if (
            intent.requires_llm
            or request.route_hint == DecisionRoute.LOCAL_LLM.value
            or request.needs_model
            or (request.unresolved and not handler)
        ):
            return self._reasoning_decision(request, metadata, actions, handler, capability_name)

        # 4. The agentic loop, when the work is "keep going until it works".
        if intent.intent in AGENTIC_INTENTS:
            return self._decision(
                request,
                decision_type=DecisionType.PLANNING,
                route=DecisionRoute.AGENT,
                handler=handler or "agent",
                selected_capability=capability_name,
                selected_model=self._planner_model(request.environment),
                requires_planning=True,
                requires_confirmation=bool(intent.requires_confirmation),
                metadata=metadata,
                reason_code=ReasonCode.AGENTIC_TASK,
                reason=(
                    "This needs the agentic loop: act, observe the result, verify it "
                    "and recover if it failed."
                ),
            )

        # 5. The machine's own readings — measured, never modelled.
        if intent.intent in SYSTEM_READING_INTENTS:
            return self._decision(
                request,
                decision_type=DecisionType.DETERMINISTIC,
                route=DecisionRoute.SYSTEM_TOOLS,
                handler=handler or "system",
                selected_capability=capability_name,
                requires_planning=False,
                requires_confirmation=False,
                metadata=metadata,
                reason_code=ReasonCode.SYSTEM_READING,
                reason=(
                    "This is measured on this machine from its own telemetry, so no "
                    "model and no planner are involved."
                ),
            )

        # 6. Several steps, or a shape that needs sequencing.
        #
        #    The first gate is "this intent's own executor can plan". It is
        #    widened by ``multi_step`` for the request that decomposed into
        #    several actions and for which the intent table names NO executor at
        #    all — because "no subsystem claims this intent" is not the same
        #    answer as "this request asks for nothing": it says the reader split
        #    the sentence into work and the table has no entry for the first
        #    piece. *"Find my NovaControl project, run the tests and explain why
        #    they fail"* is three actions (``find_file``, ``run_command``,
        #    ``answer_question``), its own assessment says ``needs_planner``, and
        #    the table has no handler for any of the first two — so before this
        #    the request fell through to the unhandled-capability branch, was
        #    classified from its FIRST clause, and the tests and the explanation
        #    it also asked for never happened. Performing one clause of a request
        #    that stated three is not a conservative reading; it is a dropped
        #    request.
        #
        #    Deliberately NOT widened beyond that: a multi-action reading whose
        #    primary intent DOES have an executor keeps the route it has today
        #    (a system reading stays a system reading, a composite browser task
        #    keeps the browser), so this cannot re-route work that already lands
        #    somewhere sensible.
        multi_step = not handler and len(actions) > 1
        if (handler in _PLANNER_HANDLERS or multi_step) and (
            intent.intent in PLANNING_INTENTS
            or self._needs_sequencing(request, actions)
            or handler in _COMPOSITE_HANDLERS
            or request.needs_planner
        ):
            return self._decision(
                request,
                decision_type=DecisionType.PLANNING,
                route=DecisionRoute.PLANNER,
                handler=handler or "plan",
                selected_capability=capability_name,
                selected_model=self._planner_model(request.environment),
                requires_planning=True,
                requires_web=bool(intent.requires_web),
                requires_confirmation=bool(intent.requires_confirmation),
                metadata=metadata,
                reason_code=ReasonCode.PLANNING_REQUIRED,
                reason=(
                    "More than one step must be sequenced, so the planner orders them "
                    "before anything executes."
                ),
            )

        # 7. Knowledge work that consults sources beyond this machine. A question
        #    the assistant can answer from what it already knows is NOT research:
        #    the handler decides, and the web flag is the request's own.
        if handler == "explore" or (intent.intent in KNOWLEDGE_INTENTS and intent.requires_web):
            return self._decision(
                request,
                decision_type=DecisionType.RESEARCH,
                route=DecisionRoute.LOCAL_CAPABILITY,
                handler=handler or "explore",
                selected_capability=capability_name,
                selected_model=self._planner_model(request.environment),
                requires_web=bool(intent.requires_web),
                requires_planning=False,
                metadata=metadata,
                reason_code=ReasonCode.KNOWLEDGE_RESEARCH,
                reason="Knowledge work that must consult sources, run by the research handler.",
            )

        # 8. Language-only answers.
        if intent.intent in CONVERSATION_INTENTS or handler == "chat":
            return self._decision(
                request,
                decision_type=DecisionType.CONVERSATION,
                route=DecisionRoute.CHAT,
                handler=handler or "chat",
                selected_capability=capability_name,
                selected_model=self._planner_model(request.environment),
                requires_planning=False,
                metadata=metadata,
                reason_code=ReasonCode.CONVERSATION,
                reason="A language-only answer, served by the assistant.",
            )

        # 9. A deterministic capability the application can actually dispatch:
        #    the intent table names a subsystem, so one call carries it out.
        if handler:
            return self._decision(
                request,
                decision_type=DecisionType.DETERMINISTIC,
                route=DecisionRoute.DIRECT_TOOL,
                handler=handler,
                selected_capability=capability_name,
                requires_planning=False,
                requires_confirmation=bool(intent.requires_confirmation),
                metadata=metadata,
                reason_code=ReasonCode.DETERMINISTIC_CAPABILITY,
                reason=(
                    "A deterministic capability carries this out — no model and no "
                    "planner are needed."
                ),
            )

        # 10. Nothing in the intent table dispatches this reading: the assistant's
        #    own classifier resolves which local subsystem carries it out. The
        #    route stays local and cheap — but the empty handler is reported
        #    plainly (``dispatchable: false``, ``executor_unknown: true``) rather
        #    than papered over with an executor that does not exist.
        return self._decision(
            request,
            decision_type=DecisionType.CAPABILITY,
            route=DecisionRoute.LOCAL_CAPABILITY,
            handler=handler,
            selected_capability=capability_name,
            requires_planning=False,
            requires_confirmation=bool(intent.requires_confirmation),
            metadata={
                **metadata,
                "executor_unknown": True,
                "capability_without_handler": capability_name,
            },
            reason_code=ReasonCode.LOCAL_CAPABILITY,
            reason=(
                "Handled locally: the assistant's own classifier picks the subsystem, "
                "because no single capability is registered for this reading yet."
            ),
        )

    # -- pieces ---------------------------------------------------------------

    def _reasoning_decision(
        self,
        request: DecisionRequest,
        metadata: dict[str, Any],
        actions: tuple[str, ...],
        handler: str,
        capability_name: str,
    ) -> Decision:
        """Route to a language model — local by preference, cloud when necessary."""
        environment = request.environment
        if environment.local_model:
            route = DecisionRoute.LOCAL_LLM
            model = environment.local_model
            code = ReasonCode.REASONING_REQUIRED
            reason = (
                "The lightweight layers could not resolve this, so the request goes to "
                "the local model to be understood and planned."
            )
        elif environment.cloud_configured:
            route = DecisionRoute.CLOUD
            model = environment.cloud_model
            code = ReasonCode.CLOUD_ESCALATION
            reason = (
                "No local model is configured, so the request goes to the cloud "
                "provider that is."
            )
        else:
            # No model anywhere. Recording the need is the honest answer: the
            # application still answers with whatever it has, but the decision
            # does not pretend the request was resolvable locally.
            route = DecisionRoute.LOCAL_LLM
            model = ""
            code = ReasonCode.REASONING_REQUIRED
            reason = (
                "This needs language understanding, and no model is configured on this "
                "machine yet."
            )
        # What the model is needed FOR decides which handler carries it out. A
        # request the reader split into SEVERAL actions is asking for those
        # actions, not for prose about them: naming ``chat`` there would have the
        # assistant answer about work it never did. A single-action reading keeps
        # the language handler, which is what the fallback path has always meant.
        multi_step = len(actions) > 1
        return self._decision(
            request,
            decision_type=DecisionType.REASONING,
            route=route,
            handler=handler or ("plan" if multi_step else "chat"),
            selected_capability=capability_name,
            selected_model=model,
            requires_planning=self._needs_sequencing(request, actions) or request.needs_planner,
            requires_web=bool(request.intent.requires_web),
            requires_confirmation=bool(request.intent.requires_confirmation),
            metadata={
                **metadata,
                "model_available": bool(environment.local_model or environment.cloud_configured),
                "local_model_loaded": environment.local_model_loaded,
            },
            reason_code=code,
            reason=reason,
        )

    def _decision(self, request: DecisionRequest, **kwargs: Any) -> Decision:
        """Build a decision, carrying the evidence it was made from.

        ``actions`` and ``confidence`` are always taken from the request rather
        than passed in: a provider must not be able to widen the action list or
        raise the certainty of the reading it was asked about.
        """
        intent = request.intent
        return Decision(
            actions=self._actions(request),
            confidence=intent.confidence,
            requires_vision=bool(kwargs.pop("requires_vision", intent.requires_vision)),
            requires_web=bool(kwargs.pop("requires_web", intent.requires_web)),
            provider=self.name,
            **kwargs,
        )

    @staticmethod
    def _actions(request: DecisionRequest) -> tuple[str, ...]:
        """The action list the NLU produced, with a single-action fallback."""
        intent = request.intent
        if intent.actions:
            return tuple(intent.actions)
        return (intent.action,) if intent.action else ()

    @staticmethod
    def _shape_demands_more(request: DecisionRequest, actions: tuple[str, ...]) -> bool:
        """True when the request is work, so a question would be an evasion.

        Clarification asks for one missing detail; it cannot sequence a task or
        read a request written in three clauses. The NLU asks its own question
        when it cannot read something at all — and when it has a model it has
        already tried it — so this only overrides the question in the case where
        the request's SHAPE is the reason it is hard: the assessment says it
        needs a model or a planner, or clause material was never read.

        Measured: *"find my NovaControl project, inspect the latest changes and
        fix the failing tests"* is COMPLEX, and asking "which file?" answers
        nothing. With no model configured the route then says so plainly
        (``local_llm`` with no model) instead of pretending a question covers
        it. A reading with **no actions at all** is left alone: nothing was
        understood, so a question really is the whole answer.
        """
        if not actions:
            return False
        return bool(request.needs_model or request.needs_planner or request.unresolved)

    @staticmethod
    def _needs_sequencing(request: DecisionRequest, actions: tuple[str, ...]) -> bool:
        """True when more than one step must be ordered (or the assessment says so)."""
        if len(actions) > 1:
            return True
        return str(request.complexity).lower() in {"moderate", "complex"}

    @staticmethod
    def _planner_model(environment: DecisionEnvironment) -> str:
        """The model planning would use, if any — reported, not required."""
        return environment.local_model or environment.cloud_model

    def _capability(self, intent: IntentName) -> Any:
        if self._capabilities is None:
            return None
        return self._capabilities.best(intent)

    @staticmethod
    def _group(handler: str, executor: str) -> str:
        if handler in _GROUP_BY_HANDLER:
            return _GROUP_BY_HANDLER[handler]
        return _GROUP_BY_EXECUTOR.get(executor, "")


# -- optional external provider ------------------------------------------------

#: A provider is an advisor, not a channel: bound what it can send back.
_MAX_ACTIONS = 12
_MAX_FIELD = 120


class JevDecisionProvider:
    """Optional external decision provider, isolated behind the interface.

    NovaControl does not depend on Jev — or on anything else — for this. The
    provider is constructed only when configuration names it, sends a redacted
    routing summary (intent, flags, complexity, environment: never the user's
    words or entities), and every field of the reply is validated against the
    local vocabularies. If the service is slow, absent or wrong, ``decide``
    returns ``None`` and the local provider answers instead.
    """

    name = "jev"

    def __init__(
        self,
        endpoint: str = "",
        *,
        timeout_s: float = 2.0,
        allow_remote: bool = False,
    ) -> None:
        self.endpoint = endpoint.strip()
        self.timeout_s = max(0.1, float(timeout_s))
        #: Two gates rather than one: an endpoint must be configured AND remote
        #: decisions must be opted into, so no default install ever sends the
        #: shape of someone's requests to a service they did not ask for.
        self.allow_remote = bool(allow_remote)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint) and self.allow_remote

    def decide(self, request: DecisionRequest) -> Decision | None:
        if not self.enabled:
            return None
        reply = self._ask(request)
        if reply is None:
            return None
        return self._validate(reply, request)

    # -- transport ------------------------------------------------------------

    def _ask(self, request: DecisionRequest) -> Mapping[str, Any] | None:
        payload = {"schema": "novacontrol.decision.v1", **redacted_intent_payload(request)}
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - payload is plain data
            return None
        http_request = urllib.request.Request(  # noqa: S310 - endpoint is operator-configured
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:  # noqa: S310
                raw = response.read(64 * 1024)
        except (urllib.error.URLError, OSError, ValueError):
            # Unreachable, refused, timed out or garbage: all the same answer.
            return None
        try:
            decoded = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, Mapping):
            return None
        decision = decoded.get("decision")
        if isinstance(decision, Mapping):
            return decision
        return decoded

    # -- validation -----------------------------------------------------------

    def _validate(self, reply: Mapping[str, Any], request: DecisionRequest) -> Decision | None:
        """Accept only what the local vocabularies can express.

        Unknown routes are refused rather than mapped to something convenient:
        an external service that answers with a vocabulary NovaControl does not
        have is not answering the question it was asked.
        """
        try:
            route = DecisionRoute(str(reply.get("route", "")))
            decision_type = DecisionType(str(reply.get("decision_type", self._type_for(route))))
        except ValueError:
            return None
        try:
            reason_code = ReasonCode(str(reply.get("reason_code", "")))
        except ValueError:
            reason_code = self._code_for(route)

        intent = request.intent
        claimed = reply.get("actions")
        actions = self._matching_actions(claimed, request)
        provider_confidence = reply.get("confidence")
        return Decision(
            decision_type=decision_type,
            route=route,
            # The capability name is advisory text; the handler is NOT taken from
            # the provider — only the local table may name an executor.
            selected_capability=_short(reply.get("selected_capability", "")),
            selected_model=(
                _short(reply.get("selected_model", ""))
                or self._fallback_model(route, request)
            ),
            actions=actions or self._actions(request),
            confidence=intent.confidence,
            requires_planning=bool(reply.get("requires_planning", False)),
            # Caution is monotone: an advisor may ADD a requirement, never remove
            # one. A provider that could clear `requires_confirmation` would be
            # able to turn an approved-every-time action into a silent one, which
            # is authorization — and authorization is not the provider's to give.
            requires_vision=bool(intent.requires_vision or route is DecisionRoute.VISION),
            requires_web=bool(intent.requires_web or reply.get("requires_web", False)),
            requires_confirmation=bool(
                intent.requires_confirmation or reply.get("requires_confirmation", False)
            ),
            reason_code=reason_code,
            handler=handler_for(intent.intent),
            provider=self.name,
            # Deliberately the LOCAL sentence for the route: remote free-form
            # prose is neither displayed nor logged.
            reason=_REASON_BY_ROUTE.get(route, ""),
            metadata={
                "provider_confidence": (
                    round(float(provider_confidence), 3)
                    if isinstance(provider_confidence, int | float)
                    else None
                ),
                "provider_advisory": True,
                "actions_claimed": (
                    len(claimed)
                    if isinstance(claimed, Sequence) and not isinstance(claimed, str)
                    else 0
                ),
            },
        )

    @staticmethod
    def _matching_actions(claimed: object, request: DecisionRequest) -> tuple[str, ...]:
        """Keep only claimed actions that the local reading also produced."""
        if not isinstance(claimed, Sequence) or isinstance(claimed, str):
            return ()
        known = set(LocalDecisionProvider._actions(request))
        picked: list[str] = []
        for item in claimed[:_MAX_ACTIONS]:
            name = str(item).strip()
            if name and name in known and name not in picked:
                picked.append(name)
        return tuple(picked)

    @staticmethod
    def _fallback_model(route: DecisionRoute, request: DecisionRequest) -> str:
        if route is DecisionRoute.CLOUD:
            return request.environment.cloud_model
        if route is DecisionRoute.VISION:
            return request.environment.vision_model
        if route in (DecisionRoute.LOCAL_LLM, DecisionRoute.PLANNER, DecisionRoute.AGENT):
            return LocalDecisionProvider._planner_model(request.environment)
        return ""

    @staticmethod
    def _type_for(route: DecisionRoute) -> str:
        return {
            DecisionRoute.DIRECT_TOOL: DecisionType.DETERMINISTIC.value,
            DecisionRoute.SYSTEM_TOOLS: DecisionType.DETERMINISTIC.value,
            DecisionRoute.LOCAL_CAPABILITY: DecisionType.CAPABILITY.value,
            DecisionRoute.PLANNER: DecisionType.PLANNING.value,
            DecisionRoute.AGENT: DecisionType.PLANNING.value,
            DecisionRoute.LOCAL_LLM: DecisionType.REASONING.value,
            DecisionRoute.CLOUD: DecisionType.REASONING.value,
            DecisionRoute.VISION: DecisionType.VISION.value,
            DecisionRoute.CHAT: DecisionType.CONVERSATION.value,
            DecisionRoute.CLARIFY: DecisionType.CLARIFICATION.value,
        }[route]

    @staticmethod
    def _code_for(route: DecisionRoute) -> ReasonCode:
        return {
            DecisionRoute.DIRECT_TOOL: ReasonCode.DETERMINISTIC_CAPABILITY,
            DecisionRoute.SYSTEM_TOOLS: ReasonCode.SYSTEM_READING,
            DecisionRoute.LOCAL_CAPABILITY: ReasonCode.LOCAL_CAPABILITY,
            DecisionRoute.PLANNER: ReasonCode.PLANNING_REQUIRED,
            DecisionRoute.AGENT: ReasonCode.AGENTIC_TASK,
            DecisionRoute.LOCAL_LLM: ReasonCode.REASONING_REQUIRED,
            DecisionRoute.CLOUD: ReasonCode.CLOUD_ESCALATION,
            DecisionRoute.VISION: ReasonCode.VISION_REQUIRED,
            DecisionRoute.CHAT: ReasonCode.CONVERSATION,
            DecisionRoute.CLARIFY: ReasonCode.CLARIFICATION_NEEDED,
        }[route]

    @staticmethod
    def _actions(request: DecisionRequest) -> tuple[str, ...]:
        return LocalDecisionProvider._actions(request)


#: The local, templated sentence for each route — the only "reason" text an
#: external provider's decision is ever shown with.
_REASON_BY_ROUTE: dict[DecisionRoute, str] = {
    DecisionRoute.DIRECT_TOOL: (
        "A deterministic capability carries this out — no model and no planner are needed."
    ),
    DecisionRoute.SYSTEM_TOOLS: (
        "This is measured on this machine from its own telemetry, so no model and no "
        "planner are involved."
    ),
    DecisionRoute.LOCAL_CAPABILITY: "A local capability handles this without planning or a model.",
    DecisionRoute.PLANNER: (
        "More than one step must be sequenced, so the planner orders them before "
        "anything executes."
    ),
    DecisionRoute.AGENT: (
        "This needs the agentic loop: act, observe the result, verify it and recover "
        "if it failed."
    ),
    DecisionRoute.LOCAL_LLM: (
        "The lightweight layers could not resolve this, so the request goes to the local "
        "model to be understood and planned."
    ),
    DecisionRoute.CLOUD: (
        "No local model is configured, so the request goes to the cloud provider that is."
    ),
    DecisionRoute.VISION: (
        "The request needs an image understood, so it goes to the vision pipeline — never "
        "to a text-only model."
    ),
    DecisionRoute.CHAT: "A language-only answer, served by the assistant.",
    DecisionRoute.CLARIFY: (
        "The request was not understood confidently enough to act on, so NovaControl asks "
        "one precise question instead of guessing."
    ),
}


def _short(value: object, *, limit: int = _MAX_FIELD) -> str:
    """Trim provider-supplied text so it can never become a payload of its own."""
    text = str(value or "").strip()
    return text[:limit]


def build_decision_provider(
    name: str = "local",
    *,
    endpoint: str = "",
    timeout_s: float = 2.0,
    allow_remote: bool = False,
) -> DecisionProvider | None:
    """Map configuration onto a provider, or ``None`` for the local one.

    ``None`` is a real answer, not a failure: it means "the local provider IS
    the provider", which is every default install. An unknown name also maps to
    ``None`` — an unconfigured name must not break boot — but the engine reports
    what was REQUESTED next to what is ACTIVE, so a typo is visible in the
    status surface instead of silently changing how requests are decided.
    """
    requested = name.strip().lower()
    if requested in ("", "local"):
        return None
    if requested == "jev":
        return JevDecisionProvider(endpoint, timeout_s=timeout_s, allow_remote=allow_remote)
    return None
