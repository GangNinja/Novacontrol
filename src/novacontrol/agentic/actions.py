"""The action space, and the mask that keeps a policy inside it.

A policy never gets to pick from "everything". It picks from the actions the
environment offers, filtered by four questions the mask asks of each one:

    unavailable    the state does not contain the tool or capability it needs
    unauthorized   the permission layer refuses it as it stands
    unsafe         it needs a person's approval and nobody has given one
    incompatible   the environment will not accept it in this state

The mask is the phase's safety boundary made concrete. It is built from the
EXISTING permission layer — :class:`~novacontrol.reliability.permissions.PermissionManager`
— so an action the rest of NovaControl would refuse cannot sneak through a
policy, and a decision that says "this needs confirmation" is reported rather
than dropped, because a report has to show what was asked about.

``allow_confirmation`` is how a caller says "a person is in the loop for this
run". It never means "ignore the permission layer": it means the mask may keep
actions that require confirmation, and the rollout will ask before executing
them. With it off — the default — those actions are masked out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.agentic.models import (
    ActionType,
    AgentAction,
    AgentState,
    as_text,
    as_texts,
)
from novacontrol.core.security import RiskLevel
from novacontrol.planning.models import PlanStep, StepEffect, VerificationSpec

#: Why an action was masked out. Every blocked action carries exactly one.
MASK_UNAVAILABLE = "unavailable"
MASK_UNAUTHORIZED = "unauthorized"
MASK_UNSAFE = "unsafe"
MASK_INCOMPATIBLE = "incompatible"
MASK_CONFIRMATION_REQUIRED = "confirmation_required"

MASK_REASONS: tuple[str, ...] = (
    MASK_UNAVAILABLE,
    MASK_UNAUTHORIZED,
    MASK_UNSAFE,
    MASK_INCOMPATIBLE,
    MASK_CONFIRMATION_REQUIRED,
)

#: Which plan effect an action type has. Used when an action has to be handed to
#: the recovery layer, which reasons in effects rather than action types.
_EFFECT_BY_TYPE: Mapping[str, StepEffect] = {
    ActionType.TOOL.value: StepEffect.LOCAL_WRITE,
    ActionType.PLANNER_STEP.value: StepEffect.READ_ONLY,
    ActionType.RECOVERY.value: StepEffect.READ_ONLY,
    ActionType.INFORMATION.value: StepEffect.READ_ONLY,
    ActionType.COMMUNICATION.value: StepEffect.EXTERNAL,
    ActionType.LOCAL_COMPUTATION.value: StepEffect.READ_ONLY,
    ActionType.ENVIRONMENT.value: StepEffect.LOCAL_WRITE,
    ActionType.NOOP.value: StepEffect.READ_ONLY,
}

#: Effects an action may declare for itself with ``expected_effect``.
_EFFECTS: Mapping[str, StepEffect] = {item.value: item for item in StepEffect}


@dataclass(frozen=True, slots=True)
class MaskedAction:
    """One action that did not survive the mask, and the reason why."""

    action: AgentAction
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action.action_id,
            "name": self.action.name,
            "action_type": self.action.action_type,
            "reason": self.reason,
            "detail": self.detail,
            "requires_confirmation": self.action.requires_confirmation,
            "risk_level": self.action.risk_level.value,
        }


@dataclass(frozen=True, slots=True)
class ActionMask:
    """The actions a policy may choose from, and what was removed and why."""

    allowed: tuple[AgentAction, ...] = ()
    blocked: tuple[MaskedAction, ...] = ()
    confirmations_available: bool = False
    #: Allowed actions the mask kept ONLY because somebody can be asked: their
    #: ids. The rollout asks about every one of them before it runs, so a mask
    #: that kept an action on the strength of "a person is reachable" cannot turn
    #: into an execution nobody approved.
    confirmation_required: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.allowed

    def ids(self) -> tuple[str, ...]:
        return tuple(action.action_id for action in self.allowed)

    def names(self) -> tuple[str, ...]:
        return tuple(action.name for action in self.allowed)

    def find(self, action_id: str) -> AgentAction | None:
        for action in self.allowed:
            if action.action_id == action_id:
                return action
        return None

    def blocked_reason(self, action_id: str) -> str:
        for item in self.blocked:
            if item.action.action_id == action_id:
                return item.reason
        return ""

    def needs_confirmation(self, action_id: str) -> bool:
        """Whether this ALLOWED action still needs a person's yes before it runs."""
        return action_id in self.confirmation_required

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": [action.as_mapping() for action in self.allowed],
            "blocked": [item.to_dict() for item in self.blocked],
            "confirmations_available": self.confirmations_available,
            "confirmation_required": list(self.confirmation_required),
        }


