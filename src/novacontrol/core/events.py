"""Event primitives for NovaControl.

Phase 9.1 added the typed vocabulary the rest of the system publishes its
lifecycle through — :class:`EventType` — and the three things an event bus needs
before a system can be built on it:

  * **typed payloads.** Each lifecycle event declares the fields it must carry,
    and :meth:`EventBus.publish` refuses an event that is missing one, so a
    subscriber never has to guess whether ``event.payload["tool"]`` exists.
  * **one handler cannot take the others down.** Every subscriber of an event
    runs even when an earlier one raised; the failures are logged, collected and
    reported in a :class:`PublishResult`, and the bus only *raises* about them
    when it was asked to (``continue_on_error=False``, the strict mode tests use).
  * **optional history.** A bounded in-memory ring of the most recent events,
    for "what just happened?" without a journal (the durable record is still
    :class:`~novacontrol.core.journal.EventJournal`).

Custom event types continue to work untouched: the vocabulary is a naming and
validation layer over the same bus, not a gate in front of it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isawaitable
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from novacontrol.core.errors import EventDeliveryError, HandlerFailure
from novacontrol.core.journal import EventJournal

#: A handler may be async, or a plain function (an observer that only records).
EventHandler = Callable[["Event"], Awaitable[None] | None]
EventErrorHandler = Callable[[HandlerFailure], Awaitable[None]]

_logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """The lifecycle vocabulary — what happens inside NovaControl, by name.

    These are the events a UI, a journal or a test subscribes to. Names are
    ``<subject>.<what happened>`` and every one of them is published from the
    live path (see ``application.py``), because a vocabulary nobody publishes
    is a documentation exercise rather than an event system.
    """

    # -- understanding a request ----------------------------------------------
    INTENT_DETECTED = "intent.detected"
    CONTEXT_RESOLVED = "context.resolved"
    DECISION_CREATED = "decision.created"
    PLAN_CREATED = "plan.created"
    # -- a task's life ---------------------------------------------------------
    TASK_STARTED = "task.started"
    TASK_PAUSED = "task.paused"
    TASK_RESUMED = "task.resumed"
    TASK_CANCELLED = "task.cancelled"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # -- tools -----------------------------------------------------------------
    TOOL_SELECTED = "tool.selected"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    # -- checking and recovering ----------------------------------------------
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_COMPLETED = "verification.completed"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"
    # -- models and senses -----------------------------------------------------
    MODEL_LOADED = "model.loaded"
    MODEL_UNLOADED = "model.unloaded"
    VISION_STARTED = "vision.started"
    VISION_COMPLETED = "vision.completed"
    # -- plugins (Phase 10) ----------------------------------------------------
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_INITIALIZED = "plugin.initialized"
    PLUGIN_ENABLED = "plugin.enabled"
    PLUGIN_DISABLED = "plugin.disabled"
    PLUGIN_UNLOADED = "plugin.unloaded"
    PLUGIN_FAILED = "plugin.failed"
    # -- local knowledge (Phase 11) -------------------------------------------
    KNOWLEDGE_INDEXED = "knowledge.indexed"
    KNOWLEDGE_RETRIEVED = "knowledge.retrieved"

    # -- real-time perception (Phase 21) --------------------------------------
    # A frame and a scene are ANNOUNCEMENTS, not deliveries: the payloads carry
    # identity, counts and the measured change, never an image, a path or a text
    # block — a notification channel is the wrong place for someone's screen.
    PERCEPTION_STARTED = "perception.started"
    PERCEPTION_COMPLETED = "perception.completed"
    PERCEPTION_FAILED = "perception.failed"
    FRAME_RECEIVED = "perception.frame"
    SCENE_CHANGED = "perception.scene_changed"
    OBJECT_APPEARED = "perception.object_appeared"
    OBJECT_DISAPPEARED = "perception.object_disappeared"
    OBJECT_MOVED = "perception.object_moved"

    # -- world model and state reasoning (Phase 22) ---------------------------
    # A state change is an ANNOUNCEMENT: the payload carries ids, kinds and counts,
    # never an observation body, a frame, a text block or a reasoning trace. The
    # world id is not in the payload either — an event already knows its publisher,
    # and a world is a scope rather than a field.
    WORLD_OBSERVATION_INGESTED = "world.observation_ingested"
    WORLD_STATE_UPDATED = "world.state_updated"
    WORLD_TRANSITION_CREATED = "world.transition_created"
    WORLD_ENTITY_CHANGED = "world.entity_changed"
    WORLD_RELATIONSHIP_CHANGED = "world.relationship_changed"
    WORLD_CONFLICT_DETECTED = "world.conflict_detected"
    WORLD_STATE_RESTORED = "world.state_restored"
    WORLD_PREDICTION_COMPLETED = "world.prediction_completed"
    WORLD_PREDICTION_FAILED = "world.prediction_failed"

    # -- interactive learning (Phase 23) --------------------------------------
    # A learning event is an ANNOUNCEMENT of a lifecycle step: ids, statuses and
    # counts travel, never a hypothesis body, an observation body or a reasoning
    # trace. ``objective_id`` is the STABLE id of the bounded question, so a
    # watcher that wants one objective's whole story keys on it alone.
    LEARNING_OBJECTIVE_CREATED = "learning.objective_created"
    LEARNING_HYPOTHESIS_PROPOSED = "learning.hypothesis_proposed"
    LEARNING_HYPOTHESIS_UPDATED = "learning.hypothesis_updated"
    LEARNING_EXPERIMENT_PROPOSED = "learning.experiment_proposed"
    LEARNING_EXPERIMENT_APPROVED = "learning.experiment_approved"
    LEARNING_EXPERIMENT_REJECTED = "learning.experiment_rejected"
    LEARNING_EXPERIMENT_STARTED = "learning.experiment_started"
    LEARNING_OBSERVATION_COLLECTED = "learning.observation_collected"
    LEARNING_PREDICTION_EVALUATED = "learning.prediction_evaluated"
    LEARNING_KNOWLEDGE_UPDATED = "learning.knowledge_updated"
    LEARNING_EXPERIMENT_COMPLETED = "learning.experiment_completed"
    LEARNING_EXPERIMENT_FAILED = "learning.experiment_failed"
    LEARNING_EXPERIMENT_INTERRUPTED = "learning.experiment_interrupted"
    LEARNING_EXPERIMENT_CANCELLED = "learning.experiment_cancelled"
    # A ceiling reached before an action is an event of its own rather than a
    # failure: nothing went wrong, and the objective is not finished either.
    LEARNING_BUDGET_EXHAUSTED = "learning.budget_exhausted"

    # -- planning a goal (Phase 24) -------------------------------------------
    # The payloads name the goal and, where it applies, the candidate plan or the
    # step the fact is about. ``steps``/``tasks``/``issues`` are COUNTS of what a
    # plan holds and ``status`` is a category (``completed``, ``blocked``,
    # ``denied``, ``needs_observation``, ``failed``, ``partial``) — never a plan
    # body, a step output or an observation. A refusal and a blocked step are
    # events OF THEIR OWN rather than failures with a note, because neither is a
    # failure of the work: nothing ran in both cases.
    GOAL_CREATED = "goal.created"
    GOAL_DECOMPOSED = "goal.decomposed"
    GOAL_PLAN_SELECTED = "goal.plan_selected"
    GOAL_PLAN_REFUSED = "goal.plan_refused"
    GOAL_STEP_BLOCKED = "goal.step_blocked"
    GOAL_EXECUTED = "goal.executed"
    GOAL_REASSESSED = "goal.reassessed"
    GOAL_CANCELLED = "goal.cancelled"

    # -- an embodied session (Phase 25) ---------------------------------------
    # The payloads name the session, the environment and — where it applies —
    # the input or the objective the fact is about. ``actions``/``entities`` are
    # COUNTS, and ``status`` is a category, never an observation body, a frame or
    # a trajectory. A blocked session and a denied input are events OF THEIR OWN
    # rather than failures with a note, because neither is a failure of the work.
    EMBODIED_SESSION_STARTED = "embodied.session_started"
    EMBODIED_SESSION_PAUSED = "embodied.session_paused"
    EMBODIED_SESSION_RESUMED = "embodied.session_resumed"
    EMBODIED_SESSION_CANCELLED = "embodied.session_cancelled"
    EMBODIED_SESSION_ENDED = "embodied.session_ended"
    EMBODIED_OBSERVATION = "embodied.observation"
    EMBODIED_ACTION_SELECTED = "embodied.action_selected"
    EMBODIED_ACTION_EXECUTED = "embodied.action_executed"
    EMBODIED_ACTION_FAILED = "embodied.action_failed"
    EMBODIED_RECOVERY = "embodied.recovery"
    EMBODIED_BLOCKED = "embodied.blocked"
    EMBODIED_OBJECTIVE_VERIFIED = "embodied.objective_verified"
    EMBODIED_OBJECTIVE_FAILED = "embodied.objective_failed"

    # -- a generalization evaluation (Phase 26) -------------------------------
    # ``run_id`` is the STABLE id of one evaluation run, which is also its
    # correlation id: a watcher that wants a run's whole story keys on it alone.
    # Counts are counts, a status is a category and ``reason``/``category`` are
    # human-readable text — never a task's expected output, a predicted answer
    # or a solver's raw content, because an event channel is not a place to
    # carry a benchmark's answers around.
    GENERALIZATION_TASK_REGISTERED = "generalization.task_registered"
    GENERALIZATION_EVALUATION_STARTED = "generalization.evaluation_started"
    GENERALIZATION_EVALUATION_COMPLETED = "generalization.evaluation_completed"
    GENERALIZATION_FAILURE_CLASSIFIED = "generalization.failure_classified"
    GENERALIZATION_LEAKAGE_DETECTED = "generalization.leakage_detected"

    # -- scheduled work (Phase 13) --------------------------------------------
    # ``automation_id`` is the STABLE id of the scheduled request, and ``run_id``
    # names one execution of it: a watcher that wants the schedule keys on the
    # first, one that wants a single run's history keys on the second.
    AUTOMATION_CREATED = "automation.created"
    AUTOMATION_UPDATED = "automation.updated"
    AUTOMATION_CANCELLED = "automation.cancelled"
    AUTOMATION_STARTED = "automation.started"
    AUTOMATION_COMPLETED = "automation.completed"
    AUTOMATION_FAILED = "automation.failed"
    # A refusal and an unmet condition are events OF THEIR OWN rather than
    # failures with a note, because neither is a failure of the work.
    AUTOMATION_DENIED = "automation.denied"
    AUTOMATION_SKIPPED = "automation.skipped"

    # -- the audit trail (Phase 13.3) -----------------------------------------
    AUDIT_RECORDED = "audit.recorded"
    AUDIT_PRUNED = "audit.pruned"

    # -- privacy and execution modes (Phase 14.2/14.3) ------------------------
    # A mode change is a fact about the whole installation; a denial names the
    # ONE outbound action that was refused (and the control or mode that
    # refused it), never a person's data.
    PRIVACY_MODE_CHANGED = "privacy.mode_changed"
    PRIVACY_DENIED = "privacy.denied"


#: The payload fields each lifecycle event MUST carry. Checked when an event of
#: a known type is published, so "the handler will find it" is a guarantee rather
#: than a convention. A custom event type has no schema and is not inspected.
EVENT_PAYLOAD_FIELDS: Mapping[EventType, tuple[str, ...]] = {
    EventType.INTENT_DETECTED: ("intent",),
    EventType.CONTEXT_RESOLVED: ("strategy",),
    EventType.DECISION_CREATED: ("route", "decision_type"),
    EventType.PLAN_CREATED: ("goal", "steps"),
    EventType.TASK_STARTED: ("task_id",),
    EventType.TASK_PAUSED: ("task_id",),
    EventType.TASK_RESUMED: ("task_id",),
    EventType.TASK_CANCELLED: ("task_id",),
    EventType.TASK_COMPLETED: ("task_id",),
    EventType.TASK_FAILED: ("task_id",),
    EventType.TOOL_SELECTED: ("tool",),
    EventType.TOOL_STARTED: ("tool",),
    EventType.TOOL_COMPLETED: ("tool", "status"),
    EventType.TOOL_FAILED: ("tool", "error"),
    EventType.VERIFICATION_STARTED: ("step_id",),
    EventType.VERIFICATION_COMPLETED: ("step_id", "status"),
    EventType.RECOVERY_STARTED: ("step_id",),
    EventType.RECOVERY_COMPLETED: ("step_id", "outcome"),
    EventType.MODEL_LOADED: ("model",),
    EventType.MODEL_UNLOADED: ("model",),
    # The image being read is ``image``, NOT ``source``: every event already
    # carries a ``source`` (the publisher's own name), and a payload field that
    # shadows it could never be set through ``emit``.
    EventType.VISION_STARTED: ("image",),
    EventType.VISION_COMPLETED: ("image", "answered"),
    # ``plugin_id`` is the STABLE identity, not the display name: a plugin's
    # name is a label that may change, and the id is what a watcher keys on.
    EventType.PLUGIN_LOADED: ("plugin_id",),
    EventType.PLUGIN_INITIALIZED: ("plugin_id",),
    EventType.PLUGIN_ENABLED: ("plugin_id",),
    EventType.PLUGIN_DISABLED: ("plugin_id",),
    EventType.PLUGIN_UNLOADED: ("plugin_id",),
    EventType.PLUGIN_FAILED: ("plugin_id", "error"),
    # The perception vocabulary. ``source_kind`` rather than ``source``: every
    # event's envelope already carries a ``source`` (the publisher's name), and a
    # payload field that shadows it could never be set through ``emit``.
    EventType.PERCEPTION_STARTED: ("mode", "source_kind"),
    EventType.PERCEPTION_COMPLETED: ("status", "objects", "escalated"),
    EventType.PERCEPTION_FAILED: ("status", "error"),
    EventType.FRAME_RECEIVED: ("frame_id", "sequence"),
    EventType.SCENE_CHANGED: ("scene_id", "changes"),
    EventType.OBJECT_APPEARED: ("object_id", "label"),
    EventType.OBJECT_DISAPPEARED: ("object_id", "label"),
    EventType.OBJECT_MOVED: ("object_id", "label", "distance"),
    # Phase 23: the payloads name the objective, the hypothesis or the experiment
    # the step was about. An outcome is a category (``confirmed``, ``contradicted``,
    # ``unable_to_evaluate``) and never the observation itself.
    EventType.LEARNING_OBJECTIVE_CREATED: ("objective_id", "environment_id"),
    EventType.LEARNING_HYPOTHESIS_PROPOSED: ("objective_id", "hypothesis_id", "hypotheses"),
    EventType.LEARNING_HYPOTHESIS_UPDATED: ("objective_id", "hypothesis_id", "status"),
    EventType.LEARNING_EXPERIMENT_PROPOSED: ("objective_id", "experiment_id", "action"),
    EventType.LEARNING_EXPERIMENT_APPROVED: ("objective_id", "experiment_id"),
    EventType.LEARNING_EXPERIMENT_REJECTED: ("objective_id", "experiment_id", "reason"),
    EventType.LEARNING_EXPERIMENT_STARTED: ("objective_id", "experiment_id"),
    EventType.LEARNING_OBSERVATION_COLLECTED: ("objective_id", "experiment_id", "complete"),
    EventType.LEARNING_PREDICTION_EVALUATED: (
        "objective_id",
        "experiment_id",
        "outcome",
        "basis",
    ),
    EventType.LEARNING_KNOWLEDGE_UPDATED: ("objective_id", "change"),
    EventType.LEARNING_EXPERIMENT_COMPLETED: ("objective_id", "experiment_id", "status"),
    EventType.LEARNING_EXPERIMENT_FAILED: ("objective_id", "experiment_id", "status", "error"),
    EventType.LEARNING_EXPERIMENT_INTERRUPTED: ("objective_id", "experiment_id", "reason"),
    EventType.LEARNING_EXPERIMENT_CANCELLED: ("objective_id", "reason"),
    EventType.LEARNING_BUDGET_EXHAUSTED: ("objective_id", "reason"),
    # Phase 24: the planned-goal vocabulary. ``candidate_id`` names the plan a
    # comparison picked and ``planner`` names which algorithm produced it, so a
    # watcher can see WHICH planner won rather than only that one did.
    EventType.GOAL_CREATED: ("goal_id",),
    EventType.GOAL_DECOMPOSED: ("goal_id", "tasks", "issues"),
    EventType.GOAL_PLAN_SELECTED: ("goal_id", "candidate_id", "planner"),
    EventType.GOAL_PLAN_REFUSED: ("goal_id",),
    EventType.GOAL_STEP_BLOCKED: ("goal_id", "step_id", "verdict"),
    EventType.GOAL_EXECUTED: ("goal_id", "status"),
    EventType.GOAL_REASSESSED: ("goal_id", "should_replan"),
    EventType.GOAL_CANCELLED: ("goal_id",),
    # Phase 25: the embodied-session vocabulary. ``session_id`` is the STABLE id
    # of one session, which is also its correlation id: a watcher that wants a
    # session's whole story keys on it alone. ``basis``/``freshness`` describe the
    # reading the step was taken from — never the reading itself.
    EventType.EMBODIED_SESSION_STARTED: ("session_id", "environment_id", "status"),
    EventType.EMBODIED_SESSION_PAUSED: ("session_id", "reason"),
    EventType.EMBODIED_SESSION_RESUMED: ("session_id", "reason"),
    EventType.EMBODIED_SESSION_CANCELLED: ("session_id", "reason"),
    EventType.EMBODIED_SESSION_ENDED: ("session_id", "status", "reason"),
    EventType.EMBODIED_OBSERVATION: ("session_id", "basis", "freshness"),
    EventType.EMBODIED_ACTION_SELECTED: ("session_id", "kind", "direction"),
    EventType.EMBODIED_ACTION_EXECUTED: ("session_id", "kind", "verified"),
    EventType.EMBODIED_ACTION_FAILED: ("session_id", "status"),
    EventType.EMBODIED_RECOVERY: ("session_id", "attempt", "ceiling"),
    EventType.EMBODIED_BLOCKED: ("session_id", "reason"),
    EventType.EMBODIED_OBJECTIVE_VERIFIED: ("session_id", "conditions"),
    EventType.EMBODIED_OBJECTIVE_FAILED: ("session_id", "reason"),
    # Phase 26: the run's identity and the counts it ended with. ``status`` is a
    # run category (``completed``/``budget_exhausted``/...) and ``category`` is a
    # failure category — both vocabularies live in ``generalization.models``.
    EventType.GENERALIZATION_TASK_REGISTERED: ("task_id", "family_id", "split"),
    EventType.GENERALIZATION_EVALUATION_STARTED: ("run_id", "solver", "tasks"),
    EventType.GENERALIZATION_EVALUATION_COMPLETED: ("run_id", "status", "solved"),
    EventType.GENERALIZATION_FAILURE_CLASSIFIED: ("run_id", "task_id", "category"),
    EventType.GENERALIZATION_LEAKAGE_DETECTED: ("run_id", "reason"),
    # ``sources``/``chunks`` are counts of what one ingest did (an ingest may
    # cover a whole directory); ``source_id`` names the single source it was
    # about. ``status`` carries the IngestReport outcome, so a watcher sees
    # added/updated/unchanged/skipped rather than a bare "something happened".
    EventType.KNOWLEDGE_INDEXED: ("sources", "chunks"),
    EventType.KNOWLEDGE_RETRIEVED: ("query", "hits", "sources"),
    # The world vocabulary. ``source_kind`` rather than ``source`` for the same
    # reason the perception vocabulary uses it: the envelope already carries
    # ``source``. ``entities``/``relationships`` are COUNTS of what moved, not the
    # records themselves — a notification channel is the wrong place for a state.
    EventType.WORLD_OBSERVATION_INGESTED: ("observation_id", "source_kind", "entities"),
    EventType.WORLD_STATE_UPDATED: ("state_id", "version", "entities"),
    EventType.WORLD_TRANSITION_CREATED: ("transition_id", "kind", "state_id"),
    EventType.WORLD_ENTITY_CHANGED: ("entity_id", "change"),
    EventType.WORLD_RELATIONSHIP_CHANGED: ("relationship_id", "kind", "status"),
    EventType.WORLD_CONFLICT_DETECTED: ("subject", "detail"),
    EventType.WORLD_STATE_RESTORED: ("state_id", "version"),
    EventType.WORLD_PREDICTION_COMPLETED: ("status", "provider"),
    EventType.WORLD_PREDICTION_FAILED: ("status", "error"),
    EventType.AUTOMATION_CREATED: ("automation_id",),
    EventType.AUTOMATION_UPDATED: ("automation_id", "status"),
    EventType.AUTOMATION_CANCELLED: ("automation_id",),
    EventType.AUTOMATION_STARTED: ("automation_id",),
    EventType.AUTOMATION_COMPLETED: ("automation_id", "run_id", "status"),
    EventType.AUTOMATION_FAILED: ("automation_id", "run_id", "error"),
    EventType.AUTOMATION_DENIED: ("automation_id", "reason"),
    EventType.AUTOMATION_SKIPPED: ("automation_id", "reason"),
    EventType.AUDIT_RECORDED: ("record_id", "task_id"),
    EventType.AUDIT_PRUNED: ("removed",),
    EventType.PRIVACY_MODE_CHANGED: ("mode",),
    EventType.PRIVACY_DENIED: ("action", "reason"),
}

#: Which event a Phase 8 task state publishes when it is entered. Kept here as
#: strings so the event layer does not import the reliability layer (which
#: imports this one's siblings) and neither has to know the other's types.
TASK_STATE_EVENTS: Mapping[str, EventType] = {
    "running": EventType.TASK_STARTED,
    "paused": EventType.TASK_PAUSED,
    "cancelled": EventType.TASK_CANCELLED,
    "completed": EventType.TASK_COMPLETED,
    "failed": EventType.TASK_FAILED,
    "resumed": EventType.TASK_RESUMED,
}


def task_event_name(state: str) -> EventType | None:
    """The lifecycle event for a task state, or None for a state with no event.

    Only the states that mean something to a watcher are named: RUNNING means
    work began or resumed, and the planning/verifying/recovering states are
    steps *within* a run rather than lifecycle moments of their own.
    """
    return TASK_STATE_EVENTS.get(str(state).strip().lower())


class InvalidEventPayloadError(ValueError):
    """A lifecycle event was published without the fields its type promises."""

    def __init__(self, type_: str, missing: Iterable[str]) -> None:
        self.type = type_
        self.missing = tuple(missing)
        super().__init__(
            f"Event {type_!r} is missing required payload field(s): "
            + ", ".join(self.missing)
        )


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one publish did: who heard it, and who failed.

    Returned rather than only logged, because "the event was published" and
    "the subscribers handled it" are different facts and a caller that cares can
    now tell them apart.
    """

    event_type: str
    delivered: int
    failures: tuple[HandlerFailure, ...] = ()

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "delivered": self.delivered,
            "failed": self.failed,
            "ok": self.ok,
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event exchanged between NovaControl modules."""

    type: str
    payload: MappingProxyType[str, Any] | dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    causation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.type or not self.type.strip():
            raise ValueError("Event type is required.")
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        validate_payload(self.type, self.payload)

    @classmethod
    def of(
        cls,
        type_: EventType | str,
        /,
        *,
        source: str = "system",
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> Event:
        """Build a typed event, refusing one that is missing what it promises."""
        return cls(
            type=str(type_),
            payload=dict(payload),
            source=source,
            correlation_id=correlation_id or uuid4().hex,
            causation_id=causation_id,
        )

    def child(self, type_: EventType | str, /, **payload: Any) -> Event:
        """A follow-on event caused by this one, keeping the correlation.

        What ties a whole request together — an intent, the decision about it,
        the tools it ran — without every publisher having to thread an id around.
        """
        return Event.of(
            type_,
            source=self.source,
            correlation_id=self.correlation_id,
            causation_id=self.correlation_id,
            **payload,
        )


def validate_payload(type_: str, payload: Mapping[str, Any]) -> None:
    """Check a known event type's required fields; ignore custom types.

    A type this build does not know is left alone on purpose: the vocabulary
    describes the lifecycle events, and a module is free to publish its own
    (``command.progress``, ``activity.completed``) without registering here.
    """
    known = _KNOWN_TYPES.get(str(type_))
    if known is None:
        return
    missing = [name for name in known if name not in payload]
    if missing:
        raise InvalidEventPayloadError(str(type_), missing)


_KNOWN_TYPES: Mapping[str, tuple[str, ...]] = {
    member.value: EVENT_PAYLOAD_FIELDS[member] for member in EventType
}


class EventBus:
    """Small async event bus used by modules to communicate without direct imports.

    Handlers may be coroutines or plain callables, and they are all invoked even
    when one of them fails: an event that a second subscriber never saw because
    a first subscriber raised is the silent coupling this bus exists to avoid.
    The failures still surface — logged, handed to the error handler, and
    returned in the :class:`PublishResult` — and in strict mode
    (``continue_on_error=False``, the default) publishing raises
    :class:`~novacontrol.core.errors.EventDeliveryError` once every subscriber
    has had its turn.
    """

    def __init__(
        self,
        *,
        journal: EventJournal | None = None,
        error_handler: EventErrorHandler | None = None,
        continue_on_error: bool = False,
        history: int = 0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: list[EventHandler] = []
        self._lock = asyncio.Lock()
        self._journal = journal
        self._error_handler = error_handler
        self._continue_on_error = continue_on_error
        #: Bounded in-memory history (0 = off). The durable record is the
        #: journal; this is the "what just happened?" ring a UI or a test reads.
        self._history_size = max(0, int(history))
        self._history: deque[Event] = deque(maxlen=self._history_size or None)
        self._logger = logger or _logger

    async def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe a handler to an event type, or `*` for all events."""
        if not event_type or not event_type.strip():
            raise ValueError("event_type is required.")
        async with self._lock:
            if event_type == "*":
                self._wildcard_handlers.append(handler)
            else:
                self._handlers[event_type].append(handler)

    async def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler previously registered via subscribe(). No-op when absent.

        Needed for short-lived subscribers such as SSE channels: a dropped client
        must not leave its forwarder invoked on every publish forever.
        """
        async with self._lock:
            if event_type == "*":
                if handler in self._wildcard_handlers:
                    self._wildcard_handlers.remove(handler)
                return
            bucket = self._handlers.get(event_type)
            if bucket is None:
                return
            if handler in bucket:
                bucket.remove(handler)
            if not bucket:
                del self._handlers[event_type]

    async def publish(self, event: Event) -> PublishResult:
        """Publish an event to exact-match and wildcard subscribers.

        Every subscriber runs. A handler that raises is recorded, logged and
        reported; in strict mode the publish then raises once, after the last
        handler has had its turn.
        """
        validate_payload(event.type, event.payload)
        if self._journal is not None:
            await self._journal.append(event)
        if self._history_size:
            self._history.append(event)

        async with self._lock:
            handlers = [
                *self._handlers.get(event.type, ()),
                *self._wildcard_handlers,
            ]

        failures: list[HandlerFailure] = []
        delivered = 0
        for handler in handlers:
            try:
                produced = handler(event)
                if isawaitable(produced):
                    await produced
            except Exception as exc:
                failure = HandlerFailure(event=event, handler=repr(handler), error=exc)
                failures.append(failure)
                self._logger.warning(
                    "Event handler failed for %s: %s: %s",
                    event.type,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                if self._error_handler is not None:
                    try:
                        handled = self._error_handler(failure)
                        if isawaitable(handled):
                            await handled
                    except Exception as nested:  # noqa: BLE001 - never mask the original
                        self._logger.warning(
                            "Event error handler failed for %s: %s: %s",
                            event.type,
                            type(nested).__name__,
                            nested,
                            exc_info=True,
                        )
            else:
                delivered += 1

        result = PublishResult(
            event_type=event.type, delivered=delivered, failures=tuple(failures)
        )
        if failures and not self._continue_on_error:
            raise EventDeliveryError(event, failures)
        return result

    async def emit(
        self,
        type_: EventType | str,
        /,
        *,
        source: str = "system",
        correlation_id: str = "",
        causation_id: str | None = None,
        **payload: Any,
    ) -> PublishResult:
        """Build and publish a typed event, never raising from a subscriber.

        The door a live system publishes through: one broken watcher must not be
        able to fail the request that announced itself to it. A payload that does
        not match the event's declared fields still raises, because that is a bug
        in the publisher rather than in a subscriber.

        ``source``, ``correlation_id`` and ``causation_id`` belong to the
        envelope and are therefore RESERVED: a payload field with one of those
        names would be a second value for the same argument, and Python refuses
        it. Name the value for what it is instead — an image being read is
        ``image``, a selection's provenance is ``selection_source``.
        """
        event = Event.of(
            type_,
            source=source,
            correlation_id=correlation_id,
            causation_id=causation_id,
            **payload,
        )
        try:
            return await self.publish(event)
        except EventDeliveryError as exc:
            self._logger.warning(
                "Event %s reached a failing handler: %s", event.type, exc
            )
            return PublishResult(
                event_type=event.type,
                delivered=0,
                failures=tuple(exc.failures),
            )

    def recent(
        self,
        *,
        limit: int = 20,
        type_: EventType | str | None = None,
    ) -> tuple[Event, ...]:
        """The most recent events this bus carried, newest last (history off -> empty)."""
        if not self._history_size:
            return ()
        wanted = str(type_) if type_ is not None else None
        kept = [
            event
            for event in self._history
            if wanted is None or event.type == wanted
        ]
        return tuple(kept[-max(0, int(limit)) :]) if limit else tuple(kept)

    def subscriber_count(self, event_type: str | None = None) -> int:
        """How many handlers a type would reach (wildcards included)."""
        if event_type is None:
            return len(self._wildcard_handlers) + sum(
                len(bucket) for bucket in self._handlers.values()
            )
        return len(self._handlers.get(event_type, ())) + len(self._wildcard_handlers)

    def subscribers(self, event_type: str | None = None) -> tuple[str, ...]:
        """Which event types have handlers, in a stable order (for status output)."""
        if event_type is not None:
            return tuple(
                sorted({type_ for type_ in self._handlers if type_ == event_type})
            )
        return tuple(sorted(self._handlers))

    async def clear(self) -> None:
        """Remove all subscriptions (history is kept)."""
        async with self._lock:
            self._handlers.clear()
            self._wildcard_handlers.clear()
