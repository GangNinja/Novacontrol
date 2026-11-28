"""Skill domain models.

The original package held a small callable registry (``SkillSchema`` /
``SkillInvocation`` / ``SkillResult``), and that API is preserved here. Phase 31
adds the durable, versioned skill definition: a procedure extracted from
evidence, with declared inputs, tools, permissions, environments,
postconditions, provenance and an evidence-based reliability record.

Everything in this module is a value object. It performs no I/O, calls no model
and executes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Existing function-skill surface (kept for compatibility)
# ---------------------------------------------------------------------------


class SkillStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SkillSchema:
    name: str
    description: str
    input_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    skill_name: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class SkillResult:
    invocation_id: str
    skill_name: str
    status: SkillStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "skill_name": self.skill_name,
            "status": self.status.value,
            "output": dict(self.output),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Phase 31 vocabulary
# ---------------------------------------------------------------------------


class SkillLifecycle(StrEnum):
    """A skill's evidence state, never a promise about its quality."""

    ACQUIRED = "acquired"
    VALIDATED = "validated"
    ADAPTED = "adapted"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class SkillCategory(StrEnum):
    FILE = "file"
    RESEARCH = "research"
    DESKTOP = "desktop"
    BROWSER = "browser"
    DATA = "data"
    DOCUMENT = "document"
    GENERAL = "general"


class SkillStepKind(StrEnum):
    REQUEST = "request"
    TOOL = "tool"
    DESKTOP = "desktop"
    BROWSER = "browser"
    AGENT = "agent"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class SkillInput:
    name: str
    description: str = ""
    required: bool = True
    example: str = ""
    kind: str = "string"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A skill input needs a name.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "example": self.example,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SkillInput | None:
        if not isinstance(payload, Mapping):
            return None
        name = str(payload.get("name", "") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            description=str(payload.get("description", "") or ""),
            required=bool(payload.get("required", True)),
            example=str(payload.get("example", "") or ""),
            kind=str(payload.get("kind", "string") or "string"),
        )


@dataclass(frozen=True, slots=True)
class SkillStep:
    """One procedure step. ``instruction`` may contain ``{input}`` placeholders."""

    instruction: str
    kind: SkillStepKind = SkillStepKind.REQUEST
    tool: str = ""
    permission: str = ""
    environment: str = ""
    verify: bool = False
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise ValueError("A skill step needs an instruction.")

    @property
    def normalized_instruction(self) -> str:
        return " ".join(self.instruction.lower().split())

    def render(self, inputs: Mapping[str, Any]) -> str:
        if not inputs:
            return self.instruction
        try:
            return self.instruction.format_map(_SafeInputs(inputs))
        except (KeyError, IndexError, ValueError):
            return self.instruction

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "kind": self.kind.value,
            "tool": self.tool,
            "permission": self.permission,
            "environment": self.environment,
            "verify": self.verify,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SkillStep | None:
        if not isinstance(payload, Mapping):
            return None
        instruction = str(payload.get("instruction", "") or "").strip()
        if not instruction:
            return None
        kind = str(payload.get("kind", SkillStepKind.REQUEST.value) or "")
        try:
            step_kind = SkillStepKind(kind)
        except ValueError:
            step_kind = SkillStepKind.REQUEST
        return cls(
            instruction=instruction,
            kind=step_kind,
            tool=str(payload.get("tool", "") or ""),
            permission=str(payload.get("permission", "") or ""),
            environment=str(payload.get("environment", "") or ""),
            verify=bool(payload.get("verify", False)),
            optional=bool(payload.get("optional", False)),
        )