@dataclass
class ActionMasker:
    """Builds the mask from the permission layer and the current state.

    ``permission_manager`` is the Phase 8 layer. When none is wired, the masker
    still enforces the rules it can see for itself — a destructive or
    irreversible action without an approval is masked, always — because "there is
    no permission layer here" must not mean "anything goes".

    Its answers to the approval questions are part of the interface a caller
    relies on: :meth:`is_approved` (a yes already given) and
    :meth:`approval_available` (whether a yes could come from anywhere). The mask
    it builds reports the allowed actions that still need one
    (``ActionMask.confirmation_required``), and the rollout asks about exactly
    those.
    """

    permission_manager: Any | None = None
    #: Whether a person is available to approve actions this run. When False,
    #: anything needing confirmation is masked out.
    allow_confirmation: bool = False
    #: Actions a caller has already approved (by id, or by tool name).
    approved: frozenset[str] = frozenset()
    #: The environment's acceptance check, when it has one. Returns (ok, reason).
    acceptance: Any | None = None

    # -- the mask ---------------------------------------------------------------

    def mask(
        self, state: AgentState, candidates: Iterable[AgentAction]
    ) -> ActionMask:
        allowed: list[AgentAction] = []
        blocked: list[MaskedAction] = []
        confirmation_required: list[str] = []
        for action in candidates:
            reason, detail, needs_confirmation = self._reason_to_mask(state, action)
            if reason:
                blocked.append(MaskedAction(action=action, reason=reason, detail=detail))
                continue
            allowed.append(action)
            if needs_confirmation and not self.is_approved(action):
                confirmation_required.append(action.action_id)
        return ActionMask(
            allowed=tuple(allowed),
            blocked=tuple(blocked),
            confirmations_available=self.allow_confirmation,
            confirmation_required=tuple(confirmation_required),
        )

    def allows(self, state: AgentState, action: AgentAction) -> bool:
        """Whether one action survives the mask, without building the whole mask."""
        reason, _detail, _needs_confirmation = self._reason_to_mask(state, action)
        return not reason

    # -- approvals ---------------------------------------------------------------

    def is_approved(self, action: AgentAction) -> bool:
        """Whether a caller already approved this exact action, or its tool."""
        return bool(
            action.action_id in self.approved
            or (bool(action.tool) and action.tool in self.approved)
        )

    def approval_available(self, action: AgentAction) -> bool:
        """Whether an approval for this action could come from ANYWHERE at all.

        ``allow_confirmation`` means a person is reachable for this run; an entry
        in ``approved`` means one already said yes. Neither is an approval by
        itself — the rollout still asks — but with both absent there is nobody to
        ask, and an action needing confirmation is refused rather than run.
        """
        return bool(self.allow_confirmation or self.is_approved(action))

    # -- the four questions -----------------------------------------------------

    def _reason_to_mask(
        self, state: AgentState, action: AgentAction
    ) -> tuple[str, str, bool]:
        """Why the action is masked (empty means it is not), the detail, and
        whether it would still need a person's approval once allowed."""
        if not action.action_id:
            return MASK_INCOMPATIBLE, "the action has no id, so nothing could record it", False
        # 1. Unavailable: the state says this installation cannot do it.
        if action.tool and state.tools and action.tool not in state.tools:
            return (
                MASK_UNAVAILABLE,
                f"tool {action.tool!r} is not in the state's available tools",
                False,
            )
        if action.capability and state.capabilities and action.capability not in state.capabilities:
            return (
                MASK_UNAVAILABLE,
                f"capability {action.capability!r} is not in the state's available capabilities",
                False,
            )
        # 2. Incompatible: the environment will not accept it here.
        incompatible = self._incompatible(state, action)
        if incompatible:
            return MASK_INCOMPATIBLE, incompatible, False
        # 3 & 4. Unauthorized / unsafe, from the permission layer.
        return self._permission_reason(action)

    def _incompatible(self, state: AgentState, action: AgentAction) -> str:
        declared = as_text(action.expected_effect)
        if declared and declared not in _EFFECTS:
            return (
                f"expected_effect {declared!r} is not a known effect "
                f"({', '.join(sorted(_EFFECTS))})"
            )
        if self.acceptance is None:
            return ""
        try:
            verdict = self.acceptance(state, action)
        except Exception as error:  # pragma: no cover - a broken environment probe
            return f"the environment's acceptance check raised {type(error).__name__}"
        if isinstance(verdict, Mapping):
            if bool(verdict.get("accepted", True)):
                return ""
            return as_text(verdict.get("reason"), "the environment refused the action")
        if isinstance(verdict, tuple) and verdict:
            accepted = bool(verdict[0])
            if accepted:
                return ""
            return as_text(verdict[1], "the environment refused the action") if len(verdict) > 1 else "refused"
        if isinstance(verdict, bool):
            return "" if verdict else "the environment refused the action"
        return ""

    def _permission_reason(self, action: AgentAction) -> tuple[str, str, bool]:
        manager = self.permission_manager
        approved = self.is_approved(action)
        declared_unsafe = (
            action.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
            or not action.reversible
            or action.requires_confirmation
        )
        if manager is None:
            # No permission layer wired: the masker is the last line, and it
            # keeps the two rules that can be checked without one.
            if action.requires_confirmation and not (approved or self.allow_confirmation):
                return (
                    MASK_CONFIRMATION_REQUIRED,
                    "the action declares that it needs confirmation and no approval is available",
                    False,
                )
            if not action.reversible and not approved:
                return (
                    MASK_UNSAFE,
                    "the action is irreversible and nothing approved it",
                    False,
                )
            return "", "", bool(action.requires_confirmation and not approved)
        try:
            assessment = manager.assess(
                action.tool,
                action=as_text(action.expected_effect) or action.action_type,
                parameters=dict(action.arguments),
            )
            decision = manager.check(
                action.tool,
                action=as_text(action.expected_effect) or action.action_type,
                approved=approved,
                parameters=dict(action.arguments),
            )
        except Exception as error:  # pragma: no cover - a broken permission layer
            # An evaluator that throws must not become an allow.
            return (
                MASK_UNSAFE,
                f"the permission layer raised {type(error).__name__}; the action is not cleared",
                False,
            )
        if not decision.allow:
            return (
                MASK_UNAUTHORIZED,
                as_text(decision.reason, "the permission layer refused it"),
                False,
            )
        needs_confirmation = bool(
            getattr(decision, "requires_confirmation", False)
            or getattr(assessment, "requires_confirmation", False)
            or action.requires_confirmation
            or declared_unsafe
        )
        if needs_confirmation and not approved:
            if self.allow_confirmation:
                # Kept — but the mask says so, so the rollout asks before it runs.
                return "", "", True
            return (
                MASK_CONFIRMATION_REQUIRED,
                "the permission layer requires a person's approval and this run has none",
                False,
            )
        return "", "", False


