"""Phase 20's records: what an agentic episode IS, step by step.

Phases 15–19 recorded a RUN and what it was worth. Phase 20 records the same
work as a SEQUENCE — a goal, the state the agent was in, what it observed, what
it decided, what it did, what came back, what verification said, what it earned,
and the state it moved to — because that is the unit agentic reinforcement
learning consumes.

Everything here is a PROJECTION of records NovaControl already produces. A state
is built from the task state machine and the context the app already assembles;
an action names a tool or capability the tool registry already describes; a
reward reuses Phase 15's component/penalty vocabulary; an episode can be
projected into Phase 18's :class:`~novacontrol.rlhf.models.Rollout` and into
Phase 15's :class:`~novacontrol.evaluation.models.AgentTrajectory` rather than
replacing either.

Two rules shape every dataclass:

**No hidden chain-of-thought, ever.** There is no field for a model's private
reasoning and there is deliberately nowhere to put one. A decision stores the
action it chose, why in a STRUCTURED reason code, the alternatives it considered
and their value estimates; an observation stores what was perceived. A reader can
always reconstruct what happened and what it was based on without reading
anybody's mind.

**A figure that was not measured is ``None``.** An episode's success is
``bool | None`` because ``None`` means nobody decided whether it worked, which
is a real outcome and must not be stored as a failure. A value estimate that no
critic produced stays ``None`` rather than becoming a zero that reads as a
prediction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.core.security import RiskLevel
from novacontrol.evaluation.models import AgentTrajectory, now_iso
from novacontrol.evaluation.reward import RewardComponent, RewardPenalty
from novacontrol.rlhf.models import (
    RewardSource,
    Rollout,
    RolloutStatus,
    RolloutStep,
)

#: The vocabulary version of this phase's records.
AGENTIC_VERSION = "phase20.1"
AGENTIC_SCHEMA_VERSION = 1
EPISODE_SCHEMA_VERSION = 1
POLICY_RECORD_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 1

#: Hard ceilings. A configuration mistake must not be able to build an episode
#: that cannot terminate, so the limits are enforced by the model, not only by
#: the config validator that reports them.
MAX_EPISODE_STEPS_CEILING = 512
MAX_PLANNING_HORIZON_CEILING = 512
MAX_RETRIES_CEILING = 20


# ── small parsers ────────────────────────────────────────────────────────────


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def as_flag(value: Any, default: bool | None = None) -> bool | None:
    return value if isinstance(value, bool) else default


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def as_real(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def as_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (list, tuple)):
        return tuple(as_mapping(item) for item in value if isinstance(item, Mapping))
    return ()


def as_texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def as_reals(value: Any) -> tuple[float, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(
            float(item) for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)
        )
    return ()


def _record(value: Any, factory: Any) -> Any:
    return factory(as_mapping(value)) if isinstance(value, Mapping) else None


# ── vocabulary ───────────────────────────────────────────────────────────────


class ActionType(StrEnum):
    """What KIND of thing an action is. The verb lives in ``capability``/``tool``.

    The list is deliberately the phase's own: an agentic episode is not only a
    sequence of tool calls. Gathering information, handing a step to the planner,
    recovering from a failure and doing local computation are all actions, and
    the reward layer scores them differently, so they must be distinguishable.
    """

    TOOL = "tool_invocation"
    PLANNER_STEP = "planner_step"
    RECOVERY = "recovery_action"
    INFORMATION = "information_gathering"
    COMMUNICATION = "communication"
    LOCAL_COMPUTATION = "local_computation"
    ENVIRONMENT = "environment_interaction"
    #: The action that does nothing (a policy with nothing left to say). It is
    #: recorded rather than skipped, so a stalled episode is visible.
    NOOP = "noop"


ACTION_TYPES: tuple[str, ...] = tuple(member.value for member in ActionType)


class TerminationReason(StrEnum):
    """Why an episode ended. Exactly one of these is recorded, always.

    ``SUCCESS`` means the goal was achieved AND verification agreed. A run that
    stopped without that is `FAILURE`, or one of the four bounded endings — the
    distinction between "it did not work" and "it was stopped" matters to both
    the reward and the report.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    MAX_STEPS = "max_steps"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SAFETY_STOP = "safety_stop"
    ENVIRONMENT_ERROR = "environment_error"


TERMINATION_REASONS: tuple[str, ...] = tuple(member.value for member in TerminationReason)


class PolicyReasonCode(StrEnum):
    """WHY the policy chose the action it chose — structured, not narrated."""

    HIGHEST_VERIFIED_VALUE = "highest_verified_value"
    REQUIRED_TOOL = "required_tool"
    RECOVERY = "recovery"
    EXPLORATION = "exploration"
    SAFETY_CONSTRAINT = "safety_constraint"
    USER_GOAL = "user_goal"
    PLANNER_DECISION = "planner_decision"


POLICY_REASON_CODES: tuple[str, ...] = tuple(member.value for member in PolicyReasonCode)


class PolicyStatus(StrEnum):
    """A candidate policy's lifecycle. Nothing promotes itself."""

    EXPERIMENTAL = "experimental"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    PRODUCTION = "production"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


POLICY_STATUSES: tuple[str, ...] = tuple(member.value for member in PolicyStatus)

#: Statuses a policy may hold while it is still a candidate.
CANDIDATE_STATUSES: frozenset[str] = frozenset(
    {PolicyStatus.EXPERIMENTAL.value, PolicyStatus.EVALUATING.value}
)
#: Statuses that mean "this policy may be used for real work".
LIVE_STATUSES: frozenset[str] = frozenset(
    {PolicyStatus.APPROVED.value, PolicyStatus.PRODUCTION.value}
)


class CreditMethod(StrEnum):
    """How credit for the outcome is attributed back to the steps."""

    #: Every step gets an equal share of the terminal reward.
    TERMINAL_PROPAGATION = "terminal_propagation"
    #: Discounted returns: what each step's remainder of the episode was worth.
    DISCOUNTED_RETURN = "discounted_return"
    #: The step's own measured reward, accumulated — no shaping.
    STEP_ACCUMULATION = "step_accumulation"
    #: Return minus the baseline: which steps did better than the episode's mean.
    ADVANTAGE = "advantage"
    #: The verifier's verdict decides: verified steps are credited, unverified
    #: ones are not, whatever the raw reward said.
    VERIFIER_ATTRIBUTION = "verifier_attribution"


CREDIT_METHODS: tuple[str, ...] = tuple(member.value for member in CreditMethod)


