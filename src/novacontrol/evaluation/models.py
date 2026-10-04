"""Phase 15's shared vocabulary: what a trajectory IS, and what it is not.

A trajectory is the structured record of one piece of work: what was asked, what
was understood, what was decided, what was planned, what ran, what was observed,
what was verified, what was recovered, and how it ended — plus the measurements
that describe the run (latency, memory) and the feedback a person gave it.

Two rules shape every field here, and they are the phase's own instructions:

  * **structured only.** Intent, decision, plan, tool calls, verification and
    recovery are stored as the records the live layers already produce. The
    private reasoning that produced them is NOT stored, and there is no field
    for it: a model's hidden chain-of-thought is not data this project collects.
  * **a figure is either measured or absent.** ``duration_ms`` and the resource
    readings are ``None`` when nobody measured them, never a zero that would
    read as "free". That is the same rule ``optimization/models.py`` follows.

Every field is optional with a usable default, so an existing execution path can
hand over as much or as little as it has. Unknown keys in a stored row are
ignored by :meth:`AgentTrajectory.from_dict` rather than refused, because a row
written by a later schema is a compatibility case, not a corruption.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.audit.models import json_safe

#: The trajectory schema's version. Bumped only for a change a reader must know
#: about; a reader that sees a newer number still parses what it understands.
TRAJECTORY_SCHEMA_VERSION = 1

#: Evaluation and reward rows carry their own version so a stored score can be
#: read later knowing which rules produced it.
EVALUATION_SCHEMA_VERSION = 1
REWARD_SCHEMA_VERSION = 1
GOLDEN_DATASET_SCHEMA_VERSION = 1


def now_iso() -> str:
    """The timestamp format every stored row uses."""
    return datetime.now(UTC).isoformat()


class TrajectoryStatus(StrEnum):
    """How far a trajectory got."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether the run this trajectory describes has ended."""
        return self is not TrajectoryStatus.IN_PROGRESS


class QualityVerdictValue(StrEnum):
    """What the data-quality filter decided about one trajectory."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class EvaluationDimension(StrEnum):
    """The nine levels the evaluation engine scores, one at a time."""

    NLU = "nlu"
    DECISION = "decision"
    TOOL_SELECTION = "tool_selection"
    PLANNING = "planning"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    SAFETY = "safety"
    EFFICIENCY = "efficiency"


class DimensionStatus(StrEnum):
    """A dimension's reading: scored well, scored badly, or had no evidence."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


class OverallStatus(StrEnum):
    """The whole evaluation's reading."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


# ── small parsers ────────────────────────────────────────────────────────────
#
# Every ``from_dict`` below reads a row that may have been written by another
# build (or hand-edited), so each field is read defensively: a value of the
# wrong shape falls back to its default rather than raising. A trajectory store
# that refuses to load rows is a store nobody can analyse.


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    return {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value)
    return ()


def _rows(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    """One step of a plan, as the plan layer states it and execution found it."""

    step_id: str = ""
    index: int = 0
    description: str = ""
    action: str = ""
    status: str = ""
    tool: str = ""
    depends_on: tuple[str, ...] = ()
    attempts: int = 0
    duration_ms: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "index": self.index,
            "description": self.description,
            "action": self.action,
            "status": self.status,
            "tool": self.tool,
            "depends_on": list(self.depends_on),
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionStep:
        return cls(
            step_id=_text(data.get("step_id")),
            index=int(_number(data.get("index")) or 0),
            description=_text(data.get("description")),
            action=_text(data.get("action")),
            status=_text(data.get("status")),
            tool=_text(data.get("tool")),
            depends_on=_texts(data.get("depends_on")),
            attempts=int(_number(data.get("attempts")) or 0),
            duration_ms=_number(data.get("duration_ms")),
            error=_text(data.get("error")),
        )


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call: what was asked of it, and what came back.

    Arguments are stored REDACTED (the recorder sanitises before anything is
    written), because a tool argument is where a password or an API key would
    otherwise land in a dataset that outlives the request.
    """

    tool: str = ""
    call_id: str = ""
    step_id: str = ""
    capability: str = ""
    executor: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str = "started"
    error: str = ""
    duration_ms: float | None = None
    output_summary: str = ""
    selection_source: str = ""
    confidence: float | None = None
    requires_confirmation: bool = False
    approved: bool | None = None
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "call_id": self.call_id,
            "step_id": self.step_id,
            "capability": self.capability,
            "executor": self.executor,
            "arguments": dict(self.arguments),
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "output_summary": self.output_summary,
            "selection_source": self.selection_source,
            "confidence": self.confidence,
            "requires_confirmation": self.requires_confirmation,
            "approved": self.approved,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolCallRecord:
        return cls(
            tool=_text(data.get("tool")),
            call_id=_text(data.get("call_id")),
            step_id=_text(data.get("step_id")),
            capability=_text(data.get("capability")),
            executor=_text(data.get("executor")),
            arguments=_mapping(data.get("arguments")),
            status=_text(data.get("status"), "started"),
            error=_text(data.get("error")),
            duration_ms=_number(data.get("duration_ms")),
            output_summary=_text(data.get("output_summary")),
            selection_source=_text(data.get("selection_source")),
            confidence=_number(data.get("confidence")),
            requires_confirmation=bool(data.get("requires_confirmation", False)),
            approved=_flag(data.get("approved")),
            timestamp=_text(data.get("timestamp")),
        )

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and not self.error

    @property
    def failed(self) -> bool:
        return self.status == "failed" or bool(self.error)


