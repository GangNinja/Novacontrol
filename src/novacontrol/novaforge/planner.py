"""NovaForge game planning and action selection (NF-20).

NF-15 observes, NF-16 reads state, NF-17 interprets objectives, NF-18 remembers
verified knowledge and NF-19 sends ONE bounded input. This module is the missing
step between them: it turns those readings into a *plan* — a short, ordered,
inspectable list of supported control actions — and then submits that plan
through the NF-19 interface, one action at a time, stopping the moment its
evidence stops supporting it.

What this module deliberately is not
------------------------------------
* It is **not** a second control path. Every step becomes an
  :class:`~novacontrol.novaforge.control.ControlAction` and is executed by the
  session's own :class:`~novacontrol.novaforge.control.GameControlService`; the
  planner never touches an injector, a window handle or a keycode itself.
* It is **not** an autonomous gameplay loop. A plan is bounded in steps, in
  deadline and in the session's action budget; execution stops on the first
  refusal, failure, stale observation or material state change. Nothing is
  retried automatically.
* It is **not** an outcome verifier. ``effect_verified`` is always ``False``;
  a plan that executed every step reports that the *input operations*
  completed, never that the game reacted. NF-21 owns effects.

Where candidate actions come from
---------------------------------
Only two sources, both inspectable:

1. **Declared plan rules** (:class:`PlanRuleRegistry`). A rule is configuration,
   not perception: it names a game/profile, an objective shape, a supported
   control and the HUD fields it needs. This build ships ONE rule, for the
   NF-17 *fixture* mission scenario (a labelled state fixture, not a commercial
   game). No rule exists for any real game, because this build has not verified
   one — an empty rule set is an honest ``no_action`` answer.
2. **Verified knowledge** from NF-18: a ``control`` record that is VERIFIED, has
   a verified *effect* and carries an explicit input binding (``key``) plus a
   declared objective match (``supports`` tokens or an ``objective`` phrase).
   A verified effect without a binding is reported as an unbound control, never
   guessed into a key press.

Scoring is a declared weighted sum over named components and is labelled
heuristic. It is a ranking, not a probability; candidates never carry a
fabricated confidence.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from .control import (
    CONTROL_KINDS,
    ControlAction,
    ControlKind,
    ControlPolicy,
    ControlStatus,
    ControlTarget,
)
from .knowledge import KnowledgeCategory, KnowledgeRecord, KnowledgeStatus
from .objectives import ObjectiveCategory, ObjectiveState, ObjectiveStatus, UnderstoodObjective
from .state import GameState, HudStatus

__all__ = [
    "MAX_PLAN_ACTIONS",
    "MAX_PLANNING_MS",
    "PLAN_SCHEMA_VERSION",
    "REPLAN_TRIGGERS",
    "ActionPlan",
    "CandidateAction",
    "GamePlanService",
    "PlanPolicy",
    "PlanRule",
    "PlanRuleRegistry",
    "PlanStatus",
    "PlanningContext",
    "Precondition",
    "RiskLevel",
    "default_plan_rules",
]

PLAN_SCHEMA_VERSION = "nf20.1"

#: Hard ceilings. A policy may lower these, never raise them.
MAX_PLAN_ACTIONS = 8
MAX_PLANNING_MS = 5_000
MAX_REPLANS = 5
MAX_PLAN_HISTORY = 40

#: The ONLY causes a plan may be regenerated for. An unrecognised string is a
#: refusal: replanning without a cause is how a failed sequence restarts forever.
REPLAN_TRIGGERS = frozenset(
    {
        "operator",
        "state_changed",
        "action_failed",
        "action_refused",
        "objective_changed",
        "precondition_false",
        "observation_stale",
        "session_invalid",
        "budget_exceeded",
    }
)

#: Objective statuses in which planning is meaningful.
_OPEN_OBJECTIVE_STATUSES = frozenset(
    {
        ObjectiveStatus.DISCOVERED,
        ObjectiveStatus.ACTIVE,
        ObjectiveStatus.IN_PROGRESS,
        ObjectiveStatus.BLOCKED,
        ObjectiveStatus.NOT_VERIFIABLE,
    }
)


class PlanStatus(StrEnum):
    """Where one plan stands — named for what actually happened.

    ``EXECUTED`` means every submitted step's *input operation* completed. It is
    deliberately not called ``COMPLETED`` or ``SUCCESS``: no in-game effect is
    observed by this phase.
    """

    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    EXECUTED = "executed"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    NO_ACTION = "no_action"
    REPLAN_REQUIRED = "replan_required"
    REPLAN_EXHAUSTED = "replan_exhausted"
    INVALIDATED = "invalidated"

    @property
    def executable(self) -> bool:
        return self in {PlanStatus.PLANNED, PlanStatus.AWAITING_APPROVAL}

    @property
    def terminal(self) -> bool:
        return self not in {
            PlanStatus.PLANNED,
            PlanStatus.AWAITING_APPROVAL,
            PlanStatus.RUNNING,
        }


class RiskLevel(StrEnum):
    """A declared risk class, never an inferred one."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class PlanPolicy:
    """The planner's bounds. One object, validated once, reported everywhere."""

    max_actions_per_plan: int = 4
    max_candidates: int = 24
    planning_deadline_ms: int = 2000
    max_state_age_seconds: float = 10.0
    #: Re-analysing a stale state is ONE bounded request, never a loop.
    refresh_stale_state: bool = True
    #: An executable plan is approved explicitly unless a deployment says not to.
    require_approval: bool = True
    max_replans: int = 3
    max_plans_kept: int = 30
    #: Execution re-reads the state after each step so progress can be observed.
    refresh_between_steps: bool = True
    #: A HIGH-risk candidate is refused unless a deployment opts in. Risk is
    #: DECLARED by the rule (or the knowledge record); this gate makes a high or
    #: missing declaration a refusal instead of a penalty nobody reads.
    allow_high_risk: bool = False
    #: The same rule for an action that cannot be undone. No shipped rule is
    #: irreversible, and a knowledge record must say so explicitly.
    allow_irreversible: bool = False
    #: Scoring weights. Named, stated and reported with every score.
    weight_alignment: float = 2.0
    weight_progress: float = 2.0
    weight_cost: float = 0.5
    weight_risk: float = 1.0
    weight_reversible: float = 0.5
    weight_information: float = 1.0

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not (1 <= self.max_actions_per_plan <= MAX_PLAN_ACTIONS):
            problems.append(
                f"max_actions_per_plan must be between 1 and {MAX_PLAN_ACTIONS}."
            )
        if not (1 <= self.max_candidates <= 200):
            problems.append("max_candidates must be between 1 and 200.")
        if not (1 <= self.planning_deadline_ms <= MAX_PLANNING_MS):
            problems.append(
                f"planning_deadline_ms must be between 1 and {MAX_PLANNING_MS}."
            )
        if not (0.1 <= self.max_state_age_seconds <= 600):
            problems.append("max_state_age_seconds must be between 0.1 and 600.")
        if not (0 <= self.max_replans <= MAX_REPLANS):
            problems.append(f"max_replans must be between 0 and {MAX_REPLANS}.")
        if not (1 <= self.max_plans_kept <= MAX_PLAN_HISTORY):
            problems.append(f"max_plans_kept must be between 1 and {MAX_PLAN_HISTORY}.")
        for name in (
            "weight_alignment",
            "weight_progress",
            "weight_cost",
            "weight_risk",
            "weight_reversible",
            "weight_information",
        ):
            if float(getattr(self, name)) < 0:
                problems.append(f"{name} cannot be negative.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_actions_per_plan": self.max_actions_per_plan,
            "max_candidates": self.max_candidates,
            "planning_deadline_ms": self.planning_deadline_ms,
            "max_state_age_seconds": self.max_state_age_seconds,
            "refresh_stale_state": self.refresh_stale_state,
            "require_approval": self.require_approval,
            "max_replans": self.max_replans,
            "max_plans_kept": self.max_plans_kept,
            "allow_high_risk": self.allow_high_risk,
            "allow_irreversible": self.allow_irreversible,
            "refresh_between_steps": self.refresh_between_steps,
            "weights": {
                "alignment": self.weight_alignment,
                "progress": self.weight_progress,
                "cost": self.weight_cost,
                "risk": self.weight_risk,
                "reversible": self.weight_reversible,
                "information": self.weight_information,
            },
            "score_note": (
                "A declared weighted sum over named components; a ranking, not a "
                "probability and not a claim about the game."
            ),
        }


@dataclass(frozen=True, slots=True)
class PlanRule:
    """One declared action shape for one game/objective family.

    A rule is configuration: it never reads the screen, and it is matched by
    game tokens plus objective category/tokens. ``required_fields`` names HUD
    element ids that must have been RECOGNIZED for the rule to be applicable.
    """

    rule_id: str
    kind: ControlKind
    description: str
    expected_effect: str
    game_tokens: tuple[str, ...] = ()
    objective_categories: tuple[ObjectiveCategory, ...] = ()
    objective_tokens: tuple[str, ...] = ()
    key: str = ""
    button: str = "left"
    duration_ms: int = 0
    required_fields: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    reversible: bool = True
    objective_alignment: float = 1.0
    information_gain: float = 0.0
    repeatable: bool = True
    fixture_only: bool = False
    source: str = "declared rule"

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("A plan rule needs an id.")
        if not isinstance(self.kind, ControlKind):
            raise ValueError(f"Unknown plan-rule kind: {self.kind!r}")
        if not 0.0 <= self.objective_alignment <= 1.0:
            raise ValueError("objective_alignment is a declared match strength in [0, 1].")
        if self.duration_ms < 0:
            raise ValueError("A plan rule duration cannot be negative.")

    def matches(self, game: str, objective: UnderstoodObjective) -> tuple[bool, str]:
        lowered = game.casefold()
        if self.game_tokens and not all(token.casefold() in lowered for token in self.game_tokens):
            return False, "game does not match this rule"
        if self.objective_categories and objective.category not in self.objective_categories:
            return False, f"objective category {objective.category.value!r} does not match"
        text = f"{objective.description} {objective.source_text}".casefold()
        if self.objective_tokens and not any(
            token.casefold() in text for token in self.objective_tokens
        ):
            return False, "objective text does not match this rule"
        return True, ""

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value, "button": self.button}
        if self.key:
            data["key"] = self.key
        if self.duration_ms:
            data["duration_ms"] = self.duration_ms
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "description": self.description,
            "expected_effect": self.expected_effect,
            "game_tokens": list(self.game_tokens),
            "objective_categories": [item.value for item in self.objective_categories],
            "objective_tokens": list(self.objective_tokens),
            "key": self.key,
            "button": self.button,
            "duration_ms": self.duration_ms,
            "objective_alignment": self.objective_alignment,
            "required_fields": list(self.required_fields),
            "risk": self.risk.value,
            "reversible": self.reversible,
            "repeatable": self.repeatable,
            "fixture_only": self.fixture_only,
            "source": self.source,
        }