class ExplorationStrategy(StrEnum):
    """How exploration picks among the actions the mask left standing."""

    GREEDY = "greedy"
    EPSILON_GREEDY = "epsilon_greedy"
    TEMPERATURE = "temperature"
    #: A bounded random step: never more than ``bounded_steps`` in a row, so an
    #: exploration phase cannot become an unsupervised wanders-about phase.
    BOUNDED_STOCHASTIC = "bounded_stochastic"


EXPLORATION_STRATEGIES: tuple[str, ...] = tuple(
    member.value for member in ExplorationStrategy
)


class RewardDimension(StrEnum):
    """The separate reward dimensions. Never collapsed into one opaque number."""

    TASK_SUCCESS = "task_success"
    VERIFICATION = "verification"
    SAFETY = "safety"
    EFFICIENCY = "efficiency"
    LATENCY = "latency"
    RESOURCE_USAGE = "resource_usage"
    TOOL_CORRECTNESS = "tool_correctness"
    PLANNING_EFFICIENCY = "planning_efficiency"
    RECOVERY_QUALITY = "recovery_quality"


REWARD_DIMENSIONS: tuple[str, ...] = tuple(member.value for member in RewardDimension)

#: Safety is its own dimension and is reported separately from efficiency: a
#: policy that is fast and unsafe must never look like a policy that is fast.
SAFETY_DIMENSION = RewardDimension.SAFETY.value
EFFICIENCY_DIMENSIONS: tuple[str, ...] = (
    RewardDimension.EFFICIENCY.value,
    RewardDimension.LATENCY.value,
    RewardDimension.RESOURCE_USAGE.value,
    RewardDimension.PLANNING_EFFICIENCY.value,
)


class StepSignal(StrEnum):
    """A positive step signal, named exactly as the specification names it."""

    CORRECT_TOOL = "correct_tool_selected"
    VALID_ACTION = "valid_action"
    SUBTASK_SUCCEEDED = "successful_subtask"
    VERIFICATION_PASSED = "successful_verification"
    INFORMATION_GAINED = "useful_information_gained"
    EFFICIENT_ACTION = "efficient_action"


class StepPenalty(StrEnum):
    """A penalty, named exactly as the specification names it."""

    UNNECESSARY_ACTION = "unnecessary_action"
    FAILED_ACTION = "failed_action"
    INCORRECT_TOOL = "incorrect_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    REPEATED_FAILURE = "repeated_failure"
    EXCESSIVE_LATENCY = "excessive_latency"
    EXCESSIVE_RESOURCE_USAGE = "excessive_resource_usage"
    UNSAFE_ACTION = "unsafe_action"
    UNNECESSARY_RECOVERY = "unnecessary_recovery"


STEP_SIGNALS: tuple[str, ...] = tuple(member.value for member in StepSignal)
STEP_PENALTIES: tuple[str, ...] = tuple(member.value for member in StepPenalty)


class TaskDifficultyLevel(IntEnum):
    """The six curriculum levels, in the order the specification lists them."""

    SIMPLE_DETERMINISTIC = 1
    MULTI_STEP = 2
    TOOL_SELECTION = 3
    FAILURE_RECOVERY = 4
    LONG_HORIZON = 5
    AMBIGUOUS = 6

    @property
    def label(self) -> str:
        return {
            TaskDifficultyLevel.SIMPLE_DETERMINISTIC: "simple deterministic tasks",
            TaskDifficultyLevel.MULTI_STEP: "multi-step tasks",
            TaskDifficultyLevel.TOOL_SELECTION: "tasks with tool selection",
            TaskDifficultyLevel.FAILURE_RECOVERY: "tasks with failures and recovery",
            TaskDifficultyLevel.LONG_HORIZON: "long-horizon tasks",
            TaskDifficultyLevel.AMBIGUOUS: "ambiguous or context-dependent tasks",
        }[self]


MIN_CURRICULUM_LEVEL = int(TaskDifficultyLevel.SIMPLE_DETERMINISTIC)
MAX_CURRICULUM_LEVEL = int(TaskDifficultyLevel.AMBIGUOUS)


