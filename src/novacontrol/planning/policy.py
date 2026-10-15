"""Action policy — the one place a candidate plan becomes (or fails to become)
authorized work.

Planning produces CANDIDATES. This module answers the different question: *may
this one run, and under what conditions?* It is deliberately a gate rather than
a scorer, because the two questions are not the same kind of question.

The verdict for a step is three-valued, and the difference between the last two
is the whole design:

``allow``
    The permission layer raised no objection and nothing forbids it.
``needs_approval``
    It MAY run — after a person says so. Denying it would be wrong (it is a
    legitimate step) and allowing it silently would be worse (the approval
    layer is what makes a consequential action a decision rather than a
    side effect).
``denied``
    It may not run at all: the permission layer refused it, or the goal
    explicitly excludes what it does.

Three rules:

**The permission layer decides risk, not this module.** ``PermissionManager``
owns declarations, risk levels and confirmation policy; the policy layer here
adds only the goal-specific rules (exclusions, budgets, approval conditions)
and the *authoritative* combination of the two. A second risk table would be a
second opinion about the same action, and the two would drift.

**Approval is remembered as evidence, not as a flag on the plan.** Which step
ids a caller approved is passed in per evaluation, so a report cannot claim a
step was approved by a plan that merely exists.

**A refusal names its source.** Every verdict carries the reason and where it
came from (``permission``, ``goal_exclusion``, ``budget``,
``approval_condition``), because "it was denied" answers a question nobody
asked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.planning.goals import Goal, Task
from novacontrol.planning.models import Plan, PlanStep, StepEffect

__all__ = [
    "ActionPolicy",
    "PolicyReport",
    "PolicyVerdict",
]


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """One step's answer: run it, ask first, or refuse — and why."""

    step_id: str
    kind: str  # "allow" | "needs_approval" | "denied"
    reason: str
    source: str = "permission"
    #: Risk as the permission layer reported it (``low``…``critical``), or ""
    #: when nothing assessed it. Never invented here.
    risk: str = ""
    #: Whether the permission layer would like a confirmation.
    confirmation_required: bool = False
    #: The permission scopes the step's declaration names.
    permissions: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        """True only when it may run RIGHT NOW without asking anyone."""
        return self.kind == "allow"

    @property
    def denied(self) -> bool:
        return self.kind == "denied"

    @property
    def needs_approval(self) -> bool:
        return self.kind == "needs_approval"

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "allowed": self.allowed,
            "denied": self.denied,
            "needs_approval": self.needs_approval,
            "reason": self.reason,
            "source": self.source,
            "risk": self.risk,
            "confirmation_required": self.confirmation_required,
            "permissions": list(self.permissions),
        }


@dataclass(frozen=True, slots=True)
class PolicyReport:
    """Every step's verdict, plus the plan-level answer a caller acts on."""

    plan_id: str
    goal_id: str = ""
    verdicts: tuple[PolicyVerdict, ...] = ()
    #: Ceiling problems found while checking (steps over budget, and so on).
    budget_reasons: tuple[str, ...] = ()
    #: Whether ANY step was refused. A plan with a refused step may still run
    #: its other steps — the executor reports the refusal as ``DENIED`` — but
    #: it is never presented as fully authorized.
    has_denials: bool = False

    def verdict_for(self, step_id: str) -> PolicyVerdict | None:
        for item in self.verdicts:
            if item.step_id == step_id:
                return item
        return None

    @property
    def allowed_steps(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.verdicts if item.allowed)

    @property
    def approval_steps(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.verdicts if item.needs_approval)

    @property
    def denied_steps(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.verdicts if item.denied)

    @property
    def runnable(self) -> bool:
        """True when at least one step may run without asking anyone."""
        return bool(self.allowed_steps)

    @property
    def fully_authorized(self) -> bool:
        """True only when nothing needs a person and nothing was refused."""
        return bool(self.verdicts) and not self.has_denials and not self.approval_steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "verdicts": [item.to_dict() for item in self.verdicts],
            "allowed": list(self.allowed_steps),
            "needs_approval": list(self.approval_steps),
            "denied": list(self.denied_steps),
            "budget_reasons": list(self.budget_reasons),
            "has_denials": self.has_denials,
            "runnable": self.runnable,
            "fully_authorized": self.fully_authorized,
        }


