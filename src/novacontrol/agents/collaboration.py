"""Multi-agent collaboration (Phase 28).

This module is the coordination layer, not a second agent framework. Every
subtask is handed to an agent that is already in the ordinary
:class:`~novacontrol.agents.registry.AgentRegistry`; a Phase 12 specialist run
therefore still walks the one shared ``SpecialistPipeline`` (NLU → Context →
Decision → Planner → Tool Selection → Execution → Verification → Recovery).
The collaboration layer adds only what is genuinely missing above that:

* deciding whether one objective benefits from more than one agent,
* decomposing it into subtasks with explicit dependencies and expected results,
* assigning each subtask to a registered agent whose declared role fits,
* running independent subtasks concurrently under a bounded semaphore,
* passing each subtask only the upstream results it declared it needs,
* detecting and resolving contradictory claims from actual evidence,
* aggregating only outputs whose own verification passed, and
* persisting the task so a restart preserves completed work.

Three rules are load-bearing:

1. **A response is not a verification.** ``AgentResponse.status`` says the
   agent finished; the run's own ``verified`` flag (from its verification stage)
   is what this layer trusts. A completed but unverified subtask can never
   produce a verified collaboration.
2. **Parallel is measured, not asserted.** Independent subtasks are gathered
   with ``asyncio.gather`` and the task records wall time and summed subtask
   time, so the report can show the difference rather than claiming concurrency.
3. **Conflicts are surfaced.** Where evidence cannot settle a disagreement, the
   conflict is reported unresolved and the collaboration is not verified.

The engine never edits a permission, widens a risk decision, or retries a
non-idempotent subtask. ``authorize=True`` builds a per-run authorized pipeline
via the injected factory, exactly the convention the specialist agents already
use; the build's shared pipeline is untouched.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from novacontrol.agents.bus import AgentMessageBus
from novacontrol.agents.collaboration_controls import (
    HardwareSchedule,
    ProviderCapabilities,
    ProviderPreflight,
    ProviderPreflightChecker,
    TokenLedger,
    backoff_decision,
    schedule_hardware,
)
from novacontrol.agents.models import (
    AgentMessage,
    AgentResponse,
    AgentRole,
    AgentTask,
    AgentTaskStatus,
)
from novacontrol.agents.registry import AgentRegistry, RunnableAgent
from novacontrol.core.events import EventBus, EventType

__all__ = [
    "COLLABORATION_SCHEMA_VERSION",
    "CollaborationAssessment",
    "CollaborationEngine",
    "CollaborationMode",
    "CollaborationPlan",
    "CollaborationPolicy",
    "CollaborationStatus",
    "CollaborationSubtask",
    "CollaborationTask",
    "ConflictRecord",
    "HardwareSchedule",
    "ProviderCapabilities",
    "ProviderPreflight",
    "SubtaskResult",
    "SubtaskStatus",
    "TokenLedger",
]

COLLABORATION_SCHEMA_VERSION = "phase28.1"

#: The objective is split only when it actually names separate work. These are
#: the markers the planner treats as evidence of that, kept narrow on purpose:
#: "and" is not one of them, because "open chrome and search" is one action.
_COLLABORATION_MARKERS = re.compile(
    r"\b(?:collaborat\w*|multiple agents|another agent|use agents|delegate|"
    r"independently|cross[- ]?check|in parallel|parallel\w*|split (?:the |this )?(?:work|task)|"
    r"each (?:column|file|dataset)|analy[sz]e each|verify (?:the )?(?:findings|results|claims|analysis|implementation)|"
    r"(?:research|investigate)\b.*\b(?:and|then)\b.*\b(?:verify|check|validate))\b",
    re.IGNORECASE,
)
_RESEARCH_WORDS = re.compile(
    r"\b(?:research|investigate|find sources|look up|compare sources|literature|evidence)\b",
    re.IGNORECASE,
)
_CODING_WORDS = re.compile(
    r"\b(?:implement|code|coding|refactor|fix|bug|patch|feature|repository|project)\b",
    re.IGNORECASE,
)
_TEST_WORDS = re.compile(r"\b(?:test|tests|pytest|verify|verification|validate|review)\b", re.IGNORECASE)
_ANALYSIS_WORDS = re.compile(r"\b(?:analy[sz]e|analysis|dataset|csv|statistics|column|mean|average|total)\b", re.IGNORECASE)
_DESTRUCTIVE_WORDS = re.compile(
    r"\b(?:delete|remove|erase|format|drop|truncate|overwrite|send|publish|purchase|pay|deploy|uninstall)\b",
    re.IGNORECASE,
)
_SIMPLE_WORDS = re.compile(
    r"^(?:open|launch|close|what time|what is|who is|calculate|compute|convert|screenshot|"
    r"take a screenshot|show|list files)\b",
    re.IGNORECASE,
)
_CSV_PATH = re.compile(r"""["']?([^\s"']+\.csv)["']?""", re.IGNORECASE)
_COLUMN_HINT = re.compile(
    r"\bcolumns?\s+(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][\w]*))", re.IGNORECASE
)

#: How much of an upstream result a dependent subtask may see. A dependent gets
#: the result, its claims and its verification — never the whole task history.
_MAX_UPSTREAM_CONTENT = 6000
_MAX_CLAIMS = 120
_MAX_CONFLICTS = 30


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    """A bounded JSON-safe copy: persistence must never meet a live object."""
    if value is None or isinstance(value, str | int | float | bool):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in list(value.items())[:200]}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in list(value)[:500]]
    return str(value)


class CollaborationMode(StrEnum):
    SINGLE = "single"
    RESEARCH = "research"
    CODING = "coding"
    DATA_ANALYSIS = "data_analysis"


class CollaborationStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    DECLINED = "declined"


class SubtaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self not in {SubtaskStatus.PENDING, SubtaskStatus.RUNNING}

    @property
    def successful(self) -> bool:
        return self is SubtaskStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class CollaborationSubtask:
    """One independently assignable unit, with its dependencies declared."""

    id: str
    title: str
    goal: str
    role: AgentRole
    depends_on: tuple[str, ...] = ()
    expected: str = ""
    required: bool = True
    retryable: bool = False
    timeout_seconds: float | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    destructive: bool = False

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("A collaboration subtask needs an id.")
        if not str(self.goal).strip():
            raise ValueError(f"Subtask {self.id} needs a goal.")
        if self.timeout_seconds is not None and float(self.timeout_seconds) <= 0:
            raise ValueError(f"Subtask {self.id} has a non-positive timeout.")
        if self.id in self.depends_on:
            raise ValueError(f"Subtask {self.id} cannot depend on itself.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "role": self.role.value,
            "depends_on": list(self.depends_on),
            "expected": self.expected,
            "required": self.required,
            "retryable": self.retryable,
            "timeout_seconds": self.timeout_seconds,
            "context": _jsonable(dict(self.context)),
            "metadata": _jsonable(dict(self.metadata)),
            "destructive": self.destructive,
        }


@dataclass(frozen=True, slots=True)
class CollaborationAssessment:
    """Whether an objective should be split, and why."""

    beneficial: bool
    mode: CollaborationMode
    rationale: str
    blocked_reason: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "beneficial": self.beneficial,
            "mode": self.mode.value,
            "rationale": self.rationale,
            "blocked_reason": self.blocked_reason,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class CollaborationPlan:
    """The decomposition, validated as a DAG before anything runs."""

    id: str
    objective: str
    mode: CollaborationMode
    rationale: str
    subtasks: tuple[CollaborationSubtask, ...] = ()
    beneficial: bool = True
    created_at: str = field(default_factory=_timestamp)
    requires_approval: bool = False
    blocked_reason: str = ""
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [subtask.id for subtask in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Collaboration subtask ids must be unique.")
        known = set(ids)
        for subtask in self.subtasks:
            missing = [dep for dep in subtask.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"Subtask {subtask.id} depends on unknown subtask(s): {', '.join(missing)}"
                )
        _assert_acyclic(self.subtasks)

    @property
    def parallel_width(self) -> int:
        """How many subtasks may start together at the widest point."""
        depth = _critical_path_depth(self.subtasks)
        # Every subtask not on one chain can share a wave; this is a bounded,
        # honest estimate used for status, not a scheduling promise.
        return max(1, min(len(self.subtasks), len(self.subtasks) - depth + 1))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "mode": self.mode.value,
            "rationale": self.rationale,
            "beneficial": self.beneficial,
            "requires_approval": self.requires_approval,
            "blocked_reason": self.blocked_reason,
            "limitations": list(self.limitations),
            "parallel_width": self.parallel_width,
            "created_at": self.created_at,
            "subtasks": [subtask.to_dict() for subtask in self.subtasks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CollaborationPlan":
        subtasks = tuple(
            CollaborationSubtask(
                id=str(item["id"]),
                title=str(item.get("title", "")),
                goal=str(item["goal"]),
                role=AgentRole(str(item.get("role", AgentRole.PLANNING.value))),
                depends_on=tuple(str(dep) for dep in item.get("depends_on", ())),
                expected=str(item.get("expected", "")),
                required=bool(item.get("required", True)),
                retryable=bool(item.get("retryable", False)),
                timeout_seconds=(
                    float(item["timeout_seconds"])
                    if item.get("timeout_seconds") is not None
                    else None
                ),
                context=dict(item.get("context", {})),
                metadata=dict(item.get("metadata", {})),
                destructive=bool(item.get("destructive", False)),
            )
            for item in data.get("subtasks", ())
            if isinstance(item, Mapping) and item.get("id") and item.get("goal")
        )
        return cls(
            id=str(data.get("id") or f"plan:{uuid4().hex[:12]}"),
            objective=str(data.get("objective", "")),
            mode=CollaborationMode(str(data.get("mode", CollaborationMode.SINGLE.value))),
            rationale=str(data.get("rationale", "")),
            subtasks=subtasks,
            beneficial=bool(data.get("beneficial", True)),
            created_at=str(data.get("created_at", "") or _timestamp()),
            requires_approval=bool(data.get("requires_approval", False)),
            blocked_reason=str(data.get("blocked_reason", "")),
            limitations=tuple(str(item) for item in data.get("limitations", ())),
        )


def _assert_acyclic(subtasks: Sequence[CollaborationSubtask]) -> None:
    by_id = {subtask.id: subtask for subtask in subtasks}
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        mark = state.get(node, 0)
        if mark == 1:
            raise ValueError(f"Collaboration subtasks contain a dependency cycle at {node}.")
        if mark == 2:
            return
        state[node] = 1
        for dep in by_id[node].depends_on:
            visit(dep)
        state[node] = 2

    for subtask in subtasks:
        visit(subtask.id)


def _critical_path_depth(subtasks: Sequence[CollaborationSubtask]) -> int:
    by_id = {subtask.id: subtask for subtask in subtasks}
    memo: dict[str, int] = {}

    def depth(node: str) -> int:
        if node in memo:
            return memo[node]
        deps = by_id[node].depends_on
        memo[node] = 1 if not deps else 1 + max(depth(dep) for dep in deps)
        return memo[node]

    return max((depth(subtask.id) for subtask in subtasks), default=0)


@dataclass(slots=True)
class ConflictRecord:
    """Two results saying incompatible things about the same topic."""

    kind: str
    topic: str
    left: str
    right: str
    left_agent: str = ""
    right_agent: str = ""
    resolution: str = ""
    resolved: bool = False
    reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "topic": self.topic,
            "left": self.left,
            "right": self.right,
            "left_agent": self.left_agent,
            "right_agent": self.right_agent,
            "resolution": self.resolution,
            "resolved": self.resolved,
            "reason": self.reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConflictRecord":
        return cls(
            kind=str(data.get("kind", "unknown")),
            topic=str(data.get("topic", "")),
            left=str(data.get("left", "")),
            right=str(data.get("right", "")),
            left_agent=str(data.get("left_agent", "")),
            right_agent=str(data.get("right_agent", "")),
            resolution=str(data.get("resolution", "")),
            resolved=bool(data.get("resolved", False)),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.0) or 0.0),
        )


@dataclass(slots=True)
class SubtaskResult:
    """What one agent actually returned, and whether its own run verified it."""

    subtask_id: str
    agent: str
    role: AgentRole
    status: SubtaskStatus
    content: str = ""
    verified: bool = False
    verification: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    claims: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    attempts: int = 1
    retries: int = 0
    duration_ms: float = 0.0
    started_at: str = field(default_factory=_timestamp)
    finished_at: str = field(default_factory=_timestamp)
    output: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        """The context a declared dependent may see, and nothing else."""
        return {
            "subtask_id": self.subtask_id,
            "agent": self.agent,
            "status": self.status.value,
            "content": self.content[:_MAX_UPSTREAM_CONTENT],
            "verified": self.verified,
            "verification": _jsonable(self.verification),
            "claims": [_jsonable(claim) for claim in self.claims[:_MAX_CLAIMS]],
            "evidence": _jsonable(self.evidence),
            "usage": _jsonable(self.usage),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "agent": self.agent,
            "role": self.role.value,
            "status": self.status.value,
            "content": self.content,
            "verified": self.verified,
            "verification": _jsonable(self.verification),
            "evidence": _jsonable(self.evidence),
            "claims": [_jsonable(claim) for claim in self.claims],
            "usage": _jsonable(self.usage),
            "error": self.error,
            "attempts": self.attempts,
            "retries": self.retries,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": _jsonable(self.output),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SubtaskResult":
        try:
            role = AgentRole(str(data.get("role", AgentRole.PLANNING.value)))
        except ValueError:
            role = AgentRole.PLANNING
        try:
            status = SubtaskStatus(str(data.get("status", SubtaskStatus.PENDING.value)))
        except ValueError:
            status = SubtaskStatus.FAILED
        return cls(
            subtask_id=str(data.get("subtask_id", "")),
            agent=str(data.get("agent", "")),
            role=role,
            status=status,
            content=str(data.get("content", "")),
            verified=bool(data.get("verified", False)),
            verification=dict(data.get("verification", {}) or {}),
            evidence=dict(data.get("evidence", {}) or {}),
            claims=tuple(
                dict(item) for item in data.get("claims", ()) if isinstance(item, Mapping)
            ),
            usage=dict(data.get("usage", {}) or {}),
            error=str(data.get("error", "")),
            attempts=max(1, int(data.get("attempts", 1) or 1)),
            retries=max(0, int(data.get("retries", 0) or 0)),
            duration_ms=max(0.0, float(data.get("duration_ms", 0.0) or 0.0)),
            started_at=str(data.get("started_at", "") or _timestamp()),
            finished_at=str(data.get("finished_at", "") or _timestamp()),
            output=dict(data.get("output", {}) or {}),
        )


@dataclass(slots=True)
class CollaborationTask:
    """One persisted collaboration: plan, results, messages and verdict."""

    id: str
    objective: str
    mode: CollaborationMode
    plan: CollaborationPlan
    context: dict[str, Any] = field(default_factory=dict)
    status: CollaborationStatus = CollaborationStatus.PLANNED
    results: dict[str, SubtaskResult] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    cancelled: bool = False
    idempotency_key: str = ""
    source: str = "api"
    learning: dict[str, Any] = field(default_factory=dict)
    provider_preflight: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    scheduling: dict[str, Any] = field(default_factory=dict)
    backoff_events: list[dict[str, Any]] = field(default_factory=list)

    def result_list(self) -> list[SubtaskResult]:
        return [self.results[s.id] for s in self.plan.subtasks if s.id in self.results]

    def to_dict(self, *, include_plan: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.id,
            "objective": self.objective,
            "mode": self.mode.value,
            "status": self.status.value,
            "cancelled": self.cancelled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "context": _jsonable(self.context),
            "results": [result.to_dict() for result in self.result_list()],
            "messages": [_jsonable(message) for message in self.messages],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "aggregate": _jsonable(self.aggregate),
            "learning": _jsonable(self.learning),
            "provider_preflight": _jsonable(self.provider_preflight),
            "token_usage": _jsonable(self.token_usage),
            "scheduling": _jsonable(self.scheduling),
            "backoff_events": _jsonable(self.backoff_events),
        }
        if include_plan:
            payload["plan"] = self.plan.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CollaborationTask":
        plan_data = data.get("plan")
        if not isinstance(plan_data, Mapping):
            raise ValueError("a stored collaboration task has no plan")
        plan = CollaborationPlan.from_dict(plan_data)
        results: dict[str, SubtaskResult] = {}
        for item in data.get("results", ()):
            if not isinstance(item, Mapping):
                continue
            result = SubtaskResult.from_dict(item)
            if result.subtask_id:
                results[result.subtask_id] = result
        try:
            status = CollaborationStatus(str(data.get("status", CollaborationStatus.INTERRUPTED.value)))
        except ValueError:
            status = CollaborationStatus.INTERRUPTED
        try:
            mode = CollaborationMode(str(data.get("mode", plan.mode.value)))
        except ValueError:
            mode = plan.mode
        conflicts = [
            ConflictRecord.from_dict(item)
            for item in data.get("conflicts", ())
            if isinstance(item, Mapping)
        ]
        return cls(
            id=str(data.get("task_id") or data.get("id") or ""),
            objective=str(data.get("objective", plan.objective)),
            mode=mode,
            plan=plan,
            context=dict(data.get("context", {}) or {}),
            status=status,
            results=results,
            messages=[
                dict(item) for item in data.get("messages", ()) if isinstance(item, Mapping)
            ],
            conflicts=conflicts,
            aggregate=dict(data.get("aggregate", {}) or {}),
            created_at=str(data.get("created_at", "") or _timestamp()),
            updated_at=str(data.get("updated_at", "") or _timestamp()),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            error=str(data.get("error", "")),
            cancelled=bool(data.get("cancelled", False)),
            idempotency_key=str(data.get("idempotency_key", "")),
            source=str(data.get("source", "api")),
            learning=dict(data.get("learning", {}) or {}),
            provider_preflight=[
                dict(item)
                for item in data.get("provider_preflight", ())
                if isinstance(item, Mapping)
            ],
            token_usage=dict(data.get("token_usage", {}) or {}),
            scheduling=dict(data.get("scheduling", {}) or {}),
            backoff_events=[
                dict(item)
                for item in data.get("backoff_events", ())
                if isinstance(item, Mapping)
            ],
        )


@dataclass(frozen=True, slots=True)
class CollaborationPolicy:
    """Ceilings and switches for collaboration on this machine."""

    enabled: bool = True
    persist: bool = True
    capture_learning: bool = True
    max_concurrency: int = 3
    max_subtasks: int = 6
    max_tasks: int = 50
    max_retries: int = 1
    default_timeout_seconds: float = 120.0
    rate_limit_backoff_seconds: float = 0.5
    rate_limit_backoff_max_seconds: float = 5.0
    token_budget: int = 0
    hardware_aware: bool = True
    sequential_fallback: bool = True
    allow_destructive: bool = False

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1.")
        if self.max_subtasks < 1:
            raise ValueError("max_subtasks must be at least 1.")
        if self.max_tasks < 1:
            raise ValueError("max_tasks must be at least 1.")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative.")
        if self.default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive.")
        if self.rate_limit_backoff_seconds < 0:
            raise ValueError("rate_limit_backoff_seconds cannot be negative.")
        if self.rate_limit_backoff_max_seconds < 0:
            raise ValueError("rate_limit_backoff_max_seconds cannot be negative.")
        if self.token_budget < 0:
            raise ValueError("token_budget cannot be negative.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "persist": self.persist,
            "capture_learning": self.capture_learning,
            "max_concurrency": self.max_concurrency,
            "max_subtasks": self.max_subtasks,
            "max_tasks": self.max_tasks,
            "max_retries": self.max_retries,
            "default_timeout_seconds": self.default_timeout_seconds,
            "rate_limit_backoff_seconds": self.rate_limit_backoff_seconds,
            "rate_limit_backoff_max_seconds": self.rate_limit_backoff_max_seconds,
            "token_budget": self.token_budget,
            "hardware_aware": self.hardware_aware,
            "sequential_fallback": self.sequential_fallback,
            "allow_destructive": self.allow_destructive,
        }


def _implementation_goal(objective: str) -> str:
    """Keep the change request and give the test clause to the verifier.

    The phrase is deliberately explicit: ``DeveloperAgent`` routes "apply the
    change" to its write path, whereas an objective that still contains "run
    the tests" routes to its test path first and writes nothing.
    """

    text = str(objective).strip()
    text = re.sub(
        r"\b(?:and\s+then|then|and)\s+(?:run|rerun|execute)\b[^.]*"
        r"\b(?:tests?|test suite)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:and\s+then|then|and)\s+(?:verify|check|validate)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:run|rerun|execute)\s+(?:the\s+)?(?:tests?|test suite)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" ,.;")
    return f"apply the change: {text}" if text else objective


class CollaborationPlanner:
    """Deterministic decomposition over the existing agent roles.

    No model is required and none is consulted: the decision is a structural
    reading of the objective plus the registered roles. That keeps ``simple
    tasks never split`` testable and reproducible.
    """

    def assess(
        self,
        objective: str,
        *,
        context: Mapping[str, Any] | None = None,
        registry: AgentRegistry | None = None,
        policy: CollaborationPolicy | None = None,
    ) -> CollaborationAssessment:
        text = str(objective or "").strip()
        policy = policy or CollaborationPolicy()
        limitations: list[str] = []
        if not text:
            return CollaborationAssessment(
                beneficial=False,
                mode=CollaborationMode.SINGLE,
                rationale="No objective was supplied.",
                blocked_reason="an objective is required",
            )
        if _DESTRUCTIVE_WORDS.search(text) and not policy.allow_destructive:
            return CollaborationAssessment(
                beneficial=False,
                mode=CollaborationMode.SINGLE,
                rationale=(
                    "The objective names a destructive or externally visible action; "
                    "multi-agent delegation is not offered for it."
                ),
                blocked_reason=(
                    "destructive or externally visible objectives are refused by the "
                    "collaboration layer and must use the existing single-action approval flow"
                ),
                limitations=("destructive actions keep their existing authorization path",),
            )
        words = text.split()
        context = context or {}
        has_path = bool(_dataset_path(text, context))
        explicit = bool(_COLLABORATION_MARKERS.search(text))
        research = bool(_RESEARCH_WORDS.search(text))
        coding = bool(_CODING_WORDS.search(text))
        testing = bool(_TEST_WORDS.search(text))
        analysis = bool(_ANALYSIS_WORDS.search(text)) or has_path
        columns = _dataset_columns(text, context)

        if analysis and (has_path or columns or "dataset" in text.lower()):
            if len(columns) >= 2 or explicit or re.search(r"\bparallel|each column|independently\b", text, re.I):
                mode = CollaborationMode.DATA_ANALYSIS
                beneficial = len(columns) >= 2 or explicit
                rationale = (
                    f"{len(columns)} independent dataset column(s) can be analysed "
                    "concurrently and checked by a separate verification agent."
                    if columns
                    else "Independent dataset analysis was explicitly requested."
                )
            else:
                mode = CollaborationMode.SINGLE
                beneficial = False
                rationale = "One dataset column is a single analysis, not a collaboration."
        elif coding and testing and (explicit or re.search(r"\b(?:and|then)\b", text, re.I)):
            mode = CollaborationMode.CODING
            beneficial = True
            rationale = (
                "Implementation and verification are separate responsibilities; the "
                "verifier runs the project's own tests rather than trusting the coder."
            )
        elif research and explicit:
            mode = CollaborationMode.RESEARCH
            beneficial = True
            rationale = (
                "Research and independent claim verification are separate workstreams."
            )
        elif explicit and (research or coding or analysis):
            if research:
                mode = CollaborationMode.RESEARCH
            elif coding:
                mode = CollaborationMode.CODING
            else:
                mode = CollaborationMode.DATA_ANALYSIS
            beneficial = True
            rationale = "The objective explicitly asks for independent or verified agent work."
        else:
            mode = CollaborationMode.SINGLE
            beneficial = False
            rationale = (
                "This reads as one action; the existing single-agent path is the honest "
                "choice and is left unchanged."
            )

        if beneficial and registry is not None:
            for role in _roles_for_mode(mode):
                if not registry.by_role(role):
                    limitations.append(f"no registered agent fills the {role.value} role")
            if limitations:
                beneficial = False
                rationale += " " + "; ".join(limitations) + "."
        if len(words) < 4 and not explicit:
            beneficial = False
            mode = CollaborationMode.SINGLE
            rationale = "Too short to name separate work; the existing single-agent path is used."
        return CollaborationAssessment(
            beneficial=beneficial,
            mode=mode,
            rationale=rationale,
            limitations=tuple(limitations),
        )

    def plan(
        self,
        objective: str,
        *,
        context: Mapping[str, Any] | None = None,
        registry: AgentRegistry | None = None,
        policy: CollaborationPolicy | None = None,
        force: bool = False,
    ) -> CollaborationPlan:
        policy = policy or CollaborationPolicy()
        context = dict(context or {})
        assessment = self.assess(
            objective, context=context, registry=registry, policy=policy
        )
        if assessment.blocked_reason:
            return CollaborationPlan(
                id=f"plan:{uuid4().hex[:12]}",
                objective=str(objective or "").strip(),
                mode=CollaborationMode.SINGLE,
                rationale=assessment.rationale,
                subtasks=(),
                beneficial=False,
                blocked_reason=assessment.blocked_reason,
                limitations=assessment.limitations,
            )
        if not assessment.beneficial and not force:
            return CollaborationPlan(
                id=f"plan:{uuid4().hex[:12]}",
                objective=str(objective or "").strip(),
                mode=CollaborationMode.SINGLE,
                rationale=assessment.rationale,
                subtasks=(),
                beneficial=False,
                limitations=assessment.limitations,
            )
        mode = assessment.mode
        if mode is CollaborationMode.SINGLE:
            mode = self._infer_forced_mode(objective, context)
        if mode is CollaborationMode.DATA_ANALYSIS:
            subtasks, requires_approval, limits = self._data_plan(
                objective, context, policy
            )
        elif mode is CollaborationMode.CODING:
            subtasks, requires_approval, limits = self._coding_plan(objective, policy)
        else:
            subtasks, requires_approval, limits = self._research_plan(objective, policy)
        limitations = tuple(dict.fromkeys([*assessment.limitations, *limits]))
        return CollaborationPlan(
            id=f"plan:{uuid4().hex[:12]}",
            objective=str(objective).strip(),
            mode=mode,
            rationale=assessment.rationale,
            subtasks=subtasks,
            beneficial=bool(subtasks),
            requires_approval=requires_approval,
            limitations=limitations,
        )

    # -- templates -----------------------------------------------------------

    def _research_plan(
        self, objective: str, policy: CollaborationPolicy
    ) -> tuple[tuple[CollaborationSubtask, ...], bool, tuple[str, ...]]:
        subtasks = (
            CollaborationSubtask(
                id="t1",
                title="Gather cited findings",
                goal=objective,
                role=AgentRole.RESEARCH,
                expected="Claims with sources and a verification verdict",
                retryable=True,
                context={"question": objective},
                metadata={
                    "provider_requirements": {
                        "required_capabilities": ["text_generation"],
                        "require_provider": True,
                    }
                },
            ),
            CollaborationSubtask(
                id="t2",
                title="Independently verify claims and citations",
                goal="verify the research claims and citations independently",
                role=AgentRole.VERIFICATION,
                depends_on=("t1",),
                expected="Every evidence claim resolves to a real source and conflicts are named",
                retryable=True,
                context={"verification": "claims"},
                metadata={"verification": "claims"},
            ),
        )
        return subtasks[: policy.max_subtasks], False, ()

    def _coding_plan(
        self, objective: str, policy: CollaborationPolicy
    ) -> tuple[tuple[CollaborationSubtask, ...], bool, tuple[str, ...]]:
        implementation_goal = _implementation_goal(objective)
        subtasks: tuple[CollaborationSubtask, ...] = (
            CollaborationSubtask(
                id="t1",
                title="Implement the requested change",
                # The coder must receive the change request, not the whole
                # sentence that also says "run the tests". DeveloperAgent routes
                # "run ... tests" before "implement", so leaving the clause in
                # made the coder run the suite and write nothing while the task
                # still reported success. The verifier owns the test clause.
                goal=implementation_goal,
                role=AgentRole.CODING,
                expected=(
                    "The change is applied and the file on disk matches the "
                    "proposal, or the coder reports why it could not apply it"
                ),
                retryable=False,
                context={"task": objective, "change_request": implementation_goal},
            ),
            CollaborationSubtask(
                id="t2",
                title="Run the project's tests independently",
                goal="run the project's test suite and verify the implementation",
                role=AgentRole.VERIFICATION,
                depends_on=("t1",),
                expected="The test suite ran and its actual result is reported",
                retryable=True,
                context={"verification": "tests"},
                metadata={"verification": "tests"},
            ),
        )
        limits: list[str] = []
        if len(subtasks) > policy.max_subtasks:
            subtasks = subtasks[: policy.max_subtasks]
            limits.append("the plan was capped at max_subtasks")
        return subtasks, True, tuple(limits)

    def _data_plan(
        self,
        objective: str,
        context: Mapping[str, Any],
        policy: CollaborationPolicy,
    ) -> tuple[tuple[CollaborationSubtask, ...], bool, tuple[str, ...]]:
        path = _dataset_path(objective, context)
        columns = _dataset_columns(objective, context, path=path)
        limits: list[str] = []
        if not path:
            limits.append("no CSV dataset path was supplied; analysis will report the missing input")
            return (
                (
                    CollaborationSubtask(
                        id="t1",
                        title="Analyse the dataset",
                        goal=objective,
                        role=AgentRole.DATA_ANALYSIS,
                        expected="Per-column statistics from the named CSV",
                        retryable=True,
                        context=dict(context),
                    ),
                ),
                False,
                tuple(limits),
            )
        if not columns:
            columns = _columns_from_csv(path)
        columns = columns[: max(1, policy.max_subtasks - 1)]
        if len(columns) > policy.max_subtasks - 1:
            limits.append("the column list was capped so one verification slot remains")
        subtasks: list[CollaborationSubtask] = []
        for index, column in enumerate(columns, start=1):
            subtasks.append(
                CollaborationSubtask(
                    id=f"t{index}",
                    title=f"Analyse column {column!r}",
                    goal=f"analyze column {column!r} in {path}",
                    role=AgentRole.DATA_ANALYSIS,
                    expected=f"Verified statistics for column {column!r}",
                    retryable=True,
                    context={"dataset_path": str(path), "column": column, **dict(context)},
                    metadata={"dataset": str(path), "column": column},
                )
            )
        if not subtasks:
            subtasks.append(
                CollaborationSubtask(
                    id="t1",
                    title="Analyse the dataset",
                    goal=objective,
                    role=AgentRole.DATA_ANALYSIS,
                    expected="Per-column statistics from the named CSV",
                    retryable=True,
                    context={"dataset_path": str(path), "column": ""},
                    metadata={"dataset": str(path)},
                )
            )
        verification_id = f"t{len(subtasks) + 1}"
        subtasks.append(
            CollaborationSubtask(
                id=verification_id,
                title="Recompute the reported statistics independently",
                goal="verify the data analysis findings independently",
                role=AgentRole.VERIFICATION,
                depends_on=tuple(subtask.id for subtask in subtasks),
                expected="Every reported metric matches an independent recomputation",
                retryable=True,
                context={"verification": "metrics"},
                metadata={"verification": "metrics"},
            )
        )
        return tuple(subtasks[: policy.max_subtasks]), False, tuple(limits)

    @staticmethod
    def _infer_forced_mode(
        objective: str, context: Mapping[str, Any]
    ) -> CollaborationMode:
        if _dataset_path(objective, context) or _ANALYSIS_WORDS.search(objective):
            return CollaborationMode.DATA_ANALYSIS
        if _CODING_WORDS.search(objective):
            return CollaborationMode.CODING
        return CollaborationMode.RESEARCH


def _roles_for_mode(mode: CollaborationMode) -> tuple[AgentRole, ...]:
    if mode is CollaborationMode.DATA_ANALYSIS:
        return (AgentRole.DATA_ANALYSIS, AgentRole.VERIFICATION)
    if mode is CollaborationMode.CODING:
        return (AgentRole.CODING, AgentRole.VERIFICATION)
    if mode is CollaborationMode.RESEARCH:
        return (AgentRole.RESEARCH, AgentRole.VERIFICATION)
    return ()


def _dataset_path(objective: str, context: Mapping[str, Any]) -> str:
    supplied = str(context.get("dataset_path") or context.get("path") or "").strip()
    if supplied:
        return supplied
    match = _CSV_PATH.search(str(objective))
    return match.group(1) if match else ""


def _dataset_columns(
    objective: str, context: Mapping[str, Any], *, path: str = ""
) -> list[str]:
    supplied = context.get("columns")
    if isinstance(supplied, str):
        columns = [item.strip() for item in supplied.split(",") if item.strip()]
    elif isinstance(supplied, Sequence):
        columns = [str(item).strip() for item in supplied if str(item).strip()]
    else:
        columns = []
    if not columns:
        match = _COLUMN_HINT.search(str(objective))
        if match:
            phrase = next((group for group in match.groups() if group), "").strip()
            columns = [item.strip() for item in re.split(r",| and ", phrase) if item.strip()]
    if path:
        # The planner may read the header: it is a structural reading of the
        # task, not analysis, and it is what makes independent per-column
        # subtasks possible without asking a model. A parsed phrase only counts
        # when it names a real header — "each column in data.csv" must not turn
        # "in data" into a column name.
        header = _columns_from_csv(path)
        if header:
            columns = [column for column in columns if column in header]
            if not columns:
                columns = header
        elif not columns:
            columns = []
    return list(dict.fromkeys(columns))


def _columns_from_csv(path: str) -> list[str]:
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [str(name) for name in (reader.fieldnames or ()) if str(name).strip()]
    except (OSError, UnicodeError, csv.Error):
        return []


class _LearningSink(Protocol):
    def capture(self, *args: Any, **kwargs: Any) -> Any: ...

    def record(self, evidence: Any, *, assess: bool = False) -> Any: ...

    def experiences(
        self,
        *,
        task_kind: str = "",
        outcome: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> Any: ...


def _agent_availability(agent: RunnableAgent) -> tuple[bool, str, dict[str, Any]]:
    """Read an agent's live availability without inventing one.

    Registration proves an object exists, not that its required local resource
    exists. A custom agent may expose ``availability()`` or a boolean
    ``available`` attribute; anything else is treated as available, which is
    the registry's historical contract.
    """
    probe = getattr(agent, "availability", None)
    if callable(probe):
        try:
            report = probe()
        except Exception as exc:
            return False, f"availability probe failed: {type(exc).__name__}: {exc}", {}
        if isinstance(report, Mapping):
            return (
                bool(report.get("available", True)),
                str(report.get("reason", "")),
                dict(report),
            )
        return bool(report), "", {}
    available = bool(getattr(agent, "available", True))
    return available, "" if available else "agent reports unavailable", {"available": available}


class CollaborationEngine:
    """Plans, schedules and judges multi-agent work over an ``AgentRegistry``."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        planner: CollaborationPlanner | None = None,
        policy: CollaborationPolicy | None = None,
        message_bus: AgentMessageBus | None = None,
        event_bus: EventBus | None = None,
        store: Any | None = None,
        learning: _LearningSink | None = None,
        authorize_pipeline: Callable[[], Any] | None = None,
        provider: object | None = None,
        provider_getter: Callable[[], object | None] | None = None,
        hardware_governor: object | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        source: str = "application",
        model_assigner: (
            Callable[[AgentRole, str, Mapping[str, Any]], Mapping[str, Any] | None]
            | None
        ) = None,
    ) -> None:
        self.registry = registry
        self.planner = planner or CollaborationPlanner()
        self.policy = policy or CollaborationPolicy()
        self.bus = message_bus or AgentMessageBus()
        self._event_bus = event_bus
        self._store = store
        self._learning = learning
        self._authorize_pipeline = authorize_pipeline
        self.provider_preflight = ProviderPreflightChecker(
            provider, provider_getter=provider_getter
        )
        self._hardware_governor = hardware_governor
        self._sleep = sleep or asyncio.sleep
        self._ledgers: dict[str, TokenLedger] = {}
        self._last_schedule: HardwareSchedule | None = None
        # Phase 33: optional per-role model assignment. It only annotates the
        # plan with the router's evidence; authorization and execution stay
        # exactly where they were.
        self._model_assigner = model_assigner
        self._source = source
        self._tasks: dict[str, CollaborationTask] = {}
        self._idempotency: dict[str, str] = {}
        self._running: dict[str, asyncio.Task[SubtaskResult]] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self.restore_report: dict[str, Any] = {}
        self._errors: list[str] = []

    # -- public surface ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    def plan(
        self,
        objective: str,
        *,
        context: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> CollaborationPlan:
        plan = self.planner.plan(
            objective,
            context=context,
            registry=self.registry,
            policy=self.policy,
            force=force,
        )
        return self._with_model_assignments(plan, context or {})

    def _with_model_assignments(
        self, plan: CollaborationPlan, context: Mapping[str, Any]
    ) -> CollaborationPlan:
        """Attach a capability-appropriate model to each agent's subtask.

        This is a PLAN annotation, not a second execution path: the agent still
        runs through its ordinary pipeline, and a route failure leaves the
        subtask without an assignment rather than inventing one. Every
        assignment carries the router's model/provider and its rationale so a
        later provider change or fallback is explainable.
        """
        if self._model_assigner is None or not plan.subtasks:
            return plan
        assigned: list[CollaborationSubtask] = []
        for subtask in plan.subtasks:
            try:
                route = self._model_assigner(subtask.role, subtask.goal, context)
            except Exception:
                route = None
            if not route or not route.get("model"):
                assigned.append(subtask)
                continue
            assignment = {
                "provider_id": str(route.get("provider_id", "")),
                "model": str(route.get("model", "")),
                "role": str(route.get("role", "")),
                "rationale": list(route.get("rationale", ())),
                "route": str(route.get("route", "")),
                "source": str(route.get("source", "adaptive_model_router")),
            }
            assigned.append(
                replace(
                    subtask,
                    metadata={**dict(subtask.metadata), "model_assignment": assignment},
                    context={**dict(subtask.context), "model_assignment": assignment},
                )
            )
        return replace(plan, subtasks=tuple(assigned))

    def model_assignments(self, plan: CollaborationPlan) -> list[dict[str, Any]]:
        """The per-subtask model evidence currently attached to a plan."""
        rows = []
        for subtask in plan.subtasks:
            assignment = subtask.metadata.get("model_assignment")
            if isinstance(assignment, Mapping):
                rows.append(
                    {
                        "subtask_id": subtask.id,
                        "role": subtask.role.value,
                        **dict(assignment),
                    }
                )
        return rows

    async def run(
        self,
        objective: str | None = None,
        *,
        plan: CollaborationPlan | None = None,
        context: Mapping[str, Any] | None = None,
        authorize: bool = False,
        idempotency_key: str = "",
        source: str = "",
    ) -> CollaborationTask:
        """Execute a plan, or return the earlier task for the same key."""
        if not self.enabled:
            raise RuntimeError("multi-agent collaboration is disabled by configuration")
        key = str(idempotency_key or "").strip()
        if key and key in self._idempotency:
            existing = self._tasks.get(self._idempotency[key])
            if existing is not None:
                return existing
        context = dict(context or {})
        selected = plan or self.plan(str(objective or ""), context=context)
        if selected.blocked_reason:
            raise ValueError(selected.blocked_reason)
        if not selected.beneficial or not selected.subtasks:
            return self._declined_task(
                str(objective or selected.objective),
                selected,
                context,
                reason=selected.rationale or "the objective is a single-agent task",
                source=source,
            )
        if len(selected.subtasks) > self.policy.max_subtasks:
            raise ValueError(
                f"the plan has {len(selected.subtasks)} subtasks, above the "
                f"maximum of {self.policy.max_subtasks}"
            )
        if selected.requires_approval and not authorize:
            task = CollaborationTask(
                id=f"collab:{uuid4().hex[:16]}",
                objective=selected.objective,
                mode=selected.mode,
                plan=selected,
                context=_jsonable(context),
                status=CollaborationStatus.DECLINED,
                error="this collaboration writes or executes; pass authorize=true to run it",
                source=source or self._source,
                idempotency_key=key,
                finished_at=_timestamp(),
            )
            self._remember(task)
            return task
        task = CollaborationTask(
            id=f"collab:{uuid4().hex[:16]}",
            objective=selected.objective,
            mode=selected.mode,
            plan=selected,
            context=_jsonable(context),
            status=CollaborationStatus.PLANNED,
            source=source or self._source,
            idempotency_key=key,
        )
        self._remember(task)
        if key:
            self._idempotency[key] = task.id
        await self._save()
        await self._emit(
            EventType.COLLABORATION_PLANNED,
            task,
            subtasks=len(selected.subtasks),
            mode=selected.mode.value,
        )
        try:
            await self._execute(task, authorize=authorize)
        except asyncio.CancelledError:
            task.cancelled = True
            task.status = CollaborationStatus.CANCELLED
            task.error = "the collaboration was cancelled"
            task.finished_at = _timestamp()
            self._save_sync()
            raise
        except Exception as exc:  # containment: a failed collaboration is a result
            task.status = CollaborationStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.finished_at = _timestamp()
            self._errors.append(f"run {task.id}: {task.error}")
            await self._emit(EventType.COLLABORATION_FAILED, task, status=task.status.value)
            self._save_sync()
        return task

    async def resume(self, task_id: str, *, authorize: bool = False) -> CollaborationTask:
        """Continue a stopped task without re-running completed subtasks.

        An approval is a decision about one run, not a permanent grant. A plan
        that writes or executes therefore needs a fresh explicit ``authorize``
        on resume; silently reusing the original approval would let a restart
        replay a decision the person made for an earlier attempt.
        """
        task = self._task_or_raise(task_id)
        if task.status not in {
            CollaborationStatus.INTERRUPTED,
            CollaborationStatus.PARTIAL,
            CollaborationStatus.CANCELLED,
        }:
            raise ValueError(f"collaboration {task_id} is {task.status.value}, not resumable")
        if task.plan.requires_approval and not authorize:
            raise ValueError(
                "resuming this collaboration requires authorize=true; the original "
                "approval is not reused"
            )
        if task.cancelled:
            task.cancelled = False
        for subtask_id, result in list(task.results.items()):
            spec = next((s for s in task.plan.subtasks if s.id == subtask_id), None)
            if spec is None:
                continue
            if result.status is SubtaskStatus.INTERRUPTED:
                if spec.retryable:
                    task.results.pop(subtask_id, None)
                else:
                    result.status = SubtaskStatus.FAILED
                    result.error = "interrupted; not retried because the subtask is not idempotent"
            elif result.status is SubtaskStatus.CANCELLED:
                # A cancelled unit that already started may have produced a
                # side effect. Only a never-started cancellation is safe to
                # re-queue for a non-idempotent subtask.
                if result.agent and not spec.retryable:
                    result.status = SubtaskStatus.FAILED
                    result.error = (
                        "cancelled after starting; not retried because the subtask "
                        "is not idempotent"
                    )
                else:
                    task.results.pop(subtask_id, None)
            elif result.status is SubtaskStatus.FAILED and spec.retryable:
                task.results.pop(subtask_id, None)
        task.status = CollaborationStatus.RUNNING
        task.error = ""
        task.finished_at = ""
        await self._execute(task, authorize=authorize)
        return task

    def cancel(self, task_id: str) -> CollaborationTask:
        """Stop scheduling; cancel in-flight subtasks only when they are safe to abort."""
        task = self._task_or_raise(task_id)
        if task.status in {
            CollaborationStatus.COMPLETED,
            CollaborationStatus.PARTIAL,
            CollaborationStatus.FAILED,
            CollaborationStatus.CANCELLED,
            CollaborationStatus.DECLINED,
        }:
            return task
        task.cancelled = True
        for subtask in task.plan.subtasks:
            result = task.results.get(subtask.id)
            if result is None or result.status is SubtaskStatus.PENDING:
                task.results[subtask.id] = SubtaskResult(
                    subtask_id=subtask.id,
                    agent="",
                    role=subtask.role,
                    status=SubtaskStatus.CANCELLED,
                    error="cancelled before it started",
                )
        for key, handle in list(self._running.items()):
            if key.startswith(f"{task.id}:"):
                subtask_id = key.rsplit(":", 1)[-1]
                spec = next((s for s in task.plan.subtasks if s.id == subtask_id), None)
                if spec is not None and spec.retryable:
                    handle.cancel()
        task.status = CollaborationStatus.CANCELLED
        task.finished_at = _timestamp()
        self._save_sync()
        return task

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for task in self._tasks.values():
            counts[task.status.value] += 1
        return {
            "phase": 28,
            "enabled": self.enabled,
            "policy": self.policy.to_dict(),
            "tasks": len(self._tasks),
            "counts": dict(sorted(counts.items())),
            "modes": sorted({task.mode.value for task in self._tasks.values()}),
            "running": len(self._running),
            "agents": [self._describe_agent(agent) for agent in self.registry.list()],
            "provider": self.provider_preflight.describe(),
            "scheduling": (
                self._last_schedule.to_dict()
                if self._last_schedule is not None
                else schedule_hardware(
                    None,
                    requested_concurrency=self.policy.max_concurrency,
                    hardware_aware=self.policy.hardware_aware,
                    sequential_fallback=self.policy.sequential_fallback,
                ).to_dict()
            ),
            "tokens": self._token_totals(),
            "restored": dict(self.restore_report),
            "errors": list(self._errors[-10:]),
            "boundaries": {
                "replaces_specialist_pipeline": False,
                "widens_permissions": False,
                "retries_non_idempotent_subtasks": False,
                "destructive_actions_require_existing_approval": True,
                "agent_output_is_trusted_only_after_verification": True,
            },
        }

    def _token_totals(self) -> dict[str, Any]:
        totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "reported_calls": 0,
            "reported_tasks": 0,
        }
        for task in self._tasks.values():
            usage = task.token_usage if isinstance(task.token_usage, Mapping) else {}
            if usage.get("reported_calls"):
                totals["reported_tasks"] += 1
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "calls",
                "reported_calls",
            ):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += max(0, value)
        return {
            **totals,
            "per_task_budget": self.policy.token_budget,
        }

    def overview(self) -> dict[str, Any]:
        return {
            "phase": 28,
            "name": "multi-agent collaboration",
            "summary": (
                "Objectives that name separable work are decomposed into a validated "
                "dependency graph, assigned to registered specialists, executed with "
                "bounded concurrency, and aggregated only from verified evidence."
            ),
            "modes": [mode.value for mode in CollaborationMode],
            "architecture": {
                "agent_source": "the existing AgentRegistry and SpecialistPipeline",
                "planner": "deterministic, structural; no model and no second planner",
                "scheduling": (
                    "asyncio.gather over ready subtasks, bounded by a semaphore whose "
                    "limit is capped from the live hardware reading"
                ),
                "context": "declared per-subtask; dependents receive upstream public views only",
                "provider": (
                    "the live completion provider's declared capabilities are checked "
                    "before a subtask that declares requirements is dispatched"
                ),
                "tokens": "one central per-collaboration ledger, fed only by reported usage",
                "backoff": "bounded exponential with Retry-After support for idempotent work",
                "verification": "the agent run's own verification, plus independent verifier subtasks",
                "conflicts": "evidence-resolved when possible; otherwise surfaced unresolved",
                "learning": "Phase 27 experiences with task_kind collaboration.<mode>",
            },
            "capabilities": [
                "register and discover agents by role and capability declaration",
                "decline tasks that do not benefit from collaboration",
                "decompose research, coding and dataset analysis into dependency graphs",
                "run independent subtasks concurrently and dependent ones in order",
                "pass only declared upstream context to dependents",
                "time out, retry idempotent subtasks, reassign to another same-role agent",
                "preserve completed work when a sibling fails",
                "surface unresolved conflicts instead of majority-voting them away",
                "persist tasks and resume pending work after a restart",
                "capture collaboration evidence into Phase 27 without upgrading unverified work",
                "account reported token usage against one central per-collaboration budget",
                "preflight declared provider capabilities before dispatching a subtask",
                "honor Retry-After and bounded exponential backoff for idempotent work",
                "cap concurrency from the live CPU/RAM/GPU reading when the governor is wired",
            ],
            "limitations": [
                "decomposition is template-based for the three supported modes; it does not invent new roles",
                "parallel execution is limited to independent subtasks and the effective concurrency",
                "a non-idempotent subtask is never retried after interruption",
                "an objective that does not clearly name separable work takes the single-agent path",
                "token usage is only as complete as the numeric usage agents report; no billing reconciliation is inferred",
                "a provider that does not declare a context/output capability cannot satisfy a requirement for it",
            ],
        }

    def tasks(
        self, *, status: str = "", limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        rows = sorted(
            self._tasks.values(), key=lambda task: task.created_at, reverse=True
        )
        if status:
            rows = [task for task in rows if task.status.value == status]
        bounded = rows[offset : offset + max(0, min(limit, 100))]
        return {
            "total": len(rows),
            "tasks": [task.to_dict(include_plan=False) for task in bounded],
        }

    def task(self, task_id: str) -> CollaborationTask:
        return self._task_or_raise(task_id)

    def flush(self) -> dict[str, Any]:
        if not self.policy.persist or self._store is None:
            return {"saved": False, "reason": "persistence is disabled"}
        tasks = sorted(
            self._tasks.values(), key=lambda item: item.created_at
        )[-self.policy.max_tasks :]
        try:
            self._store.write(
                "collaboration",
                {
                    "schema_version": COLLABORATION_SCHEMA_VERSION,
                    "tasks": [task.to_dict() for task in tasks],
                },
            )
            return {"saved": True, "tasks": len(tasks)}
        except Exception as exc:  # persistence is auxiliary and never fails work
            self._errors.append(f"save: {type(exc).__name__}: {exc}")
            return {"saved": False, "reason": type(exc).__name__}

    def restore(self) -> dict[str, Any]:
        """Load persisted tasks; completed work is kept, running work is not replayed."""
        if not self.policy.persist or self._store is None:
            self.restore_report = {"restored": 0, "reason": "persistence is disabled"}
            return dict(self.restore_report)
        try:
            payload = self._store.read("collaboration")
        except Exception as exc:
            self.restore_report = {"restored": 0, "reason": type(exc).__name__}
            return dict(self.restore_report)
        if str(payload.get("schema_version", "")) != COLLABORATION_SCHEMA_VERSION:
            self.restore_report = {
                "restored": 0,
                "reason": "no compatible persisted collaboration schema",
            }
            return dict(self.restore_report)
        restored = 0
        interrupted = 0
        for row in payload.get("tasks", ())[: self.policy.max_tasks]:
            if not isinstance(row, Mapping):
                continue
            try:
                task = CollaborationTask.from_dict(row)
            except (ValueError, TypeError, KeyError):
                continue
            if not task.id:
                continue
            if task.status is CollaborationStatus.RUNNING:
                task.status = CollaborationStatus.INTERRUPTED
                interrupted += 1
                for subtask_id, result in task.results.items():
                    if result.status is SubtaskStatus.RUNNING:
                        spec = next(
                            (s for s in task.plan.subtasks if s.id == subtask_id), None
                        )
                        if spec is not None and spec.retryable:
                            task.results.pop(subtask_id, None)
                        else:
                            result.status = SubtaskStatus.INTERRUPTED
                            result.error = "interrupted by a restart; not replayed automatically"
            elif task.status is CollaborationStatus.PLANNED:
                task.status = CollaborationStatus.INTERRUPTED
                interrupted += 1
            self._tasks[task.id] = task
            if task.idempotency_key:
                self._idempotency[task.idempotency_key] = task.id
            restored += 1
        self.restore_report = {
            "restored": restored,
            "interrupted": interrupted,
            "schema_version": COLLABORATION_SCHEMA_VERSION,
        }
        return dict(self.restore_report)

    # -- scheduling ----------------------------------------------------------

    async def _execute(self, task: CollaborationTask, *, authorize: bool) -> None:
        task.status = CollaborationStatus.RUNNING
        task.started_at = task.started_at or _timestamp()
        task.updated_at = _timestamp()
        schedule = schedule_hardware(
            self._hardware_governor,
            requested_concurrency=self.policy.max_concurrency,
            hardware_aware=self.policy.hardware_aware,
            sequential_fallback=self.policy.sequential_fallback,
        )
        self._last_schedule = schedule
        task.scheduling = {
            **schedule.to_dict(),
            "model_assignments": self.model_assignments(task.plan),
        }
        self._semaphore = asyncio.Semaphore(schedule.effective_concurrency)
        ledger = TokenLedger.from_dict(task.token_usage, limit=self.policy.token_budget)
        if not task.token_usage:
            # A resumed task has results but may not carry the ledger from the
            # earlier process. Rebuild only from usage those results reported.
            for prior in task.result_list():
                ledger.account(
                    prior.usage,
                    agent=prior.agent,
                    subtask_id=prior.subtask_id,
                )
        self._ledgers[task.id] = ledger
        task.token_usage = ledger.to_dict()
        preflight = self.provider_preflight.check_plan(task.plan)
        task.provider_preflight = [report.to_dict() for report in preflight]
        for report in preflight:
            if report.ok or report.subtask_id in task.results:
                continue
            task.results[report.subtask_id] = SubtaskResult(
                subtask_id=report.subtask_id,
                agent="",
                role=next(
                    (
                        spec.role
                        for spec in task.plan.subtasks
                        if spec.id == report.subtask_id
                    ),
                    AgentRole.PLANNING,
                ),
                status=SubtaskStatus.FAILED,
                error="provider preflight failed: " + "; ".join(report.reasons),
                verification={
                    "status": "failed",
                    "source": "provider_preflight",
                    "missing": list(report.missing),
                },
                finished_at=_timestamp(),
            )
        await self._emit(
            EventType.COLLABORATION_STARTED,
            task,
            subtasks=len(task.plan.subtasks),
        )
        started = time.monotonic()
        try:
            while True:
                if task.cancelled:
                    self._cancel_remaining(task)
                    break
                self._fail_dependents(task)
                ready = [
                    spec
                    for spec in task.plan.subtasks
                    if spec.id not in task.results
                    and all(
                        dep_id in task.results
                        and task.results[dep_id].status is SubtaskStatus.COMPLETED
                        for dep_id in spec.depends_on
                    )
                ]
                if not ready:
                    break
                scheduled: list[CollaborationSubtask] = []
                for spec in ready:
                    if ledger.exhausted:
                        task.results[spec.id] = SubtaskResult(
                            subtask_id=spec.id,
                            agent="",
                            role=spec.role,
                            status=SubtaskStatus.SKIPPED,
                            error=(
                                f"token budget exhausted ({ledger.total_tokens}/"
                                f"{ledger.limit}) before this subtask started"
                            ),
                            finished_at=_timestamp(),
                        )
                        self._message(
                            task,
                            from_agent="coordinator",
                            to_agent="*",
                            kind="budget_exhausted",
                            content=f"{spec.id} skipped: token budget exhausted",
                            subtask_id=spec.id,
                        )
                        continue
                    scheduled.append(spec)
                if not scheduled:
                    task.token_usage = ledger.to_dict()
                    continue
                ready = scheduled
                for spec in ready:
                    task.results[spec.id] = SubtaskResult(
                        subtask_id=spec.id,
                        agent="",
                        role=spec.role,
                        status=SubtaskStatus.RUNNING,
                        started_at=_timestamp(),
                    )
                    self._message(
                        task,
                        from_agent="coordinator",
                        to_agent=spec.role.value,
                        kind="assigned",
                        content=f"{spec.title}: {spec.goal}",
                        subtask_id=spec.id,
                    )
                    await self._emit(
                        EventType.COLLABORATION_SUBTASK_STARTED,
                        task,
                        subtask_id=spec.id,
                        agent=spec.role.value,
                    )
                handles: dict[str, asyncio.Task[SubtaskResult]] = {}
                for spec in ready:
                    handle = asyncio.create_task(
                        self._run_subtask(task, spec, authorize=authorize),
                        name=f"{task.id}:{spec.id}",
                    )
                    handles[spec.id] = handle
                    self._running[f"{task.id}:{spec.id}"] = handle
                try:
                    completed = await asyncio.gather(*handles.values())
                finally:
                    for spec in ready:
                        self._running.pop(f"{task.id}:{spec.id}", None)
                for result in completed:
                    task.results[result.subtask_id] = result
                    self._message(
                        task,
                        from_agent=result.agent or result.role.value,
                        to_agent="coordinator",
                        kind="result",
                        content=result.content[:1000],
                        subtask_id=result.subtask_id,
                        verified=result.verified,
                        status=result.status.value,
                    )
                    await self._emit(
                        EventType.COLLABORATION_SUBTASK_COMPLETED,
                        task,
                        subtask_id=result.subtask_id,
                        status=result.status.value,
                        verified=result.verified,
                    )
                task.updated_at = _timestamp()
                task.token_usage = ledger.to_dict()
                await self._save()
                if task.cancelled:
                    self._cancel_remaining(task)
                    break
                # Progress is persisted after each wave, so a crash keeps every
                # subtask that actually finished.
                if len(task.results) >= len(task.plan.subtasks):
                    break
            task.conflicts = self._detect_conflicts(task)
            if any(not conflict.resolved for conflict in task.conflicts):
                await self._emit(
                    EventType.COLLABORATION_CONFLICT_DETECTED,
                    task,
                    conflicts=sum(1 for c in task.conflicts if not c.resolved),
                )
            task.aggregate = self._aggregate(task, wall_ms=(time.monotonic() - started) * 1000)
            task.status = CollaborationStatus(str(task.aggregate["status"]))
            task.finished_at = _timestamp()
            task.updated_at = task.finished_at
            task.learning = self._capture_learning(task)
            await self._emit(
                EventType.COLLABORATION_COMPLETED
                if task.status
                in {CollaborationStatus.COMPLETED, CollaborationStatus.PARTIAL}
                else EventType.COLLABORATION_FAILED,
                task,
                status=task.status.value,
                verified=bool(task.aggregate.get("verified", False)),
            )
        finally:
            task.token_usage = ledger.to_dict()
            self._ledgers.pop(task.id, None)
            self._semaphore = None
            await self._save()

    def _account_usage(self, task: CollaborationTask, result: SubtaskResult) -> None:
        """Record one attempt's reported tokens in the task's central ledger."""
        ledger = self._ledgers.get(task.id)
        if ledger is None:
            return
        ledger.account(
            result.usage,
            agent=result.agent,
            subtask_id=result.subtask_id,
        )
        task.token_usage = ledger.to_dict()

    async def _run_subtask(
        self,
        task: CollaborationTask,
        spec: CollaborationSubtask,
        *,
        authorize: bool,
    ) -> SubtaskResult:
        all_candidates = list(self.registry.by_role(spec.role))
        candidates = self._candidates(spec.role)
        began = time.monotonic()
        started_at = _timestamp()
        attempts = 0
        retries = 0
        last_error = ""
        last_agent = ""

        def finish(result: SubtaskResult) -> SubtaskResult:
            """Attach this subtask's cumulative usage, including retries."""
            ledger = self._ledgers.get(task.id)
            if ledger is not None:
                bucket = ledger.by_subtask.get(spec.id)
                if bucket:
                    # Keep the agent's own non-token fields (cost, model, …) and
                    # layer the cumulative token counts over them, so a retry's
                    # final result reports every attempt without losing its
                    # provider's other measurements.
                    merged = dict(result.usage)
                    merged.update(bucket)
                    # The generic aggregate key is cumulative too; leaving the
                    # last attempt's value would undercount a retried subtask.
                    merged["tokens"] = bucket["total_tokens"]
                    result.usage = merged
            return result

        if not candidates:
            unavailable = [
                agent.name
                for agent in all_candidates
                if not _agent_availability(agent)[0]
            ]
            detail = f"no registered agent fills the {spec.role.value} role"
            if unavailable:
                detail = (
                    f"no available agent fills the {spec.role.value} role; "
                    f"unavailable: {', '.join(unavailable)}"
                )
            return finish(SubtaskResult(
                subtask_id=spec.id,
                agent="",
                role=spec.role,
                status=SubtaskStatus.FAILED,
                error=detail,
                started_at=started_at,
                finished_at=_timestamp(),
                duration_ms=(time.monotonic() - began) * 1000,
            ))
        deadline_seconds = float(
            spec.timeout_seconds or self.policy.default_timeout_seconds
        )
        # Two independent subtasks with the same role must not both land on the
        # first candidate: that would advertise multi-agent work while one agent
        # did all of it, and it would hide a failing peer behind a healthy first
        # in the list. Rotation is deterministic by subtask position, and a
        # retry moves to the next capable agent.
        position = next(
            (index for index, item in enumerate(task.plan.subtasks) if item.id == spec.id),
            0,
        )
        while attempts <= self.policy.max_retries:
            attempts += 1
            agent = candidates[(position + attempts - 1) % len(candidates)]
            last_agent = agent.name
            runnable: RunnableAgent = agent
            if authorize:
                if self._authorize_pipeline is None:
                    return finish(SubtaskResult(
                        subtask_id=spec.id,
                        agent=agent.name,
                        role=spec.role,
                        status=SubtaskStatus.FAILED,
                        error="authorized execution requested but no authorized pipeline factory is configured",
                        attempts=attempts,
                        started_at=started_at,
                        finished_at=_timestamp(),
                        duration_ms=(time.monotonic() - began) * 1000,
                    ))
                clone = getattr(agent, "clone", None)
                if not callable(clone):
                    return finish(SubtaskResult(
                        subtask_id=spec.id,
                        agent=agent.name,
                        role=spec.role,
                        status=SubtaskStatus.FAILED,
                        error="agent cannot accept an authorized pipeline; no clone hook",
                        attempts=attempts,
                        started_at=started_at,
                        finished_at=_timestamp(),
                        duration_ms=(time.monotonic() - began) * 1000,
                    ))
                try:
                    runnable = clone(self._authorize_pipeline())
                except Exception as exc:
                    return finish(SubtaskResult(
                        subtask_id=spec.id,
                        agent=agent.name,
                        role=spec.role,
                        status=SubtaskStatus.FAILED,
                        error=f"authorized clone failed: {type(exc).__name__}: {exc}",
                        attempts=attempts,
                        started_at=started_at,
                        finished_at=_timestamp(),
                        duration_ms=(time.monotonic() - began) * 1000,
                    ))
            agent_task = AgentTask(
                goal=spec.goal,
                role=spec.role,
                metadata={
                    "collaboration_task_id": task.id,
                    "subtask_id": spec.id,
                    "context": self._input_context(task, spec),
                    **dict(spec.metadata),
                },
            )
            try:
                semaphore = self._semaphore or asyncio.Semaphore(1)
                async with semaphore:
                    response = await asyncio.wait_for(
                        runnable.handle_task(agent_task), timeout=deadline_seconds
                    )
                result = self._result_from_response(spec, response, attempts)
            except TimeoutError:
                result = SubtaskResult(
                    subtask_id=spec.id,
                    agent=runnable.name,
                    role=spec.role,
                    status=SubtaskStatus.FAILED,
                    error=f"timed out after {deadline_seconds:.1f}s",
                    attempts=attempts,
                    started_at=started_at,
                )
            except asyncio.CancelledError:
                return finish(SubtaskResult(
                    subtask_id=spec.id,
                    agent=runnable.name,
                    role=spec.role,
                    status=SubtaskStatus.CANCELLED,
                    error="cancelled while running",
                    attempts=attempts,
                    started_at=started_at,
                    finished_at=_timestamp(),
                    duration_ms=(time.monotonic() - began) * 1000,
                ))
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                result = SubtaskResult(
                    subtask_id=spec.id,
                    agent=runnable.name,
                    role=spec.role,
                    status=SubtaskStatus.FAILED,
                    error=last_error,
                    attempts=attempts,
                    started_at=started_at,
                )
            result.attempts = attempts
            result.retries = retries
            result.started_at = started_at
            result.finished_at = _timestamp()
            result.duration_ms = (time.monotonic() - began) * 1000
            self._account_usage(task, result)
            if result.status is SubtaskStatus.COMPLETED:
                return finish(result)
            if result.status is SubtaskStatus.DENIED:
                return finish(result)
            if not spec.retryable or attempts > self.policy.max_retries:
                return finish(result)
            ledger = self._ledgers.get(task.id)
            if ledger is not None and ledger.exhausted:
                return finish(result)
            decision = backoff_decision(
                result.error,
                attempt=attempts,
                base_seconds=self.policy.rate_limit_backoff_seconds,
                maximum_seconds=self.policy.rate_limit_backoff_max_seconds,
            )
            if decision.rate_limited:
                task.backoff_events.append(
                    {
                        "subtask_id": spec.id,
                        "agent": result.agent,
                        **decision.to_dict(),
                    }
                )
                if decision.delay_seconds > 0:
                    await self._sleep(decision.delay_seconds)
            retries += 1
            last_error = result.error
        return finish(SubtaskResult(
            subtask_id=spec.id,
            agent=last_agent,
            role=spec.role,
            status=SubtaskStatus.FAILED,
            error=last_error or "the subtask failed",
            attempts=attempts,
            retries=retries,
            started_at=started_at,
            finished_at=_timestamp(),
            duration_ms=(time.monotonic() - began) * 1000,
        ))

    def _result_from_response(
        self, spec: CollaborationSubtask, response: AgentResponse, attempts: int
    ) -> SubtaskResult:
        metadata = dict(response.metadata or {})
        report = metadata.get("report") if isinstance(metadata.get("report"), Mapping) else {}
        report = dict(report or {})
        denied = metadata.get("denied") or report.get("denied") or []
        status = SubtaskStatus.COMPLETED
        if response.status is not AgentTaskStatus.COMPLETED:
            status = SubtaskStatus.FAILED
        if denied:
            status = SubtaskStatus.DENIED
        verified = bool(metadata.get("verified", report.get("verified", False)))
        claims_raw = report.get("claims") or metadata.get("claims") or []
        claims = tuple(
            _jsonable(item) for item in claims_raw if isinstance(item, Mapping)
        )[:_MAX_CLAIMS]
        verification: dict[str, Any]
        raw_verification = metadata.get("verification") or report.get("verification")
        steps = metadata.get("verifications") or report.get("verifications") or {}
        if isinstance(steps, Mapping) and steps:
            confidences = [
                float(entry.get("confidence", 0.0) or 0.0)
                for entry in steps.values()
                if isinstance(entry, Mapping)
            ]
            verification = {
                "status": "passed" if verified else "failed",
                "confidence": max(confidences, default=0.0),
                "steps": dict(steps),
            }
        elif isinstance(raw_verification, Mapping):
            verification = dict(raw_verification)
        else:
            verification = {"status": str(raw_verification or "")}
        if "confidence" not in verification:
            verification["confidence"] = 0.0
        evidence = report or {
            key: value
            for key, value in metadata.items()
            if key in {"sources", "source_count", "tests", "metrics", "files", "checks"}
        }
        raw_usage = metadata.get("usage") or report.get("usage") or {}
        usage: dict[str, float] = {}
        if isinstance(raw_usage, Mapping):
            for key, value in raw_usage.items():
                amount = _number(value)
                if amount is not None:
                    usage[str(key)] = amount
        error = ""
        if status is SubtaskStatus.DENIED:
            error = "the agent's permission layer refused a required step"
        elif status is SubtaskStatus.FAILED:
            error = response.content or (metadata.get("errors") or ["the agent run failed"])[0]
        return SubtaskResult(
            subtask_id=spec.id,
            agent=response.agent_name,
            role=response.role,
            status=status,
            content=str(response.content or "")[:12000],
            verified=verified,
            verification=_jsonable(dict(verification)),
            evidence=_jsonable(evidence),
            claims=claims,
            usage=_jsonable(usage),
            error=str(error),
            attempts=attempts,
            output=_jsonable(
                {
                    "content": str(response.content or "")[:12000],
                    "report": report,
                    "metadata": {
                        key: value
                        for key, value in metadata.items()
                        if key
                        in {
                            "verified",
                            "errors",
                            "outcomes",
                            "denied",
                            "summary",
                            "report",
                        }
                    },
                }
            ),
        )

    def _input_context(
        self, task: CollaborationTask, spec: CollaborationSubtask
    ) -> dict[str, Any]:
        context: dict[str, Any] = dict(task.context)
        context.update(dict(spec.context))
        upstream: dict[str, Any] = {}
        for dep_id in spec.depends_on:
            result = task.results.get(dep_id)
            if result is not None:
                upstream[dep_id] = result.public_view()
        if upstream:
            context["upstream"] = upstream
            # Verification templates read the claims/metrics they were asked to
            # check; this is the only place an upstream body crosses a subtask
            # boundary, and it is bounded by public_view().
            context["verification_input"] = upstream
        return context

    def _describe_agent(self, agent: RunnableAgent) -> dict[str, Any]:
        """Identity, advertised capabilities, limits and verified history.

        Read live from the registered object: a capability list copied at boot
        would keep advertising a tool the agent no longer declares.
        """
        declarations: dict[str, Any] = {}
        declares = getattr(agent, "declarations", None)
        if callable(declares):
            try:
                declarations = {
                    str(name): declaration.to_dict()
                    for name, declaration in declares().items()
                }
            except Exception:
                declarations = {}
        history = self._agent_history()
        available, availability_reason, availability = _agent_availability(agent)
        return {
            "name": agent.name,
            "role": agent.role.value,
            "description": str(getattr(agent, "description", "") or ""),
            "available": available,
            "availability_reason": availability_reason,
            "availability": availability,
            "execution_model": "shared SpecialistPipeline",
            "tools": sorted(declarations),
            "permissions": declarations,
            "context_requirements": [
                "declared per-subtask context; no cross-task history",
            ],
            "constraints": {
                "timeout_seconds": self.policy.default_timeout_seconds,
                "max_concurrency": self.policy.max_concurrency,
                "max_retries": self.policy.max_retries,
            },
            "verified_collaborations": int(history.get(agent.name, 0)),
        }

    def _candidates(self, role: AgentRole) -> list[RunnableAgent]:
        agents = [
            agent
            for agent in self.registry.by_role(role)
            if _agent_availability(agent)[0]
        ]
        if len(agents) <= 1:
            return agents
        history = self._agent_history()
        return sorted(
            agents,
            key=lambda agent: (-history.get(agent.name, 0), agents.index(agent)),
        )

    def _agent_history(self) -> dict[str, int]:
        """Verified collaboration counts per agent, read from Phase 27 evidence.

        Only records whose outcome is verified success contribute. An unverified
        run teaches nothing about reliability, which is the point.
        """
        if self._learning is None:
            return {}
        try:
            payload = self._learning.experiences(limit=500)
        except Exception:
            return {}
        rows = payload.get("experiences", ()) if isinstance(payload, Mapping) else ()
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("verified"):
                continue
            if not str(row.get("task_kind", "")).startswith("collaboration."):
                continue
            context = row.get("context", {})
            if not isinstance(context, Mapping):
                continue
            for name in context.get("agents", ()):
                counts[str(name)] += 1
        return dict(counts)

    # -- aggregation ---------------------------------------------------------

    def _aggregate(self, task: CollaborationTask, *, wall_ms: float) -> dict[str, Any]:
        specs = task.plan.subtasks
        results = task.results
        tokens = dict(task.token_usage or {})
        required = [spec for spec in specs if spec.required]
        completed = [spec for spec in specs if results.get(spec.id, None) and results[spec.id].status is SubtaskStatus.COMPLETED]
        verified = [spec for spec in specs if results.get(spec.id, None) and results[spec.id].verified]
        missing = [
            spec.id
            for spec in required
            if spec.id not in results
            or results[spec.id].status is not SubtaskStatus.COMPLETED
        ]
        unverified = [
            spec.id
            for spec in required
            if spec.id in results
            and results[spec.id].status is SubtaskStatus.COMPLETED
            and not results[spec.id].verified
        ]
        unresolved = [conflict for conflict in task.conflicts if not conflict.resolved]
        failed = [
            spec.id
            for spec in specs
            if spec.id in results
            and results[spec.id].status
            in {
                SubtaskStatus.FAILED,
                SubtaskStatus.DENIED,
                SubtaskStatus.SKIPPED,
                SubtaskStatus.INTERRUPTED,
            }
        ]
        verified_required = [
            spec for spec in required if spec.id in {item.id for item in verified}
        ]
        all_required_verified = len(verified_required) == len(required) and not missing and not unverified
        is_verified = bool(all_required_verified and not unresolved and results)
        if task.cancelled:
            status = CollaborationStatus.CANCELLED
        elif not verified_required:
            status = CollaborationStatus.FAILED
        elif is_verified:
            status = CollaborationStatus.COMPLETED
        else:
            status = CollaborationStatus.PARTIAL
        subtask_ms = sum(result.duration_ms for result in results.values())
        usage_totals: dict[str, float] = {}
        for result in results.values():
            for key, value in result.usage.items():
                amount = _number(value)
                if amount is not None:
                    usage_totals[str(key)] = usage_totals.get(str(key), 0.0) + amount
        confidence = 0.0
        if verified:
            confidence = min(
                float(result.verification.get("confidence", 0.0) or 0.0)
                for result in (results[spec.id] for spec in verified)
            )
        if confidence <= 0.0 and verified:
            # Verification records are allowed to carry only a verdict; a
            # verified collaboration then reports the conservative baseline
            # rather than a fake precision.
            confidence = 0.6
        if not is_verified:
            confidence = min(confidence, 0.5)
        incomplete = list(dict.fromkeys([*missing, *unverified]))
        reasons: list[str] = []
        if missing:
            reasons.append(f"required subtask(s) did not complete: {', '.join(missing)}")
        if unverified:
            reasons.append(f"required subtask(s) completed without verification: {', '.join(unverified)}")
        if unresolved:
            reasons.append(f"{len(unresolved)} unresolved conflict(s)")
        if failed and not missing:
            reasons.append(f"failed subtask(s): {', '.join(failed)}")
        if tokens.get("exhausted"):
            reasons.append(
                f"token budget exhausted at {tokens.get('total_tokens', 0)}/"
                f"{tokens.get('budget', 0)} tokens"
            )
        elif tokens.get("over_budget"):
            reasons.append(
                f"token budget exceeded: {tokens.get('total_tokens', 0)}/"
                f"{tokens.get('budget', 0)} tokens"
            )
        if is_verified:
            summary = (
                f"Collaboration completed: {len(verified_required)}/{len(required)} required "
                f"subtask(s) verified across {len({r.agent for r in results.values() if r.agent})} "
                f"agent(s); {len(task.conflicts)} conflict(s) checked."
            )
        elif status is CollaborationStatus.CANCELLED:
            summary = (
                f"Collaboration cancelled: {len(verified_required)}/{len(required)} required "
                f"subtask(s) verified; completed work was preserved."
            )
        else:
            summary = (
                f"Collaboration {status.value}: {len(verified_required)}/{len(required)} required "
                f"subtask(s) verified. " + "; ".join(reasons)
            ).strip()
        contributions = [
            {
                "subtask_id": result.subtask_id,
                "agent": result.agent,
                "role": result.role.value,
                "status": result.status.value,
                "verified": result.verified,
                "duration_ms": round(result.duration_ms, 2),
            }
            for result in task.result_list()
        ]
        return {
            "status": status.value,
            "verified": is_verified,
            "summary": summary,
            "reasons": reasons,
            "required": len(required),
            "completed": len(completed),
            "verified_required": len(verified_required),
            "missing": missing,
            "unverified": unverified,
            "failed": failed,
            "incomplete": incomplete,
            "conflicts": [conflict.to_dict() for conflict in task.conflicts],
            "unresolved_conflicts": [conflict.to_dict() for conflict in unresolved],
            "resolved_conflicts": [
                conflict.to_dict() for conflict in task.conflicts if conflict.resolved
            ],
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "outputs": [
                {
                    "subtask_id": result.subtask_id,
                    "agent": result.agent,
                    "content": result.content,
                    "claims": list(result.claims),
                    "verified": result.verified,
                }
                for result in task.result_list()
            ],
            "contributions": contributions,
            "usage": {
                key: round(value, 2) for key, value in sorted(usage_totals.items())
            },
            "usage_available": bool(usage_totals)
            or bool(tokens.get("reported_calls")),
            "tokens": tokens,
            "token_budget_exhausted": bool(tokens.get("exhausted")),
            "scheduling": dict(task.scheduling),
            "provider_preflight": list(task.provider_preflight),
            "backoff": list(task.backoff_events),
            "timing": {
                "wall_ms": round(max(0.0, wall_ms), 2),
                "subtask_ms": round(subtask_ms, 2),
                "parallelism": round(subtask_ms / max(wall_ms, 1.0), 2),
            },
            "verification_basis": (
                "each required subtask's own verification stage; a verifier subtask "
                "recomputed the work it was assigned rather than trusting a peer"
            ),
        }

    def _detect_conflicts(self, task: CollaborationTask) -> list[ConflictRecord]:
        claims: list[tuple[SubtaskResult, dict[str, Any]]] = []
        for result in task.result_list():
            for claim in result.claims:
                if isinstance(claim, Mapping):
                    claims.append((result, dict(claim)))
        if len(claims) < 2:
            return []
        recomputed = self._recomputed_values(task)
        conflicts: list[ConflictRecord] = []
        seen: set[tuple[str, str, str]] = set()
        for index, (left_result, left) in enumerate(claims):
            for right_result, right in claims[index + 1 :]:
                if left_result.agent == right_result.agent and left_result.subtask_id == right_result.subtask_id:
                    continue
                topic = _claim_topic(left)
                if not topic or topic != _claim_topic(right):
                    continue
                if not _contradicts(left, right):
                    continue
                key = (
                    topic,
                    str(left.get("value", left.get("text", ""))),
                    str(right.get("value", right.get("text", ""))),
                )
                if key in seen:
                    continue
                seen.add(key)
                conflict = ConflictRecord(
                    kind="claim",
                    topic=topic,
                    left=str(left.get("text") or left.get("value") or ""),
                    right=str(right.get("text") or right.get("value") or ""),
                    left_agent=left_result.agent,
                    right_agent=right_result.agent,
                    confidence=min(
                        float(left.get("confidence", 0.5) or 0.5),
                        float(right.get("confidence", 0.5) or 0.5),
                    ),
                )
                actual = recomputed.get(topic)
                if actual is not None:
                    left_value = _number(left.get("value"))
                    right_value = _number(right.get("value"))
                    if left_value is not None and _close(left_value, actual):
                        conflict.resolved = True
                        conflict.resolution = f"independent recomputation matched {left_result.agent}: {actual}"
                    elif right_value is not None and _close(right_value, actual):
                        conflict.resolved = True
                        conflict.resolution = f"independent recomputation matched {right_result.agent}: {actual}"
                    else:
                        conflict.resolution = f"independent recomputation produced {actual}"
                if not conflict.resolved:
                    conflict.reason = (
                        "both sides cite evidence but neither can be confirmed from the "
                        "available verification"
                    )
                conflicts.append(conflict)
                if len(conflicts) >= _MAX_CONFLICTS:
                    return conflicts
        return conflicts

    def _recomputed_values(self, task: CollaborationTask) -> dict[str, float]:
        values: dict[str, float] = {}
        for spec in task.plan.subtasks:
            if str(spec.metadata.get("verification", "")) != "metrics":
                continue
            result = task.results.get(spec.id)
            if result is None:
                continue
            checks = result.evidence.get("checks") if isinstance(result.evidence, Mapping) else None
            if isinstance(checks, Mapping):
                for topic, check in checks.items():
                    if isinstance(check, Mapping) and check.get("ok"):
                        value = _number(check.get("actual"))
                        if value is not None:
                            values[str(topic)] = value
        return values

    # -- persistence, events, messages --------------------------------------

    def _remember(self, task: CollaborationTask) -> None:
        self._tasks[task.id] = task
        if len(self._tasks) > self.policy.max_tasks:
            terminal = [
                item
                for item in self._tasks.values()
                if item.status
                not in {CollaborationStatus.RUNNING, CollaborationStatus.PLANNED}
            ]
            terminal.sort(key=lambda item: item.updated_at)
            for old in terminal[: max(0, len(self._tasks) - self.policy.max_tasks)]:
                self._tasks.pop(old.id, None)

    def _declined_task(
        self,
        objective: str,
        plan: CollaborationPlan,
        context: Mapping[str, Any],
        *,
        reason: str,
        source: str = "",
    ) -> CollaborationTask:
        task = CollaborationTask(
            id=f"collab:{uuid4().hex[:16]}",
            objective=objective,
            mode=plan.mode,
            plan=plan,
            context=_jsonable(dict(context)),
            status=CollaborationStatus.DECLINED,
            error=reason,
            source=source or self._source,
            finished_at=_timestamp(),
        )
        self._remember(task)
        return task

    def _fail_dependents(self, task: CollaborationTask) -> None:
        by_id = {spec.id: spec for spec in task.plan.subtasks}
        for spec in task.plan.subtasks:
            if spec.id in task.results:
                continue
            for dep_id in spec.depends_on:
                dep = task.results.get(dep_id)
                if dep is None:
                    continue
                if dep.status is SubtaskStatus.COMPLETED:
                    continue
                # A dependency that is merely RUNNING does not fail its
                # dependents; one that ended without success does, and the
                # dependent is recorded SKIPPED with the reason rather than
                # silently omitted.
                if dep.status is SubtaskStatus.RUNNING:
                    continue
                task.results[spec.id] = SubtaskResult(
                    subtask_id=spec.id,
                    agent="",
                    role=by_id[spec.id].role,
                    status=SubtaskStatus.SKIPPED,
                    error=f"dependency {dep_id} ended {dep.status.value}",
                )
                self._message(
                    task,
                    from_agent="coordinator",
                    to_agent="*",
                    kind="dependency_failed",
                    content=f"{spec.id} skipped because {dep_id} ended {dep.status.value}",
                    subtask_id=spec.id,
                )
                break

    def _cancel_remaining(self, task: CollaborationTask) -> None:
        for spec in task.plan.subtasks:
            if spec.id in task.results:
                continue
            task.results[spec.id] = SubtaskResult(
                subtask_id=spec.id,
                agent="",
                role=spec.role,
                status=SubtaskStatus.CANCELLED,
                error="cancelled",
            )
        task.status = CollaborationStatus.CANCELLED
        task.finished_at = _timestamp()
        task.updated_at = task.finished_at

    def _message(
        self,
        task: CollaborationTask,
        *,
        from_agent: str,
        to_agent: str,
        kind: str,
        content: str,
        subtask_id: str = "",
        **metadata: Any,
    ) -> None:
        record = {
            "from": from_agent,
            "to": to_agent,
            "kind": kind,
            "content": content[:2000],
            "subtask_id": subtask_id,
            "at": _timestamp(),
            **{key: _jsonable(value) for key, value in metadata.items()},
        }
        task.messages.append(record)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        message = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content[:2000],
            task_id=task.id,
            metadata={
                "kind": kind,
                "subtask_id": subtask_id,
                **{key: _jsonable(value) for key, value in metadata.items()},
            },
        )
        asyncio.create_task(self.bus.send(message))

    async def _emit(
        self, type_: EventType, task: CollaborationTask, **payload: Any
    ) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                type_,
                source="agents.collaboration",
                correlation_id=task.id,
                task_id=task.id,
                **payload,
            )
        except Exception as exc:
            self._errors.append(f"event {type_.value}: {type(exc).__name__}: {exc}")

    def _capture_learning(self, task: CollaborationTask) -> dict[str, Any]:
        if not self.policy.capture_learning or self._learning is None:
            return {"captured": False, "reason": "learning capture is disabled"}
        try:
            steps = []
            for spec in task.plan.subtasks:
                result = task.results.get(spec.id)
                if result is None:
                    continue
                status = {
                    SubtaskStatus.COMPLETED: "completed",
                    SubtaskStatus.SKIPPED: "skipped",
                    SubtaskStatus.DENIED: "denied",
                    SubtaskStatus.CANCELLED: "skipped",
                }.get(result.status, "failed")
                steps.append(
                    {
                        "step_id": spec.id,
                        "kind": "agent_subtask",
                        "target": spec.role.value,
                        "tool": result.agent or spec.role.value,
                        "status": status,
                        "verified": result.verified,
                        "error": result.error,
                        "attempts": result.attempts,
                        "duration_ms": result.duration_ms,
                    }
                )
            agents = [
                result.agent for result in task.result_list() if result.agent
            ]
            verification = {
                "passed": bool(task.aggregate.get("verified", False)),
                "status": "passed" if task.aggregate.get("verified") else "failed",
                "reason": str(task.aggregate.get("summary", "")),
                "source": "collaboration",
                "conflicts": len(task.aggregate.get("unresolved_conflicts", ())),
            }
            evidence = self._learning.capture(
                task.objective,
                task_kind=f"collaboration.{task.mode.value}",
                source="collaboration",
                route="collaboration",
                intent=task.mode.value,
                context={
                    "agents": sorted(set(agents)),
                    "mode": task.mode.value,
                    "task_id": task.id,
                    "required": task.aggregate.get("required", 0),
                    "verified_required": task.aggregate.get("verified_required", 0),
                },
                steps=steps,
                tools=sorted(set(agents)),
                verification=verification,
                errors=[
                    result.error
                    for result in task.result_list()
                    if result.error and not result.verified
                ],
                retries=sum(result.retries for result in task.result_list()),
                attempts=sum(result.attempts for result in task.result_list()),
                latency_ms=float(task.aggregate.get("timing", {}).get("wall_ms", 0.0) or 0.0),
                correlation_id=task.id,
                started_at=task.started_at,
                finished_at=task.finished_at,
            )
            recorded = self._learning.record(evidence)
            return {
                "captured": bool(recorded.get("recorded", False)),
                "experience_id": recorded.get("experience_id", ""),
                "outcome": recorded.get("outcome", ""),
                "verified": bool(recorded.get("verified", False)),
            }
        except Exception as exc:  # learning never fails the collaboration
            self._errors.append(f"learning: {type(exc).__name__}: {exc}")
            return {"captured": False, "reason": f"{type(exc).__name__}: {exc}"}

    async def _save(self) -> None:
        if self.policy.persist and self._store is not None:
            self.flush()

    def _save_sync(self) -> None:
        if self.policy.persist and self._store is not None:
            self.flush()

    def _task_or_raise(self, task_id: str) -> CollaborationTask:
        try:
            return self._tasks[str(task_id)]
        except KeyError as exc:
            raise KeyError(f"no collaboration task {task_id!r}") from exc


