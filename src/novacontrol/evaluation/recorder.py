"""The trajectory recorder: an OBSERVER of the lifecycle NovaControl already runs.

The recorder does not add a second execution path, a second event bus or a
second source of truth. It subscribes to the typed events the application
already publishes — ``task.started``, ``intent.detected``, ``decision.created``,
``tool.*``, ``verification.*``, ``recovery.*``, ``task.completed`` — groups them
by their CORRELATION ID (the same id the request path already uses to tie a
whole request together), and turns the group into one
:class:`~novacontrol.evaluation.models.AgentTrajectory` when the task ends.

Three properties are non-negotiable here, and each is enforced structurally
rather than by convention:

  * **a recording failure cannot break the work it observes.** The handler body
    runs inside a catch-all; a failure is counted, the draft is left alone, and
    the request that announced itself continues. The event bus already isolates
    subscribers from each other; this recorder does not rely on that alone,
    because the bus's isolation is a property of the bus and this promise is a
    property of THIS component.
  * **disabled means silent.** With recording off, the handler returns before it
    looks at the payload, and no state is created. Disabling at run time is
    therefore immediate, and no half-written draft is left behind.
  * **bounded memory.** One rich trajectory per request is small; a thousand of
    them held forever is not. In-flight drafts are capped (the oldest is written
    out as an unfinished capture rather than kept), and finished rows are held in
    a small ring that exists only so a late annotation can still find them.

Annotations exist because two facts are measured OUTSIDE the event vocabulary —
the request's end-to-end latency (the interpretation telemetry's own trace) and
the machine's memory readings — and the layer that measures them should hand
them over rather than have this recorder guess. An annotation never raises and
never rewrites history: it merges into the draft, or into the recently finished
row (in which case the row is re-delivered to the sink, which upserts by
trajectory id).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.audit.redact import RedactionReport, Redactor
from novacontrol.core.events import Event, EventBus, EventType
from novacontrol.evaluation.models import (
    AgentTrajectory,
    ExecutionStep,
    LatencyMetrics,
    Observation,
    RecoveryRecord,
    ResourceUsage,
    ToolCallRecord,
    TrajectoryStatus,
    UserFeedback,
    VerificationRecord,
    now_iso,
)

_logger = logging.getLogger(__name__)

#: The lifecycle events a trajectory is assembled from. Deliberately a tuple of
#: the EXISTING vocabulary: a recorder that needed new events would be a new
#: execution path wearing an observer's name.
TRAJECTORY_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.TASK_STARTED,
    EventType.TASK_PAUSED,
    EventType.TASK_RESUMED,
    EventType.TASK_CANCELLED,
    EventType.TASK_COMPLETED,
    EventType.TASK_FAILED,
    EventType.INTENT_DETECTED,
    EventType.CONTEXT_RESOLVED,
    EventType.DECISION_CREATED,
    EventType.PLAN_CREATED,
    EventType.TOOL_SELECTED,
    EventType.TOOL_STARTED,
    EventType.TOOL_COMPLETED,
    EventType.TOOL_FAILED,
    EventType.VERIFICATION_STARTED,
    EventType.VERIFICATION_COMPLETED,
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_COMPLETED,
)

#: Where a sink puts a finished trajectory. A plain callable so the recorder can
#: be tested (and used) with no storage at all.
TrajectorySink = Callable[[AgentTrajectory], None]

#: Payload keys a tool event may carry that describe the CALL rather than the
#: work. Arguments are never published by the executor (which keeps a credential
#: out of the event stream to begin with); this set exists so an application
#: that DOES publish them can be captured deliberately.
_ARGUMENT_KEYS = ("arguments", "params", "parameters")

#: Payload keys that describe how long something took, in milliseconds.
_DURATION_KEYS = ("duration_ms", "elapsed_ms", "latency_ms", "total_ms")

#: Payload keys that describe the machine around the run.
_RESOURCE_KEYS = (
    "ram_before_bytes",
    "ram_after_bytes",
    "ram_delta_bytes",
    "cpu_percent",
    "gpu_utilization",
)

#: Payload keys that describe which model was involved.
_MODEL_KEYS = ("model", "provider", "model_size_bytes", "role")

#: The event names this recorder listens to, as plain strings (the bus speaks
#: strings, and a set lookup on every publish is cheaper than a rebuild).
_OBSERVED: frozenset[str] = frozenset(str(type_) for type_ in TRAJECTORY_EVENT_TYPES)

_OPEN_STATUSES = frozenset({"selected", "started"})
_TERMINAL_EVENTS = frozenset(
    {
        EventType.TASK_COMPLETED.value,
        EventType.TASK_FAILED.value,
        EventType.TASK_CANCELLED.value,
    }
)


@dataclass(slots=True)
class _Draft:
    """A trajectory being built. Mutable on purpose; frozen when it is written."""

    correlation_id: str
    trajectory_id: str
    source: str = "application"
    task_id: str = ""
    parent_task_id: str = ""
    started_at: str = field(default_factory=now_iso)
    user_request: str = ""
    context_summary: dict[str, Any] = field(default_factory=dict)
    structured_intent: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    execution_steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    recovery_events: list[dict[str, Any]] = field(default_factory=list)
    final_result: dict[str, Any] = field(default_factory=dict)
    status: str = TrajectoryStatus.IN_PROGRESS.value
    success: bool | None = None
    failure_reason: str = ""
    model_information: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=lambda: LatencyMetrics().to_dict())
    resources: dict[str, Any] = field(default_factory=lambda: ResourceUsage().to_dict())
    feedback: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    event_count: int = 0
    redactions: int = 0
    redaction_kinds: tuple[str, ...] = ()

    def open_call_index(self, tool: str) -> int | None:
        """The newest open call for a tool (a tool call completes where it started)."""
        for index in range(len(self.tool_calls) - 1, -1, -1):
            call = self.tool_calls[index]
            if str(call.get("tool", "")) == tool and str(call.get("status", "")) in _OPEN_STATUSES:
                return index
        return None


class TrajectoryRecorder:
    """Assembles trajectories from the lifecycle events on an existing bus."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        redactor: Redactor | None = None,
        sink: TrajectorySink | None = None,
        source: str = "application",
        max_in_flight: int = 64,
        max_recent: int = 32,
    ) -> None:
        self._enabled = bool(enabled)
        self._redactor = redactor if redactor is not None else Redactor()
        self._sink = sink
        self._source = str(source or "application")
        self._max_in_flight = max(1, int(max_in_flight))
        self._max_recent = max(0, int(max_recent))
        self._drafts: dict[str, _Draft] = {}
        #: Recently finished rows, so a late annotation can still find one. It is
        #: a convenience cache, not storage — the repositories are the record.
        self._recent: dict[str, AgentTrajectory] = {}
        self._completed: list[AgentTrajectory] = []
        self._attached_bus: EventBus | None = None
        #: Counters a status endpoint can read. ``failures`` is the honest
        #: number: an observer that has been failing is still an observer that
        #: must not have broken anything, and saying so requires counting.
        self.dropped = 0
        self.failures = 0
        self.flushed_partial = 0

    # -- configuration ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> bool:
        """Turn recording on or off. In-flight drafts are written out, not lost."""
        self._enabled = bool(enabled)
        if not self._enabled:
            self.flush(complete=False)
        return self._enabled

    @property
    def sink(self) -> TrajectorySink | None:
        return self._sink

    def set_sink(self, sink: TrajectorySink | None) -> None:
        self._sink = sink

    @property
    def in_flight(self) -> int:
        return len(self._drafts)

    # -- bus attachment --------------------------------------------------------

    async def attach(self, event_bus: EventBus) -> None:
        """Subscribe to the lifecycle. Idempotent for the same bus."""
        if self._attached_bus is event_bus:
            return
        if self._attached_bus is not None:
            await self.detach(self._attached_bus)
        for type_ in TRAJECTORY_EVENT_TYPES:
            await event_bus.subscribe(str(type_), self.observe)
        self._attached_bus = event_bus

    async def detach(self, event_bus: EventBus | None = None) -> None:
        """Unsubscribe, and write out anything still in flight."""
        bus = event_bus if event_bus is not None else self._attached_bus
        if bus is None:
            return
        for type_ in TRAJECTORY_EVENT_TYPES:
            try:
                await bus.unsubscribe(str(type_), self.observe)
            except Exception:  # noqa: BLE001 - detaching must not fail a stop
                self.failures += 1
        if bus is self._attached_bus:
            self._attached_bus = None
        self.flush(complete=False)

    # -- observation -----------------------------------------------------------

    async def observe(self, event: Event) -> None:
        """The bus handler. Never raises: the work it describes must go on.

        Recording is off, or the event is not one this recorder understands, and
        this returns without touching any state.
        """
        if not self._enabled:
            return
        if event.type not in _OBSERVED:
            return
        try:
            await self._observe(event)
        except Exception as exc:  # noqa: BLE001 - an observer must not break the work
            self.failures += 1
            _logger.warning(
                "Trajectory recorder ignored a %s event it could not use: %s: %s",
                event.type,
                type(exc).__name__,
                exc,
            )

    async def _observe(self, event: Event) -> None:
        correlation = str(event.correlation_id or "")
        payload = {str(key): value for key, value in event.payload.items()}
        if _TERMINAL_EVENTS.intersection({event.type}):
            # A terminal event may arrive for a run this recorder never saw start
            # (a specialist pipeline publishes its own lifecycle): the row is
            # still worth keeping, so the draft is created here rather than lost.
            draft = self._draft(correlation, source=str(event.source or self._source))
            draft.event_count += 1
            self._apply_terminal(draft, event.type, payload)
            self._finish(correlation, draft)
            return
        draft = self._draft(correlation, source=str(event.source or self._source))
        draft.event_count += 1
        self._apply(draft, event.type, payload)

    def _apply(self, draft: _Draft, type_: str, payload: Mapping[str, Any]) -> None:
        if type_ == EventType.TASK_STARTED.value:
            draft.task_id = _text(payload.get("task_id")) or draft.task_id
            draft.parent_task_id = _text(payload.get("parent_task_id")) or draft.parent_task_id
        elif type_ == EventType.INTENT_DETECTED.value:
            draft.structured_intent = self._safe_mapping(payload, draft)
        elif type_ == EventType.CONTEXT_RESOLVED.value:
            draft.context_summary = self._safe_mapping(payload, draft)
        elif type_ == EventType.DECISION_CREATED.value:
            draft.decision = self._safe_mapping(payload, draft)
        elif type_ == EventType.PLAN_CREATED.value:
            draft.plan = self._safe_mapping(payload, draft)
            steps = payload.get("steps")
            if isinstance(steps, (list, tuple)):
                draft.execution_steps = [
                    self._safe_mapping(step, draft) if isinstance(step, Mapping) else {}
                    for step in steps
                ]
        elif type_ == EventType.TOOL_SELECTED.value:
            draft.tool_calls.append(
                self._safe_mapping({**payload, "status": "selected", "timestamp": now_iso()}, draft)
            )
        elif type_ == EventType.TOOL_STARTED.value:
            self._open_call(draft, payload)
        elif type_ == EventType.TOOL_COMPLETED.value:
            self._close_call(draft, payload, default_status="completed")
        elif type_ == EventType.TOOL_FAILED.value:
            self._close_call(draft, payload, default_status="failed")
        elif type_ == EventType.VERIFICATION_COMPLETED.value:
            draft.verification_results.append(
                self._safe_mapping({**payload, "timestamp": now_iso()}, draft)
            )
        elif type_ == EventType.RECOVERY_COMPLETED.value:
            draft.recovery_events.append(
                self._safe_mapping({**payload, "timestamp": now_iso()}, draft)
            )
        elif type_ in {
            EventType.VERIFICATION_STARTED.value,
            EventType.RECOVERY_STARTED.value,
        }:
            draft.observations.append(
                self._safe_mapping(
                    {"kind": type_, "summary": "", "source": "lifecycle", **payload}, draft
                )
            )
        elif type_ == EventType.TASK_PAUSED.value:
            draft.metadata["paused"] = True
        elif type_ == EventType.TASK_RESUMED.value:
            draft.metadata["resumed"] = True
        self._capture_figures(draft, payload)

    def _apply_terminal(self, draft: _Draft, type_: str, payload: Mapping[str, Any]) -> None:
        if type_ == EventType.TASK_COMPLETED.value:
            draft.status = TrajectoryStatus.COMPLETED.value
            draft.success = True
            draft.final_result = self._safe_mapping(payload, draft)
        elif type_ == EventType.TASK_FAILED.value:
            draft.status = TrajectoryStatus.FAILED.value
            draft.success = False
            draft.failure_reason = _text(payload.get("error")) or "task failed"
            draft.final_result = self._safe_mapping(payload, draft)
        else:  # TASK_CANCELLED
            draft.status = TrajectoryStatus.CANCELLED.value
            draft.success = None
            draft.failure_reason = "cancelled"
            draft.final_result = self._safe_mapping(payload, draft)
        draft.task_id = _text(payload.get("task_id")) or draft.task_id
        self._capture_figures(draft, payload)

    def _open_call(self, draft: _Draft, payload: Mapping[str, Any]) -> None:
        tool = _text(payload.get("tool"))
        index = draft.open_call_index(tool)
        started = self._safe_mapping(
            {**payload, "status": "started", "timestamp": now_iso()}, draft
        )
        if index is None:
            draft.tool_calls.append(started)
            return
        merged = {**draft.tool_calls[index], **started}
        draft.tool_calls[index] = merged

    def _close_call(
        self, draft: _Draft, payload: Mapping[str, Any], *, default_status: str
    ) -> None:
        tool = _text(payload.get("tool"))
        closed = self._safe_mapping(
            {**payload, "status": _text(payload.get("status")) or default_status}, draft
        )
        index = draft.open_call_index(tool)
        if index is None:
            # A completion for a call this recorder never saw start (the tool ran
            # inside a nested service). Record it rather than drop it: the fact
            # that the tool ran is true either way.
            draft.tool_calls.append(closed)
            return
        merged = {**draft.tool_calls[index], **closed}
        draft.tool_calls[index] = merged
        if default_status == "failed":
            output = _text(payload.get("output", ""))
            if output:
                draft.observations.append(
                    self._safe_mapping(
                        {
                            "kind": "tool_error",
                            "summary": _text(payload.get("error")) or output,
                            "source": tool,
                            "detail": {"status": merged.get("status", "")},
                        },
                        draft,
                    )
                )

    def _capture_figures(self, draft: _Draft, payload: Mapping[str, Any]) -> None:
        """Pick up measured figures a payload happens to carry, and nothing else.

        This is how a model name, a duration or a memory reading published beside
        a lifecycle event reaches the trajectory. A key that is absent stays
        absent: there is no default invented for it here.
        """
        model = {
            key: payload[key]
            for key in _MODEL_KEYS
            if key in payload and payload[key] not in (None, "")
        }
        if model:
            draft.model_information.update(model)
        resources = {
            key: payload[key] for key in _RESOURCE_KEYS if isinstance(payload.get(key), int)
        }
        if resources:
            draft.resources.update(resources)
            draft.resources["samples"] = int(draft.resources.get("samples", 0)) + 1
        for key in _DURATION_KEYS:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                draft.latency["total_ms"] = float(value)
                break

    def _safe_mapping(self, value: Any, draft: _Draft) -> dict[str, Any]:
        """A payload mapping, redacted, with the redactions counted on the draft."""
        if not isinstance(value, Mapping):
            return {}
        cleaned, report = self._redactor.redact_value(dict(value))
        self._count_redactions(draft, report)
        if isinstance(cleaned, Mapping):
            return {str(key): item for key, item in cleaned.items()}
        return {}

    def _count_redactions(self, draft: _Draft, report: RedactionReport) -> None:
        if not report.count:
            return
        draft.redactions += report.count
        draft.redaction_kinds = tuple(dict.fromkeys(draft.redaction_kinds + report.kinds))

    # -- drafts ----------------------------------------------------------------

    def _draft(self, correlation_id: str, *, source: str) -> _Draft:
        draft = self._drafts.get(correlation_id)
        if draft is not None:
            return draft
        if len(self._drafts) >= self._max_in_flight:
            self._evict_oldest()
        draft = _Draft(
            correlation_id=correlation_id,
            trajectory_id=correlation_id or "",
            source=source or self._source,
        )
        self._drafts[correlation_id] = draft
        return draft

    def _evict_oldest(self) -> None:
        """Write out the oldest unfinished draft instead of growing without bound."""
        if not self._drafts:
            return
        key = next(iter(self._drafts))
        draft = self._drafts.pop(key)
        self.flushed_partial += 1
        self._deliver(self._build(draft))

    def _finish(self, correlation_id: str, draft: _Draft) -> None:
        self._drafts.pop(correlation_id, None)
        trajectory = self._build(draft)
        self._remember(trajectory)
        self._deliver(trajectory)

    def _remember(self, trajectory: AgentTrajectory) -> None:
        self._completed.append(trajectory)
        if self._max_recent <= 0:
            # ``rows[-0:]`` is the whole list, not none of it: a zero-sized ring
            # has to be emptied explicitly.
            self._completed.clear()
        elif len(self._completed) > self._max_recent:
            self._completed = self._completed[-self._max_recent :]
        if trajectory.task_id:
            self._recent[trajectory.task_id] = trajectory
        self._recent[trajectory.trajectory_id] = trajectory
        if len(self._recent) > max(self._max_recent, 1) * 2:
            for key in list(self._recent)[: -max(self._max_recent, 1)]:
                self._recent.pop(key, None)

    def _deliver(self, trajectory: AgentTrajectory) -> None:
        """Hand a trajectory to the sink, never letting the sink break the run."""
        if self._sink is None:
            return
        try:
            self._sink(trajectory)
        except Exception as exc:  # noqa: BLE001 - a sink is a consumer, not a gate
            self.failures += 1
            _logger.warning(
                "Trajectory sink failed for %s: %s: %s",
                trajectory.trajectory_id,
                type(exc).__name__,
                exc,
            )

    def _build(self, draft: _Draft) -> AgentTrajectory:
        feedback = draft.feedback if isinstance(draft.feedback, Mapping) else None
        return AgentTrajectory(
            trajectory_id=draft.trajectory_id or draft.correlation_id,
            task_id=draft.task_id or draft.correlation_id,
            parent_task_id=draft.parent_task_id,
            timestamp=draft.started_at,
            source=draft.source,
            user_request=draft.user_request,
            context_summary=draft.context_summary,
            structured_intent=draft.structured_intent,
            decision=draft.decision,
            plan=draft.plan,
            execution_steps=tuple(_parsed(draft.execution_steps, ExecutionStep.from_dict)),
            tool_calls=tuple(_parsed(draft.tool_calls, ToolCallRecord.from_dict)),
            observations=tuple(_parsed(draft.observations, Observation.from_dict)),
            verification_results=tuple(
                _parsed(draft.verification_results, VerificationRecord.from_dict)
            ),
            recovery_events=tuple(_parsed(draft.recovery_events, RecoveryRecord.from_dict)),
            final_result=draft.final_result,
            status=draft.status,
            success=draft.success,
            failure_reason=draft.failure_reason,
            model_information=draft.model_information,
            latency_metrics=LatencyMetrics.from_dict(draft.latency),
            resource_usage=ResourceUsage.from_dict(draft.resources),
            user_feedback=UserFeedback.from_dict(dict(feedback)) if feedback else None,
            event_count=draft.event_count,
            redactions=draft.redactions,
            redaction_kinds=draft.redaction_kinds,
            metadata=draft.metadata,
        )

    # -- annotation ------------------------------------------------------------

    def annotate(self, correlation_id: str, **fields: Any) -> bool:
        """Attach a measured fact the lifecycle does not carry. Never raises.

        Known fields are ``user_request``, ``context_summary``, ``final_result``,
        ``model_information``, ``latency_metrics``, ``resource_usage``,
        ``user_feedback``, ``parent_task_id`` and ``metadata``. Anything else
        lands in ``metadata`` under its own name, because a caller handing over an
        extra fact should not have it silently dropped.

        Returns whether anything was written.
        """
        key = str(correlation_id or "")
        if not key:
            return False
        try:
            draft = self._drafts.get(key)
            if draft is not None:
                self._merge(draft, fields)
                return True
            recent = self._recent.get(key)
            if recent is not None:
                updated = self._apply_to_row(recent, fields)
                self._remember(updated)
                self._deliver(updated)
                return True
            draft = self._draft(key, source=self._source)
            self._merge(draft, fields)
            return True
        except Exception as exc:  # noqa: BLE001 - an annotation is a courtesy
            self.failures += 1
            _logger.warning(
                "Trajectory annotation failed for %s: %s: %s", key, type(exc).__name__, exc
            )
            return False

    def _merge(self, draft: _Draft, fields: Mapping[str, Any]) -> None:
        for name, value in fields.items():
            if name == "user_request":
                cleaned = self._redactor.redact_text(_text(value))
                draft.user_request = cleaned.text
                self._count_redactions(draft, RedactionReport(cleaned.count, cleaned.kinds))
            elif name == "context_summary":
                draft.context_summary.update(self._safe_mapping(value, draft))
            elif name == "final_result":
                draft.final_result.update(self._safe_mapping(value, draft))
            elif name == "model_information":
                draft.model_information.update(self._safe_mapping(value, draft))
            elif name == "resource_usage":
                draft.resources.update(self._safe_mapping(value, draft))
                draft.resources["samples"] = int(draft.resources.get("samples", 0)) + 1
            elif name == "latency_metrics":
                draft.latency.update(self._safe_mapping(value, draft))
            elif name == "user_feedback":
                draft.feedback = self._safe_mapping(value, draft)
            elif name == "parent_task_id":
                draft.parent_task_id = _text(value)
            elif name == "metadata":
                draft.metadata.update(self._safe_mapping(value, draft))
            else:
                draft.metadata[name] = _text(value) if isinstance(value, str) else value

    def _apply_to_row(self, row: AgentTrajectory, fields: Mapping[str, Any]) -> AgentTrajectory:
        """A late annotation against a finished row: re-redact, then rebuild."""
        scratch = _Draft(correlation_id=row.trajectory_id, trajectory_id=row.trajectory_id)
        patch = self._safe_mapping(fields, scratch)
        updates: dict[str, Any] = {}
        if "user_request" in fields:
            outcome = self._redactor.redact_text(_text(fields["user_request"]))
            updates["user_request"] = outcome.text
        if "context_summary" in patch:
            updates["context_summary"] = {**row.context_summary, **patch["context_summary"]}
        if "final_result" in patch:
            updates["final_result"] = {**row.final_result, **patch["final_result"]}
        if "model_information" in patch:
            updates["model_information"] = {**row.model_information, **patch["model_information"]}
        if "latency_metrics" in patch:
            merged = {**row.latency_metrics.to_dict(), **patch["latency_metrics"]}
            updates["latency_metrics"] = LatencyMetrics.from_dict(merged)
        if "resource_usage" in patch:
            merged = {**row.resource_usage.to_dict(), **patch["resource_usage"]}
            updates["resource_usage"] = ResourceUsage.from_dict(merged)
        if "user_feedback" in patch:
            updates["user_feedback"] = UserFeedback.from_dict(patch["user_feedback"])
        if "parent_task_id" in fields:
            updates["parent_task_id"] = _text(fields["parent_task_id"])
        if "metadata" in patch:
            updates["metadata"] = {**row.metadata, **patch["metadata"]}
        if scratch.redactions:
            # The annotation was redacted on the way in; say so on the row it
            # updated, so a late annotation cannot lower a row's stated
            # redaction count by arriving after the run ended.
            updates["redactions"] = row.redactions + scratch.redactions
            updates["redaction_kinds"] = tuple(
                dict.fromkeys(row.redaction_kinds + scratch.redaction_kinds)
            )
        if not updates:
            return row
        return replace(row, **updates)

    # -- reading and maintenance ----------------------------------------------

    def trajectories(self, limit: int = 0) -> tuple[AgentTrajectory, ...]:
        """Finished trajectories this recorder still holds (newest last)."""
        rows = list(self._completed)
        if limit and limit > 0:
            return tuple(rows[-limit:])
        return tuple(rows)

    def flush(self, *, complete: bool = False) -> tuple[AgentTrajectory, ...]:
        """Write out every in-flight draft, as an unfinished capture by default.

        Called when recording is switched off and when the recorder is detached.
        ``complete=True`` marks the rows as completed instead — a caller that
        knows the run ended but never saw the terminal event (a crashed process
        being replayed) can say so.
        """
        produced: list[AgentTrajectory] = []
        for key in list(self._drafts):
            draft = self._drafts.pop(key)
            if complete:
                draft.status = TrajectoryStatus.COMPLETED.value
                draft.success = True if draft.success is None else draft.success
            self.flushed_partial += 1
            trajectory = self._build(draft)
            self._remember(trajectory)
            self._deliver(trajectory)
            produced.append(trajectory)
        return tuple(produced)

    def clear(self) -> int:
        """Forget everything held in memory. The repositories are untouched."""
        removed = len(self._drafts) + len(self._completed)
        self._drafts.clear()
        self._recent.clear()
        self._completed.clear()
        return removed

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "in_flight": len(self._drafts),
            "completed_held": len(self._completed),
            "failures": self.failures,
            "flushed_partial": self.flushed_partial,
            "max_in_flight": self._max_in_flight,
            "redaction_enabled": self._redactor.enabled,
            "observed_event_types": [str(type_) for type_ in TRAJECTORY_EVENT_TYPES],
        }


def _parsed(rows: Sequence[Mapping[str, Any]], parser: Any) -> list[Any]:
    parsed: list[Any] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            parsed.append(parser(dict(row)))
        except (TypeError, ValueError):
            continue
    return parsed


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


__all__ = ["TRAJECTORY_EVENT_TYPES", "TrajectoryRecorder", "TrajectorySink"]
