"""Reliability: verification, recovery, task control and the permission layer.

Phase 8's four subsystems, and the rule they share: nothing in NovaControl may
report success it did not observe, retry something it may not repeat, cancel
something halfway, or run something dangerous without asking.

Each one EXTENDS what already existed rather than replacing it:

    VerificationEngine  subclasses planning's DeterministicVerifier, adding
                        per-tool strategies and the check an action implies;
    RecoveryEngine      decides with planning's RecoveryAdvisor, adding
                        alternatives, confirmation and a hard attempt ceiling;
    TaskStateMachine    the formal states and legal transitions of a task;
    TaskController      pause / resume / cancel, honoured at safe checkpoints;
    PermissionManager   one risk/permission layer over core.security's
                        vocabulary and the tool catalog's declarations.

The names the specification asks for are here; the behaviour lives where it
always did, so every existing call site keeps working unchanged.
"""

from novacontrol.reliability.control import (
    CancelHook,
    TaskCancelled,
    TaskCommand,
    TaskController,
    TaskControlResult,
    parse_task_command,
)
from novacontrol.reliability.permissions import (
    CONFIRMING_RISK,
    PermissionDecision,
    PermissionDeclaration,
    PermissionManager,
    PermissionPolicy,
)
from novacontrol.reliability.recovery import (
    AlternativeStrategy,
    FailureAnalysis,
    RecoveryEngine,
    RecoveryKind,
    RecoveryPlan,
    RecoveryRun,
    VerifyStep,
)
from novacontrol.reliability.task_state import (
    ALLOWED_TRANSITIONS,
    PAUSABLE_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    StateObserver,
    TaskSnapshot,
    TaskState,
    TaskStateMachine,
)
from novacontrol.reliability.verification import (
    ToolCheck,
    ToolObjectVerifier,
    ToolVerifier,
    VerificationEngine,
    VerificationVerdict,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CONFIRMING_RISK",
    "PAUSABLE_STATES",
    "TERMINAL_STATES",
    "AlternativeStrategy",
    "CancelHook",
    "FailureAnalysis",
    "InvalidTransitionError",
    "PermissionDecision",
    "PermissionDeclaration",
    "PermissionManager",
    "PermissionPolicy",
    "RecoveryEngine",
    "RecoveryKind",
    "RecoveryPlan",
    "RecoveryRun",
    "StateObserver",
    "TaskCancelled",
    "TaskCommand",
    "TaskControlResult",
    "TaskController",
    "TaskSnapshot",
    "TaskState",
    "TaskStateMachine",
    "ToolCheck",
    "ToolObjectVerifier",
    "ToolVerifier",
    "VerificationEngine",
    "VerificationVerdict",
    "VerifyStep",
    "parse_task_command",
]
