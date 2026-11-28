"""Data models for Phase 27 — continual learning and self-improvement.

Everything here is a RECORD, not an executor. The vocabulary exists so that an
experience can be captured once, assessed against actual verification evidence,
and later retrieved by a task that resembles it — without any of these classes
being able to run a tool, call a model, or change a strategy by themselves.

Two rules shape the models:

* **An outcome is not a field a caller may invent.** ``ExecutionEvidence.outcome``
  starts empty and is filled by ``continual.evaluate.OutcomeEvaluator`` from the
  evidence the execution actually produced. ``assessed`` says whether that has
  happened, and a record that has never been assessed cannot support a strategy.
* **A strategy is DATA.** A ``StrategyRecord`` is an ordered list of step
  descriptors plus a tool policy — a plan template a planner may consult, never
  code that runs. Promoting one moves a version number; it can never edit the
  build, a permission, or a safety rule.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence

__all__ = [
    "SCHEMA_VERSION",
    "OutcomeClass",
    "StepStatus",
    "SignalKind",
    "StrategyStatus",
    "ProposalStatus",
    "StrategyKind",
    "StepEvidence",
    "ExecutionEvidence",
    "OutcomeAssessment",
    "LearningSignal",
    "StrategyStats",
    "StrategyRecord",
    "LearningProposal",
    "Recommendation",
    "RetrievalHit",
    "new_id",
    "now_iso",
    "fingerprint_for",
    "normalize_goal",
    "clamp01",
]

#: Bumped whenever a stored record's meaning changes. A record written by another
#: schema is skipped on restore with a reason, never reinterpreted.
SCHEMA_VERSION = "phase27.1"

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
        "please", "can", "you", "me", "my", "i", "it", "that", "this", "is",
        "are", "be", "do", "does", "then", "now", "just", "some", "any",
    }
)


def clamp01(value: float) -> float:
    """Clamp a confidence-like number into [0, 1]; NaN reads as 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def now_iso() -> str:
    """UTC timestamp with a ``Z`` suffix — the same shape the audit trail uses."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:16]}"


def normalize_goal(text: str) -> str:
    """A stable, lower-case token string used for similarity and fingerprints.

    Punctuation is dropped rather than rewritten, and the tokens are sorted so
    "open chrome and search cats" and "search cats and open chrome" share a
    signature. Word ORDER is not lost to the caller: the original goal is kept
    beside this value, and retrieval also compares the raw tokens in order.
    """
    words = [word for word in _WORD.findall(str(text or "").lower()) if word not in _STOPWORDS]
    return " ".join(words)


def fingerprint_for(
    *,
    goal: str,
    task_kind: str,
    route: str = "",
    tools: Sequence[str] = (),
    context: Mapping[str, Any] | None = None,
) -> str:
    """Deterministic identity for one logical task, used for de-duplication.

    Two executions with the same goal, kind, route, tool set and *compatible*
    context are the same task attempted twice — the second attempt must be
    learnable from the first, not a silently duplicated row. Device is part of
    the key on purpose: a phone command and a desktop command with the same
    words are different tasks and must never be averaged together.
    """
    context = context or {}
    parts = [
        normalize_goal(goal),
        str(task_kind or "").strip().lower(),
        str(route or "").strip().lower(),
        ",".join(sorted({str(tool).strip().lower() for tool in tools if str(tool).strip()})),
        str(context.get("device", "") or "").strip().lower(),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


class OutcomeClass(StrEnum):
    """How an execution actually ended — never how it looked.

    ``UNVERIFIED`` is a first-class outcome: work happened, but nothing
    independent confirmed it. Treating it as success is exactly the failure this
    phase exists to prevent, so it is never folded into ``SUCCESS``.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    RECOVERED = "recovered"
    USER_CORRECTED = "user_corrected"
    UNVERIFIED = "unverified"


class StepStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    DENIED = "denied"
    UNKNOWN = "unknown"


