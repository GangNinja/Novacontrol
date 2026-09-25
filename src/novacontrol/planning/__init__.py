"""Planning subsystem: plan a goal, then run it and check it.

Two generations of the same idea live here, and both are exported:

    PlanningEngine / PlanningModule   the original goal decomposition: turn a
                                      sentence into ordered steps;
    PlanCompiler / WorkflowExecutor   Phase 4: turn a decision and a goal into
                                      steps that name a TOOL, carry an EFFECT,
                                      an expected result and a way to be
                                      VERIFIED — then execute them in
                                      dependency order, behind the existing
                                      approval layer, with bounded retries.

`AgentLoop` drives the whole sequence (understand → decide → plan → execute →
observe → verify → recover → answer) and reports what could NOT be verified
alongside what could.
"""

from novacontrol.planning.agent import (
    DEFAULT_MAX_CYCLES,
    AgentEvent,
    AgentLoop,
    AgentPhase,
    AgentRun,
)
from novacontrol.planning.compiler import Clause, ClauseKind, PlanCompiler, StepTemplate
from novacontrol.planning.engine import ClarificationPolicy, PlanningEngine
from novacontrol.planning.executor import (
    MAX_WAVE_SIZE,
    ConfirmationChannel,
    RecoveryPolicy,
    StepContext,
    StepHandler,
    StepObserver,
    WorkflowExecutor,
)
from novacontrol.planning.models import (
    EFFECTS_NEEDING_CONFIRMATION,
    EFFECTS_NEEDING_VERIFICATION,
    EFFECTS_NEVER_RETRIED,
    MAX_STEP_ATTEMPTS,
    FailureKind,
    Plan,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RetryPolicy,
    StepEffect,
    StepError,
    StepOutcome,
    VerificationMethod,
    VerificationPolicy,
    VerificationResult,
    VerificationSpec,
    VerificationStatus,
    WorkflowResult,
)
from novacontrol.planning.recovery import (
    RecoveryAction,
    RecoveryAdvisor,
    RecoveryDecision,
    repair_parameters,
)
from novacontrol.planning.runtime import PlanningModule
from novacontrol.planning.verification import (
    DeterministicVerifier,
    ProcessProbe,
    VerifyCallable,
)

__all__ = [
    "DEFAULT_MAX_CYCLES",
    "EFFECTS_NEEDING_CONFIRMATION",
    "EFFECTS_NEEDING_VERIFICATION",
    "EFFECTS_NEVER_RETRIED",
    "MAX_STEP_ATTEMPTS",
    "MAX_WAVE_SIZE",
    "AgentEvent",
    "AgentLoop",
    "AgentPhase",
    "AgentRun",
    "Clause",
    "ClauseKind",
    "ClarificationPolicy",
    "ConfirmationChannel",
    "DeterministicVerifier",
    "FailureKind",
    "Plan",
    "PlanCompiler",
    "PlanStatus",
    "PlanStep",
    "PlanStepStatus",
    "PlanningEngine",
    "PlanningModule",
    "ProcessProbe",
    "RecoveryAction",
    "RecoveryAdvisor",
    "RecoveryDecision",
    "RecoveryPolicy",
    "RetryPolicy",
    "StepContext",
    "StepEffect",
    "StepError",
    "StepHandler",
    "StepObserver",
    "StepOutcome",
    "StepTemplate",
    "VerificationMethod",
    "VerificationPolicy",
    "VerificationResult",
    "VerificationSpec",
    "VerificationStatus",
    "VerifyCallable",
    "WorkflowExecutor",
    "WorkflowResult",
    "repair_parameters",
]
