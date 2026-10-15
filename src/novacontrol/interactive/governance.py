"""Budgets and permissions — the checks that happen BEFORE an experiment runs.

Interactive learning is the phase most able to become an unrestricted action loop, so
the controls are in two places and both are consulted before anything executes:

``BudgetTracker``
    Counts experiments, actions, seconds, retries and cost units, and answers
    "does this one fit?" with the reason when it does not. The check is a *verdict*,
    not a counter read after the fact (§11/§18).

``SafetyGate``
    Says whether an action may run at all. A **simulator** action is allowed without
    asking, because it cannot affect anything outside this process — and it says so
    in the decision, so a caller can tell "allowed because safe" from "allowed because
    approved". A **real** action requires the permission layer to allow it AND an
    explicit approval; with no permission layer wired a real action is REFUSED rather
    than waved through, because "no gate" is not permission.

The policy in between is declared, not inferred: destructiveness, approval
requirements and the stop conditions all live in ``LearningPolicy`` so a report can
print the rules that were in force instead of describing the code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.interactive.environments import Environment
from novacontrol.interactive.models import (
    BudgetUsage,
    BudgetVerdict,
    EnvironmentKind,
    ExperimentSpec,
    LearningBudget,
    SafetyDecision,
    as_bool,
    as_int,
    clamp,
)

__all__ = [
    "BudgetTracker",
    "LearningPolicy",
    "SafetyGate",
]


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    """What interactive learning is allowed to do, stated once.

    The stop conditions live here too, so the manager's termination is a policy a
    reader can see rather than a loop with a condition buried inside it.
    """

    allow_destructive: bool = False
    require_approval_for_real: bool = True
    auto_approve_simulator: bool = True
    require_permission_layer_for_real: bool = True
    allow_unpredicted_experiments: bool = True
    stop_when_all_resolved: bool = True
    max_uninformative_in_a_row: int = 2
    require_evaluable_outcome: bool = False
    limitations: tuple[str, ...] = (
        "a simulator's result is evidence about the simulator, not about the real world",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_destructive": self.allow_destructive,
            "require_approval_for_real": self.require_approval_for_real,
            "auto_approve_simulator": self.auto_approve_simulator,
            "require_permission_layer_for_real": self.require_permission_layer_for_real,
            "allow_unpredicted_experiments": self.allow_unpredicted_experiments,
            "stop_when_all_resolved": self.stop_when_all_resolved,
            "max_uninformative_in_a_row": self.max_uninformative_in_a_row,
            "require_evaluable_outcome": self.require_evaluable_outcome,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LearningPolicy:
        defaults = cls()
        return cls(
            allow_destructive=as_bool(data.get("allow_destructive"), defaults.allow_destructive),
            require_approval_for_real=as_bool(
                data.get("require_approval_for_real"), defaults.require_approval_for_real
            ),
            auto_approve_simulator=as_bool(
                data.get("auto_approve_simulator"), defaults.auto_approve_simulator
            ),
            require_permission_layer_for_real=as_bool(
                data.get("require_permission_layer_for_real"),
                defaults.require_permission_layer_for_real,
            ),
            allow_unpredicted_experiments=as_bool(
                data.get("allow_unpredicted_experiments"), defaults.allow_unpredicted_experiments
            ),
            stop_when_all_resolved=as_bool(
                data.get("stop_when_all_resolved"), defaults.stop_when_all_resolved
            ),
            max_uninformative_in_a_row=max(
                1,
                as_int(data.get("max_uninformative_in_a_row"), defaults.max_uninformative_in_a_row),
            ),
            require_evaluable_outcome=as_bool(
                data.get("require_evaluable_outcome"), defaults.require_evaluable_outcome
            ),
            limitations=defaults.limitations,
        )


class BudgetTracker:
    """What one objective has spent, and whether one more experiment fits."""

    def __init__(self, budget: LearningBudget | None = None) -> None:
        self.budget = budget or LearningBudget()
        self.usage = BudgetUsage()

    def verdict(self, spec: ExperimentSpec) -> BudgetVerdict:
        """Whether this experiment fits, with what remains when it does not."""
        budget = self.budget
        remaining = {
            "experiments": max(0, budget.max_experiments - self.usage.experiments),
            "actions": max(0, budget.max_actions_per_experiment),
            "seconds": max(0.0, budget.max_seconds_total - self.usage.seconds),
            "cost_units": max(0.0, budget.max_cost_units - self.usage.cost_units),
            "retries": max(0, budget.max_retries - self.usage.retries),
        }
        if self.usage.experiments >= budget.max_experiments:
            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"the objective has run {self.usage.experiments} experiment(s), which is "
                    f"its ceiling ({budget.max_experiments})"
                ),
                remaining=remaining,
            )
        if self.usage.seconds >= budget.max_seconds_total:
            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"the objective has spent {self.usage.seconds:.2f}s of its "
                    f"{budget.max_seconds_total:.2f}s"
                ),
                remaining=remaining,
            )
        if self.usage.cost_units + spec.estimated_cost_units > budget.max_cost_units:
            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"this experiment ({spec.estimated_cost_units:.2f} cost units) would take "
                    f"the objective past its ceiling ({budget.max_cost_units:.2f})"
                ),
                remaining=remaining,
            )
        return BudgetVerdict(allowed=True, reason="", remaining=remaining)

    def record(
        self,
        *,
        actions: int = 0,
        seconds: float = 0.0,
        cost: float = 0.0,
        retries: int = 0,
        experiments: int = 0,
    ) -> BudgetUsage:
        """Charge what was actually spent. Only what happened is charged."""
        self.usage = BudgetUsage(
            experiments=self.usage.experiments + max(0, experiments),
            actions=self.usage.actions + max(0, actions),
            seconds=self.usage.seconds + max(0.0, seconds),
            retries=self.usage.retries + max(0, retries),
            cost_units=self.usage.cost_units + max(0.0, cost),
        )
        return self.usage

    def exhausted(self) -> tuple[bool, str]:
        """Whether the objective has no room left, and which ceiling stopped it."""
        budget = self.budget
        if self.usage.experiments >= budget.max_experiments:
            return True, (
                f"the experiment ceiling was reached ({self.usage.experiments}/"
                f"{budget.max_experiments})"
            )
        if self.usage.seconds >= budget.max_seconds_total:
            return True, (
                f"the time budget was spent ({self.usage.seconds:.2f}s of "
                f"{budget.max_seconds_total:.2f}s)"
            )
        if self.usage.cost_units >= budget.max_cost_units:
            return True, (
                f"the cost budget was spent ({self.usage.cost_units:.2f} of "
                f"{budget.max_cost_units:.2f})"
            )
        return False, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "exhausted": self.exhausted()[0],
        }

    def restore(self, usage: BudgetUsage) -> None:
        """Resume a stored usage — a restart must not hand back the budget it spent."""
        self.usage = BudgetUsage(
            experiments=max(0, usage.experiments),
            actions=max(0, usage.actions),
            seconds=max(0.0, usage.seconds),
            retries=max(0, usage.retries),
            cost_units=max(0.0, usage.cost_units),
        )


class SafetyGate:
    """Whether an experiment may act, asked before every meaningful action."""

    def __init__(
        self,
        *,
        policy: LearningPolicy | None = None,
        permissions: Any | None = None,
    ) -> None:
        self.policy = policy or LearningPolicy()
        self.permissions = permissions
        self.refusals = 0
        self.approvals = 0

    def evaluate(
        self,
        spec: ExperimentSpec,
        *,
        environment: Environment | None,
        approval: bool = False,
    ) -> SafetyDecision:
        """The decision, which names the layer that made it."""
        if environment is None:
            self.refusals += 1
            return SafetyDecision(
                allowed=False,
                reason=f"no environment {spec.environment_id!r} is registered",
                source="environment",
            )
        simulator_action = (
            environment.kind is EnvironmentKind.SIMULATOR
            and spec.simulator
            and not spec.destructive
        )
        if simulator_action and self.policy.auto_approve_simulator:
            self.approvals += 1
            return SafetyDecision(
                allowed=True,
                reason=(
                    "a simulator action cannot affect anything outside this process, so it "
                    "runs without approval"
                ),
                source="simulator",
                risk="low",
                requires_approval=False,
                approved=True,
                reversible=True,
                external_side_effect=False,
            )
        if spec.destructive and not self.policy.allow_destructive:
            self.refusals += 1
            return SafetyDecision(
                allowed=False,
                reason=(
                    "destructive experiments are refused by policy: a destructive action "
                    "is never approved, so no approval would change this"
                ),
                source="policy",
                risk="high",
                requires_approval=True,
                destructive=True,
                reversible=spec.reversible,
                external_side_effect=True,
            )
        decision = None
        if self.permissions is not None:
            decide = getattr(self.permissions, "assess", None)
            if callable(decide):
                decision = decide(
                    tool=spec.environment_id,
                    action=spec.action,
                    permissions=tuple(spec.permissions) or None,
                )
        if decision is None and self.policy.require_permission_layer_for_real:
            self.refusals += 1
            return SafetyDecision(
                allowed=False,
                reason=(
                    "no permission layer is wired, so authorization for a real action "
                    "cannot be established; nothing was attempted"
                ),
                source="policy",
                risk="unknown",
                requires_approval=True,
                destructive=spec.destructive,
                reversible=spec.reversible,
                external_side_effect=True,
            )
        risk = "low"
        reason = ""
        if decision is not None:
            risk = _risk_text(decision)
            if not bool(getattr(decision, "allow", True)):
                self.refusals += 1
                return SafetyDecision(
                    allowed=False,
                    reason=str(getattr(decision, "reason", "the permission layer refused")),
                    source="permissions",
                    risk=risk,
                    requires_approval=True,
                    destructive=bool(getattr(decision, "destructive", spec.destructive)),
                    reversible=bool(getattr(decision, "reversible", spec.reversible)),
                    external_side_effect=bool(getattr(decision, "external_side_effect", True)),
                )
            reason = str(getattr(decision, "reason", "")) or "the permission layer allowed it"
        if self.policy.require_approval_for_real and not approval:
            self.refusals += 1
            return SafetyDecision(
                allowed=False,
                reason=(
                    f"a real action ({spec.action!r} in {spec.environment_id!r}) requires "
                    f"explicit approval before it may run; nothing was attempted"
                ),
                source="approval",
                risk=risk,
                requires_approval=True,
                approved=False,
                destructive=spec.destructive,
                reversible=spec.reversible,
                external_side_effect=True,
            )
        if not spec.reversible and not self.policy.allow_destructive:
            self.refusals += 1
            return SafetyDecision(
                allowed=False,
                reason="an irreversible action needs a policy that allows destructive work",
                source="policy",
                risk="high",
                requires_approval=True,
                destructive=spec.destructive,
                reversible=False,
                external_side_effect=True,
            )
        self.approvals += 1
        return SafetyDecision(
            allowed=True,
            reason=reason or "approved for this run",
            source="permissions" if decision is not None else "approval",
            risk=risk,
            requires_approval=True,
            approved=True,
            destructive=spec.destructive,
            reversible=spec.reversible,
            external_side_effect=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "permissions_wired": self.permissions is not None,
            "approvals": self.approvals,
            "refusals": self.refusals,
        }


def _risk_text(decision: Any) -> str:
    """The risk a permission decision reported, as text."""
    risk = getattr(decision, "risk", None)
    value = getattr(risk, "value", None)
    return str(value) if value is not None else str(risk or "unknown")


def budget_from_mapping(data: Mapping[str, Any]) -> LearningBudget:
    """A budget from a request body — clamped by ``LearningBudget`` itself."""
    return LearningBudget.from_mapping(data)


def cost_of(seconds: float, actions: int, *, cost_per_second: float = 0.1) -> float:
    """A cost in units for one run: actions plus time, on a declared scale.

    The units are arbitrary — they exist so two runs can be compared and so a
    ceiling can be enforced. Nothing here claims to be money.
    """
    return clamp(actions * 1.0 + max(0.0, seconds) * cost_per_second, 0.0, 1_000.0)
