"""The SFT dataset builder: accepted Phase 15 work becomes training examples.

One trajectory contains several kinds of teaching signal — what the request
MEANT, what was decided, which tool ran, what the recovery did — and each is a
different supervised task. The builder therefore produces SEVEN dataset types
(:class:`~novacontrol.training.models.DatasetType`) from the same rows, one
shape of input and target per type, rather than forcing every task into one
format.

The rules that keep a dataset worth training on:

  * **quality first.** A row enters only when its Phase 15 verdict says it is
    accepted (a filter can be supplied to assess rows that never went through
    one), its execution succeeded, its recorded verifications passed, and the
    structured data the target needs is actually present. A successful
    trajectory is NOT automatically a good example, and every row that was
    skipped is counted by reason in the dataset's own statistics.
  * **no secrets, no reasoning.** Every composed example runs through the audit
    trail's ``Redactor``; residual sensitive text drops the example. A row that
    carries a hidden-reasoning key is dropped too — this phase does not train on
    chain-of-thought, and it does not silently strip one either.
  * **deterministic, leak-free splits.** Whole groups (one trajectory's
    examples, or a named session) go to exactly one split, ordered by a seed, so
    the same input always produces the same split and a task cannot be on both
    sides of the train/test line.
  * **immutable versions.** ``name@version`` is the identity; a version is
    never edited after it exists, and a rebuild that differs is a new version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.quality import DataQualityFilter
from novacontrol.training.models import (
    PREPROCESSING_VERSION,
    SPLIT_NAMES,
    DatasetStatistics,
    DatasetType,
    SelectionRules,
    SFTDatasetVersion,
    SFTTrainingExample,
    SplitConfig,
    dataset_version_id,
    difficulty_of,
    reasoning_violations,
    source_data_version,
)

# ── skip reasons (one vocabulary, counted in the statistics) ────────────────

REASON_QUALITY = "quality_not_accepted"
REASON_NOT_SUCCESSFUL = "not_successful"
REASON_VERIFICATION_FAILED = "verification_failed"
REASON_VERIFICATION_MISSING = "verification_missing"
REASON_BELOW_REWARD = "below_min_reward"
REASON_BELOW_SCORE = "below_min_score"
REASON_MODEL = "model_filter"
REASON_CATEGORY = "task_category_filter"
REASON_SOURCE = "source_filter"
REASON_TAGS = "tag_filter"
REASON_DATE = "date_filter"
REASON_DEVELOPER_EXCLUDED = "developer_excluded"
REASON_RESEARCH_EXCLUDED = "research_excluded"
REASON_MISSING_DATA = "missing_structured_data"
REASON_DUPLICATE = "duplicate"
REASON_SENSITIVE = "residual_sensitive_data"
REASON_REASONING = "hidden_reasoning"
REASON_MALFORMED = "malformed_example"
REASON_NO_EXAMPLES = "no_examples_for_type"

_DEVELOPER_HINTS = {"developer", "code", "coding", "engineering", "build"}
_RESEARCH_HINTS = {"research", "explore", "study", "investigate"}
_CODE_INTENTS = {"code", "write_code", "develop", "refactor", "debug", "build_code"}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _rows(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _bump(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return "1.0.0"
    major, minor, patch = (int(part) for part in parts)
    return f"{major}.{minor}.{patch + 1}"


def next_version(existing: Sequence[str], requested: str = "") -> str:
    """The version a new dataset should carry.

    An explicit ``requested`` version wins (the operator said so). Otherwise the
    smallest unused patch above the highest stored version is chosen, so
    versions accumulate without anyone tracking numbers by hand.
    """
    wanted = str(requested or "").strip()
    if wanted:
        return wanted
    clean = [str(item).strip() for item in existing if str(item).strip()]
    numeric = [item for item in clean if len(item.split(".")) == 3 and all(
        part.isdigit() for part in item.split(".")
    )]
    if not numeric:
        return "1.0.0"
    latest = max(numeric, key=lambda item: tuple(int(part) for part in item.split(".")))
    candidate = _bump(latest)
    while candidate in clean:
        candidate = _bump(candidate)
    return candidate


def _difficulty(trajectory: AgentTrajectory) -> str:
    """The shared reading (``models.difficulty_of``), so Phase 17 agrees with it."""
    return difficulty_of(trajectory)


def _category(trajectory: AgentTrajectory) -> str:
    for key in ("task_category", "category"):
        named = _text(trajectory.metadata.get(key))
        if named:
            return named
    return _text(trajectory.source)


def _trajectory_tags(trajectory: AgentTrajectory) -> set[str]:
    tags = {str(item).lower() for item in _rows(trajectory.metadata.get("tags"))}
    intent = _text(trajectory.structured_intent.get("intent")).lower()
    if intent:
        tags.add(intent)
    source = _text(trajectory.source).lower()
    if source:
        tags.add(source)
    for key in ("agent", "category", "task_category"):
        value = _text(trajectory.metadata.get(key)).lower()
        if value:
            tags.add(value)
    return tags


def _mentions_model(trajectory: AgentTrajectory, wanted: str) -> bool:
    needle = wanted.lower()
    for key, value in trajectory.model_information.items():
        if needle in str(value).lower() or needle in str(key).lower():
            return True
    return False


def _intent_name(trajectory: AgentTrajectory) -> str:
    return _text(trajectory.structured_intent.get("intent")).lower()


def _is_developer(trajectory: AgentTrajectory) -> bool:
    if _intent_name(trajectory) in _CODE_INTENTS:
        return True
    if _trajectory_tags(trajectory) & _DEVELOPER_HINTS:
        return True
    return any(
        hint in _text(call.tool).lower() or hint in _text(call.capability).lower()
        for call in trajectory.tool_calls
        for hint in ("code", "developer")
    )


def _is_research(trajectory: AgentTrajectory) -> bool:
    return bool(_trajectory_tags(trajectory) & _RESEARCH_HINTS)


def _overall_score(evaluation: EvaluationResult) -> float | None:
    scores = [score for score in evaluation.scores().values() if score is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)


class SFTDatasetBuilder:
    """Turns accepted Phase 15 rows into versioned supervised datasets."""

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        quality: DataQualityFilter | None = None,
    ) -> None:
        self._redactor = redactor if redactor is not None else Redactor()
        self._quality = quality if quality is not None else DataQualityFilter()

    # -- entry point -----------------------------------------------------------

    def build(
        self,
        *,
        name: str,
        dataset_type: DatasetType | str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Sequence[EvaluationResult] = (),
        rules: SelectionRules | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        existing_versions: Sequence[str] = (),
    ) -> SFTDatasetVersion:
        """Build one immutable dataset version from the rows given."""
        wanted = dataset_type.value if isinstance(dataset_type, DatasetType) else str(dataset_type)
        selection = rules if rules is not None else SelectionRules()
        split_config = split if split is not None else SplitConfig()
        issues = split_config.issues()
        if issues:
            raise ValueError("; ".join(issues))
        clean_name = str(name).strip()
        if not clean_name or "@" in clean_name:
            raise ValueError("a dataset name must be non-empty and must not contain '@'")
        by_trajectory = {result.trajectory_id: result for result in evaluations}
        examples, statistics = self.build_examples(
            dataset_type=wanted,
            trajectories=trajectories,
            evaluations=by_trajectory,
            rules=selection,
        )
        chosen = next_version(existing_versions, version)
        splits = self.split_examples(examples, split_config)
        statistics = self._with_split_statistics(statistics, examples, splits)
        identifier = dataset_version_id(clean_name, chosen)
        stamped = tuple(
            SFTTrainingExample.from_dict(
                {**example.to_dict(), "dataset_version": identifier}
            )
            if example.dataset_version != identifier
            else example
            for example in examples
        )
        return SFTDatasetVersion(
            dataset_version_id=identifier,
            name=clean_name,
            version=chosen,
            dataset_type=wanted,
            description=str(description or ""),
            examples=stamped,
            splits=splits,
            selection=selection.to_mapping(),
            split_config=split_config.to_mapping(),
            statistics=statistics,
            source_data_version=source_data_version(
                trajectories=len(trajectories),
                evaluations=len(evaluations),
                rewards=len(trajectories),
                extra=f"dataset_type={wanted}",
            ),
            preprocessing_version=PREPROCESSING_VERSION,
            tags=tuple(str(item) for item in tags),
        )

    # -- eligibility -----------------------------------------------------------

    def _verdict_of(
        self, trajectory: AgentTrajectory
    ) -> tuple[str, Mapping[str, Any] | None]:
        """The stored quality verdict, or one from the Phase 15 filter now."""
        if isinstance(trajectory.quality, Mapping):
            return _text(trajectory.quality.get("verdict")), trajectory.quality
        verdict = self._quality.assess(trajectory)
        return verdict.verdict, verdict.to_dict()

    def _skip_reason(
        self,
        trajectory: AgentTrajectory,
        evaluation: EvaluationResult | None,
        rules: SelectionRules,
    ) -> str:
        """Why this row may not train, or ``""`` when it may."""
        verdict, _ = self._verdict_of(trajectory)
        if verdict not in set(rules.quality):
            return REASON_QUALITY
        if rules.require_success and trajectory.success is not True:
            return REASON_NOT_SUCCESSFUL
        if trajectory.verification_results:
            if rules.verification_must_pass and trajectory.failed_verifications:
                return REASON_VERIFICATION_FAILED
            if rules.require_verification and not any(
                record.passed for record in trajectory.verification_results
            ):
                return REASON_VERIFICATION_MISSING
        elif rules.require_verification:
            return REASON_VERIFICATION_MISSING
        if rules.min_reward is not None:
            reward = trajectory.reward if isinstance(trajectory.reward, Mapping) else {}
            total = reward.get("total_reward")
            if not isinstance(total, (int, float)) or float(total) < rules.min_reward:
                return REASON_BELOW_REWARD
        if rules.min_evaluation_score is not None:
            if evaluation is None:
                return REASON_BELOW_SCORE
            measured = (
                evaluation.score(rules.evaluation_dimension)
                if rules.evaluation_dimension
                else _overall_score(evaluation)
            )
            if measured is None or measured < rules.min_evaluation_score:
                return REASON_BELOW_SCORE
        if rules.model and not _mentions_model(trajectory, rules.model):
            return REASON_MODEL
        if rules.task_category and rules.task_category.lower() not in _category(trajectory).lower():
            return REASON_CATEGORY
        if rules.source and rules.source.lower() not in _text(trajectory.source).lower():
            return REASON_SOURCE
        if rules.tags and not (set(rules.tags) & _trajectory_tags(trajectory)):
            return REASON_TAGS
        if rules.since and trajectory.timestamp < rules.since:
            return REASON_DATE
        if rules.until and trajectory.timestamp > rules.until:
            return REASON_DATE
        return ""

    # -- example construction --------------------------------------------------

    def build_examples(
        self,
        *,
        dataset_type: str,
        trajectories: Sequence[AgentTrajectory],
        evaluations: Mapping[str, EvaluationResult] | None = None,
        rules: SelectionRules | None = None,
    ) -> tuple[list[SFTTrainingExample], DatasetStatistics]:
        """Build (and sanitise) every example the rules allow, with statistics."""
        selection = rules if rules is not None else SelectionRules()
        by_trajectory = dict(evaluations or {})
        skipped: dict[str, int] = {}
        examples: list[SFTTrainingExample] = []
        fingerprints: set[str] = set()
        duplicates = 0
        sensitive = 0
        malformed = 0
        sources: set[str] = set()

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for trajectory in trajectories:
            # A source row that carries hidden reasoning is REFUSED, not quietly
            # trimmed. `_base_example` copies a fixed set of provenance fields,
            # so a `chain_of_thought` sitting in a trajectory's metadata would
            # never reach the example and the example-level guard would never
            # see it: the row would train with its reasoning silently removed,
            # which is the one outcome this guard exists to prevent.
            if reasoning_violations(trajectory.to_dict(), path="trajectory"):
                malformed += 1
                skip(REASON_REASONING)
                continue
            reason = self._skip_reason(
                trajectory, by_trajectory.get(trajectory.trajectory_id), selection
            )
            if reason:
                skip(reason)
                continue
            if (
                dataset_type == DatasetType.DEVELOPER.value
                and not selection.include_developer
                and _is_developer(trajectory)
            ):
                skip(REASON_DEVELOPER_EXCLUDED)
                continue
            if (
                dataset_type == DatasetType.RESEARCH.value
                and not selection.include_research
                and _is_research(trajectory)
            ):
                skip(REASON_RESEARCH_EXCLUDED)
                continue
            built = self._examples_for(dataset_type, trajectory)
            if not built:
                skip(REASON_MISSING_DATA)
                continue
            for example in built:
                if selection.max_examples and len(examples) >= selection.max_examples:
                    break
                if example.reasoning_violations():
                    malformed += 1
                    skip(REASON_REASONING)
                    continue
                cleaned, report = self._sanitise(example)
                if report.count:
                    sensitive += 1
                    skip(REASON_SENSITIVE)
                    continue
                try:
                    json.dumps(cleaned.to_dict(), ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError):
                    malformed += 1
                    skip(REASON_MALFORMED)
                    continue
                fingerprint = cleaned.fingerprint()
                if fingerprint in fingerprints:
                    duplicates += 1
                    skip(REASON_DUPLICATE)
                    continue
                fingerprints.add(fingerprint)
                examples.append(cleaned)
                sources.add(trajectory.trajectory_id)

        statistics = DatasetStatistics(
            total=len(examples),
            by_difficulty=self._count(examples, lambda item: item.difficulty),
            by_quality=self._count(examples, lambda item: item.quality_status or "unknown"),
            source_trajectories=len(sources),
            duplicates_removed=duplicates,
            sensitive_removed=sensitive,
            malformed_removed=malformed,
            skipped=skipped,
            estimated_tokens=sum(example.estimated_tokens for example in examples),
        )
        if not examples and not skipped:
            skipped[REASON_NO_EXAMPLES] = 1
            statistics = replace(statistics, skipped=dict(skipped))
        return (examples, statistics)

    @staticmethod
    def _count(
        examples: Sequence[SFTTrainingExample], reader: Any
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for example in examples:
            name = str(reader(example))
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _base_example(
        self,
        trajectory: AgentTrajectory,
        *,
        dataset_type: str,
        payload: Mapping[str, Any],
        target: Mapping[str, Any],
        tags: Sequence[str] = (),
    ) -> SFTTrainingExample:
        evaluation_id = _text(trajectory.evaluation_id)
        verdict, _ = self._verdict_of(trajectory)
        metadata = {
            "group_key": _text(trajectory.task_id) or _text(trajectory.parent_task_id)
            or trajectory.trajectory_id,
            "task_id": trajectory.task_id,
            "source": trajectory.source,
            "intent": _text(trajectory.structured_intent.get("intent")),
            "category": _category(trajectory),
            "route": _text(trajectory.decision.get("route")),
            "status": trajectory.status,
            "source_timestamp": trajectory.timestamp,
        }
        return SFTTrainingExample(
            dataset_type=dataset_type,
            input=_mapping(payload.get("input")),
            context=_mapping(payload.get("context")),
            target=dict(target),
            metadata=metadata,
            source_trajectory_id=trajectory.trajectory_id,
            source_evaluation_id=evaluation_id,
            quality_status=verdict,
            difficulty=_difficulty(trajectory),
            tags=tuple(str(item) for item in tags),
        )

    def _sanitise(
        self, example: SFTTrainingExample
    ) -> tuple[SFTTrainingExample, Any]:
        """Redact an example on the way in; report what the redactor found."""
        payload = example.to_dict()
        cleaned, report = self._redactor.redact_value(payload)
        if not isinstance(cleaned, Mapping):
            return (example, report)
        return (SFTTrainingExample.from_dict(cleaned), report)

    def _examples_for(
        self, dataset_type: str, trajectory: AgentTrajectory
    ) -> list[SFTTrainingExample]:
        if dataset_type == DatasetType.NLU.value:
            return self._nlu_examples(trajectory)
        if dataset_type == DatasetType.DECISION.value:
            return self._decision_examples(trajectory)
        if dataset_type == DatasetType.TOOL_SELECTION.value:
            return self._tool_examples(trajectory)
        if dataset_type == DatasetType.PLANNING.value:
            return self._planning_examples(trajectory)
        if dataset_type == DatasetType.RECOVERY.value:
            return self._recovery_examples(trajectory)
        if dataset_type == DatasetType.DEVELOPER.value:
            return self._developer_examples(trajectory)
        if dataset_type == DatasetType.RESEARCH.value:
            return self._research_examples(trajectory)
        return []

    # -- one builder per dataset type ------------------------------------------

    def _nlu_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        request = _text(trajectory.user_request)
        intent = _mapping(trajectory.structured_intent)
        if not request or not _text(intent.get("intent")):
            return []
        target: dict[str, Any] = {
            key: intent[key]
            for key in ("intent", "confidence", "entities", "requires_vision", "strategy")
            if key in intent
        }
        return [
            self._base_example(
                trajectory,
                dataset_type=DatasetType.NLU.value,
                payload={
                    "input": {"request": request},
                    "context": {"context_summary": _mapping(trajectory.context_summary)},
                },
                target=target,
                tags=("nlu",),
            )
        ]

    def _decision_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        intent = _mapping(trajectory.structured_intent)
        decision = _mapping(trajectory.decision)
        if not _text(intent.get("intent")) or not _text(decision.get("route")):
            return []
        target: dict[str, Any] = {
            key: decision[key]
            for key in ("route", "decision_type", "capability", "model", "requires_confirmation")
            if key in decision
        }
        return [
            self._base_example(
                trajectory,
                dataset_type=DatasetType.DECISION.value,
                payload={
                    "input": {"intent": intent},
                    "context": {
                        "context_summary": _mapping(trajectory.context_summary),
                        "capabilities": sorted(
                            {
                                _text(call.capability)
                                for call in trajectory.tool_calls
                                if _text(call.capability)
                            }
                        ),
                    },
                },
                target=target,
                tags=("decision",),
            )
        ]

    def _tool_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        examples: list[SFTTrainingExample] = []
        available = sorted({_text(call.tool) for call in trajectory.tool_calls if _text(call.tool)})
        for call in trajectory.tool_calls:
            if not call.succeeded or not _text(call.tool):
                continue
            if not isinstance(call.arguments, Mapping):
                continue
            examples.append(
                self._base_example(
                    trajectory,
                    dataset_type=DatasetType.TOOL_SELECTION.value,
                    payload={
                        "input": {
                            "task": _text(trajectory.user_request),
                            "intent": _text(trajectory.structured_intent.get("intent")),
                            "available_tools": available,
                        },
                        "context": {"capability": _text(call.capability)},
                    },
                    target={
                        "tool": call.tool,
                        "arguments": dict(call.arguments),
                        "capability": call.capability,
                        "status": call.status,
                    },
                    tags=("tool_selection",),
                )
            )
        return examples

    def _planning_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        plan = _mapping(trajectory.plan)
        steps = _rows(plan.get("steps")) or [
            step.to_dict() for step in trajectory.execution_steps
        ]
        cleaned_steps: list[dict[str, Any]] = []
        for step in steps:
            data = _mapping(step)
            action = _text(data.get("action")) or _text(data.get("description"))
            if not action and not _text(data.get("step_id")):
                continue
            cleaned_steps.append(
                {
                    key: data[key]
                    for key in ("step_id", "index", "description", "action", "tool", "depends_on")
                    if key in data
                }
            )
        goal = _text(plan.get("goal")) or _text(trajectory.user_request)
        if not goal or not cleaned_steps:
            return []
        return [
            self._base_example(
                trajectory,
                dataset_type=DatasetType.PLANNING.value,
                payload={
                    "input": {
                        "goal": goal,
                        "intent": _text(trajectory.structured_intent.get("intent")),
                    },
                    "context": {
                        "capabilities": sorted(
                            {
                                _text(call.capability) or _text(call.tool)
                                for call in trajectory.tool_calls
                                if _text(call.capability) or _text(call.tool)
                            }
                        )
                    },
                },
                target={"goal": goal, "steps": cleaned_steps},
                tags=("planning",),
            )
        ]

    def _recovery_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        if not trajectory.recovery_events:
            return []
        recovered = [record for record in trajectory.recovery_events if _text(record.outcome)]
        if not recovered:
            return []
        by_step = {record.step_id: record for record in recovered if record.step_id}
        examples: list[SFTTrainingExample] = []
        observations = [
            _text(item.summary) for item in trajectory.observations if _text(item.summary)
        ]
        for call in trajectory.failed_tools:
            record = by_step.get(call.step_id) or recovered[0]
            examples.append(
                self._base_example(
                    trajectory,
                    dataset_type=DatasetType.RECOVERY.value,
                    payload={
                        "input": {
                            "failed_step": _text(call.step_id) or _text(call.tool),
                            "error": _text(call.error),
                            "observations": observations[:10],
                        },
                        "context": {"available_tools": sorted(
                            {_text(item.tool) for item in trajectory.tool_calls if _text(item.tool)}
                        )},
                    },
                    target={
                        "strategy": record.strategy,
                        "outcome": record.outcome,
                        "attempts": record.attempts,
                    },
                    tags=("recovery",),
                )
            )
        return examples

    def _developer_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        if not _is_developer(trajectory):
            return []
        result = _mapping(trajectory.final_result)
        actions = [
            {
                "tool": call.tool,
                "status": call.status,
                "summary": _text(call.output_summary),
            }
            for call in trajectory.tool_calls
        ]
        if not result and not actions:
            return []
        return [
            self._base_example(
                trajectory,
                dataset_type=DatasetType.DEVELOPER.value,
                payload={
                    "input": {"task": _text(trajectory.user_request)},
                    "context": {
                        "repository_context": _mapping(trajectory.context_summary),
                        "intent": _text(trajectory.structured_intent.get("intent")),
                    },
                },
                target={"actions": actions, "result": result},
                tags=("developer",),
            )
        ]

    def _research_examples(self, trajectory: AgentTrajectory) -> list[SFTTrainingExample]:
        if not _is_research(trajectory):
            return []
        result = _mapping(trajectory.final_result)
        evidence = [
            {"kind": item.kind, "summary": _text(item.summary), "source": item.source}
            for item in trajectory.observations
            if _text(item.summary)
        ]
        if not result and not evidence:
            return []
        return [
            self._base_example(
                trajectory,
                dataset_type=DatasetType.RESEARCH.value,
                payload={
                    "input": {"request": _text(trajectory.user_request)},
                    "context": {"available_evidence": evidence[:20]},
                },
                target={"result": result, "evidence": evidence[:20]},
                tags=("research",),
            )
        ]

    # -- splitting -------------------------------------------------------------

    def split_examples(
        self, examples: Sequence[SFTTrainingExample], config: SplitConfig
    ) -> dict[str, tuple[str, ...]]:
        """Assign whole groups to splits, deterministically and without leakage.

        Groups are ordered by a hash of (seed, group key), so the same inputs
        always produce the same split. Each group then goes to the split whose
        current share is furthest below its target — a deterministic walk that
        keeps the ratios close without ever splitting a group.
        """
        ratios = config.ratios()
        groups: dict[str, list[str]] = {}
        for example in sorted(examples, key=lambda item: item.example_id):
            key = (
                example.group_key
                if config.group_by == "task"
                else example.example_id
            )
            groups.setdefault(key, []).append(example.example_id)
        ordered = sorted(
            groups.items(),
            key=lambda item: hashlib.sha256(
                f"{config.seed}:{item[0]}".encode()
            ).hexdigest(),
        )
        total = sum(len(ids) for _, ids in ordered)
        targets = {name: total * ratios.get(name, 0.0) for name in SPLIT_NAMES}
        assigned: dict[str, list[str]] = {name: [] for name in SPLIT_NAMES}
        # Small splits fill first on a tie, so a 80/10/10 split does not starve
        # validation and test when there are only a few groups.
        preference = {"validation": 0, "test": 1, "train": 2}

        def capacity(name: str) -> float:
            """How far below its share this split still is, RELATIVE to that share.

            The comparison has to be relative, not absolute. With absolute
            room, a 80/10/10 split hands the first groups to ``train`` because
            its target is simply the largest number — four groups of ten become
            30/10/0, and the test split the evaluator reads by default is empty.
            Measuring each split against its own target instead fills all three:
            the same four groups become 20/10/10.
            """
            target = targets[name]
            if ratios.get(name, 0.0) <= 0 or target <= 0:
                return float("-inf")
            return (target - len(assigned[name])) / target

        for _, ids in ordered:
            chosen = max(
                SPLIT_NAMES,
                key=lambda name: (
                    capacity(name),
                    -ratios.get(name, 0.0),
                    -preference.get(name, 9),
                ),
            )
            assigned[chosen].extend(ids)
        placed = {example_id for ids in assigned.values() for example_id in ids}
        missing = [
            example.example_id for example in examples if example.example_id not in placed
        ]
        if missing:
            assigned["train"].extend(missing)
        return {name: tuple(ids) for name, ids in assigned.items()}

    def _with_split_statistics(
        self,
        statistics: DatasetStatistics,
        examples: Sequence[SFTTrainingExample],
        splits: Mapping[str, tuple[str, ...]],
    ) -> DatasetStatistics:
        groups = {example.group_key for example in examples}
        return DatasetStatistics(
            total=statistics.total,
            by_difficulty=dict(statistics.by_difficulty),
            by_quality=dict(statistics.by_quality),
            by_split={name: len(ids) for name, ids in splits.items() if ids},
            groups=len(groups),
            source_trajectories=statistics.source_trajectories,
            duplicates_removed=statistics.duplicates_removed,
            sensitive_removed=statistics.sensitive_removed,
            malformed_removed=statistics.malformed_removed,
            skipped=dict(statistics.skipped),
            estimated_tokens=statistics.estimated_tokens,
        )

    # -- validation ------------------------------------------------------------

    def validate(self, dataset: SFTDatasetVersion) -> tuple[str, ...]:
        """Every reason this dataset should not be trained on (empty is good)."""
        issues: list[str] = []
        if not dataset.name or not dataset.version:
            issues.append("the dataset has no name or no version")
        if dataset.dataset_version_id != dataset_version_id(dataset.name, dataset.version):
            issues.append("dataset_version_id does not match name@version")
        if not dataset.examples:
            issues.append("the dataset has no examples")
        if dataset.dataset_type not in {member.value for member in DatasetType}:
            issues.append(f"unknown dataset_type {dataset.dataset_type!r}")

        seen: set[str] = set()
        split_of: dict[str, str] = {}
        for name, ids in dataset.splits.items():
            if name not in SPLIT_NAMES:
                issues.append(f"unknown split {name!r}")
            for example_id in ids:
                if example_id in split_of:
                    issues.append(
                        f"example {example_id} appears in both {split_of[example_id]} and {name}"
                    )
                split_of[example_id] = name

        for example in dataset.examples:
            if example.example_id in seen:
                issues.append(f"duplicate example id {example.example_id}")
            seen.add(example.example_id)
            if example.dataset_type != dataset.dataset_type:
                issues.append(
                    f"example {example.example_id} has type {example.dataset_type}, "
                    f"the dataset has {dataset.dataset_type}"
                )
            violations = example.reasoning_violations()
            if violations:
                issues.append(
                    f"example {example.example_id} carries hidden reasoning at "
                    + ", ".join(violations)
                )
            if not example.target:
                issues.append(f"example {example.example_id} has no structured target")
            if example.example_id not in split_of:
                issues.append(f"example {example.example_id} is not in any split")

        # Leakage: every example from one group must be in one split.
        group_splits: dict[str, set[str]] = {}
        for example in dataset.examples:
            name = split_of.get(example.example_id, "")
            if name:
                group_splits.setdefault(example.group_key, set()).add(name)
        for key, names in group_splits.items():
            if len(names) > 1:
                issues.append(
                    f"group {key!r} leaks across splits: {', '.join(sorted(names))}"
                )

        leaked_ids = split_of.keys() - seen
        if leaked_ids:
            issues.append(
                "the split references examples that are not stored: "
                + ", ".join(sorted(leaked_ids)[:5])
            )
        if dataset.statistics.total != len(dataset.examples):
            issues.append(
                f"statistics say {dataset.statistics.total} examples, the dataset has "
                f"{len(dataset.examples)}"
            )
        return tuple(issues)


__all__ = [
    "REASON_BELOW_REWARD",
    "REASON_BELOW_SCORE",
    "REASON_CATEGORY",
    "REASON_DATE",
    "REASON_DEVELOPER_EXCLUDED",
    "REASON_DUPLICATE",
    "REASON_MALFORMED",
    "REASON_MISSING_DATA",
    "REASON_MODEL",
    "REASON_NO_EXAMPLES",
    "REASON_NOT_SUCCESSFUL",
    "REASON_QUALITY",
    "REASON_REASONING",
    "REASON_RESEARCH_EXCLUDED",
    "REASON_SENSITIVE",
    "REASON_SOURCE",
    "REASON_TAGS",
    "REASON_VERIFICATION_FAILED",
    "REASON_VERIFICATION_MISSING",
    "SFTDatasetBuilder",
    "next_version",
]
