"""The data contracts of Phase 23 — what a hypothesis, an experiment and a rule ARE.

Every type here exists to keep one distinction visible that a learning system can
lose very easily:

* an **observation** is something that was seen (``EnvironmentObservation``);
* a **prediction** is what someone expected to see, and says WHO expected it and on
  what basis (``Prediction`` — ``unavailable`` is a legal, and the common, basis);
* an **inference** is a conclusion drawn from observations (a ``Hypothesis`` status,
  a ``LearnedRule``) and never carries the authority of the observation it came from;
* a **confidence** is heuristic unless said otherwise (``ConfidenceKind``), and
  ``insufficient_evidence`` is the honest answer for a hypothesis nobody has tested.

Two rules are enforced by the SHAPE of these types rather than by convention:

1. A prediction does not become evidence. ``EvidenceReference.kind`` separates a
   direct observation from an inference from a prediction, and only the first two
   ever move a hypothesis' support.
2. Evidence is counted twice: as it was recorded, and as it is *independent*.
   ``evidence_source_key`` is what makes two readings from one source one fact
   (``Hypothesis.independent_support``), because repeated observations of the same
   event are not new evidence (§7).

``to_dict``/``from_mapping`` exist so a restart can rebuild these records; the
mapping form is the stored form, and it stays reference-only — no observation body
and no reasoning text is ever stored here.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from novacontrol.world.timeutil import parse_timestamp, seconds_between

__all__ = [
    "MAX_EVIDENCE_PER_HYPOTHESIS",
    "MAX_EXPERIMENTS_PER_OBJECTIVE",
    "MAX_HYPOTHESES_PER_OBJECTIVE",
    "MAX_RULES",
    "MAX_STATEMENT",
    "PHASE",
    "SCHEMA_VERSION",
    "ActionSpec",
    "ActionResult",
    "BudgetUsage",
    "BudgetVerdict",
    "CandidateExperiment",
    "ComparisonResult",
    "ConfidenceKind",
    "CuriosityScore",
    "EnvironmentKind",
    "EnvironmentObservation",
    "EvidenceKind",
    "EvidenceReference",
    "EvidenceRole",
    "ExperimentRun",
    "ExperimentSpec",
    "ExperimentStatus",
    "Hypothesis",
    "HypothesisStatus",
    "HypothesisVersion",
    "InformationValue",
    "KnowledgeChange",
    "KnowledgeUpdate",
    "LearnedRule",
    "LearningBudget",
    "LearningCycleReport",
    "LearningCycleStep",
    "LearningObjective",
    "MatchingPolicy",
    "ObjectiveStatus",
    "OutcomeKind",
    "Prediction",
    "PredictionBasis",
    "RuleStatus",
    "SafetyDecision",
    "StepKind",
    "as_bool",
    "as_float",
    "as_int",
    "as_text",
    "clamp",
    "new_id",
    "now_iso",
]

#: This package's phase label, in the vocabulary every other phase uses.
PHASE = "phase23"

#: The schema version of the stored records, so a report can say what it read.
SCHEMA_VERSION = "phase23.1"

#: A hypothesis statement is a sentence a person can read and reject, not a document.
MAX_STATEMENT = 400

#: Ceilings: a learning session that grows without bound is one that never stops.
MAX_HYPOTHESES_PER_OBJECTIVE = 64
MAX_EXPERIMENTS_PER_OBJECTIVE = 128
MAX_EVIDENCE_PER_HYPOTHESIS = 64
MAX_RULES = 64

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


# ── tolerant readers ─────────────────────────────────────────────────────────


def as_text(value: Any, default: str = "") -> str:
    """Text, stripped, or the default — never ``None`` and never ``"None"``."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def as_int(value: Any, default: int = 0) -> int:
    """An integer from whatever arrived; anything unreadable is the default."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """A float from whatever arrived; anything unreadable is the default."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    """A boolean from a real boolean or the usual words — never from a string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return default


def clamp(value: float, low: float, high: float) -> float:
    """``value`` held inside ``[low, high]``."""
    return max(low, min(high, value))


def now_iso() -> str:
    """The current time as an aware ISO string — the build's one timestamp form."""
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    """A fresh identifier for a record that has no content hash to be named by."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _mapping(value: Any) -> dict[str, Any]:
    """A plain ``dict`` from a mapping, or an empty one — for tolerant reads."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _texts(value: Any) -> tuple[str, ...]:
    """A tuple of non-empty strings from a sequence, dropping anything else."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, Sequence):
        return ()
    out = [as_text(item) for item in value]
    return tuple(item for item in out if item)


# ── vocabulary ───────────────────────────────────────────────────────────────


class HypothesisStatus(StrEnum):
    """Where a hypothesis stands. Every member is a claim about EVIDENCE, not hope."""

    PROPOSED = "proposed"
    TESTABLE = "testable"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class ObjectiveStatus(StrEnum):
    """How a learning objective ended — and "still open" is a real answer."""

    OPEN = "open"
    ACHIEVED = "achieved"
    EXHAUSTED = "exhausted"
    STOPPED = "stopped"
    UNJUSTIFIED = "unjustified"
    ABANDONED = "abandoned"


class ExperimentStatus(StrEnum):
    """What happened to an experiment. Only COMPLETED means it actually ran.

    ``REJECTED``, ``BLOCKED`` and ``PROPOSED`` all mean no action was taken, which
    is why they are separate members rather than a boolean: a blocked experiment
    that reported success is the exact defect §18 asks this phase to prevent.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class OutcomeKind(StrEnum):
    """The result of comparing a prediction with what was actually seen."""

    CONFIRMED = "confirmed"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"
    UNABLE_TO_EVALUATE = "unable_to_evaluate"