class PlanRuleRegistry:
    """The declared rules, with their validation and refusals reported."""

    def __init__(self, rules: Sequence[PlanRule] = ()) -> None:
        self._rules: dict[str, PlanRule] = {}
        self.rejected: list[dict[str, str]] = []
        for rule in rules:
            if rule.rule_id in self._rules:
                self.rejected.append(
                    {"rule_id": rule.rule_id, "reason": "duplicate rule id; kept the first"}
                )
                continue
            self._rules[rule.rule_id] = rule

    def add(self, rule: PlanRule) -> bool:
        if rule.rule_id in self._rules:
            return False
        self._rules[rule.rule_id] = rule
        return True

    def all(self) -> tuple[PlanRule, ...]:
        """Every accepted rule, in declared id order."""
        return tuple(rule for _, rule in sorted(self._rules.items()))

    def rows(self) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in self.all()]

    def matching(self, game: str, objective: UnderstoodObjective) -> list[PlanRule]:
        found: list[PlanRule] = []
        for _, rule in sorted(self._rules.items()):
            ok, _reason = rule.matches(game, objective)
            if ok:
                found.append(rule)
        return found


def default_plan_rules() -> PlanRuleRegistry:
    """The rules this build ships: ONE fixture-scenario rule, no game rules.

    The mission profile and its objective rule are NF-17's declared fixture
    (labelled states generated by an evaluation script, not pixels from a game).
    Mapping that fixture's "collect" objective onto a bounded ``w`` hold is
    therefore a *fixture* statement, and the rule says so in its own text. No
    commercial game gets a rule: none has a verified control mapping in this
    build, and inventing one is exactly what NF-20 forbids.
    """
    return PlanRuleRegistry(
        (
            PlanRule(
                rule_id="fixture-collect@1",
                kind=ControlKind.KEY_HOLD,
                key="w",
                duration_ms=120,
                description="Hold W for the fixture's declared control duration",
                expected_effect=(
                    "The fixture's progress counter may advance by one (a hypothesis: "
                    "the fixture is a declared scenario, and no effect is verified here)"
                ),
                game_tokens=("nc forge mission",),
                objective_categories=(ObjectiveCategory.MISSION, ObjectiveCategory.COUNTER),
                objective_tokens=("collect",),
                required_fields=("objective", "progress"),
                risk=RiskLevel.LOW,
                reversible=True,
                repeatable=True,
                fixture_only=True,
                source="NF-17 fixture scenario rule (not a commercial game)",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class Precondition:
    """One condition and whether the evidence establishes it (three-valued)."""

    name: str
    satisfied: bool | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "satisfied": self.satisfied, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CandidateAction:
    """One possible next action, with everything needed to judge it."""

    candidate_id: str
    kind: ControlKind
    description: str
    expected_effect: str
    source: str
    parameters: Mapping[str, Any]
    preconditions: tuple[Precondition, ...] = ()
    evidence: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.LOW
    reversible: bool = True
    objective_alignment: float = 0.0
    expected_progress: float = 1.0
    information_gain: float = 0.0
    expected_cost_ms: int = 0
    repeatable: bool = True
    risk_declared: bool = True
    confidence: float | None = None
    rejection: str = ""
    score: float | None = None
    score_parts: Mapping[str, float] = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return not self.rejection and all(item.satisfied is True for item in self.preconditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "description": self.description,
            "expected_effect": self.expected_effect,
            "source": self.source,
            "parameters": dict(self.parameters),
            "preconditions": [item.to_dict() for item in self.preconditions],
            "evidence": list(self.evidence),
            "risk": self.risk.value,
            "risk_declared": self.risk_declared,
            "reversible": self.reversible,
            "objective_alignment": self.objective_alignment,
            "information_gain": self.information_gain,
            "expected_cost_ms": self.expected_cost_ms,
            "repeatable": self.repeatable,
            "confidence": self.confidence,
            "rejection": self.rejection,
            "feasible": self.feasible,
            "score": self.score,
            "score_parts": dict(self.score_parts),
        }


@dataclass(slots=True)
class PlannedStep:
    """One ordered step of a plan: a validated control action and its purpose."""

    index: int
    action: ControlAction
    candidate_id: str
    source: str
    description: str
    expected_effect: str
    risk: RiskLevel
    reversible: bool
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "candidate_id": self.candidate_id,
            "source": self.source,
            "action_id": self.action.action_id,
            "kind": self.action.kind.value,
            "parameters": self.action.parameters(),
            "description": self.description,
            "expected_effect": self.expected_effect,
            "risk": self.risk.value,
            "reversible": self.reversible,
            "evidence": list(self.evidence),
        }


#: The ceiling on a strategy's ordering term. It is a tie-breaker among
#: already-justified candidates, never a way to promote an unsuitable one: the
#: smallest meaningful non-strategy difference (cost, risk) is 0.1 in the score,
#: and the declared weights scale those differences well above this bound at the
#: default settings.
_MAX_PREFERENCE_WEIGHT = 1.0


@dataclass(frozen=True, slots=True)
class StrategyHint:
    """NF-26's bounded recommendation, as the planner is allowed to read it.

    A hint comes from validated experience and may do exactly three things, each
    of which only NARROWS what the planner would otherwise do:

    1. **Ordering** — a bounded scoring term that moves preferred action kinds
       earlier and avoided kinds later among candidates that already exist. It
       never adds a candidate, never removes one, and never makes an infeasible
       action feasible.
    2. **Plan length** — ``max_actions_per_plan`` can only lower the policy's cap.
    3. **Freshness** — ``max_state_age_seconds`` can only demand *fresher* evidence.

    The planner clamps every field against its own policy rather than trusting a
    caller, and an invalid hint is ignored with a reason — never half-applied.
    """

    strategy_id: str
    version: int = 0
    preferred_kinds: tuple[str, ...] = ()
    avoided_kinds: tuple[str, ...] = ()
    preference_weight: float = 0.0
    max_actions_per_plan: int | None = None
    max_state_age_seconds: float | None = None
    evidence: tuple[str, ...] = ()
    rationale: str = ""

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not str(self.strategy_id or "").strip():
            problems.append("A strategy hint must name its strategy.")
        if int(self.version) < 0:
            problems.append("A strategy version cannot be negative.")
        for field_name, kinds in (
            ("preferred_kinds", self.preferred_kinds),
            ("avoided_kinds", self.avoided_kinds),
        ):
            seen: set[str] = set()
            for kind in kinds:
                text = str(kind or "").strip().lower()
                if text not in CONTROL_KINDS:
                    problems.append(
                        f"{field_name} contains {kind!r}, which is not an action this "
                        "build can perform."
                    )
                if text in seen:
                    problems.append(f"{field_name} names {text!r} twice.")
                seen.add(text)
        both = {str(item).strip().lower() for item in self.preferred_kinds} & {
            str(item).strip().lower() for item in self.avoided_kinds
        }
        if both:
            problems.append(
                "A kind cannot be both preferred and avoided: " + ", ".join(sorted(both))
            )
        if not (0.0 <= float(self.preference_weight) <= _MAX_PREFERENCE_WEIGHT):
            problems.append(
                f"preference_weight must be between 0 and {_MAX_PREFERENCE_WEIGHT:g}."
            )
        if self.max_actions_per_plan is not None and not (
            1 <= int(self.max_actions_per_plan) <= MAX_PLAN_ACTIONS
        ):
            problems.append(f"max_actions_per_plan must be between 1 and {MAX_PLAN_ACTIONS}.")
        if self.max_state_age_seconds is not None and not (
            0.1 <= float(self.max_state_age_seconds) <= 600.0
        ):
            problems.append("max_state_age_seconds must be between 0.1 and 600.")
        return problems

    @property
    def usable(self) -> bool:
        return not self.errors()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id).strip(),
            "version": int(self.version),
            "preferred_kinds": [str(item).strip().lower() for item in self.preferred_kinds],
            "avoided_kinds": [str(item).strip().lower() for item in self.avoided_kinds],
            "preference_weight": float(self.preference_weight),
            "max_actions_per_plan": self.max_actions_per_plan,
            "max_state_age_seconds": self.max_state_age_seconds,
            "evidence": list(self.evidence),
            "rationale": str(self.rationale or ""),
        }


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """Everything one planning decision is allowed to read.

    The manager builds this from the session's OWN services; a test builds it
    from fixtures. ``state_age_seconds`` is ``None`` when the evidence's time
    could not be read, which planning treats as *not fresh* rather than fresh.
    """

    session_id: str
    game: str
    target: ControlTarget | None
    state: GameState | None
    objective_state: ObjectiveState | None
    knowledge: tuple[KnowledgeRecord, ...]
    gate: Mapping[str, Any]
    budget_remaining: int | None
    state_age_seconds: float | None
    control_policy: ControlPolicy
    actions_executed: int = 0
    #: NF-26's bounded recommendation, or ``None`` when no strategy is active.
    #: An invalid hint is ignored by the planner (with a recorded reason).
    strategy: StrategyHint | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def active_objectives(self) -> list[UnderstoodObjective]:
        source = self.objective_state
        if source is None:
            return []
        return [
            item
            for item in source.objectives
            if item.status in _OPEN_OBJECTIVE_STATUSES and item.session_id == self.session_id
        ]

    def active_objective(self) -> tuple[UnderstoodObjective | None, str]:
        """The ONE objective this plan is for, or why there is not one.

        Ambiguity is refused instead of guessed: if two open explicit objectives
        name different tasks, a plan built for either could be the wrong one.
        """
        active = self.active_objectives()
        explicit = [item for item in active if item.explicit]
        pool = explicit or active
        if not pool:
            return None, "no active objective is understood"
        descriptions = {item.description.casefold().strip() for item in pool}
        if len(descriptions) > 1:
            return None, "more than one active objective is understood; refusing to guess"
        return pool[0], ""