# ── task difficulty ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskDifficulty:
    """What makes a task hard, on the dimensions the curriculum and reports use.

    A level is DERIVED from the dimensions rather than stored independently: two
    places that both claim to know how hard a task is will eventually disagree,
    and the derived one is the one that can be checked.
    """

    steps: int = 1
    tools: int = 1
    branching_factor: int = 1
    ambiguity: float = 0.0
    verification_complexity: float = 0.0
    recovery_required: bool = False
    resource_requirement: float = 0.0
    planning_horizon: int = 1

    def __post_init__(self) -> None:
        for name in ("steps", "tools", "branching_factor", "planning_horizon"):
            if getattr(self, name) < 1:
                raise ValueError(f"Task difficulty {name} must be at least 1.")

    @property
    def level(self) -> TaskDifficultyLevel:
        """The curriculum level these dimensions describe.

        Ordered from the most demanding requirement downward, because a task
        that needs recovery inside a ten-step horizon is a recovery task first:
        the level names the capability the task is really testing.

        Tool selection is the level for a task whose HARD PART is choosing among
        candidate tools — few steps, several tools — so a longer task is a
        multi-step task first and a one-tool task is simple however many decoys
        the environment offers it: a decoy is what makes acting badly possible,
        not a tool the task needs.
        """
        if self.ambiguity >= 0.5 or self.verification_complexity >= 0.75:
            return TaskDifficultyLevel.AMBIGUOUS
        if self.recovery_required:
            return TaskDifficultyLevel.FAILURE_RECOVERY
        if self.planning_horizon >= 6 or self.steps >= 8:
            return TaskDifficultyLevel.LONG_HORIZON
        if self.tools >= 2 and self.steps < 3:
            return TaskDifficultyLevel.TOOL_SELECTION
        if self.steps >= 2:
            return TaskDifficultyLevel.MULTI_STEP
        return TaskDifficultyLevel.SIMPLE_DETERMINISTIC

    def score(self) -> float:
        """A single comparable number, for ordering — never a reward."""
        return round(
            float(self.steps)
            + float(self.tools)
            + float(self.branching_factor)
            + 2.0 * float(self.planning_horizon)
            + 3.0 * float(self.ambiguity)
            + 3.0 * float(self.verification_complexity)
            + 4.0 * float(self.recovery_required)
            + float(self.resource_requirement),
            6,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "tools": self.tools,
            "branching_factor": self.branching_factor,
            "ambiguity": self.ambiguity,
            "verification_complexity": self.verification_complexity,
            "recovery_required": self.recovery_required,
            "resource_requirement": self.resource_requirement,
            "planning_horizon": self.planning_horizon,
            "level": int(self.level),
            "level_label": self.level.label,
            "score": self.score(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskDifficulty:
        rows = as_mapping(data)
        return cls(
            steps=max(1, as_int(rows.get("steps"), 1)),
            tools=max(1, as_int(rows.get("tools"), 1)),
            branching_factor=max(1, as_int(rows.get("branching_factor"), 1)),
            ambiguity=as_real(rows.get("ambiguity"), 0.0) or 0.0,
            verification_complexity=as_real(rows.get("verification_complexity"), 0.0) or 0.0,
            recovery_required=bool(as_flag(rows.get("recovery_required"), False)),
            resource_requirement=as_real(rows.get("resource_requirement"), 0.0) or 0.0,
            planning_horizon=max(1, as_int(rows.get("planning_horizon"), 1)),
        )


# ── state ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentState:
    """Everything the agent knows at one moment, in structured form.

    This does NOT replace the task state machine or the context manager: it is
    the SHAPE a policy reads and an episode records, assembled from those layers.
    ``task_state`` is the task state machine's published view, ``context`` is the
    relevant slice of context, ``capabilities``/``tools`` are what this
    installation can actually do, and the rest is what the episode observed.
    """

    goal: str = ""
    task_state: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()
    previous_actions: tuple[str, ...] = ()
    verification_results: tuple[Mapping[str, Any], ...] = ()
    active_errors: tuple[str, ...] = ()
    resource_state: Mapping[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    environment_state: Mapping[str, Any] = field(default_factory=dict)
    step_index: int = 0
    episode_id: str = ""

    @property
    def observation(self) -> Mapping[str, Any]:
        """The latest observation, or an empty mapping when there is none yet."""
        return self.observations[-1] if self.observations else {}

    @property
    def failed_verification(self) -> bool:
        """Whether the most recent check failed. ``None``-free: absent is False."""
        if not self.verification_results:
            return False
        return as_text(self.verification_results[-1].get("status")) == "fail"

    def fingerprint(self) -> str:
        """A stable identity for a state, for repeat detection and reporting."""
        canonical = json.dumps(
            {
                "goal": self.goal.strip().lower(),
                "step": self.step_index,
                "task": dict(self.task_state),
                "environment": dict(self.environment_state),
                "errors": list(self.active_errors),
                "observations": len(self.observations),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    # -- advancing (a state is immutable; these return a new one) ---------------

    def advanced(
        self,
        *,
        observation: Mapping[str, Any] | None = None,
        action: str = "",
        verification: Mapping[str, Any] | None = None,
        error: str = "",
        environment_state: Mapping[str, Any] | None = None,
        task_state: Mapping[str, Any] | None = None,
        resource_state: Mapping[str, Any] | None = None,
    ) -> AgentState:
        """The next state after one step: append what happened, keep the rest."""
        errors = list(self.active_errors)
        if error:
            errors.append(error)
        verification_rows = list(self.verification_results)
        if verification:
            verification_rows.append(dict(verification))
            if as_text(verification.get("status")) == "pass":
                # A later improvement clears the errors the failure recorded: the
                # episode learned something, and carrying the old error forward
                # would make the state claim a problem that was fixed.
                errors = []
        return replace(
            self,
            observations=(*self.observations, dict(observation)) if observation else self.observations,
            previous_actions=(*self.previous_actions, action) if action else self.previous_actions,
            verification_results=tuple(verification_rows),
            active_errors=tuple(errors),
            environment_state=(
                dict(environment_state) if environment_state is not None else self.environment_state
            ),
            task_state=dict(task_state) if task_state is not None else self.task_state,
            resource_state=(
                dict(resource_state) if resource_state is not None else self.resource_state
            ),
            step_index=self.step_index + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "task_state": dict(self.task_state),
            "context": dict(self.context),
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "observations": [dict(item) for item in self.observations],
            "previous_actions": list(self.previous_actions),
            "verification_results": [dict(item) for item in self.verification_results],
            "active_errors": list(self.active_errors),
            "resource_state": dict(self.resource_state),
            "permissions": list(self.permissions),
            "environment_state": dict(self.environment_state),
            "step_index": self.step_index,
            "episode_id": self.episode_id,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentState:
        rows = as_mapping(data)
        return cls(
            goal=as_text(rows.get("goal")),
            task_state=as_mapping(rows.get("task_state")),
            context=as_mapping(rows.get("context")),
            capabilities=as_texts(rows.get("capabilities")),
            tools=as_texts(rows.get("tools")),
            observations=as_mappings(rows.get("observations")),
            previous_actions=as_texts(rows.get("previous_actions")),
            verification_results=as_mappings(rows.get("verification_results")),
            active_errors=as_texts(rows.get("active_errors")),
            resource_state=as_mapping(rows.get("resource_state")),
            permissions=as_texts(rows.get("permissions")),
            environment_state=as_mapping(rows.get("environment_state")),
            step_index=max(0, as_int(rows.get("step_index"))),
            episode_id=as_text(rows.get("episode_id")),
        )


# ── actions ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentAction:
    """One thing the agent could do, in the vocabulary the tool layer already uses.

    A risk level, a reversibility flag and a confirmation requirement travel with
    the action because the permission layer needs them BEFORE anything runs, not
    after. This is not a second tool schema: ``tool``/``capability`` name entries
    the existing registry and permission manager already know about.
    """

    action_id: str = ""
    action_type: str = ActionType.TOOL.value
    capability: str = ""
    tool: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    expected_effect: str = ""
    reversible: bool = True
    requires_confirmation: bool = False
    #: A SHORT human label ("open the editor"), never a reasoning trace.
    label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The id is DERIVED from what the action is, never drawn at random. Two
        # actions that mean the same thing are the same action, whether they came
        # from an environment's candidate list or were built by a policy — which
        # is what lets a mask say "this exact action is allowed" and a shadow
        # comparison say "the two policies chose the same thing". A caller may
        # still pass an id explicitly.
        if not self.action_id:
            object.__setattr__(self, "action_id", self._derived_id())

    def _derived_id(self) -> str:
        canonical = json.dumps(
            {
                "action_type": self.action_type,
                "capability": self.capability,
                "tool": self.tool,
                "arguments": dict(self.arguments),
                "expected_effect": self.expected_effect,
                "risk_level": self.risk_level.value,
                "reversible": bool(self.reversible),
                "requires_confirmation": bool(self.requires_confirmation),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    @property
    def is_noop(self) -> bool:
        return self.action_type == ActionType.NOOP.value

    @property
    def name(self) -> str:
        """The shortest stable name: the tool if there is one, else the action type."""
        return self.tool or self.capability or self.action_type

    def signature(self) -> str:
        """What "the same action again" means, for repeat detection.

        Deliberately arguments-free: retrying an action with REPAIRED arguments is
        a different attempt, not a repeat.
        """
        return f"{self.action_type}:{self.tool}:{self.capability}"

    def with_arguments(self, **arguments: Any) -> AgentAction:
        # Different arguments are a different action, so the id is derived again.
        return replace(self, action_id="", arguments={**self.arguments, **arguments})

    def as_mapping(self) -> dict[str, Any]:
        """The compact form used inside a rollout step and a trajectory."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "capability": self.capability,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "risk_level": self.risk_level.value,
            "expected_effect": self.expected_effect,
            "reversible": self.reversible,
            "requires_confirmation": self.requires_confirmation,
            "label": self.label,
            "metadata": dict(self.metadata),
        }

    to_dict = as_mapping

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentAction:
        rows = as_mapping(data)
        risk = as_text(rows.get("risk_level"), RiskLevel.LOW.value)
        if risk not in RiskLevel._value2member_map_:
            risk = RiskLevel.LOW.value
        action_type = as_text(rows.get("action_type"), ActionType.TOOL.value)
        if action_type not in ACTION_TYPES:
            action_type = ActionType.TOOL.value
        return cls(
            action_id=as_text(rows.get("action_id")),
            action_type=action_type,
            capability=as_text(rows.get("capability")),
            tool=as_text(rows.get("tool")),
            arguments=as_mapping(rows.get("arguments")),
            risk_level=RiskLevel(risk),
            expected_effect=as_text(rows.get("expected_effect")),
            reversible=bool(as_flag(rows.get("reversible"), True)),
            requires_confirmation=bool(as_flag(rows.get("requires_confirmation"), False)),
            label=as_text(rows.get("label")),
            metadata=as_mapping(rows.get("metadata")),
        )


# ── rewards ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StepReward:
    """One step's reward with its dimensions kept APART.

    ``total`` is the weighted sum, and the components that produced it stay in
    ``dimensions`` with the signals and penalties that fired. Safety is its own
    entry, always, so an unsafe-but-efficient step cannot look good by averaging.
    """

    index: int = 0
    total: float = 0.0
    dimensions: Mapping[str, float] = field(default_factory=dict)
    signals: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    safety: float = 0.0
    source: str = RewardSource.RULE.value
    terminal: bool = False

    @property
    def unsafe(self) -> bool:
        return StepPenalty.UNSAFE_ACTION.value in self.penalties or self.safety < 0

    def dimension(self, name: str) -> float:
        return float(self.dimensions.get(name, 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "total": round(self.total, 6),
            "dimensions": {key: round(float(value), 6) for key, value in self.dimensions.items()},
            "signals": list(self.signals),
            "penalties": list(self.penalties),
            "reasons": list(self.reasons),
            "safety": round(self.safety, 6),
            "source": self.source,
            "terminal": self.terminal,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepReward:
        rows = as_mapping(data)
        dimensions = {
            str(key): float(value)
            for key, value in as_mapping(rows.get("dimensions")).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return cls(
            index=max(0, as_int(rows.get("index"))),
            total=as_real(rows.get("total"), 0.0) or 0.0,
            dimensions=dimensions,
            signals=as_texts(rows.get("signals")),
            penalties=as_texts(rows.get("penalties")),
            reasons=as_texts(rows.get("reasons")),
            safety=as_real(rows.get("safety"), 0.0) or 0.0,
            source=as_text(rows.get("source"), RewardSource.RULE.value),
            terminal=bool(as_flag(rows.get("terminal"), False)),
        )


#: An alias so a reader sees the phase's own name for the module's record.
MultiObjectiveReward = StepReward


# ── decisions ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """What a policy decided, why in a reason code, and what it rejected.

    ``requires_confirmation`` is carried, never invented by the policy: it is the
    permission layer's answer, and the rollout refuses to execute an action whose
    decision says a person must approve it first.
    """

    selected_action: AgentAction | None = None
    confidence: float = 0.0
    alternatives: tuple[AgentAction, ...] = ()
    expected_value: float | None = None
    value_estimates: Mapping[str, float] = field(default_factory=dict)
    exploration_metadata: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str = ""
    model_id: str = ""
    requires_confirmation: bool = False
    reason_code: str = PolicyReasonCode.PLANNER_DECISION.value
    #: The actions the mask removed, with the reason. Kept so a report can show
    #: what the policy was not allowed to consider — the mask is part of the
    #: decision, not a hidden filter.
    masked: tuple[Mapping[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def action(self) -> AgentAction | None:
        return self.selected_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_action": self.selected_action.as_mapping() if self.selected_action else None,
            "confidence": round(float(self.confidence), 6),
            "alternatives": [item.as_mapping() for item in self.alternatives],
            "expected_value": (
                None if self.expected_value is None else round(float(self.expected_value), 6)
            ),
            "value_estimates": {
                str(key): round(float(value), 6) for key, value in self.value_estimates.items()
            },
            "exploration_metadata": dict(self.exploration_metadata),
            "policy_version": self.policy_version,
            "model_id": self.model_id,
            "requires_confirmation": self.requires_confirmation,
            "reason_code": self.reason_code,
            "reason": self.reason_code,
            "masked": [dict(item) for item in self.masked],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PolicyDecision:
        rows = as_mapping(data)
        selected = _record(rows.get("selected_action"), AgentAction.from_dict)
        return cls(
            selected_action=selected,
            confidence=as_real(rows.get("confidence"), 0.0) or 0.0,
            alternatives=tuple(
                AgentAction.from_dict(item)
                for item in as_mappings(rows.get("alternatives"))
            ),
            expected_value=as_real(rows.get("expected_value")),
            value_estimates={
                str(key): float(value)
                for key, value in as_mapping(rows.get("value_estimates")).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            exploration_metadata=as_mapping(rows.get("exploration_metadata")),
            policy_version=as_text(rows.get("policy_version")),
            model_id=as_text(rows.get("model_id")),
            requires_confirmation=bool(as_flag(rows.get("requires_confirmation"), False)),
            reason_code=as_text(
                rows.get("reason_code"), PolicyReasonCode.PLANNER_DECISION.value
            ),
            masked=as_mappings(rows.get("masked")),
            notes=as_texts(rows.get("notes")),
        )


@dataclass(frozen=True, slots=True)
class PolicyFeedback:
    """What the policy is told after an episode, so a learned one can update.

    Deliberately outcome-shaped only: rewards, returns, credits and the
    verification verdict. There is no field for a critique of the policy's
    reasoning, because none was stored in the first place.
    """

    episode_id: str = ""
    success: bool | None = None
    total_reward: float = 0.0
    step_rewards: tuple[float, ...] = ()
    returns: Mapping[str, float] = field(default_factory=dict)
    credits: Mapping[str, float] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    termination_reason: str = ""
    notes: tuple[str, ...] = ()

    @classmethod
    def for_episode(cls, episode: Episode) -> PolicyFeedback:
        return cls(
            episode_id=episode.episode_id,
            success=episode.success,
            total_reward=episode.total_reward,
            step_rewards=episode.step_rewards(),
            returns=dict(episode.returns),
            credits=dict(episode.credits),
            verification=dict(episode.verification_summary),
            termination_reason=episode.termination_reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "success": self.success,
            "total_reward": round(float(self.total_reward), 6),
            "step_rewards": [round(float(value), 6) for value in self.step_rewards],
            "returns": {str(key): round(float(value), 6) for key, value in self.returns.items()},
            "credits": {str(key): round(float(value), 6) for key, value in self.credits.items()},
            "verification": dict(self.verification),
            "termination_reason": self.termination_reason,
            "notes": list(self.notes),
        }


# ── transitions ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateTransition:
    """One turn of an episode, exactly as the specification describes it.

    state_t + action_t → environment/tool → observation_t+1 → state_t+1, plus
    what the action returned, what verification said, what it earned and any
    recovery that happened. Every optional numeric field stays ``None`` when
    nothing measured it.
    """

    index: int = 0
    previous_state: AgentState = field(default_factory=AgentState)
    action: AgentAction = field(default_factory=AgentAction)
    observation: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    next_state: AgentState = field(default_factory=AgentState)
    verification: Mapping[str, Any] = field(default_factory=dict)
    reward: StepReward | None = None
    step_reward: float = 0.0
    cumulative_reward: float = 0.0
    value_estimate: float | None = None
    advantage_estimate: float | None = None
    recovery_event: Mapping[str, Any] = field(default_factory=dict)
    critique: Mapping[str, Any] = field(default_factory=dict)
    policy_metadata: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    executed: bool = True
    terminal: bool = False
    termination_reason: str = ""
    timestamp: str = field(default_factory=now_iso)
    schema_version: int = AGENTIC_SCHEMA_VERSION

    @property
    def verified(self) -> bool:
        """Whether verification PASSED. An unrunnable check is not a pass."""
        return as_text(self.verification.get("status")) == "pass"

    @property
    def verification_result(self) -> str:
        """The verdict in one word, including ``not_run`` for "nobody checked"."""
        return as_text(self.verification.get("status"), "not_run")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "previous_state": self.previous_state.to_dict(),
            "action": self.action.as_mapping(),
            "observation": dict(self.observation),
            "result": dict(self.result),
            "next_state": self.next_state.to_dict(),
            "verification": dict(self.verification),
            "verification_result": self.verification_result,
            "verified": self.verified,
            "reward": self.reward.to_dict() if self.reward is not None else None,
            "step_reward": round(float(self.step_reward), 6),
            "cumulative_reward": round(float(self.cumulative_reward), 6),
            "value_estimate": self.value_estimate,
            "advantage_estimate": self.advantage_estimate,
            "recovery_event": dict(self.recovery_event),
            "critique": dict(self.critique),
            "policy_metadata": dict(self.policy_metadata),
            "error": self.error,
            "executed": self.executed,
            "terminal": self.terminal,
            "termination_reason": self.termination_reason,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StateTransition:
        rows = as_mapping(data)
        reward_row = rows.get("reward")
        return cls(
            index=max(0, as_int(rows.get("index"))),
            previous_state=AgentState.from_dict(as_mapping(rows.get("previous_state"))),
            action=AgentAction.from_dict(as_mapping(rows.get("action"))),
            observation=as_mapping(rows.get("observation")),
            result=as_mapping(rows.get("result")),
            next_state=AgentState.from_dict(as_mapping(rows.get("next_state"))),
            verification=as_mapping(rows.get("verification")),
            reward=StepReward.from_dict(reward_row) if isinstance(reward_row, Mapping) else None,
            step_reward=as_real(rows.get("step_reward"), 0.0) or 0.0,
            cumulative_reward=as_real(rows.get("cumulative_reward"), 0.0) or 0.0,
            value_estimate=as_real(rows.get("value_estimate")),
            advantage_estimate=as_real(rows.get("advantage_estimate")),
            recovery_event=as_mapping(rows.get("recovery_event")),
            critique=as_mapping(rows.get("critique")),
            policy_metadata=as_mapping(rows.get("policy_metadata")),
            error=as_text(rows.get("error")),
            executed=bool(as_flag(rows.get("executed"), True)),
            terminal=bool(as_flag(rows.get("terminal"), False)),
            termination_reason=as_text(rows.get("termination_reason")),
            timestamp=as_text(rows.get("timestamp")) or now_iso(),
            schema_version=max(1, as_int(rows.get("schema_version"), AGENTIC_SCHEMA_VERSION)),
        )


# ── episodes ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Episode:
    """A whole agentic episode: the goal, the steps, and how it ended.

    An episode is the unit agentic RL learns from, so it carries everything the
    learning side needs — the rewards (as a sequence AND per dimension), the
    verification summary, the credits/returns once they are assigned, the
    resource usage and the identities of the policy and model that produced it.
    """

    episode_id: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    environment_id: str = ""
    start_time: str = field(default_factory=now_iso)
    end_time: str = ""
    initial_state: AgentState = field(default_factory=AgentState)
    goal: str = ""
    steps: tuple[StateTransition, ...] = ()
    total_reward: float = 0.0
    success: bool | None = None
    termination_reason: str = ""
    verification_summary: Mapping[str, Any] = field(default_factory=dict)
    resource_usage: Mapping[str, Any] = field(default_factory=dict)
    policy_version: str = ""
    model_id: str = ""
    difficulty: TaskDifficulty | None = None
    reward_breakdown: Mapping[str, Any] = field(default_factory=dict)
    credits: Mapping[str, float] = field(default_factory=dict)
    returns: Mapping[str, float] = field(default_factory=dict)
    #: Task-level objective totals per reward dimension (step rewards summed).
    dimension_totals: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = EPISODE_SCHEMA_VERSION
    created_at: str = field(default_factory=now_iso)

    # -- reading ---------------------------------------------------------------

    @property
    def length(self) -> int:
        return len(self.steps)

    @property
    def terminal(self) -> bool:
        """Whether the episode ended. An episode with no termination reason did not."""
        return bool(self.termination_reason)

    @property
    def cancelled(self) -> bool:
        return self.termination_reason == TerminationReason.CANCELLED.value

    @property
    def safety_stopped(self) -> bool:
        return self.termination_reason == TerminationReason.SAFETY_STOP.value

    def step_rewards(self) -> tuple[float, ...]:
        return tuple(float(step.step_reward) for step in self.steps)

    def cumulative_rewards(self) -> tuple[float, ...]:
        return tuple(float(step.cumulative_reward) for step in self.steps)

    def rewards_for(self, dimension: str) -> tuple[float, ...]:
        return tuple(
            step.reward.dimension(dimension) if step.reward is not None else 0.0
            for step in self.steps
        )

    def transition(self, index: int) -> StateTransition | None:
        for step in self.steps:
            if step.index == index:
                return step
        return None

    @property
    def verified_steps(self) -> tuple[StateTransition, ...]:
        return tuple(step for step in self.steps if step.verified)

    @property
    def failed_steps(self) -> tuple[StateTransition, ...]:
        return tuple(step for step in self.steps if step.error)

    @property
    def recovery_events(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(step.recovery_event for step in self.steps if step.recovery_event)

    @property
    def safety_penalty(self) -> float:
        """Total safety penalty, kept apart from every other dimension."""
        return sum(
            step.reward.dimension(SAFETY_DIMENSION) if step.reward is not None else 0.0
            for step in self.steps
        )

    def credits_by_index(self) -> dict[int, float]:
        """The per-step credits, keyed by the integer step index."""
        return {int(key): float(value) for key, value in self.credits.items()}

    def to_rollout(self) -> Rollout:
        """This episode in Phase 18's vocabulary, so the same machinery can read it."""
        steps: list[RolloutStep] = []
        for step in self.steps:
            steps.append(
                RolloutStep(
                    index=step.index,
                    action={
                        "action": step.action.action_type,
                        "tool": step.action.tool,
                        "arguments": dict(step.action.arguments),
                    },
                    observation=dict(step.observation),
                    reward=(
                        step.reward.to_dict()
                        if step.reward is not None
                        else {"total_reward": step.step_reward}
                    ),
                    terminal=step.terminal,
                    metadata={
                        "verification": step.verification_result,
                        "executed": step.executed,
                        "episode_id": self.episode_id,
                    },
                )
            )
        return Rollout(
            task_id=self.task_id,
            trajectory_id="",
            environment=self.environment_id,
            policy=self.policy_version or self.model_id,
            seed=as_int(self.metadata.get("seed")),
            status=(
                RolloutStatus.COMPLETED.value
                if self.terminal
                else RolloutStatus.TRUNCATED.value
            ),
            steps=tuple(steps),
            total_reward=self.total_reward,
            source=as_text(self.metadata.get("reward_source"), RewardSource.RULE.value),
            metadata={
                "episode_id": self.episode_id,
                "termination_reason": self.termination_reason,
                "success": self.success,
                "difficulty": self.difficulty.to_dict() if self.difficulty else None,
            },
        )

    def to_trajectory(self) -> AgentTrajectory:
        """The episode's FINAL step as a Phase 15 trajectory with agentic fields.

        One trajectory row describes one step, so a caller that wants the whole
        episode as trajectories maps over :meth:`step_trajectories`. The run-level
        fields (goal, success, verification summary) are filled here so a single
        row is still a complete description of the task.
        """
        rows = self.step_trajectories()
        if rows:
            return rows[-1]
        return AgentTrajectory(
            task_id=self.task_id,
            user_request=self.goal,
            status="failed" if self.success is False else "completed",
            success=self.success,
            failure_reason="",
            episode_id=self.episode_id,
            termination_reason=self.termination_reason,
            step_terminal=self.terminal,
            verification_result=dict(self.verification_summary),
            policy_metadata={
                "policy_version": self.policy_version,
                "model_id": self.model_id,
            },
        )

    def step_trajectories(self) -> tuple[AgentTrajectory, ...]:
        """Every step as a trajectory row, in order, with its agentic fields."""
        rows: list[AgentTrajectory] = []
        for step in self.steps:
            rows.append(
                AgentTrajectory(
                    task_id=self.task_id,
                    user_request=self.goal,
                    status="completed" if step.terminal else "in_progress",
                    success=self.success if step.terminal else None,
                    episode_id=self.episode_id,
                    step_index=step.index,
                    state=step.previous_state.to_dict(),
                    observation=dict(step.observation),
                    action=step.action.as_mapping(),
                    action_type=step.action.action_type,
                    action_arguments=dict(step.action.arguments),
                    action_result=dict(step.result),
                    next_state=step.next_state.to_dict(),
                    step_reward=float(step.step_reward),
                    cumulative_reward=float(step.cumulative_reward),
                    verification_result=dict(step.verification),
                    critique=dict(step.critique),
                    recovery_event=dict(step.recovery_event),
                    policy_metadata=dict(step.policy_metadata),
                    value_estimate=step.value_estimate,
                    advantage_estimate=step.advantage_estimate,
                    step_terminal=step.terminal,
                    termination_reason=step.termination_reason,
                    reward=step.reward.to_dict() if step.reward is not None else None,
                    reward_breakdown=dict(self.reward_breakdown),
                )
            )
        return tuple(rows)

    # -- writing (an episode is immutable; these return a new one) -------------

    def with_credits(self, credits: Mapping[str, Any], returns: Mapping[str, Any]) -> Episode:
        return replace(
            self,
            credits={str(key): float(value) for key, value in credits.items()},
            returns={str(key): float(value) for key, value in returns.items()},
        )

    def with_reward(self, total: float, breakdown: Mapping[str, Any]) -> Episode:
        return replace(self, total_reward=float(total), reward_breakdown=dict(breakdown))

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "environment_id": self.environment_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "initial_state": self.initial_state.to_dict(),
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "total_reward": round(float(self.total_reward), 6),
            "success": self.success,
            "termination_reason": self.termination_reason,
            "verification_summary": dict(self.verification_summary),
            "resource_usage": dict(self.resource_usage),
            "policy_version": self.policy_version,
            "model_id": self.model_id,
            "difficulty": self.difficulty.to_dict() if self.difficulty else None,
            "reward_breakdown": dict(self.reward_breakdown),
            "credits": {str(key): round(float(value), 6) for key, value in self.credits.items()},
            "returns": {str(key): round(float(value), 6) for key, value in self.returns.items()},
            "dimension_totals": {
                str(key): round(float(value), 6) for key, value in self.dimension_totals.items()
            },
            "step_rewards": [round(value, 6) for value in self.step_rewards()],
            "length": self.length,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Episode:
        rows = as_mapping(data)
        difficulty_row = rows.get("difficulty")
        return cls(
            episode_id=as_text(rows.get("episode_id")) or uuid4().hex,
            task_id=as_text(rows.get("task_id")),
            environment_id=as_text(rows.get("environment_id")),
            start_time=as_text(rows.get("start_time")) or now_iso(),
            end_time=as_text(rows.get("end_time")),
            initial_state=AgentState.from_dict(as_mapping(rows.get("initial_state"))),
            goal=as_text(rows.get("goal")),
            steps=tuple(
                StateTransition.from_dict(item) for item in as_mappings(rows.get("steps"))
            ),
            total_reward=as_real(rows.get("total_reward"), 0.0) or 0.0,
            success=as_flag(rows.get("success")),
            termination_reason=as_text(rows.get("termination_reason")),
            verification_summary=as_mapping(rows.get("verification_summary")),
            resource_usage=as_mapping(rows.get("resource_usage")),
            policy_version=as_text(rows.get("policy_version")),
            model_id=as_text(rows.get("model_id")),
            difficulty=(
                TaskDifficulty.from_dict(difficulty_row)
                if isinstance(difficulty_row, Mapping)
                else None
            ),
            reward_breakdown=as_mapping(rows.get("reward_breakdown")),
            credits={
                str(key): float(value)
                for key, value in as_mapping(rows.get("credits")).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            returns={
                str(key): float(value)
                for key, value in as_mapping(rows.get("returns")).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            dimension_totals={
                str(key): float(value)
                for key, value in as_mapping(rows.get("dimension_totals")).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            metadata=as_mapping(rows.get("metadata")),
            schema_version=max(1, as_int(rows.get("schema_version"), EPISODE_SCHEMA_VERSION)),
            created_at=as_text(rows.get("created_at")) or now_iso(),
        )


# ── credit assignment ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CreditAssignmentResult:
    """How much each step contributed, by the method that was asked for.

    Keyed by step index AS A STRING so the record is JSON-safe; use
    :meth:`credit_for` to read it by integer index.
    """

    method: str = CreditMethod.STEP_ACCUMULATION.value
    credits: Mapping[str, float] = field(default_factory=dict)
    returns: Mapping[str, float] = field(default_factory=dict)
    advantages: Mapping[str, float] = field(default_factory=dict)
    discounted_return: float = 0.0
    gamma: float = 1.0
    reasons: tuple[str, ...] = ()

    def credit_for(self, index: int) -> float:
        return float(self.credits.get(str(index), 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "credits": {key: round(float(value), 6) for key, value in self.credits.items()},
            "returns": {key: round(float(value), 6) for key, value in self.returns.items()},
            "advantages": {key: round(float(value), 6) for key, value in self.advantages.items()},
            "discounted_return": round(float(self.discounted_return), 6),
            "gamma": self.gamma,
            "reasons": list(self.reasons),
        }


# ── policies ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """A candidate policy, tracked the way a model is — because it IS one.

    The registry extends the model registry's idea rather than inventing a second
    one: a policy has an id, a version, the run that produced it, the environment
    and reward/verifier versions it was evaluated against, its evaluation results
    and a STATUS. Nothing here promotes anything.
    """

    policy_id: str = ""
    model_id: str = ""
    policy_version: str = ""
    training_run_id: str = ""
    environment: str = ""
    curriculum_version: str = ""
    reward_version: str = ""
    verifier_versions: Mapping[str, str] = field(default_factory=dict)
    evaluation_results: tuple[Mapping[str, Any], ...] = ()
    status: str = PolicyStatus.EXPERIMENTAL.value
    #: Whether this policy changes weights. A mock policy says False, always.
    learns: bool = False
    simulated: bool = True
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    notes: tuple[str, ...] = ()
    schema_version: int = POLICY_RECORD_SCHEMA_VERSION

    @property
    def live(self) -> bool:
        """Whether this policy may act on real work."""
        return self.status in LIVE_STATUSES

    @property
    def candidate(self) -> bool:
        return self.status in CANDIDATE_STATUSES

    def evaluated(self) -> bool:
        return bool(self.evaluation_results)

    def latest_evaluation(self) -> Mapping[str, Any]:
        return self.evaluation_results[-1] if self.evaluation_results else {}

    def with_status(self, status: str, *, note: str = "") -> PolicyRecord:
        if status not in POLICY_STATUSES:
            raise ValueError(
                f"unknown policy status {status!r}: expected one of "
                + ", ".join(POLICY_STATUSES)
            )
        notes = (*self.notes, note) if note else self.notes
        return replace(self, status=status, updated_at=now_iso(), notes=notes)

    def with_evaluation(self, result: Mapping[str, Any]) -> PolicyRecord:
        return replace(
            self,
            evaluation_results=(*self.evaluation_results, dict(result)),
            updated_at=now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "model_id": self.model_id,
            "policy_version": self.policy_version,
            "training_run_id": self.training_run_id,
            "environment": self.environment,
            "curriculum_version": self.curriculum_version,
            "reward_version": self.reward_version,
            "verifier_versions": dict(self.verifier_versions),
            "evaluation_results": [dict(item) for item in self.evaluation_results],
            "status": self.status,
            "learns": self.learns,
            "simulated": self.simulated,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": list(self.notes),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PolicyRecord:
        rows = as_mapping(data)
        status = as_text(rows.get("status"), PolicyStatus.EXPERIMENTAL.value)
        if status not in POLICY_STATUSES:
            status = PolicyStatus.EXPERIMENTAL.value
        return cls(
            policy_id=as_text(rows.get("policy_id")),
            model_id=as_text(rows.get("model_id")),
            policy_version=as_text(rows.get("policy_version")),
            training_run_id=as_text(rows.get("training_run_id")),
            environment=as_text(rows.get("environment")),
            curriculum_version=as_text(rows.get("curriculum_version")),
            reward_version=as_text(rows.get("reward_version")),
            verifier_versions={
                str(key): str(value)
                for key, value in as_mapping(rows.get("verifier_versions")).items()
            },
            evaluation_results=as_mappings(rows.get("evaluation_results")),
            status=status,
            learns=bool(as_flag(rows.get("learns"), False)),
            simulated=bool(as_flag(rows.get("simulated"), True)),
            created_at=as_text(rows.get("created_at")) or now_iso(),
            updated_at=as_text(rows.get("updated_at")) or now_iso(),
            notes=as_texts(rows.get("notes")),
            schema_version=max(
                1, as_int(rows.get("schema_version"), POLICY_RECORD_SCHEMA_VERSION)
            ),
        )


# ── evaluation ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgenticEvaluationResult:
    """What one policy did across a set of tasks, dimension by dimension.

    ``metrics`` holds ``float | None``: ``None`` means the metric was not
    measurable for this set (no recovery ever happened, so recovery quality has
    no sample). That is different from a zero, and it is reported as such so a
    policy cannot look better by being unmeasurable.
    """

    policy_id: str = ""
    policy_version: str = ""
    episodes: int = 0
    metrics: Mapping[str, float | None] = field(default_factory=dict)
    dimensions: Mapping[str, float] = field(default_factory=dict)
    by_horizon: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    by_outcome: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    by_difficulty: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    safety: Mapping[str, Any] = field(default_factory=dict)
    efficiency: Mapping[str, Any] = field(default_factory=dict)
    sample_size: int = 0
    minimum_sample_size: int = 0
    notes: tuple[str, ...] = ()
    episode_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    schema_version: int = EVALUATION_SCHEMA_VERSION

    @property
    def meets_minimum_sample(self) -> bool:
        return self.sample_size >= self.minimum_sample_size

    def metric(self, name: str) -> float | None:
        return self.metrics.get(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "episodes": self.episodes,
            "sample_size": self.sample_size,
            "minimum_sample_size": self.minimum_sample_size,
            "meets_minimum_sample": self.meets_minimum_sample,
            "metrics": {
                str(key): (None if value is None else round(float(value), 6))
                for key, value in self.metrics.items()
            },
            "dimensions": {
                str(key): round(float(value), 6) for key, value in self.dimensions.items()
            },
            "by_horizon": {str(key): dict(value) for key, value in self.by_horizon.items()},
            "by_outcome": {str(key): dict(value) for key, value in self.by_outcome.items()},
            "by_difficulty": {str(key): dict(value) for key, value in self.by_difficulty.items()},
            "safety": dict(self.safety),
            "efficiency": dict(self.efficiency),
            "notes": list(self.notes),
            "episode_ids": list(self.episode_ids),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgenticEvaluationResult:
        rows = as_mapping(data)
        return cls(
            policy_id=as_text(rows.get("policy_id")),
            policy_version=as_text(rows.get("policy_version")),
            episodes=max(0, as_int(rows.get("episodes"))),
            metrics={
                str(key): (None if value is None else float(value))
                for key, value in as_mapping(rows.get("metrics")).items()
                if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))
            },
            dimensions={
                str(key): float(value)
                for key, value in as_mapping(rows.get("dimensions")).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            by_horizon={
                str(key): as_mapping(value)
                for key, value in as_mapping(rows.get("by_horizon")).items()
            },
            by_outcome={
                str(key): as_mapping(value)
                for key, value in as_mapping(rows.get("by_outcome")).items()
            },
            by_difficulty={
                str(key): as_mapping(value)
                for key, value in as_mapping(rows.get("by_difficulty")).items()
            },
            safety=as_mapping(rows.get("safety")),
            efficiency=as_mapping(rows.get("efficiency")),
            sample_size=max(0, as_int(rows.get("sample_size"))),
            minimum_sample_size=max(0, as_int(rows.get("minimum_sample_size"))),
            notes=as_texts(rows.get("notes")),
            episode_ids=as_texts(rows.get("episode_ids")),
            created_at=as_text(rows.get("created_at")) or now_iso(),
            schema_version=max(1, as_int(rows.get("schema_version"), EVALUATION_SCHEMA_VERSION)),
        )


# ── promotion ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """The verdict of the promotion gates, with every check that produced it."""

    approved: bool = False
    policy_id: str = ""
    status: str = PolicyStatus.EVALUATING.value
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    baseline_metrics: Mapping[str, Any] = field(default_factory=dict)
    checks: tuple[Mapping[str, Any], ...] = ()
    failures: tuple[str, ...] = ()
    requires_approval: bool = True
    approved_by: str = ""
    sample_size: int = 0
    minimum_sample_size: int = 0
    reasons: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "policy_id": self.policy_id,
            "status": self.status,
            "thresholds": dict(self.thresholds),
            "metrics": dict(self.metrics),
            "baseline_metrics": dict(self.baseline_metrics),
            "checks": [dict(item) for item in self.checks],
            "failures": list(self.failures),
            "requires_approval": self.requires_approval,
            "approved_by": self.approved_by,
            "sample_size": self.sample_size,
            "minimum_sample_size": self.minimum_sample_size,
            "reasons": list(self.reasons),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PromotionDecision:
        rows = as_mapping(data)
        return cls(
            approved=bool(as_flag(rows.get("approved"), False)),
            policy_id=as_text(rows.get("policy_id")),
            status=as_text(rows.get("status"), PolicyStatus.EVALUATING.value),
            thresholds=as_mapping(rows.get("thresholds")),
            metrics=as_mapping(rows.get("metrics")),
            baseline_metrics=as_mapping(rows.get("baseline_metrics")),
            checks=as_mappings(rows.get("checks")),
            failures=as_texts(rows.get("failures")),
            requires_approval=bool(as_flag(rows.get("requires_approval"), True)),
            approved_by=as_text(rows.get("approved_by")),
            sample_size=max(0, as_int(rows.get("sample_size"))),
            minimum_sample_size=max(0, as_int(rows.get("minimum_sample_size"))),
            reasons=as_texts(rows.get("reasons")),
            created_at=as_text(rows.get("created_at")) or now_iso(),
        )


__all__ = [
    "ACTION_TYPES",
    "AGENTIC_SCHEMA_VERSION",
    "AGENTIC_VERSION",
    "CANDIDATE_STATUSES",
    "CREDIT_METHODS",
    "EFFICIENCY_DIMENSIONS",
    "EPISODE_SCHEMA_VERSION",
    "EVALUATION_SCHEMA_VERSION",
    "EXPLORATION_STRATEGIES",
    "LIVE_STATUSES",
    "MAX_CURRICULUM_LEVEL",
    "MAX_EPISODE_STEPS_CEILING",
    "MAX_PLANNING_HORIZON_CEILING",
    "MAX_RETRIES_CEILING",
    "MIN_CURRICULUM_LEVEL",
    "POLICY_RECORD_SCHEMA_VERSION",
    "POLICY_REASON_CODES",
    "POLICY_STATUSES",
    "REWARD_DIMENSIONS",
    "SAFETY_DIMENSION",
    "STEP_PENALTIES",
    "STEP_SIGNALS",
    "TERMINATION_REASONS",
    "ActionType",
    "AgentAction",
    "AgentState",
    "AgenticEvaluationResult",
    "CreditAssignmentResult",
    "CreditMethod",
    "Episode",
    "ExplorationStrategy",
    "MultiObjectiveReward",
    "PolicyDecision",
    "PolicyFeedback",
    "PolicyReasonCode",
    "PolicyRecord",
    "PolicyStatus",
    "PromotionDecision",
    "RewardComponent",
    "RewardDimension",
    "RewardPenalty",
    "StateTransition",
    "StepPenalty",
    "StepReward",
    "StepSignal",
    "TaskDifficulty",
    "TaskDifficultyLevel",
    "TerminationReason",
    "as_flag",
    "as_int",
    "as_mapping",
    "as_mappings",
    "as_real",
    "as_reals",
    "as_text",
    "as_texts",
]