class PredictionBasis(StrEnum):
    """Where a prediction came from — the field that stops a guess looking like a model."""

    RULE_BASED = "rule_based"
    ENVIRONMENT_RULE = "environment_rule"
    MODEL = "model"
    UNAVAILABLE = "unavailable"


class ConfidenceKind(StrEnum):
    """What a confidence number MEANS. Only one of these is a probability claim."""

    HEURISTIC = "heuristic"
    STATED = "stated"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class EvidenceRole(StrEnum):
    """Which way one piece of evidence points."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    INCONCLUSIVE = "inconclusive"


class EvidenceKind(StrEnum):
    """What kind of thing a piece of evidence is.

    Only ``OBSERVATION`` and ``INFERENCE`` move a hypothesis. A ``PREDICTION`` is
    recorded so a report can show what was expected, and is never counted as
    support for itself (§6/§7).
    """

    OBSERVATION = "observation"
    INFERENCE = "inference"
    PREDICTION = "prediction"


class KnowledgeChange(StrEnum):
    """What one knowledge update DID to the record it names."""

    SUPPORT = "support"
    CONTRADICT = "contradict"
    INCONCLUSIVE = "inconclusive"
    PROMOTE = "promote"
    REVISE = "revise"
    RETIRE = "retire"
    NO_CHANGE = "no_change"


class RuleStatus(StrEnum):
    """How far a learned rule is allowed to travel."""

    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    RETIRED = "retired"


class EnvironmentKind(StrEnum):
    """What an environment IS, so a simulator's success is never reported as real."""

    SIMULATOR = "simulator"
    WORLD = "world"
    BRIDGED = "bridged"


class StepKind(StrEnum):
    """One step of the learning cycle, named as the cycle is named."""

    OBSERVE = "observe"
    IDENTIFY_UNCERTAINTY = "identify_uncertainty"
    FORM_HYPOTHESIS = "form_hypothesis"
    SELECT_EXPERIMENT = "select_experiment"
    CHECK_SAFETY = "check_safety"
    EXECUTE = "execute"
    OBSERVE_RESULT = "observe_result"
    COMPARE = "compare"
    UPDATE_KNOWLEDGE = "update_knowledge"
    EVALUATE = "evaluate"
    NEXT = "next"


