"""Phase 16's vocabulary: datasets, examples, runs, checkpoints, models.

This module is the shared language of the supervised-fine-tuning subsystem. It
defines what a training EXAMPLE is (structured input, structured target), what a
versioned DATASET is, what a training RUN and its CHECKPOINT look like, what the
resource ESTIMATE says before anything starts, and what a trained MODEL record
carries once a run exists.

Three rules shape every field here, and they are the phase's own instructions:

  * **structured, never free-form.** A target is a mapping — the same shape the
    live layers already produce (a StructuredIntent, a Decision, a plan, a tool
    call). The one thing that is deliberately absent is hidden reasoning: no
    field stores a chain-of-thought, and :func:`reasoning_violations` exists so
    an imported row that carries one is REJECTED rather than trained on.
  * **a figure is either measured or absent.** Loss, memory and latency are
    ``None`` when nobody measured them, never a zero that would read as free.
  * **immutability where it matters.** Datasets, runs and model records are
    frozen dataclasses; enriching one returns a new one. A training run that
    references ``name@version`` is referencing a row that cannot change under it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.audit.models import json_safe
from novacontrol.evaluation.models import TRAJECTORY_SCHEMA_VERSION, now_iso

#: Versions carried by every stored row, so a measurement can be read later
#: knowing which rules produced it.
TRAINING_SCHEMA_VERSION = 1
DATASET_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
TRAINING_MODEL_SCHEMA_VERSION = 1
TRAINING_EVALUATION_SCHEMA_VERSION = 1

#: The preprocessing generation that produced the examples. Bumped when the
#: example stringification changes, so a dataset can be regenerated from the
#: same source rows and compared honestly.
PREPROCESSING_VERSION = "phase16.1"

#: The three splits, in the order they are reported.
SPLIT_NAMES: tuple[str, ...] = ("train", "validation", "test")

#: How many loss points a run keeps. A curve, bounded — not a file.
MAX_LOSS_POINTS = 200


class DatasetType(StrEnum):
    """A task family, one shape of input and target each.

    The builder does not force every trajectory into one format: an NLU example
    teaches "request → StructuredIntent", a tool-selection example teaches
    "task + tools → tool + arguments". A dataset names exactly ONE family, so
    its examples share one target schema.
    """

    NLU = "nlu"
    DECISION = "decision"
    TOOL_SELECTION = "tool_selection"
    PLANNING = "planning"
    RECOVERY = "recovery"
    DEVELOPER = "developer"
    RESEARCH = "research"


class Difficulty(StrEnum):
    """How hard one example is, from the shape of the work it came from."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class HardwarePolicy(StrEnum):
    """Where training is allowed to run. ``AUTO`` prefers GPU, then NPU, then CPU."""

    AUTO = "auto"
    LOCAL_CPU = "local_cpu"
    LOCAL_GPU = "local_gpu"
    LOCAL_NPU = "local_npu"


class TrainingMethod(StrEnum):
    """How the base model is adapted.

    ``LORA`` is the default because a 16 GB machine can fine-tune an adapter
    where it could not fine-tune a full model; ``FULL`` exists for a machine
    that genuinely has the memory, and the resource estimator will say so.
    """

    LORA = "lora"
    QLORA = "qlora"
    FULL = "full"


class TrainingRunStatus(StrEnum):
    """Where a training run is in its lifecycle."""

    CREATED = "created"
    VALIDATING = "validating"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """Whether the run has stopped for good."""
        return self in {
            TrainingRunStatus.COMPLETED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELLED,
        }


class ResourceVerdict(StrEnum):
    """What the resource estimate says about starting."""

    SAFE = "safe"
    WARNING = "warning"
    UNSAFE = "unsafe"


class ModelStatus(StrEnum):
    """A trained model's place in the registry.

    Promotion is explicit by design: a run that completed registers as
    ``EXPERIMENTAL`` and nothing about training promotes it further.
    """

    EXPERIMENTAL = "experimental"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    PRODUCTION = "production"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


#: Which status transitions the registry will accept. A rejected model may be
#: re-evaluated (the data changed); a deprecated or production model may only
#: move forward through the same explicit gates.
ALLOWED_STATUS_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ModelStatus.EXPERIMENTAL.value: frozenset(
        {
            ModelStatus.EVALUATING.value,
            ModelStatus.REJECTED.value,
            ModelStatus.DEPRECATED.value,
        }
    ),
    ModelStatus.EVALUATING.value: frozenset(
        {
            ModelStatus.APPROVED.value,
            ModelStatus.REJECTED.value,
            ModelStatus.EXPERIMENTAL.value,
        }
    ),
    ModelStatus.APPROVED.value: frozenset(
        {
            ModelStatus.PRODUCTION.value,
            ModelStatus.DEPRECATED.value,
            ModelStatus.REJECTED.value,
        }
    ),
    ModelStatus.PRODUCTION.value: frozenset({ModelStatus.DEPRECATED.value}),
    ModelStatus.DEPRECATED.value: frozenset(),
    ModelStatus.REJECTED.value: frozenset({ModelStatus.EXPERIMENTAL.value}),
}

# ── hidden-reasoning guard ───────────────────────────────────────────────────


def _normalised_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


