"""Exploration: trying something else ON PURPOSE, inside a budget and a mask.

Exploration is where an agentic system can do damage, so the rules are part of
the design rather than a configuration:

  * exploration only ever chooses from the actions the mask ALLOWED. A masked
    action cannot be explored into, so an unsafe or unapproved action is not
    reachable by "trying something new".
  * exploration never selects an action that requires confirmation, is
    irreversible, or is rated HIGH risk — even when the mask kept it because a
    person is available to approve it. Asking a person to approve an action
    chosen at random wastes their attention, and a policy that does that learns
    to spam approvals.
  * a budget stops it. Actions, failed actions, accumulated cost, the share of
    steps spent exploring and any safety intervention each have a limit, and
    reaching any one of them ends exploration for the run while leaving the
    episode running greedily.

Four strategies are implemented — greedy, epsilon-greedy, temperature and
bounded stochastic — and each is seeded, so the same run explores the same way
twice and a test can assert exactly which action exploration picked.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.agentic.config import ExplorationConfig
from novacontrol.agentic.models import (
    AgentAction,
    AgentState,
    ExplorationStrategy,
    PolicyDecision,
    PolicyReasonCode,
    as_int,
    as_mapping,
    as_real,
    as_text,
)
from novacontrol.core.security import RiskLevel

#: Why exploration stopped. ``""`` means it has not.
STOP_BUDGET = "maximum_exploration_actions_reached"
STOP_RATE = "maximum_exploration_rate_reached"
STOP_FAILURES = "too_many_failed_exploration_actions"
STOP_SAFETY = "safety_intervention"
STOP_COST = "exploration_cost_budget_reached"
STOP_DISABLED = "exploration_is_disabled"

STOP_REASONS: tuple[str, ...] = (
    STOP_BUDGET,
    STOP_RATE,
    STOP_FAILURES,
    STOP_SAFETY,
    STOP_COST,
    STOP_DISABLED,
)

#: Risk levels exploration may never choose. HIGH and CRITICAL are excluded, and
#: so is anything irreversible or needing confirmation.
EXPLORABLE_RISKS: frozenset[RiskLevel] = frozenset({RiskLevel.LOW, RiskLevel.MEDIUM})


@dataclass
class ExplorationBudget:
    """The counters that bound exploration, and the rule that stops it."""

    max_actions: int = 64
    max_rate: float = 0.25
    max_failed: int = 8
    max_safety_interventions: int = 1
    cost_per_action: float = 0.02
    max_cost: float = 2.0

    actions: int = 0
    failed: int = 0
    reward_gained: float = 0.0
    cost: float = 0.0
    safety_interventions: int = 0
    consecutive: int = 0
    steps: int = 0
    stopped_reason: str = ""

    @classmethod
    def from_config(cls, config: ExplorationConfig) -> ExplorationBudget:
        return cls(
            max_actions=max(0, int(config.max_exploration_actions)),
            max_rate=max(0.0, min(1.0, float(config.max_exploration_rate))),
            max_failed=max(0, int(config.max_failed_exploration)),
            max_safety_interventions=max(0, int(config.max_safety_interventions)),
            cost_per_action=max(0.0, float(config.cost_per_action)),
            max_cost=max(0.0, float(config.max_cost)),
        )

    @property
    def stopped(self) -> bool:
        return bool(self.stopped_reason)

    def rate(self) -> float:
        return (self.actions / self.steps) if self.steps else 0.0

    def record_step(self, *, explored: bool, failed: bool = False, reward: float = 0.0, unsafe: bool = False) -> None:
        """One step happened. Exploration counters move only for explored steps."""
        self.steps += 1
        if unsafe:
            self.safety_interventions += 1
        if not explored:
            self.consecutive = 0
            return
        self.actions += 1
        self.consecutive += 1
        self.cost = math.fsum([self.cost, self.cost_per_action])
        self.reward_gained = math.fsum([self.reward_gained, float(reward)])
        if failed:
            self.failed += 1

    def stop_reason(self) -> str:
        """Whether exploration must stop now, and why (empty means it may run)."""
        if self.stopped_reason:
            return self.stopped_reason
        if self.max_safety_interventions >= 0 and self.safety_interventions >= max(
            1, self.max_safety_interventions
        ):
            return STOP_SAFETY
        if self.actions >= self.max_actions:
            return STOP_BUDGET
        if self.failed >= self.max_failed:
            return STOP_FAILURES
        if self.cost >= self.max_cost:
            return STOP_COST
        if self.steps and self.rate() > self.max_rate and self.actions > 1:
            return STOP_RATE
        return ""

    def suspend(self, reason: str) -> str:
        """Stop exploration for the rest of the run, recording why."""
        if not self.stopped_reason:
            self.stopped_reason = as_text(reason, STOP_DISABLED)
        return self.stopped_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": self.actions,
            "steps": self.steps,
            "rate": round(self.rate(), 6),
            "failed": self.failed,
            "reward_gained": round(self.reward_gained, 6),
            "cost": round(self.cost, 6),
            "safety_interventions": self.safety_interventions,
            "consecutive": self.consecutive,
            "stopped": self.stopped,
            "stopped_reason": self.stopped_reason,
            "limits": {
                "max_actions": self.max_actions,
                "max_rate": self.max_rate,
                "max_failed": self.max_failed,
                "max_safety_interventions": self.max_safety_interventions,
                "max_cost": self.max_cost,
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExplorationBudget:
        rows = as_mapping(data)
        limits = as_mapping(rows.get("limits"))
        return cls(
            max_actions=as_int(limits.get("max_actions"), 64),
            max_rate=as_real(limits.get("max_rate"), 0.25) or 0.0,
            max_failed=as_int(limits.get("max_failed"), 8),
            max_safety_interventions=as_int(limits.get("max_safety_interventions"), 1),
            max_cost=as_real(limits.get("max_cost"), 2.0) or 0.0,
            actions=as_int(rows.get("actions")),
            failed=as_int(rows.get("failed")),
            reward_gained=as_real(rows.get("reward_gained"), 0.0) or 0.0,
            cost=as_real(rows.get("cost"), 0.0) or 0.0,
            safety_interventions=as_int(rows.get("safety_interventions")),
            consecutive=as_int(rows.get("consecutive")),
            steps=as_int(rows.get("steps")),
            stopped_reason=as_text(rows.get("stopped_reason")),
        )


@dataclass(frozen=True, slots=True)
class ExplorationDecision:
    """The decision the policy should take, and whether exploration made it."""

    decision: PolicyDecision = field(default_factory=PolicyDecision)
    explored: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "explored": self.explored,
            "reason": self.reason,
        }


class ExplorationPolicy:
    """A seeded exploration layer over whatever decision a policy produced."""

    def __init__(
        self,
        config: ExplorationConfig | None = None,
        *,
        budget: ExplorationBudget | None = None,
        seed: int | None = None,
    ) -> None:
        self.config = config if config is not None else ExplorationConfig()
        self.budget = budget if budget is not None else ExplorationBudget.from_config(self.config)
        self.seed = int(self.config.seed if seed is None else seed)
        self._draws = 0

    @property
    def strategy(self) -> str:
        return self.config.strategy

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled) and self.config.strategy != ExplorationStrategy.GREEDY.value

    def reset(self) -> None:
        """A new episode: the exploration counters stay, the consecutive run does not."""
        self.budget.consecutive = 0
        self._draws = 0

    # -- choosing ---------------------------------------------------------------

    def explore(
        self,
        base: PolicyDecision,
        actions: Sequence[AgentAction],
        *,
        state: AgentState | None = None,
    ) -> ExplorationDecision:
        """Whether to keep the policy's choice or try another allowed action."""
        del state
        if not self.enabled:
            return ExplorationDecision(decision=base, explored=False, reason=STOP_DISABLED)
        stopped = self.budget.stop_reason()
        if stopped:
            self.budget.suspend(stopped)
            return ExplorationDecision(decision=base, explored=False, reason=stopped)
        if base.selected_action is None:
            # A policy that selected nothing is NOT overridden into exploring:
            # "no action" is a real answer (the mask was empty).
            return ExplorationDecision(decision=base, explored=False, reason="no_action_to_replace")
        candidates = self._explorable(actions)
        if len(candidates) < 2:
            return ExplorationDecision(
                decision=base,
                explored=False,
                reason="fewer than two safe actions are available to explore",
            )
        if self.config.strategy == ExplorationStrategy.EPSILON_GREEDY.value:
            if self._draw() >= self.config.epsilon:
                return ExplorationDecision(
                    decision=base, explored=False, reason="epsilon draw kept the policy's choice"
                )
            chosen = self._pick(candidates, base)
        elif self.config.strategy == ExplorationStrategy.TEMPERATURE.value:
            chosen = self._sample_temperature(candidates, base)
        elif self.config.strategy == ExplorationStrategy.BOUNDED_STOCHASTIC.value:
            if self.budget.consecutive >= self.config.max_exploration_actions:
                self.budget.suspend(STOP_BUDGET)
                return ExplorationDecision(decision=base, explored=False, reason=STOP_BUDGET)
            chosen = self._pick(candidates, base)
        else:
            return ExplorationDecision(decision=base, explored=False, reason=STOP_DISABLED)
        if chosen is None or chosen.action_id == base.selected_action.action_id:
            return ExplorationDecision(
                decision=base, explored=False, reason="exploration drew the same action"
            )
        explored = replace(
            base,
            selected_action=chosen,
            confidence=min(base.confidence, 0.4),
            reason_code=PolicyReasonCode.EXPLORATION.value,
            exploration_metadata={
                "strategy": self.strategy,
                "explored": True,
                "epsilon": self.config.epsilon,
                "temperature": self.config.temperature,
                "budget": self.budget.to_dict(),
                "replaced": (
                    base.selected_action.name if base.selected_action is not None else ""
                ),
            },
            notes=(
                *base.notes,
                f"exploration selected {chosen.name!r} instead of the policy's choice",
            ),
        )
        return ExplorationDecision(
            decision=explored,
            explored=True,
            reason=f"explored with strategy {self.strategy}",
        )

    def record_outcome(
        self, *, explored: bool, failed: bool = False, reward: float = 0.0, unsafe: bool = False
    ) -> str:
        """Tell the budget what the step did. Returns the stop reason, if any."""
        self.budget.record_step(explored=explored, failed=failed, reward=reward, unsafe=unsafe)
        if unsafe:
            return self.budget.suspend(STOP_SAFETY)
        reason = self.budget.stop_reason()
        if reason:
            return self.budget.suspend(reason)
        return ""

    def describe(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "enabled": self.enabled,
            "epsilon": self.config.epsilon,
            "temperature": self.config.temperature,
            "seed": self.seed,
            "budget": self.budget.to_dict(),
            "never_explores": sorted(level.value for level in RiskLevel if level not in EXPLORABLE_RISKS),
            "note": (
                "exploration chooses only from the actions the mask allowed, and never "
                "from an action that needs confirmation, is irreversible or is rated "
                "HIGH or CRITICAL"
            ),
        }

    # -- helpers ----------------------------------------------------------------

    def _explorable(self, actions: Sequence[AgentAction]) -> list[AgentAction]:
        return [
            action
            for action in actions
            if action.risk_level in EXPLORABLE_RISKS
            and action.reversible
            and not action.requires_confirmation
        ]

    def _pick(self, candidates: Sequence[AgentAction], base: PolicyDecision) -> AgentAction | None:
        preferred = [
            action
            for action in candidates
            if base.selected_action is None or action.action_id != base.selected_action.action_id
        ]
        pool = preferred or list(candidates)
        if not pool:
            return None
        self._draws += 1
        generator = random.Random(self.seed + self._draws)
        return pool[generator.randrange(len(pool))]

    def _sample_temperature(
        self, candidates: Sequence[AgentAction], base: PolicyDecision
    ) -> AgentAction | None:
        pool = [
            action
            for action in candidates
            if base.selected_action is None or action.action_id != base.selected_action.action_id
        ]
        if not pool:
            return None
        temperature = max(1e-6, float(self.config.temperature))
        values = [
            float(base.value_estimates.get(action.action_id, 0.0)) for action in pool
        ]
        # A numerically stable softmax: subtract the maximum before exponentiating
        # so a large value cannot overflow the exponent.
        top = max(values) if values else 0.0
        weights = [math.exp((value - top) / temperature) for value in values]
        total = math.fsum(weights)
        self._draws += 1
        generator = random.Random(self.seed + self._draws)
        needle = generator.random() * total
        running = 0.0
        for action, weight in zip(pool, weights, strict=False):
            running += weight
            if needle <= running:
                return action
        return pool[-1]

    def _draw(self) -> float:
        """The epsilon draw, seeded by the draw count so it is reproducible."""
        self._draws += 1
        generator = random.Random(self.seed + self._draws)
        return generator.random()


def exploration_budget_from(config: Mapping[str, Any]) -> ExplorationBudget:
    """A budget read from a stored mapping (a run's records)."""
    return ExplorationBudget.from_dict(config)


__all__ = [
    "EXPLORABLE_RISKS",
    "STOP_BUDGET",
    "STOP_COST",
    "STOP_DISABLED",
    "STOP_FAILURES",
    "STOP_RATE",
    "STOP_REASONS",
    "STOP_SAFETY",
    "ExplorationBudget",
    "ExplorationDecision",
    "ExplorationPolicy",
    "exploration_budget_from",
]