# ── evidence ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """One thing that was seen, and enough context to find it again.

    ``source_key`` is the independence key: two references sharing it describe the
    same source (one observation, one experiment run, one reading), so they count
    as one fact when support is counted. ``kind`` keeps a prediction from ever
    being counted as support.
    """

    evidence_id: str
    role: EvidenceRole
    kind: EvidenceKind = EvidenceKind.OBSERVATION
    detail: str = ""
    source_key: str = ""
    observation_ref: str = ""
    experiment_id: str = ""
    hypothesis_id: str = ""
    timestamp: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "role": self.role.value,
            "kind": self.kind.value,
            "detail": self.detail,
            "source_key": self.source_key,
            "observation_ref": self.observation_ref,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "timestamp": self.timestamp,
            "values": dict(self.values),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvidenceReference:
        return cls(
            evidence_id=as_text(data.get("evidence_id")),
            role=EvidenceRole(as_text(data.get("role"), EvidenceRole.INCONCLUSIVE.value)),
            kind=EvidenceKind(as_text(data.get("kind"), EvidenceKind.OBSERVATION.value)),
            detail=as_text(data.get("detail")),
            source_key=as_text(data.get("source_key")),
            observation_ref=as_text(data.get("observation_ref")),
            experiment_id=as_text(data.get("experiment_id")),
            hypothesis_id=as_text(data.get("hypothesis_id")),
            timestamp=as_text(data.get("timestamp")),
            values=_mapping(data.get("values")),
        )


# ── prediction and observation ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Prediction:
    """What was expected before an experiment ran, and on what basis.

    ``basis=UNAVAILABLE`` is normal in this build: nothing fabricates a prediction
    to fill the field, and an experiment without a prediction can still run and
    still be useful — it simply cannot be scored for agreement (§6).
    """

    basis: PredictionBasis = PredictionBasis.UNAVAILABLE
    expected: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    confidence: float | None = None
    confidence_kind: ConfidenceKind = ConfidenceKind.INSUFFICIENT_EVIDENCE
    reason: str = ""
    limitations: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.basis is not PredictionBasis.UNAVAILABLE and bool(self.expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis.value,
            "expected": dict(self.expected),
            "provider": self.provider,
            "confidence": self.confidence,
            "confidence_kind": self.confidence_kind.value,
            "reason": self.reason,
            "limitations": list(self.limitations),
            "available": self.available,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Prediction:
        confidence = data.get("confidence")
        return cls(
            basis=PredictionBasis(as_text(data.get("basis"), PredictionBasis.UNAVAILABLE.value)),
            expected=_mapping(data.get("expected")),
            provider=as_text(data.get("provider")),
            confidence=None if confidence is None else as_float(confidence),
            confidence_kind=ConfidenceKind(
                as_text(data.get("confidence_kind"), ConfidenceKind.INSUFFICIENT_EVIDENCE.value)
            ),
            reason=as_text(data.get("reason")),
            limitations=_texts(data.get("limitations")),
        )


@dataclass(frozen=True, slots=True)
class EnvironmentObservation:
    """What an environment showed after (or before) an action.

    ``complete=False`` means the source did not cover the whole scope, so a key
    that is absent proves nothing. That single flag is what stops a missing
    reading from being interpreted as "it did not happen" (§6).
    """

    observation_ref: str
    values: Mapping[str, Any] = field(default_factory=dict)
    complete: bool = False
    quality: float = 1.0
    timestamp: str = ""
    source: str = ""
    kind: EnvironmentKind = EnvironmentKind.SIMULATOR
    note: str = ""
    conflicting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_ref": self.observation_ref,
            "values": dict(self.values),
            "complete": self.complete,
            "quality": self.quality,
            "timestamp": self.timestamp,
            "source": self.source,
            "kind": self.kind.value,
            "note": self.note,
            "conflicting": self.conflicting,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EnvironmentObservation:
        return cls(
            observation_ref=as_text(data.get("observation_ref")),
            values=_mapping(data.get("values")),
            complete=as_bool(data.get("complete")),
            quality=clamp(as_float(data.get("quality"), 1.0), 0.0, 1.0),
            timestamp=as_text(data.get("timestamp")),
            source=as_text(data.get("source")),
            kind=EnvironmentKind(as_text(data.get("kind"), EnvironmentKind.SIMULATOR.value)),
            note=as_text(data.get("note")),
            conflicting=as_bool(data.get("conflicting")),
        )


@dataclass(frozen=True, slots=True)
class MatchingPolicy:
    """How strictly a prediction is compared with an observation.

    Numeric tolerance is explicit because "close enough" is exactly the judgement
    that should be visible in a report rather than buried in a comparison.
    """

    numeric_tolerance: float = 0.0
    per_key_tolerance: Mapping[str, float] = field(default_factory=dict)
    require_complete: bool = True

    def tolerance_for(self, key: str) -> float:
        if key in self.per_key_tolerance:
            return as_float(self.per_key_tolerance[key], self.numeric_tolerance)
        return self.numeric_tolerance

    def to_dict(self) -> dict[str, Any]:
        return {
            "numeric_tolerance": self.numeric_tolerance,
            "per_key_tolerance": dict(self.per_key_tolerance),
            "require_complete": self.require_complete,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> MatchingPolicy:
        return cls(
            numeric_tolerance=as_float(data.get("numeric_tolerance"), 0.0),
            per_key_tolerance={
                str(key): as_float(value)
                for key, value in _mapping(data.get("per_key_tolerance")).items()
            },
            require_complete=as_bool(data.get("require_complete"), True),
        )


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """The verdict of prediction vs observation — including "cannot tell"."""

    kind: OutcomeKind
    matched: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    detail: str = ""
    prediction_basis: PredictionBasis = PredictionBasis.UNAVAILABLE
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "matched": list(self.matched),
            "mismatched": list(self.mismatched),
            "missing": list(self.missing),
            "detail": self.detail,
            "prediction_basis": self.prediction_basis.value,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ComparisonResult:
        return cls(
            kind=OutcomeKind(as_text(data.get("kind"), OutcomeKind.INCONCLUSIVE.value)),
            matched=_texts(data.get("matched")),
            mismatched=_texts(data.get("mismatched")),
            missing=_texts(data.get("missing")),
            detail=as_text(data.get("detail")),
            prediction_basis=PredictionBasis(
                as_text(data.get("prediction_basis"), PredictionBasis.UNAVAILABLE.value)
            ),
            limitations=_texts(data.get("limitations")),
        )


# ── hypotheses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HypothesisVersion:
    """One revision of a hypothesis, kept so a change can be audited (§7)."""

    version: int
    status: HypothesisStatus
    confidence: float | None
    confidence_kind: ConfidenceKind
    statement: str = ""
    reason: str = ""
    timestamp: str = ""
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status.value,
            "confidence": self.confidence,
            "confidence_kind": self.confidence_kind.value,
            "statement": self.statement,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> HypothesisVersion:
        confidence = data.get("confidence")
        return cls(
            version=as_int(data.get("version"), 1),
            status=HypothesisStatus(as_text(data.get("status"), HypothesisStatus.PROPOSED.value)),
            confidence=None if confidence is None else as_float(confidence),
            confidence_kind=ConfidenceKind(
                as_text(data.get("confidence_kind"), ConfidenceKind.INSUFFICIENT_EVIDENCE.value)
            ),
            statement=as_text(data.get("statement")),
            reason=as_text(data.get("reason")),
            timestamp=as_text(data.get("timestamp")),
            evidence_ids=_texts(data.get("evidence_ids")),
        )


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A possible explanation, its evidence, and how much of it is independent.

    ``predicted`` is what this hypothesis expects an experiment to produce, keyed by
    observation key. Two hypotheses that predict the SAME thing for an action are
    not distinguishable by it, which is what experiment selection reads (§5).

    ``independent_support``/``independent_contradiction`` count distinct
    ``source_key``s among the supporting/contradicting evidence; the raw evidence
    tuples count every reading. A hypothesis supported ten times by one source is
    supported once.
    """

    hypothesis_id: str
    objective_id: str
    statement: str
    scope: str = ""
    predicted: Mapping[str, Any] = field(default_factory=dict)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    version: int = 1
    prior_confidence: float | None = None
    confidence: float | None = None
    confidence_kind: ConfidenceKind = ConfidenceKind.INSUFFICIENT_EVIDENCE
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    inconclusive: tuple[str, ...] = ()
    independent_support: int = 0
    independent_contradiction: int = 0
    source_keys: tuple[str, ...] = ()
    #: Source keys behind the SUPPORTING references, and the contradicting ones.
    #: Kept per role so the independent counts stay recomputable from the evidence
    #: rather than being nudged upward by whichever call happens to arrive (§7).
    supporting_sources: tuple[str, ...] = ()
    contradicting_sources: tuple[str, ...] = ()
    experiments: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    versions: tuple[HypothesisVersion, ...] = ()
    superseded_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    limitations: tuple[str, ...] = ()

    @property
    def open_status(self) -> bool:
        """Whether this hypothesis still asks a question."""
        return self.status in {
            HypothesisStatus.PROPOSED,
            HypothesisStatus.TESTABLE,
            HypothesisStatus.INCONCLUSIVE,
        }

    @property
    def uncertainty(self) -> float:
        """How unsettled this hypothesis is, on the 0..1 scale the selector reads.

        A hypothesis nobody has tested is maximally uncertain; one that is
        supported and contradicted by independent sources stays uncertain, which is
        the whole point of counting independence rather than readings.
        """
        if self.confidence is None:
            return 1.0
        return clamp(1.0 - abs(self.confidence - 0.5) * 2.0, 0.0, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "objective_id": self.objective_id,
            "statement": self.statement,
            "scope": self.scope,
            "predicted": dict(self.predicted),
            "status": self.status.value,
            "version": self.version,
            "prior_confidence": self.prior_confidence,
            "confidence": self.confidence,
            "confidence_kind": self.confidence_kind.value,
            "supporting": list(self.supporting),
            "contradicting": list(self.contradicting),
            "inconclusive": list(self.inconclusive),
            "independent_support": self.independent_support,
            "independent_contradiction": self.independent_contradiction,
            "source_keys": list(self.source_keys),
            "supporting_sources": list(self.supporting_sources),
            "contradicting_sources": list(self.contradicting_sources),
            "experiments": list(self.experiments),
            "observations": list(self.observations),
            "versions": [item.to_dict() for item in self.versions],
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Hypothesis:
        prior = data.get("prior_confidence")
        confidence = data.get("confidence")
        return cls(
            hypothesis_id=as_text(data.get("hypothesis_id")),
            objective_id=as_text(data.get("objective_id")),
            statement=as_text(data.get("statement")),
            scope=as_text(data.get("scope")),
            predicted=_mapping(data.get("predicted")),
            status=HypothesisStatus(as_text(data.get("status"), HypothesisStatus.PROPOSED.value)),
            version=as_int(data.get("version"), 1),
            prior_confidence=None if prior is None else as_float(prior),
            confidence=None if confidence is None else as_float(confidence),
            confidence_kind=ConfidenceKind(
                as_text(data.get("confidence_kind"), ConfidenceKind.INSUFFICIENT_EVIDENCE.value)
            ),
            supporting=_texts(data.get("supporting")),
            contradicting=_texts(data.get("contradicting")),
            inconclusive=_texts(data.get("inconclusive")),
            independent_support=as_int(data.get("independent_support")),
            independent_contradiction=as_int(data.get("independent_contradiction")),
            source_keys=_texts(data.get("source_keys")),
            supporting_sources=_texts(data.get("supporting_sources")),
            contradicting_sources=_texts(data.get("contradicting_sources")),
            experiments=_texts(data.get("experiments")),
            observations=_texts(data.get("observations")),
            versions=tuple(
                HypothesisVersion.from_mapping(item)
                for item in data.get("versions", []) or []
                if isinstance(item, Mapping)
            ),
            superseded_by=as_text(data.get("superseded_by")),
            created_at=as_text(data.get("created_at")),
            updated_at=as_text(data.get("updated_at")),
            limitations=_texts(data.get("limitations")),
        )


# ── experiments ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """An action an environment exposes — declared, never discovered by trying."""

    name: str
    description: str = ""
    parameters: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    destructive: bool = False
    reversible: bool = True
    external_side_effect: bool = False
    estimated_seconds: float = 0.05
    simulator: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": list(self.parameters),
            "preconditions": list(self.preconditions),
            "permissions": list(self.permissions),
            "destructive": self.destructive,
            "reversible": self.reversible,
            "external_side_effect": self.external_side_effect,
            "estimated_seconds": self.estimated_seconds,
            "simulator": self.simulator,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ActionSpec:
        return cls(
            name=as_text(data.get("name")),
            description=as_text(data.get("description")),
            parameters=_texts(data.get("parameters")),
            preconditions=_texts(data.get("preconditions")),
            permissions=_texts(data.get("permissions")),
            destructive=as_bool(data.get("destructive")),
            reversible=as_bool(data.get("reversible"), True),
            external_side_effect=as_bool(data.get("external_side_effect")),
            estimated_seconds=as_float(data.get("estimated_seconds"), 0.05),
            simulator=as_bool(data.get("simulator"), True),
        )


@dataclass(frozen=True, slots=True)
class LearningBudget:
    """The ceilings a learning objective runs under. Every one is enforced BEFORE."""

    max_experiments: int = 6
    max_actions_per_experiment: int = 1
    max_seconds_total: float = 60.0
    max_seconds_per_experiment: float = 10.0
    max_retries: int = 1
    max_concurrent: int = 1
    max_cost_units: float = 6.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_experiments": self.max_experiments,
            "max_actions_per_experiment": self.max_actions_per_experiment,
            "max_seconds_total": self.max_seconds_total,
            "max_seconds_per_experiment": self.max_seconds_per_experiment,
            "max_retries": self.max_retries,
            "max_concurrent": self.max_concurrent,
            "max_cost_units": self.max_cost_units,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LearningBudget:
        defaults = cls()
        return cls(
            max_experiments=max(1, as_int(data.get("max_experiments"), defaults.max_experiments)),
            max_actions_per_experiment=max(
                1,
                as_int(
                    data.get("max_actions_per_experiment"),
                    defaults.max_actions_per_experiment,
                ),
            ),
            max_seconds_total=max(
                0.5, as_float(data.get("max_seconds_total"), defaults.max_seconds_total)
            ),
            max_seconds_per_experiment=max(
                0.1,
                as_float(
                    data.get("max_seconds_per_experiment"),
                    defaults.max_seconds_per_experiment,
                ),
            ),
            max_retries=max(0, as_int(data.get("max_retries"), defaults.max_retries)),
            max_concurrent=max(1, as_int(data.get("max_concurrent"), defaults.max_concurrent)),
            max_cost_units=max(1.0, as_float(data.get("max_cost_units"), defaults.max_cost_units)),
        )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """What a learning objective has spent so far."""

    experiments: int = 0
    actions: int = 0
    seconds: float = 0.0
    retries: int = 0
    cost_units: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiments": self.experiments,
            "actions": self.actions,
            "seconds": round(self.seconds, 6),
            "retries": self.retries,
            "cost_units": round(self.cost_units, 6),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> BudgetUsage:
        return cls(
            experiments=as_int(data.get("experiments")),
            actions=as_int(data.get("actions")),
            seconds=as_float(data.get("seconds")),
            retries=as_int(data.get("retries")),
            cost_units=as_float(data.get("cost_units")),
        )


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether one more experiment fits, and what is left if it does not."""

    allowed: bool
    reason: str = ""
    remaining: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "remaining": dict(self.remaining),
        }


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Whether an experiment may act, and which layer decided.

    A simulator action is allowed without asking because it cannot affect anything
    outside the process — and it says so. Anything with an external effect, or that
    a permission layer refuses, is a refusal recorded here rather than an action
    taken and apologised for (§11).
    """

    allowed: bool
    reason: str = ""
    source: str = "policy"
    risk: str = "low"
    requires_approval: bool = False
    approved: bool = False
    destructive: bool = False
    reversible: bool = True
    external_side_effect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "source": self.source,
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            "approved": self.approved,
            "destructive": self.destructive,
            "reversible": self.reversible,
            "external_side_effect": self.external_side_effect,
        }


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """One bounded intervention, fully specified before anything runs.

    ``predicted_by_hypothesis`` is the key field of the whole phase: it is what
    makes an experiment able to DISTINGUISH hypotheses, and it is why a prediction
    is written down before the action rather than reconstructed afterwards.
    """

    experiment_id: str
    objective_id: str
    hypothesis_ids: tuple[str, ...]
    environment_id: str
    action: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    predicted_by_hypothesis: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    observation_method: str = "environment.observe"
    information_value: float | None = None
    information_value_kind: str = "heuristic"
    estimated_seconds: float = 0.05
    estimated_cost_units: float = 1.0
    max_actions: int = 1
    max_seconds: float = 10.0
    max_retries: int = 1
    success_criteria: tuple[str, ...] = ()
    failure_criteria: tuple[str, ...] = ()
    inconclusive_criteria: tuple[str, ...] = ()
    cancellation_conditions: tuple[str, ...] = ()
    destructive: bool = False
    reversible: bool = True
    simulator: bool = True
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    attempts: int = 0
    rejection_reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def needs_approval(self) -> bool:
        """Whether a person (or policy) must approve this before it runs."""
        return not self.simulator or self.destructive

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "objective_id": self.objective_id,
            "hypothesis_ids": list(self.hypothesis_ids),
            "environment_id": self.environment_id,
            "action": self.action,
            "parameters": dict(self.parameters),
            "predicted_by_hypothesis": {
                key: dict(value) for key, value in self.predicted_by_hypothesis.items()
            },
            "preconditions": list(self.preconditions),
            "permissions": list(self.permissions),
            "observation_method": self.observation_method,
            "information_value": self.information_value,
            "information_value_kind": self.information_value_kind,
            "estimated_seconds": self.estimated_seconds,
            "estimated_cost_units": self.estimated_cost_units,
            "max_actions": self.max_actions,
            "max_seconds": self.max_seconds,
            "max_retries": self.max_retries,
            "success_criteria": list(self.success_criteria),
            "failure_criteria": list(self.failure_criteria),
            "inconclusive_criteria": list(self.inconclusive_criteria),
            "cancellation_conditions": list(self.cancellation_conditions),
            "destructive": self.destructive,
            "reversible": self.reversible,
            "simulator": self.simulator,
            "status": self.status.value,
            "attempts": self.attempts,
            "rejection_reason": self.rejection_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ExperimentSpec:
        predicted_raw = _mapping(data.get("predicted_by_hypothesis"))
        value = data.get("information_value")
        return cls(
            experiment_id=as_text(data.get("experiment_id")),
            objective_id=as_text(data.get("objective_id")),
            hypothesis_ids=_texts(data.get("hypothesis_ids")),
            environment_id=as_text(data.get("environment_id")),
            action=as_text(data.get("action")),
            parameters=_mapping(data.get("parameters")),
            predicted_by_hypothesis={
                str(key): _mapping(item) for key, item in predicted_raw.items()
            },
            preconditions=_texts(data.get("preconditions")),
            permissions=_texts(data.get("permissions")),
            observation_method=as_text(data.get("observation_method"), "environment.observe"),
            information_value=None if value is None else as_float(value),
            information_value_kind=as_text(data.get("information_value_kind"), "heuristic"),
            estimated_seconds=as_float(data.get("estimated_seconds"), 0.05),
            estimated_cost_units=as_float(data.get("estimated_cost_units"), 1.0),
            max_actions=max(1, as_int(data.get("max_actions"), 1)),
            max_seconds=as_float(data.get("max_seconds"), 10.0),
            max_retries=max(0, as_int(data.get("max_retries"), 1)),
            success_criteria=_texts(data.get("success_criteria")),
            failure_criteria=_texts(data.get("failure_criteria")),
            inconclusive_criteria=_texts(data.get("inconclusive_criteria")),
            cancellation_conditions=_texts(data.get("cancellation_conditions")),
            destructive=as_bool(data.get("destructive")),
            reversible=as_bool(data.get("reversible"), True),
            simulator=as_bool(data.get("simulator"), True),
            status=ExperimentStatus(as_text(data.get("status"), ExperimentStatus.PROPOSED.value)),
            attempts=as_int(data.get("attempts")),
            rejection_reason=as_text(data.get("rejection_reason")),
            created_at=as_text(data.get("created_at")),
            updated_at=as_text(data.get("updated_at")),
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What executing one action in an environment produced."""

    ok: bool
    status: str
    observation: EnvironmentObservation | None = None
    error: str = ""
    reason: str = ""
    duration_ms: float = 0.0
    actions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "observation": None if self.observation is None else self.observation.to_dict(),
            "error": self.error,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "actions": self.actions,
        }


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    """One attempt at an experiment: prediction, action, observation, comparison."""

    run_id: str
    experiment_id: str
    objective_id: str
    status: ExperimentStatus
    environment_id: str = ""
    environment_kind: EnvironmentKind = EnvironmentKind.SIMULATOR
    prediction: Prediction = field(default_factory=Prediction)
    observation: EnvironmentObservation | None = None
    comparison: ComparisonResult | None = None
    before: EnvironmentObservation | None = None
    actions: int = 0
    retries: int = 0
    duration_ms: float = 0.0
    cost_units: float = 0.0
    error: str = ""
    reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    simulated: bool = True

    @property
    def executed(self) -> bool:
        """Whether any action actually ran. A rejected run is not an experiment."""
        return self.status in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMED_OUT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "objective_id": self.objective_id,
            "status": self.status.value,
            "environment_id": self.environment_id,
            "environment_kind": self.environment_kind.value,
            "prediction": self.prediction.to_dict(),
            "observation": None if self.observation is None else self.observation.to_dict(),
            "comparison": None if self.comparison is None else self.comparison.to_dict(),
            "before": None if self.before is None else self.before.to_dict(),
            "actions": self.actions,
            "retries": self.retries,
            "duration_ms": self.duration_ms,
            "cost_units": self.cost_units,
            "error": self.error,
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "simulated": self.simulated,
            "executed": self.executed,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ExperimentRun:
        prediction = data.get("prediction")
        observation = data.get("observation")
        comparison = data.get("comparison")
        before = data.get("before")
        return cls(
            run_id=as_text(data.get("run_id")),
            experiment_id=as_text(data.get("experiment_id")),
            objective_id=as_text(data.get("objective_id")),
            status=ExperimentStatus(as_text(data.get("status"), ExperimentStatus.PROPOSED.value)),
            environment_id=as_text(data.get("environment_id")),
            environment_kind=EnvironmentKind(
                as_text(data.get("environment_kind"), EnvironmentKind.SIMULATOR.value)
            ),
            prediction=(
                Prediction.from_mapping(prediction)
                if isinstance(prediction, Mapping)
                else Prediction()
            ),
            observation=(
                EnvironmentObservation.from_mapping(observation)
                if isinstance(observation, Mapping)
                else None
            ),
            comparison=(
                ComparisonResult.from_mapping(comparison)
                if isinstance(comparison, Mapping)
                else None
            ),
            before=(
                EnvironmentObservation.from_mapping(before) if isinstance(before, Mapping) else None
            ),
            actions=as_int(data.get("actions")),
            retries=as_int(data.get("retries")),
            duration_ms=as_float(data.get("duration_ms")),
            cost_units=as_float(data.get("cost_units")),
            error=as_text(data.get("error")),
            reason=as_text(data.get("reason")),
            started_at=as_text(data.get("started_at")),
            finished_at=as_text(data.get("finished_at")),
            simulated=as_bool(data.get("simulated"), True),
        )


# ── knowledge ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KnowledgeUpdate:
    """One recorded change to knowledge, with what it was based on and how to undo it."""

    update_id: str
    change: KnowledgeChange
    target_id: str
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    experiment_id: str = ""
    previous_version: int = 0
    new_version: int = 0
    previous_status: str = ""
    new_status: str = ""
    timestamp: str = ""
    reversible: bool = True
    reverted: bool = False
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "change": self.change.value,
            "target_id": self.target_id,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "experiment_id": self.experiment_id,
            "previous_version": self.previous_version,
            "new_version": self.new_version,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "timestamp": self.timestamp,
            "reversible": self.reversible,
            "reverted": self.reverted,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> KnowledgeUpdate:
        return cls(
            update_id=as_text(data.get("update_id")),
            change=KnowledgeChange(as_text(data.get("change"), KnowledgeChange.NO_CHANGE.value)),
            target_id=as_text(data.get("target_id")),
            reason=as_text(data.get("reason")),
            evidence_ids=_texts(data.get("evidence_ids")),
            experiment_id=as_text(data.get("experiment_id")),
            previous_version=as_int(data.get("previous_version")),
            new_version=as_int(data.get("new_version")),
            previous_status=as_text(data.get("previous_status")),
            new_status=as_text(data.get("new_status")),
            timestamp=as_text(data.get("timestamp")),
            reversible=as_bool(data.get("reversible"), True),
            reverted=as_bool(data.get("reverted")),
            limitations=_texts(data.get("limitations")),
        )


@dataclass(frozen=True, slots=True)
class LearnedRule:
    """A scoped rule learned from evidence — kept apart from the observations behind it.

    ``condition`` and ``outcome`` are declarative key/value facts (``{"action": …}``
    → ``{"value": …}``), never stored code: a rule that carried something executable
    would be a second, unreviewed way to act (§7).

    ``scope`` is the environment the rule was tested in, and ``generalizes_to`` stays
    empty until a held-out case confirms it elsewhere. A provisional rule is the
    honest state after one or two experiments; it is not weaker evidence, it is the
    correct label (§8).
    """

    rule_id: str
    objective_id: str
    statement: str
    condition: Mapping[str, Any] = field(default_factory=dict)
    outcome: Mapping[str, Any] = field(default_factory=dict)
    scope: str = ""
    generalizes_to: tuple[str, ...] = ()
    status: RuleStatus = RuleStatus.PROVISIONAL
    version: int = 1
    supporting: tuple[str, ...] = ()
    contradicting: tuple[str, ...] = ()
    independent_support: int = 0
    experiments: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    superseded_by: str = ""
    limitations: tuple[str, ...] = ()

    def applies_to(self, scope: str) -> bool:
        """Whether this rule claims to hold in a scope — provisional rules stay home."""
        if not scope or not self.scope:
            return True
        return scope == self.scope or scope in self.generalizes_to

    def predicts(self, action: str) -> Mapping[str, Any] | None:
        """What this rule expects for an action, or ``None`` when it says nothing."""
        if as_text(self.condition.get("action")) != action:
            return None
        return self.outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "objective_id": self.objective_id,
            "statement": self.statement,
            "condition": dict(self.condition),
            "outcome": dict(self.outcome),
            "scope": self.scope,
            "generalizes_to": list(self.generalizes_to),
            "status": self.status.value,
            "version": self.version,
            "supporting": list(self.supporting),
            "contradicting": list(self.contradicting),
            "independent_support": self.independent_support,
            "experiments": list(self.experiments),
            "provenance": list(self.provenance),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by": self.superseded_by,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LearnedRule:
        return cls(
            rule_id=as_text(data.get("rule_id")),
            objective_id=as_text(data.get("objective_id")),
            statement=as_text(data.get("statement")),
            condition=_mapping(data.get("condition")),
            outcome=_mapping(data.get("outcome")),
            scope=as_text(data.get("scope")),
            generalizes_to=_texts(data.get("generalizes_to")),
            status=RuleStatus(as_text(data.get("status"), RuleStatus.PROVISIONAL.value)),
            version=as_int(data.get("version"), 1),
            supporting=_texts(data.get("supporting")),
            contradicting=_texts(data.get("contradicting")),
            independent_support=as_int(data.get("independent_support")),
            experiments=_texts(data.get("experiments")),
            provenance=_texts(data.get("provenance")),
            created_at=as_text(data.get("created_at")),
            updated_at=as_text(data.get("updated_at")),
            superseded_by=as_text(data.get("superseded_by")),
            limitations=_texts(data.get("limitations")),
        )


# ── objectives, selection, cycles ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LearningObjective:
    """A bounded question the system is allowed to spend effort answering."""

    objective_id: str
    question: str
    environment_id: str = ""
    scope: str = ""
    status: ObjectiveStatus = ObjectiveStatus.OPEN
    budget: LearningBudget = field(default_factory=LearningBudget)
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    hypotheses: tuple[str, ...] = ()
    experiments: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    correlation_id: str = ""
    stop_reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "question": self.question,
            "environment_id": self.environment_id,
            "scope": self.scope,
            "status": self.status.value,
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "hypotheses": list(self.hypotheses),
            "experiments": list(self.experiments),
            "rules": list(self.rules),
            "correlation_id": self.correlation_id,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LearningObjective:
        budget = data.get("budget")
        usage = data.get("usage")
        return cls(
            objective_id=as_text(data.get("objective_id")),
            question=as_text(data.get("question")),
            environment_id=as_text(data.get("environment_id")),
            scope=as_text(data.get("scope")),
            status=ObjectiveStatus(as_text(data.get("status"), ObjectiveStatus.OPEN.value)),
            budget=(
                LearningBudget.from_mapping(budget)
                if isinstance(budget, Mapping)
                else LearningBudget()
            ),
            usage=(
                BudgetUsage.from_mapping(usage) if isinstance(usage, Mapping) else BudgetUsage()
            ),
            hypotheses=_texts(data.get("hypotheses")),
            experiments=_texts(data.get("experiments")),
            rules=_texts(data.get("rules")),
            correlation_id=as_text(data.get("correlation_id")),
            stop_reason=as_text(data.get("stop_reason")),
            created_at=as_text(data.get("created_at")),
            updated_at=as_text(data.get("updated_at")),
            limitations=_texts(data.get("limitations")),
        )


@dataclass(frozen=True, slots=True)
class InformationValue:
    """A heuristic ranking, labelled as one — never presented as Bayesian gain."""

    score: float
    kind: str = "heuristic"
    components: Mapping[str, float] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 6),
            "kind": self.kind,
            "components": {key: round(value, 6) for key, value in self.components.items()},
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class CuriosityScore:
    """How much an experiment is expected to TEACH — bounded and explained."""

    score: float
    kind: str = "heuristic"
    components: Mapping[str, float] = field(default_factory=dict)
    penalties: Mapping[str, float] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 6),
            "kind": self.kind,
            "components": {key: round(value, 6) for key, value in self.components.items()},
            "penalties": {key: round(value, 6) for key, value in self.penalties.items()},
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class CandidateExperiment:
    """One candidate the selector considered, whether or not it was chosen."""

    experiment: ExperimentSpec
    strategy: str = ""
    selected: bool = False
    rejected: bool = False
    reason: str = ""
    information_value: float | None = None
    curiosity: float | None = None
    safety: SafetyDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment": self.experiment.to_dict(),
            "strategy": self.strategy,
            "selected": self.selected,
            "rejected": self.rejected,
            "reason": self.reason,
            "information_value": self.information_value,
            "curiosity": self.curiosity,
            "safety": None if self.safety is None else self.safety.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LearningCycleStep:
    """One step of the cycle as it actually happened — a trace, not a plan."""

    kind: StepKind
    detail: str = ""
    index: int = 0
    timestamp: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "detail": self.detail,
            "index": self.index,
            "timestamp": self.timestamp,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class LearningCycleReport:
    """The outcome of running one objective's cycle to a terminal state."""

    objective_id: str
    status: ObjectiveStatus
    environment_id: str = ""
    environment_kind: EnvironmentKind = EnvironmentKind.SIMULATOR
    simulated: bool = True
    steps: tuple[LearningCycleStep, ...] = ()
    experiment_id: str = ""
    run: ExperimentRun | None = None
    comparison: ComparisonResult | None = None
    updates: tuple[KnowledgeUpdate, ...] = ()
    rules: tuple[str, ...] = ()
    stop_reason: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    hypotheses: tuple[Hypothesis, ...] = ()
    limitations: tuple[str, ...] = ()
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "status": self.status.value,
            "environment_id": self.environment_id,
            "environment_kind": self.environment_kind.value,
            "simulated": self.simulated,
            "steps": [item.to_dict() for item in self.steps],
            "experiment_id": self.experiment_id,
            "run": None if self.run is None else self.run.to_dict(),
            "comparison": None if self.comparison is None else self.comparison.to_dict(),
            "updates": [item.to_dict() for item in self.updates],
            "rules": list(self.rules),
            "stop_reason": self.stop_reason,
            "metrics": dict(self.metrics),
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "limitations": list(self.limitations),
            "correlation_id": self.correlation_id,
        }


def with_updates[T](record: T, **changes: Any) -> T:
    """A frozen record with fields replaced — the one way these records change.

    Generic on purpose: a caller gets back the type it passed in, so a record with
    its own fields does not silently degrade to ``Any`` at every update site.
    """
    return replace(record, **changes)  # type: ignore[type-var]


def elapsed_seconds(started: str, finished: str) -> float:
    """Seconds between two stored timestamps, or ``0.0`` when either is unreadable."""
    value = seconds_between(started, finished)
    return 0.0 if value is None else value


def is_readable_timestamp(value: Any) -> bool:
    """Whether a value parses as a timestamp — used by restore, not by policy."""
    return parse_timestamp(value) is not None
