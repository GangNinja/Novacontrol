"""General-purpose skills: acquisition, transfer, composition and execution.

The original callable registry (``FunctionSkill`` / ``SkillRegistry``) remains
available. Phase 31 adds the durable, evidence-based catalog and service.
"""

from novacontrol.skills.acquisition import SkillAcquisition
from novacontrol.skills.catalog import DuplicateSkillError, SkillCatalog
from novacontrol.skills.composition import SkillComposer
from novacontrol.skills.models import (
    AcquisitionResult,
    AdaptationResult,
    Compatibility,
    CompositionResult,
    Skill,
    SkillCategory,
    SkillEvidence,
    SkillExecution,
    SkillInput,
    SkillInvocation,
    SkillLifecycle,
    SkillMatch,
    SkillObservation,
    SkillReliability,
    SkillResult,
    SkillSchema,
    SkillStatus,
    SkillStep,
    SkillStepKind,
    StepOutcome,
)
from novacontrol.skills.registry import FunctionSkill, SkillRegistry
from novacontrol.skills.service import DEFAULT_SKILL_SCOPE, SkillService
from novacontrol.skills.transfer import SkillTransfer

__all__ = [
    "AcquisitionResult",
    "AdaptationResult",
    "Compatibility",
    "CompositionResult",
    "DEFAULT_SKILL_SCOPE",
    "DuplicateSkillError",
    "FunctionSkill",
    "Skill",
    "SkillAcquisition",
    "SkillCatalog",
    "SkillCategory",
    "SkillComposer",
    "SkillEvidence",
    "SkillExecution",
    "SkillInput",
    "SkillInvocation",
    "SkillLifecycle",
    "SkillMatch",
    "SkillObservation",
    "SkillReliability",
    "SkillRegistry",
    "SkillResult",
    "SkillSchema",
    "SkillService",
    "SkillStatus",
    "SkillStep",
    "SkillStepKind",
    "SkillTransfer",
    "StepOutcome",
]