class SignalKind(StrEnum):
    TOOL_RELIABILITY = "tool_reliability"
    PLAN_MISTAKE = "plan_mistake"
    RECOVERY_EFFECTIVENESS = "recovery_effectiveness"
    USER_CORRECTION = "user_correction"
    LATENCY = "latency"
    REPEAT_PATTERN = "repeat_pattern"


class StrategyStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class StrategyKind(StrEnum):
    PLAN_TEMPLATE = "plan_template"
    TOOL_POLICY = "tool_policy"


@dataclass(slots=True)
class StepEvidence:
    """One step of an execution, with what was observed about it."""

    step_id: str
    kind: str = ""
    target: str = ""
    tool: str = ""
    status: str = StepStatus.UNKNOWN.value
    verified: bool | None = None
    error: str = ""
    attempts: int = 1
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "target": self.target,
            "tool": self.tool,
            "status": self.status,
            "verified": self.verified,
            "error": self.error,
            "attempts": int(self.attempts),
            "duration_ms": float(self.duration_ms),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StepEvidence | None":
        if not isinstance(data, Mapping):
            return None
        step_id = str(data.get("step_id", "") or "").strip()
        if not step_id:
            return None
        status = str(data.get("status", StepStatus.UNKNOWN.value) or "").strip().lower()
        if status not in {item.value for item in StepStatus}:
            status = StepStatus.UNKNOWN.value
        verified = data.get("verified")
        if verified is not None and not isinstance(verified, bool):
            verified = None
        attempts = data.get("attempts", 1)
        duration = data.get("duration_ms", 0.0)
        return cls(
            step_id=step_id,
            kind=str(data.get("kind", "") or ""),
            target=str(data.get("target", "") or ""),
            tool=str(data.get("tool", "") or ""),
            status=status,
            verified=verified,
            error=str(data.get("error", "") or ""),
            attempts=_non_negative_int(attempts),
            duration_ms=_non_negative_float(duration),
        )

    @property
    def counts_as_done(self) -> bool:
        return self.status == StepStatus.COMPLETED.value

    @property
    def counts_as_failed(self) -> bool:
        return self.status in {StepStatus.FAILED.value, StepStatus.DENIED.value}