@dataclass(slots=True)
class ActionPlan:
    """One bounded plan, its rationale, its approval and its execution record."""

    plan_id: str
    session_id: str
    status: PlanStatus
    created_at: str
    game: str = ""
    objective_id: str = ""
    objective_description: str = ""
    objective_revision: int = 0
    state_id: str = ""
    observation_id: str = ""
    state_sequence: int = 0
    state_age_seconds: float | None = None
    #: The reading's own status and quality, carried into the plan so uncertainty
    #: is a recorded fact rather than a sentence in a rationale.
    state_status: str = ""
    state_quality: dict[str, Any] = field(default_factory=dict)
    steps: list[PlannedStep] = field(default_factory=list)
    candidates: list[CandidateAction] = field(default_factory=list)
    rationale: str = ""
    reason: str = ""
    code: str = ""
    budgets: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    invalidation: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    knowledge_refs: list[str] = field(default_factory=list)
    #: NF-26: which strategy produced this plan, what it asked for and what was
    #: actually applied, or ``None`` when no strategy was active.
    strategy: dict[str, Any] | None = None
    reports: list[str] = field(default_factory=list)
    revision: int = 1
    replan_count: int = 0
    parent_plan_id: str = ""
    updated_at: str = ""
    schema_version: str = PLAN_SCHEMA_VERSION
    MAX_STEPS = MAX_PLAN_ACTIONS

    @property
    def signature(self) -> str:
        parts = [
            self.objective_id,
            *(
                f"{step.action.kind.value}:{sorted(step.action.parameters().items())}"
                for step in self.steps
            ),
        ]
        return "|".join(parts)

    @property
    def executable(self) -> bool:
        return self.status.executable and bool(self.steps)

    def validate(self, policy: ControlPolicy) -> list[str]:
        """Every reason this plan must not run; empty means it may."""
        problems: list[str] = []
        if not self.plan_id.strip():
            problems.append("A plan needs an id.")
        if not self.session_id.strip():
            problems.append("A plan needs a session id.")
        if self.status.executable:
            if not self.steps:
                problems.append("An executable plan needs at least one step.")
            if len(self.steps) > MAX_PLAN_ACTIONS:
                problems.append(f"A plan may hold at most {MAX_PLAN_ACTIONS} steps.")
            if not self.objective_id:
                problems.append("An executable plan needs an objective reference.")
            if not self.state_id or not self.observation_id:
                problems.append("An executable plan needs the observation it was based on.")
            for step in self.steps:
                if step.action.session_id != self.session_id:
                    problems.append("A step's action belongs to a different session.")
                for problem in step.action.validate(policy):
                    problems.append(f"Step {step.index}: {problem}")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "game": self.game,
            "status": self.status.value,
            "code": self.code,
            "reason": self.reason,
            "objective": {
                "objective_id": self.objective_id,
                "description": self.objective_description,
                "revision": self.objective_revision,
            },
            "evidence_refs": {
                "state_id": self.state_id,
                "observation_id": self.observation_id,
                "state_sequence": self.state_sequence,
                "state_age_seconds": self.state_age_seconds,
                "state_status": self.state_status,
                "state_quality": dict(self.state_quality),
            },
            "steps": [step.to_dict() for step in self.steps],
            "candidates": [item.to_dict() for item in self.candidates],
            "rationale": self.rationale,
            "budgets": dict(self.budgets),
            "approval": dict(self.approval),
            "execution": dict(self.execution),
            "invalidation": dict(self.invalidation),
            "evidence": [dict(item) for item in self.evidence],
            "knowledge_refs": list(self.knowledge_refs),
            "strategy": dict(self.strategy) if self.strategy else None,
            "reports": list(self.reports),
            "revision": self.revision,
            "replan_count": self.replan_count,
            "parent_plan_id": self.parent_plan_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            # False until NF-21 records a verified effect on this plan; the two
            # sentences are deliberately different answers, not one reworded one.
            "effect_verified": bool(self.execution.get("effect_verified")),
            "effect_note": str(
                self.execution.get("effect_note")
                or (
                    "An executed plan means its input operations completed. No in-game "
                    "effect was observed or verified; that belongs to NF-21."
                )
            ),
        }


