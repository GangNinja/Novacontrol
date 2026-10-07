"""Phase 19's records: verification results, critiques, corrections and datasets.

Phase 15 records what NovaControl DID. Phase 16 learns from it; Phase 17 learns
which of two behaviours a person or an evaluation preferred; Phase 18 turns
feedback into a reward. This module records the two things Phase 19 adds:

**Verification** — what an OBJECTIVE check said about one observable outcome.
A :class:`VerificationResult` is the answer to a question with a checkable
answer: did the file exist, did the process exit zero, did the tests pass, did
the response carry the required fields. Its evidence is the observable fact it
read (a hash, an exit code, a status line), never a model's opinion.

**Critique** — a structured, evidence-based statement about what failed and how
it could be corrected. A :class:`CritiqueResult` names a category, a severity, a
failed component, the observed and expected behaviour, and an optional
correction. It deliberately has no field for private reasoning: a critique is
the SHAPE of the failure, not the narrator's stream of thought, and there is
nowhere to put one.

Both are immutable, versioned records. Provenance is part of the value: a result
from a deterministic verifier, from a rule, from a person or from an AI evaluator
are four different things and carry their source so a reader can tell them apart
— and so a reward built on a deterministic verifier can outweigh one built on an
opinion.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.evaluation.models import now_iso

#: The vocabulary version of this phase's records.
RLVR_VERSION = "phase19.1"

VERIFICATION_SCHEMA_VERSION = 1
CRITIQUE_SCHEMA_VERSION = 1
CORRECTION_SCHEMA_VERSION = 1
CRITIQUE_DATASET_SCHEMA_VERSION = 1

#: Evidence kinds a verifier or a reward may cite. Plain strings, because a
#: deployment may add its own observable fact without a schema change.
EVIDENCE_FILE_EXISTS = "file_exists"
EVIDENCE_FILE_CONTENT = "file_content"
EVIDENCE_FILE_HASH = "file_hash"
EVIDENCE_PROCESS_STATE = "process_state"
EVIDENCE_EXIT_CODE = "exit_code"
EVIDENCE_TESTS_PASSED = "tests_passed"
EVIDENCE_TEST_RESULT = "test_result"
EVIDENCE_HTTP_STATUS = "http_status"
EVIDENCE_RESPONSE_SCHEMA = "response_schema"
EVIDENCE_DATABASE_ROWS = "database_rows"
EVIDENCE_GIT_STATE = "git_state"
EVIDENCE_OUTPUT_MATCH = "output_match"
EVIDENCE_SCHEMA_VALID = "schema_valid"
EVIDENCE_CUSTOM_CHECK = "custom_check"
EVIDENCE_VERIFIER_DISABLED = "verifier_disabled"
EVIDENCE_EXPECTED_TAMPERED = "expected_result_tampered"
EVIDENCE_VERIFIER_TAMPERED = "verifier_tampered"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _flag(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _whole(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (list, tuple)):
        return tuple(_mapping(item) for item in value if isinstance(item, Mapping))
    return ()


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


# ── vocabularies ────────────────────────────────────────────────────────────


class VerificationStatus(StrEnum):
    """What a verifier decided about one observable outcome.

    The five statuses are deliberately not a boolean. ``PARTIAL`` exists because
    a task with five independently checkable subtasks can genuinely be four
    fifths done, and forcing that into pass/fail throws away the signal the
    phase exists to collect. ``INCONCLUSIVE`` is what a verifier says when the
    observation it was handed does not contain the fact it was asked to check —
    ''nothing was measured'' is a status, not a failure of the run.
    """

    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


VERIFICATION_STATUSES: tuple[str, ...] = tuple(
    member.value for member in VerificationStatus
)


class VerifierCategory(StrEnum):
    """What kind of observable fact a verifier reads."""

    FILE = "file"
    PROCESS = "process"
    TEST = "test"
    HTTP = "http"
    DATABASE = "database"
    GIT = "git"
    OUTPUT = "output"
    SCHEMA = "schema"
    CUSTOM = "custom"


VERIFIER_CATEGORIES: tuple[str, ...] = tuple(member.value for member in VerifierCategory)


class VerificationScope(StrEnum):
    """Where in a task a verification was taken: a step, a subtask, the task."""

    STEP = "step"
    SUBTASK = "subtask"
    TASK = "task"


VERIFICATION_SCOPES: tuple[str, ...] = tuple(member.value for member in VerificationScope)


class RiskLevel(StrEnum):
    """How much a verifier's subject can affect the real world.

    Only used for metadata and for the security gate: a verifier that reads a
    file is not the same risk as one that checks a destructive command's exit
    code, and a report should be able to say which it was.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


RISK_LEVELS: tuple[str, ...] = tuple(member.value for member in RiskLevel)


class CritiqueCategory(StrEnum):
    """What kind of observable failure a critique names."""

    PLANNING_ERROR = "planning_error"
    TOOL_SELECTION_ERROR = "tool_selection_error"
    ARGUMENT_ERROR = "argument_error"
    EXECUTION_ERROR = "execution_error"
    VERIFICATION_ERROR = "verification_error"
    RECOVERY_ERROR = "recovery_error"
    SAFETY_ERROR = "safety_error"
    EFFICIENCY_ISSUE = "efficiency_issue"
    OUTPUT_FORMAT_ERROR = "output_format_error"
    CONTEXT_ERROR = "context_error"
    OTHER = "other"


