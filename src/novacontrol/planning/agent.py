"""The agent loop: understand, decide, plan, execute, observe, verify, recover.

Each of those verbs already exists as a layer. This is the thing that drives
them in order and, crucially, the thing that does not skip the last three:

    UNDERSTAND   what did the user actually ask for?
    DECIDE       what should be done about it?            (Phase 3)
    PLAN         which steps reach the goal?              (the compiler)
    EXECUTE      run them, in dependency order            (the executor)
    OBSERVE      what actually came back?
    VERIFY       was each result checked, or merely claimed?
    RECOVER      continue / retry / escalate — one bounded decision
    FINAL        say what happened, including what did not

The loop is where an agent most easily starts lying: it wants to finish. So
three rules are structural rather than stylistic. Nothing is marked done on the
strength of a step returning — the verification result is the only evidence.
The recovery cycle is BOUNDED by ``max_cycles``; a loop that can always try
again is a loop that can never stop. And a warning that a step could not be
verified is not swallowed on the way to the final response: it is carried into
it, because the user asked for an outcome, not a mood.

Every collaborator is injected — understanding, deciding, planning, executing,
summarising, escalating. The loop therefore has no opinion about models, tools
or the machine, and can be driven end to end in a test by five fake callables.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import isawaitable
from typing import Any

from novacontrol.decision.models import Decision, DecisionRoute
from novacontrol.planning.models import (
    Plan,
    PlanStatus,
    PlanStepStatus,
    WorkflowResult,
)

#: Injectable collaborators. Each may be sync or async.
UnderstandFn = Callable[[str], Any]
DecideFn = Callable[[Any], Any]
PlanFn = Callable[[str, Any, Decision], Any]
ExecuteFn = Callable[[Plan], Any]
SummarizeFn = Callable[[str, Plan, WorkflowResult], Any]
ReplanFn = Callable[[Plan, WorkflowResult], Any]
EscalateFn = Callable[[str, Plan, WorkflowResult], Any]

#: How many recovery cycles a run may take. Small on purpose: a plan that has
#: failed three times needs a person or a different request, not a fourth try.
DEFAULT_MAX_CYCLES = 3


class AgentPhase(StrEnum):
    """The phase a run is in — the loop's own state, not the plan's."""

    UNDERSTAND = "understand"
    DECIDE = "decide"
    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    VERIFY = "verify"
    RECOVER = "recover"
    CLARIFY = "clarify"
    ESCALATE = "escalate"
    FINAL = "final_response"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One phase transition, recorded so a run can be explained after the fact."""

    phase: AgentPhase
    detail: str
    cycle: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "detail": self.detail,
            "cycle": self.cycle,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class AgentRun:
    """The whole run: what was planned, what ran, what was proven, and what was said."""

    goal: str
    phase: AgentPhase
    response: str
    plan: Plan | None = None
    workflow: WorkflowResult | None = None
    decision: Decision | None = None
    events: tuple[AgentEvent, ...] = ()
    cycles: int = 0
    stopped_because: str = ""
    #: True only when every step that required a check passed one.
    verified: bool = False
    #: True when the run handed the problem upward for reasoning.
    escalated: bool = False

    @property
    def succeeded(self) -> bool:
        """A run succeeded when it executed, finished, and proved its results."""
        if self.workflow is None:
            return False
        return (
            self.workflow.status is PlanStatus.COMPLETED
            and self.workflow.executed
            and not self.workflow.unverified_steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "phase": self.phase.value,
            "response": self.response,
            "cycles": self.cycles,
            "stopped_because": self.stopped_because,
            "verified": self.verified,
            "escalated": self.escalated,
            "succeeded": self.succeeded,
            "plan": self.plan.to_dict() if self.plan else None,
            "workflow": self.workflow.to_dict() if self.workflow else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "trace": [event.to_dict() for event in self.events],
        }


