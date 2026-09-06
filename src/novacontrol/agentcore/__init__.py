"""Agentic core: orchestrator loop over NovaControl's existing controllers.

This package implements the agentic architecture on TOP of the existing
automation, vision, research, and memory modules — it never replaces them:

    Task Interpreter -> Planner -> Perception -> Action Engine -> Verifier
        -> Recovery Engine -> Application Knowledge -> Evaluation

Every component is independently instantiable and testable; the only entry
point most callers need is `AgenticOrchestrator.run_task(...)`.
"""

from novacontrol.agentcore.actions import ActionEngine, ActionOutcome
from novacontrol.agentcore.evaluation import EvaluationLedger
from novacontrol.agentcore.interpreter import InterpretedGoal, TaskInterpreter
from novacontrol.agentcore.knowledge import (
    ApplicationKnowledgeGraph,
    ApplicationKnowledgeNode,
    WorkflowKnowledge,
)
from novacontrol.agentcore.orchestrator import AgenticOrchestrator, AgentTaskState
from novacontrol.agentcore.ui_state import UiElement, UiState
from novacontrol.agentcore.perception import UiPerceptionEngine
from novacontrol.agentcore.planner import AdaptivePlanner, AgentPlan, AgentPlanStep
from novacontrol.agentcore.recovery import RecoveryEngine, RecoveryOutcome, RecoveryStrategy
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus, Verifier

__all__ = [
    "ActionEngine",
    "ActionOutcome",
    "AdaptivePlanner",
    "AgentPlan",
    "AgentPlanStep",
    "AgentTaskState",
    "AgenticOrchestrator",
    "ApplicationKnowledgeGraph",
    "ApplicationKnowledgeNode",
    "EvaluationLedger",
    "InterpretedGoal",
    "RecoveryEngine",
    "RecoveryOutcome",
    "RecoveryStrategy",
    "TaskInterpreter",
    "UiPerceptionEngine",
    "UiState",
    "UiElement",
    "VerificationResult",
    "VerificationStatus",
    "Verifier",
    "WorkflowKnowledge",
]
