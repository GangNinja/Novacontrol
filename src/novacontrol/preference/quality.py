"""The preference-quality gate: which pairs may be learned from, said out loud.

A preference pair is worse than useless when it is wrong: a model asked to prefer
the worse of two behaviours learns the mistake, confidently. So every pair is
checked before it is trusted, and the checks are the ones the phase names —
identical candidates, malformed outputs, two failures, thin evidence, verification
missing where verification is required, an orientation that contradicts its own
outcomes, residual secrets, missing provenance, low confidence, and two candidates
that cannot be compared at all.

Three rules shape this filter, and they are the same ones Phase 15's data-quality
gate already follows, because a reader should not have to learn a second set:

  * **nothing is deleted silently.** The filter classifies; it never removes a row
    from a store, and a rejected pair keeps its structured reasons so "why did the
    dataset shrink?" stays answerable.
  * **review is a real verdict, not a soft rejection.** A pair whose evidence is
    thin, or whose source is unverified, is genuinely undecided: throwing it away
    loses the reviewer's chance to settle it, and accepting it poisons the set. It
    is held, and `PreferenceReviewQueue` is where a person settles it.
  * **the lines it draws are configuration.** How much confidence is enough, how
    strong the evidence must be, whether residual sensitive text rejects or is
    held, and which sources are in scope all live in
    :class:`PreferenceQualityConfig`.

An ERROR rejects, a WARN holds for review, an INFO is a note on an accepted pair —
the severity vocabulary is Phase 15's, imported rather than re-declared.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.audit.redact import Redactor
from novacontrol.evaluation.models import now_iso
from novacontrol.evaluation.quality import IssueSeverity
from novacontrol.preference.models import (
    PreferenceDatasetType,
    PreferenceExample,
    PreferenceQualityStatus,
    PreferenceSource,
)

#: Bounds on :class:`PreferenceQualityConfig`, so a configured threshold cannot
#: become "no threshold at all" by accident.
MAX_PREFERENCE_CONFIDENCE = 1.0
MAX_EVIDENCE_FLOOR = 1.0


class PreferenceRuleCode(StrEnum):
    """Every reason this filter can give, as a machine-readable code."""

    FILTER_DISABLED = "filter_disabled"
    EMPTY_PROMPT = "empty_prompt"
    EMPTY_CANDIDATE = "empty_candidate"
    IDENTICAL_CANDIDATES = "identical_candidates"
    UNKNOWN_DATASET_TYPE = "unknown_dataset_type"
    MALFORMED_CANDIDATE = "malformed_candidate"
    INCOMPARABLE_CANDIDATES = "incomparable_candidates"
    BOTH_CANDIDATES_FAILED = "both_candidates_failed"
    ORIENTATION_CONTRADICTS_OUTCOME = "orientation_contradicts_outcome"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    WEAK_EVIDENCE = "weak_evidence"
    LOW_CONFIDENCE = "low_confidence"
    VERIFICATION_MISSING = "verification_missing"
    MISSING_PROVENANCE = "missing_provenance"
    UNVERIFIED_SOURCE = "unverified_source"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    SENSITIVE_DATA = "sensitive_data"
    MALFORMED_DATA = "malformed_data"


#: What each rule means by default. One table, so the severity of every finding is
#: auditable in a single place rather than scattered through ten checks.
DEFAULT_SEVERITIES: Mapping[PreferenceRuleCode, IssueSeverity] = {
    PreferenceRuleCode.FILTER_DISABLED: IssueSeverity.INFO,
    PreferenceRuleCode.EMPTY_PROMPT: IssueSeverity.ERROR,
    PreferenceRuleCode.EMPTY_CANDIDATE: IssueSeverity.ERROR,
    PreferenceRuleCode.IDENTICAL_CANDIDATES: IssueSeverity.ERROR,
    PreferenceRuleCode.UNKNOWN_DATASET_TYPE: IssueSeverity.ERROR,
    PreferenceRuleCode.MALFORMED_CANDIDATE: IssueSeverity.ERROR,
    PreferenceRuleCode.INCOMPARABLE_CANDIDATES: IssueSeverity.ERROR,
    PreferenceRuleCode.BOTH_CANDIDATES_FAILED: IssueSeverity.ERROR,
    PreferenceRuleCode.ORIENTATION_CONTRADICTS_OUTCOME: IssueSeverity.ERROR,
    PreferenceRuleCode.INSUFFICIENT_EVIDENCE: IssueSeverity.WARN,
    PreferenceRuleCode.WEAK_EVIDENCE: IssueSeverity.WARN,
    PreferenceRuleCode.LOW_CONFIDENCE: IssueSeverity.WARN,
    PreferenceRuleCode.VERIFICATION_MISSING: IssueSeverity.WARN,
    PreferenceRuleCode.MISSING_PROVENANCE: IssueSeverity.WARN,
    PreferenceRuleCode.UNVERIFIED_SOURCE: IssueSeverity.WARN,
    PreferenceRuleCode.SOURCE_NOT_ALLOWED: IssueSeverity.ERROR,
    PreferenceRuleCode.SENSITIVE_DATA: IssueSeverity.ERROR,
    PreferenceRuleCode.MALFORMED_DATA: IssueSeverity.ERROR,
}

#: Names of the checks :meth:`PreferenceQualityFilter.assess` runs, in order.
#: Recorded on the verdict, so a stored pair says WHAT was checked, not only what
#: was found.
CHECKS_RUN: tuple[str, ...] = (
    "presence",
    "shape",
    "comparability",
    "orientation",
    "evidence",
    "provenance",
    "privacy",
    "serialisable",
)

#: The keys each preference family must expose on BOTH sides for the pair to mean
#: anything. A tool preference whose rejected side has no tool cannot be compared,
#: and calling that "a weak pair" would hide a malformed one.
REQUIRED_CANDIDATE_KEYS: Mapping[str, tuple[str, ...]] = {
    PreferenceDatasetType.NLU.value: ("intent",),
    PreferenceDatasetType.DECISION.value: ("route",),
    PreferenceDatasetType.TOOL_SELECTION.value: ("tool",),
    PreferenceDatasetType.PLANNING.value: ("steps",),
    PreferenceDatasetType.RECOVERY.value: ("strategy",),
    PreferenceDatasetType.RESPONSE.value: (),
}


def _settled(example: PreferenceExample) -> bool:
    """Whether a reviewer has already chosen a side for this pair."""
    decision = str(example.review.get("decision", "")).strip()
    return decision in {"choose_a", "choose_b"}


@dataclass(frozen=True, slots=True)
class PreferenceQualityConfig:
    """Every line this filter draws, in one place."""

    enabled: bool = True
    #: Below this, the pair is held for review rather than trusted.
    min_confidence: float = 0.35
    #: Below this, the evidence is too weak to accept without a human.
    min_evidence_strength: float = 0.3
    #: Require the preferred candidate to carry a VERIFIED outcome.
    require_verified_outcome: bool = False
    #: Require at least one trajectory or evaluation id behind the pair.
    require_provenance: bool = True
    #: Hold a pair whose source is a teacher model or a synthetic rule.
    require_review_for_unverified: bool = True
    #: When true, residual sensitive text rejects the pair; when false it holds it.
    residual_sensitive_rejects: bool = True
    #: Sources in scope. Empty means every source is allowed.
    allow_sources: tuple[str, ...] = ()
    #: Drop the INFO-level note from the verdict's issues (it stays in `checks`).
    keep_notes: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_confidence": self.min_confidence,
            "min_evidence_strength": self.min_evidence_strength,
            "require_verified_outcome": self.require_verified_outcome,
            "require_provenance": self.require_provenance,
            "require_review_for_unverified": self.require_review_for_unverified,
            "residual_sensitive_rejects": self.residual_sensitive_rejects,
            "allow_sources": list(self.allow_sources),
            "keep_notes": self.keep_notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> PreferenceQualityConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults

        def flag(key: str, fallback: bool) -> bool:
            value = data.get(key)
            return value if isinstance(value, bool) else fallback

        def unit(key: str, fallback: float) -> float:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return fallback
            return round(min(MAX_PREFERENCE_CONFIDENCE, max(0.0, float(value))), 6)

        sources = data.get("allow_sources")
        if isinstance(sources, str):
            allowed = tuple(part.strip() for part in sources.split(",") if part.strip())
        elif isinstance(sources, (list, tuple, set, frozenset)):
            allowed = tuple(str(item).strip() for item in sources if str(item).strip())
        else:
            allowed = defaults.allow_sources
        return cls(
            enabled=flag("enabled", defaults.enabled),
            min_confidence=unit("min_confidence", defaults.min_confidence),
            min_evidence_strength=unit(
                "min_evidence_strength", defaults.min_evidence_strength
            ),
            require_verified_outcome=flag(
                "require_verified_outcome", defaults.require_verified_outcome
            ),
            require_provenance=flag("require_provenance", defaults.require_provenance),
            require_review_for_unverified=flag(
                "require_review_for_unverified", defaults.require_review_for_unverified
            ),
            residual_sensitive_rejects=flag(
                "residual_sensitive_rejects", defaults.residual_sensitive_rejects
            ),
            allow_sources=allowed,
            keep_notes=flag("keep_notes", defaults.keep_notes),
        )


@dataclass(frozen=True, slots=True)
class PreferenceIssue:
    """One finding about one pair, in the shape a diff can act on."""

    code: str
    severity: str = IssueSeverity.WARN.value
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreferenceIssue:
        code = str(data.get("code", "")).strip()
        severity = str(data.get("severity", IssueSeverity.WARN.value)).strip()
        detail = str(data.get("detail", "")).strip()
        return cls(code=code, severity=severity or IssueSeverity.WARN.value, detail=detail)


@dataclass(frozen=True, slots=True)
class PreferenceQualityVerdict:
    """The filter's answer about one pair, with its reasons."""

    status: str = PreferenceQualityStatus.NEEDS_REVIEW.value
    issues: tuple[PreferenceIssue, ...] = ()
    checks: tuple[str, ...] = ()
    fingerprint: str = ""
    checked_at: str = field(default_factory=now_iso)

    @property
    def accepted(self) -> bool:
        return self.status == PreferenceQualityStatus.ACCEPTED.value

    @property
    def rejected(self) -> bool:
        return self.status == PreferenceQualityStatus.REJECTED.value

    @property
    def needs_review(self) -> bool:
        return self.status == PreferenceQualityStatus.NEEDS_REVIEW.value

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "reasons": list(self.reasons),
            "checks": list(self.checks),
            "fingerprint": self.fingerprint,
            "checked_at": self.checked_at,
        }