@dataclass(frozen=True, slots=True)
class Observation:
    """Something the run SAW — a tool's answer, a reading, a note."""

    kind: str = "note"
    summary: str = ""
    source: str = ""
    step_id: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "source": self.source,
            "step_id": self.step_id,
            "detail": dict(self.detail),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Observation:
        return cls(
            kind=_text(data.get("kind"), "note"),
            summary=_text(data.get("summary")),
            source=_text(data.get("source")),
            step_id=_text(data.get("step_id")),
            detail=_mapping(data.get("detail")),
            timestamp=_text(data.get("timestamp")),
        )


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """What verification said about one step."""

    step_id: str = ""
    status: str = ""
    verifier: str = ""
    detail: str = ""
    attempts: int = 0
    duration_ms: float | None = None
    timestamp: str = ""

    @property
    def passed(self) -> bool:
        return self.status in {"verified", "ok", "passed", "success"}

    @property
    def failed(self) -> bool:
        return self.status in {"failed", "error", "mismatch"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "verifier": self.verifier,
            "detail": self.detail,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationRecord:
        return cls(
            step_id=_text(data.get("step_id")),
            status=_text(data.get("status")),
            verifier=_text(data.get("verifier")),
            detail=_text(data.get("detail")),
            attempts=int(_number(data.get("attempts")) or 0),
            duration_ms=_number(data.get("duration_ms")),
            timestamp=_text(data.get("timestamp")),
        )


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """What the recovery layer tried when something failed, and how it ended."""

    step_id: str = ""
    outcome: str = ""
    strategy: str = ""
    detail: str = ""
    attempts: int = 0
    duration_ms: float | None = None
    timestamp: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome in {"recovered", "completed", "success", "ok"}

    @property
    def failed(self) -> bool:
        return self.outcome in {"failed", "exhausted", "unrecoverable", "error"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "outcome": self.outcome,
            "strategy": self.strategy,
            "detail": self.detail,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RecoveryRecord:
        return cls(
            step_id=_text(data.get("step_id")),
            outcome=_text(data.get("outcome")),
            strategy=_text(data.get("strategy")),
            detail=_text(data.get("detail")),
            attempts=int(_number(data.get("attempts")) or 0),
            duration_ms=_number(data.get("duration_ms")),
            timestamp=_text(data.get("timestamp")),
        )


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    """How long the run took, and where the time went. ``None`` = not measured."""

    total_ms: float | None = None
    stages_ms: Mapping[str, Any] = field(default_factory=dict)
    first_token_ms: float | None = None
    tool_ms: float | None = None
    steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "stages_ms": dict(self.stages_ms),
            "first_token_ms": self.first_token_ms,
            "tool_ms": self.tool_ms,
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LatencyMetrics:
        return cls(
            total_ms=_number(data.get("total_ms")),
            stages_ms=_mapping(data.get("stages_ms")),
            first_token_ms=_number(data.get("first_token_ms")),
            tool_ms=_number(data.get("tool_ms")),
            steps=int(_number(data.get("steps")) or 0),
        )


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """What the machine looked like around the run. ``None`` = not measured."""

    ram_before_bytes: int | None = None
    ram_after_bytes: int | None = None
    ram_delta_bytes: int | None = None
    cpu_percent: float | None = None
    gpu_utilization: float | None = None
    samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ram_before_bytes": self.ram_before_bytes,
            "ram_after_bytes": self.ram_after_bytes,
            "ram_delta_bytes": self.ram_delta_bytes,
            "cpu_percent": self.cpu_percent,
            "gpu_utilization": self.gpu_utilization,
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourceUsage:
        def whole(key: str) -> int | None:
            value = _number(data.get(key))
            return None if value is None else int(value)

        return cls(
            ram_before_bytes=whole("ram_before_bytes"),
            ram_after_bytes=whole("ram_after_bytes"),
            ram_delta_bytes=whole("ram_delta_bytes"),
            cpu_percent=_number(data.get("cpu_percent")),
            gpu_utilization=_number(data.get("gpu_utilization")),
            samples=int(_number(data.get("samples")) or 0),
        )

    @property
    def measured(self) -> bool:
        return self.samples > 0 or self.ram_delta_bytes is not None


@dataclass(frozen=True, slots=True)
class UserFeedback:
    """What a person said about a run — the one signal a model cannot invent."""

    rating: int | None = None
    label: str = ""
    comment: str = ""
    source: str = "operator"
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rating": self.rating,
            "label": self.label,
            "comment": self.comment,
            "source": self.source,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UserFeedback:
        rating = _number(data.get("rating"))
        return cls(
            rating=None if rating is None else int(rating),
            label=_text(data.get("label")),
            comment=_text(data.get("comment")),
            source=_text(data.get("source"), "operator"),
            timestamp=_text(data.get("timestamp")),
        )


def _records(
    value: Any, parser: Any
) -> list[Any]:
    """Parse a list of mappings with ``parser``, skipping anything unusable."""
    parsed: list[Any] = []
    for row in _rows(value):
        if not isinstance(row, Mapping):
            continue
        try:
            parsed.append(parser(dict(row)))
        except (TypeError, ValueError):
            continue
    return parsed


@dataclass(frozen=True, slots=True)
class AgentTrajectory:
    """One piece of work, in full: the record future learning phases consume.

    The pipeline this describes is the one NovaControl already runs —
    request → understanding → context → decision → plan → execution → tools →
    observations → verification → recovery → result — so a trajectory is a
    PROJECTION of that path rather than a second copy of it.

    ``success`` is ``bool | None``: ``None`` means the run ended without
    anything deciding whether it worked, which is a real outcome and must not be
    stored as a failure.
    """

    trajectory_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    parent_task_id: str = ""
    timestamp: str = field(default_factory=now_iso)
    schema_version: int = TRAJECTORY_SCHEMA_VERSION
    source: str = "application"
    user_request: str = ""
    context_summary: Mapping[str, Any] = field(default_factory=dict)
    structured_intent: Mapping[str, Any] = field(default_factory=dict)
    decision: Mapping[str, Any] = field(default_factory=dict)
    plan: Mapping[str, Any] = field(default_factory=dict)
    execution_steps: tuple[ExecutionStep, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    observations: tuple[Observation, ...] = ()
    verification_results: tuple[VerificationRecord, ...] = ()
    recovery_events: tuple[RecoveryRecord, ...] = ()
    final_result: Mapping[str, Any] = field(default_factory=dict)
    status: str = TrajectoryStatus.IN_PROGRESS.value
    success: bool | None = None
    failure_reason: str = ""
    model_information: Mapping[str, Any] = field(default_factory=dict)
    latency_metrics: LatencyMetrics = field(default_factory=LatencyMetrics)
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    user_feedback: UserFeedback | None = None
    reward: Mapping[str, Any] | None = None
    #: Phase 18's optional step/intermediate/terminal/cumulative reading of the
    #: same run. Empty for every Phase 15 row, so the schema stays compatible;
    #: the keys are written by the RL reward propagator, never inferred here.
    reward_breakdown: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Any] | None = None
    evaluation_id: str = ""
    event_count: int = 0
    redactions: int = 0
    redaction_kinds: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # -- reading ---------------------------------------------------------------

    @property
    def terminal(self) -> bool:
        """Whether the run has ended (a non-terminal row is still being built)."""
        for candidate in TrajectoryStatus:
            if candidate.value == self.status:
                return candidate.terminal
        return False

    @property
    def complete(self) -> bool:
        """Whether this row carries enough to be evaluated as a finished task.

        "Finished" means the run ended AND the two facts an evaluation needs are
        present: what was asked, and how it went. A row missing either is a
        partial capture, not a failure.
        """
        return (
            self.terminal
            and bool(self.task_id or self.user_request)
            and self.success is not None
        )

    @property
    def failed_tools(self) -> tuple[ToolCallRecord, ...]:
        return tuple(call for call in self.tool_calls if call.failed)

    @property
    def succeeded_tools(self) -> tuple[ToolCallRecord, ...]:
        return tuple(call for call in self.tool_calls if call.succeeded)

    @property
    def failed_verifications(self) -> tuple[VerificationRecord, ...]:
        return tuple(record for record in self.verification_results if record.failed)

    @property
    def retries(self) -> int:
        """Total attempts beyond the first, across steps and REPEATED tools.

        Counted per distinct tool, not per call: five calls to one tool are four
        retries, not the fifteen a per-call sum would report.
        """
        extra_steps = sum(max(0, step.attempts - 1) for step in self.execution_steps)
        names = {call.tool for call in self.tool_calls if call.tool}
        extra_tools = sum(max(0, len(self.tool_calls_for(name)) - 1) for name in names)
        return extra_steps + extra_tools

    def tool_calls_for(self, tool: str) -> tuple[ToolCallRecord, ...]:
        return tuple(call for call in self.tool_calls if call.tool == tool)

    def fingerprint(self) -> str:
        """A stable identity for duplicate detection.

        Built from the request plus the SHAPE of what ran — not from ids or
        timestamps, which differ between two captures of the same work — so the
        second capture of one task is recognisable as a duplicate.
        """
        canonical = json.dumps(
            {
                "request": self.user_request.strip().lower(),
                "intent": str(self.structured_intent.get("intent", "")),
                "route": str(self.decision.get("route", "")),
                "tools": [call.tool for call in self.tool_calls],
                "steps": [step.action for step in self.execution_steps],
                "status": self.status,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    # -- writing ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "source": self.source,
            "user_request": self.user_request,
            "context_summary": dict(self.context_summary),
            "structured_intent": dict(self.structured_intent),
            "decision": dict(self.decision),
            "plan": dict(self.plan),
            "execution_steps": [step.to_dict() for step in self.execution_steps],
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "observations": [item.to_dict() for item in self.observations],
            "verification_results": [item.to_dict() for item in self.verification_results],
            "recovery_events": [item.to_dict() for item in self.recovery_events],
            "final_result": dict(self.final_result),
            "status": self.status,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "model_information": dict(self.model_information),
            "latency_metrics": self.latency_metrics.to_dict(),
            "resource_usage": self.resource_usage.to_dict(),
            "user_feedback": self.user_feedback.to_dict() if self.user_feedback else None,
            "reward": dict(self.reward) if self.reward is not None else None,
            "reward_breakdown": dict(self.reward_breakdown),
            "quality": dict(self.quality) if self.quality is not None else None,
            "evaluation_id": self.evaluation_id,
            "event_count": self.event_count,
            "redactions": self.redactions,
            "redaction_kinds": list(self.redaction_kinds),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentTrajectory:
        """Read a stored row; unknown keys are ignored, unusable ones defaulted."""
        status = _text(data.get("status"), TrajectoryStatus.IN_PROGRESS.value)
        if not any(candidate.value == status for candidate in TrajectoryStatus):
            status = TrajectoryStatus.IN_PROGRESS.value
        feedback = data.get("user_feedback")
        reward = data.get("reward")
        quality = data.get("quality")
        version = _number(data.get("schema_version"))
        return cls(
            trajectory_id=_text(data.get("trajectory_id")) or uuid4().hex,
            task_id=_text(data.get("task_id")),
            parent_task_id=_text(data.get("parent_task_id")),
            timestamp=_text(data.get("timestamp")) or now_iso(),
            schema_version=int(version) if version else TRAJECTORY_SCHEMA_VERSION,
            source=_text(data.get("source"), "application"),
            user_request=_text(data.get("user_request")),
            context_summary=_mapping(data.get("context_summary")),
            structured_intent=_mapping(data.get("structured_intent")),
            decision=_mapping(data.get("decision")),
            plan=_mapping(data.get("plan")),
            execution_steps=tuple(
                _records(data.get("execution_steps"), ExecutionStep.from_dict)
            ),
            tool_calls=tuple(_records(data.get("tool_calls"), ToolCallRecord.from_dict)),
            observations=tuple(_records(data.get("observations"), Observation.from_dict)),
            verification_results=tuple(
                _records(data.get("verification_results"), VerificationRecord.from_dict)
            ),
            recovery_events=tuple(
                _records(data.get("recovery_events"), RecoveryRecord.from_dict)
            ),
            final_result=_mapping(data.get("final_result")),
            status=status,
            success=_flag(data.get("success")),
            failure_reason=_text(data.get("failure_reason")),
            model_information=_mapping(data.get("model_information")),
            latency_metrics=LatencyMetrics.from_dict(
                _mapping(data.get("latency_metrics"))
            ),
            resource_usage=ResourceUsage.from_dict(_mapping(data.get("resource_usage"))),
            user_feedback=UserFeedback.from_dict(dict(feedback))
            if isinstance(feedback, Mapping)
            else None,
            reward=dict(reward) if isinstance(reward, Mapping) else None,
            reward_breakdown=_mapping(data.get("reward_breakdown")),
            quality=dict(quality) if isinstance(quality, Mapping) else None,
            evaluation_id=_text(data.get("evaluation_id")),
            event_count=int(_number(data.get("event_count")) or 0),
            redactions=int(_number(data.get("redactions")) or 0),
            redaction_kinds=_texts(data.get("redaction_kinds")),
            metadata=_mapping(data.get("metadata")),
        )

    # -- enriching (a trajectory is immutable; these return a new one) ---------

    def with_quality(self, verdict: Mapping[str, Any]) -> AgentTrajectory:
        return replace(self, quality=dict(verdict))

    def with_reward(self, reward: Mapping[str, Any]) -> AgentTrajectory:
        return replace(self, reward=dict(reward))

    def with_reward_breakdown(self, breakdown: Mapping[str, Any]) -> AgentTrajectory:
        """The same run, plus the RL reading of its rewards (Phase 18)."""
        return replace(self, reward_breakdown=dict(breakdown))

    def with_feedback(self, feedback: UserFeedback) -> AgentTrajectory:
        return replace(self, user_feedback=feedback)

    def with_metadata(self, **extra: Any) -> AgentTrajectory:
        merged = {**self.metadata, **extra}
        return replace(self, metadata=merged)


def json_safe_trajectory(trajectory: AgentTrajectory) -> dict[str, Any]:
    """The trajectory as a JSON-safe mapping — what every store writes."""
    safe = json_safe(trajectory.to_dict())
    if not isinstance(safe, Mapping):
        raise TypeError("a trajectory must serialise to a mapping")
    return {str(key): value for key, value in safe.items()}


def parse_trajectories(rows: Sequence[Mapping[str, Any]]) -> tuple[AgentTrajectory, ...]:
    """Parse stored rows, skipping any row that cannot be read."""
    parsed: list[AgentTrajectory] = []
    for row in rows:
        try:
            parsed.append(AgentTrajectory.from_dict(dict(row)))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "GOLDEN_DATASET_SCHEMA_VERSION",
    "REWARD_SCHEMA_VERSION",
    "TRAJECTORY_SCHEMA_VERSION",
    "AgentTrajectory",
    "DimensionStatus",
    "EvaluationDimension",
    "ExecutionStep",
    "LatencyMetrics",
    "Observation",
    "OverallStatus",
    "QualityVerdictValue",
    "RecoveryRecord",
    "ResourceUsage",
    "ToolCallRecord",
    "TrajectoryStatus",
    "UserFeedback",
    "VerificationRecord",
    "json_safe_trajectory",
    "now_iso",
    "parse_trajectories",
]
