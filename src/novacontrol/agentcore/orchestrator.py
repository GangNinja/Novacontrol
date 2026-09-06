"""Agentic Orchestrator: the perceive→plan→act→observe→verify→recover loop.

Composes NovaControl's EXISTING controllers (browser, desktop, vision,
explore/research, memory) behind the agentcore interfaces. Owns one
AgentTaskState per task, persists the application knowledge graph and
evaluation ledger through JsonStateStore, and publishes every phase on the
EventBus for observability.

Research is an injected callable so this module has no hard dependency on the
explore package (the application composition root wires it).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from novacontrol.agentcore.actions import ActionEngine, ActionOutcome
from novacontrol.agentcore.evaluation import EvaluationLedger
from novacontrol.agentcore.interpreter import TaskInterpreter
from novacontrol.agentcore.knowledge import ApplicationKnowledgeGraph
from novacontrol.agentcore.planner import AdaptivePlanner, AgentPlan, AgentPlanStep, StepKind
from novacontrol.agentcore.recovery import RecoveryEngine, RecoveryStrategy
from novacontrol.agentcore.state import (
    ActionRecord,
    AgentTaskState,
    Lesson,
    Observation,
    TaskPhase,
)

__all__ = ["AgenticOrchestrator", "AgentTaskState"]
from novacontrol.agentcore.ui_state import UiState
from novacontrol.agentcore.verifier import Expectation, Verifier, VerificationStatus


class _EventPublisher(Protocol):
    async def publish(self, event: Any) -> None:  # pragma: no cover - protocol
        ...


ResearchFn = Callable[[str], Awaitable[dict[str, Any]]]
PersistFn = Callable[[], None]


class AgenticOrchestrator:
    """Runs the full agentic loop for a natural-language goal."""

    def __init__(
        self,
        *,
        perception: Any,
        actions: ActionEngine,
        verifier: Verifier | None = None,
        planner: AdaptivePlanner | None = None,
        recovery: RecoveryEngine | None = None,
        knowledge: ApplicationKnowledgeGraph | None = None,
        evaluation: EvaluationLedger | None = None,
        interpreter: TaskInterpreter | None = None,
        research_fn: ResearchFn | None = None,
        persist_fn: PersistFn | None = None,
        event_bus: Any | None = None,
        max_plan_iterations: int = 6,
    ) -> None:
        self.perception = perception
        self.actions = actions
        self.verifier = verifier or Verifier()
        self.planner = planner or AdaptivePlanner()
        self.recovery = recovery or RecoveryEngine(knowledge=knowledge)
        self.knowledge = knowledge or ApplicationKnowledgeGraph()
        self.evaluation = evaluation or EvaluationLedger()
        self.interpreter = interpreter or TaskInterpreter()
        self._research_fn = research_fn
        self._persist_fn = persist_fn
        self._event_bus = event_bus
        self._max_plan_iterations = max_plan_iterations

    # -- public API -------------------------------------------------------------

    async def run_task(self, request: str, *, context: dict[str, Any] | None = None) -> AgentTaskState:
        started = time.monotonic()
        interpretation = await self.interpreter.interpret(request)
        state = AgentTaskState(goal=request)
        state.interpretation = interpretation.to_dict()
        await self._announce("agentcore.task_started", state, {"interpretation": state.interpretation})

        # Memory retrieval BEFORE acting: a previously verified workflow shapes
        # the plan but never replaces observation.
        known = self.planner.workflow_for(self._application_hint(state))
        plan = self.planner.create_plan(request, interpretation=state.interpretation)
        if known:
            state.learn(Lesson(
                kind="workflow",
                text=f"Known workflow for {self._application_hint(state)}: {' -> '.join(known)}",
                confidence=self.knowledge.confidence_for(self._application_hint(state), known[0]),
                source="application_memory",
            ))
        self._store_plan(state, plan)
        state.transition(TaskPhase.EXECUTING)

        step: AgentPlanStep | None = None
        for _ in range(self._max_plan_iterations):
            step = self._next_step(state)
            if step is None:
                break  # plan exhausted
            state.current_step = step.description
            await self._announce("agentcore.step_started", state, {"step": step.to_dict()})

            if step.kind is StepKind.VERIFY:
                await self._run_final_verification(state)
                if state.status is TaskPhase.COMPLETED:
                    break
                continue

            if step.kind is StepKind.RESEARCH:
                await self._run_research(state, step)
                continue

            await self._execute_step(state, step)
            if state.status is TaskPhase.FAILED:
                break

        await self._finalize(state, started)
        return state

    async def get_state(self, task_id: str) -> AgentTaskState | None:
        return self._active.get(task_id) if hasattr(self, "_active") else None

    # -- loop phases ---------------------------------------------------------------

    def _store_plan(self, state: AgentTaskState, plan: AgentPlan) -> None:
        state.plan = [step.to_dict() for step in plan.steps]
        self._plan = plan
        state.touch()

    def _next_step(self, state: AgentTaskState) -> AgentPlanStep | None:
        """First plan step not yet executed (plan is the live queue).
        `status: done` markers and executed action ids both retire steps."""
        executed = {record.step_id for record in state.actions}
        for entry in state.plan:
            if entry.get("status") == "done" or entry.get("id") in executed:
                continue
            return AgentPlanStep.from_dict(entry)
        return None

    async def _execute_step(self, state: AgentTaskState, step: AgentPlanStep) -> None:
        # An OBSERVE step IS the perception action: take the snapshot, record
        # it, done. It is never "verified" like an action — that would demand
        # the world match a sentence about itself and spiral the recovery loop.
        if step.kind is StepKind.OBSERVE:
            await self._observe(state, step.id)
            self._mark_step(state, step.id, "done")
            return

        # OBSERVE before acting (the loop is observe→act→observe, not act→pray).
        before = await self._observe(state, f"before:{step.id}")

        outcome = await self._dispatch(state, step, before)
        record = ActionRecord(
            step_id=step.id,
            action=outcome.action,
            target=outcome.target,
            success=outcome.success,
            detail=outcome.detail,
            output=outcome.output,
        )
        state.actions.append(record)
        state.touch()
        await self._announce("agentcore.action_completed", state, {"action": record.to_dict()})

        # OBSERVE after, then VERIFY — never trust a bare return code.
        after = await self._observe(state, f"after:{step.id}")
        expectation = Expectation(description=step.description, ui_contains=_needles(step.expected))
        verification = self.verifier.verify(expectation, after, before_state=before)
        state.verification = {**state.verification, f"step:{step.id}": verification.to_dict()}
        await self._announce("agentcore.step_verified", state, {"verification": verification.to_dict()})

        if verification.status is VerificationStatus.PASS:
            state.learn(Lesson(kind="observation", text=f"Step succeeded: {step.description}", confidence=verification.confidence))
            self._mark_step(state, step.id, "done")
            return

        if not outcome.success and verification.status is VerificationStatus.FAIL:
            await self._recover(state, step, record, after)
            return

        # Action executed but expectation not met → treat as failure too.
        await self._recover(state, step, record, after)

    async def _dispatch(self, state: AgentTaskState, step: AgentPlanStep, before: UiState) -> ActionOutcome:
        try:
            if step.kind is StepKind.BROWSER:
                url = step.target if step.target.startswith(("http://", "https://")) else self._url_from_target(step.target)
                if url:
                    return await self.actions.navigate(url)
                return await self.actions.click_element(before, step.target)
            if step.kind is StepKind.DESKTOP:
                lowered = step.description.lower()
                if lowered.startswith(("open ", "launch ", "start ")):
                    return await self.actions.open_application(step.target or step.description.split(" ", 1)[1])
                return await self.actions.click_element(before, step.target or step.description)
            if step.kind is StepKind.PHONE:
                return ActionOutcome(False, "phone", step.target, detail="Phone actions run through the J.A.R.V.I.S approval flow, not the agentic loop")
            if step.kind is StepKind.REASON:
                return ActionOutcome(True, "reason", step.target, detail="Handled during verification with the observations gathered so far")
            return ActionOutcome(True, "observe", step.target, detail="Observation-only step")
        except Exception as exc:
            return ActionOutcome(False, step.kind.value, step.target, detail=f"{type(exc).__name__}: {exc}")

    async def _recover(self, state: AgentTaskState, step: AgentPlanStep, record: ActionRecord, after: UiState | None) -> None:
        state.transition(TaskPhase.RECOVERING)
        app = self._application_hint(state) or (after.application if after else "")
        outcome = self.recovery.diagnose(record, current_state=after, application=app)
        state.recoveries.append({
            "step_id": step.id,
            "failed_action": record.action,
            "failed_target": record.target,
            **outcome.to_dict(),
            "succeeded": False,
        })
        state.add_error(f"Step failed: {step.description} — {outcome.diagnosis}")
        await self._announce("agentcore.recovery_planned", state, {"recovery": outcome.to_dict()})

        if outcome.strategy is RecoveryStrategy.ABORT:
            self._fail(state, f"Recovery aborted: {outcome.diagnosis}")
            return

        if outcome.strategy is RecoveryStrategy.RESEARCH and self._research_fn is not None:
            await self._run_research(state, AgentPlanStep(
                kind=StepKind.RESEARCH,
                description=outcome.strategy_text or f"Research {record.target}",
                expected="A documented procedure",
            ))

        # Apply the strategy to the remaining plan.
        recovery_step = AgentPlanStep(
            kind=step.kind,
            description=outcome.alternative_target and f"{step.kind.value}: {outcome.alternative_target}" or outcome.strategy_text or step.description,
            target=outcome.alternative_target or step.target,
            expected=step.expected,
        )
        replanned = self.planner.replan(self._plan, failed_step=step, observation_summary=after.raw_text[:120] if after else "", recovery_strategy=recovery_step.description)
        self._plan = replanned
        state.plan = [s.to_dict() for s in replanned.steps]
        state.transition(TaskPhase.EXECUTING)

    async def _run_research(self, state: AgentTaskState, step: AgentPlanStep) -> None:
        if self._research_fn is None:
            state.learn(Lesson(kind="research", text="Research requested but no research provider is configured.", confidence=0.2))
            self._mark_step(state, step.id, "done")
            return
        try:
            report = await self._research_fn(step.description)
            summary = str(report.get("summary", ""))[:600]
            state.learn(Lesson(kind="research", text=f"Researched {step.description!r}: {summary}", confidence=0.7, source="research_agent"))
            state.observations.append(Observation(source="research", summary=summary, data={"sources": report.get("sources", [])[:5]}))
        except Exception as exc:
            state.add_error(f"Research failed: {type(exc).__name__}: {exc}")
        self._mark_step(state, step.id, "done")

    async def _run_final_verification(self, state: AgentTaskState) -> None:
        after = await self._observe(state, "final")
        evidence = "\n".join([after.raw_text] + [obs.summary for obs in state.observations]).lower()
        needles = _needles(state.goal)
        if not evidence.strip():
            state.verification = {"final": {"status": "inconclusive", "reason": "No observations to verify the goal against."}}
            self._mark_step(state, self._current_verify_step_id(state), "done")
            return
        # Evidence must actually mention the goal's substance. "We ran some
        # actions" is NOT completion — that conflation is exactly what the
        # Verifier exists to prevent.
        hit = any(needle in evidence for needle in needles)
        if hit:
            state.verification = {"final": {"status": "pass", "reason": "Observed evidence supports goal completion.", "actions_executed": len(state.actions)}}
            state.transition(TaskPhase.COMPLETED)
        else:
            state.verification = {"final": {"status": "fail", "reason": "No observed evidence supports goal completion."}}
            state.transition(TaskPhase.FAILED)
        self._mark_step(state, self._current_verify_step_id(state), "done")

    async def _finalize(self, state: AgentTaskState, started: float) -> None:
        if state.status not in (TaskPhase.COMPLETED, TaskPhase.FAILED):
            # Plan exhausted without explicit verification: judge from evidence.
            ok = all(record.success for record in state.actions) and bool(state.actions)
            state.transition(TaskPhase.COMPLETED if ok else TaskPhase.FAILED)
        state.summary = self._summarize(state)
        # Learning: store verified workflows back into the knowledge graph.
        if state.status is TaskPhase.COMPLETED and state.actions:
            app = self._application_hint(state) or "unknown"
            steps = tuple(record.action + " " + record.target for record in state.actions)
            self.knowledge.record_workflow(app, state.goal[:80], steps, confidence=0.6)
            state.learn(Lesson(kind="workflow", text=f"Stored verified workflow for {app}", confidence=0.6))
        elif state.status is TaskPhase.FAILED:
            self.knowledge.record_failure(self._application_hint(state) or "unknown", state.goal[:80])
        self._persist()
        await self._announce("agentcore.task_finished", state, {"status": state.status.value, "summary": state.summary})
        self.evaluation.record_task(state.to_dict(), duration_seconds=time.monotonic() - started)
        self._persist()

    # -- helpers -------------------------------------------------------------------

    async def _observe(self, state: AgentTaskState, tag: str) -> UiState:
        try:
            ui_state: UiState = await self.perception.observe()
            state.observations.append(Observation(
                source=f"step:{tag}",
                summary=f"{ui_state.application} | {ui_state.window} | elements={len(ui_state.elements)}",
                ui_state=ui_state.to_dict(),
            ))
            state.touch()
            return ui_state
        except Exception as exc:
            state.add_error(f"Observation failed at {tag}: {type(exc).__name__}: {exc}")
            return UiState(application="unknown", source="failed", raw_text="")

    def _application_hint(self, state: AgentTaskState) -> str:
        tools = state.interpretation.get("required_tools", [])
        if "browser" in tools:
            return "browser"
        if "desktop" in tools:
            return "desktop"
        if "phone" in tools:
            return "phone"
        return ""

    @staticmethod
    def _url_from_target(target: str) -> str:
        lowered = target.lower()
        if "github" in lowered:
            return "https://github.com"
        if "youtube" in lowered:
            return "https://www.youtube.com"
        if lowered.startswith("http"):
            return target
        return ""

    def _mark_step(self, state: AgentTaskState, step_id: str, marker: str) -> None:
        for entry in state.plan:
            if entry.get("id") == step_id:
                entry["status"] = marker
        state.touch()

    def _current_verify_step_id(self, state: AgentTaskState) -> str:
        for entry in state.plan:
            if entry.get("kind") == "verify" and entry.get("status") != "done":
                return str(entry.get("id", ""))
        return ""

    def _fail(self, state: AgentTaskState, reason: str) -> None:
        state.add_error(reason)
        state.transition(TaskPhase.FAILED)

    def _summarize(self, state: AgentTaskState) -> str:
        ok = state.status is TaskPhase.COMPLETED
        head = "Task completed." if ok else "Task failed."
        actions = f"{len(state.actions)} action(s) executed" if state.actions else "no actions were executed"
        recoveries = f", {len(state.recoveries)} recovery attempt(s)" if state.recoveries else ""
        lessons = f"; {len(state.learned_information)} item(s) learned" if state.learned_information else ""
        return f"{head} {actions}{recoveries}{lessons}. {state.verification.get('final', {}).get('reason', '')}".strip()

    def _persist(self) -> None:
        if self._persist_fn is not None:
            try:
                self._persist_fn()
            except Exception:
                pass

    async def _announce(self, event_type: str, state: AgentTaskState, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        from novacontrol.core.events import Event

        try:
            await self._event_bus.publish(Event(type=event_type, payload={"task_id": state.id, **payload}, source="agentcore"))
        except Exception:
            pass  # observability must never break execution


def _needles(text: str) -> tuple[str, ...]:
    stop = {"the", "that", "this", "with", "from", "then", "after", "and", "for", "was", "were", "verify"}
    words = [word.lower() for word in text.replace(":", " ").split() if len(word) >= 4 and word.lower() not in stop]
    return tuple(words[:3])