class PreferenceQualityFilter:
    """Classifies preference pairs: ACCEPTED / REJECTED / NEEDS_REVIEW."""

    def __init__(
        self,
        config: PreferenceQualityConfig | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.config = config if config is not None else PreferenceQualityConfig()
        self.redactor = redactor if redactor is not None else Redactor()

    # -- severity --------------------------------------------------------------

    @staticmethod
    def severity_of(code: PreferenceRuleCode) -> str:
        return DEFAULT_SEVERITIES.get(code, IssueSeverity.WARN).value

    def _issue(self, code: PreferenceRuleCode, detail: str = "") -> PreferenceIssue:
        return PreferenceIssue(code=code.value, severity=self.severity_of(code), detail=detail)

    # -- privacy ---------------------------------------------------------------

    def sanitise(self, example: PreferenceExample) -> tuple[PreferenceExample, Any]:
        """Redact a pair on the way in; report what the redactor found.

        Both candidates and their outcomes are redacted, so a secret that sat in
        the rejected side cannot survive because only the chosen side was read.
        """
        payload = example.to_dict()
        cleaned, report = self.redactor.redact_value(payload)
        if not isinstance(cleaned, Mapping):
            return (example, report)
        return (PreferenceExample.from_dict(cleaned), report)

    def apply(
        self, example: PreferenceExample
    ) -> tuple[PreferenceExample | None, PreferenceQualityVerdict]:
        """Sanitise, then classify. Returns ``(pair or None, verdict)``.

        The pair comes back ``None`` only when it must not be learned from: a
        rejected verdict, or residual sensitive text under a configuration that
        rejects it. Everything else comes back with its verdict attached, so a
        held pair can keep waiting for a reviewer.
        """
        redacted, report = self.sanitise(example)
        verdict = self.assess(redacted, redactions=int(getattr(report, "count", 0) or 0))
        if verdict.accepted or verdict.needs_review:
            return (redacted.with_quality(verdict.status, reasons=verdict.reasons), verdict)
        return (None, verdict)

    # -- classification --------------------------------------------------------

    def assess(
        self, example: PreferenceExample, *, redactions: int = 0
    ) -> PreferenceQualityVerdict:
        """Every finding about one pair, and the verdict they add up to."""
        config = self.config
        if not config.enabled:
            return PreferenceQualityVerdict(
                status=PreferenceQualityStatus.ACCEPTED.value,
                issues=(self._issue(PreferenceRuleCode.FILTER_DISABLED, "the filter is off"),),
                checks=("disabled",),
                fingerprint=example.fingerprint(),
            )
        issues: list[PreferenceIssue] = []
        checks: list[str] = []

        # presence
        checks.append("presence")
        if not example.prompt:
            issues.append(self._issue(PreferenceRuleCode.EMPTY_PROMPT, "no prompt"))
        if not example.chosen or not example.rejected:
            issues.append(
                self._issue(
                    PreferenceRuleCode.EMPTY_CANDIDATE,
                    "one of the two candidates is empty, so there is nothing to compare",
                )
            )

        # shape
        checks.append("shape")
        known = {member.value for member in PreferenceDatasetType}
        if example.dataset_type not in known:
            issues.append(
                self._issue(
                    PreferenceRuleCode.UNKNOWN_DATASET_TYPE,
                    f"unknown dataset_type {example.dataset_type!r}",
                )
            )
        else:
            required = REQUIRED_CANDIDATE_KEYS.get(example.dataset_type, ())
            missing = [
                key
                for key in required
                if key not in example.chosen or key not in example.rejected
            ]
            if missing:
                issues.append(
                    self._issue(
                        PreferenceRuleCode.MALFORMED_CANDIDATE,
                        "both sides must expose " + ", ".join(missing),
                    )
                )

        # comparability
        checks.append("comparability")
        if example.chosen and example.rejected:
            if dict(example.chosen) == dict(example.rejected):
                issues.append(
                    self._issue(
                        PreferenceRuleCode.IDENTICAL_CANDIDATES,
                        "the two candidates are the same: the pair teaches nothing",
                    )
                )
            elif not set(example.chosen) & set(example.rejected):
                issues.append(
                    self._issue(
                        PreferenceRuleCode.INCOMPARABLE_CANDIDATES,
                        "the candidates share no field, so they are not comparable "
                        "outputs of the same step",
                    )
                )

        # orientation
        checks.append("orientation")
        chosen_success = example.chosen_outcome.get("success")
        rejected_success = example.rejected_outcome.get("success")
        if chosen_success is False and rejected_success is False:
            issues.append(
                self._issue(
                    PreferenceRuleCode.BOTH_CANDIDATES_FAILED,
                    "both candidates failed, so neither is a behaviour to prefer",
                )
            )
        elif chosen_success is False and rejected_success is True:
            issues.append(
                self._issue(
                    PreferenceRuleCode.ORIENTATION_CONTRADICTS_OUTCOME,
                    "the rejected candidate succeeded and the chosen one did not",
                )
            )

        # evidence
        checks.append("evidence")
        strength = example.strength
        if not strength.evidence:
            issues.append(
                self._issue(
                    PreferenceRuleCode.INSUFFICIENT_EVIDENCE,
                    "the pair cites no evidence for its orientation",
                )
            )
        elif strength.evidence_quality < config.min_evidence_strength:
            issues.append(
                self._issue(
                    PreferenceRuleCode.WEAK_EVIDENCE,
                    f"evidence quality {strength.evidence_quality:.2f} is below "
                    f"{config.min_evidence_strength:.2f}",
                )
            )
        if example.confidence < config.min_confidence:
            issues.append(
                self._issue(
                    PreferenceRuleCode.LOW_CONFIDENCE,
                    f"confidence {example.confidence:.2f} is below "
                    f"{config.min_confidence:.2f}",
                )
            )
        if config.require_verified_outcome and not strength.verification_strength:
            issues.append(
                self._issue(
                    PreferenceRuleCode.VERIFICATION_MISSING,
                    "this configuration requires a verified outcome behind the "
                    "preferred candidate, and none is recorded",
                )
            )

        # provenance
        checks.append("provenance")
        if config.require_provenance and not (
            example.source_trajectory_ids
            or example.source_evaluation_ids
            or example.reviewed
        ):
            issues.append(
                self._issue(
                    PreferenceRuleCode.MISSING_PROVENANCE,
                    "the pair names no trajectory, evaluation or reviewer",
                )
            )
        if config.allow_sources and example.preference_source not in set(config.allow_sources):
            issues.append(
                self._issue(
                    PreferenceRuleCode.SOURCE_NOT_ALLOWED,
                    f"source {example.preference_source!r} is not in scope for this dataset",
                )
            )
        source = PreferenceSource(example.preference_source) if example.preference_source in {
            item.value for item in PreferenceSource
        } else None
        if (
            config.require_review_for_unverified
            and source is not None
            and not source.verified
            and not strength.verified
        ):
            issues.append(
                self._issue(
                    PreferenceRuleCode.UNVERIFIED_SOURCE,
                    f"source {example.preference_source!r} is not a verified "
                    "observation, so a person has to agree with it",
                )
            )

        # privacy
        checks.append("privacy")
        if redactions:
            issues.append(
                self._issue(
                    PreferenceRuleCode.SENSITIVE_DATA,
                    f"{redactions} sensitive value(s) were removed on the way in",
                )
            )
            if not config.residual_sensitive_rejects:
                issues[-1] = PreferenceIssue(
                    code=PreferenceRuleCode.SENSITIVE_DATA.value,
                    severity=IssueSeverity.WARN.value,
                    detail=issues[-1].detail,
                )

        # serialisable
        checks.append("serialisable")
        try:
            json.dumps(example.to_dict(), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            issues.append(
                self._issue(
                    PreferenceRuleCode.MALFORMED_DATA,
                    "the pair is not serialisable as JSON",
                )
            )

        if not config.keep_notes:
            issues = [
                issue for issue in issues if issue.severity != IssueSeverity.INFO.value
            ]
        return PreferenceQualityVerdict(
            status=self._status_for(issues, settled=_settled(example)),
            issues=tuple(issues),
            checks=tuple(checks),
            fingerprint=example.fingerprint(),
        )

    @staticmethod
    def _status_for(issues: Sequence[PreferenceIssue], *, settled: bool = False) -> str:
        """An ERROR rejects, a WARN holds — unless a person has already answered.

        ``settled`` means this pair has been through review and a reviewer chose a
        side. The doubts a WARN raises are questions ("is this evidence strong
        enough?", "was this pair submitted by a machine?"), and a reviewer picking
        a candidate IS the answer to them; re-holding the pair would make the queue
        something a person can never empty. An ERROR still rejects: identical
        candidates and a contradictory orientation are not matters of opinion.
        """
        if any(issue.severity == IssueSeverity.ERROR.value for issue in issues):
            return PreferenceQualityStatus.REJECTED.value
        if not settled and any(
            issue.severity == IssueSeverity.WARN.value for issue in issues
        ):
            return PreferenceQualityStatus.NEEDS_REVIEW.value
        return PreferenceQualityStatus.ACCEPTED.value


__all__ = [
    "CHECKS_RUN",
    "DEFAULT_SEVERITIES",
    "MAX_EVIDENCE_FLOOR",
    "MAX_PREFERENCE_CONFIDENCE",
    "REQUIRED_CANDIDATE_KEYS",
    "PreferenceIssue",
    "PreferenceQualityConfig",
    "PreferenceQualityFilter",
    "PreferenceQualityVerdict",
    "PreferenceRuleCode",
]
