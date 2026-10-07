"""Measuring the verifiers and the critiques themselves.

A reward built on a wrong verifier teaches the wrong thing, so Phase 19
evaluates its own instruments as well as the model:

  * **Verification accuracy** — did the verdict match the labelled truth?
  * **False positives / false negatives** — a PASS on a case that should have
    failed is the dangerous direction (it rewards a broken run); a FAIL on a
    case that should have passed is the costly one (it punishes good work).
  * **Verifier agreement** — where two verifiers saw the same subject, did they
    reach the same status? Disagreement is not an error; it is a prompt to
    look.
  * **Critique accuracy and agreement** — did the critique name the category
    the case was labelled with, and do two critique sources agree?
  * **Correction success** — how many corrections survived verification.

Everything reads observations and labels that already exist. Nothing here
consults a model, and nothing stores a reasoning trace: the evaluator's own
output is counts and rates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from novacontrol.evaluation.models import now_iso
from novacontrol.rlhf.models import RewardIntegrityCheck, RewardIntegrityStatus
from novacontrol.rlvr.models import (
    CritiqueResult,
    VerificationResult,
    VerificationStatus,
    VerificationSummary,
    _text,
)


@dataclass(frozen=True, slots=True)
class RLVREvaluation:
    """What the verifiers and critiques actually measured, in numbers."""

    evaluation_id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str = ""
    dataset_version: str = ""
    verifications: int = 0
    verification_accuracy: float | None = None
    task_success: float | None = None
    partial_task_success: float | None = None
    false_positive_verification: int = 0
    false_negative_verification: int = 0
    verifier_agreement: float | None = None
    verifier_pairs: int = 0
    critiques: int = 0
    critique_accuracy: float | None = None
    critique_agreement: float | None = None
    correction_success: float | None = None
    reward_integrity: Mapping[str, int] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def integrity_ok(self) -> bool:
        return self.reward_integrity.get("invalid", 0) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "run_id": self.run_id,
            "dataset_version": self.dataset_version,
            "verifications": self.verifications,
            "verification_accuracy": self.verification_accuracy,
            "task_success": self.task_success,
            "partial_task_success": self.partial_task_success,
            "false_positive_verification": self.false_positive_verification,
            "false_negative_verification": self.false_negative_verification,
            "verifier_agreement": self.verifier_agreement,
            "verifier_pairs": self.verifier_pairs,
            "critiques": self.critiques,
            "critique_accuracy": self.critique_accuracy,
            "critique_agreement": self.critique_agreement,
            "correction_success": self.correction_success,
            "reward_integrity": dict(self.reward_integrity),
            "metrics": dict(self.metrics),
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLVREvaluation:
        def real(key: str) -> float | None:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        def whole(key: str) -> int:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            return 0

        raw_integrity = data.get("reward_integrity")
        integrity: dict[str, int] = {}
        if isinstance(raw_integrity, Mapping):
            for name, value in raw_integrity.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    integrity[str(name)] = int(value)
        return cls(
            evaluation_id=_text(data.get("evaluation_id")) or uuid4().hex,
            run_id=_text(data.get("run_id")),
            dataset_version=_text(data.get("dataset_version")),
            verifications=whole("verifications"),
            verification_accuracy=real("verification_accuracy"),
            task_success=real("task_success"),
            partial_task_success=real("partial_task_success"),
            false_positive_verification=whole("false_positive_verification"),
            false_negative_verification=whole("false_negative_verification"),
            verifier_agreement=real("verifier_agreement"),
            verifier_pairs=whole("verifier_pairs"),
            critiques=whole("critiques"),
            critique_accuracy=real("critique_accuracy"),
            critique_agreement=real("critique_agreement"),
            correction_success=real("correction_success"),
            reward_integrity=integrity,
            metrics=dict(data.get("metrics") or {}),
            evidence=tuple(str(item) for item in data.get("evidence", []) if str(item)),
            created_at=_text(data.get("created_at")) or now_iso(),
        )


class RLVREvaluator:
    """Builds one :class:`RLVREvaluation` from observations already recorded."""

    def evaluate(
        self,
        *,
        summaries: Sequence[VerificationSummary] = (),
        results: Sequence[VerificationResult] = (),
        ground_truth: Mapping[str, str] = {},
        critiques: Sequence[CritiqueResult] = (),
        expected_categories: Mapping[str, str] = {},
        agreement_pairs: Sequence[tuple[VerificationResult, VerificationResult]] = (),
        critique_pairs: Sequence[tuple[CritiqueResult, CritiqueResult]] = (),
        integrity: Sequence[RewardIntegrityCheck] = (),
        corrections: Sequence[Any] = (),
        run_id: str = "",
        dataset_version: str = "",
    ) -> RLVREvaluation:
        rows = list(results)
        summaries_rows = list(summaries)
        evidence: list[str] = []
        accuracy: float | None = None
        false_positive = 0
        false_negative = 0
        labelled = [
            item
            for item in rows
            if item.verification_id in ground_truth
            or item.subject_key() in ground_truth
        ]
        if labelled:
            correct = 0
            for item in labelled:
                wanted = _text(
                    ground_truth.get(item.verification_id)
                    or ground_truth.get(item.subject_key())
                )
                if _status_matches(item.status, wanted):
                    correct += 1
                elif item.verified:
                    false_positive += 1
                elif item.failed:
                    false_negative += 1
            accuracy = round(correct / len(labelled), 6)
            evidence.append(f"verification_accuracy over {len(labelled)} labelled case(s)")
        task_success: float | None = None
        partial: float | None = None
        if summaries_rows:
            task_success = round(
                sum(1 for item in summaries_rows if item.passed) / len(summaries_rows), 6
            )
            partial = round(
                sum(item.pass_ratio for item in summaries_rows) / len(summaries_rows), 6
            )
        agreement: float | None = None
        if agreement_pairs:
            same = sum(1 for left, right in agreement_pairs if left.status == right.status)
            agreement = round(same / len(agreement_pairs), 6)
            evidence.append(f"verifier agreement over {len(agreement_pairs)} pair(s)")
        critique_accuracy: float | None = None
        if critiques and expected_categories:
            judged = [
                row
                for row in critiques
                if _text(expected_categories.get(row.critique_id))
                or _text(expected_categories.get(row.trajectory_id))
            ]
            if judged:
                correct = 0
                for row in judged:
                    wanted = _text(
                        expected_categories.get(row.critique_id)
                        or expected_categories.get(row.trajectory_id)
                    )
                    if row.category == wanted:
                        correct += 1
                critique_accuracy = round(correct / len(judged), 6)
        critique_agreement: float | None = None
        if critique_pairs:
            same = sum(
                1
                for left, right in critique_pairs
                if left.category == right.category
            )
            critique_agreement = round(same / len(critique_pairs), 6)
        correction_success: float | None = None
        if corrections:
            verdicts = [
                1.0 if bool(getattr(item, "verified", False)) else 0.0
                for item in corrections
            ]
            correction_success = round(sum(verdicts) / len(verdicts), 6)
        integrity_counts: dict[str, int] = {}
        for check in integrity:
            key = check.status or RewardIntegrityStatus.VALID.value
            integrity_counts[key] = integrity_counts.get(key, 0) + 1
        return RLVREvaluation(
            run_id=run_id,
            dataset_version=dataset_version,
            verifications=len(rows),
            verification_accuracy=accuracy,
            task_success=task_success,
            partial_task_success=partial,
            false_positive_verification=false_positive,
            false_negative_verification=false_negative,
            verifier_agreement=agreement,
            verifier_pairs=len(agreement_pairs),
            critiques=len(critiques),
            critique_accuracy=critique_accuracy,
            critique_agreement=critique_agreement,
            correction_success=correction_success,
            reward_integrity=integrity_counts,
            metrics={
                "verification_statuses": _status_counts(rows),
                "critique_categories": _critique_counts(critiques),
                "summaries": len(summaries_rows),
            },
            evidence=tuple(evidence),
        )


def _status_matches(observed: str, wanted: str) -> bool:
    name = _text(wanted).lower()
    if not name:
        return True
    if name in {"pass", "passed", "success", "ok"}:
        return observed == VerificationStatus.PASS.value
    if name in {"fail", "failed", "failure"}:
        return observed == VerificationStatus.FAIL.value
    if name in {"partial", "partially"}:
        return observed == VerificationStatus.PARTIAL.value
    if name in {"inconclusive", "unknown"}:
        return observed == VerificationStatus.INCONCLUSIVE.value
    return observed.lower() == name


def _status_counts(rows: Sequence[VerificationResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def _critique_counts(rows: Sequence[CritiqueResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.category] = counts.get(item.category, 0) + 1
    return counts


__all__ = ["RLVREvaluation", "RLVREvaluator"]