@dataclass(slots=True)
class ExecutionEvidence:
    """One task execution, captured from actual evidence.

    This is deliberately a *record*, not a mutable event: it is written once,
    assessed once, and afterwards only a person's feedback may change its
    outcome. Nothing here is inferred from the response text.
    """

    experience_id: str
    goal: str
    normalized_goal: str = ""
    task_kind: str = "general"
    source: str = "request"
    route: str = ""
    intent: str = ""
    strategy_id: str = ""
    strategy_version: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    steps: list[StepEvidence] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    recoveries: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    user_feedback: str = ""
    feedback_verdict: str = ""
    outcome: str = ""
    outcome_confidence: float = 0.0
    outcome_reasons: list[str] = field(default_factory=list)
    completion_ratio: float = 0.0
    verified: bool = False
    started_at: str = ""
    finished_at: str = ""
    latency_ms: float = 0.0
    retries: int = 0
    attempts: int = 1
    correlation_id: str = ""
    fingerprint: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.normalized_goal:
            self.normalized_goal = normalize_goal(self.goal)
        if not self.fingerprint:
            self.fingerprint = fingerprint_for(
                goal=self.goal,
                task_kind=self.task_kind,
                route=self.route,
                tools=self.tools or [step.tool for step in self.steps],
                context=self.context,
            )
        if not self.started_at:
            self.started_at = now_iso()
        if not self.finished_at:
            self.finished_at = self.started_at
        # Keep the tool list consistent with the steps: a tool a step names is a
        # tool this execution used, whether the caller remembered to list it.
        known = {str(tool).strip().lower() for tool in self.tools if str(tool).strip()}
        for step in self.steps:
            name = str(step.tool or "").strip().lower()
            if name and name not in known:
                self.tools.append(name)
                known.add(name)

    # -- counters ---------------------------------------------------------

    @property
    def assessed(self) -> bool:
        return bool(self.outcome)

    @property
    def verified_outcome(self) -> bool:
        return self.outcome in {OutcomeClass.SUCCESS.value, OutcomeClass.RECOVERED.value} and self.verified

    @property
    def failed_steps(self) -> list[StepEvidence]:
        return [step for step in self.steps if step.counts_as_failed]

    @property
    def completed_steps(self) -> list[StepEvidence]:
        return [step for step in self.steps if step.counts_as_done]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experience_id": self.experience_id,
            "goal": self.goal,
            "normalized_goal": self.normalized_goal,
            "task_kind": self.task_kind,
            "source": self.source,
            "route": self.route,
            "intent": self.intent,
            "strategy_id": self.strategy_id,
            "strategy_version": int(self.strategy_version),
            "context": dict(self.context),
            "steps": [step.to_dict() for step in self.steps],
            "tools": list(self.tools),
            "errors": list(self.errors),
            "recoveries": list(self.recoveries),
            "verification": dict(self.verification),
            "user_feedback": self.user_feedback,
            "feedback_verdict": self.feedback_verdict,
            "outcome": self.outcome,
            "outcome_confidence": float(self.outcome_confidence),
            "outcome_reasons": list(self.outcome_reasons),
            "completion_ratio": float(self.completion_ratio),
            "verified": bool(self.verified),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency_ms": float(self.latency_ms),
            "retries": int(self.retries),
            "attempts": int(self.attempts),
            "correlation_id": self.correlation_id,
            "fingerprint": self.fingerprint,
            "assessed": self.assessed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionEvidence | None":
        """Rebuild a record, or ``None`` when it is not one this build reads.

        A record from another schema, without an id or without a goal, is not
        guessed at: the restore reports it as skipped. Losing one corrupt row is
        the cost of never treating corrupt evidence as established knowledge.
        """
        if not isinstance(data, Mapping):
            return None
        if str(data.get("schema_version", "")) != SCHEMA_VERSION:
            return None
        experience_id = str(data.get("experience_id", "") or "").strip()
        goal = str(data.get("goal", "") or "").strip()
        if not experience_id or not goal:
            return None
        outcome = str(data.get("outcome", "") or "").strip().lower()
        if outcome and outcome not in {item.value for item in OutcomeClass}:
            return None
        steps: list[StepEvidence] = []
        for raw in data.get("steps", []) or []:
            step = StepEvidence.from_dict(raw) if isinstance(raw, Mapping) else None
            if step is not None:
                steps.append(step)
        verification = data.get("verification")
        if not isinstance(verification, Mapping):
            verification = {}
        context = data.get("context")
        if not isinstance(context, Mapping):
            context = {}
        return cls(
            experience_id=experience_id,
            goal=goal,
            normalized_goal=str(data.get("normalized_goal", "") or ""),
            task_kind=str(data.get("task_kind", "general") or "general"),
            source=str(data.get("source", "request") or "request"),
            route=str(data.get("route", "") or ""),
            intent=str(data.get("intent", "") or ""),
            strategy_id=str(data.get("strategy_id", "") or ""),
            strategy_version=_non_negative_int(data.get("strategy_version", 0)),
            context={str(key): value for key, value in context.items()},
            steps=steps,
            tools=[str(tool) for tool in data.get("tools", []) or [] if str(tool).strip()],
            errors=[str(item) for item in data.get("errors", []) or []],
            recoveries=[str(item) for item in data.get("recoveries", []) or []],
            verification={str(key): value for key, value in verification.items()},
            user_feedback=str(data.get("user_feedback", "") or ""),
            feedback_verdict=str(data.get("feedback_verdict", "") or ""),
            outcome=outcome,
            outcome_confidence=clamp01(data.get("outcome_confidence", 0.0)),
            outcome_reasons=[str(item) for item in data.get("outcome_reasons", []) or []],
            completion_ratio=clamp01(data.get("completion_ratio", 0.0)),
            verified=bool(data.get("verified", False)),
            started_at=str(data.get("started_at", "") or ""),
            finished_at=str(data.get("finished_at", "") or ""),
            latency_ms=_non_negative_float(data.get("latency_ms", 0.0)),
            retries=_non_negative_int(data.get("retries", 0)),
            attempts=max(1, _non_negative_int(data.get("attempts", 1))),
            correlation_id=str(data.get("correlation_id", "") or ""),
            fingerprint=str(data.get("fingerprint", "") or ""),
        )