CRITIQUE_CATEGORIES: tuple[str, ...] = tuple(member.value for member in CritiqueCategory)


class CritiqueSeverity(StrEnum):
    """How much a critique should affect learning."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


CRITIQUE_SEVERITIES: tuple[str, ...] = tuple(member.value for member in CritiqueSeverity)

#: Severity ordering, so a filter can say "only HIGH and above".
SEVERITY_RANK: Mapping[str, int] = {
    CritiqueSeverity.LOW.value: 0,
    CritiqueSeverity.MEDIUM.value: 1,
    CritiqueSeverity.HIGH.value: 2,
    CritiqueSeverity.CRITICAL.value: 3,
}


class CritiqueSource(StrEnum):
    """Where a critique came from. Verifier evidence outranks opinion."""

    VERIFIER = "verifier"
    RULE_BASED = "rule_based"
    HUMAN = "human"
    AI_EVALUATOR = "ai_evaluator"
    SYSTEM = "system"
    COMPOSITE = "composite"


CRITIQUE_SOURCES: tuple[str, ...] = tuple(member.value for member in CritiqueSource)

#: The trust ordering as numbers. A composite keeps the strongest reading it saw.
CRITIQUE_SOURCE_STRENGTH: Mapping[str, float] = {
    CritiqueSource.VERIFIER.value: 1.0,
    CritiqueSource.COMPOSITE.value: 0.9,
    CritiqueSource.RULE_BASED.value: 0.8,
    CritiqueSource.HUMAN.value: 0.7,
    CritiqueSource.SYSTEM.value: 0.6,
    CritiqueSource.AI_EVALUATOR.value: 0.5,
}


class CorrectionStatus(StrEnum):
    """Whether a correction has enough evidence to be learned from."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


CORRECTION_STATUSES: tuple[str, ...] = tuple(member.value for member in CorrectionStatus)


class CritiqueExampleKind(StrEnum):
    """The three dataset shapes a critique can produce, plus the correction."""

    ACTION = "action"
    PLAN = "plan"
    TOOL = "tool"
    RESPONSE = "response"
    CORRECTION = "correction"


CRITIQUE_EXAMPLE_KINDS: tuple[str, ...] = tuple(
    member.value for member in CritiqueExampleKind
)


def _status_of(value: Any) -> str:
    text = _text(value)
    if text and any(member.value == text for member in VerificationStatus):
        return text
    return VerificationStatus.INCONCLUSIVE.value


def _category_of(value: Any) -> str:
    text = _text(value)
    if text and any(member.value == text for member in CritiqueCategory):
        return text
    return CritiqueCategory.OTHER.value


def _severity_of(value: Any) -> str:
    text = _text(value)
    if text and any(member.value == text for member in CritiqueSeverity):
        return text
    return CritiqueSeverity.MEDIUM.value


def _source_of(value: Any, default: str = CritiqueSource.RULE_BASED.value) -> str:
    text = _text(value, default)
    if text and any(member.value == text for member in CritiqueSource):
        return text
    return default