class AgentLoop:
    """Drives understand → decide → plan → execute → observe → verify → recover."""

    def __init__(
        self,
        *,
        understand: UnderstandFn,
        decide: DecideFn,
        plan: PlanFn,
        execute: ExecuteFn,
        summarize: SummarizeFn | None = None,
        replan: ReplanFn | None = None,
        escalate: EscalateFn | None = None,
        max_cycles: int = DEFAULT_MAX_CYCLES,
    ) -> None:
        self.understand = understand
        self.decide = decide
        self.plan = plan
        self.execute = execute
        self.summarize = summarize
        #: Recovery: asked for a NEW plan when execution failed and something
        #: is still worth trying. Returning None (or absent) means "stop", which
        #: is why the loop cannot spin: it needs help to continue.
        self.replan = replan
        self.escalate = escalate
        self.max_cycles = max(1, max_cycles)

    async def run(self, goal: str) -> AgentRun:
        """Run one goal through the whole loop, and report honestly."""
        events: list[AgentEvent] = []

        understood = await _call(self.understand, goal)
        intent = getattr(understood, "intent", None)
        events.append(
            AgentEvent(
                AgentPhase.UNDERSTAND,
                f"resolved intent {getattr(intent, 'intent', 'unknown')}",
                data={"strategy": str(getattr(understood, "strategy", ""))},
            )
        )

        decision = await _call(self.decide, understood)
        events.append(
            AgentEvent(
                AgentPhase.DECIDE,
                decision.reason or f"route {decision.route.value}",
                data={"route": decision.route.value, "reason_code": decision.reason_code.value},
            )
        )

        # A decision that says "ask first" ends the run here. Planning work the
        # user did not describe is how a question turns into an action nobody
        # authorized.
        if decision.route is DecisionRoute.CLARIFY:
            events.append(
                AgentEvent(AgentPhase.CLARIFY, "clarification requested instead of acting")
            )
            response = decision.reason or "Could you say a little more about what you want?"
            return self._finish(
                goal, AgentPhase.CLARIFY, response, events, 0, "the request needs clarifying",
                decision=decision,
            )

        plan = await _call(self.plan, goal, understood, decision)
        events.append(
            AgentEvent(
                AgentPhase.PLAN,
                f"{len(plan.steps)} step(s) planned",
                data={"steps": [step.id for step in plan.steps]},
            )
        )
        if plan.needs_clarification:
            events.append(
                AgentEvent(AgentPhase.CLARIFY, "the plan needs clarifying before it can run")
            )
            return self._finish(
                goal,
                AgentPhase.CLARIFY,
                "I need one more detail before I can carry that out.",
                events,
                0,
                "the plan needs clarifying",
                plan=plan,
                decision=decision,
            )

        cycle = 0
        workflow: WorkflowResult | None = None
        while cycle < self.max_cycles:
            cycle += 1
            workflow = await _call(self.execute, plan)
            events.append(
                AgentEvent(
                    AgentPhase.EXECUTE,
                    workflow.summary or f"workflow {workflow.status.value}",
                    cycle=cycle,
                    data={"status": workflow.status.value, "executed": workflow.executed},
                )
            )

            observed = _observe(workflow)
            events.append(
                AgentEvent(
                    AgentPhase.OBSERVE,
                    f"{observed['completed']} completed, {observed['failed']} failed, "
                    f"{observed['blocked']} not attempted",
                    cycle=cycle,
                    data=observed,
                )
            )

            verified = _verify(workflow)
            events.append(
                AgentEvent(
                    AgentPhase.VERIFY,
                    verified["detail"],
                    cycle=cycle,
                    data=verified,
                )
            )

            if workflow.status is PlanStatus.COMPLETED:
                response = await self._say(goal, plan, workflow, escalated=False)
                return self._finish(
                    goal,
                    AgentPhase.FINAL,
                    response,
                    events,
                    cycle,
                    "every step completed",
                    plan=plan,
                    workflow=workflow,
                    decision=decision,
                    verified=bool(verified["verified"]),
                )

            # Failure. One bounded decision: retry with a new plan, hand it
            # upward, or stop.
            recovery = _recoverable(workflow)
            if recovery["escalate"] and self.escalate is not None and cycle < self.max_cycles:
                events.append(
                    AgentEvent(
                        AgentPhase.ESCALATE,
                        recovery["reason"],
                        cycle=cycle,
                        data=recovery,
                    )
                )
                response = await _call(self.escalate, goal, plan, workflow)
                return self._finish(
                    goal,
                    AgentPhase.ESCALATE,
                    str(response),
                    events,
                    cycle,
                    recovery["reason"],
                    plan=plan,
                    workflow=workflow,
                    decision=decision,
                    escalated=True,
                )

            if self.replan is None or cycle >= self.max_cycles:
                response = await self._say(goal, plan, workflow, escalated=False)
                # Running out of cycles is a different stop from having nothing
                # left to try, and saying which one it was is the difference
                # between a report and an excuse.
                stopped = recovery["reason"]
                if self.replan is not None and cycle >= self.max_cycles:
                    stopped = f"cycle limit reached after {cycle} cycle(s): {stopped}"
                events.append(
                    AgentEvent(
                        AgentPhase.RECOVER,
                        f"stopping after {cycle} cycle(s): {stopped}",
                        cycle=cycle,
                        data=recovery,
                    )
                )
                return self._finish(
                    goal,
                    AgentPhase.FINAL,
                    response,
                    events,
                    cycle,
                    stopped,
                    plan=plan,
                    workflow=workflow,
                    decision=decision,
                )

            replacement = await _call(self.replan, plan, workflow)
            events.append(
                AgentEvent(
                    AgentPhase.RECOVER,
                    "replanned" if replacement is not None else "no recovery available",
                    cycle=cycle,
                    data={"replanned": replacement is not None},
                )
            )
            if replacement is None or replacement.needs_clarification:
                response = await self._say(goal, plan, workflow, escalated=False)
                return self._finish(
                    goal,
                    AgentPhase.FINAL,
                    response,
                    events,
                    cycle,
                    "no recovery was possible",
                    plan=plan,
                    workflow=workflow,
                    decision=decision,
                )
            plan = replacement

        # Unreachable while the loop is bounded, but an exit that reports
        # honestly beats one that assumes completion.
        assert workflow is not None
        return self._finish(
            goal,
            AgentPhase.FINAL,
            await self._say(goal, plan, workflow, escalated=False),
            events,
            cycle,
            "cycle limit reached",
            plan=plan,
            workflow=workflow,
            decision=decision,
        )

    # -- response -------------------------------------------------------------

    async def _say(
        self, goal: str, plan: Plan, workflow: WorkflowResult, *, escalated: bool
    ) -> str:
        """The user-facing summary: the model's when there is one, else the facts.

        The deterministic fallback is not a placeholder — it is built from the
        workflow result, so it names what failed and what could not be verified.
        """
        if self.summarize is not None:
            try:
                said = await _call(self.summarize, goal, plan, workflow)
                if said:
                    return str(said)
            except Exception:
                # A summariser that fails costs the run its polish, not its
                # outcome: the facts below are still the truth.
                pass
        return _fallback_response(goal, workflow)

    def _finish(
        self,
        goal: str,
        phase: AgentPhase,
        response: str,
        events: list[AgentEvent],
        cycles: int,
        stopped_because: str,
        *,
        plan: Plan | None = None,
        workflow: WorkflowResult | None = None,
        decision: Decision | None = None,
        verified: bool = False,
        escalated: bool = False,
    ) -> AgentRun:
        events.append(
            AgentEvent(
                AgentPhase.FINAL,
                response[:160],
                cycle=cycles,
                data={"stopped_because": stopped_because},
            )
        )
        return AgentRun(
            goal=goal,
            phase=phase,
            response=response,
            plan=plan,
            workflow=workflow,
            decision=decision,
            events=tuple(events),
            cycles=cycles,
            stopped_because=stopped_because,
            verified=verified,
            escalated=escalated,
        )


