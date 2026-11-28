"""The shared specialist pipeline (Phase 12).

Phase 12 asks for specialized agents and forbids a second agent framework, so
this module is the *only* loop a specialist ever runs:

    NLU -> Context -> Decision -> Planner -> Tool Selection
        -> Execution -> Verification -> Recovery

Every specialist is a :class:`SpecialistAgent` — a set of stage hooks — and
:class:`SpecialistPipeline` is the stage runner. A specialist therefore cannot
skip verification or invent its own recovery: the eight stages are recorded on
every run, in order, whether or not a stage had anything to do (a stage with
nothing to do is recorded ``SKIPPED`` with the reason, never omitted).

It is built out of the components that already exist rather than new ones:

* **NLU** is :class:`~novacontrol.agentcore.interpreter.TaskInterpreter`;
* **Context** is the Phase 11 knowledge engine's ``context_for`` plus whatever
  local facts the specialist can read (the developer reads its project context,
  the researcher reads its own index);
* **Decision** is the specialist's own routing of the request to one of the
  things it is allowed to do — the coordinator's role routing happened before
  the run and is not repeated here;
* **Planner** produces :class:`PipelineStep` objects, not macros;
* **Tool Selection** is the centralized
  :class:`~novacontrol.reliability.permissions.PermissionManager` plus the live
  tool registry, so a step whose tool is not registered, or whose risk nobody
  approved, never reaches execution;
* **Verification** reports the Phase 8 vocabulary
  (:class:`~novacontrol.agentcore.verifier.VerificationResult`) — and a step
  that was not verified is never counted as success;
* **Recovery** reports the Phase 8 strategy vocabulary
  (:class:`~novacontrol.agentcore.recovery.RecoveryStrategy`).

Each stage publishes the typed lifecycle events ``core/events.py`` already
defines (``intent.detected``, ``context.resolved``, ``decision.created``,
``plan.created``, ``tool.*``, ``verification.*``, ``recovery.*``,
``task.*``), so a run is observable without any new event vocabulary.

Failures are contained by design: a stage that raises is recorded FAILED and
the run ends with a report, so a broken specialist can never take its caller
down. A stage whose failure is survivable (a missing knowledge index) degrades
instead — it is recorded ``SKIPPED`` with the reason, and the caller can see
exactly which stage was thin.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from novacontrol.agentcore.interpreter import InterpretedGoal, TaskInterpreter
from novacontrol.agentcore.recovery import RecoveryStrategy
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus
from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.core.events import EventBus, EventType
from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.reliability.permissions import PermissionDeclaration, PermissionManager

__all__ = [
    "AgentRun",
    "PipelineStep",
    "RecoveryPlan",
    "SKILL_TOOL",
    "STAGE_ORDER",
    "SkillProvider",
    "SpecialistAgent",
    "SpecialistDecision",
    "SpecialistPipeline",
    "StageName",
    "StageRecord",
    "StageStatus",
    "StepOutcome",
    "StepStatus",
    "ToolGrant",
]

#: The capability name an in-pipeline skill step runs through. It is not a
#: model-selectable tool and is never registered in the tool registry: it names
#: the Phase 31 skill library that the pipeline consults and executes through,
#: so a specialist gains skills WITHOUT a second registry or executor.
SKILL_TOOL = "skill.execute"


class StageName(StrEnum):
    """The eight stages every specialist run walks, in order."""

    NLU = "nlu"
    CONTEXT = "context"
    DECISION = "decision"
    PLANNER = "planner"
    TOOL_SELECTION = "tool_selection"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    RECOVERY = "recovery"


#: The stage order, spelled out rather than derived from the enum: this tuple is
#: the contract ("all agents use these stages"), so it is written where a reader
#: can check it against the specification.
STAGE_ORDER: tuple[StageName, ...] = (
    StageName.NLU,
    StageName.CONTEXT,
    StageName.DECISION,
    StageName.PLANNER,
    StageName.TOOL_SELECTION,
    StageName.EXECUTION,
    StageName.VERIFICATION,
    StageName.RECOVERY,
)


class StageStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    RECOVERED = "recovered"
    DENIED = "denied"
    FAILED = "failed"


#: Worst-first severity, used when two records land on the same stage (an
#: execution that failed and then recovered reports the whole story, not just
#: the happy ending).
_STATUS_SEVERITY: dict[StageStatus, int] = {
    StageStatus.OK: 0,
    StageStatus.SKIPPED: 1,
    StageStatus.RECOVERED: 2,
    StageStatus.DENIED: 3,
    StageStatus.FAILED: 4,
}


class StepStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"


#: What one step's ending says about the stage that ran it. One table rather than
#: nested conditionals because three different endings must stay tellable
#: apart: a step that did not apply (SKIPPED) and a step that was refused
#: (DENIED) are not failures, and a stage table that folds them into FAILED
#: hides exactly what a reader opens it to find.
_STAGE_FOR_OUTCOME: dict[StepStatus, StageStatus] = {
    StepStatus.COMPLETED: StageStatus.OK,
    StepStatus.SKIPPED: StageStatus.SKIPPED,
    StepStatus.DENIED: StageStatus.DENIED,
    StepStatus.FAILED: StageStatus.FAILED,
}


@dataclass(frozen=True, slots=True)
class StageRecord:
    """One stage's entry in a run: what it did, and whether it worked."""

    stage: StageName
    status: StageStatus
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass(frozen=True, slots=True)
class PipelineStep:
    """One planned unit of work, with how it will be verified.

    ``action`` is the specialist's own verb (``inspect_repository``,
    ``run_tests``, ``search_web``); ``tool`` names the capability it runs
    through, which is what the permission layer and the tool registry are asked
    about. ``expected`` is the statement verification checks, so the planner
    cannot produce a step nobody can verify.
    """

    action: str
    description: str = ""
    tool: str = ""
    target: str = ""
    expected: str = ""
    permission: PermissionScope | None = None
    risk: RiskLevel | None = None
    requires_approval: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("A pipeline step needs an action.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "description": self.description,
            "tool": self.tool,
            "target": self.target,
            "expected": self.expected,
            "permission": self.permission.value if self.permission else None,
            "risk": self.risk.value if self.risk else None,
            "requires_approval": self.requires_approval,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What executing one step produced."""

    step_id: str
    action: str
    status: StepStatus
    detail: str = ""
    output: Mapping[str, Any] = field(default_factory=dict)
    tool: str = ""

    @property
    def ok(self) -> bool:
        return self.status is StepStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "status": self.status.value,
            "detail": self.detail,
            "output": dict(self.output),
            "tool": self.tool,
        }


@dataclass(frozen=True, slots=True)
class ToolGrant:
    """The permission layer's verdict on one step, before anything ran."""

    step_id: str
    tool: str
    allowed: bool
    reason: str = ""
    risk: RiskLevel = RiskLevel.LOW
    source: str = "default"
    approval_id: str | None = None
    permissions: tuple[PermissionScope, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "allowed": self.allowed,
            "reason": self.reason,
            "risk": self.risk.value,
            "source": self.source,
            "approval_id": self.approval_id,
            "permissions": [scope.value for scope in self.permissions],
        }


@dataclass(frozen=True, slots=True)
class SpecialistDecision:
    """The specialist's answer to "what am I being asked to do?"."""

    action: str
    reason: str = ""
    confidence: float = 0.0
    requires_approval: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "requires_approval": self.requires_approval,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What to do about a step that failed, in the Phase 8 strategy vocabulary."""

    diagnosis: str
    strategy: RecoveryStrategy = RecoveryStrategy.ABORT
    steps: tuple[PipelineStep, ...] = ()
    text: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis": self.diagnosis,
            "strategy": self.strategy.value,
            "steps": [step.to_dict() for step in self.steps],
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class AgentRun:
    """One specialist run: the plan, the stages, the evidence, the verdict.

    ``stages`` always holds at most one record per stage name, in
    :data:`STAGE_ORDER`, so "which stage failed?" is answerable at a glance and
    a run can never silently skip a stage.
    """

    agent: str
    role: AgentRole
    task_id: str
    goal: str
    id: str = field(default_factory=lambda: uuid4().hex)
    status: AgentTaskStatus = AgentTaskStatus.PENDING
    interpretation: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    context_text: str = ""
    decision: dict[str, Any] = field(default_factory=dict)
    plan: tuple[dict[str, Any], ...] = ()
    stages: list[StageRecord] = field(default_factory=list)
    grants: list[ToolGrant] = field(default_factory=list)
    outcomes: list[StepOutcome] = field(default_factory=list)
    verifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def record(
        self, stage: StageName, status: StageStatus, detail: str = "", **data: Any
    ) -> StageRecord:
        """Add one stage entry, merging into an existing one for the same stage.

        Merging (rather than appending) is what keeps ``stages`` one-per-stage
        while still telling the truth about a stage that both failed and
        recovered: the status escalates to the worst seen and the details are
        kept side by side.
        """
        for index, existing in enumerate(self.stages):
            if existing.stage is not stage:
                continue
            merged_status = (
                status
                if _STATUS_SEVERITY[status] > _STATUS_SEVERITY[existing.status]
                else existing.status
            )
            detail_text = existing.detail
            if detail and detail not in detail_text:
                detail_text = f"{detail_text}; {detail}" if detail_text else detail
            merged = StageRecord(
                stage=stage,
                status=merged_status,
                detail=detail_text,
                data={**existing.data, **data},
            )
            self.stages[index] = merged
            return merged
        record = StageRecord(stage=stage, status=status, detail=detail, data=dict(data))
        self.stages.append(record)
        return record

    def stage(self, name: StageName) -> StageRecord | None:
        return next((record for record in self.stages if record.stage is name), None)

    @property
    def verified(self) -> bool:
        """True when at least one step was verified as passing."""
        return any(
            entry.get("status") == VerificationStatus.PASS.value
            for entry in self.verifications.values()
        )

    @property
    def denied(self) -> tuple[ToolGrant, ...]:
        return tuple(grant for grant in self.grants if not grant.allowed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "role": self.role.value,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "stages": [record.to_dict() for record in self.stages],
            "interpretation": self.interpretation,
            "context": self.context,
            "context_tokens": len(self.context_text.split()),
            "decision": self.decision,
            "plan": list(self.plan),
            "tools": [grant.to_dict() for grant in self.grants],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "verifications": self.verifications,
            "recoveries": self.recoveries,
            "events": list(self.events),
            "errors": list(self.errors),
            "verified": self.verified,
            "report": self.report,
            "summary": self.summary,
        }


class KnowledgeProvider(Protocol):
    """The Phase 11 knowledge engine, as this module needs it."""

    async def context_for(self, question: str, *, active_task: str = "") -> Any: ...


class ToolCatalog(Protocol):
    """The tool registry, as this module needs it."""

    def list(self) -> Sequence[Any]: ...


class SkillProvider(Protocol):
    """The Phase 31 skill library, as an in-pipeline selection needs it.

    Deliberately two methods and no state: the pipeline asks which validated
    procedure matches a goal and, when one does, asks for it to run. Both are
    answered by the application's ONE ``SkillService``, which keeps its own
    catalog, evidence, permission checks and execution routing. A provider that
    cannot answer returns nothing, and the run reports "nothing to do" exactly
    as it did before this seam existed.
    """

    def select(
        self,
        goal: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        skill: str = "",
        limit: int = 5,
    ) -> Sequence[Mapping[str, Any]]: ...

    async def execute(
        self,
        skill_id: str,
        *,
        goal: str,
        inputs: Mapping[str, Any],
        version: int | None = None,
    ) -> Mapping[str, Any]: ...


class SpecialistAgent(ABC):
    """A specialized agent expressed as pipeline stage hooks.

    Subclasses state what they are asked to do (:meth:`decide`), what doing it
    takes (:meth:`plan`), and how to do one step (:meth:`execute`). Everything
    else — interpretation, context assembly, permission checks, the
    execute/verify/recover loop, event publication — is the pipeline's, which is
    why two specialists cannot drift apart in how careful they are.
    """

    stages: tuple[StageName, ...] = STAGE_ORDER

    def __init__(
        self,
        name: str,
        role: AgentRole,
        description: str = "",
        *,
        pipeline: SpecialistPipeline | None = None,
    ) -> None:
        self._name = name
        self.role = role
        self.description = description
        self.pipeline = pipeline or SpecialistPipeline()

    @property
    def name(self) -> str:
        return self._name

    # -- stage hooks ----------------------------------------------------------

    def declarations(self) -> Mapping[str, PermissionDeclaration]:
        """How this agent's actions behave, declared to the permission layer.

        Stating the risk is what stops the layer from having to guess it from a
        verb: ``run_tests`` is not a read just because it looks like one. The
        keys are capability names, which is also what the tool-selection stage
        validates a step's tool against, so the set of things this agent may run
        is declared in exactly one place.
        """
        return {}

    def tools(self) -> tuple[str, ...]:
        """The capability names this agent may select, by default its declared ones."""
        return tuple(self.declarations())

    def availability(self) -> Mapping[str, Any]:
        """Whether this agent can accept work right now, and why not.

        Registration means the object exists, not that its required local
        resource exists. A filesystem agent whose root disappeared is present
        but unavailable; reporting it as available is a false claim the
        coordinator would then act on.
        """
        return {"available": True, "reason": ""}

    @abstractmethod
    def clone(self, pipeline: SpecialistPipeline) -> SpecialistAgent:
        """This agent bound to another pipeline.

        A caller that authorizes one run (an explicit approval, a narrower
        policy) must not loosen the build's pipeline for every other caller, so
        authorization is a per-run pipeline the agent is cloned onto — never a
        flag flipped on shared state.
        """

    def context_query(self, task: AgentTask, interpretation: InterpretedGoal) -> str:
        """What to ask the knowledge engine for. Defaults to the goal itself."""
        return task.goal

    def local_context(self, task: AgentTask, interpretation: InterpretedGoal) -> Mapping[str, Any]:
        """Facts this agent can read cheaply that the knowledge engine cannot.

        The developer's project context and the researcher's local index live
        here; a hook that fails costs its own facts and nothing else.
        """
        return {}

    @abstractmethod
    def decide(
        self,
        task: AgentTask,
        interpretation: InterpretedGoal,
        context: Mapping[str, Any],
    ) -> SpecialistDecision:
        """Route the request to one of the things this agent is allowed to do."""

    @abstractmethod
    def plan(
        self,
        task: AgentTask,
        interpretation: InterpretedGoal,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
    ) -> tuple[PipelineStep, ...]:
        """Turn the decision into ordered, individually verifiable steps."""

    @abstractmethod
    async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        """Carry out one step. Returning is not success — verification decides."""

    def verify(self, step: PipelineStep, outcome: StepOutcome, run: AgentRun) -> VerificationResult:
        """Check a step against what it said success would look like.

        The default is deliberately conservative: a completed step that
        produced nothing observable is INCONCLUSIVE, never a pass, because
        "the call returned" is the exact confusion verification exists to
        prevent.
        """
        if not outcome.ok:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or step.description or step.action,
                reason=outcome.detail or f"{step.action} did not complete.",
                evidence={"status": outcome.status.value},
                confidence=0.8,
            )
        if not outcome.output:
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                expectation=step.expected or step.action,
                reason=f"{step.action} completed but produced nothing to check.",
                evidence={"status": outcome.status.value},
                confidence=0.0,
            )
        return VerificationResult(
            status=VerificationStatus.PASS,
            expectation=step.expected or step.action,
            reason=f"{step.action} produced the expected result.",
            evidence={"status": outcome.status.value},
            confidence=0.6,
        )

    def recover(
        self, step: PipelineStep, outcome: StepOutcome, attempt: int, run: AgentRun
    ) -> RecoveryPlan:
        """One retry, then stop.

        A failure is not terminal on the first attempt, and it is also not a
        licence to hammer: with no specialist knowledge of what went wrong, the
        honest strategies are RETRY (once) and ABORT.
        """
        if attempt <= 1:
            return RecoveryPlan(
                diagnosis=outcome.detail or f"{step.action} failed.",
                strategy=RecoveryStrategy.RETRY,
                steps=(step,),
                text=f"Re-observe, then retry: {step.action}.",
                confidence=0.5,
            )
        return RecoveryPlan(
            diagnosis=outcome.detail or f"{step.action} failed repeatedly.",
            strategy=RecoveryStrategy.ABORT,
            text=f"Stop after {attempt} attempts and report the failure.",
            confidence=1.0,
        )

    def report(self, run: AgentRun) -> dict[str, Any]:
        """The specialist's own payload for the response. Optional."""
        return {}

    def summarize(self, run: AgentRun) -> str:
        """One line for the chat surface: what happened, and what was proven."""
        ok = run.status is AgentTaskStatus.COMPLETED
        head = f"{self.name} {'completed' if ok else 'did not complete'} the task."
        done = [outcome.action for outcome in run.outcomes if outcome.ok]
        detail = f" Steps: {', '.join(done)}." if done else ""
        denied = [grant.tool for grant in run.denied]
        blocked = f" Not authorized: {', '.join(sorted(set(denied)))}." if denied else ""
        return f"{head}{detail}{blocked}".strip()

    # -- the entry point ------------------------------------------------------

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        """Run the shared pipeline and report what it found.

        Registered with the ordinary :class:`~novacontrol.agents.registry.AgentRegistry`,
        so a specialist is reachable through the coordinator, the agent event
        bus and the API without a second dispatch mechanism.
        """
        run = await self.pipeline.run(self, task)
        payload = run.to_dict()
        payload["agent"] = self.name
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=run.status,
            content=run.summary,
            metadata=payload,
        )

    async def run_task(self, goal: str, *, metadata: Mapping[str, Any] | None = None) -> AgentRun:
        """Run the pipeline for a bare goal — the direct-call surface."""
        task = AgentTask(goal=goal, role=self.role, metadata=dict(metadata or {}))
        return await self.pipeline.run(self, task)

    def declares(self) -> dict[str, Any]:
        """What this agent is and what it may do — for discovery and docs."""
        return {
            "agent": self.name,
            "role": self.role.value,
            "description": self.description,
            "stages": [stage.value for stage in self.stages],
            "declarations": {
                name: declaration.to_dict()
                for name, declaration in sorted(self.declarations().items())
            },
        }