# ── the action space ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ActionSpace:
    """The candidate actions available in one state.

    Candidates come from the environment (what it will accept), the tool layer
    (what this installation can do) and the planner (the next step it proposed).
    The space does not decide anything — it collects, and the mask filters.
    """

    actions: tuple[AgentAction, ...] = ()

    def __len__(self) -> int:
        return len(self.actions)

    def __iter__(self) -> Any:
        return iter(self.actions)

    def names(self) -> tuple[str, ...]:
        return tuple(action.name for action in self.actions)

    def find(self, name: str) -> AgentAction | None:
        needle = name.strip().lower()
        for action in self.actions:
            if needle in (action.tool.lower(), action.capability.lower(), action.label.lower()):
                return action
        return None

    def without(self, action_ids: Iterable[str]) -> ActionSpace:
        removed = {str(item) for item in action_ids}
        return ActionSpace(
            actions=tuple(item for item in self.actions if item.action_id not in removed)
        )

    def to_dict(self) -> dict[str, Any]:
        return {"actions": [action.as_mapping() for action in self.actions]}

    @classmethod
    def from_mappings(cls, rows: Iterable[Mapping[str, Any]]) -> ActionSpace:
        return cls(
            actions=tuple(
                AgentAction.from_dict(item) for item in rows if isinstance(item, Mapping)
            )
        )


def effect_for(action_type: str) -> StepEffect:
    """The plan effect an action type has (READ_ONLY when nothing says)."""
    return _EFFECT_BY_TYPE.get(as_text(action_type), StepEffect.READ_ONLY)


def declaration_for_action(action: AgentAction) -> dict[str, Any]:
    """The permission declaration an action carries, in the layer's own words."""
    return {
        "risk_level": action.risk_level.value,
        "reversible": action.reversible,
        "requires_confirmation": action.requires_confirmation,
        "destructive": action.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        and not action.reversible,
        "external_side_effect": action.action_type == ActionType.COMMUNICATION.value,
    }


def action_to_plan_step(action: AgentAction, *, step_id: str = "") -> PlanStep:
    """An action as a plan step, so the recovery layer can reason about it.

    The recovery engine speaks PlanStep/StepError; the agentic loop speaks
    AgentAction. This is the single translation between them, and it is
    deliberately lossy in one direction only: an action's risk and
    reversibility become the step EFFECT, which is what every safety rule in the
    recovery layer is keyed on.
    """
    effect = effect_for(action.action_type)
    if action.expected_effect in _EFFECTS:
        effect = _EFFECTS[action.expected_effect]
    if action.risk_level is RiskLevel.CRITICAL:
        effect = StepEffect.DESTRUCTIVE
    elif not action.reversible:
        effect = StepEffect.DESTRUCTIVE if effect in (StepEffect.LOCAL_WRITE,) else effect
    label = action.label or action.name
    return PlanStep(
        id=step_id or action.action_id,
        title=label,
        description=action.expected_effect or label,
        action=action.capability or action.action_type,
        tool=action.tool,
        parameters=dict(action.arguments),
        expected_result=action.expected_effect,
        verification=VerificationSpec(),
        effect=effect,
    )


def candidate_tools(state: AgentState) -> tuple[str, ...]:
    """The tool names a state says are available, in a stable order."""
    return tuple(sorted(as_texts(state.tools)))


__all__ = [
    "MASK_CONFIRMATION_REQUIRED",
    "MASK_INCOMPATIBLE",
    "MASK_REASONS",
    "MASK_UNAVAILABLE",
    "MASK_UNAUTHORIZED",
    "MASK_UNSAFE",
    "ActionMask",
    "ActionMasker",
    "ActionSpace",
    "MaskedAction",
    "action_to_plan_step",
    "candidate_tools",
    "declaration_for_action",
    "effect_for",
]
