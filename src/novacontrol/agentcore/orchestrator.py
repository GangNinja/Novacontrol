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

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from novacontrol.agentcore.actions import ActionEngine, ActionOutcome
from novacontrol.agentcore.evaluation import EvaluationLedger
from novacontrol.agentcore.interpreter import TaskInterpreter
from novacontrol.agentcore.knowledge import ApplicationKnowledgeGraph
from novacontrol.agentcore.planner import AdaptivePlanner, AgentPlan, AgentPlanStep, StepKind
from novacontrol.agentcore.recovery import (
    RecoveryEngine,
    RecoveryOutcome,
    RecoveryStrategy,
    input_delivery_state,
)
from novacontrol.agentcore.state import (
    ActionRecord,
    AgentTaskState,
    Lesson,
    Observation,
    TaskPhase,
)
from novacontrol.agentcore.targeting import (
    WindowBinding,
    binding_from_dict,
    requested_target,
)

__all__ = ["AgenticOrchestrator", "AgentTaskState"]
from novacontrol.agentcore.ui_state import UiState
from novacontrol.agentcore.verifier import Expectation, VerificationStatus, Verifier


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
        max_plan_iterations: int = 12,
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
        # Task state used to be reachable only while a caller held the return
        # value. The operator needs one live answer to "what is running?":
        # _active is what a stop route reads, _tasks retains the last completed
        # states for inspection without making the ledger grow forever.
        self._active: dict[str, AgentTaskState] = {}
        self._tasks: dict[str, AgentTaskState] = {}

    # -- public API -------------------------------------------------------------

    async def run_task(
        self,
        request: str,
        *,
        context: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentTaskState:
        started = time.monotonic()
        state = AgentTaskState(goal=request)
        self._active[state.id] = state
        self._tasks[state.id] = state
        while len(self._tasks) > 25:
            oldest = next(iter(self._tasks))
            self._tasks.pop(oldest, None)
        interpretation = await self.interpreter.interpret(request)
        state.interpretation = interpretation.to_dict()
        # D12: the window the OBJECTIVE names, extracted once, before any
        # observation. Every observation is then grounded on it, and every
        # input-producing step is refused unless that window is still bound.
        state.interpretation["target_window"] = requested_target(request)
        if context:
            state.interpretation["context"] = dict(context)
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
        try:
            for _ in range(self._max_plan_iterations):
                if self._stop_requested(state, cancel_event):
                    break
                step = self._next_step(state)
                if step is None:
                    break  # plan exhausted
                state.current_step = step.description
                await self._announce("agentcore.step_started", state, {"step": step.to_dict()})

                if step.kind is StepKind.VERIFY:
                    if not self._stop_requested(state, cancel_event):
                        await self._run_final_verification(state)
                    if state.status in (TaskPhase.COMPLETED, TaskPhase.CANCELLED):
                        break
                    continue

                if step.kind is StepKind.RESEARCH:
                    if self._stop_requested(state, cancel_event):
                        break
                    await self._run_research(state, step)
                    continue

                await self._execute_step(state, step, cancel_event)
                if state.status in (TaskPhase.FAILED, TaskPhase.CANCELLED):
                    break
        finally:
            await self._finalize(state, started)
            self._active.pop(state.id, None)
        return state

    async def get_state(self, task_id: str) -> AgentTaskState | None:
        return self._active.get(task_id) or self._tasks.get(task_id)

    def _stop_requested(self, state: AgentTaskState, cancel_event: asyncio.Event | None) -> bool:
        """Cooperative stop check, made before observation and before input."""
        if cancel_event is None or not cancel_event.is_set():
            return False
        if state.status is not TaskPhase.CANCELLED:
            state.add_error("Task cancelled by the operator; no further action was started.")
            state.transition(TaskPhase.CANCELLED)
            self._mark_current_step(state, "cancelled")
        return True

    def _mark_current_step(self, state: AgentTaskState, marker: str) -> None:
        for entry in state.plan:
            if entry.get("status") != "done":
                entry["status"] = marker
                break
        state.touch()

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

    async def _execute_step(
        self,
        state: AgentTaskState,
        step: AgentPlanStep,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        # An OBSERVE step IS the perception action: take the snapshot, record
        # it, done. It is never "verified" like an action — that would demand
        # the world match a sentence about itself and spiral the recovery loop.
        if step.kind is StepKind.OBSERVE:
            if not self._stop_requested(state, cancel_event):
                await self._observe(state, step.id)
                self._mark_step(state, step.id, "done")
            return

        # OBSERVE before acting (the loop is observe→act→observe, not act→pray).
        if self._stop_requested(state, cancel_event):
            return
        before = await self._observe(state, f"before:{step.id}")
        if self._stop_requested(state, cancel_event):
            return

        # D12 gate: this observation is the revalidation immediately before
        # input. An input step runs only when the objective's window is bound
        # here, unambiguously, with a stable handle/pid that has not changed
        # since the task first bound it. Otherwise nothing is sent and the task
        # fails with the reason recorded.
        target = self._bound_target(before)
        refusal = self._target_refusal(state, step, target)
        if refusal:
            state.add_error(refusal)
            target_evidence = (
                target.to_dict() if target is not None else {"status": "none_requested"}
            )
            state.verification = {**state.verification, f"target:{step.id}": target_evidence}
            state.actions.append(ActionRecord(
                step_id=step.id,
                action="refused",
                target=target.requested if target is not None else step.target,
                success=False,
                detail=refusal,
                output={"refused": True, "target": target_evidence},
            ))
            state.touch()
            await self._announce(
                "agentcore.action_completed", state, {"action": "refused", "reason": refusal}
            )
            state.transition(TaskPhase.FAILED)
            self._mark_current_step(state, "refused")
            return

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
        if self._stop_requested(state, cancel_event):
            return

        # OBSERVE after, then VERIFY — never trust a bare return code.
        after = await self._observe(state, f"after:{step.id}")
        bound_ref = target.ref if target is not None and target.ok else None
        expectation = Expectation(
            description=step.description,
            ui_contains=_needles(step.expected),
            target_identity=bound_ref.identity if bound_ref is not None else None,
            target_title=bound_ref.title if bound_ref is not None else "",
        )
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

    @staticmethod
    def _is_input_step(step: AgentPlanStep) -> bool:
        """Steps that deliver keystrokes/pointer input and therefore need a target."""
        if step.kind is not StepKind.DESKTOP:
            return False
        lowered = step.description.lower()
        return (
            lowered.startswith("type ")
            or lowered.startswith("save ")
            or bool(re.search(r"\b(click|press|scroll|drag|select all|copy|paste)\b", lowered))
        )

    @staticmethod
    def _bound_target(ui_state: UiState) -> WindowBinding | None:
        """The objective-bound window this observation reports, if any."""
        data = (ui_state.metadata or {}).get("target")
        if not isinstance(data, dict):
            return None
        binding = binding_from_dict(data)
        return None if binding.status == "none_requested" else binding

    def _target_refusal(
        self, state: AgentTaskState, step: AgentPlanStep, target: WindowBinding | None
    ) -> str:
        """Why this step must not send input, or ``""`` when it may."""
        if not self._is_input_step(step):
            return ""
        if target is None:
            return (
                "Refusing to send input: the objective names no window, so no "
                "destination could be established from the observations."
            )
        if not target.ok:
            return (
                f"Refusing to send input: the objective names {target.requested!r}, "
                f"but {target.reason}."
            )
        ref = target.ref
        if ref is None or not (ref.handle and ref.pid):
            return (
                f"Refusing to send input to {target.requested!r}: the observation "
                "reports no stable window handle/pid, so the window's identity "
                "cannot be carried through to verification."
            )
        known = tuple(state.interpretation.get("target_identity") or ())
        if known and tuple(ref.identity) != known:
            return (
                f"Refusing to send input: the window named {target.requested!r} "
                f"changed identity ({known} -> {tuple(ref.identity)}); it is no "
                "longer the target this task verified."
            )
        state.interpretation["target_identity"] = list(ref.identity)
        return ""

    async def _dispatch(self, state: AgentTaskState, step: AgentPlanStep, before: UiState) -> ActionOutcome:
        try:
            if step.kind is StepKind.BROWSER:
                lowered = f"{step.target} {step.description}".lower()
                if lowered.strip().startswith(("go back", "back", "previous page")) or "go back" in lowered:
                    return await self.actions.go_back()
                url = step.target if step.target.startswith(("http://", "https://")) else self._url_from_target(step.target)
                if url:
                    return await self.actions.navigate(url)
                return await self.actions.click_element(before, step.target)
            if step.kind is StepKind.DESKTOP:
                bound = self._bound_target(before)
                ref = bound.ref if bound is not None and bound.ok else None
                lowered = step.description.lower()
                if lowered.startswith("type "):
                    text = re.sub(r"^type\s+", "", step.description, flags=re.IGNORECASE)
                    text = re.split(r"\s+into\s+", text, maxsplit=1, flags=re.IGNORECASE)[0].strip().strip("'\"")
                    if not text:
                        return ActionOutcome(False, "type", "active window", detail="The step names no text to type.")
                    if ref is None:
                        return ActionOutcome(
                            False, "type", "unverified window",
                            detail="Refusing to type: no objective-bound window was established.",
                        )
                    return await self.actions.type_into_active(
                        text,
                        window_title=ref.title,
                        window_handle=ref.handle,
                        window_pid=ref.pid,
                    )
                if lowered.startswith("save "):
                    path = re.sub(r"^save(?:\s+(?:the|this)\s+(?:document|file))?(?:\s+as)?\s+", "", step.description, flags=re.IGNORECASE).strip().strip("'\"")
                    if not path:
                        return ActionOutcome(False, "save", "document", detail="The save step names no destination path.")
                    return await self.actions.save_document(
                        path, window_title=ref.title if ref is not None else ""
                    )
                if re.search(r"\b(click|press|scroll|drag|select|copy|paste)\b", lowered) and "open " not in lowered:
                    target = step.target or step.description
                    if re.search(r"\bscroll\b", lowered):
                        return await self.actions.scroll(before, -480, target=target)
                    if re.search(r"\b(select all|copy|paste)\b", lowered):
                        if "select all" in lowered:
                            return await self.actions.select_all()
                        if re.search(r"\bcopy\b", lowered):
                            return await self.actions.copy()
                        return await self.actions.paste()
                    return await self.actions.click_element(before, target, require_bound_target=True)
                if lowered.startswith(("focus ", "switch to ", "bring ")):
                    return await self.actions.focus_window(step.target or step.description)
                if lowered.startswith(("minimize ",)):
                    return await self.actions.minimize_window(step.target or step.description)
                if lowered.startswith(("maximize ",)):
                    return await self.actions.maximize_window(step.target or step.description)
                if lowered.startswith(("close ", "stop ")):
                    return await self.actions.close_application(step.target or step.description.split(" ", 1)[-1])
                if lowered.startswith(("open ", "launch ", "start ")):
                    return await self.actions.open_application(step.target or step.description.split(" ", 1)[1])
                return await self.actions.click_element(
                    before, step.target or step.description, require_bound_target=True
                )
            if step.kind is StepKind.PHONE:
                return ActionOutcome(False, "phone", step.target, detail="Phone actions run through the J.A.R.V.I.S approval flow, not the agentic loop")
            if step.kind is StepKind.REASON:
                return ActionOutcome(True, "reason", step.target, detail="Handled during verification with the observations gathered so far")
            return ActionOutcome(True, "observe", step.target, detail="Observation-only step")
        except Exception as exc:
            # An input attempt that failed still carries where its events went
            # (DF-6/M2.8): without it a refused paste reports no destination.
            carried = getattr(exc, "dispatch_record", None)
            carried_output = dict(carried) if isinstance(carried, dict) else {}
            return ActionOutcome(
                False,
                step.kind.value,
                step.target,
                detail=f"{type(exc).__name__}: {exc}",
                output=carried_output,
            )

    async def _recover(self, state: AgentTaskState, step: AgentPlanStep, record: ActionRecord, after: UiState | None) -> None:
        state.transition(TaskPhase.RECOVERING)
        app = self._application_hint(state) or (after.application if after else "")
        expected_identity = tuple(state.interpretation.get("target_identity") or ()) or None
        delivery = input_delivery_state(record)
        outcome = self.recovery.diagnose(
            record,
            current_state=after,
            application=app,
            expected_identity=expected_identity,
        )
        if delivery in ("delivered", "uncertain") and outcome.strategy in (
            RecoveryStrategy.RETRY,
            RecoveryStrategy.RELOCATE,
        ):
            # Defence in depth: input that may already have landed is never
            # re-dispatched, whatever a recovery engine selects.
            outcome = RecoveryOutcome(
                RecoveryStrategy.RECHECK,
                f"The {record.action} to {record.target!r} may already have been "
                "delivered, so it will not be repeated automatically.",
                strategy_text="Re-observe and re-verify; do not resend input.",
                confidence=0.6,
            )
        state.recoveries.append({
            "step_id": step.id,
            "failed_action": record.action,
            "failed_target": record.target,
            "delivery": delivery,
            **outcome.to_dict(),
            "succeeded": False,
        })
        state.add_error(f"Step failed: {step.description} — {outcome.diagnosis}")
        await self._announce("agentcore.recovery_planned", state, {"recovery": outcome.to_dict()})

        if outcome.strategy is RecoveryStrategy.ABORT:
            self._fail(state, f"Recovery aborted: {outcome.diagnosis}")
            return

        if outcome.strategy is RecoveryStrategy.RECHECK:
            # The only safe response to possibly-delivered input: look again,
            # verify again, and never send the input a second time.
            await self._recheck(state, step, record, after)
            return

        if outcome.strategy is RecoveryStrategy.RESEARCH and self._research_fn is not None:
            await self._run_research(state, AgentPlanStep(
                kind=StepKind.RESEARCH,
                description=outcome.strategy_text or f"Research {record.target}",
                expected="A documented procedure",
            ))

        # Apply the strategy to the remaining plan. A RETRY re-runs the SAME step
        # (a rewritten description used to degrade "type …" into a click); a
        # RELOCATE names the different element it will act on.
        if outcome.strategy is RecoveryStrategy.RETRY:
            recovery_step = replace(step)
        else:
            recovery_step = AgentPlanStep(
                kind=step.kind,
                description=outcome.alternative_target and f"{step.kind.value}: {outcome.alternative_target}" or outcome.strategy_text or step.description,
                target=outcome.alternative_target or step.target,
                expected=step.expected,
            )
        replanned = self.planner.replan(self._plan, failed_step=step, observation_summary=after.raw_text[:120] if after else "", recovery_strategy=recovery_step.description)
        if outcome.strategy is RecoveryStrategy.RETRY:
            # The retry is a NEW attempt: `_next_step` retires plan entries whose
            # id already has an action record, so reusing the failed id made every
            # retry inert. The failed action keeps its own id and record.
            retry_id = f"{step.id}:retry:{len(state.recoveries)}"
            replanned = AgentPlan(
                goal=replanned.goal,
                steps=tuple(
                    replace(candidate, id=retry_id) if candidate.id == step.id else candidate
                    for candidate in replanned.steps
                ),
            )
        self._plan = replanned
        state.plan = [s.to_dict() for s in replanned.steps]
        state.transition(TaskPhase.EXECUTING)

    async def _recheck(
        self,
        state: AgentTaskState,
        step: AgentPlanStep,
        record: ActionRecord,
        after: UiState | None,
    ) -> None:
        """Re-observe and re-verify without resending a possibly-delivered action.

        No action record is appended: the durable history keeps exactly one record
        for the action that ran. Success here means the fresh observation now
        supports the step — never that the input call returned.
        """
        fresh = await self._observe(state, f"recheck:{step.id}")
        bound = self._bound_target(fresh)
        ref = bound.ref if bound is not None and bound.ok else None
        expectation = Expectation(
            description=step.description,
            ui_contains=_needles(step.expected),
            target_identity=ref.identity if ref is not None else None,
            target_title=ref.title if ref is not None else "",
        )
        verification = self.verifier.verify(expectation, fresh, before_state=after)
        state.verification = {
            **state.verification,
            f"recheck:{step.id}": verification.to_dict(),
        }
        await self._announce(
            "agentcore.step_verified", state, {"verification": verification.to_dict()}
        )
        entry = next(
            (
                row
                for row in reversed(state.recoveries)
                if row.get("step_id") == step.id
            ),
            None,
        )
        if verification.status is VerificationStatus.PASS:
            if entry is not None:
                entry["succeeded"] = True
            state.learn(Lesson(
                kind="observation",
                text=f"Recovery re-verified without resending input: {step.description}",
                confidence=verification.confidence,
            ))
            self._mark_step(state, step.id, "done")
            state.transition(TaskPhase.EXECUTING)
            return
        if entry is not None:
            entry["succeeded"] = False
            entry["recheck"] = verification.to_dict()
        self._fail(
            state,
            "The action was not repeated because its input may already have been "
            "delivered, and a fresh observation did not confirm the expected "
            f"effect: {verification.reason}",
        )

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

    @staticmethod
    def _unverified_input_steps(state: AgentTaskState) -> list[str]:
        """Input steps whose effect was never observed in the intended target.

        A later observation of the screen cannot substitute for them: the input
        API returning is not proof that the text/click landed where the objective
        said, and a goal word appearing in a window title is not that proof
        either. A retried step with the same description that DID verify clears it.
        """
        per_step = {
            key: value
            for key, value in state.verification.items()
            if key.startswith("step:") and isinstance(value, dict)
        }
        passed = {
            str(value.get("expectation") or "").strip().lower()
            for value in per_step.values()
            if str(value.get("status")) == "pass"
        }
        unverified: list[str] = []
        for value in per_step.values():
            if str(value.get("status")) == "pass":
                continue
            description = str(value.get("expectation") or "").strip()
            lowered = description.lower()
            if not lowered.startswith(
                ("type ", "click ", "press ", "save ", "scroll ", "select ", "copy ", "paste ")
            ):
                continue
            if lowered in passed:
                continue
            unverified.append(description)
        return unverified

    async def _run_final_verification(self, state: AgentTaskState) -> None:
        after = await self._observe(state, "final")
        evidence = "\n".join([after.raw_text] + [obs.summary for obs in state.observations]).lower()
        needles = _needles(state.goal)
        unverified = self._unverified_input_steps(state)
        if unverified:
            state.verification = {
                **state.verification,
                "final": {
                    "status": "fail",
                    "reason": (
                        f"{len(unverified)} input step(s) were never verified in the "
                        f"intended target: {unverified[:3]}"
                    ),
                    "actions_executed": len(state.actions),
                },
            }
            state.transition(TaskPhase.FAILED)
            self._mark_step(state, self._current_verify_step_id(state), "done")
            return
        if not evidence.strip():
            state.verification = {
                **state.verification,
                "final": {
                    "status": "inconclusive",
                    "reason": "No observations to verify the goal against.",
                },
            }
            self._mark_step(state, self._current_verify_step_id(state), "done")
            return
        if not needles:
            # The goal names nothing distinctive, so nothing on a screen could
            # confirm it. Claiming completion here would be exactly the
            # "we ran some actions" conflation this verifier exists to prevent.
            state.verification = {
                **state.verification,
                "final": {
                    "status": "inconclusive",
                    "reason": (
                        "The goal names nothing observable, so completion cannot be claimed."
                    ),
                },
            }
            self._mark_step(state, self._current_verify_step_id(state), "done")
            return
        # Evidence must actually mention the goal's substance. "We ran some
        # actions" is NOT completion — that conflation is exactly what the
        # Verifier exists to prevent.
        hit = any(needle in evidence for needle in needles)
        if hit:
            state.verification = {
                **state.verification,
                "final": {
                    "status": "pass",
                    "reason": "Observed evidence supports goal completion.",
                    "actions_executed": len(state.actions),
                },
            }
            state.transition(TaskPhase.COMPLETED)
        else:
            state.verification = {
                **state.verification,
                "final": {
                    "status": "fail",
                    "reason": "No observed evidence supports goal completion.",
                },
            }
            state.transition(TaskPhase.FAILED)
        self._mark_step(state, self._current_verify_step_id(state), "done")

    async def _finalize(self, state: AgentTaskState, started: float) -> None:
        if state.status is TaskPhase.CANCELLED:
            state.summary = self._summarize(state)
            self._persist()
            await self._announce("agentcore.task_finished", state, {"status": state.status.value, "summary": state.summary})
            self.evaluation.record_task(state.to_dict(), duration_seconds=time.monotonic() - started)
            self._persist()
            return
        if state.status not in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED):
            # Plan exhausted (for example at the iteration bound) without a
            # final verification. A successful action is not a completed goal:
            # take the missing observation and let the verifier decide.
            final = state.verification.get("final", {})
            if not isinstance(final, dict) or final.get("status") != "pass":
                await self._run_final_verification(state)
            if state.status not in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CANCELLED):
                self._fail(state, "The plan ended without a verified outcome.")
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
        # A browser task is already observed through the DOM layer that answers
        # it, so paying for a window probe and a screen capture there buys
        # pictures of nothing the step is about — and would change what a page
        # observation contains. Every other kind of work (desktop, phone, or a
        # goal whose tools are not yet known) must LOOK at the machine, or the
        # observation it verifies against carries no evidence at all. That is
        # the difference the game-control audit measured: before this, every
        # observation in a desktop run came back empty.
        include_desktop = self._application_hint(state) != "browser"
        try:
            target_window = str(state.interpretation.get("target_window") or "")
            if target_window:
                ui_state: UiState = await self.perception.observe(
                    include_desktop=include_desktop, target_window=target_window
                )
            else:
                ui_state = await self.perception.observe(include_desktop=include_desktop)
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
        if state.status is TaskPhase.CANCELLED:
            head = "Task cancelled."
        else:
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