#: Key names that mean "this is private reasoning". Normalised (case and
#: punctuation removed) before comparison, so ``"chain_of_thought"`` and
#: ``"Chain-Of-Thought"`` are caught by the same entry. The guard exists for
#: imported or hand-edited rows: Phase 15 never stores these, and Phase 16
#: refuses to train on a row that does.
FORBIDDEN_REASONING_KEYS: frozenset[str] = frozenset(
    {
        "chainofthought",
        "chainofthoughts",
        "cot",
        "reasoning",
        "reasoningtrace",
        "thinking",
        "thought",
        "thoughts",
        "scratchpad",
        "innermonologue",
        "hiddenreasoning",
        "privateReasoning".lower(),
    }
)


def reasoning_violations(value: Any, *, path: str = "value") -> tuple[str, ...]:
    """Every place a value carries a hidden-reasoning key, named by its path.

    Recursive over mappings and sequences, because a reasoning trace hides just
    as well one level down (``{"metadata": {"reasoning": "..."}}``). This is a
    structural check, not a semantic one: no field is named that way in a
    NovaControl record, and any row that is gets refused.
    """
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = _normalised_key(key)
            here = f"{path}.{key}"
            if name in FORBIDDEN_REASONING_KEYS:
                found.append(here)
            found.extend(reasoning_violations(item, path=here))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(reasoning_violations(item, path=f"{path}[{index}]"))
    return tuple(found)


# ── small parsers (each field is read defensively; a store must load) ────────


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value)
    return text or default


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    return {}


def _mappings(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_mapping(row) for row in value if isinstance(row, Mapping))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    parsed = _number(value)
    return None if parsed is None else int(parsed)


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value)
    return ()