# --------------------------------------------------------------------------- #
# Reading a workflow result
# --------------------------------------------------------------------------- #


def _observe(workflow: WorkflowResult) -> dict[str, Any]:
    counts = {status.value: 0 for status in PlanStepStatus}
    for outcome in workflow.step_results.values():
        counts[outcome.status.value] = counts.get(outcome.status.value, 0) + 1
    return {
        "completed": counts.get(PlanStepStatus.COMPLETED.value, 0),
        "unverified": counts.get(PlanStepStatus.UNVERIFIED.value, 0),
        "failed": counts.get(PlanStepStatus.FAILED.value, 0)
        + counts.get(PlanStepStatus.DENIED.value, 0),
        "blocked": counts.get(PlanStepStatus.BLOCKED.value, 0),
        "status": workflow.status.value,
        "executed": workflow.executed,
    }


def _verify(workflow: WorkflowResult) -> dict[str, Any]:
    """What the run can and cannot claim, stated as counts rather than a mood."""
    unverified = list(workflow.unverified_steps)
    if unverified:
        detail = (
            f"{len(unverified)} step(s) ran but were not verified: "
            f"{', '.join(unverified)}"
        )
    elif workflow.verified:
        detail = "every step that needed checking passed its check"
    elif not workflow.executed:
        detail = "nothing was executed, so there is nothing to verify"
    else:
        detail = "no step in this plan required verification"
    return {
        "verified": bool(workflow.verified and not unverified),
        "unverified": unverified,
        "detail": detail,
    }