@dataclass(slots=True)
class OutcomeAssessment:
    """The evaluator's verdict, with the reasons that produced it."""

    outcome: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    completion_ratio: float = 0.0
    verified: bool = False
    verification_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "confidence": round(clamp01(self.confidence), 3),
            "reasons": list(self.reasons),
            "completion_ratio": round(clamp01(self.completion_ratio), 3),
            "verified": bool(self.verified),
            "verification_source": self.verification_source,
        }


@dataclass(slots=True)
class LearningSignal:
    """A statement derived from evidence, with the evidence that supports it."""

    signal_id: str
    kind: str
    subject: str
    task_kind: str = ""
    statement: str = ""
    direction: str = "note"  # prefer | avoid | note
    support: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "kind": self.kind,
            "subject": self.subject,
            "task_kind": self.task_kind,
            "statement": self.statement,
            "direction": self.direction,
            "support": int(self.support),
            "evidence_ids": list(self.evidence_ids),
            "confidence": round(clamp01(self.confidence), 3),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LearningSignal | None":
        if not isinstance(data, Mapping):
            return None
        signal_id = str(data.get("signal_id", "") or "").strip()
        if not signal_id:
            return None
        kind = str(data.get("kind", "") or "").strip().lower()
        if kind not in {item.value for item in SignalKind}:
            return None
        return cls(
            signal_id=signal_id,
            kind=kind,
            subject=str(data.get("subject", "") or ""),
            task_kind=str(data.get("task_kind", "") or ""),
            statement=str(data.get("statement", "") or ""),
            direction=str(data.get("direction", "note") or "note"),
            support=_non_negative_int(data.get("support", 0)),
            evidence_ids=[str(item) for item in data.get("evidence_ids", []) or []],
            confidence=clamp01(data.get("confidence", 0.0)),
            created_at=str(data.get("created_at", "") or now_iso()),
        )