class Publish(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class GamePlanService:
    """Bounded, evidence-grounded planning over ONE session's own services.

    ``context_reader`` returns the session's current evidence; ``submit`` sends
    one validated action through NF-19 and returns its envelope. Both are
    injected so this module has no window, injector or event-bus dependency of
    its own.
    """

    policy: PlanPolicy
    context_reader: Callable[[str, bool], Awaitable[PlanningContext]]
    submit: Callable[[str, ControlAction], Awaitable[dict[str, Any]]]
    cancel_action: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    rules: PlanRuleRegistry = field(default_factory=default_plan_rules)
    publish: Publish | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    _plans: OrderedDict[str, ActionPlan] = field(default_factory=OrderedDict, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _running_plan: str = field(default="", init=False, repr=False)
    _running_session: str = field(default="", init=False, repr=False)
    _cancel: asyncio.Event | None = field(default=None, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    _counts: dict[str, int] = field(
        default_factory=lambda: {
            "planned": 0,
            "insufficient_information": 0,
            "no_action": 0,
            "executed": 0,
            "stopped": 0,
            "cancelled": 0,
            "failed": 0,
            "replanned": 0,
            "replan_refused": 0,
        },
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        problems = self.policy.errors()
        if problems:
            raise ValueError("Invalid plan policy: " + "; ".join(problems))

    # ── read surface ───────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def running_plan(self) -> str:
        return self._running_plan

    def get(self, plan_id: str) -> ActionPlan | None:
        return self._plans.get(plan_id)

    def current_for(self, session_id: str) -> ActionPlan | None:
        for plan in reversed(self._plans.values()):
            if plan.session_id == session_id:
                return plan
        return None

    def history(self, session_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
        rows = [
            plan.to_dict()
            for plan in reversed(self._plans.values())
            if not session_id or plan.session_id == session_id
        ]
        return rows[: max(1, min(int(limit), self.policy.max_plans_kept))]

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "available": True,
            "stopped": self._stopped,
            "running": self._running,
            "running_plan": self._running_plan,
            "policy": self.policy.to_dict(),
            "counts": dict(self._counts),
            "rules": self.rules.rows(),
            "rejected_rules": list(self.rules.rejected),
            "plans_kept": len(self._plans),
            "limitations": [
                "Planning uses only verified state, objective and knowledge; a missing "
                "or stale reading is a refusal, never a guess.",
                "No plan rule ships for a commercial game. Without a declared rule or a "
                "verified control binding, the planner returns no_action.",
                "An executed plan means its input operations completed, not that the game "
                "reacted; effects belong to NF-21.",
                "Plans are process-lifetime records; their audit events persist on the "
                "session timeline.",
                "No action is retried automatically.",
            ],
        }

    async def shutdown(self) -> None:
        self._stopped = True
        if self._running and self._cancel is not None:
            self._cancel.set()

    # ── planning ───────────────────────────────────────────────────────────
    async def plan(self, session_id: str, *, refresh: bool = True) -> ActionPlan:
        """Build ONE bounded plan for the session's active objective."""
        started = time.monotonic()
        try:
            context = await self._read_context(session_id, refresh)
        except Exception as exc:  # noqa: BLE001 - a context bug is an honest refusal
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "context_unavailable",
                f"The session's evidence could not be read: {type(exc).__name__}: {exc}",
            )
        if self._stopped:
            return await self._record_refusal(
                session_id, PlanStatus.REJECTED, "service_stopped", "Planning is shut down."
            )
        missing = self._context_problem(context)
        if missing is not None:
            status, code, reason = missing
            return await self._record_refusal(session_id, status, code, reason, context=context)

        objective, objective_problem = context.active_objective()
        if objective is None:
            return await self._record_refusal(
                session_id,
                PlanStatus.INSUFFICIENT_INFORMATION,
                "no_active_objective",
                objective_problem or "No active objective was understood.",
                context=context,
            )
        if objective.revision < 1:  # pragma: no cover - model guarantees this
            return await self._record_refusal(
                session_id, PlanStatus.REJECTED, "invalid_objective", "Invalid objective.", context=context
            )
        progress = objective.progress
        if (
            progress is not None
            and progress.numeric
            and progress.target is not None
            and progress.value is not None
            and progress.value >= progress.target
        ):
            return await self._record_refusal(
                session_id,
                PlanStatus.NO_ACTION,
                "objective_progress_complete",
                (
                    f"The objective's observed counter is already at its target "
                    f"({progress.value:g}/{progress.target:g}); no action is planned. "
                    "Completion is still the verifier's verdict, not this planner's."
                ),
                context=context,
                objective=objective,
            )

        deadline = time.monotonic() + self.policy.planning_deadline_ms / 1000.0
        try:
            candidates, rejections, timed_out, truncated = self._candidates(
                context, objective, deadline
            )
        except Exception as exc:  # noqa: BLE001 - a generator bug is a safe refusal
            return await self._record_refusal(
                session_id,
                PlanStatus.FAILED,
                "candidate_generation_failed",
                (
                    "Candidate generation raised "
                    f"{type(exc).__name__}: {exc}; nothing was selected or sent."
                ),
                context=context,
                objective=objective,
            )
        if timed_out and not candidates and not rejections:
            return await self._record_refusal(
                session_id,
                PlanStatus.INSUFFICIENT_INFORMATION,
                "planning_timeout",
                (
                    f"Planning exceeded its {self.policy.planning_deadline_ms} ms deadline "
                    "before a candidate could be judged; nothing was selected."
                ),
                context=context,
                objective=objective,
            )
        if not candidates and not rejections:
            return await self._record_refusal(
                session_id,
                PlanStatus.INSUFFICIENT_INFORMATION,
                "no_plan_rule",
                (
                    f"No declared plan rule or verified control matches {context.game!r} and "
                    f"the objective {objective.description!r}. This build does not invent "
                    "game mechanics."
                ),
                context=context,
                objective=objective,
            )
        feasible = [item for item in candidates if item.feasible]
        if not feasible:
            best_reason = "; ".join(
                f"{item.description}: {item.rejection}"
                for item in candidates
                if item.rejection
            )
            return await self._record_refusal(
                session_id,
                PlanStatus.NO_ACTION,
                "no_feasible_action",
                "No candidate action met its preconditions. " + (best_reason[:600] or ""),
                context=context,
                objective=objective,
                candidates=[*candidates, *rejections],
            )

        feasible.sort(key=self._selection_key)
        selected = feasible[0]
        steps = self._steps_for(context, objective, selected)
        if not steps:
            return await self._record_refusal(
                session_id,
                PlanStatus.NO_ACTION,
                "no_action_budget",
                "The session's action budget leaves no room for a plan.",
                context=context,
                objective=objective,
                candidates=candidates,
            )
        now = self._stamp()
        plan = ActionPlan(
            plan_id=uuid4().hex[:12],
            session_id=session_id,
            status=(
                PlanStatus.AWAITING_APPROVAL
                if self.policy.require_approval
                else PlanStatus.PLANNED
            ),
            created_at=now,
            updated_at=now,
            game=context.game,
            objective_id=objective.objective_id,
            objective_description=objective.description,
            objective_revision=objective.revision,
            state_id=context.state.state_id if context.state is not None else "",
            observation_id=(
                context.state.observation.observation_id if context.state is not None else ""
            ),
            state_sequence=context.state.sequence if context.state is not None else 0,
            state_age_seconds=context.state_age_seconds,
            state_status=context.state.status.value if context.state is not None else "",
            state_quality=dict(context.state.quality) if context.state is not None else {},
            steps=steps,
            candidates=list(candidates) + list(rejections),
            rationale=self._rationale(context, objective, selected, steps, feasible, rejections),
            budgets={
                "max_actions": self.policy.max_actions_per_plan,
                "planned_actions": len(steps),
                "remaining_actions": context.budget_remaining,
                "deadline_ms": self.policy.planning_deadline_ms,
                "max_state_age_seconds": self._state_age_limit(context),
                "candidates_truncated": truncated,
                # NF-20 Scope B: the session's own action history at build time,
                # so the record says what budget this plan was chosen under.
                "actions_already_executed": context.actions_executed,
            },
            approval={
                "required": self.policy.require_approval,
                "status": "pending" if self.policy.require_approval else "not_required",
            },
            evidence=self._evidence(context, objective),
            strategy=self._strategy_row(context, steps),
            knowledge_refs=[
                row.knowledge_id
                for row in context.knowledge
                if row.knowledge_id in selected.evidence
            ],
            reports=[
                "The selected action is a hypothesis about progress; its effect is not "
                "observed or verified by this phase.",
                "The state is a reading and the objective is an interpretation; both are "
                "evidence, never a verified outcome. Their status, quality and age are "
                "recorded with this plan.",
                "Execution rechecks the session gate, target identity, state freshness and "
                "the objective before each step.",
            ],
        )
        if truncated:
            plan.reports.append(
                "Candidate generation stopped at its bound (the candidate cap or the "
                "planning deadline); this selection was made from a PARTIAL candidate set."
            )
        if plan.strategy is not None:
            plan.reports.append(
                "A strategy was applied to this plan: it reordered already-feasible "
                "candidates and could only narrow the plan's length and accepted "
                "freshness. The bounds and the approval requirement are unchanged, and "
                "the strategy's identity and evidence travel on this plan."
            )
        problems = plan.validate(context.control_policy)
        if problems:
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "invalid_plan",
                "The plan failed its own validation: " + "; ".join(problems),
                context=context,
                objective=objective,
                candidates=candidates,
            )
        self._store(plan)
        self._counts["planned"] += 1
        await self._announce("novaforge.plan.created", self._event_payload(plan))
        return plan

    def _context_problem(
        self, context: PlanningContext
    ) -> tuple[PlanStatus, str, str] | None:
        if not bool(context.gate.get("allow")):
            state = str(context.gate.get("state") or "not_running")
            reason = str(context.gate.get("reason") or "The session gate refuses planning.")
            # A closed or obsolete session, an unsupported input path and an
            # exhausted budget are REFUSALS: no amount of information makes them
            # plannable. Every other gate state is a "not yet" and says so.
            status = (
                PlanStatus.REJECTED
                if state
                in {"closed", "no_session", "unsupported", "budget_exhausted", "budget_unenforced"}
                else PlanStatus.INSUFFICIENT_INFORMATION
            )
            return status, f"gate_{state}", reason
        if context.state is None:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                "no_state",
                (
                    "No validated game state has been read. Analyze the screen first: "
                    "planning never captures or guesses."
                ),
            )
        if context.state.observation.session_id and (
            context.state.observation.session_id != context.session_id
        ):
            return (
                PlanStatus.REJECTED,
                "wrong_session_state",
                "The newest state belongs to a different session.",
            )
        if context.state_age_seconds is None:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                "unknown_state_age",
                "The state carries no readable timestamp, so its freshness cannot be established.",
            )
        age_limit = self._state_age_limit(context)
        if context.state_age_seconds > age_limit:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                "stale_state",
                (
                    f"The newest state is {context.state_age_seconds:.1f}s old (limit "
                    f"{age_limit:.1f}s). Analyze the screen again."
                ),
            )
        if context.state.status.value in {"unavailable", "failed"}:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                f"state_{context.state.status.value}",
                "The newest state is not usable evidence; planning is refused.",
            )
        if context.objective_state is None:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                "no_objective_interpretation",
                "No objective has been interpreted from the newest state.",
            )
        if context.objective_state.observation_id != context.state.observation.observation_id:
            return (
                PlanStatus.INSUFFICIENT_INFORMATION,
                "objective_stale",
                "The objective interpretation is from an older observation than the state.",
            )
        if context.budget_remaining is not None and context.budget_remaining <= 0:
            return (
                PlanStatus.REJECTED,
                "budget_exhausted",
                "The session's action budget is exhausted.",
            )
        return None

    # ── candidate generation ───────────────────────────────────────────────
    def _candidates(
        self,
        context: PlanningContext,
        objective: UnderstoodObjective,
        deadline: float,
    ) -> tuple[list[CandidateAction], list[CandidateAction], bool, bool]:
        """Every candidate this policy's bounds allow, plus how generation ended.

        ``timed_out`` is the deadline; ``truncated`` says generation stopped at a
        limit with work left, so a caller can report that the selection was made
        from a partial candidate set instead of implying it saw them all.
        """
        candidates: list[CandidateAction] = []
        rejected: list[CandidateAction] = []
        generated = 0
        timed_out = False
        truncated = False
        for rule in self.rules.matching(context.game, objective):
            if time.monotonic() >= deadline or generated >= self.policy.max_candidates:
                timed_out = time.monotonic() >= deadline
                truncated = True
                break
            generated += 1
            candidate = self._rule_candidate(context, objective, rule)
            if candidate.feasible:
                candidates.append(candidate)
            else:
                rejected.append(candidate)
        for record in context.knowledge:
            if time.monotonic() >= deadline or generated >= self.policy.max_candidates:
                timed_out = time.monotonic() >= deadline
                truncated = True
                break
            if record.category not in {KnowledgeCategory.CONTROL, KnowledgeCategory.INTERACTION_RULE}:
                continue
            if record.status is not KnowledgeStatus.VERIFIED or not record.verified_effect:
                continue
            if not self._knowledge_matches(record, objective):
                continue
            generated += 1
            candidate = self._knowledge_candidate(context, objective, record)
            if candidate.feasible:
                candidates.append(candidate)
            else:
                rejected.append(candidate)
        for item in [*candidates, *rejected]:
            if item.feasible:
                score, parts = self._score(item, context.strategy)
                object.__setattr__(item, "score", score)
                object.__setattr__(item, "score_parts", parts)
        return candidates, rejected, timed_out, truncated

    def _rule_candidate(
        self, context: PlanningContext, objective: UnderstoodObjective, rule: PlanRule
    ) -> CandidateAction:
        preconditions, problems = self._base_preconditions(context, objective)
        for field_name in rule.required_fields:
            reading = next(
                (row for row in (context.state.hud if context.state else ()) if row.element_id == field_name),
                None,
            )
            ok = reading is not None and reading.status is HudStatus.RECOGNIZED
            preconditions.append(
                Precondition(
                    f"field:{field_name}",
                    ok,
                    "the field was recognized" if ok else "the declared field was not recognized",
                )
            )
            if not ok:
                problems.append(f"required field {field_name!r} was not recognized")
        problems.extend(self._policy_problems(rule.risk, rule.reversible))
        parameters = rule.payload()
        action = self._action_for(context, parameters)
        if action is None:
            problems.append(self._action_refusal(context, parameters))
        candidate = CandidateAction(
            candidate_id=f"rule:{rule.rule_id}:{uuid4().hex[:6]}",
            kind=rule.kind,
            description=rule.description,
            expected_effect=rule.expected_effect,
            source=rule.source,
            parameters=parameters,
            preconditions=tuple(preconditions),
            evidence=(f"rule:{rule.rule_id}",),
            risk=rule.risk,
            reversible=rule.reversible,
            objective_alignment=rule.objective_alignment,
            expected_progress=1.0,
            information_gain=rule.information_gain,
            expected_cost_ms=max(0, rule.duration_ms) + 50,
            repeatable=rule.repeatable,
            rejection="; ".join(problems),
        )
        return candidate

    def _knowledge_candidate(
        self,
        context: PlanningContext,
        objective: UnderstoodObjective,
        record: KnowledgeRecord,
    ) -> CandidateAction:
        content = dict(record.content)
        parameters: dict[str, Any] = {}
        problems: list[str] = []
        raw_kind = str(content.get("kind", "") or "").strip().lower()
        raw_key = str(content.get("key", "") or "").strip()
        if raw_key:
            parameters["kind"] = raw_kind or ControlKind.KEY_HOLD.value
            parameters["key"] = raw_key
            if content.get("duration_ms") is not None:
                parameters["duration_ms"] = content.get("duration_ms")
        elif content.get("button"):
            parameters["kind"] = raw_kind or ControlKind.MOUSE_PRESS.value
            parameters["button"] = str(content.get("button"))
        else:
            problems.append(
                "the verified control has no input binding (`key`/`button`) in its content"
            )
        if parameters.get("kind") and str(parameters["kind"]) not in CONTROL_KINDS:
            problems.append(f"unsupported control kind {parameters['kind']!r}")
        risk_raw = str(content.get("risk", "") or "").strip().lower()
        try:
            # An UNDECLARED risk is not "medium": it is unknown, and an unknown
            # risk is treated as the highest one so the policy gate can refuse it.
            risk = RiskLevel(risk_raw) if risk_raw else RiskLevel.HIGH
            risk_declared = bool(risk_raw)
        except ValueError:
            risk = RiskLevel.HIGH
            risk_declared = False
        reversible = content.get("reversible") is not False
        preconditions, base_problems = self._base_preconditions(context, objective)
        problems.extend(base_problems)
        problems.extend(self._policy_problems(risk, reversible))
        action = self._action_for(context, parameters) if not problems else None
        if action is None and not problems:
            problems.append(self._action_refusal(context, parameters))
        expected = str(
            content.get("expected_effect")
            or content.get("effect")
            or content.get("action")
            or record.key
        )
        return CandidateAction(
            candidate_id=f"knowledge:{record.knowledge_id}:{uuid4().hex[:6]}",
            kind=action.kind if action is not None else ControlKind.KEY_PRESS,
            description=(
                f"{record.key}: {content.get('action', record.key)} "
                "(verified knowledge, explicit binding)"
            ),
            expected_effect=expected,
            source=f"knowledge:{record.knowledge_id}",
            parameters=parameters,
            preconditions=tuple(preconditions),
            evidence=(record.knowledge_id, f"record-revision:{record.revision}"),
            risk=risk,
            risk_declared=risk_declared,
            reversible=reversible,
            objective_alignment=1.0,
            expected_progress=1.0,
            information_gain=0.0,
            expected_cost_ms=100,
            repeatable=False,
            rejection="; ".join(dict.fromkeys(problems)),
        )

    def _knowledge_matches(
        self, record: KnowledgeRecord, objective: UnderstoodObjective
    ) -> bool:
        content = record.content
        text = f"{objective.description} {objective.source_text}".casefold()
        supports = content.get("supports")
        tokens: list[str] = []
        if isinstance(supports, str):
            tokens = [supports]
        elif isinstance(supports, (list, tuple)):
            tokens = [str(item) for item in supports if str(item).strip()]
        if tokens and any(token.casefold() in text for token in tokens):
            return True
        declared = str(content.get("objective", "") or "").strip().casefold()
        if declared and declared in text:
            return True
        return False

    def _base_preconditions(
        self, context: PlanningContext, objective: UnderstoodObjective
    ) -> tuple[list[Precondition], list[str]]:
        checks = [
            Precondition("session_gate", bool(context.gate.get("allow")), str(context.gate.get("reason", ""))),
            Precondition("state_present", context.state is not None, "a validated state is required"),
            Precondition(
                "state_fresh",
                context.state_age_seconds is not None
                and context.state_age_seconds <= self._state_age_limit(context),
                (
                    "age unknown"
                    if context.state_age_seconds is None
                    else f"{context.state_age_seconds:.1f}s old"
                ),
            ),
            Precondition(
                "objective_open",
                objective.status in _OPEN_OBJECTIVE_STATUSES,
                f"objective status is {objective.status.value}",
            ),
            Precondition(
                "objective_explicit",
                objective.explicit,
                "the objective must be stated by the screen, not inferred",
            ),
            Precondition(
                "action_budget",
                context.budget_remaining is None or context.budget_remaining > 0,
                "the session has action budget remaining",
            ),
        ]
        problems = [f"{item.name} is not satisfied" for item in checks if item.satisfied is not True]
        return checks, problems

    def _action_for(
        self, context: PlanningContext, parameters: Mapping[str, Any]
    ) -> ControlAction | None:
        if context.target is None:
            return None
        try:
            action = ControlAction.from_payload(
                parameters, session_id=context.session_id, target=context.target
            )
        except (ValueError, TypeError):
            return None
        if action.validate(context.control_policy):
            return None
        return action

    def _policy_problems(self, risk: RiskLevel, reversible: bool) -> list[str]:
        """Declared risk/irreversibility checked against the plan policy."""
        problems: list[str] = []
        if risk is RiskLevel.HIGH and not self.policy.allow_high_risk:
            problems.append(
                "its declared risk is HIGH and this policy does not allow high-risk actions"
            )
        if not reversible and not self.policy.allow_irreversible:
            problems.append(
                "it is irreversible and this policy does not allow irreversible actions"
            )
        return problems

    def _action_refusal(
        self, context: PlanningContext, parameters: Mapping[str, Any]
    ) -> str:
        """WHY the control interface would refuse these parameters — no guessing.

        A generic "unsupported" hides the one fact a person needs (an unknown key,
        a disallowed button, a hold above the bound), so the refusal names the
        control service's own reasons.
        """
        if context.target is None:
            return "no validated target window is pinned for this session"
        try:
            action = ControlAction.from_payload(
                parameters, session_id=context.session_id, target=context.target
            )
        except (ValueError, TypeError) as exc:
            return f"the control interface refused it: {exc}"
        problems = action.validate(context.control_policy)
        if problems:
            return "the control interface refused it: " + "; ".join(problems)
        return "the control interface does not support this action"

    @staticmethod
    def _selection_key(candidate: CandidateAction) -> tuple[float, str, str, str]:
        """Score first, then the candidate's DECLARED shape — never its random id.

        ``candidate_id`` ends in ``uuid4().hex[:6]``. The shipped ids all carry a
        distinguishing prefix, so ties were decided by rule/record name anyway,
        but a generated name is not a selection criterion: the key is the score
        and then the action itself (kind, source, parameters). Equal keys stay in
        generation order, which is deterministic — rules by id, then knowledge in
        the order it was read.
        """
        parameters = ";".join(f"{key}={value}" for key, value in sorted(candidate.parameters.items()))
        return (-(candidate.score or 0.0), candidate.kind.value, candidate.source, parameters)

    # ── NF-26: the strategy hint's three bounded effects ───────────────────
    def _active_hint(self, context: PlanningContext) -> StrategyHint | None:
        """The context's hint when it is usable, else ``None``.

        An invalid hint is IGNORED rather than repaired: a partially applied
        recommendation would claim an adaptation that did not happen.
        """
        hint = context.strategy
        if hint is None:
            return None
        return hint if hint.usable else None

    def _state_age_limit(self, context: PlanningContext) -> float:
        """The policy's freshness limit, tightened by a strategy when it asks.

        A hint may only demand fresher evidence. ``min`` is applied against the
        policy rather than trusting the hint's own validation, so the planner's
        bound is the one that always holds.
        """
        limit = float(self.policy.max_state_age_seconds)
        hint = self._active_hint(context)
        if hint is not None and hint.max_state_age_seconds is not None:
            limit = min(limit, float(hint.max_state_age_seconds))
        return limit

    def _strategy_row(self, context: PlanningContext, steps: Sequence[PlannedStep]) -> dict[str, Any] | None:
        """What this plan did about a strategy, recorded on the plan itself."""
        hint = self._active_hint(context)
        if hint is None:
            return None
        return {
            **hint.to_dict(),
            "applied": {
                # Report the effects this hint ACTUALLY asked for: a caps-only
                # hint reorders nothing, and saying otherwise would put a
                # behavior on the plan that never happened.
                "preferred_first": bool(hint.preferred_kinds),
                "avoided_last": bool(hint.avoided_kinds),
                "max_actions_per_plan": min(
                    int(self.policy.max_actions_per_plan),
                    int(hint.max_actions_per_plan)
                    if hint.max_actions_per_plan is not None
                    else int(self.policy.max_actions_per_plan),
                ),
                "max_state_age_seconds": self._state_age_limit(context),
                "planned_actions": len(steps),
            },
            "note": (
                "The hint only reorders already-feasible candidates and can only "
                "narrow the plan's length and the freshness it accepts; it never adds "
                "an action or widens a budget."
            ),
        }

    def _score(
        self, candidate: CandidateAction, hint: StrategyHint | None = None
    ) -> tuple[float, dict[str, float]]:
        risk_penalty = {
            RiskLevel.LOW: 0.1,
            RiskLevel.MEDIUM: 0.5,
            RiskLevel.HIGH: 1.0,
        }[candidate.risk]
        parts = {
            "alignment": round(self.policy.weight_alignment * candidate.objective_alignment, 4),
            "progress": round(
                self.policy.weight_progress
                * candidate.objective_alignment
                * candidate.expected_progress,
                4,
            ),
            "cost": round(-self.policy.weight_cost * (candidate.expected_cost_ms / 1000.0), 4),
            "risk": round(-self.policy.weight_risk * risk_penalty, 4),
            "reversible": round(
                self.policy.weight_reversible * (1.0 if candidate.reversible else 0.0), 4
            ),
            "information": round(self.policy.weight_information * candidate.information_gain, 4),
        }
        if hint is not None and hint.usable:
            name = candidate.kind.value
            preferred = {str(item).strip().lower() for item in hint.preferred_kinds}
            avoided = {str(item).strip().lower() for item in hint.avoided_kinds}
            if name in preferred:
                parts["strategy"] = round(float(hint.preference_weight), 4)
            elif name in avoided:
                parts["strategy"] = round(-float(hint.preference_weight), 4)
        return round(sum(parts.values()), 4), parts

    def _steps_for(
        self,
        context: PlanningContext,
        objective: UnderstoodObjective,
        selected: CandidateAction,
    ) -> list[PlannedStep]:
        limit = self.policy.max_actions_per_plan
        hint = self._active_hint(context)
        if hint is not None and hint.max_actions_per_plan is not None:
            # A strategy may shorten a plan, never lengthen it: the policy's own
            # cap stays the ceiling whatever the hint asks for.
            limit = min(limit, int(hint.max_actions_per_plan))
        if context.budget_remaining is not None:
            limit = min(limit, max(0, context.budget_remaining))
        if not selected.repeatable:
            limit = 1
        progress = objective.progress
        if (
            progress is not None
            and progress.numeric
            and progress.target is not None
            and progress.value is not None
        ):
            remaining = max(0, int(round(progress.target - progress.value)))
            if remaining:
                limit = min(limit, remaining)
        if self._action_for(context, selected.parameters) is None or context.target is None or limit <= 0:
            return []
        steps: list[PlannedStep] = []
        for index in range(1, limit + 1):
            try:
                step_action = ControlAction.from_payload(
                    selected.parameters,
                    session_id=context.session_id,
                    target=context.target,
                )
            except (ValueError, TypeError):
                return []
            steps.append(
                PlannedStep(
                    index=index,
                    action=step_action,
                    candidate_id=selected.candidate_id,
                    source=selected.source,
                    description=selected.description,
                    expected_effect=selected.expected_effect,
                    risk=selected.risk,
                    reversible=selected.reversible,
                    evidence=selected.evidence,
                )
            )
        return steps

    def _rationale(
        self,
        context: PlanningContext,
        objective: UnderstoodObjective,
        selected: CandidateAction,
        steps: Sequence[PlannedStep],
        feasible: Sequence[CandidateAction],
        rejected: Sequence[CandidateAction] = (),
    ) -> str:
        parts = [
            f"Objective {objective.description!r} ({objective.objective_id}, revision "
            f"{objective.revision}) is open.",
            f"Evidence: state {context.state.state_id if context.state else '(none)'} from "
            f"observation {context.state.observation.observation_id if context.state else '(none)'}, "
            f"{context.state_age_seconds:.1f}s old." if context.state_age_seconds is not None
            else "Evidence: state with unreadable age.",
            f"Selected {selected.description!r} from {selected.source} because it is feasible "
            f"and scored highest ({selected.score}) among {len(feasible)} feasible candidate(s).",
            f"The plan submits {len(steps)} bounded input operation(s); each is a hypothesis, "
            "and no effect is claimed.",
        ]
        # A feasible candidate that lost on score is not a "rejected" one, and a
        # rationale that only names the infeasible alternatives hides the closest
        # comparison the selection actually made.
        others = [item for item in feasible if item is not selected][:3]
        if others:
            parts.append(
                "Compared and not selected: "
                + "; ".join(
                    f"{item.description!r} (score {item.score})" for item in others
                )
                + "."
            )
        if rejected:
            parts.append(
                "Not selected: "
                + "; ".join(
                    f"{item.description!r} ({item.rejection or 'a precondition was not true'})"
                    for item in list(rejected)[:4]
                )
                + "."
            )
        return " ".join(parts)

    # ── execution ──────────────────────────────────────────────────────────
    async def execute(
        self, session_id: str, plan_id: str, *, approved: bool = False
    ) -> ActionPlan:
        """Submit one approved plan, step by step, stopping on the first problem."""
        plan = self._plans.get(plan_id)
        if plan is None or plan.session_id != session_id:
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "unknown_plan",
                "No plan with that id belongs to this session.",
            )
        if not plan.steps:
            plan.status = PlanStatus.REJECTED
            plan.code = "no_steps"
            plan.reason = "The plan has no executable steps; nothing was sent."
            return plan
        if not plan.status.executable:
            plan.reason = f"The plan is {plan.status.value}; it cannot run again."
            return plan
        if plan.status is PlanStatus.AWAITING_APPROVAL and not approved:
            plan.code = "approval_required"
            plan.reason = (
                "This plan is awaiting the operator's explicit approval. Nothing was sent."
            )
            return plan
        if self._stopped:
            plan.status = PlanStatus.REJECTED
            plan.code = "service_stopped"
            plan.reason = "Planning is shut down."
            return plan
        if self._running:
            plan.code = "busy"
            plan.reason = (
                "Another plan is executing; this one was not started. One plan at a time."
            )
            return plan
        context: PlanningContext | None = None
        try:
            context = await self._read_context(session_id, True)
        except Exception as exc:  # noqa: BLE001 - an unreadable context is a safe stop
            plan.status = PlanStatus.REPLAN_REQUIRED
            plan.code = "context_unavailable"
            plan.reason = f"The plan's evidence could not be refreshed: {type(exc).__name__}: {exc}"
            return plan
        problem = self._execution_problem(plan, context)
        if problem is not None:
            status, code, reason = problem
            plan.status = status
            plan.code = code
            plan.reason = reason
            plan.invalidation = {"code": code, "reason": reason, "at": self._stamp()}
            await self._announce("novaforge.plan.invalidated", self._event_payload(plan))
            return plan

        now = self._stamp()
        plan.status = PlanStatus.RUNNING
        plan.updated_at = now
        plan.approval = {
            **plan.approval,
            "status": "approved" if plan.approval.get("required") else "not_required",
            "approved_at": now,
            "approved_by": "operator" if approved else "policy",
        }
        plan.execution = {
            "started_at": now,
            "finished_at": "",
            "steps": [],
            "effect_verified": False,
        }
        self._running = True
        self._running_plan = plan.plan_id
        self._running_session = session_id
        self._cancel = asyncio.Event()
        try:
            # The announcement is INSIDE the try because awaiting it is a point a
            # caller's cancellation can arrive at. With it outside, a disconnected
            # request skipped the finally: ``_running`` stayed set and every later
            # plan was refused as "busy" for the life of the process.
            await self._announce("novaforge.plan.started", self._event_payload(plan))
            for index, step in enumerate(plan.steps, start=1):
                if self._cancel is not None and self._cancel.is_set():
                    plan.status = PlanStatus.CANCELLED
                    plan.code = "cancelled"
                    plan.reason = "The operator cancelled the plan before the next step."
                    break
                if self._stopped:
                    plan.status = PlanStatus.CANCELLED
                    plan.code = "service_stopped"
                    plan.reason = "The planner shut down mid-plan; no further step was sent."
                    break
                try:
                    envelope = await self.submit(session_id, step.action)
                except Exception as exc:  # noqa: BLE001 - a dispatch bug is a failed step
                    plan.status = PlanStatus.FAILED
                    plan.code = "interface_error"
                    plan.reason = (
                        f"Step {index} could not be submitted ({type(exc).__name__}: {exc}); "
                        "no further step was sent."
                    )
                    break
                if not isinstance(envelope, Mapping):
                    plan.status = PlanStatus.FAILED
                    plan.code = "interface_error"
                    plan.reason = f"Step {index} returned no result envelope; the plan stopped."
                    break
                result = dict(envelope.get("result") or {})
                record = {
                    "index": index,
                    "action_id": step.action.action_id,
                    "kind": step.action.kind.value,
                    "status": str(envelope.get("status") or result.get("status") or ""),
                    "ok": bool(envelope.get("ok")),
                    "code": str(envelope.get("code") or result.get("code") or ""),
                    "reason": str(envelope.get("reason") or result.get("reason") or ""),
                    "error": str(result.get("error") or ""),
                    "requested_at": str(result.get("requested_at") or ""),
                    "started_at": str(result.get("started_at") or ""),
                    "dispatched_at": str(result.get("dispatched_at") or ""),
                    "finished_at": str(result.get("finished_at") or ""),
                    "duration_ms": result.get("duration_ms"),
                    "deadline_ms": result.get("deadline_ms"),
                    "cleanup": dict(result.get("cleanup") or {}),
                    "effect": None,
                }
                plan.execution["steps"].append(record)
                plan.updated_at = self._stamp()
                await self._announce("novaforge.plan.step", self._event_payload(plan))
                if not record["ok"]:
                    plan.status, plan.code = self._failure_status(record)
                    plan.reason = (
                        f"Step {index} ended {record['status']}"
                        + (f" ({record['code']})" if record["code"] else "")
                        + f": {record['reason']}"
                    )
                    if plan.status in {PlanStatus.REPLAN_REQUIRED, PlanStatus.STOPPED}:
                        plan.invalidation = {
                            "code": record["code"],
                            "reason": plan.reason,
                            "at": plan.updated_at,
                        }
                    break
                if index < len(plan.steps):
                    if self.policy.refresh_between_steps:
                        try:
                            context = await self._read_context(session_id, True)
                        except Exception as exc:  # noqa: BLE001 - stop, never guess
                            plan.status = PlanStatus.REPLAN_REQUIRED
                            plan.code = "context_unavailable"
                            plan.reason = (
                                f"Step {index} completed, but the state could not be refreshed "
                                f"({type(exc).__name__}: {exc}); the plan stopped."
                            )
                            break
                        stop = self._execution_problem(plan, context)
                        if stop is not None:
                            plan.status, plan.code, plan.reason = stop
                            plan.invalidation = {
                                "code": plan.code,
                                "reason": plan.reason,
                                "at": self._stamp(),
                            }
                            break
                        objective, _problem = context.active_objective()
                        if objective is not None and objective.status in {
                            ObjectiveStatus.COMPLETED,
                            ObjectiveStatus.FAILED,
                        }:
                            plan.status = PlanStatus.EXECUTED
                            plan.code = "objective_terminal"
                            plan.reason = (
                                f"Step {index} completed; the objective is now "
                                f"{objective.status.value} by its own evidence. No effect is "
                                "claimed by this planner."
                            )
                            break
            else:
                plan.status = PlanStatus.EXECUTED
                plan.code = "all_steps_submitted"
                plan.reason = (
                    "Every planned input operation completed. The game's reaction was not "
                    "observed or verified."
                )
        except asyncio.CancelledError:
            # The caller went away (a disconnected request, an aborted task). That is
            # NOT an interface error, and recording one would claim a fault this plan
            # never hit; without this branch the finally labeled it interface_error.
            # State is restored by the finally either way, and the cancellation is
            # re-raised so the caller's task really is cancelled.
            if plan.status is PlanStatus.RUNNING:
                plan.status = PlanStatus.CANCELLED
                plan.code = "caller_cancelled"
                plan.reason = "The executing task was cancelled; no further step was sent."
            raise
        finally:
            plan.execution["finished_at"] = self._stamp()
            plan.execution["effect_verified"] = False
            plan.execution["effect_note"] = (
                "Executed means the input operations completed; NF-21 verifies effects."
            )
            self._running = False
            self._running_plan = ""
            self._running_session = ""
            self._cancel = None
            if plan.status is PlanStatus.RUNNING:  # pragma: no cover - defensive
                plan.status = PlanStatus.FAILED
                plan.code = "interface_error"
                plan.reason = "The plan ended without a terminal status."
            key = {
                PlanStatus.EXECUTED: "executed",
                PlanStatus.STOPPED: "stopped",
                PlanStatus.FAILED: "failed",
                PlanStatus.CANCELLED: "cancelled",
                PlanStatus.REPLAN_REQUIRED: "stopped",
            }.get(plan.status)
            if key:
                self._counts[key] += 1
            await self._announce("novaforge.plan.finished", self._event_payload(plan))
        return plan

    def _execution_problem(
        self, plan: ActionPlan, context: PlanningContext
    ) -> tuple[PlanStatus, str, str] | None:
        """Whether the plan's evidence still supports its next step.

        Called before the first step and after every refresh, so a plan never
        outlives the evidence that justified it: the same checks the plan was
        built on are re-derived from the reading that is current NOW.
        """
        base = self._context_problem(context)
        if base is not None:
            return base
        objective, problem = context.active_objective()
        if objective is None:
            return (
                PlanStatus.STOPPED,
                "objective_lost",
                problem or "The plan's objective is no longer active.",
            )
        if objective.objective_id != plan.objective_id:
            return (
                PlanStatus.INVALIDATED,
                "objective_changed",
                "The active objective changed since this plan was built.",
            )
        if objective.revision != plan.objective_revision:
            return (
                PlanStatus.INVALIDATED,
                "objective_revised",
                "The objective was revised since this plan was built; replanning is required.",
            )
        if objective.status in {ObjectiveStatus.COMPLETED, ObjectiveStatus.FAILED}:
            return (
                PlanStatus.STOPPED,
                "objective_terminal",
                f"The objective is already {objective.status.value}; no further action is sent.",
            )
        stale = self._state_preconditions(plan, context)
        if stale is not None:
            return stale
        for step in plan.steps:
            problems = step.action.validate(context.control_policy)
            if problems:
                return (
                    PlanStatus.REJECTED,
                    "plan_invalid",
                    "A planned action is no longer supported: " + "; ".join(problems),
                )
        if context.budget_remaining is not None and context.budget_remaining <= 0:
            return (
                PlanStatus.REPLAN_REQUIRED,
                "budget_exhausted",
                "The session's action budget was exhausted before the plan finished.",
            )
        return None

    @staticmethod
    def _candidate_subject(candidate_id: str) -> tuple[str, str]:
        """The source prefix and the declared id inside a candidate id.

        ``rule:<rule_id>:<random>`` and ``knowledge:<knowledge_id>:<random>``, so
        the subject is everything between the first and the last colon — a rule
        or record id that itself contains a colon survives.
        """
        prefix, _, rest = candidate_id.partition(":")
        return prefix, rest.rpartition(":")[0]

    def _state_preconditions(
        self, plan: ActionPlan, context: PlanningContext
    ) -> tuple[PlanStatus, str, str] | None:
        """Re-derive the STATE-derived preconditions on the freshest reading.

        Candidate generation checks the declared HUD fields a rule needs and the
        verification status of the knowledge it used. Between two steps of the
        same plan either can change — a progress field stops being recognized, a
        record is contradicted — so execution rechecks them instead of trusting
        the reading the plan was built on. An action with no relationship to the
        state it will run against is exactly what a precondition is for.
        """
        rules = {rule.rule_id: rule for rule in self.rules.all()}
        for step in plan.steps:
            prefix, subject = self._candidate_subject(step.candidate_id)
            if prefix == "rule":
                rule = rules.get(subject)
                if rule is None:
                    return (
                        PlanStatus.REJECTED,
                        "plan_invalid",
                        f"The rule {subject!r} this plan was built from is no longer "
                        "declared; no further step was sent.",
                    )
                for field_name in rule.required_fields:
                    reading = next(
                        (
                            row
                            for row in (context.state.hud if context.state else ())
                            if row.element_id == field_name
                        ),
                        None,
                    )
                    if reading is None or reading.status is not HudStatus.RECOGNIZED:
                        return (
                            PlanStatus.STOPPED,
                            "precondition_false",
                            (
                                f"The declared field {field_name!r} this plan depends on is no "
                                "longer recognized in the current state; the plan stopped "
                                "before sending another step."
                            ),
                        )
            elif prefix == "knowledge":
                record = next(
                    (row for row in context.knowledge if row.knowledge_id == subject), None
                )
                # Only a record that is STILL reported is judged: a record the
                # reader simply did not return is not evidence that it was
                # withdrawn, and stopping on that would be a guess.
                if record is not None and (
                    record.status is not KnowledgeStatus.VERIFIED or not record.verified_effect
                ):
                    return (
                        PlanStatus.STOPPED,
                        "precondition_false",
                        (
                            f"The verified knowledge {subject!r} behind this plan is now "
                            f"{record.status.value} for the current reading; the plan stopped "
                            "before sending another step."
                        ),
                    )
        return None

    @staticmethod
    def _failure_status(record: Mapping[str, Any]) -> tuple[PlanStatus, str]:
        status = str(record.get("status") or "")
        code = str(record.get("code") or "")
        if status == ControlStatus.CANCELLED.value:
            return PlanStatus.CANCELLED, code or "cancelled"
        if status == ControlStatus.TIMED_OUT.value:
            return PlanStatus.REPLAN_REQUIRED, code or "timed_out"
        if status == ControlStatus.CLEANUP_FAILED.value:
            return PlanStatus.FAILED, code or "cleanup_failed"
        if status == ControlStatus.REJECTED.value:
            if code in {
                "closed",
                "not_running",
                "held",
                "pending",
                "unvalidated",
                "budget_exhausted",
                "budget_unenforced",
                "unsupported",
            }:
                return PlanStatus.STOPPED, code or "gate_refused"
            return PlanStatus.REPLAN_REQUIRED, code or "rejected"
        if status == ControlStatus.FAILED.value:
            return PlanStatus.REPLAN_REQUIRED, code or "dispatch_failed"
        return PlanStatus.FAILED, code or "unknown"

    async def cancel(self, session_id: str, plan_id: str = "") -> ActionPlan | None:
        """Ask the running plan (and its in-flight action) to stop."""
        plan = self._plans.get(plan_id) if plan_id else self.current_for(session_id)
        if plan is None or plan.session_id != session_id:
            return None
        if self._cancel is not None and self._running:
            self._cancel.set()
            if self.cancel_action is not None:
                try:
                    await self.cancel_action(session_id)
                except Exception:  # noqa: BLE001 - the flag still stops the loop
                    pass
            plan.code = "cancel_requested"
            plan.reason = "Cancel requested; the plan stops at its next checkpoint."
            return plan
        if plan.status.executable:
            plan.status = PlanStatus.CANCELLED
            plan.code = "cancelled"
            plan.reason = "The operator cancelled the plan before it ran."
            plan.updated_at = self._stamp()
            await self._announce("novaforge.plan.cancelled", self._event_payload(plan))
        return plan

    async def replan(
        self, session_id: str, plan_id: str, *, reason: str = "operator"
    ) -> ActionPlan:
        """Regenerate a stopped/failed plan once, from FRESH evidence, bounded."""
        previous = self._plans.get(plan_id)
        if previous is None or previous.session_id != session_id:
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "unknown_plan",
                "No plan with that id belongs to this session.",
            )
        if reason not in REPLAN_TRIGGERS:
            self._counts["replan_refused"] += 1
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "unknown_trigger",
                (
                    f"Replanning needs a cause from {sorted(REPLAN_TRIGGERS)}; "
                    f"{reason!r} is not one, so nothing was regenerated."
                ),
            )
        if previous.status is PlanStatus.RUNNING:
            self._counts["replan_refused"] += 1
            return await self._record_refusal(
                session_id,
                PlanStatus.REJECTED,
                "plan_running",
                "The plan is executing; cancel it before replanning.",
            )
        if self._running:
            previous.code = "busy"
            previous.reason = "A plan is executing; replanning waits for it to stop."
            return previous
        if previous.replan_count >= self.policy.max_replans:
            previous.status = PlanStatus.REPLAN_EXHAUSTED
            previous.code = "replan_limit"
            previous.reason = (
                f"The replanning limit ({self.policy.max_replans}) is reached; no new plan "
                "is generated."
            )
            self._counts["replan_refused"] += 1
            return previous
        fresh = await self.plan(session_id, refresh=True)
        if not fresh.steps:
            self._counts["replan_refused"] += 1
            return fresh
        same_evidence = fresh.state_id == previous.state_id
        same_plan = fresh.signature == previous.signature
        if same_plan and same_evidence:
            fresh.status = PlanStatus.REPLAN_EXHAUSTED
            fresh.code = "repeat_plan_refused"
            fresh.reason = (
                "Fresh evidence is identical to the failed plan's evidence and the candidate "
                "sequence is unchanged; repeating it would not be a new attempt."
            )
            self._counts["replan_refused"] += 1
            await self._announce("novaforge.plan.replan_refused", self._event_payload(fresh))
            return fresh
        # The old plan is obsolete from this moment: it must not be run later by
        # an id that is still lying around.
        if previous.status.executable:
            previous.status = PlanStatus.INVALIDATED
            previous.code = "replanned"
            previous.reason = f"Replaced by a fresh plan ({reason})."
            previous.updated_at = self._stamp()
        fresh.parent_plan_id = previous.plan_id
        fresh.revision = previous.revision + 1
        fresh.replan_count = previous.replan_count + 1
        fresh.reason = f"Replanned ({reason}); parent {previous.plan_id}."
        fresh.status = PlanStatus.AWAITING_APPROVAL if self.policy.require_approval else PlanStatus.PLANNED
        self._counts["replanned"] += 1
        await self._announce("novaforge.plan.replanned", self._event_payload(fresh))
        return fresh

    def record_effects(
        self, session_id: str, plan_id: str, outcomes: Sequence[Mapping[str, Any]]
    ) -> ActionPlan | None:
        """Accept NF-21's verified outcomes for this plan's already-sent steps.

        The planner verifies nothing itself; it records what the verifier
        observed, so the plan a person reads back carries the outcome of each step
        it sent. A row naming an action this plan never sent is reported as
        unmatched rather than attached to a step it does not describe, and
        ``effect_verified`` moves only when EVERY verified row was a verified
        success — one unknown step is not a success.
        """
        plan = self._plans.get(plan_id)
        if plan is None or plan.session_id != session_id:
            return None
        steps = plan.execution.get("steps")
        step_rows: list[dict[str, Any]] = (
            [row for row in steps if isinstance(row, dict)] if isinstance(steps, list) else []
        )
        effects: dict[str, dict[str, Any]] = {}
        unmatched: list[str] = []
        for row in outcomes:
            if not isinstance(row, Mapping):
                continue
            action_id = str(row.get("action_id") or "")
            if not action_id:
                unmatched.append("a row named no action")
                continue
            effects[action_id] = {
                "outcome_id": str(row.get("outcome_id") or ""),
                "status": str(row.get("status") or ""),
                "effect_verified": bool(row.get("effect_verified")),
                "code": str(row.get("code") or ""),
                "reason": str(row.get("reason") or ""),
                "observed_effect": str(row.get("observed_effect") or ""),
                "confidence": row.get("confidence"),
                "confidence_basis": str(row.get("confidence_basis") or ""),
                "recovery": str(row.get("recovery") or ""),
                "evidence_refs": [
                    str(item) for item in (row.get("evidence_refs") or []) if str(item)
                ][:8],
                "verified_at": str(row.get("verified_at") or row.get("observed_at") or "")
                or self._stamp(),
            }
        for step in step_rows:
            effect = effects.get(str(step.get("action_id") or ""))
            if effect is None:
                continue
            step["effect"] = dict(effect)
        seen = {str(step.get("action_id") or "") for step in step_rows}
        unmatched.extend(sorted(action_id for action_id in effects if action_id not in seen))
        recorded = [row for action_id, row in effects.items() if action_id in seen]
        plan.execution["outcomes"] = [
            {"action_id": action_id, **effect} for action_id, effect in effects.items()
        ]
        plan.execution["unmatched"] = sorted(set(unmatched))
        plan.execution["effect_verified"] = bool(recorded) and all(
            bool(row.get("effect_verified")) for row in recorded
        )
        plan.execution["effect_note"] = (
            "Every verified step's declared observable was observed by NF-21."
            if plan.execution["effect_verified"]
            else (
                f"NF-21 verified {len(recorded)} of {len(step_rows)} sent step(s) and "
                "did not establish a verified effect for all of them."
            )
        )
        plan.execution["verified_at"] = self._stamp()
        plan.updated_at = plan.execution["verified_at"]
        return plan

    # ── records / helpers ──────────────────────────────────────────────────
    async def _read_context(self, session_id: str, refresh: bool) -> PlanningContext:
        return await self.context_reader(session_id, refresh)

    async def _record_refusal(
        self,
        session_id: str,
        status: PlanStatus,
        code: str,
        reason: str,
        *,
        context: PlanningContext | None = None,
        objective: UnderstoodObjective | None = None,
        candidates: Sequence[CandidateAction] = (),
    ) -> ActionPlan:
        now = self._stamp()
        plan = ActionPlan(
            plan_id=uuid4().hex[:12],
            session_id=session_id,
            status=status,
            created_at=now,
            updated_at=now,
            game=context.game if context is not None else "",
            objective_id=objective.objective_id if objective is not None else "",
            objective_description=objective.description if objective is not None else "",
            objective_revision=objective.revision if objective is not None else 0,
            state_id=context.state.state_id if context is not None and context.state else "",
            observation_id=(
                context.state.observation.observation_id
                if context is not None and context.state is not None
                else ""
            ),
            state_age_seconds=context.state_age_seconds if context is not None else None,
            state_status=(
                context.state.status.value
                if context is not None and context.state is not None
                else ""
            ),
            state_quality=(
                dict(context.state.quality)
                if context is not None and context.state is not None
                else {}
            ),
            candidates=list(candidates),
            code=code,
            reason=reason,
            budgets={"max_actions": self.policy.max_actions_per_plan},
            approval={"required": self.policy.require_approval, "status": "not_requested"},
            evidence=(
                self._evidence(context, objective)
                if context is not None and objective is not None
                else []
            ),
            reports=["No action was generated. Nothing was sent."],
        )
        key = {
            PlanStatus.INSUFFICIENT_INFORMATION: "insufficient_information",
            PlanStatus.NO_ACTION: "no_action",
        }.get(status)
        if key:
            self._counts[key] += 1
        self._store(plan)
        await self._announce("novaforge.plan.refused", self._event_payload(plan))
        return plan

    def _store(self, plan: ActionPlan) -> None:
        self._plans[plan.plan_id] = plan
        while len(self._plans) > self.policy.max_plans_kept:
            self._plans.popitem(last=False)

    def _evidence(
        self, context: PlanningContext, objective: UnderstoodObjective
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if context.state is not None:
            rows.append(
                {
                    "kind": "state",
                    "state_id": context.state.state_id,
                    "observation_id": context.state.observation.observation_id,
                    "sequence": context.state.sequence,
                    "age_seconds": context.state_age_seconds,
                }
            )
        rows.append(
            {
                "kind": "objective",
                "objective_id": objective.objective_id,
                "revision": objective.revision,
                "status": objective.status.value,
                "evidence": [item.to_dict() for item in objective.evidence],
            }
        )
        for record in context.knowledge:
            rows.append(
                {
                    "kind": "knowledge",
                    "knowledge_id": record.knowledge_id,
                    "status": record.status.value,
                    "verified_effect": record.verified_effect,
                }
            )
        return rows

    def _event_payload(self, plan: ActionPlan) -> dict[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "session_id": plan.session_id,
            "status": plan.status.value,
            "code": plan.code,
            "reason": plan.reason,
            "objective_id": plan.objective_id,
            "steps": len(plan.steps),
            "dispatched": any(
                bool(row.get("dispatched_at")) for row in plan.execution.get("steps", [])
            ),
            # Only NF-21 moves this, and only after an observation supports it.
            "effect_verified": bool(plan.execution.get("effect_verified")),
        }

    async def _announce(self, event_type: str, payload: dict[str, Any]) -> None:
        """Announce one edge and WAIT for the seam that records it.

        The publish seam is the audit trail (the manager writes the session
        timeline from it). Scheduling it as a background task let a plan edge be
        recorded after the caller had already moved on, and dropped outright if
        the loop stopped first — an event nobody can read back is not a record.
        A broken observer is still swallowed: an announcement is not the work.
        """
        if self.publish is None:
            return
        try:
            outcome = self.publish(event_type, payload)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - an announcement is not the work
            return

    def _stamp(self) -> str:
        try:
            return self.clock().isoformat()
        except Exception:  # noqa: BLE001 - a failing clock is still a timestamp gap
            return datetime.now(UTC).isoformat()