# Words that appear in almost every plan sentence AND in ordinary screen text,
# so matching one proves nothing about whether the goal was reached. They were
# kept before because an observation was almost always empty; the moment
# observations became real (game-control remediation Phase A) the word
# "application" matched "ApplicationFrameHost" in a window title and a stubbed
# action was reported as a completed goal. A needle has to name the thing the
# goal is ABOUT.
_NEEDLE_STOPWORDS = frozenset({
    "that", "this", "with", "from", "then", "after", "and", "for", "was", "were", "verify",
    # instruction verbs: they describe what to DO, not what success looks like
    "open", "launch", "start", "close", "click", "type", "press", "scroll", "run", "use",
    # description glue from the planner's own `expected` sentences
    "state", "reflects", "reflect", "observable", "observed", "observe", "evidence",
    "current", "environment", "carried", "shows", "documented", "procedure",
    "alternative", "path", "reasoned", "answer", "grounded", "observations",
    "interactive", "elements", "goal", "achieved", "reached", "success",
    # domain nouns that name a CATEGORY: every screen has windows and apps
    "desktop", "application", "app", "program", "window", "screen", "computer",
    "ui", "page", "content", "results", "listing", "folder",
})


def _needles(text: str) -> tuple[str, ...]:
    """The DISTINCTIVE words of a goal or expectation: what it is about.

    Deliberately not "every word of four letters or more". These words are
    matched against screen text, window titles and page content, so a generic
    word ("desktop", "application", "window") matches almost any screen and
    turns verification into a formality — it would report a stubbed action as a
    completed goal. What survives is the noun the request names: the calculator,
    the folder, the repository. That is the only thing whose presence is
    evidence the goal was reached.
    """
    words = [
        word.lower()
        for word in re.split(r"[^A-Za-z0-9]+", text)
        if len(word) >= 4 and word.lower() not in _NEEDLE_STOPWORDS
    ]
    return tuple(dict.fromkeys(words))[:4]