@dataclass(slots=True)
class StrategyStats:
    """What actually happened when this strategy was used.

    ``unverified`` is counted separately and never enters the success rate: a run
    nobody could confirm says nothing about whether the strategy works.
    """

    trials: int = 0
    success: int = 0
    recovered: int = 0
    partial: int = 0
    failure: int = 0
    unverified: int = 0
    corrections: int = 0
    retries_total: int = 0
    latency_ms_total: float = 0.0

    @property
    def verified_trials(self) -> int:
        return self.success + self.recovered

    @property
    def decided_trials(self) -> int:
        return self.success + self.recovered + self.partial + self.failure

    @property
    def success_rate(self) -> float:
        decided = self.decided_trials
        return (self.success + self.recovered) / decided if decided else 0.0

    @property
    def average_latency_ms(self) -> float:
        return self.latency_ms_total / self.trials if self.trials else 0.0

    def record(self, evidence: ExecutionEvidence) -> None:
        self.trials += 1
        outcome = evidence.outcome
        if outcome == OutcomeClass.SUCCESS.value:
            self.success += 1
        elif outcome == OutcomeClass.RECOVERED.value:
            self.recovered += 1
        elif outcome == OutcomeClass.PARTIAL.value:
            self.partial += 1
        elif outcome == OutcomeClass.FAILURE.value:
            self.failure += 1
        elif outcome == OutcomeClass.USER_CORRECTED.value:
            self.partial += 1
            self.corrections += 1
        else:
            self.unverified += 1
        self.retries_total += max(0, int(evidence.retries))
        self.latency_ms_total += max(0.0, float(evidence.latency_ms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "success": self.success,
            "recovered": self.recovered,
            "partial": self.partial,
            "failure": self.failure,
            "unverified": self.unverified,
            "corrections": self.corrections,
            "retries_total": self.retries_total,
            "latency_ms_total": round(self.latency_ms_total, 3),
            "verified_trials": self.verified_trials,
            "decided_trials": self.decided_trials,
            "success_rate": round(self.success_rate, 4),
            "average_latency_ms": round(self.average_latency_ms, 3),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyStats":
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            trials=_non_negative_int(data.get("trials", 0)),
            success=_non_negative_int(data.get("success", 0)),
            recovered=_non_negative_int(data.get("recovered", 0)),
            partial=_non_negative_int(data.get("partial", 0)),
            failure=_non_negative_int(data.get("failure", 0)),
            unverified=_non_negative_int(data.get("unverified", 0)),
            corrections=_non_negative_int(data.get("corrections", 0)),
            retries_total=_non_negative_int(data.get("retries_total", 0)),
            latency_ms_total=_non_negative_float(data.get("latency_ms_total", 0.0)),
        )


@dataclass(slots=True)
class StrategyRecord:
    """One version of a reusable strategy, with its provenance and performance."""

    strategy_id: str
    task_kind: str
    name: str
    version: int
    status: str = StrategyStatus.PROPOSED.value
    kind: str = StrategyKind.PLAN_TEMPLATE.value
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    avoid_tools: list[str] = field(default_factory=list)
    retry_budget: int = 0
    verification_required: bool = True
    provenance: list[str] = field(default_factory=list)
    stats: StrategyStats = field(default_factory=StrategyStats)
    parent_version: int = 0
    created_at: str = field(default_factory=now_iso)
    promoted_at: str = ""
    decided_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "task_kind": self.task_kind,
            "name": self.name,
            "version": int(self.version),
            "status": self.status,
            "kind": self.kind,
            "steps": [dict(step) for step in self.steps],
            "tool_order": list(self.tool_order),
            "avoid_tools": list(self.avoid_tools),
            "retry_budget": int(self.retry_budget),
            "verification_required": bool(self.verification_required),
            "provenance": list(self.provenance),
            "stats": self.stats.to_dict(),
            "parent_version": int(self.parent_version),
            "created_at": self.created_at,
            "promoted_at": self.promoted_at,
            "decided_reason": self.decided_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyRecord | None":
        if not isinstance(data, Mapping):
            return None
        strategy_id = str(data.get("strategy_id", "") or "").strip()
        task_kind = str(data.get("task_kind", "") or "").strip()
        if not strategy_id or not task_kind:
            return None
        status = str(data.get("status", "") or "").strip().lower()
        if status not in {item.value for item in StrategyStatus}:
            return None
        kind = str(data.get("kind", StrategyKind.PLAN_TEMPLATE.value) or "").strip().lower()
        if kind not in {item.value for item in StrategyKind}:
            kind = StrategyKind.PLAN_TEMPLATE.value
        steps = [dict(step) for step in data.get("steps", []) or [] if isinstance(step, Mapping)]
        return cls(
            strategy_id=strategy_id,
            task_kind=task_kind,
            name=str(data.get("name", "") or f"{task_kind} strategy"),
            version=max(1, _non_negative_int(data.get("version", 1))),
            status=status,
            kind=kind,
            steps=steps,
            tool_order=[str(item) for item in data.get("tool_order", []) or []],
            avoid_tools=[str(item) for item in data.get("avoid_tools", []) or []],
            retry_budget=_non_negative_int(data.get("retry_budget", 0)),
            verification_required=bool(data.get("verification_required", True)),
            provenance=[str(item) for item in data.get("provenance", []) or []],
            stats=StrategyStats.from_dict(data.get("stats", {})),
            parent_version=_non_negative_int(data.get("parent_version", 0)),
            created_at=str(data.get("created_at", "") or now_iso()),
            promoted_at=str(data.get("promoted_at", "") or ""),
            decided_reason=str(data.get("decided_reason", "") or ""),
        )


@dataclass(slots=True)
class LearningProposal:
    """A proposed change, separated from anything already validated or active."""

    proposal_id: str
    kind: str
    task_kind: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    support: int = 0
    confidence: float = 0.0
    status: str = ProposalStatus.PROPOSED.value
    risk: str = "low"
    reasons: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    strategy_id: str = ""
    created_at: str = field(default_factory=now_iso)
    decided_at: str = ""
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "task_kind": self.task_kind,
            "summary": self.summary,
            "payload": dict(self.payload),
            "evidence_ids": list(self.evidence_ids),
            "support": int(self.support),
            "confidence": round(clamp01(self.confidence), 3),
            "status": self.status,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "validation": dict(self.validation),
            "strategy_id": self.strategy_id,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision_reason": self.decision_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LearningProposal | None":
        if not isinstance(data, Mapping):
            return None
        proposal_id = str(data.get("proposal_id", "") or "").strip()
        task_kind = str(data.get("task_kind", "") or "").strip()
        if not proposal_id or not task_kind:
            return None
        status = str(data.get("status", "") or "").strip().lower()
        if status not in {item.value for item in ProposalStatus}:
            return None
        payload = data.get("payload")
        validation = data.get("validation")
        return cls(
            proposal_id=proposal_id,
            kind=str(data.get("kind", StrategyKind.PLAN_TEMPLATE.value) or ""),
            task_kind=task_kind,
            summary=str(data.get("summary", "") or ""),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            evidence_ids=[str(item) for item in data.get("evidence_ids", []) or []],
            support=_non_negative_int(data.get("support", 0)),
            confidence=clamp01(data.get("confidence", 0.0)),
            status=status,
            risk=str(data.get("risk", "low") or "low"),
            reasons=[str(item) for item in data.get("reasons", []) or []],
            validation=dict(validation) if isinstance(validation, Mapping) else {},
            strategy_id=str(data.get("strategy_id", "") or ""),
            created_at=str(data.get("created_at", "") or now_iso()),
            decided_at=str(data.get("decided_at", "") or ""),
            decision_reason=str(data.get("decision_reason", "") or ""),
        )


@dataclass(slots=True)
class Recommendation:
    """What previous evidence suggests for this task — advisory, never forced.

    ``source`` says where the suggestion came from. Only a validated, ACTIVE
    strategy may ship step templates; a weaker signal may ship a tool preference
    or a retry budget at most. ``compatible`` is False when the retrieving task
    differs in a way the evidence cannot cover (another device, another app), and
    then the caller must ignore the suggestion.
    """

    task_kind: str = ""
    strategy_id: str = ""
    strategy_version: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    avoid_tools: list[str] = field(default_factory=list)
    prefer_tools: list[str] = field(default_factory=list)
    retry_budget: int = 0
    verification_required: bool = True
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    compatible: bool = True
    adapt_targets: bool = False
    reason: str = ""
    source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "strategy_id": self.strategy_id,
            "strategy_version": int(self.strategy_version),
            "steps": [dict(step) for step in self.steps],
            "avoid_tools": list(self.avoid_tools),
            "prefer_tools": list(self.prefer_tools),
            "retry_budget": int(self.retry_budget),
            "verification_required": bool(self.verification_required),
            "confidence": round(clamp01(self.confidence), 3),
            "evidence_ids": list(self.evidence_ids),
            "compatible": bool(self.compatible),
            "adapt_targets": bool(self.adapt_targets),
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(slots=True)
class RetrievalHit:
    """One comparable previous execution and why it was considered relevant."""

    experience_id: str
    score: float
    outcome: str
    goal: str
    fingerprint: str
    compatible: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "score": round(clamp01(self.score), 4),
            "outcome": self.outcome,
            "goal": self.goal,
            "fingerprint": self.fingerprint,
            "compatible": bool(self.compatible),
            "reason": self.reason,
        }


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _non_negative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return max(0.0, number)
