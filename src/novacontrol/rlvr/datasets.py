"""Corrections and critique datasets: failed behaviour becomes learnable data.

The builder turns what the verifiers and the critique engine recorded into the
three shapes the phase promises:

    bad action → critique → correct action
    bad plan   → critique → corrected plan
    bad tool   → critique → correct tool

and a fourth for output-format failures (a response row). Every row keeps the
critique that produced it, the verification that settled it (when one exists)
and the reasons it was accepted, held or rejected. A row without a verified
correction is kept and HELD rather than dropped, because "what to do about this
failure is not yet known" is itself information a reviewer can act on.

The dataset part reuses Phase 16's machinery on purpose: immutable
``name@version`` identities, the same deterministic group-safe splitter, the
same statistics shape. Corrections can also be projected into Phase 17
preference pairs — chosen = the verified correction, rejected = the failed
behaviour — but only when the correction carries a PASS verification, because a
preference pair is training data and an unverified suggestion is not evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.preference.models import (
    EVIDENCE_VERIFIED_SUCCESS,
    PreferenceEvidence,
    PreferenceExample,
    PreferenceSource,
    PreferenceStrength,
)
from novacontrol.rlvr.config import CritiqueConfig
from novacontrol.rlvr.models import (
    CRITIQUE_EXAMPLE_KINDS,
    SEVERITY_RANK,
    CorrectedExample,
    CorrectionStatus,
    CritiqueCategory,
    CritiqueDatasetStatistics,
    CritiqueDatasetVersion,
    CritiqueExample,
    CritiqueExampleKind,
    CritiqueResult,
    VerificationResult,
    _text,
    critique_dataset_version_id,
)
from novacontrol.training.datasets import SFTDatasetBuilder, next_version
from novacontrol.training.models import (
    SFTTrainingExample,
    SplitConfig,
    reasoning_violations,
)

#: Why a critique row was skipped, accepted or held. Named, never a shrug.
REASON_NO_CRITIQUE = "no_critique"
REASON_NO_CORRECTION = "no_corrected_output"
REASON_CORRECTION_UNVERIFIED = "correction_not_verified"
REASON_CORRECTION_FAILED = "correction_verification_failed"
REASON_REASONING = "hidden_reasoning_present"
REASON_DUPLICATE = "duplicate_critique_example"
REASON_MALFORMED = "malformed_critique_example"
REASON_SOURCE_NOT_ALLOWED = "critique_source_not_allowed"
REASON_KIND_NOT_ALLOWED = "critique_kind_not_allowed"
REASON_SEVERITY_TOO_LOW = "critique_severity_below_floor"
REASON_REJECTED = "critique_rejected"

#: Which kind a correction of each category produces.
KIND_BY_CATEGORY: Mapping[str, str] = {
    CritiqueCategory.PLANNING_ERROR.value: CritiqueExampleKind.PLAN.value,
    CritiqueCategory.TOOL_SELECTION_ERROR.value: CritiqueExampleKind.TOOL.value,
    CritiqueCategory.ARGUMENT_ERROR.value: CritiqueExampleKind.TOOL.value,
    CritiqueCategory.EXECUTION_ERROR.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.VERIFICATION_ERROR.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.RECOVERY_ERROR.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.SAFETY_ERROR.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.EFFICIENCY_ISSUE.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.OUTPUT_FORMAT_ERROR.value: CritiqueExampleKind.RESPONSE.value,
    CritiqueCategory.CONTEXT_ERROR.value: CritiqueExampleKind.ACTION.value,
    CritiqueCategory.OTHER.value: CritiqueExampleKind.ACTION.value,
}

#: The severity floor a dataset may be built with when its rules name none.
DEFAULT_MIN_SEVERITY = "low"


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


@dataclass(frozen=True, slots=True)
class CritiqueDatasetRules:
    """Which critiques may become rows, and on what terms."""

    min_severity: str = DEFAULT_MIN_SEVERITY
    kinds: tuple[str, ...] = CRITIQUE_EXAMPLE_KINDS
    sources: tuple[str, ...] = ()
    require_verified_corrections: bool = True
    hold_unverified_corrections: bool = True
    include_rejected: bool = False
    max_examples: int = 0
    group_by: str = "task"
    note: str = ""

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.min_severity not in SEVERITY_RANK:
            problems.append(
                f"min_severity must be one of {', '.join(SEVERITY_RANK)} "
                f"(got {self.min_severity!r})"
            )
        unknown = [name for name in self.kinds if name not in CRITIQUE_EXAMPLE_KINDS]
        if unknown:
            problems.append(f"unknown example kind(s): {', '.join(sorted(unknown))}")
        if self.group_by not in {"task", "trajectory", "example"}:
            problems.append(
                f"group_by must be task, trajectory or example (got {self.group_by!r})"
            )
        return tuple(problems)

    def allows_kind(self, kind: str) -> bool:
        return not self.kinds or _text(kind) in set(self.kinds)

    def allows_source(self, source: str) -> bool:
        return not self.sources or _text(source) in set(self.sources)

    def meets_severity(self, severity: str) -> bool:
        return SEVERITY_RANK.get(_text(severity), 1) >= SEVERITY_RANK.get(
            self.min_severity, 0
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "min_severity": self.min_severity,
            "kinds": list(self.kinds),
            "sources": list(self.sources),
            "require_verified_corrections": self.require_verified_corrections,
            "hold_unverified_corrections": self.hold_unverified_corrections,
            "include_rejected": self.include_rejected,
            "max_examples": self.max_examples,
            "group_by": self.group_by,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> CritiqueDatasetRules:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        severity = _text(data.get("min_severity"), defaults.min_severity)
        if severity not in SEVERITY_RANK:
            severity = defaults.min_severity
        kinds_raw = data.get("kinds")
        kinds = (
            tuple(
                str(item).strip()
                for item in kinds_raw
                if str(item).strip() in CRITIQUE_EXAMPLE_KINDS
            )
            if isinstance(kinds_raw, (list, tuple))
            else ()
        )
        return cls(
            min_severity=severity,
            kinds=kinds or defaults.kinds,
            sources=tuple(
                str(item).strip()
                for item in data.get("sources", [])
                if str(item).strip()
            )
            if isinstance(data.get("sources"), (list, tuple))
            else (),
            require_verified_corrections=_flag(
                data.get("require_verified_corrections"),
                defaults.require_verified_corrections,
            ),
            hold_unverified_corrections=_flag(
                data.get("hold_unverified_corrections"),
                defaults.hold_unverified_corrections,
            ),
            include_rejected=_flag(data.get("include_rejected"), defaults.include_rejected),
            max_examples=max(0, int(data.get("max_examples", 0) or 0)),
            group_by=_text(data.get("group_by"), defaults.group_by),
            note=_text(data.get("note")),
        )


@dataclass(frozen=True, slots=True)
class CritiqueDatasetRequest:
    """One build request, so a caller states the name, kinds and rules once."""

    name: str = ""
    kinds: tuple[str, ...] = ()
    rules: CritiqueDatasetRules = field(default_factory=CritiqueDatasetRules)
    split: SplitConfig = field(default_factory=SplitConfig)
    version: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    source_datasets: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        name: str,
        *,
        kinds: Sequence[str] = (),
        rules: CritiqueDatasetRules | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
    ) -> CritiqueDatasetRequest:
        return cls(
            name=_text(name),
            kinds=tuple(str(item).strip() for item in kinds if str(item).strip()),
            rules=rules if rules is not None else CritiqueDatasetRules(),
            split=split if split is not None else SplitConfig(),
            version=_text(version),
            description=_text(description),
            tags=tuple(str(item).strip() for item in tags if str(item).strip()),
        )


class CorrectedExampleBuilder:
    """Turns a critique plus a proposed correction into an evidence-checked row.

    The quality ladder is explicit:

      * a correction with a PASS verification is ACCEPTED;
      * a correction whose verification FAILED is REJECTED — it was checked and
        did not work;
      * a correction nobody could verify is HELD (NEEDS_REVIEW) when the
        configuration says to hold, because "an AI suggested it" is not
        evidence;
      * a correction carrying hidden reasoning is REJECTED outright.
    """

    def __init__(self, config: CritiqueConfig | None = None) -> None:
        self.config = config if config is not None else CritiqueConfig()

    def build(
        self,
        *,
        original_input: Mapping[str, Any],
        original_output: Mapping[str, Any],
        critique: CritiqueResult,
        corrected_output: Mapping[str, Any],
        verification: VerificationResult | None = None,
        source_trajectory_id: str = "",
        correction_source: str = "",
    ) -> CorrectedExample:
        reasons: list[str] = []
        status = CorrectionStatus.NEEDS_REVIEW.value
        reasoning = reasoning_violations(
            {
                "input": dict(original_input),
                "original": dict(original_output),
                "corrected": dict(corrected_output),
                "critique": critique.to_dict(),
            }
        )
        if reasoning:
            status = CorrectionStatus.REJECTED.value
            reasons.append(REASON_REASONING)
        elif not corrected_output:
            status = CorrectionStatus.REJECTED.value
            reasons.append(REASON_NO_CORRECTION)
        elif verification is not None and verification.failed:
            status = CorrectionStatus.REJECTED.value
            reasons.append(REASON_CORRECTION_FAILED)
        elif verification is not None and verification.verified:
            status = CorrectionStatus.ACCEPTED.value
        elif self.config.require_verification_for_correction:
            status = (
                CorrectionStatus.NEEDS_REVIEW.value
                if self.config.hold_unverified_corrections
                else CorrectionStatus.ACCEPTED.value
            )
            reasons.append(REASON_CORRECTION_UNVERIFIED)
        else:
            status = CorrectionStatus.ACCEPTED.value
            reasons.append(REASON_CORRECTION_UNVERIFIED)
        evidence = tuple(
            dict.fromkeys(
                (
                    *critique.evidence,
                    *(verification.evidence if verification is not None else ()),
                )
            )
        )
        return CorrectedExample(
            original_input=dict(original_input),
            original_output=dict(original_output),
            critique=critique,
            corrected_output=dict(corrected_output),
            verification_result=verification,
            source_trajectory_id=source_trajectory_id or critique.trajectory_id,
            quality_status=status,
            correction_source=correction_source or critique.source,
            evidence=evidence,
            reasons=tuple(reasons),
            created_at=now_iso(),
        )


def _critique_reasoning(critique: CritiqueResult) -> tuple[str, ...]:
    return reasoning_violations({"critique": critique.to_dict()})


def _split_for(rules: CritiqueDatasetRules, config: SplitConfig | None) -> SplitConfig:
    if config is not None:
        return config
    return replace(SplitConfig(), group_by=rules.group_by)


class CritiqueDatasetBuilder:
    """Builds immutable critique datasets from critiques and corrections."""

    def __init__(
        self,
        *,
        config: CritiqueConfig | None = None,
        corrector: CorrectedExampleBuilder | None = None,
        splitter: SFTDatasetBuilder | None = None,
    ) -> None:
        self.config = config if config is not None else CritiqueConfig()
        self.corrector = (
            corrector if corrector is not None else CorrectedExampleBuilder(self.config)
        )
        self._splitter = splitter if splitter is not None else SFTDatasetBuilder()

    # -- building ------------------------------------------------------------------

    def build(
        self,
        request: CritiqueDatasetRequest | str,
        *,
        critiques: Sequence[CritiqueResult] = (),
        corrections: Sequence[Any] = (),
        verifications: Sequence[VerificationResult] = (),
        rules: CritiqueDatasetRules | None = None,
        split: SplitConfig | None = None,
        version: str = "",
        description: str = "",
        tags: Sequence[str] = (),
        existing_versions: Sequence[str] = (),
        source_datasets: Sequence[str] = (),
    ) -> CritiqueDatasetVersion:
        """Assemble, screen and split one critique dataset version."""
        spec = (
            request
            if isinstance(request, CritiqueDatasetRequest)
            else CritiqueDatasetRequest.of(str(request))
        )
        selection = rules if rules is not None else spec.rules
        problems = selection.validate()
        if problems:
            raise ValueError("; ".join(problems))
        split_config = _split_for(selection, split if split is not None else spec.split)
        split_problems = split_config.issues()
        if split_problems:
            raise ValueError("; ".join(split_problems))
        name = spec.name.strip()
        if not name or "@" in name:
            raise ValueError("a dataset name must be non-empty and must not contain '@'")
        candidates: list[CritiqueExample] = []
        skipped: dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        corrected_ids = {
            getattr(item, "critique", CritiqueResult()).critique_id: item
            for item in corrections
            if getattr(item, "critique", None) is not None
        }
        for critique in critiques:
            if not self.config.allows_category(critique.category):
                skip(REASON_SOURCE_NOT_ALLOWED)
                continue
            if not selection.allows_source(critique.source):
                skip(REASON_SOURCE_NOT_ALLOWED)
                continue
            if not selection.meets_severity(critique.severity):
                skip(REASON_SEVERITY_TOO_LOW)
                continue
            if _critique_reasoning(critique):
                skip(REASON_REASONING)
                continue
            corrected = corrected_ids.get(critique.critique_id)
            candidates.append(self._row_for(critique, corrected, selection))
        seen_corrections = {item.critique_id for item in critiques}
        for correction in corrections:
            attached = getattr(correction, "critique", None)
            if not isinstance(attached, CritiqueResult):
                skip(REASON_NO_CRITIQUE)
                continue
            if attached.critique_id in seen_corrections:
                continue
            if not self.config.allows_category(attached.category):
                skip(REASON_SOURCE_NOT_ALLOWED)
                continue
            if not selection.meets_severity(attached.severity):
                skip(REASON_SEVERITY_TOO_LOW)
                continue
            if _critique_reasoning(attached):
                skip(REASON_REASONING)
                continue
            candidates.append(self._row_for(attached, correction, selection))
        screened: list[CritiqueExample] = []
        fingerprints: set[str] = set()
        for row in candidates:
            if not selection.allows_kind(row.kind):
                skip(REASON_KIND_NOT_ALLOWED)
                continue
            if not self._row_reasoning_free(row):
                skip(REASON_REASONING)
                continue
            fingerprint = row.fingerprint()
            if fingerprint in fingerprints:
                skip(REASON_DUPLICATE)
                continue
            fingerprints.add(fingerprint)
            if row.status == CorrectionStatus.REJECTED.value and not selection.include_rejected:
                skip(REASON_REJECTED)
                continue
            screened.append(row)
        if selection.max_examples and len(screened) > selection.max_examples:
            screened = screened[: selection.max_examples]
        resolved = _text(version) or next_version(existing_versions, spec.version)
        identifier = critique_dataset_version_id(name, resolved)
        projections: list[SFTTrainingExample] = [
            row.to_sft_example() for row in screened
        ]
        splits = self._splitter.split_examples(projections, split_config)
        stamped = tuple(
            row if row.dataset_version == identifier else replace(row, dataset_version=identifier)
            for row in screened
        )
        statistics = self._statistics(
            stamped,
            splits=splits,
            skipped=skipped,
            source_trajectories=len(
                {row.source_trajectory_id for row in stamped if row.source_trajectory_id}
            ),
        )
        rules_mapping = dict(selection.to_mapping())
        rules_mapping["critique"] = self.config.to_mapping()
        return CritiqueDatasetVersion(
            dataset_version_id=identifier,
            name=name,
            version=resolved,
            description=_text(description) or spec.description,
            examples=stamped,
            splits=splits,
            statistics=statistics,
            rules=rules_mapping,
            split_config=split_config.to_mapping(),
            source_datasets=tuple(
                str(item) for item in (source_datasets or spec.source_datasets)
            ),
            preprocessing_version=self.config.version,
            source_data_version=(
                f"critiques={len(critiques)};corrections={len(corrections)};"
                f"verifications={len(verifications)}"
            ),
            tags=tuple(str(item) for item in (tags or spec.tags)),
        )

    def _row_for(
        self,
        critique: CritiqueResult,
        correction: Any,
        rules: CritiqueDatasetRules,
    ) -> CritiqueExample:
        kind = KIND_BY_CATEGORY.get(critique.category, CritiqueExampleKind.ACTION.value)
        if correction is None:
            status = (
                CorrectionStatus.NEEDS_REVIEW.value
                if rules.hold_unverified_corrections
                else CorrectionStatus.REJECTED.value
            )
            reasons = (REASON_NO_CORRECTION,)
            corrected: Mapping[str, Any] = {}
            verification_id = ""
            verified = False
        else:
            status = _text(getattr(correction, "quality_status", ""))
            if status not in {
                member.value for member in CorrectionStatus
            }:
                status = CorrectionStatus.NEEDS_REVIEW.value
            corrected = dict(getattr(correction, "corrected_output", {}) or {})
            verification = getattr(correction, "verification_result", None)
            verification_id = (
                getattr(verification, "verification_id", "") if verification is not None else ""
            )
            verified = bool(verification is not None and getattr(verification, "verified", False))
            reasons = tuple(getattr(correction, "reasons", ()) or ())
        prompt = dict(critique.observed_behavior) or {"task_id": critique.task_id}
        return CritiqueExample(
            kind=kind,
            prompt=prompt,
            original=dict(critique.observed_behavior),
            corrected=dict(corrected),
            critique_id=critique.critique_id,
            critique_category=critique.category,
            severity=critique.severity,
            verification_id=verification_id,
            verified=verified,
            source_trajectory_id=critique.trajectory_id,
            status=status,
            reasons=reasons,
            group_key_name=critique.task_id or critique.trajectory_id,
            tags=("rlvr", critique.category),
        )

    @staticmethod
    def _row_reasoning_free(row: CritiqueExample) -> bool:
        return not reasoning_violations(
            {
                "prompt": dict(row.prompt),
                "original": dict(row.original),
                "corrected": dict(row.corrected),
            }
        )

    # -- statistics ---------------------------------------------------------------

    @staticmethod
    def _statistics(
        rows: Sequence[CritiqueExample],
        *,
        splits: Mapping[str, tuple[str, ...]],
        skipped: Mapping[str, int],
        source_trajectories: int,
    ) -> CritiqueDatasetStatistics:
        by_kind: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for row in rows:
            by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
            if row.critique_category:
                by_category[row.critique_category] = (
                    by_category.get(row.critique_category, 0) + 1
                )
            if row.severity:
                by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
            source = "verified" if row.verified else "rule_based"
            by_source[source] = by_source.get(source, 0) + 1
        groups = {row.group_key for row in rows}
        return CritiqueDatasetStatistics(
            total=len(rows),
            accepted=sum(1 for row in rows if row.accepted),
            rejected=sum(1 for row in rows if row.status == CorrectionStatus.REJECTED.value),
            needs_review=sum(
                1 for row in rows if row.status == CorrectionStatus.NEEDS_REVIEW.value
            ),
            verified=sum(1 for row in rows if row.verified),
            unverified=sum(1 for row in rows if row.corrected and not row.verified),
            corrections=sum(1 for row in rows if row.corrected),
            accepted_corrections=sum(
                1 for row in rows if row.corrected and row.accepted
            ),
            by_kind=by_kind,
            by_category=by_category,
            by_severity=by_severity,
            by_source=by_source,
            groups=len(groups),
            source_trajectories=source_trajectories,
            estimated_tokens=sum(row.estimated_tokens for row in rows),
            by_split={name: len(ids) for name, ids in splits.items() if ids},
            skipped=dict(skipped),
        )

    # -- reading -------------------------------------------------------------------

    def validate(self, dataset: CritiqueDatasetVersion | None) -> tuple[str, ...]:
        """Every reason this dataset should not be trained on (empty is good)."""
        if dataset is None:
            return ("no critique dataset was supplied",)
        issues: list[str] = []
        if not dataset.name or not dataset.version:
            issues.append("the dataset has no name or no version")
        if dataset.dataset_version_id != critique_dataset_version_id(
            dataset.name, dataset.version
        ):
            issues.append("dataset_version_id does not match name@version")
        if not dataset.examples:
            issues.append("the dataset has no examples")
        if not dataset.split_names():
            issues.append("the dataset has no non-empty splits")
        known = {row.example_id for row in dataset.examples}
        for name, ids in dataset.splits.items():
            unknown = [item for item in ids if item not in known]
            if unknown:
                issues.append(
                    f"split {name!r} references {len(unknown)} example id(s) that are "
                    "not in the dataset"
                )
        trained = [row for row in dataset.examples if row.accepted]
        if not trained:
            issues.append(
                "no accepted example is present: every correction is held or rejected"
            )
        unverified = [
            row
            for row in trained
            if row.corrected
            and not row.verified
            and self.config.require_verification_for_correction
        ]
        if unverified:
            issues.append(
                f"{len(unverified)} accepted correction(s) carry no verified "
                "evidence, which the critique configuration forbids"
            )
        for row in dataset.examples:
            problems = reasoning_violations(
                {
                    "prompt": dict(row.prompt),
                    "original": dict(row.original),
                    "corrected": dict(row.corrected),
                }
            )
            if problems:
                issues.append(
                    f"example {row.example_id!r} carries hidden reasoning: "
                    + ", ".join(problems)
                )
                break
        fingerprints: dict[str, str] = {}
        for row in dataset.examples:
            fingerprint = row.fingerprint()
            if fingerprint in fingerprints:
                issues.append(
                    f"examples {fingerprints[fingerprint]!r} and {row.example_id!r} "
                    "are duplicates"
                )
                break
            fingerprints[fingerprint] = row.example_id
        return tuple(issues)

    @staticmethod
    def describe(dataset: CritiqueDatasetVersion) -> dict[str, Any]:
        return {
            "dataset_version": dataset.dataset_version_id,
            "name": dataset.name,
            "version": dataset.version,
            "examples": len(dataset),
            "splits": {name: len(ids) for name, ids in dataset.splits.items() if ids},
            "statistics": dataset.statistics.to_dict(),
            "by_kind": {
                kind: len(dataset.by_kind(kind))
                for kind in {row.kind for row in dataset.examples}
            },
        }

    def as_sft_examples(
        self, dataset: CritiqueDatasetVersion, *, accepted_only: bool = False
    ) -> tuple[SFTTrainingExample, ...]:
        """The dataset projected into Phase 16's example shape."""
        rows = dataset.accepted_examples() if accepted_only else dataset.examples
        return tuple(row.to_sft_example() for row in rows)

    # -- Phase 17 integration ---------------------------------------------------------

    def to_preference_pairs(
        self,
        dataset: CritiqueDatasetVersion,
        *,
        verified_only: bool = True,
    ) -> tuple[PreferenceExample, ...]:
        """Corrections as preference pairs: chosen=correction, rejected=original.

        Only accepted rows with a correction take part. When ``verified_only``
        is set (the default), the correction must carry a PASS verification:
        a preference pair teaches a model to prefer one behaviour over another,
        and an unverified suggestion must not be smuggled into that role.
        """
        pairs: list[PreferenceExample] = []
        for row in dataset.examples:
            if not row.accepted or not row.corrected:
                continue
            if verified_only and not row.verified:
                continue
            evidence = PreferenceEvidence(
                kind=EVIDENCE_VERIFIED_SUCCESS,
                strength=1.0 if row.verified else 0.5,
                detail=(
                    f"correction verified by {row.verification_id}"
                    if row.verified
                    else "correction carries no verification"
                ),
                source="rlvr",
                verified=row.verified,
            )
            pairs.append(
                PreferenceExample(
                    dataset_type=_preference_family(row.kind),
                    prompt={
                        **dict(row.prompt),
                        "group_key": row.group_key,
                    },
                    context={
                        "critique_id": row.critique_id,
                        "critique_category": row.critique_category,
                        "severity": row.severity,
                        "source_dataset": dataset.dataset_version_id,
                    },
                    chosen=dict(row.corrected),
                    rejected=dict(row.original),
                    chosen_outcome={"verified": row.verified},
                    rejected_outcome={"failed": True},
                    preference_source=PreferenceSource.VERIFIED_OUTCOME.value,
                    confidence=1.0 if row.verified else 0.5,
                    strength=PreferenceStrength(
                        confidence=1.0 if row.verified else 0.5,
                        evidence_quality=1.0 if row.verified else 0.4,
                        verification_strength=1.0 if row.verified else 0.0,
                        evidence=(evidence,),
                        source=PreferenceSource.VERIFIED_OUTCOME.value,
                    ),
                    source_trajectory_ids=(row.source_trajectory_id,)
                    if row.source_trajectory_id
                    else (),
                    difficulty=row.difficulty,
                    tags=(*row.tags, "critique_correction"),
                )
            )
        return tuple(pairs)


def _preference_family(kind: str) -> str:
    """Which Phase 17 family a critique row belongs to."""
    name = _text(kind)
    if name == CritiqueExampleKind.PLAN.value:
        return "planning"
    if name == CritiqueExampleKind.TOOL.value:
        return "tool_selection"
    if name == CritiqueExampleKind.RESPONSE.value:
        return "response"
    return "decision"


def critique_dataset_fingerprint(dataset: CritiqueDatasetVersion) -> str:
    payload = json.dumps(
        {
            "name": dataset.name,
            "version": dataset.version,
            "examples": [row.fingerprint() for row in dataset.examples],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