class SpecialistPipeline:
    """Runs the eight specialist stages over any :class:`SpecialistAgent`."""

    def __init__(
        self,
        *,
        interpreter: TaskInterpreter | None = None,
        permissions: PermissionManager | None = None,
        approvals: ApprovalGateway | None = None,
        tools: ToolCatalog | None = None,
        knowledge: KnowledgeProvider | None = None,
        event_bus: EventBus | None = None,
        skills: SkillProvider | None = None,
        max_recovery_attempts: int = 2,
    ) -> None:
        self._interpreter = interpreter or TaskInterpreter()
        self._permissions = permissions or PermissionManager()
        # Deny-by-default unless the composition root wires the build's real
        # approval flow: a specialist must never be more permissive than the
        # system it runs inside.
        self._approvals = approvals or DenyByDefaultApprovalGateway()
        self._tools = tools
        self._knowledge = knowledge
        self._event_bus = event_bus
        #: Optional on purpose: a pipeline built without the skill library runs
        #: exactly as it did before Phase 31's integration, and every existing
        #: caller keeps its behaviour.
        self._skills = skills
        self._max_recovery_attempts = max(0, int(max_recovery_attempts))

    @property
    def permissions(self) -> PermissionManager:
        return self._permissions

    # -- the run --------------------------------------------------------------

    async def run(
        self,
        agent: SpecialistAgent,
        task: AgentTask,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> AgentRun:
        """Walk all eight stages, and never raise at the caller."""
        run = AgentRun(
            agent=agent.name,
            role=agent.role,
            task_id=task.id,
            goal=task.goal,
            context=dict(context or {}),
        )
        # The task's declared metadata is part of the run's context, not a
        # separate channel: an agent's execute hook only receives the run, and a
        # verifier that cannot see the evidence it was asked to check would have
        # to guess. The collaboration layer puts each subtask's declared inputs
        # here, so no hidden global context is consulted.
        run.context.setdefault("task_metadata", dict(task.metadata))
        self._declare(agent)
        await self._emit(run, EventType.TASK_STARTED, {"task_id": run.id, "agent": agent.name})
        try:
            interpretation = await self._stage_nlu(agent, task, run)
            if interpretation is not None:
                context_block = await self._stage_context(agent, task, interpretation, run)
                decision = await self._stage_decision(
                    agent, task, interpretation, context_block, run
                )
                if decision is not None:
                    steps = await self._stage_planner(
                        agent, task, interpretation, decision, context_block, run
                    )
                    if steps:
                        await self._stage_tool_selection(agent, steps, run)
                        await self._stage_execution(agent, steps, run)
        except Exception as exc:  # a broken specialist must not break its caller
            run.record(
                StageName.EXECUTION,
                StageStatus.FAILED,
                f"unhandled {type(exc).__name__}: {exc}",
            )
            run.errors.append(f"{type(exc).__name__}: {exc}")
            run.status = AgentTaskStatus.FAILED
        return await self._finish(agent, run)

    # -- stages ---------------------------------------------------------------

    async def _stage_nlu(
        self, agent: SpecialistAgent, task: AgentTask, run: AgentRun
    ) -> InterpretedGoal | None:
        try:
            interpretation = await self._interpreter.interpret(task.goal)
        except Exception as exc:
            run.record(
                StageName.NLU,
                StageStatus.FAILED,
                f"the request could not be interpreted: {type(exc).__name__}: {exc}",
            )
            run.status = AgentTaskStatus.FAILED
            return None
        run.interpretation = interpretation.to_dict()
        run.record(
            StageName.NLU,
            StageStatus.OK,
            f"{len(interpretation.subtasks)} subtask(s), tools: "
            + (", ".join(interpretation.required_tools) or "none"),
            **{"interpretation": run.interpretation},
        )
        await self._emit(
            run,
            EventType.INTENT_DETECTED,
            {
                "intent": agent.role.value,
                "subtasks": list(interpretation.subtasks),
                "required_tools": list(interpretation.required_tools),
            },
        )
        return interpretation

    async def _stage_context(
        self,
        agent: SpecialistAgent,
        task: AgentTask,
        interpretation: InterpretedGoal,
        run: AgentRun,
    ) -> dict[str, Any]:
        block: dict[str, Any] = {}
        try:
            block.update(dict(agent.local_context(task, interpretation)))
        except Exception as exc:
            # Local facts are a convenience: losing them costs detail, not the
            # run, so this is recorded as a degraded stage rather than a failure.
            run.errors.append(f"local context: {type(exc).__name__}: {exc}")
        # Phase 32: the application may attach a NARROW, already-filtered
        # personal-knowledge view to the task. It is context, not a permission
        # grant, and it is kept separate from the knowledge engine's block so
        # provenance is visible to the agent that receives it.
        personal_rows = (task.metadata or {}).get("personal_knowledge")
        if isinstance(personal_rows, (list, tuple)):
            lines = []
            for row in personal_rows[:8]:
                if not isinstance(row, Mapping):
                    continue
                record = row.get("record")
                record = record if isinstance(record, Mapping) else {}
                content = str(record.get("content", "") or "").strip()
                if not content:
                    continue
                confidence = float(record.get("confidence", 0.0) or 0.0)
                category = str(record.get("category", "fact") or "fact")
                lines.append(f"- [{category}; {confidence:.2f}] {content}")
            if lines:
                block["personal_knowledge"] = personal_rows
                run.context_text = "\n".join(
                    ["Relevant permitted personal knowledge:", *lines]
                )
        query = agent.context_query(task, interpretation) or task.goal
        strategy = "local-only"
        if self._knowledge is not None:
            try:
                knowledge = await self._knowledge.context_for(query, active_task=task.goal)
                text = str(getattr(knowledge, "text", "") or "")
                if text:
                    run.context_text = "\n\n".join(
                        part for part in (run.context_text, text) if part
                    )
                    block["knowledge_tokens"] = int(getattr(knowledge, "tokens", 0) or 0)
                    backend = str(getattr(knowledge, "embedding_backend", "") or "lexical")
                    strategy = f"{backend} retrieval"
            except Exception as exc:
                run.errors.append(f"knowledge context: {type(exc).__name__}: {exc}")
                run.record(
                    StageName.CONTEXT,
                    StageStatus.SKIPPED,
                    f"knowledge retrieval was unavailable: {type(exc).__name__}: {exc}",
                )
                await self._emit(run, EventType.CONTEXT_RESOLVED, {"strategy": "unavailable"})
                return block
        block.setdefault("query", query)
        run.context.update(block)
        run.record(
            StageName.CONTEXT,
            StageStatus.OK,
            f"{strategy}; {len(run.context_text.split())} word(s) of retrieved context",
            **{key: value for key, value in block.items() if _jsonable(value)},
        )
        await self._emit(run, EventType.CONTEXT_RESOLVED, {"strategy": strategy})
        return block

    async def _stage_decision(
        self,
        agent: SpecialistAgent,
        task: AgentTask,
        interpretation: InterpretedGoal,
        context: Mapping[str, Any],
        run: AgentRun,
    ) -> SpecialistDecision | None:
        try:
            decision = agent.decide(task, interpretation, context)
        except Exception as exc:
            run.record(
                StageName.DECISION,
                StageStatus.FAILED,
                f"the request could not be routed: {type(exc).__name__}: {exc}",
            )
            run.status = AgentTaskStatus.FAILED
            return None
        run.decision = decision.to_dict()
        run.record(
            StageName.DECISION,
            StageStatus.OK,
            f"{decision.action} (confidence {decision.confidence:.2f}): {decision.reason}",
            **{"decision": run.decision},
        )
        await self._emit(
            run,
            EventType.DECISION_CREATED,
            {
                "route": agent.role.value,
                "decision_type": decision.action,
                "reason": decision.reason,
                "confidence": decision.confidence,
            },
        )
        return decision

    async def _stage_planner(
        self,
        agent: SpecialistAgent,
        task: AgentTask,
        interpretation: InterpretedGoal,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
        run: AgentRun,
    ) -> tuple[PipelineStep, ...]:
        try:
            steps = tuple(agent.plan(task, interpretation, decision, context))
        except Exception as exc:
            run.record(
                StageName.PLANNER,
                StageStatus.FAILED,
                f"planning failed: {type(exc).__name__}: {exc}",
            )
            run.status = AgentTaskStatus.FAILED
            return ()
        requested = str(
            task.metadata.get("skill") or task.metadata.get("use_skill") or ""
        ).strip()
        if requested:
            # An operator who NAMES a skill is asking for that procedure, not for
            # the specialist's own. Every shipped specialist returns a plan, so
            # without this branch an explicit request was silently ignored and the
            # library was only ever consulted when a specialist had nothing to do.
            chosen = self._select_skill(task, run)
            if chosen is not None:
                steps = (chosen,)
        if not steps:
            # The specialist has no procedure of its own for this goal. That is
            # the one place an existing plan is never at risk, and exactly where
            # a validated skill is worth selecting: the run can now do something
            # it previously reported as nothing to do.
            selected = self._select_skill(task, run)
            if selected is not None:
                steps = (selected,)
            else:
                run.record(
                    StageName.PLANNER,
                    StageStatus.SKIPPED,
                    f"nothing to do for {decision.action}",
                )
                run.status = AgentTaskStatus.FAILED
                return ()
        run.plan = tuple(step.to_dict() for step in steps)
        run.record(
            StageName.PLANNER,
            StageStatus.OK,
            f"{len(steps)} step(s): " + " -> ".join(step.action for step in steps),
            **{"steps": list(run.plan)},
        )
        await self._emit(
            run,
            EventType.PLAN_CREATED,
            {"goal": task.goal, "steps": [step.action for step in steps]},
        )
        return steps

    async def _stage_tool_selection(
        self, agent: SpecialistAgent, steps: tuple[PipelineStep, ...], run: AgentRun
    ) -> tuple[ToolGrant, ...]:
        available = self._available_tools(agent)
        grants = [await self._authorize(step, available) for step in steps]
        run.grants.extend(grants)
        denied = [grant for grant in grants if not grant.allowed]
        unknown = [grant for grant in denied if grant.source == "catalog"]
        status = StageStatus.OK
        detail = f"{len(grants)} tool(s) selected"
        if denied:
            status = StageStatus.DENIED
            detail = "; ".join(f"{grant.tool}: {grant.reason}" for grant in denied)
            if unknown:
                status = StageStatus.FAILED
        run.record(StageName.TOOL_SELECTION, status, detail)
        for grant in grants:
            await self._emit(
                run,
                EventType.TOOL_SELECTED,
                {
                    "tool": grant.tool,
                    "allowed": grant.allowed,
                    "risk": grant.risk.value,
                    "selection_source": grant.source,
                },
            )
        return tuple(grants)

    async def _stage_execution(
        self, agent: SpecialistAgent, steps: tuple[PipelineStep, ...], run: AgentRun
    ) -> None:
        grants = {grant.step_id: grant for grant in run.grants}
        queue: deque[PipelineStep] = deque(steps)
        attempts: dict[str, int] = {}
        executions = 0
        while queue:
            step = queue.popleft()
            executions += 1
            if executions > self._max_executions(len(steps)):
                run.record(
                    StageName.EXECUTION,
                    StageStatus.FAILED,
                    "execution budget exhausted while recovering; stopping",
                )
                break
            grant = grants.get(step.id)
            if grant is not None and not grant.allowed:
                # A refusal ends the step here: it is never executed, never
                # verified and never retried (the retry would reuse the very
                # grant that denied it, and recovery can never widen what was
                # authorized). The run reports it through `denied` and cannot
                # report success while a step was refused.
                run.outcomes.append(
                    StepOutcome(
                        step_id=step.id,
                        action=step.action,
                        status=StepStatus.DENIED,
                        detail=grant.reason,
                        tool=step.tool,
                    )
                )
                # The stage records the refusal rather than being left for the
                # end-of-run fill: this step was considered and turned down,
                # which is not the same fact as "the run ended before this
                # stage" — and the refusal is the fact a reader is after.
                run.record(
                    StageName.EXECUTION,
                    StageStatus.DENIED,
                    f"{step.action}: {grant.reason}",
                    **{"outcomes": [entry.to_dict() for entry in run.outcomes]},
                )
                continue
            await self._emit(
                run, EventType.TOOL_STARTED, {"tool": step.tool or step.action, "step_id": step.id}
            )
            try:
                if step.tool == SKILL_TOOL and self._skills is not None:
                    outcome = await self._run_skill_step(step, run)
                else:
                    outcome = await agent.execute(step, run)
            except Exception as exc:
                outcome = StepOutcome(
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.FAILED,
                    detail=f"{type(exc).__name__}: {exc}",
                    tool=step.tool,
                )
            run.outcomes.append(outcome)
            await self._emit(
                run,
                EventType.TOOL_COMPLETED if outcome.ok else EventType.TOOL_FAILED,
                (
                    {
                        "tool": step.tool or step.action,
                        "status": outcome.status.value,
                        "step_id": step.id,
                    }
                    if outcome.ok
                    else {
                        "tool": step.tool or step.action,
                        "error": outcome.detail or outcome.status.value,
                        "step_id": step.id,
                    }
                ),
            )
            run.record(
                StageName.EXECUTION,
                _STAGE_FOR_OUTCOME[outcome.status],
                f"{step.action}: {outcome.detail or outcome.status.value}",
                **{"outcomes": [entry.to_dict() for entry in run.outcomes]},
            )
            # A failed step is re-queued by recovery (or not, when the strategy
            # is ABORT); the re-queued step reuses its grant and its permission
            # verdict, so recovery can never widen what was authorized.
            await self._verify_and_recover(agent, step, outcome, attempts, run, queue)

    async def _verify_and_recover(
        self,
        agent: SpecialistAgent,
        step: PipelineStep,
        outcome: StepOutcome,
        attempts: dict[str, int],
        run: AgentRun,
        queue: deque[PipelineStep],
    ) -> RecoveryPlan | None:
        await self._emit(run, EventType.VERIFICATION_STARTED, {"step_id": step.id})
        if outcome.status is StepStatus.SKIPPED:
            # There is nothing to verify about a step that never ran, so the
            # agent's own `verify` is not asked and the stage says SKIPPED. The
            # alternative — a FAILED verification for work nobody attempted —
            # is the one reading of a run that hides whether the step failed or
            # simply did not apply, and it disagrees with recovery, which
            # already declines to retry a step that was not applicable.
            verification = VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                expectation=step.expected or step.action,
                reason=f"{step.action} did not run: {outcome.detail or 'not applicable'}",
                evidence={"status": outcome.status.value},
                confidence=0.0,
            )
        elif step.tool == SKILL_TOOL and self._skills is not None:
            # The skill library's own verified verdict is the verification. An
            # agent's default verifier would pass any step that produced output,
            # and a skill that ran without verifying did produce output.
            verification = self._verify_skill_step(step, outcome)
        else:
            try:
                verification = agent.verify(step, outcome, run)
            except Exception as exc:
                verification = VerificationResult(
                    status=VerificationStatus.INCONCLUSIVE,
                    expectation=step.expected or step.action,
                    reason=f"verification could not run: {type(exc).__name__}: {exc}",
                    confidence=0.0,
                )
        run.verifications[step.id] = verification.to_dict()
        run.record(
            StageName.VERIFICATION,
            (
                StageStatus.SKIPPED
                if outcome.status is StepStatus.SKIPPED
                else (
                    StageStatus.OK
                    if verification.status is VerificationStatus.PASS
                    else StageStatus.FAILED
                )
            ),
            f"{step.action}: {verification.status.value} — {verification.reason}",
            **{"verifications": dict(run.verifications)},
        )
        await self._emit(
            run,
            EventType.VERIFICATION_COMPLETED,
            {
                "step_id": step.id,
                "status": verification.status.value,
                "reason": verification.reason,
            },
        )
        # A refusal is a decision, not a transient fault: retrying it reuses a
        # verdict that will not change, and for a skill step it starts a SECOND
        # execution with its own approval checkpoint — one refusal became
        # several pending approvals for one step.
        if outcome.status is StepStatus.DENIED:
            run.record(
                StageName.RECOVERY,
                StageStatus.SKIPPED,
                f"{step.action}: refused, not retried",
            )
            return None
        # A step that never ran is INCONCLUSIVE, so it never reaches recovery:
        # retrying it cannot change the fact that there was nothing to do.
        if verification.status is not VerificationStatus.FAIL:
            return None
        if attempts.get(step.action, 0) >= self._max_recovery_attempts:
            run.record(
                StageName.RECOVERY,
                StageStatus.FAILED,
                f"{step.action}: retry budget exhausted",
            )
            return None
        attempts[step.action] = attempts.get(step.action, 0) + 1
        await self._emit(run, EventType.RECOVERY_STARTED, {"step_id": step.id})
        try:
            plan = agent.recover(step, outcome, attempts[step.action], run)
        except Exception as exc:
            plan = RecoveryPlan(
                diagnosis=f"recovery could not be planned: {type(exc).__name__}: {exc}",
                strategy=RecoveryStrategy.ABORT,
                confidence=1.0,
            )
        run.recoveries.append({"step_id": step.id, "action": step.action, **plan.to_dict()})
        run.record(
            StageName.RECOVERY,
            # ABORT is a decision NOT to recover, and saying so is the point:
            # a stage that reports RECOVERED for a step nobody retried would
            # hide exactly the failures a reader wants to find.
            (
                StageStatus.FAILED
                if plan.strategy is RecoveryStrategy.ABORT
                else StageStatus.RECOVERED
            ),
            f"{step.action}: {plan.strategy.value} — {plan.diagnosis}",
            **{"recoveries": list(run.recoveries)},
        )
        await self._emit(
            run,
            EventType.RECOVERY_COMPLETED,
            {"step_id": step.id, "outcome": plan.strategy.value, "diagnosis": plan.diagnosis},
        )
        if plan.strategy is not RecoveryStrategy.ABORT:
            for replacement in reversed(plan.steps):
                queue.appendleft(replacement)
        return plan

    # -- skills: Phase 31 procedures selected inside the pipeline --------------

    def _skill_inputs(self, task: AgentTask) -> dict[str, Any]:
        """The inputs a selected skill is bound with, from the task's metadata."""
        raw = task.metadata.get("skill_inputs")
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _select_skill(self, task: AgentTask, run: AgentRun) -> PipelineStep | None:
        """Choose a validated skill for a goal the specialist cannot plan itself.

        Selection is the library's: this asks for the matches and refuses the
        ones it can see are unusable — an unvalidated version, an incompatible
        environment, an unbound required input. It never guesses a skill from a
        name it recognises, and a goal nobody has a validated procedure for is
        reported as nothing to do rather than handed the closest-looking skill.
        """
        if self._skills is None:
            return None
        requested = str(
            task.metadata.get("skill") or task.metadata.get("use_skill") or ""
        ).strip()
        try:
            matches = self._skills.select(
                task.goal,
                inputs=self._skill_inputs(task),
                skill=requested,
                limit=5,
            )
        except Exception as exc:  # a library that cannot answer selects nothing
            run.errors.append(f"skill selection: {type(exc).__name__}: {exc}")
            return None
        for match in matches:
            row = (
                dict(match)
                if isinstance(match, Mapping)
                else (match.to_dict() if hasattr(match, "to_dict") else {})
            )
            if str(row.get("lifecycle", "") or "").lower() not in (
                "validated",
                "adapted",
            ):
                continue
            if row.get("compatible") is False:
                continue
            if row.get("missing_inputs") or row.get("missing_capabilities"):
                continue
            skill_id = str(row.get("skill_id") or row.get("id") or "").strip()
            if not skill_id:
                continue
            if not requested and float(row.get("score", 0.0) or 0.0) <= 0.0:
                continue
            name = str(row.get("name") or skill_id)
            version = row.get("version")
            return PipelineStep(
                action=f"skill:{name}",
                description=f"run the validated skill {name!r}",
                tool=SKILL_TOOL,
                target=skill_id,
                expected=f"{name!r} completes and its own verification passes",
                metadata={
                    "skill_id": skill_id,
                    "version": version if isinstance(version, int) else None,
                    "inputs": self._skill_inputs(task),
                    "selection": row,
                },
            )
        return None

    async def _run_skill_step(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        """Run the selected skill through the application's ONE skill service."""
        provider = self._skills
        assert provider is not None  # guarded by the caller
        skill_id = str(step.metadata.get("skill_id") or step.target or "")
        inputs = dict(step.metadata.get("inputs") or {})
        version = step.metadata.get("version")
        try:
            payload = await provider.execute(
                skill_id,
                goal=run.goal,
                inputs=inputs,
                version=version if isinstance(version, int) else None,
            )
        except Exception as exc:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
                tool=SKILL_TOOL,
            )
        result = dict(payload) if isinstance(payload, Mapping) else {}
        status = str(result.get("status", "") or "")
        verified = bool(result.get("verified", False))
        reason = str(result.get("reason") or result.get("error") or "")
        rows = [row for row in (result.get("results") or ()) if isinstance(row, Mapping)]
        output = {
            "skill_id": skill_id,
            "version": result.get("version", version),
            "status": status,
            "verified": verified,
            "execution_id": str(result.get("id", "") or ""),
            "steps": [
                {
                    "index": row.get("index"),
                    "verified": bool(row.get("verified", False)),
                    "detail": str(row.get("detail", ""))[:200],
                }
                for row in rows
            ],
        }
        if status == "completed" and verified:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.COMPLETED,
                detail=f"{step.action}: verified by the skill library",
                output=output,
                tool=SKILL_TOOL,
            )
        if status in ("waiting_approval", "blocked", "refused"):
            # Nothing was started, or the library stopped for a person: that is
            # a refusal to run, not a failed attempt, and the run must not
            # report success while it stands.
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.DENIED,
                detail=reason or status,
                output=output,
                tool=SKILL_TOOL,
            )
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.FAILED,
            detail=reason or f"the skill reported {status or 'no status'}",
            output=output,
            tool=SKILL_TOOL,
        )

    @staticmethod
    def _verify_skill_step(step: PipelineStep, outcome: StepOutcome) -> VerificationResult:
        """Verify a skill step from the library's own recorded verdict."""
        expectation = step.expected or step.action
        evidence = {
            "status": outcome.status.value,
            "skill": dict(outcome.output),
        }
        if outcome.status is StepStatus.SKIPPED:
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                expectation=expectation,
                reason=f"{step.action} did not run",
                evidence=evidence,
                confidence=0.0,
            )
        if outcome.ok and bool(outcome.output.get("verified", False)):
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=expectation,
                reason=f"{step.action} verified ({len(outcome.output.get('steps') or ())} step(s))",
                evidence=evidence,
                confidence=0.9,
            )
        return VerificationResult(
            status=VerificationStatus.FAIL,
            expectation=expectation,
            reason=outcome.detail or f"{step.action} did not verify",
            evidence=evidence,
            confidence=0.7,
        )

    async def _finish(self, agent: SpecialistAgent, run: AgentRun) -> AgentRun:
        for stage in STAGE_ORDER:
            if run.stage(stage) is None:
                run.record(stage, StageStatus.SKIPPED, "the run ended before this stage")
        # Judged on where each step ENDED UP, not on every attempt: a step that
        # failed and was recovered by a retry is a success with a story, which is
        # what the run's recoveries record. A step that was never applicable
        # (SKIPPED) is not a failure either — research that legitimately answered
        # from the local index must not be reported as broken — and a step that
        # was skipped when it mattered is caught by its own verification failing,
        # which is where that judgement belongs.
        final: dict[str, StepOutcome] = {}
        for outcome in run.outcomes:
            final[outcome.step_id] = outcome
        blocked = bool(run.denied) or any(
            outcome.status in (StepStatus.FAILED, StepStatus.DENIED) for outcome in final.values()
        )
        if run.status is not AgentTaskStatus.FAILED:
            # "Completed" means every step ran and something was verified — the
            # pipeline refuses to report success on an unverified run.
            run.status = (
                AgentTaskStatus.COMPLETED
                if run.outcomes and not blocked and run.verified
                else AgentTaskStatus.FAILED
            )
        try:
            run.report = dict(agent.report(run))
        except Exception as exc:
            run.errors.append(f"report: {type(exc).__name__}: {exc}")
            run.report = {}
        try:
            run.summary = agent.summarize(run)
        except Exception as exc:
            run.errors.append(f"summary: {type(exc).__name__}: {exc}")
            run.summary = f"{agent.name} finished with status {run.status.value}."
        await self._emit(
            run,
            EventType.TASK_COMPLETED
            if run.status is AgentTaskStatus.COMPLETED
            else EventType.TASK_FAILED,
            (
                {"task_id": run.id, "agent": agent.name}
                if run.status is AgentTaskStatus.COMPLETED
                else {"task_id": run.id, "error": run.summary, "agent": agent.name}
            ),
        )
        return run

    # -- helpers --------------------------------------------------------------

    def _declare(self, agent: SpecialistAgent) -> None:
        for name, declaration in agent.declarations().items():
            self._permissions.declare(name, declaration)
        if self._skills is not None:
            # The skill library checks a skill's own declared scopes before any
            # step runs, so what the PIPELINE authorizes is only the right to
            # ask it. Declared LOW and reversible: the authority for a sensitive
            # skill stays with the library's permission gate, not here.
            self._permissions.declare(
                SKILL_TOOL,
                PermissionDeclaration(
                    risk_level=RiskLevel.LOW,
                    requires_confirmation=False,
                    reversible=True,
                ),
            )

    def _available_tools(self, agent: SpecialistAgent) -> frozenset[str]:
        """What this agent may select: its declared capabilities, plus the live registry.

        Both halves matter. The agent's declarations say what IT can do (its
        actions are not in the tool registry, and should not be: they are not
        model-selectable tools). The registry says what THIS BUILD has — so
        passing a registry means a step naming a tool nobody registered is
        refused instead of attempted.
        """
        names: set[str] = set(agent.tools())
        if self._skills is not None:
            names.add(SKILL_TOOL)
        if self._tools is not None:
            for entry in self._tools.list():
                tool = getattr(entry, "tool", entry)
                name = getattr(tool, "name", None)
                if name:
                    names.add(str(name))
        return frozenset(names)

    async def _authorize(self, step: PipelineStep, available: frozenset[str]) -> ToolGrant:
        tool = step.tool or step.action
        if tool not in available:
            return ToolGrant(
                step_id=step.id,
                tool=tool,
                allowed=False,
                reason=f"{tool} is not registered with this build",
                source="catalog",
            )
        scopes = (step.permission,) if step.permission is not None else None
        assessment = self._permissions.assess(tool, action=step.action, permissions=scopes)
        approval_id: str | None = None
        approved = False
        if assessment.requires_confirmation or step.requires_approval:
            approved, approval_id = await self._request_approval(step, assessment)
        decision = self._permissions.check(
            tool, action=step.action, approved=approved, permissions=scopes
        )
        return ToolGrant(
            step_id=step.id,
            tool=tool,
            allowed=decision.allow,
            reason=decision.reason,
            risk=decision.risk,
            source=decision.source,
            approval_id=approval_id,
            permissions=decision.permissions,
        )

    async def _request_approval(
        self, step: PipelineStep, assessment: Any
    ) -> tuple[bool, str | None]:
        scopes = (
            (step.permission,)
            if step.permission is not None
            else tuple(getattr(assessment, "permissions", ()) or ())
        )
        try:
            decision = await self._approvals.request_approval(
                ApprovalRequest(
                    action=step.description or step.action,
                    reason=step.expected or f"{step.action} needs approval before it runs.",
                    permissions=scopes,
                    risk=getattr(assessment, "risk", step.risk or RiskLevel.MEDIUM),
                    metadata=step.to_dict(),
                )
            )
        except Exception:
            # An approval flow that fails is a refusal, never a permit.
            return False, None
        return bool(decision.approved), str(decision.request_id)

    def _max_executions(self, planned: int) -> int:
        return max(1, planned) * (self._max_recovery_attempts + 1) + 4

    async def _emit(self, run: AgentRun, type_: EventType, payload: dict[str, Any]) -> None:
        run.events.append(type_.value)
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                type_, source="agents.specialist", correlation_id=run.id, **payload
            )
        except Exception as exc:  # observability must never break execution
            run.errors.append(f"event {type_.value}: {type(exc).__name__}: {exc}")


def _jsonable(value: Any) -> bool:
    """Whether a context value is worth copying into the recorded stage data."""
    return isinstance(value, str | int | float | bool | list | dict | tuple | type(None))