# ── verifier metadata ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerifierMetadata:
    """What a verifier says about itself, so a registry can select it.

    ``deterministic`` is the load-bearing field: a deterministic verifier's
    answer does not change between runs on the same observation, and the
    selector prefers one whenever a deterministic verifier can decide the
    question at all. An AI-evaluator fallback is recorded as non-deterministic
    even when it is stable in practice, because "usually the same answer" is a
    different promise from "always the same answer".
    """

    verifier_id: str = ""
    name: str = ""
    description: str = ""
    category: str = VerifierCategory.CUSTOM.value
    version: str = RLVR_VERSION
    risk_level: str = RiskLevel.LOW.value
    deterministic: bool = True
    #: A sentence about how sure this verifier's verdicts are when they pass.
    #: Never a numeric claim the verifier cannot support.
    confidence_characteristics: str = ""
    #: The task/action kinds this verifier understands (free-form labels).
    task_types: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.verifier_id and self.version)

    @property
    def deterministic_category(self) -> bool:
        return self.deterministic

    def supports(self, task_type: str) -> bool:
        wanted = _text(task_type).lower()
        if not wanted:
            return True
        return not self.task_types or any(
            item.lower() == wanted for item in self.task_types
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "risk_level": self.risk_level,
            "deterministic": self.deterministic,
            "confidence_characteristics": self.confidence_characteristics,
            "task_types": list(self.task_types),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerifierMetadata:
        return cls(
            verifier_id=_text(data.get("verifier_id")),
            name=_text(data.get("name")),
            description=_text(data.get("description")),
            category=_text(data.get("category"), VerifierCategory.CUSTOM.value),
            version=_text(data.get("version"), RLVR_VERSION),
            risk_level=_text(data.get("risk_level"), RiskLevel.LOW.value),
            deterministic=_flag(data.get("deterministic"), True),
            confidence_characteristics=_text(data.get("confidence_characteristics")),
            task_types=_texts(data.get("task_types")),
            input_schema=_mapping(data.get("input_schema")),
            output_schema=_mapping(data.get("output_schema")),
            enabled=_flag(data.get("enabled"), True),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """One checkable question: the action taken and what was observed.

    The expected result is carried here and FROZEN by the caller before the
    verifier sees it. That is the security property, not a convenience: a
    verifier checks the observation against an expectation that was fixed
    before the run produced it, so a policy cannot move the goalposts after
    seeing the score.
    """

    verifier_id: str = ""
    task_id: str = ""
    trajectory_id: str = ""
    step_id: str = ""
    scope: str = VerificationScope.TASK.value
    action: Mapping[str, Any] = field(default_factory=dict)
    expected: Mapping[str, Any] = field(default_factory=dict)
    observation: Mapping[str, Any] = field(default_factory=dict)
    #: The workspace a file/git check reads under, when one is given.
    workspace: str = ""
    #: Digest of ``expected``, fixed when the request was built.
    expected_digest: str = ""
    timeout_s: float = 5.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def frozen(self) -> VerificationRequest:
        """This request with its expectation hashed, ready to hand to a verifier."""
        return VerificationRequest.from_dict(
            {**self.to_dict(), "expected_digest": expected_fingerprint(self.expected)}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "step_id": self.step_id,
            "scope": self.scope,
            "action": dict(self.action),
            "expected": dict(self.expected),
            "observation": dict(self.observation),
            "workspace": self.workspace,
            "expected_digest": self.expected_digest,
            "timeout_s": self.timeout_s,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationRequest:
        return cls(
            verifier_id=_text(data.get("verifier_id")),
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            step_id=_text(data.get("step_id")),
            scope=_text(data.get("scope"), VerificationScope.TASK.value),
            action=_mapping(data.get("action")),
            expected=_mapping(data.get("expected")),
            observation=_mapping(data.get("observation")),
            workspace=_text(data.get("workspace")),
            expected_digest=_text(data.get("expected_digest")),
            timeout_s=_real(data.get("timeout_s"), 5.0) or 5.0,
            metadata=_mapping(data.get("metadata")),
        )


def expected_fingerprint(expected: Mapping[str, Any]) -> str:
    """A stable digest of an expected result: the goalposts, fixed in place."""
    payload = json.dumps(dict(expected), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ── verification results ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What one verifier said about one observable outcome, with its evidence.

    ``score`` is the verifier's own reading on 0..1 — 1.0 fully verified, 0.0
    nothing verified — and ``confidence`` says how much THIS verdict should be
    trusted (a file hash is 1.0; a process whose state was inferred from an
    incomplete report may be lower). Neither is a reward: the reward is derived
    later, from a configuration that is visible and versioned.

    ``expected`` and ``observed`` are stored beside the verdict so a reader can
    re-check the decision without re-running anything, and ``expected_digest``
    records the expectation as it was when the check ran. ``evidence`` names the
    observable facts the verdict rests on. There is no field for reasoning.
    """

    verification_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    trajectory_id: str = ""
    step_id: str = ""
    verifier_id: str = ""
    verifier_version: str = ""
    status: str = VerificationStatus.INCONCLUSIVE.value
    passed: bool | None = None
    score: float | None = None
    scope: str = VerificationScope.TASK.value
    evidence: tuple[str, ...] = ()
    expected: Mapping[str, Any] = field(default_factory=dict)
    observed: Mapping[str, Any] = field(default_factory=dict)
    expected_digest: str = ""
    error_category: str = ""
    confidence: float | None = None
    detail: str = ""
    timestamp: str = field(default_factory=now_iso)
    schema_version: int = VERIFICATION_SCHEMA_VERSION

    @property
    def verified(self) -> bool:
        return self.status == VerificationStatus.PASS.value

    @property
    def failed(self) -> bool:
        return self.status == VerificationStatus.FAIL.value

    @property
    def partial(self) -> bool:
        return self.status == VerificationStatus.PARTIAL.value

    @property
    def inconclusive(self) -> bool:
        return self.status == VerificationStatus.INCONCLUSIVE.value

    @property
    def factual(self) -> bool:
        """Whether this verdict rests on at least one named, observable fact."""
        return bool(self.evidence)

    def subject_key(self) -> str:
        """What this verdict is about, for looking up a labelled truth.

        Most specific first: a step-level truth is keyed by the step, a
        task-level truth by the task, and only then by the trajectory (a run id
        nobody labels) or the verification itself.
        """
        return self.step_id or self.task_id or self.trajectory_id or self.verification_id

    def reading(self) -> float:
        """The score as a number: the verifier's own, or a status-derived one."""
        if self.score is not None:
            return max(0.0, min(1.0, float(self.score)))
        if self.status == VerificationStatus.PASS.value:
            return 1.0
        if self.status == VerificationStatus.PARTIAL.value:
            return 0.5
        return 0.0

    def with_status(
        self,
        status: VerificationStatus | str,
        *,
        detail: str = "",
        evidence: Sequence[str] = (),
    ) -> VerificationResult:
        """A copy under a new verdict, keeping the observation it was read from."""
        value = status.value if isinstance(status, VerificationStatus) else str(status)
        return VerificationResult.from_dict(
            {
                **self.to_dict(),
                "status": value,
                "passed": value == VerificationStatus.PASS.value,
                "detail": detail or self.detail,
                "evidence": list(self.evidence) + list(evidence),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "step_id": self.step_id,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "status": self.status,
            "passed": self.passed,
            "score": self.score,
            "scope": self.scope,
            "evidence": list(self.evidence),
            "expected": dict(self.expected),
            "observed": dict(self.observed),
            "expected_digest": self.expected_digest,
            "error_category": self.error_category,
            "confidence": self.confidence,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationResult:
        status = _status_of(data.get("status"))
        passed = data.get("passed")
        return cls(
            verification_id=_text(data.get("verification_id")) or uuid4().hex,
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            step_id=_text(data.get("step_id")),
            verifier_id=_text(data.get("verifier_id")),
            verifier_version=_text(data.get("verifier_version")),
            status=status,
            passed=passed if isinstance(passed, bool) else None,
            score=_real(data.get("score")),
            scope=_text(data.get("scope"), VerificationScope.TASK.value),
            evidence=_texts(data.get("evidence")),
            expected=_mapping(data.get("expected")),
            observed=_mapping(data.get("observed")),
            expected_digest=_text(data.get("expected_digest")),
            error_category=_text(data.get("error_category")),
            confidence=_real(data.get("confidence")),
            detail=_text(data.get("detail")),
            timestamp=_text(data.get("timestamp")) or now_iso(),
            schema_version=_whole(
                data.get("schema_version"), VERIFICATION_SCHEMA_VERSION
            )
            or VERIFICATION_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """Every verification of one task, kept together and aggregated honestly.

    The aggregate rule is the point. A task whose steps all passed but whose
    final check failed is FAILED, not 80% successful: partial credit exists for
    genuinely partial work, and the task-level verdict outranks the step average
    because it is the answer to the question that was asked. When there is no
    task-level result, the aggregate is PARTIAL with the observed pass ratio as
    its score, so a run that was never checked end to end cannot read as passed.
    """

    task_id: str = ""
    trajectory_id: str = ""
    results: tuple[VerificationResult, ...] = ()

    @property
    def count(self) -> int:
        return len(self.results)

    def of_scope(self, scope: str) -> tuple[VerificationResult, ...]:
        wanted = _text(scope)
        return tuple(item for item in self.results if item.scope == wanted)

    def task_result(self) -> VerificationResult | None:
        for item in reversed(self.results):
            if item.scope == VerificationScope.TASK.value:
                return item
        return None

    @property
    def passed(self) -> bool:
        task = self.task_result()
        if task is not None:
            return task.verified
        return bool(self.results) and all(item.verified for item in self.results)

    @property
    def failed(self) -> bool:
        task = self.task_result()
        if task is not None:
            return task.failed
        return any(item.failed for item in self.results)

    @property
    def status(self) -> str:
        task = self.task_result()
        if task is not None:
            return task.status
        if not self.results:
            return VerificationStatus.INCONCLUSIVE.value
        if any(item.status == VerificationStatus.ERROR.value for item in self.results):
            return VerificationStatus.ERROR.value
        if any(item.failed for item in self.results):
            return VerificationStatus.FAIL.value
        if all(item.verified for item in self.results):
            return VerificationStatus.PASS.value
        if any(
            item.status
            in {
                VerificationStatus.PARTIAL.value,
                VerificationStatus.PASS.value,
            }
            for item in self.results
        ):
            return VerificationStatus.PARTIAL.value
        return VerificationStatus.INCONCLUSIVE.value

    @property
    def score(self) -> float:
        task = self.task_result()
        if task is not None:
            return task.reading()
        if not self.results:
            return 0.0
        return sum(item.reading() for item in self.results) / len(self.results)

    @property
    def pass_ratio(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for item in self.results if item.verified) / len(self.results)

    @property
    def categories(self) -> tuple[str, ...]:
        """The distinct failure categories this summary observed."""
        found: list[str] = []
        for item in self.results:
            if item.verified or not item.error_category:
                continue
            if item.error_category not in found:
                found.append(item.error_category)
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "count": self.count,
            "status": self.status,
            "passed": self.passed,
            "failed": self.failed,
            "score": round(self.score, 6),
            "pass_ratio": round(self.pass_ratio, 6),
            "categories": list(self.categories),
            "results": [item.to_dict() for item in self.results],
        }

    @classmethod
    def of(
        cls, results: Sequence[VerificationResult], *, follow_task: VerificationResult | None = None
    ) -> VerificationSummary:
        """A summary from results, optionally with the task-level verdict last.

        ``follow_task`` is appended only when the results do not already carry a
        task-scope verdict, so a caller can verify every step and then make ONE
        final check without the aggregate silently having two answers.
        """
        rows = list(results)
        if follow_task is not None and not any(
            item.scope == VerificationScope.TASK.value for item in rows
        ):
            rows.append(follow_task)
        task_id = next((item.task_id for item in rows if item.task_id), "")
        trajectory_id = next((item.trajectory_id for item in rows if item.trajectory_id), "")
        return cls(task_id=task_id, trajectory_id=trajectory_id, results=tuple(rows))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationSummary:
        rows = data.get("results")
        results: list[VerificationResult] = []
        if isinstance(rows, (list, tuple)):
            results = [
                VerificationResult.from_dict(row)
                for row in rows
                if isinstance(row, Mapping)
            ]
        return cls(
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            results=tuple(results),
        )


# ── critiques ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    """One structured, evidence-based statement about a failure.

    A critique answers six questions and nothing else: what failed
    (``failed_component``), how badly (``severity``), under which category, what
    was observed, what was expected, and — when a correction is possible — what
    to do instead. ``evidence`` names the observable facts. ``source`` records
    who is speaking: a deterministic verifier, a rule, a person, an AI evaluator
    or a composite of several.

    ``corrected_output``/``correction`` hold a STRUCTURED suggestion (a mapping
    or a short instruction), never a reasoning trace: a critique is training
    data, and hidden reasoning must not be. The engine refuses an AI-sourced
    critique that carries reasoning keys, the same way Phase 17 refuses a pair
    that does.
    """

    critique_id: str = field(default_factory=lambda: uuid4().hex)
    trajectory_id: str = ""
    task_id: str = ""
    step_id: str = ""
    category: str = CritiqueCategory.OTHER.value
    severity: str = CritiqueSeverity.MEDIUM.value
    failed_component: str = ""
    evidence: tuple[str, ...] = ()
    observed_behavior: Mapping[str, Any] = field(default_factory=dict)
    expected_behavior: Mapping[str, Any] = field(default_factory=dict)
    correction: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    source: str = CritiqueSource.RULE_BASED.value
    evaluator_version: str = RLVR_VERSION
    verification_id: str = ""
    detail: str = ""
    timestamp: str = field(default_factory=now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CRITIQUE_SCHEMA_VERSION

    @property
    def has_correction(self) -> bool:
        return bool(self.correction)

    @property
    def verified(self) -> bool:
        """Whether a deterministic verifier is behind this critique."""
        return self.source == CritiqueSource.VERIFIER.value and bool(self.verification_id)

    @property
    def strength(self) -> float:
        """How much this critique should weigh, from source and confidence."""
        base = float(CRITIQUE_SOURCE_STRENGTH.get(self.source, 0.5))
        if self.confidence is not None:
            base = base * max(0.0, min(1.0, float(self.confidence)))
        return round(base, 6)

    def rank(self) -> tuple[int, float]:
        """Sort key: most severe first, then strongest source."""
        return (SEVERITY_RANK.get(self.severity, 1), -self.strength)

    def to_dict(self) -> dict[str, Any]:
        return {
            "critique_id": self.critique_id,
            "trajectory_id": self.trajectory_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "category": self.category,
            "severity": self.severity,
            "failed_component": self.failed_component,
            "evidence": list(self.evidence),
            "observed_behavior": dict(self.observed_behavior),
            "expected_behavior": dict(self.expected_behavior),
            "correction": dict(self.correction),
            "confidence": self.confidence,
            "source": self.source,
            "evaluator_version": self.evaluator_version,
            "verification_id": self.verification_id,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CritiqueResult:
        return cls(
            critique_id=_text(data.get("critique_id")) or uuid4().hex,
            trajectory_id=_text(data.get("trajectory_id")),
            task_id=_text(data.get("task_id")),
            step_id=_text(data.get("step_id")),
            category=_category_of(data.get("category")),
            severity=_severity_of(data.get("severity")),
            failed_component=_text(data.get("failed_component")),
            evidence=_texts(data.get("evidence")),
            observed_behavior=_mapping(data.get("observed_behavior")),
            expected_behavior=_mapping(data.get("expected_behavior")),
            correction=_mapping(data.get("correction")),
            confidence=_real(data.get("confidence")),
            source=_source_of(data.get("source")),
            evaluator_version=_text(data.get("evaluator_version"), RLVR_VERSION),
            verification_id=_text(data.get("verification_id")),
            detail=_text(data.get("detail")),
            timestamp=_text(data.get("timestamp")) or now_iso(),
            metadata=_mapping(data.get("metadata")),
            schema_version=_whole(data.get("schema_version"), CRITIQUE_SCHEMA_VERSION)
            or CRITIQUE_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class CounterfactualComparison:
    """What would have worked, compared against what was actually done.

    The comparison is between two OBSERVED behaviours — the failed action and a
    verified-successful alternative — not a reconstruction of anyone's thoughts.
    ``difference`` is the structured delta (the arguments that changed, the tool
    that changed, the step order that changed); ``evidence`` names the checks
    that passed for the successful side.
    """

    comparison_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    trajectory_id: str = ""
    failed_action: Mapping[str, Any] = field(default_factory=dict)
    successful_action: Mapping[str, Any] = field(default_factory=dict)
    difference: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    verification_id: str = ""
    confidence: float | None = None
    detail: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def comparable(self) -> bool:
        return bool(self.failed_action and self.successful_action)

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "task_id": self.task_id,
            "trajectory_id": self.trajectory_id,
            "failed_action": dict(self.failed_action),
            "successful_action": dict(self.successful_action),
            "difference": dict(self.difference),
            "evidence": list(self.evidence),
            "verification_id": self.verification_id,
            "confidence": self.confidence,
            "detail": self.detail,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CounterfactualComparison:
        return cls(
            comparison_id=_text(data.get("comparison_id")) or uuid4().hex,
            task_id=_text(data.get("task_id")),
            trajectory_id=_text(data.get("trajectory_id")),
            failed_action=_mapping(data.get("failed_action")),
            successful_action=_mapping(data.get("successful_action")),
            difference=_mapping(data.get("difference")),
            evidence=_texts(data.get("evidence")),
            verification_id=_text(data.get("verification_id")),
            confidence=_real(data.get("confidence")),
            detail=_text(data.get("detail")),
            created_at=_text(data.get("created_at")) or now_iso(),
        )


# ── corrections ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CorrectedExample:
    """A failed behaviour, the critique of it, and a verified correction.

    The quality rule is strict on purpose: a correction is only ACCEPTED when a
    verifier checked it and passed. When verification is impossible for this
    behaviour, the row is NEEDS_REVIEW rather than accepted, because "an AI
    suggested it" is not evidence that it works. A correction that verification
    refused is REJECTED and never becomes training data.
    """

    example_id: str = field(default_factory=lambda: uuid4().hex)
    original_input: Mapping[str, Any] = field(default_factory=dict)
    original_output: Mapping[str, Any] = field(default_factory=dict)
    critique: CritiqueResult = field(default_factory=CritiqueResult)
    corrected_output: Mapping[str, Any] = field(default_factory=dict)
    verification_result: VerificationResult | None = None
    source_trajectory_id: str = ""
    quality_status: str = CorrectionStatus.NEEDS_REVIEW.value
    dataset_version: str = ""
    correction_source: str = CritiqueSource.RULE_BASED.value
    evidence: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    schema_version: int = CORRECTION_SCHEMA_VERSION

    @property
    def accepted(self) -> bool:
        return self.quality_status == CorrectionStatus.ACCEPTED.value

    @property
    def rejected(self) -> bool:
        return self.quality_status == CorrectionStatus.REJECTED.value

    @property
    def needs_review(self) -> bool:
        return self.quality_status == CorrectionStatus.NEEDS_REVIEW.value

    @property
    def verified(self) -> bool:
        return self.verification_result is not None and (
            self.verification_result.verified
        )

    def group_key(self) -> str:
        return self.source_trajectory_id or self.example_id

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "input": dict(self.original_input),
                "original": dict(self.original_output),
                "corrected": dict(self.corrected_output),
                "critique": self.critique.critique_id,
                "quality": self.quality_status,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "original_input": dict(self.original_input),
            "original_output": dict(self.original_output),
            "critique": self.critique.to_dict(),
            "corrected_output": dict(self.corrected_output),
            "verification_result": (
                self.verification_result.to_dict()
                if self.verification_result is not None
                else None
            ),
            "source_trajectory_id": self.source_trajectory_id,
            "quality_status": self.quality_status,
            "dataset_version": self.dataset_version,
            "correction_source": self.correction_source,
            "evidence": list(self.evidence),
            "reasons": list(self.reasons),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorrectedExample:
        critique = data.get("critique")
        verification = data.get("verification_result")
        status = _text(data.get("quality_status"), CorrectionStatus.NEEDS_REVIEW.value)
        if not any(member.value == status for member in CorrectionStatus):
            status = CorrectionStatus.NEEDS_REVIEW.value
        return cls(
            example_id=_text(data.get("example_id")) or uuid4().hex,
            original_input=_mapping(data.get("original_input")),
            original_output=_mapping(data.get("original_output")),
            critique=CritiqueResult.from_dict(critique)
            if isinstance(critique, Mapping)
            else CritiqueResult(),
            corrected_output=_mapping(data.get("corrected_output")),
            verification_result=VerificationResult.from_dict(verification)
            if isinstance(verification, Mapping)
            else None,
            source_trajectory_id=_text(data.get("source_trajectory_id")),
            quality_status=status,
            dataset_version=_text(data.get("dataset_version")),
            correction_source=_source_of(
                data.get("correction_source"), CritiqueSource.RULE_BASED.value
            ),
            evidence=_texts(data.get("evidence")),
            reasons=_texts(data.get("reasons")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version"), CORRECTION_SCHEMA_VERSION)
            or CORRECTION_SCHEMA_VERSION,
        )


# ── critique datasets ────────────────────────────────────────────────────────


def critique_dataset_version_id(name: str, version: str) -> str:
    return f"{str(name).strip()}@{str(version).strip()}"


@dataclass(frozen=True, slots=True)
class CritiqueExample:
    """One row of a critique dataset: a failure and what should have happened.

    The three training shapes the phase promises are the three kinds this row
    can hold: a bad action with its critique and the correct action, a bad plan
    with the corrected plan, a bad tool choice with the corrected tool. A
    ``CORRECTION`` row carries an analysed correction (the corrected example) as
    its target; ``RESPONSE`` is where a final answer's format failure lands.

    ``status`` is the quality verdict the builder reached (accepted /
    needs_review / rejected); a rejected row stays in the dataset with its
    reason so a later reader can see what was excluded and why.
    """

    example_id: str = field(default_factory=lambda: uuid4().hex)
    kind: str = CritiqueExampleKind.ACTION.value
    prompt: Mapping[str, Any] = field(default_factory=dict)
    original: Mapping[str, Any] = field(default_factory=dict)
    corrected: Mapping[str, Any] = field(default_factory=dict)
    critique_id: str = ""
    critique_category: str = ""
    severity: str = ""
    verification_id: str = ""
    verified: bool = False
    source_trajectory_id: str = ""
    status: str = CorrectionStatus.NEEDS_REVIEW.value
    reasons: tuple[str, ...] = ()
    difficulty: str = "simple"
    tags: tuple[str, ...] = ()
    group_key_name: str = ""
    dataset_version: str = ""
    created_at: str = field(default_factory=now_iso)
    schema_version: int = CRITIQUE_DATASET_SCHEMA_VERSION

    @property
    def accepted(self) -> bool:
        return self.status == CorrectionStatus.ACCEPTED.value

    @property
    def group_key(self) -> str:
        return self.group_key_name or self.source_trajectory_id or self.example_id

    @property
    def estimated_tokens(self) -> int:
        try:
            payload = json.dumps(
                {
                    "prompt": dict(self.prompt),
                    "original": dict(self.original),
                    "corrected": dict(self.corrected),
                },
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            return 0
        return max(1, len(payload) // 4)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "kind": self.kind,
                "prompt": dict(self.prompt),
                "original": dict(self.original),
                "corrected": dict(self.corrected),
                "status": self.status,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_sft_example(self) -> Any:
        """The row as a Phase 16 example, so the shared splitter can place it."""
        from novacontrol.training.models import SFTTrainingExample

        return SFTTrainingExample(
            example_id=self.example_id,
            dataset_type=self.kind,
            input=dict(self.prompt),
            target=dict(self.corrected),
            metadata={"group_key": self.group_key, "critique_id": self.critique_id},
            source_trajectory_id=self.source_trajectory_id,
            quality_status=self.status,
            difficulty=self.difficulty,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "kind": self.kind,
            "prompt": dict(self.prompt),
            "original": dict(self.original),
            "corrected": dict(self.corrected),
            "critique_id": self.critique_id,
            "critique_category": self.critique_category,
            "severity": self.severity,
            "verification_id": self.verification_id,
            "verified": self.verified,
            "source_trajectory_id": self.source_trajectory_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "group_key_name": self.group_key_name,
            "dataset_version": self.dataset_version,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CritiqueExample:
        status = _text(data.get("status"), CorrectionStatus.NEEDS_REVIEW.value)
        if not any(member.value == status for member in CorrectionStatus):
            status = CorrectionStatus.NEEDS_REVIEW.value
        kind = _text(data.get("kind"), CritiqueExampleKind.ACTION.value)
        if not any(member.value == kind for member in CritiqueExampleKind):
            kind = CritiqueExampleKind.ACTION.value
        return cls(
            example_id=_text(data.get("example_id")) or uuid4().hex,
            kind=kind,
            prompt=_mapping(data.get("prompt")),
            original=_mapping(data.get("original")),
            corrected=_mapping(data.get("corrected")),
            critique_id=_text(data.get("critique_id")),
            critique_category=_text(data.get("critique_category")),
            severity=_text(data.get("severity")),
            verification_id=_text(data.get("verification_id")),
            verified=_flag(data.get("verified")),
            source_trajectory_id=_text(data.get("source_trajectory_id")),
            status=status,
            reasons=_texts(data.get("reasons")),
            difficulty=_text(data.get("difficulty"), "simple"),
            tags=_texts(data.get("tags")),
            group_key_name=_text(data.get("group_key_name")),
            dataset_version=_text(data.get("dataset_version")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(
                data.get("schema_version"), CRITIQUE_DATASET_SCHEMA_VERSION
            )
            or CRITIQUE_DATASET_SCHEMA_VERSION,
        )


@dataclass(frozen=True, slots=True)
class CritiqueDatasetStatistics:
    """What a built critique dataset contains, counted rather than described."""

    total: int = 0
    accepted: int = 0
    rejected: int = 0
    needs_review: int = 0
    verified: int = 0
    unverified: int = 0
    corrections: int = 0
    accepted_corrections: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    by_category: Mapping[str, int] = field(default_factory=dict)
    by_severity: Mapping[str, int] = field(default_factory=dict)
    by_source: Mapping[str, int] = field(default_factory=dict)
    duplicates_removed: int = 0
    malformed_removed: int = 0
    groups: int = 0
    source_trajectories: int = 0
    estimated_tokens: int = 0
    by_split: Mapping[str, int] = field(default_factory=dict)
    skipped: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "needs_review": self.needs_review,
            "verified": self.verified,
            "unverified": self.unverified,
            "corrections": self.corrections,
            "accepted_corrections": self.accepted_corrections,
            "by_kind": dict(self.by_kind),
            "by_category": dict(self.by_category),
            "by_severity": dict(self.by_severity),
            "by_source": dict(self.by_source),
            "duplicates_removed": self.duplicates_removed,
            "malformed_removed": self.malformed_removed,
            "groups": self.groups,
            "source_trajectories": self.source_trajectories,
            "estimated_tokens": self.estimated_tokens,
            "by_split": dict(self.by_split),
            "skipped": dict(self.skipped),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CritiqueDatasetStatistics:
        def counts(key: str) -> dict[str, int]:
            raw = data.get(key)
            result: dict[str, int] = {}
            if isinstance(raw, Mapping):
                for name, value in raw.items():
                    result[str(name)] = _whole(value)
            return result

        return cls(
            total=_whole(data.get("total")),
            accepted=_whole(data.get("accepted")),
            rejected=_whole(data.get("rejected")),
            needs_review=_whole(data.get("needs_review")),
            verified=_whole(data.get("verified")),
            unverified=_whole(data.get("unverified")),
            corrections=_whole(data.get("corrections")),
            accepted_corrections=_whole(data.get("accepted_corrections")),
            by_kind=counts("by_kind"),
            by_category=counts("by_category"),
            by_severity=counts("by_severity"),
            by_source=counts("by_source"),
            duplicates_removed=_whole(data.get("duplicates_removed")),
            malformed_removed=_whole(data.get("malformed_removed")),
            groups=_whole(data.get("groups")),
            source_trajectories=_whole(data.get("source_trajectories")),
            estimated_tokens=_whole(data.get("estimated_tokens")),
            by_split=counts("by_split"),
            skipped=counts("skipped"),
        )


@dataclass(frozen=True, slots=True)
class CritiqueDatasetVersion:
    """An immutable ``name@version`` set of critique examples, split like Phase 16."""

    dataset_version_id: str = ""
    name: str = ""
    version: str = ""
    description: str = ""
    examples: tuple[CritiqueExample, ...] = ()
    splits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    statistics: CritiqueDatasetStatistics = field(default_factory=CritiqueDatasetStatistics)
    rules: Mapping[str, Any] = field(default_factory=dict)
    split_config: Mapping[str, Any] = field(default_factory=dict)
    source_datasets: tuple[str, ...] = ()
    preprocessing_version: str = RLVR_VERSION
    source_data_version: str = ""
    tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    schema_version: int = CRITIQUE_DATASET_SCHEMA_VERSION

    def __len__(self) -> int:
        return len(self.examples)

    def example(self, example_id: str) -> CritiqueExample | None:
        wanted = _text(example_id)
        for row in self.examples:
            if row.example_id == wanted:
                return row
        return None

    def split(self, name: str) -> tuple[CritiqueExample, ...]:
        wanted = _text(name)
        ids = tuple(self.splits.get(wanted, ()))
        by_id = {row.example_id: row for row in self.examples}
        return tuple(by_id[item] for item in ids if item in by_id)

    def split_names(self) -> tuple[str, ...]:
        return tuple(name for name, ids in self.splits.items() if ids)

    def accepted_examples(self) -> tuple[CritiqueExample, ...]:
        return tuple(row for row in self.examples if row.accepted)

    def by_kind(self, kind: str) -> tuple[CritiqueExample, ...]:
        wanted = _text(kind)
        return tuple(row for row in self.examples if row.kind == wanted)

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "examples": [row.fingerprint() for row in self.examples],
                "rules": dict(self.rules),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version_id": self.dataset_version_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "examples": [row.to_dict() for row in self.examples],
            "splits": {name: list(ids) for name, ids in self.splits.items()},
            "statistics": self.statistics.to_dict(),
            "rules": dict(self.rules),
            "split_config": dict(self.split_config),
            "source_datasets": list(self.source_datasets),
            "preprocessing_version": self.preprocessing_version,
            "source_data_version": self.source_data_version,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CritiqueDatasetVersion:
        rows = data.get("examples")
        examples: list[CritiqueExample] = []
        if isinstance(rows, (list, tuple)):
            examples = [
                CritiqueExample.from_dict(row)
                for row in rows
                if isinstance(row, Mapping)
            ]
        raw_splits = data.get("splits")
        splits: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_splits, Mapping):
            for name, ids in raw_splits.items():
                splits[str(name)] = tuple(
                    str(item) for item in ids if str(item).strip()
                ) if isinstance(ids, (list, tuple)) else ()
        statistics = data.get("statistics")
        return cls(
            dataset_version_id=_text(data.get("dataset_version_id")),
            name=_text(data.get("name")),
            version=_text(data.get("version")),
            description=_text(data.get("description")),
            examples=tuple(examples),
            splits=splits,
            statistics=CritiqueDatasetStatistics.from_dict(statistics)
            if isinstance(statistics, Mapping)
            else CritiqueDatasetStatistics(),
            rules=_mapping(data.get("rules")),
            split_config=_mapping(data.get("split_config")),
            source_datasets=_texts(data.get("source_datasets")),
            preprocessing_version=_text(data.get("preprocessing_version"), RLVR_VERSION),
            source_data_version=_text(data.get("source_data_version")),
            tags=_texts(data.get("tags")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(
                data.get("schema_version"), CRITIQUE_DATASET_SCHEMA_VERSION
            )
            or CRITIQUE_DATASET_SCHEMA_VERSION,
        )