class _SafeInputs(dict[str, Any]):
    """Leave unknown placeholders untouched instead of raising during render."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True, slots=True)
class SkillReliability:
    """Evidence about what this version actually did, recomputed from evidence."""

    trials: int = 0
    successes: int = 0
    failures: int = 0
    corrections: int = 0
    transfers_ok: int = 0
    transfers_failed: int = 0
    total_duration_ms: float = 0.0

    def __post_init__(self) -> None:
        for value in (
            self.trials,
            self.successes,
            self.failures,
            self.corrections,
            self.transfers_ok,
            self.transfers_failed,
        ):
            if value < 0:
                raise ValueError("Skill reliability counters cannot be negative.")

    @property
    def success_rate(self) -> float:
        decided = self.successes + self.failures
        return round(self.successes / decided, 4) if decided else 0.0

    @property
    def transfer_rate(self) -> float:
        decided = self.transfers_ok + self.transfers_failed
        return round(self.transfers_ok / decided, 4) if decided else 0.0

    @property
    def average_duration_ms(self) -> float:
        return round(self.total_duration_ms / self.trials, 3) if self.trials else 0.0

    @property
    def confidence(self) -> float:
        """More evidence raises confidence; a failure lowers the rate."""
        if not self.trials:
            return 0.0
        support = min(1.0, self.trials / 5.0)
        return round(support * self.success_rate, 4)

    def record(
        self,
        *,
        verified: bool,
        duration_ms: float = 0.0,
        transfer: bool = False,
        corrected: bool = False,
    ) -> SkillReliability:
        return replace(
            self,
            trials=self.trials + 1,
            successes=self.successes + (1 if verified else 0),
            failures=self.failures + (0 if verified else 1),
            corrections=self.corrections + (1 if corrected else 0),
            transfers_ok=self.transfers_ok + (1 if transfer and verified else 0),
            transfers_failed=self.transfers_failed + (1 if transfer and not verified else 0),
            total_duration_ms=self.total_duration_ms + max(0.0, float(duration_ms)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trials": self.trials,
            "successes": self.successes,
            "failures": self.failures,
            "corrections": self.corrections,
            "transfers_ok": self.transfers_ok,
            "transfers_failed": self.transfers_failed,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "success_rate": self.success_rate,
            "transfer_rate": self.transfer_rate,
            "average_duration_ms": self.average_duration_ms,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> SkillReliability:
        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            trials=int(data.get("trials", 0) or 0),
            successes=int(data.get("successes", 0) or 0),
            failures=int(data.get("failures", 0) or 0),
            corrections=int(data.get("corrections", 0) or 0),
            transfers_ok=int(data.get("transfers_ok", 0) or 0),
            transfers_failed=int(data.get("transfers_failed", 0) or 0),
            total_duration_ms=float(data.get("total_duration_ms", 0.0) or 0.0),
        )


@dataclass(frozen=True, slots=True)
class Skill:
    """A reusable procedure with declared contract, provenance and evidence."""

    name: str
    objective: str
    steps: tuple[SkillStep, ...]
    description: str = ""
    category: SkillCategory = SkillCategory.GENERAL
    lifecycle: SkillLifecycle = SkillLifecycle.ACQUIRED
    version: int = 1
    inputs: tuple[SkillInput, ...] = ()
    outputs: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    environments: tuple[str, ...] = ("any",)
    limitations: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    reliability: SkillReliability = field(default_factory=SkillReliability)
    parent_version: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A skill needs a name.")
        if not self.objective.strip():
            raise ValueError("A skill needs the objective it serves.")
        if not self.steps:
            raise ValueError("A skill needs at least one procedure step.")
        if self.version < 1:
            raise ValueError("A skill version starts at 1.")
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("Skill input names must be unique.")

    @property
    def signature(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (step.kind.value, step.tool, step.normalized_instruction)
            for step in self.steps
        )

    @property
    def executable(self) -> bool:
        return self.lifecycle in (
            SkillLifecycle.VALIDATED,
            SkillLifecycle.ADAPTED,
        )

    @property
    def input_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.inputs)

    def required_inputs(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.inputs if item.required)

    def with_(self, **changes: Any) -> Skill:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "objective": self.objective,
            "category": self.category.value,
            "lifecycle": self.lifecycle.value,
            "version": self.version,
            "steps": [step.to_dict() for step in self.steps],
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": list(self.outputs),
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
            "dependencies": list(self.dependencies),
            "environments": list(self.environments),
            "limitations": list(self.limitations),
            "validation": list(self.validation),
            "provenance": list(self.provenance),
            "reliability": self.reliability.to_dict(),
            "parent_version": self.parent_version,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "metadata": dict(self.metadata),
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Skill | None:
        if not isinstance(payload, Mapping):
            return None
        try:
            steps = tuple(
                step
                for step in (
                    SkillStep.from_dict(item)
                    for item in payload.get("steps", ()) or ()
                )
                if step is not None
            )
            inputs = tuple(
                item
                for item in (
                    SkillInput.from_dict(row) for row in payload.get("inputs", ()) or ()
                )
                if item is not None
            )
            return cls(
                id=str(payload["id"]),
                name=str(payload["name"]),
                description=str(payload.get("description", "") or ""),
                objective=str(payload["objective"]),
                category=SkillCategory(str(payload.get("category", "general"))),
                lifecycle=SkillLifecycle(str(payload.get("lifecycle", "acquired"))),
                version=max(1, int(payload.get("version", 1))),
                steps=steps,
                inputs=inputs,
                outputs=tuple(str(item) for item in payload.get("outputs", ()) or ()),
                tools=tuple(str(item) for item in payload.get("tools", ()) or ()),
                permissions=tuple(
                    str(item) for item in payload.get("permissions", ()) or ()
                ),
                preconditions=tuple(
                    str(item) for item in payload.get("preconditions", ()) or ()
                ),
                postconditions=tuple(
                    str(item) for item in payload.get("postconditions", ()) or ()
                ),
                dependencies=tuple(
                    str(item) for item in payload.get("dependencies", ()) or ()
                ),
                environments=tuple(
                    str(item) for item in payload.get("environments", ()) or ()
                )
                or ("any",),
                limitations=tuple(
                    str(item) for item in payload.get("limitations", ()) or ()
                ),
                validation=tuple(
                    str(item) for item in payload.get("validation", ()) or ()
                ),
                provenance=tuple(
                    str(item) for item in payload.get("provenance", ()) or ()
                ),
                reliability=SkillReliability.from_dict(payload.get("reliability")),
                parent_version=int(payload.get("parent_version", 0) or 0),
                created_at=parse_dt(payload.get("created_at")) or utcnow(),
                updated_at=parse_dt(payload.get("updated_at")) or utcnow(),
                metadata=dict(payload.get("metadata") or {}),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class SkillEvidence:
    """One real execution used to acquire, validate or judge a skill."""

    skill_id: str
    version: int
    goal: str
    verified: bool
    environment: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple[Mapping[str, Any], ...] = ()
    duration_ms: float = 0.0
    error: str = ""
    source: str = "execution"
    created_at: datetime = field(default_factory=utcnow)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "version": self.version,
            "goal": self.goal,
            "verified": self.verified,
            "environment": self.environment,
            "inputs": dict(self.inputs),
            "steps": [dict(step) for step in self.steps],
            "duration_ms": round(self.duration_ms, 3),
            "error": self.error,
            "source": self.source,
            "created_at": iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SkillEvidence | None:
        if not isinstance(payload, Mapping):
            return None
        skill_id = str(payload.get("skill_id", "") or "")
        if not skill_id:
            return None
        return cls(
            id=str(payload.get("id", "") or uuid4().hex),
            skill_id=skill_id,
            version=int(payload.get("version", 1) or 1),
            goal=str(payload.get("goal", "") or ""),
            verified=bool(payload.get("verified", False)),
            environment=str(payload.get("environment", "") or ""),
            inputs=dict(payload.get("inputs") or {}),
            steps=tuple(
                dict(step)
                for step in payload.get("steps", ()) or ()
                if isinstance(step, Mapping)
            ),
            duration_ms=float(payload.get("duration_ms", 0.0) or 0.0),
            error=str(payload.get("error", "") or ""),
            source=str(payload.get("source", "execution") or "execution"),
            created_at=parse_dt(payload.get("created_at")) or utcnow(),
        )


@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill_id: str
    name: str
    version: int
    score: float
    lifecycle: SkillLifecycle
    reasons: tuple[str, ...] = ()
    compatible: bool = True
    missing_inputs: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()

    def with_(self, **changes: Any) -> SkillMatch:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "score": round(self.score, 4),
            "lifecycle": self.lifecycle.value,
            "reasons": list(self.reasons),
            "compatible": self.compatible,
            "missing_inputs": list(self.missing_inputs),
            "missing_capabilities": list(self.missing_capabilities),
        }


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What an injected executor observed for one step."""

    verified: bool
    detail: str = ""
    evidence: tuple[str, ...] = ()
    error: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillExecution:
    skill_id: str
    version: int
    goal: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    environment: str = ""
    status: str = "pending"
    verified: bool = False
    completed_steps: tuple[int, ...] = ()
    results: tuple[Mapping[str, Any], ...] = ()
    outputs: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    adaptation: bool = False
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def resumable(self) -> bool:
        return bool(self.completed_steps) and self.status in ("failed", "interrupted")

    def with_(self, **changes: Any) -> SkillExecution:
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "version": self.version,
            "goal": self.goal,
            "inputs": dict(self.inputs),
            "environment": self.environment,
            "status": self.status,
            "verified": self.verified,
            "completed_steps": list(self.completed_steps),
            "results": [dict(row) for row in self.results],
            "outputs": dict(self.outputs),
            "error": self.error,
            "adaptation": self.adaptation,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SkillExecution | None:
        if not isinstance(payload, Mapping):
            return None
        skill_id = str(payload.get("skill_id", "") or "")
        if not skill_id:
            return None
        return cls(
            id=str(payload.get("id", "") or uuid4().hex),
            skill_id=skill_id,
            version=int(payload.get("version", 1) or 1),
            goal=str(payload.get("goal", "") or ""),
            inputs=dict(payload.get("inputs") or {}),
            environment=str(payload.get("environment", "") or ""),
            status=str(payload.get("status", "pending") or "pending"),
            verified=bool(payload.get("verified", False)),
            completed_steps=tuple(
                int(item) for item in payload.get("completed_steps", ()) or ()
            ),
            results=tuple(
                dict(row)
                for row in payload.get("results", ()) or ()
                if isinstance(row, Mapping)
            ),
            outputs=dict(payload.get("outputs") or {}),
            error=str(payload.get("error", "") or ""),
            adaptation=bool(payload.get("adaptation", False)),
            started_at=parse_dt(payload.get("started_at")) or utcnow(),
            finished_at=parse_dt(payload.get("finished_at")),
        )