def _recoverable(workflow: WorkflowResult) -> dict[str, Any]:
    """Whether a failed run has anything left worth trying."""
    failures = {
        step_id: outcome
        for step_id, outcome in workflow.step_results.items()
        if outcome.status in (PlanStepStatus.FAILED, PlanStepStatus.DENIED)
    }
    if not failures:
        return {
            "escalate": False,
            "reason": "no step is in a state this loop can retry",
            "failed_steps": [],
        }
    escalate = any(
        outcome.output.get("escalate") for outcome in failures.values()
    ) or any(
        outcome.error is not None
        and outcome.error.kind.value in ("missing_dependency", "unsupported")
        for outcome in failures.values()
    )
    denied = any(outcome.status is PlanStepStatus.DENIED for outcome in failures.values())
    first = next(iter(failures.values()))
    reason = (
        "a required action was not authorized"
        if denied
        else (first.error.message if first.error is not None else "a step failed")
    )
    return {
        # Escalation is for problems that need REASONING. A refusal is not one:
        # only a person can change it, so it is reported, not escalated.
        "escalate": escalate and not denied,
        "reason": reason,
        "failed_steps": sorted(failures),
    }


def _fallback_response(goal: str, workflow: WorkflowResult) -> str:
    lines: list[str] = []
    if workflow.status is PlanStatus.COMPLETED:
        lines.append(f"Done: {len(workflow.step_results)} step(s) for “{goal}”.")
        if workflow.unverified_steps:
            lines.append(
                "These ran but could not be verified, so treat them as unconfirmed: "
                + ", ".join(workflow.unverified_steps)
                + "."
            )
    elif workflow.status is PlanStatus.BLOCKED:
        lines.append(
            "I stopped rather than guess: a step needed authorization it did not get."
        )
    else:
        lines.append(f"I could not finish “{goal}”.")
    for step_id, message in workflow.errors.items():
        lines.append(f"- {step_id}: {message}")
    return "\n".join(lines)


async def _call(function: Any, *args: Any) -> Any:
    value = function(*args)
    if isawaitable(value):
        value = await value
    return value
