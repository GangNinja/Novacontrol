"""Multi-agent subsystem.

Two generations of agents live here, and the second is built on the first
rather than beside it:

* the Phase-1 *placeholder* agents (:func:`build_default_agents`) — deterministic
  role stubs that answer immediately, kept because a build with no model, no
  workspace and no credentials must still be able to route a task;
* the Phase-12 *specialists* (:class:`DeveloperAgent`, :class:`ResearchAgent`)
  which run the shared :class:`SpecialistPipeline`
  (NLU → Context → Decision → Planner → Tool Selection → Execution →
  Verification → Recovery) and are registered in the same
  :class:`AgentRegistry`, so the coordinator, the agent event bus and the API
  reach them with no second dispatch mechanism.
"""

from novacontrol.agents.base import SpecializedAgent, build_default_agents
from novacontrol.agents.bus import AgentMessageBus
from novacontrol.agents.coordinator import CoordinatorAgent
from novacontrol.agents.developer import (
    CodeSearchResult,
    CommandResult,
    DeveloperAgent,
    SubprocessRunner,
    TestReport,
    diagnose_failure,
    parse_test_output,
    search_code,
)
from novacontrol.agents.models import (
    AgentMessage,
    AgentResponse,
    AgentRole,
    AgentTask,
    AgentTaskStatus,
)
from novacontrol.agents.pipeline import (
    STAGE_ORDER,
    AgentRun,
    PipelineStep,
    RecoveryPlan,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StageName,
    StageRecord,
    StageStatus,
    StepOutcome,
    StepStatus,
    ToolGrant,
)
from novacontrol.agents.registry import AgentRegistry, RunnableAgent
from novacontrol.agents.research import (
    Citation,
    Claim,
    ClaimKind,
    Conflict,
    ResearchAgent,
    detect_conflicts,
)
from novacontrol.agents.runtime import AgentModule

__all__ = [
    "STAGE_ORDER",
    "AgentMessage",
    "AgentMessageBus",
    "AgentModule",
    "AgentRegistry",
    "AgentResponse",
    "AgentRole",
    "AgentRun",
    "AgentTask",
    "AgentTaskStatus",
    "Citation",
    "Claim",
    "ClaimKind",
    "CodeSearchResult",
    "CommandResult",
    "Conflict",
    "CoordinatorAgent",
    "DeveloperAgent",
    "PipelineStep",
    "RecoveryPlan",
    "ResearchAgent",
    "RunnableAgent",
    "SpecialistAgent",
    "SpecialistDecision",
    "SpecialistPipeline",
    "SpecializedAgent",
    "StageName",
    "StageRecord",
    "StageStatus",
    "StepOutcome",
    "StepStatus",
    "SubprocessRunner",
    "TestReport",
    "ToolGrant",
    "build_default_agents",
    "detect_conflicts",
    "diagnose_failure",
    "parse_test_output",
    "search_code",
]
