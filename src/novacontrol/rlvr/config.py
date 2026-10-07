"""Phase 19 configuration: verifier policy, verifiable rewards, critique rules.

Every number that decides what learning is worth lives here, in versioned
records with explicit defaults — never inline in the application. Three
configurations compose one run:

  * :class:`VerifierPolicyConfig` — which verifiers may speak, whether a
    non-deterministic fallback is allowed at all, and how much evidence a
    verdict must carry.
  * :class:`VerifiableRewardConfig` — how a verification status maps to a
    reward, including partial credit and the safety floor.
  * :class:`CritiqueConfig` — which failures may be critiqued, which sources
    may speak, and when a correction has enough evidence to be learned from.

:class:`RLVRTrainingConfig` wraps Phase 18's :class:`RLTrainingConfig` rather
than copying it: the RL schedule, LoRA settings, dry-run switch and optimizers
stay in exactly one place, and an RLVR run inherits every safety property the
RLHF layer already has (dry run by default, mock policy only, explicit
confirmation for a real run).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.rlhf.config import RLTrainingConfig
from novacontrol.rlhf.models import MODES, PLANNED_ALGORITHMS, POLICY_ALGORITHMS
from novacontrol.rlvr.models import (
    CRITIQUE_CATEGORIES,
    CRITIQUE_SOURCES,
    RLVR_VERSION,
    SEVERITY_RANK,
    VERIFICATION_STATUSES,
    VERIFIER_CATEGORIES,
)
from novacontrol.training.config import TrainingConfigValidation

#: The run vocabulary this phase adds: a run whose algorithm is this learns from
#: verified outcomes and structured critiques.
RLVR_MODE = "rlvr"

#: The verifier categories that can decide a question without an opinion.
DETERMINISTIC_CATEGORIES: tuple[str, ...] = tuple(
    name for name in VERIFIER_CATEGORIES if name != "custom"
) + ("custom",)

#: Default reward per verification status. Explicit, versioned, and visible in
#: every dry run; a deployment may override any of them through the config file.
DEFAULT_VERIFIABLE_REWARDS: Mapping[str, float] = {
    "pass": 1.0,
    "partial": 0.4,
    "fail": -1.0,
    "inconclusive": 0.0,
    "error": -0.5,
}

#: Default critique severities for a status: a failed safety check is CRITICAL,
#: a failed step is HIGH, an inconclusive check is LOW.
DEFAULT_SEVERITY_BY_STATUS: Mapping[str, str] = {
    "fail": "high",
    "partial": "medium",
    "inconclusive": "low",
    "error": "medium",
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _whole(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _real(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _bounded(value: Any, default: float, *, low: float, high: float) -> float:
    return max(low, min(high, _real(value, default)))


def _names(value: Any, known: Sequence[str], default: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return tuple(default)
    chosen = tuple(
        str(item).strip().lower() for item in value if str(item).strip()
    )
    allowed = {name.lower() for name in known}
    kept = tuple(item for item in chosen if item in allowed)
    return kept or tuple(default)


@dataclass(frozen=True, slots=True)
class VerifierPolicyConfig:
    """Which verifiers may be selected, and what their verdicts must carry.

    ``deterministic_only`` defaults to True and is the phase's central promise:
    when a deterministic verifier can decide the question, an opinion is not
    consulted. ``allow_ai_evaluator`` is the explicit escape hatch —
    ``select`` only reaches for it when no deterministic verifier matches, and
    the reward it produces is marked as non-deterministic evidence.
    """

    version: str = RLVR_VERSION
    enabled: bool = True
    categories: tuple[str, ...] = VERIFIER_CATEGORIES
    deterministic_only: bool = False
    allow_ai_evaluator: bool = False
    allow_custom: bool = True
    require_evidence: bool = True
    min_confidence: float = 0.0
    max_verifiers: int = 3
    require_expected: bool = True
    note: str = ""

    def allows(self, category: str) -> bool:
        name = _text(category).lower()
        if not self.enabled:
            return False
        return not self.categories or name in {item.lower() for item in self.categories}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "categories": list(self.categories),
            "deterministic_only": self.deterministic_only,
            "allow_ai_evaluator": self.allow_ai_evaluator,
            "allow_custom": self.allow_custom,
            "require_evidence": self.require_evidence,
            "min_confidence": self.min_confidence,
            "max_verifiers": self.max_verifiers,
            "require_expected": self.require_expected,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> VerifierPolicyConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        return cls(
            version=_text(data.get("version"), defaults.version),
            enabled=_flag(data.get("enabled"), defaults.enabled),
            categories=_names(data.get("categories"), VERIFIER_CATEGORIES, defaults.categories),
            deterministic_only=_flag(
                data.get("deterministic_only"), defaults.deterministic_only
            ),
            allow_ai_evaluator=_flag(
                data.get("allow_ai_evaluator"), defaults.allow_ai_evaluator
            ),
            allow_custom=_flag(data.get("allow_custom"), defaults.allow_custom),
            require_evidence=_flag(data.get("require_evidence"), defaults.require_evidence),
            min_confidence=_bounded(
                data.get("min_confidence"), defaults.min_confidence, low=0.0, high=1.0
            ),
            max_verifiers=max(1, _whole(data.get("max_verifiers"), defaults.max_verifiers)),
            require_expected=_flag(data.get("require_expected"), defaults.require_expected),
            note=_text(data.get("note")),
        )

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class VerifiableRewardConfig:
    """How a verification status becomes a reward. Explicit and versioned.

    A reward built from a deterministic PASS is worth more than one built from
    an AI evaluator's opinion: ``evidence_weight`` scales the total by the
    strongest evidence behind the verification. The safety penalty is its own
    component and its own floor — an unsafe run must read unsafe even when its
    task was completed, or the learner is being taught to trade safety for
    task success.
    """

    version: str = f"verifiable.{RLVR_VERSION}"
    enabled: bool = True
    rewards: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_VERIFIABLE_REWARDS)
    )
    partial_credit: bool = True
    #: A partial verification beyond this count stops being "partially done" and
    #: reads as a failure with credit — the guard against farming partials.
    max_partial_credit: float = 0.75
    clip_low: float = -1.0
    clip_high: float = 1.0
    normalize: bool = False
    keep_safety_separate: bool = True
    safety_penalty: float = -1.0
    safety_floor: float = -1.0
    unsafe_failure_is_critical: bool = True
    #: How much of the total the strongest evidence is worth scaling by.
    evidence_weight: float = 0.5
    deterministic_evidence_weight: float = 1.0
    min_confidence: float = 0.25
    require_evidence: bool = True
    require_known_verifier: bool = True
    reject_inconsistent: bool = True
    allow_ai_fallback: bool = False
    note: str = ""

    def value_for(self, status: str) -> float:
        name = _text(status).lower()
        if name in self.rewards:
            return float(self.rewards[name])
        return float(DEFAULT_VERIFIABLE_REWARDS.get(name, 0.0))

    def clip(self, value: float) -> float:
        low = min(self.clip_low, self.clip_high)
        high = max(self.clip_low, self.clip_high)
        return max(low, min(high, float(value)))

    def weighted(self, value: float, *, deterministic: bool, confidence: float | None) -> float:
        """The same value scaled by how strong its evidence is."""
        strength = (
            self.deterministic_evidence_weight
            if deterministic
            else 1.0 - self.evidence_weight
        )
        if confidence is not None:
            strength *= max(0.0, min(1.0, float(confidence)))
        return self.clip(round(float(value) * strength, 6))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "rewards": {name: self.value_for(name) for name in VERIFICATION_STATUSES},
            "partial_credit": self.partial_credit,
            "max_partial_credit": self.max_partial_credit,
            "clip_low": self.clip_low,
            "clip_high": self.clip_high,
            "normalize": self.normalize,
            "keep_safety_separate": self.keep_safety_separate,
            "safety_penalty": self.safety_penalty,
            "safety_floor": self.safety_floor,
            "unsafe_failure_is_critical": self.unsafe_failure_is_critical,
            "evidence_weight": self.evidence_weight,
            "deterministic_evidence_weight": self.deterministic_evidence_weight,
            "min_confidence": self.min_confidence,
            "require_evidence": self.require_evidence,
            "require_known_verifier": self.require_known_verifier,
            "reject_inconsistent": self.reject_inconsistent,
            "allow_ai_fallback": self.allow_ai_fallback,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> VerifiableRewardConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        merged = {
            name: float(DEFAULT_VERIFIABLE_REWARDS.get(name, 0.0))
            for name in VERIFICATION_STATUSES
        }
        raw = data.get("rewards")
        if isinstance(raw, Mapping):
            for name in VERIFICATION_STATUSES:
                merged[name] = _real(raw.get(name), merged[name])
        return cls(
            version=_text(data.get("version"), defaults.version),
            enabled=_flag(data.get("enabled"), defaults.enabled),
            rewards=merged,
            partial_credit=_flag(data.get("partial_credit"), defaults.partial_credit),
            max_partial_credit=_bounded(
                data.get("max_partial_credit"), defaults.max_partial_credit, low=0.0, high=1.0
            ),
            clip_low=_bounded(data.get("clip_low"), defaults.clip_low, low=-1000.0, high=1000.0),
            clip_high=_bounded(
                data.get("clip_high"), defaults.clip_high, low=-1000.0, high=1000.0
            ),
            normalize=_flag(data.get("normalize"), defaults.normalize),
            keep_safety_separate=_flag(
                data.get("keep_safety_separate"), defaults.keep_safety_separate
            ),
            safety_penalty=_bounded(
                data.get("safety_penalty"), defaults.safety_penalty, low=-1000.0, high=0.0
            ),
            safety_floor=_bounded(
                data.get("safety_floor"), defaults.safety_floor, low=-1000.0, high=0.0
            ),
            unsafe_failure_is_critical=_flag(
                data.get("unsafe_failure_is_critical"), defaults.unsafe_failure_is_critical
            ),
            evidence_weight=_bounded(
                data.get("evidence_weight"), defaults.evidence_weight, low=0.0, high=1.0
            ),
            deterministic_evidence_weight=_bounded(
                data.get("deterministic_evidence_weight"),
                defaults.deterministic_evidence_weight,
                low=0.0,
                high=1.0,
            ),
            min_confidence=_bounded(
                data.get("min_confidence"), defaults.min_confidence, low=0.0, high=1.0
            ),
            require_evidence=_flag(data.get("require_evidence"), defaults.require_evidence),
            require_known_verifier=_flag(
                data.get("require_known_verifier"), defaults.require_known_verifier
            ),
            reject_inconsistent=_flag(
                data.get("reject_inconsistent"), defaults.reject_inconsistent
            ),
            allow_ai_fallback=_flag(data.get("allow_ai_fallback"), defaults.allow_ai_fallback),
            note=_text(data.get("note")),
        )

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CritiqueConfig:
    """Which failures may be critiqued, by whom, and what may be corrected.

    ``require_verification_for_correction`` defaults to True: a correction that
    was not verified is held for review rather than accepted, because otherwise
    an AI evaluator's suggestion would enter training data as if it were fact.
    """

    version: str = RLVR_VERSION
    enabled: bool = True
    categories: tuple[str, ...] = CRITIQUE_CATEGORIES
    sources: tuple[str, ...] = CRITIQUE_SOURCES
    min_severity: str = "low"
    max_critiques: int = 8
    require_verification_for_correction: bool = True
    hold_unverified_corrections: bool = True
    allow_counterfactual: bool = True
    analyze_successes: bool = False
    note: str = ""

    def allows_category(self, category: str) -> bool:
        name = _text(category).lower()
        if not self.enabled:
            return False
        return not self.categories or name in {item.lower() for item in self.categories}

    def allows_source(self, source: str) -> bool:
        name = _text(source).lower()
        return not self.sources or name in {item.lower() for item in self.sources}

    def meets_severity(self, severity: str) -> bool:
        floor = SEVERITY_RANK.get(_text(self.min_severity).lower(), 0)
        return SEVERITY_RANK.get(_text(severity).lower(), 1) >= floor

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "categories": list(self.categories),
            "sources": list(self.sources),
            "min_severity": self.min_severity,
            "max_critiques": self.max_critiques,
            "require_verification_for_correction": self.require_verification_for_correction,
            "hold_unverified_corrections": self.hold_unverified_corrections,
            "allow_counterfactual": self.allow_counterfactual,
            "analyze_successes": self.analyze_successes,
            "note": self.note,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> CritiqueConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        severity = _text(data.get("min_severity"), defaults.min_severity)
        if severity not in SEVERITY_RANK:
            severity = defaults.min_severity
        return cls(
            version=_text(data.get("version"), defaults.version),
            enabled=_flag(data.get("enabled"), defaults.enabled),
            categories=_names(data.get("categories"), CRITIQUE_CATEGORIES, defaults.categories),
            sources=_names(data.get("sources"), CRITIQUE_SOURCES, defaults.sources),
            min_severity=severity,
            max_critiques=max(1, _whole(data.get("max_critiques"), defaults.max_critiques)),
            require_verification_for_correction=_flag(
                data.get("require_verification_for_correction"),
                defaults.require_verification_for_correction,
            ),
            hold_unverified_corrections=_flag(
                data.get("hold_unverified_corrections"), defaults.hold_unverified_corrections
            ),
            allow_counterfactual=_flag(
                data.get("allow_counterfactual"), defaults.allow_counterfactual
            ),
            analyze_successes=_flag(data.get("analyze_successes"), defaults.analyze_successes),
            note=_text(data.get("note")),
        )

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RLVRTrainingConfig:
    """One RLVR run: Phase 18's schedule plus Phase 19's verification rules.

    Composition rather than inheritance, deliberately: the RL schedule stays
    :class:`RLTrainingConfig` (validated, dry-run-default, mock-optimizer-only),
    and this record adds only what is new. ``as_rl_training_config`` projects it
    for the shared machinery, so the run lifecycle, checkpoints, registry and
    evaluation gates are the ones that already exist.
    """

    rl: RLTrainingConfig = field(default_factory=RLTrainingConfig)
    verifiers: VerifierPolicyConfig = field(default_factory=VerifierPolicyConfig)
    reward: VerifiableRewardConfig = field(default_factory=VerifiableRewardConfig)
    critique: CritiqueConfig = field(default_factory=CritiqueConfig)
    critique_dataset_version: str = ""
    correction_dataset_version: str = ""
    verifier_ids: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    output_directory: str = ""
    notes: str = ""

    @property
    def mode(self) -> str:
        return RLVR_MODE

    @property
    def algorithm(self) -> str:
        return self.rl.algorithm

    @property
    def base_model(self) -> str:
        return self.rl.base_model

    @property
    def dry_run(self) -> bool:
        return self.rl.dry_run

    def as_rl_training_config(self) -> RLTrainingConfig:
        """The same run in Phase 18's shape, for the shared run lifecycle."""
        return replace(
            self.rl,
            reward_dataset_version=(
                self.rl.reward_dataset_version
                or self.critique_dataset_version
                or self.correction_dataset_version
            ),
            output_directory=self.output_directory or self.rl.output_directory,
            notes=self.notes or self.rl.notes,
        )

    def validate(self) -> TrainingConfigValidation:
        shared = self.as_rl_training_config().validate()
        errors = list(shared.errors)
        warnings = list(shared.warnings)
        if not self.critique_dataset_version and not self.correction_dataset_version:
            errors.append(
                "critique_dataset_version is required for an RLVR run: build a "
                "critique dataset from verified failures first"
            )
        if self.verifiers.deterministic_only and not self.verifiers.categories:
            errors.append(
                "verifiers.deterministic_only is set but no deterministic "
                "category is allowed"
            )
        if self.reward.allow_ai_fallback and not self.verifiers.allow_ai_evaluator:
            warnings.append(
                "reward.allow_ai_fallback is set but verifiers.allow_ai_evaluator "
                "is off, so no AI-evaluator fallback will ever be selected"
            )
        if self.critique.require_verification_for_correction and not self.reward.require_evidence:
            warnings.append(
                "corrections require verification but the reward policy does not "
                "require evidence — verify one of the two settings is intended"
            )
        if self.rl.algorithm in PLANNED_ALGORITHMS:
            warnings.append(
                f"algorithm {self.rl.algorithm!r} is named but not implemented; "
                "only the deterministic mock policy optimizer runs in this phase"
            )
        if self.critique.enabled and not self.critique.sources:
            warnings.append(
                "critique.sources is empty: no critique source would ever speak"
            )
        return TrainingConfigValidation(errors=tuple(errors), warnings=tuple(warnings))

    def with_defaults(self, *, output_directory: str = "") -> RLVRTrainingConfig:
        target = output_directory or self.output_directory
        return replace(
            self,
            rl=self.rl.with_defaults(output_directory=target),
            output_directory=target or self.rl.output_directory,
        )

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_mapping(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": RLVR_MODE,
            "rl": self.rl.to_mapping(),
            "verifiers": self.verifiers.to_mapping(),
            "reward": self.reward.to_mapping(),
            "critique": self.critique.to_mapping(),
            "critique_dataset_version": self.critique_dataset_version,
            "correction_dataset_version": self.correction_dataset_version,
            "verifier_ids": list(self.verifier_ids),
            "task_types": list(self.task_types),
            "output_directory": self.output_directory,
            "notes": self.notes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> RLVRTrainingConfig:
        defaults = cls()
        if not isinstance(data, Mapping):
            return defaults
        rl_raw = data.get("rl")
        rl = (
            RLTrainingConfig.from_mapping(rl_raw)
            if isinstance(rl_raw, Mapping)
            else RLTrainingConfig()
        )
        if not any(member == rl.mode for member in MODES):
            rl = replace(rl, mode="rlhf")
        if rl.algorithm not in POLICY_ALGORITHMS:
            rl = replace(rl, algorithm=defaults.rl.algorithm)
        verifier_raw = data.get("verifiers")
        reward_raw = data.get("reward")
        critique_raw = data.get("critique")
        return cls(
            rl=rl,
            verifiers=VerifierPolicyConfig.from_mapping(verifier_raw)
            if isinstance(verifier_raw, Mapping)
            else defaults.verifiers,
            reward=VerifiableRewardConfig.from_mapping(reward_raw)
            if isinstance(reward_raw, Mapping)
            else defaults.reward,
            critique=CritiqueConfig.from_mapping(critique_raw)
            if isinstance(critique_raw, Mapping)
            else defaults.critique,
            critique_dataset_version=_text(data.get("critique_dataset_version")),
            correction_dataset_version=_text(data.get("correction_dataset_version")),
            verifier_ids=tuple(
                str(item).strip()
                for item in data.get("verifier_ids", [])
                if str(item).strip()
            )
            if isinstance(data.get("verifier_ids"), (list, tuple))
            else (),
            task_types=tuple(
                str(item).strip()
                for item in data.get("task_types", [])
                if str(item).strip()
            )
            if isinstance(data.get("task_types"), (list, tuple))
            else (),
            output_directory=_text(data.get("output_directory")),
            notes=_text(data.get("notes")),
        )


__all__ = [
    "DEFAULT_SEVERITY_BY_STATUS",
    "DEFAULT_VERIFIABLE_REWARDS",
    "DETERMINISTIC_CATEGORIES",
    "RLVR_MODE",
    "CritiqueConfig",
    "RLVRTrainingConfig",
    "VerifiableRewardConfig",
    "VerifierPolicyConfig",
]