def _claim_topic(claim: Mapping[str, Any]) -> str:
    topic = str(claim.get("topic", "") or "").strip().lower()
    if topic:
        return topic
    text = re.sub(r"[^a-z0-9 ]+", " ", str(claim.get("text", "")).lower())
    words = [word for word in text.split() if word]
    return " ".join(words[:8])


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= max(1e-9, abs(right) * 1e-6)


def _contradicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_value = _number(left.get("value"))
    right_value = _number(right.get("value"))
    if left_value is not None and right_value is not None:
        return not _close(left_value, right_value)
    left_text = str(left.get("text", "")).strip().lower()
    right_text = str(right.get("text", "")).strip().lower()
    if not left_text or not right_text or left_text == right_text:
        return False
    left_words = set(re.findall(r"[a-z0-9]+", left_text))
    right_words = set(re.findall(r"[a-z0-9]+", right_text))
    if not left_words or not right_words:
        return False
    overlap = len(left_words & right_words) / len(left_words | right_words)
    # Only a factual-looking pair with high vocabulary overlap and an explicit
    # negation counts as a contradiction. Treating every paraphrase as a
    # conflict would make the report noise, and majority voting is not proof.
    if overlap < 0.5:
        return False
    left_negated = bool(re.search(r"\b(?:not|no|never|without|isn't|aren't|wasn't|weren't)\b", left_text))
    right_negated = bool(re.search(r"\b(?:not|no|never|without|isn't|aren't|wasn't|weren't)\b", right_text))
    return left_negated != right_negated