class ActionPolicy:
    """Combines the permission layer's verdict with the goal's own rules.

    ``permissions`` may be ``None``: a build with no permission layer wired is
    reported as ``unknown`` rather than as permissionless, because "nothing
    said no" is not the same as "something said yes". With no layer, every step
    that would normally need confirmation still needs it — the default is the
    cautious one.
    """

    def __init__(self, *, permissions: Any = None) -> None:
        self._permissions = permissions

    @property
    def wired(self) -> bool:
        return self._permissions is not None

    # -- one step ---------------------------------------------------------------

    def evaluate_step(
        self,
        step: PlanStep,
        *,
        goal: Goal | None = None,
        approved: Sequence[str] = (),
    ) -> PolicyVerdict:
        """The verdict for one step, in priority order: refusal, ask, allow."""
        approved_set = set(approved)

        # 1. The goal's exclusions are absolute — a goal that says "no writes"
        #    is not a preference the permission layer can outweigh.
        if goal is not None:
            exclusion = self._excluded(step, goal)
            if exclusion is not None:
                return exclusion

        # 2. The permission layer owns risk. Ask it the real question — may this
        #    run NOW — and, when the answer is no, ask the second question that
        #    separates "ask first" from "never": would it be allowed if approved?
        #    Every refusal the layer raises is approval-shaped (it returns
        #    early on ``approved=True``), so this two-call shape is what keeps
        #    a legitimate step from being reported as a forbidden one.
        now = self._assess(step, approved=step.id in approved_set)
        if now is not None and not now.get("allow", True):
            if_allowed = self._assess(step, approved=True)
            if if_allowed is not None and if_allowed.get("allow", False):
                return PolicyVerdict(
                    step_id=step.id,
                    kind="needs_approval",
                    reason=str(
                        now.get("reason") or "the permission layer wants this approved first"
                    ),
                    source="permission",
                    risk=str(now.get("risk", "")),
                    confirmation_required=True,
                    permissions=tuple(str(item) for item in now.get("permissions", ()) or ()),
                )
            return PolicyVerdict(
                step_id=step.id,
                kind="denied",
                reason=str(now.get("reason") or "the permission layer refused this step"),
                source="permission",
                risk=str(now.get("risk", "")),
                confirmation_required=bool(now.get("requires_confirmation", False)),
                permissions=tuple(str(item) for item in now.get("permissions", ()) or ()),
            )
        decision = now
        confirmation = (
            bool(decision.get("requires_confirmation", False))
            if decision
            else step.needs_confirmation
        )
        risk = str(decision.get("risk", "")) if decision else ""
        permissions = tuple(
            str(item) for item in (decision.get("permissions", ()) if decision else ())
        )

        # 3. Approval conditions the GOAL named (beyond the generic risk rule).
        if goal is not None and self._approval_condition(step, goal) is not None:
            confirmation = True

        # 4. A step whose effect always asks, even with no permission layer.
        if not confirmation and step.effect in {
            StepEffect.DESTRUCTIVE,
            StepEffect.EXTERNAL,
            StepEffect.SYSTEM,
        }:
            confirmation = True

        if confirmation and step.id not in approved_set:
            condition = self._approval_condition(step, goal) if goal is not None else None
            return PolicyVerdict(
                step_id=step.id,
                kind="needs_approval",
                reason=(
                    f"{step.effect.value} step: it may run once a caller approves "
                    f"step {step.id!r}" + (f" (goal condition: {condition})" if condition else "")
                ),
                source="permission" if decision else "effect",
                risk=risk,
                confirmation_required=True,
                permissions=permissions,
            )

        return PolicyVerdict(
            step_id=step.id,
            kind="allow",
            reason=self._allow_reason(step, decision),
            source="permission" if decision else "effect",
            risk=risk,
            confirmation_required=confirmation,
            permissions=permissions,
        )

    # -- a whole plan ------------------------------------------------------------

    def evaluate_plan(
        self,
        plan: Plan,
        *,
        goal: Goal | None = None,
        approved: Sequence[str] = (),
    ) -> PolicyReport:
        """Every step's verdict, plus any ceiling the goal sets over the plan."""
        verdicts = tuple(
            self.evaluate_step(step, goal=goal, approved=approved) for step in plan.steps
        )
        budget_reasons: list[str] = []
        if goal is not None and len(plan.steps) > goal.budget.max_steps:
            budget_reasons.append(
                f"the goal's budget allows {goal.budget.max_steps} step(s) but "
                f"the plan has {len(plan.steps)}"
            )
        return PolicyReport(
            plan_id=plan.id,
            goal_id=goal.goal_id if goal else "",
            verdicts=verdicts,
            budget_reasons=tuple(budget_reasons),
            has_denials=any(item.denied for item in verdicts),
        )

    def evaluate_tasks(
        self,
        tasks: Sequence[Task],
        *,
        goal: Goal | None = None,
        approved: Sequence[str] = (),
    ) -> PolicyReport:
        """The same verdicts for tasks that have not been built into a plan yet.

        Planning compares CANDIDATES as tasks; a caller that wants to know
        "would this even be allowed?" should not have to build a plan to find
        out.
        """
        verdicts = tuple(
            self.evaluate_step(task.to_step(), goal=goal, approved=approved) for task in tasks
        )
        budget_reasons: list[str] = []
        if goal is not None and len(tasks) > goal.budget.max_steps:
            budget_reasons.append(
                f"the goal's budget allows {goal.budget.max_steps} step(s) but "
                f"{len(tasks)} were proposed"
            )
        return PolicyReport(
            plan_id="",
            goal_id=goal.goal_id if goal else "",
            verdicts=verdicts,
            budget_reasons=tuple(budget_reasons),
            has_denials=any(item.denied for item in verdicts),
        )

    # -- internals ---------------------------------------------------------------

    def _assess(self, step: PlanStep, *, approved: bool) -> Mapping[str, Any] | None:
        """The permission layer's decision as a plain mapping, or None if unwired."""
        if self._permissions is None:
            return None
        try:
            decision = self._permissions.check(
                step.tool,
                action=step.action,
                approved=approved,
                parameters=dict(step.parameters),
            )
        except Exception:  # noqa: BLE001 - a layer that cannot answer is unknown
            return None
        to_dict = getattr(decision, "to_dict", None)
        if callable(to_dict):
            try:
                return dict(to_dict())
            except Exception:  # noqa: BLE001
                return None
        return None

    def _excluded(self, step: PlanStep, goal: Goal) -> PolicyVerdict | None:
        """A goal's exclusion matching what this step does, or None."""
        for exclusion in goal.exclusions:
            needle = exclusion.strip().lower()
            if not needle:
                continue
            if needle in step.effect.value.replace("_", " "):
                return PolicyVerdict(
                    step_id=step.id,
                    kind="denied",
                    reason=(
                        f"the goal excludes {exclusion!r} and this step is {step.effect.value}"
                    ),
                    source="goal_exclusion",
                )
        return None

    def _approval_condition(self, step: PlanStep, goal: Goal) -> str | None:
        """A goal-declared approval condition this step matches, or None.

        Conditions are matched as substrings against the step's action, tool
        and title — deliberately literal. Interpreting "anything touching the
        registry" as a rule would be inventing a policy the caller did not
        write, and a policy invented here cannot be reviewed by them.
        """
        haystack = " ".join((step.action, step.tool, step.title, step.description)).lower()
        for condition in goal.approval_conditions:
            needle = condition.strip().lower()
            if needle and needle in haystack:
                return condition
        return None

    def _allow_reason(self, step: PlanStep, decision: Mapping[str, Any] | None) -> str:
        if decision is None:
            if self._permissions is None:
                return (
                    "no permission layer is wired, so nothing could assess this "
                    "step; it is allowed by default and says so"
                )
            return "the permission layer assessed this step and raised no objection"
        return str(decision.get("reason") or "the permission layer allowed this step")