@dataclass(frozen=True, slots=True)
class Compatibility:
    compatible: bool
    reasons: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    changed_inputs: tuple[str, ...] = ()
    environment_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "reasons": list(self.reasons),
            "missing_inputs": list(self.missing_inputs),
            "missing_capabilities": list(self.missing_capabilities),
            "changed_inputs": list(self.changed_inputs),
            "environment_changed": self.environment_changed,
        }


@dataclass(frozen=True, slots=True)
class AdaptationResult:
    source_skill_id: str
    source_version: int
    compatible: bool
    skill: Skill | None = None
    reasons: tuple[str, ...] = ()
    changes: tuple[Mapping[str, Any], ...] = ()
    missing_inputs: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    validation_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_skill_id": self.source_skill_id,
            "source_version": self.source_version,
            "compatible": self.compatible,
            "skill": self.skill.to_dict() if self.skill is not None else None,
            "reasons": list(self.reasons),
            "changes": [dict(change) for change in self.changes],
            "missing_inputs": list(self.missing_inputs),
            "missing_capabilities": list(self.missing_capabilities),
            "validation_required": self.validation_required,
        }


@dataclass(frozen=True, slots=True)
class CompositionResult:
    objective: str
    compatible: bool
    ordered_skill_ids: tuple[str, ...] = ()
    plan: tuple[Mapping[str, Any], ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "compatible": self.compatible,
            "ordered_skill_ids": list(self.ordered_skill_ids),
            "plan": [dict(row) for row in self.plan],
            "conflicts": list(self.conflicts),
            "missing_inputs": list(self.missing_inputs),
            "missing_capabilities": list(self.missing_capabilities),
            "permissions": list(self.permissions),
        }


@dataclass(frozen=True, slots=True)
class SkillObservation:
    """One completed execution offered as evidence for acquisition."""

    goal: str
    steps: tuple[SkillStep, ...]
    verified: bool
    environment: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    duration_ms: float = 0.0
    source: str = "execution"
    agent: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    created: bool
    skill: Skill | None = None
    support: int = 0
    reasons: tuple[str, ...] = ()
    rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "skill": self.skill.to_dict() if self.skill is not None else None,
            "support": self.support,
            "reasons": list(self.reasons),
            "rejected": self.rejected,
        }


def skill_tokens(text: str) -> set[str]:
    """Small deterministic tokenizer shared by retrieval and similarity."""
    cleaned = "".join(
        character.lower() if (character.isalnum() or character == "_") else " "
        for character in str(text or "")
    )
    return {token for token in cleaned.split() if len(token) > 1}


def similarity(left: str, right: str) -> float:
    a = skill_tokens(left)
    b = skill_tokens(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def unique_sorted(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