# ── training examples ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SFTTrainingExample:
    """One supervised example: a structured input and the structured target.

    ``input`` and ``context`` are what the model sees; ``target`` is what it
    should produce — a mapping, never prose. Everything else is provenance: the
    trajectory and evaluation the example came from, the quality verdict the
    source row received, how hard it is, and which dataset version it landed in.
    """

    example_id: str = field(default_factory=lambda: uuid4().hex)
    dataset_type: str = DatasetType.NLU.value
    input: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    target: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source_trajectory_id: str = ""
    source_evaluation_id: str = ""
    quality_status: str = ""
    difficulty: str = Difficulty.SIMPLE.value
    tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    dataset_version: str = ""
    schema_version: int = TRAINING_SCHEMA_VERSION

    # -- reading ---------------------------------------------------------------

    @property
    def group_key(self) -> str:
        """The unit that must not be split across train and test.

        One trajectory's examples are one group (they are the same work seen
        from two angles), and a session named in metadata groups several
        trajectories. The key is written at build time; this falls back to the
        source trajectory, then to the example itself, so an imported example
        is never split by accident.
        """
        named = str(self.metadata.get("group_key", "")).strip()
        if named:
            return named
        if self.source_trajectory_id:
            return self.source_trajectory_id
        return self.example_id

    @property
    def estimated_tokens(self) -> int:
        """A cheap size reading (serialised length / 4). Not a tokenizer."""
        try:
            payload = json.dumps(
                {
                    "input": dict(self.input),
                    "context": dict(self.context),
                    "target": dict(self.target),
                },
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            return 0
        return max(1, len(payload) // 4)

    def fingerprint(self) -> str:
        """A stable identity, so the same example cannot enter twice."""
        payload = json.dumps(
            {
                "type": self.dataset_type,
                "input": dict(self.input),
                "context": dict(self.context),
                "target": dict(self.target),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def reasoning_violations(self) -> tuple[str, ...]:
        """Any hidden-reasoning keys in this example (empty is the only good answer)."""
        return reasoning_violations(
            {
                "input": dict(self.input),
                "context": dict(self.context),
                "target": dict(self.target),
                "metadata": dict(self.metadata),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "dataset_type": self.dataset_type,
            "input": dict(self.input),
            "context": dict(self.context),
            "target": dict(self.target),
            "metadata": dict(self.metadata),
            "source_trajectory_id": self.source_trajectory_id,
            "source_evaluation_id": self.source_evaluation_id,
            "quality_status": self.quality_status,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "dataset_version": self.dataset_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SFTTrainingExample:
        return cls(
            example_id=_text(data.get("example_id")) or uuid4().hex,
            dataset_type=_text(data.get("dataset_type"), DatasetType.NLU.value),
            input=_mapping(data.get("input")),
            context=_mapping(data.get("context")),
            target=_mapping(data.get("target")),
            metadata=_mapping(data.get("metadata")),
            source_trajectory_id=_text(data.get("source_trajectory_id")),
            source_evaluation_id=_text(data.get("source_evaluation_id")),
            quality_status=_text(data.get("quality_status")),
            difficulty=_text(data.get("difficulty"), Difficulty.SIMPLE.value),
            tags=_texts(data.get("tags")),
            created_at=_text(data.get("created_at")) or now_iso(),
            dataset_version=_text(data.get("dataset_version")),
            schema_version=_whole(data.get("schema_version")) or TRAINING_SCHEMA_VERSION,
        )


# ── dataset configuration ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SelectionRules:
    """Which Phase 15 rows may become examples, and when to stop.

    These are the operator's lines, not constants: a dataset built for safety
    work may want refusals; one built for routing may not care about reward.
    ``quality`` defaults to accepted rows only — a successful trajectory is not
    automatically a good example, and a held one is held for a reason.
    """

    quality: tuple[str, ...] = ("accepted",)
    require_success: bool = True
    require_verification: bool = False
    #: When verification records exist, every one of them must have passed.
    verification_must_pass: bool = True
    min_reward: float | None = None
    min_evaluation_score: float | None = None
    evaluation_dimension: str = ""
    model: str = ""
    task_category: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    since: str = ""
    until: str = ""
    max_examples: int = 0
    include_developer: bool = True
    include_research: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "quality": list(self.quality),
            "require_success": self.require_success,
            "require_verification": self.require_verification,
            "verification_must_pass": self.verification_must_pass,
            "min_reward": self.min_reward,
            "min_evaluation_score": self.min_evaluation_score,
            "evaluation_dimension": self.evaluation_dimension,
            "model": self.model,
            "task_category": self.task_category,
            "source": self.source,
            "tags": list(self.tags),
            "since": self.since,
            "until": self.until,
            "max_examples": self.max_examples,
            "include_developer": self.include_developer,
            "include_research": self.include_research,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SelectionRules:
        defaults = cls()
        quality = _texts(data.get("quality"))
        min_reward = _number(data.get("min_reward"))
        min_score = _number(data.get("min_evaluation_score"))
        return cls(
            quality=quality if quality else defaults.quality,
            require_success=(
                data["require_success"]
                if isinstance(data.get("require_success"), bool)
                else defaults.require_success
            ),
            require_verification=(
                data["require_verification"]
                if isinstance(data.get("require_verification"), bool)
                else defaults.require_verification
            ),
            verification_must_pass=(
                data["verification_must_pass"]
                if isinstance(data.get("verification_must_pass"), bool)
                else defaults.verification_must_pass
            ),
            min_reward=min_reward,
            min_evaluation_score=min_score,
            evaluation_dimension=_text(data.get("evaluation_dimension")),
            model=_text(data.get("model")),
            task_category=_text(data.get("task_category")),
            source=_text(data.get("source")),
            tags=_texts(data.get("tags")),
            since=_text(data.get("since")),
            until=_text(data.get("until")),
            max_examples=max(0, _whole(data.get("max_examples")) or 0),
            include_developer=(
                data["include_developer"]
                if isinstance(data.get("include_developer"), bool)
                else defaults.include_developer
            ),
            include_research=(
                data["include_research"]
                if isinstance(data.get("include_research"), bool)
                else defaults.include_research
            ),
        )


@dataclass(frozen=True, slots=True)
class SplitConfig:
    """How a built dataset is divided, deterministically and without leakage.

    Whole GROUPS are assigned to one split: every example that came from the
    same trajectory (or session) travels together, so a model cannot be tested
    on a task whose other half it trained on.
    """

    train_ratio: float = 0.8
    validation_ratio: float = 0.1
    test_ratio: float = 0.1
    seed: int = 42
    #: "task" groups by task/session; "example" is only for a dataset whose
    #: examples are already independent (an import, a synthetic set).
    group_by: str = "task"

    def ratios(self) -> dict[str, float]:
        return {
            "train": self.train_ratio,
            "validation": self.validation_ratio,
            "test": self.test_ratio,
        }

    def issues(self) -> tuple[str, ...]:
        found: list[str] = []
        ratios = self.ratios()
        if any(value < 0 for value in ratios.values()):
            found.append("split ratios must not be negative")
        total = sum(ratios.values())
        if abs(total - 1.0) > 1e-6:
            found.append(f"split ratios must sum to 1.0 (they sum to {total:.4f})")
        if self.group_by not in {"task", "example"}:
            found.append(f"unknown group_by {self.group_by!r}: use 'task' or 'example'")
        return tuple(found)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "test_ratio": self.test_ratio,
            "seed": self.seed,
            "group_by": self.group_by,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SplitConfig:
        defaults = cls()
        ratios: dict[str, float] = {}
        for name, fallback in (
            ("train_ratio", defaults.train_ratio),
            ("validation_ratio", defaults.validation_ratio),
            ("test_ratio", defaults.test_ratio),
        ):
            value = _number(data.get(name))
            ratios[name] = fallback if value is None else max(0.0, min(1.0, value))
        seed = _whole(data.get("seed"))
        return cls(
            train_ratio=ratios["train_ratio"],
            validation_ratio=ratios["validation_ratio"],
            test_ratio=ratios["test_ratio"],
            seed=defaults.seed if seed is None else seed,
            group_by=_text(data.get("group_by"), defaults.group_by),
        )


# ── the built dataset ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatasetStatistics:
    """What the build saw and what it kept, so a version explains itself."""

    total: int = 0
    by_difficulty: Mapping[str, int] = field(default_factory=dict)
    by_quality: Mapping[str, int] = field(default_factory=dict)
    by_split: Mapping[str, int] = field(default_factory=dict)
    groups: int = 0
    source_trajectories: int = 0
    duplicates_removed: int = 0
    sensitive_removed: int = 0
    malformed_removed: int = 0
    #: Why rows were skipped, keyed by a short reason code.
    skipped: Mapping[str, int] = field(default_factory=dict)
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_difficulty": dict(self.by_difficulty),
            "by_quality": dict(self.by_quality),
            "by_split": dict(self.by_split),
            "groups": self.groups,
            "source_trajectories": self.source_trajectories,
            "duplicates_removed": self.duplicates_removed,
            "sensitive_removed": self.sensitive_removed,
            "malformed_removed": self.malformed_removed,
            "skipped": dict(self.skipped),
            "estimated_tokens": self.estimated_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DatasetStatistics:
        def counts(key: str) -> dict[str, int]:
            value = data.get(key)
            if not isinstance(value, Mapping):
                return {}
            return {
                str(name): int(_whole(item) or 0)
                for name, item in value.items()
            }

        return cls(
            total=max(0, _whole(data.get("total")) or 0),
            by_difficulty=counts("by_difficulty"),
            by_quality=counts("by_quality"),
            by_split=counts("by_split"),
            groups=max(0, _whole(data.get("groups")) or 0),
            source_trajectories=max(0, _whole(data.get("source_trajectories")) or 0),
            duplicates_removed=max(0, _whole(data.get("duplicates_removed")) or 0),
            sensitive_removed=max(0, _whole(data.get("sensitive_removed")) or 0),
            malformed_removed=max(0, _whole(data.get("malformed_removed")) or 0),
            skipped=counts("skipped"),
            estimated_tokens=max(0, _whole(data.get("estimated_tokens")) or 0),
        )


@dataclass(frozen=True, slots=True)
class SFTDatasetVersion:
    """An immutable, versioned set of training examples.

    ``dataset_version_id`` is ``name@version`` — the string a training run
    stores and the only thing it needs to say which data trained it. The
    ``splits`` mapping holds example IDs (not copies), so the examples live once
    and the split is a view over them.
    """

    dataset_version_id: str = ""
    name: str = ""
    version: str = ""
    dataset_type: str = DatasetType.NLU.value
    description: str = ""
    examples: tuple[SFTTrainingExample, ...] = ()
    splits: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    selection: Mapping[str, Any] = field(default_factory=dict)
    split_config: Mapping[str, Any] = field(default_factory=dict)
    statistics: DatasetStatistics = field(default_factory=DatasetStatistics)
    source_data_version: str = ""
    preprocessing_version: str = PREPROCESSING_VERSION
    tags: tuple[str, ...] = ()
    schema_version: int = DATASET_SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)

    def __len__(self) -> int:
        return len(self.examples)

    def example(self, example_id: str) -> SFTTrainingExample | None:
        for example in self.examples:
            if example.example_id == example_id:
                return example
        return None

    def split(self, name: str) -> tuple[SFTTrainingExample, ...]:
        """The examples in one split, in stored order."""
        wanted = set(self.splits.get(name, ()))
        return tuple(example for example in self.examples if example.example_id in wanted)

    def split_names(self) -> tuple[str, ...]:
        return tuple(name for name in SPLIT_NAMES if self.splits.get(name))

    def fingerprint(self) -> str:
        """A content hash, so a re-build of the same data is recognisable."""
        payload = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "type": self.dataset_type,
                "examples": [example.fingerprint() for example in self.examples],
                "splits": {key: list(value) for key, value in self.splits.items()},
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version_id": self.dataset_version_id,
            "name": self.name,
            "version": self.version,
            "dataset_type": self.dataset_type,
            "description": self.description,
            "examples": [example.to_dict() for example in self.examples],
            "splits": {name: list(ids) for name, ids in self.splits.items()},
            "selection": dict(self.selection),
            "split_config": dict(self.split_config),
            "statistics": self.statistics.to_dict(),
            "source_data_version": self.source_data_version,
            "preprocessing_version": self.preprocessing_version,
            "tags": list(self.tags),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SFTDatasetVersion:
        rows = data.get("examples")
        examples: list[SFTTrainingExample] = []
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if isinstance(row, Mapping):
                    examples.append(SFTTrainingExample.from_dict(row))
        splits_value = data.get("splits")
        splits: dict[str, tuple[str, ...]] = {}
        if isinstance(splits_value, Mapping):
            for name, ids in splits_value.items():
                splits[str(name)] = _texts(ids)
        stats = data.get("statistics")
        return cls(
            dataset_version_id=_text(data.get("dataset_version_id")),
            name=_text(data.get("name")),
            version=_text(data.get("version")),
            dataset_type=_text(data.get("dataset_type"), DatasetType.NLU.value),
            description=_text(data.get("description")),
            examples=tuple(examples),
            splits=splits,
            selection=_mapping(data.get("selection")),
            split_config=_mapping(data.get("split_config")),
            statistics=DatasetStatistics.from_dict(stats if isinstance(stats, Mapping) else {}),
            source_data_version=_text(data.get("source_data_version")),
            preprocessing_version=_text(data.get("preprocessing_version"), PREPROCESSING_VERSION),
            tags=_texts(data.get("tags")),
            schema_version=_whole(data.get("schema_version")) or DATASET_SCHEMA_VERSION,
            created_at=_text(data.get("created_at")) or now_iso(),
        )


# ── resource estimation ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResourceEstimate:
    """What a configuration is expected to cost, before anything starts.

    Every figure carries where it came from in ``reasons``; a figure nobody
    could measure is ``None``, and an estimate built on an unknown is never
    ``SAFE`` — "could not tell" must not read as a yes.
    """

    level: str = ResourceVerdict.WARNING.value
    reasons: tuple[str, ...] = ()
    backend: str = ""
    policy: str = HardwarePolicy.AUTO.value
    dry_run: bool = True
    model_size_bytes: int | None = None
    required_bytes: int = 0
    available_bytes: int | None = None
    usable_bytes: int | None = None
    headroom_bytes: int = 0
    example_count: int = 0
    estimated_tokens: int = 0
    trainable_parameters: int | None = None
    components: Mapping[str, int] = field(default_factory=dict)
    hardware: Mapping[str, Any] = field(default_factory=dict)
    backend_available: bool = False
    timestamp: str = field(default_factory=now_iso)

    @property
    def allows_training(self) -> bool:
        return self.level != ResourceVerdict.UNSAFE.value

    @property
    def override_required(self) -> bool:
        """Whether starting needs an operator's explicit override."""
        return self.level == ResourceVerdict.UNSAFE.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "backend": self.backend,
            "policy": self.policy,
            "dry_run": self.dry_run,
            "model_size_bytes": self.model_size_bytes,
            "required_bytes": self.required_bytes,
            "available_bytes": self.available_bytes,
            "usable_bytes": self.usable_bytes,
            "headroom_bytes": self.headroom_bytes,
            "example_count": self.example_count,
            "estimated_tokens": self.estimated_tokens,
            "trainable_parameters": self.trainable_parameters,
            "components": dict(self.components),
            "hardware": dict(self.hardware),
            "backend_available": self.backend_available,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourceEstimate:
        components = data.get("components")
        return cls(
            level=_text(data.get("level"), ResourceVerdict.WARNING.value),
            reasons=_texts(data.get("reasons")),
            backend=_text(data.get("backend")),
            policy=_text(data.get("policy"), HardwarePolicy.AUTO.value),
            dry_run=bool(data.get("dry_run", True)),
            model_size_bytes=_whole(data.get("model_size_bytes")),
            required_bytes=max(0, _whole(data.get("required_bytes")) or 0),
            available_bytes=_whole(data.get("available_bytes")),
            usable_bytes=_whole(data.get("usable_bytes")),
            headroom_bytes=max(0, _whole(data.get("headroom_bytes")) or 0),
            example_count=max(0, _whole(data.get("example_count")) or 0),
            estimated_tokens=max(0, _whole(data.get("estimated_tokens")) or 0),
            trainable_parameters=_whole(data.get("trainable_parameters")),
            components={
                str(name): max(0, _whole(value) or 0)
                for name, value in components.items()
            }
            if isinstance(components, Mapping)
            else {},
            hardware=_mapping(data.get("hardware")),
            backend_available=bool(data.get("backend_available", False)),
            timestamp=_text(data.get("timestamp")) or now_iso(),
        )


# ── training runs ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TrainingRun:
    """One training attempt, from CREATED to a terminal status.

    Mutable reality is expressed by returning a new run: the manager replaces
    the stored row on every step summary, so a reader always sees the last
    written state and never a half-updated one.
    """

    run_id: str = field(default_factory=lambda: uuid4().hex)
    name: str = ""
    model: str = ""
    dataset_version: str = ""
    dataset_type: str = ""
    training_config: Mapping[str, Any] = field(default_factory=dict)
    status: str = TrainingRunStatus.CREATED.value
    backend: str = ""
    #: The objective this run optimises: ``sft`` for a supervised run, ``dpo`` or
    #: ``orpo`` for a preference one. A variant of the same run rather than a
    #: second record type, so the orchestration, the checkpoints and the registry
    #: stay shared.
    algorithm: str = "sft"
    #: Objective-specific readings (a preference margin, a simulated flag). Kept
    #: apart from ``evaluation_metrics``, which is what the evaluator measured.
    preference_metrics: Mapping[str, Any] = field(default_factory=dict)
    #: Phase 18's readings for a reinforcement-learning run: reward statistics,
    #: the integrity breakdown and the rollout/policy summary. Empty everywhere
    #: else, so a Phase 16 or 17 row parses unchanged.
    rl_metrics: Mapping[str, Any] = field(default_factory=dict)
    start_time: str = ""
    end_time: str = ""
    current_epoch: int = 0
    current_step: int = 0
    total_steps: int = 0
    training_loss: float | None = None
    validation_loss: float | None = None
    best_checkpoint_id: str = ""
    evaluation_metrics: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_paths: tuple[str, ...] = ()
    resource_usage: Mapping[str, Any] = field(default_factory=dict)
    estimate: Mapping[str, Any] = field(default_factory=dict)
    loss_history: tuple[Mapping[str, Any], ...] = ()
    error: str = ""
    random_seed: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: int = RUN_SCHEMA_VERSION

    @property
    def terminal(self) -> bool:
        return self.status in {member.value for member in TrainingRunStatus if member.terminal}

    @property
    def elapsed_seconds(self) -> float | None:
        """Measured seconds between start and end, or ``None`` if unmeasured."""
        if not self.start_time or not self.end_time:
            return None
        try:
            from datetime import datetime

            return max(
                0.0,
                (
                    datetime.fromisoformat(self.end_time)
                    - datetime.fromisoformat(self.start_time)
                ).total_seconds(),
            )
        except ValueError:
            return None

    def with_status(self, status: TrainingRunStatus | str, **updates: Any) -> TrainingRun:
        value = status.value if isinstance(status, TrainingRunStatus) else str(status)
        return replace(self, status=value, updated_at=now_iso(), **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "model": self.model,
            "dataset_version": self.dataset_version,
            "dataset_type": self.dataset_type,
            "training_config": dict(self.training_config),
            "status": self.status,
            "backend": self.backend,
            "algorithm": self.algorithm,
            "preference_metrics": dict(self.preference_metrics),
            "rl_metrics": dict(self.rl_metrics),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "current_epoch": self.current_epoch,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "training_loss": self.training_loss,
            "validation_loss": self.validation_loss,
            "best_checkpoint_id": self.best_checkpoint_id,
            "evaluation_metrics": dict(self.evaluation_metrics),
            "checkpoint_paths": list(self.checkpoint_paths),
            "resource_usage": dict(self.resource_usage),
            "estimate": dict(self.estimate),
            "loss_history": [dict(point) for point in self.loss_history],
            "error": self.error,
            "random_seed": self.random_seed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrainingRun:
        return cls(
            run_id=_text(data.get("run_id")) or uuid4().hex,
            name=_text(data.get("name")),
            model=_text(data.get("model")),
            dataset_version=_text(data.get("dataset_version")),
            dataset_type=_text(data.get("dataset_type")),
            training_config=_mapping(data.get("training_config")),
            status=_text(data.get("status"), TrainingRunStatus.CREATED.value),
            backend=_text(data.get("backend")),
            algorithm=_text(data.get("algorithm"), "sft"),
            preference_metrics=_mapping(data.get("preference_metrics")),
            rl_metrics=_mapping(data.get("rl_metrics")),
            start_time=_text(data.get("start_time")),
            end_time=_text(data.get("end_time")),
            current_epoch=max(0, _whole(data.get("current_epoch")) or 0),
            current_step=max(0, _whole(data.get("current_step")) or 0),
            total_steps=max(0, _whole(data.get("total_steps")) or 0),
            training_loss=_number(data.get("training_loss")),
            validation_loss=_number(data.get("validation_loss")),
            best_checkpoint_id=_text(data.get("best_checkpoint_id")),
            evaluation_metrics=_mapping(data.get("evaluation_metrics")),
            checkpoint_paths=_texts(data.get("checkpoint_paths")),
            resource_usage=_mapping(data.get("resource_usage")),
            estimate=_mapping(data.get("estimate")),
            loss_history=_mappings(data.get("loss_history"))[:MAX_LOSS_POINTS],
            error=_text(data.get("error")),
            random_seed=_whole(data.get("random_seed")) or 0,
            created_at=_text(data.get("created_at")) or now_iso(),
            updated_at=_text(data.get("updated_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version")) or RUN_SCHEMA_VERSION,
        )


# ── checkpoints ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One saved checkpoint: where it is, what it was worth, and whether it loads.

    ``status`` is written by the checkpoint manager after a real check: a file
    that is missing, empty or unreadable is ``incomplete``/``corrupt`` and is
    never offered for resume.
    """

    checkpoint_id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str = ""
    path: str = ""
    kind: str = "periodic"
    epoch: int = 0
    step: int = 0
    metrics: Mapping[str, Any] = field(default_factory=dict)
    size_bytes: int = 0
    status: str = "complete"
    reason: str = ""
    created_at: str = field(default_factory=now_iso)
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    @property
    def loadable(self) -> bool:
        return self.status == "complete" and bool(self.path)

    @property
    def is_best(self) -> bool:
        return self.kind == "best"

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "path": self.path,
            "kind": self.kind,
            "epoch": self.epoch,
            "step": self.step,
            "metrics": dict(self.metrics),
            "size_bytes": self.size_bytes,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckpointRecord:
        return cls(
            checkpoint_id=_text(data.get("checkpoint_id")) or uuid4().hex,
            run_id=_text(data.get("run_id")),
            path=_text(data.get("path")),
            kind=_text(data.get("kind"), "periodic"),
            epoch=max(0, _whole(data.get("epoch")) or 0),
            step=max(0, _whole(data.get("step")) or 0),
            metrics=_mapping(data.get("metrics")),
            size_bytes=max(0, _whole(data.get("size_bytes")) or 0),
            status=_text(data.get("status"), "complete"),
            reason=_text(data.get("reason")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=_whole(data.get("schema_version")) or CHECKPOINT_SCHEMA_VERSION,
        )


# ── the model registry ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TrainingModelRecord:
    """A trained model as the registry knows it.

    The base model and the adapter are separate fields because they are separate
    files: a LoRA adapter is small, references its base, and must never be
    confused with a full model copy.
    """

    model_id: str = field(default_factory=lambda: uuid4().hex)
    name: str = ""
    base_model: str = ""
    training_method: str = TrainingMethod.LORA.value
    #: Which objective adapted the model: ``sft``, ``dpo`` or ``orpo``. Separate
    #: from ``training_method`` (how the weights were adapted) because a LoRA
    #: adapter trained by DPO and one trained by SFT are the same KIND of artefact
    #: and very different models.
    algorithm: str = "sft"
    dataset_version: str = ""
    training_run_id: str = ""
    adapter: Mapping[str, Any] = field(default_factory=dict)
    evaluation: Mapping[str, Any] = field(default_factory=dict)
    resource_requirements: Mapping[str, Any] = field(default_factory=dict)
    status: str = ModelStatus.EXPERIMENTAL.value
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    approved_by: str = ""
    approved_at: str = ""
    promotion: Mapping[str, Any] = field(default_factory=dict)
    rollback: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""
    history: tuple[Mapping[str, Any], ...] = ()
    schema_version: int = TRAINING_MODEL_SCHEMA_VERSION

    @property
    def production(self) -> bool:
        return self.status == ModelStatus.PRODUCTION.value

    @property
    def adapter_path(self) -> str:
        return _text(self.adapter.get("path"))

    def with_status(self, status: ModelStatus | str, **updates: Any) -> TrainingModelRecord:
        value = status.value if isinstance(status, ModelStatus) else str(status)
        return replace(self, status=value, updated_at=now_iso(), **updates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "base_model": self.base_model,
            "training_method": self.training_method,
            "algorithm": self.algorithm,
            "dataset_version": self.dataset_version,
            "training_run_id": self.training_run_id,
            "adapter": dict(self.adapter),
            "evaluation": dict(self.evaluation),
            "resource_requirements": dict(self.resource_requirements),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "history": [dict(entry) for entry in self.history],
            "promotion": dict(self.promotion),
            "rollback": dict(self.rollback),
            "notes": self.notes,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrainingModelRecord:
        return cls(
            model_id=_text(data.get("model_id")) or uuid4().hex,
            name=_text(data.get("name")),
            base_model=_text(data.get("base_model")),
            training_method=_text(data.get("training_method"), TrainingMethod.LORA.value),
            algorithm=_text(data.get("algorithm"), "sft"),
            dataset_version=_text(data.get("dataset_version")),
            training_run_id=_text(data.get("training_run_id")),
            adapter=_mapping(data.get("adapter")),
            evaluation=_mapping(data.get("evaluation")),
            resource_requirements=_mapping(data.get("resource_requirements")),
            status=_text(data.get("status"), ModelStatus.EXPERIMENTAL.value),
            created_at=_text(data.get("created_at")) or now_iso(),
            updated_at=_text(data.get("updated_at")) or now_iso(),
            approved_by=_text(data.get("approved_by")),
            approved_at=_text(data.get("approved_at")),
            promotion=_mapping(data.get("promotion")),
            rollback=_mapping(data.get("rollback")),
            notes=_text(data.get("notes")),
            history=_mappings(data.get("history")),
            schema_version=_whole(data.get("schema_version")) or TRAINING_MODEL_SCHEMA_VERSION,
        )


# ── post-training evaluation ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TrainingEvaluation:
    """Base model versus candidate, on one dataset's test split.

    The verdict is a comparison, not a loss reading: a candidate improves when
    no tracked metric regressed by more than the tolerance, and the run is
    ``inconclusive`` — never ``pass`` — when no metric could be compared, or
    when neither side produced a single usable prediction (two models that both
    fail on every example have not been shown to be equal). ``reason`` says why
    an inconclusive verdict is inconclusive. Approval requires a ``pass`` — a
    falling training loss is not evidence by itself.
    """

    evaluation_id: str = ""
    run_id: str = ""
    model_id: str = ""
    dataset_version: str = ""
    split: str = "test"
    base: Mapping[str, float] = field(default_factory=dict)
    candidate: Mapping[str, float] = field(default_factory=dict)
    deltas: Mapping[str, float] = field(default_factory=dict)
    regressions: tuple[str, ...] = ()
    improvements: tuple[str, ...] = ()
    verdict: str = "inconclusive"
    reason: str = ""
    tolerance: float = 0.02
    examples_evaluated: int = 0
    base_predictor: str = ""
    candidate_predictor: str = ""
    #: Anything the objective-specific evaluation needs to keep: the regression
    #: report, the per-model readings, the algorithm, whether a loss figure was
    #: consulted. Free-form on purpose — the comparison above is the contract, and
    #: a later phase must be able to record its own evidence without a schema bump.
    details: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    schema_version: int = TRAINING_EVALUATION_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "dataset_version": self.dataset_version,
            "split": self.split,
            "base": dict(self.base),
            "candidate": dict(self.candidate),
            "deltas": dict(self.deltas),
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "verdict": self.verdict,
            "reason": self.reason,
            "tolerance": self.tolerance,
            "examples_evaluated": self.examples_evaluated,
            "base_predictor": self.base_predictor,
            "candidate_predictor": self.candidate_predictor,
            "details": dict(self.details),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrainingEvaluation:
        def flat(key: str) -> dict[str, float]:
            value = data.get(key)
            if not isinstance(value, Mapping):
                return {}
            result: dict[str, float] = {}
            for name, item in value.items():
                parsed = _number(item)
                if parsed is not None:
                    result[str(name)] = parsed
            return result

        return cls(
            evaluation_id=_text(data.get("evaluation_id")),
            run_id=_text(data.get("run_id")),
            model_id=_text(data.get("model_id")),
            dataset_version=_text(data.get("dataset_version")),
            split=_text(data.get("split"), "test"),
            base=flat("base"),
            candidate=flat("candidate"),
            deltas=flat("deltas"),
            regressions=_texts(data.get("regressions")),
            improvements=_texts(data.get("improvements")),
            verdict=_text(data.get("verdict"), "inconclusive"),
            reason=_text(data.get("reason")),
            tolerance=_number(data.get("tolerance")) or 0.02,
            examples_evaluated=max(0, _whole(data.get("examples_evaluated")) or 0),
            base_predictor=_text(data.get("base_predictor")),
            candidate_predictor=_text(data.get("candidate_predictor")),
            details=_mapping(data.get("details")),
            created_at=_text(data.get("created_at")) or now_iso(),
            schema_version=(
                _whole(data.get("schema_version")) or TRAINING_EVALUATION_SCHEMA_VERSION
            ),
        )


# ── module-level helpers ────────────────────────────────────────────────────

#: How many status changes a model record keeps. A bounded trail is enough to
#: answer "who moved this, when, and why" without letting a record grow forever:
#: the newest decisions are the ones a reviewer asks about.
MAX_MODEL_HISTORY = 20


#: The metric names the post-training comparison tracks. One vocabulary, so a
#: regression test and an operator's report agree on what "intent accuracy" is.
TRACKED_METRICS: tuple[str, ...] = (
    "intent_accuracy",
    "decision_accuracy",
    "tool_selection_accuracy",
    "argument_correctness",
    "planning_quality",
    "recovery_quality",
    "structured_output_validity",
    "task_success",
    "verification_success",
    "safety",
    "average_latency_ms",
    "average_memory_bytes",
)


def difficulty_of(trajectory: Any) -> str:
    """How hard one trajectory's work was, read from its shape.

    Named at module level because two phases need the same answer: a supervised
    example and a preference pair taken from the same run must agree on how hard
    it was, and two copies of this rule would drift. A recorded reading wins; the
    fallback counts steps, tool calls and retries, which is what "simple" means
    here (one step, one tool, no retries).
    """
    metadata = trajectory.metadata if isinstance(trajectory.metadata, Mapping) else {}
    named = str(metadata.get("difficulty") or "").strip()
    if named in {member.value for member in Difficulty}:
        return named
    steps = len(trajectory.execution_steps)
    tools = len(trajectory.tool_calls)
    retries = trajectory.retries
    if steps <= 1 and tools <= 1 and retries == 0:
        return Difficulty.SIMPLE.value
    if steps <= 4 and tools <= 4 and retries <= 2:
        return Difficulty.MODERATE.value
    return Difficulty.COMPLEX.value


def dataset_version_id(name: str, version: str) -> str:
    """The ``name@version`` string a run stores and a reader resolves."""
    return f"{str(name).strip()}@{str(version).strip()}"


def source_data_version(
    *,
    trajectories: int,
    evaluations: int,
    rewards: int,
    extra: str = "",
) -> str:
    """A readable stamp of the Phase 15 rows a dataset was built from.

    Not a hash of every row — a count plus the schema generation, enough to say
    "built from these stores at this size, under this vocabulary".
    """
    stamp = (
        f"trajectory-schema={TRAJECTORY_SCHEMA_VERSION};"
        f"trajectories={max(0, int(trajectories))};"
        f"evaluations={max(0, int(evaluations))};"
        f"rewards={max(0, int(rewards))}"
    )
    return f"{stamp};{extra}" if extra else stamp


def dataset_by_latest(versions: Sequence[SFTDatasetVersion]) -> SFTDatasetVersion | None:
    """The newest version among stored rows for one name (or None)."""
    if not versions:
        return None
    return max(versions, key=lambda dataset: dataset.created_at)


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "CHECKPOINT_SCHEMA_VERSION",
    "DATASET_SCHEMA_VERSION",
    "FORBIDDEN_REASONING_KEYS",
    "MAX_LOSS_POINTS",
    "PREPROCESSING_VERSION",
    "RUN_SCHEMA_VERSION",
    "SPLIT_NAMES",
    "TRAINING_EVALUATION_SCHEMA_VERSION",
    "TRAINING_MODEL_SCHEMA_VERSION",
    "TRAINING_SCHEMA_VERSION",
    "TRACKED_METRICS",
    "CheckpointRecord",
    "DatasetStatistics",
    "DatasetType",
    "Difficulty",
    "HardwarePolicy",
    "ModelStatus",
    "ResourceEstimate",
    "ResourceVerdict",
    "SFTDatasetVersion",
    "SFTTrainingExample",
    "SelectionRules",
    "SplitConfig",
    "TrainingEvaluation",
    "TrainingMethod",
    "TrainingModelRecord",
    "TrainingRun",
    "TrainingRunStatus",
    "dataset_by_latest",
    "dataset_version_id",
    "reasoning_violations",
    "source_data_version",
]
