"""What Phase 24 can do, classified — and registered with the ONE registry.

The classification says what each capability IS in this build rather than what the
phase is called, and it reuses the capability vocabulary the other phases use
verbatim (two vocabularies for the same question is how a capability table starts
to lie):

    goal_representation      IMPLEMENTED       a goal with its conditions, budget
                                               and the fields it does NOT have
    decomposition            IMPLEMENTED       strategic → subgoal → task, with
                                               declared inputs, outputs and checks
    dependency_ordering      IMPLEMENTED       dependency-depth order, cycles and
                                               missing prerequisites reported
    plan_validation          IMPLEMENTED       cycles, missing prerequisites,
                                               unmeasurable completion, impossible
                                               constraints
    state_grounding          PARTIALLY_IMPL    real when a world model is wired;
                                               assertions and UNKNOWN otherwise
    candidate_generation     IMPLEMENTED       a deterministic baseline AND a
                                               bounded beam, both stating their
                                               ceilings
    plan_ranking             IMPLEMENTED       a documented weighted comparison
    outcome_estimation       IMPLEMENTED       every estimate carries its BASIS;
                                               unavailable is an answer
    action_policy            IMPLEMENTED       the shared permission/risk layer,
                                               asked separately from revalidation
    bounded_execution        IMPLEMENTED       through the Phase 4 executor, with a
                                               fresh-state gate in front of steps
    monitoring_revalidation  IMPLEMENTED       preconditions re-read before a step
    completion_verification  IMPLEMENTED       verified conditions, never "the steps
                                               ended"
    replanning               IMPLEMENTED       bounded by the goal's own ceiling
    persistence_restore      IMPLEMENTED       one key per goal, restore reports
                                               what it skipped
    world_state              PROVIDER_DEPENDENT a real reading needs a world model
    prediction               PROVIDER_DEPENDENT a projection needs a predictor or a
                                               learned rule; nothing is fabricated
    learned_knowledge        PROVIDER_DEPENDENT Phase 23 rules are read WITH their
                                               scope and status
    parallel_execution       PARTIALLY_IMPL    read-only steps may share a wave; the
                                               planner never parallelizes conflict
    generalization           DEFERRED          Phase 26 territory
                                             (the game-agent capability is no longer
                                             deferred: Phase 25 ships it as
                                             ``embodied.*`` rows on this registry)

Availability is a property of the MACHINE and is read live from the manager on
every call, never from a boot snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.perception.models import CapabilityState, CapabilityStatus

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "PlanningCapabilityDefinition",
    "capability_rows",
    "register_planning_capabilities",
]

#: The schema version of this table, so a stored report can say what it read.
CAPABILITY_SCHEMA_VERSION = "phase24.1"


@dataclass(frozen=True, slots=True)
class PlanningCapabilityDefinition:
    """One capability as this build ships it, plus how to probe it."""

    capability_id: str
    state: CapabilityState
    description: str
    #: ``deterministic`` needs nothing wired; ``world``, ``prediction``,
    #: ``permission``, ``executor`` and ``deferred`` name a collaborator or a
    #: deliberate absence.
    probe: str = "deterministic"
    requires_model: bool = False
    requires_permission: bool = False
    notes: str = ""
    permissions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PlanningCapabilityDefinition:
        return cls(
            capability_id=str(data.get("capability_id", "planning.unknown")),
            state=CapabilityState(str(data.get("state", CapabilityState.FUTURE.value))),
            description=str(data.get("description", "")),
            probe=str(data.get("probe", "deterministic")),
            requires_model=bool(data.get("requires_model", False)),
            requires_permission=bool(data.get("requires_permission", False)),
            notes=str(data.get("notes", "")),
            permissions=tuple(str(item) for item in data.get("permissions", ()) or ()),
            outputs=tuple(str(item) for item in data.get("outputs", ()) or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "state": self.state.value,
            "description": self.description,
            "probe": self.probe,
            "requires_model": self.requires_model,
            "requires_permission": self.requires_permission,
            "notes": self.notes,
            "permissions": list(self.permissions),
            "outputs": list(self.outputs),
        }


CAPABILITY_TABLE: tuple[PlanningCapabilityDefinition, ...] = (
    PlanningCapabilityDefinition(
        capability_id="planning.goal_representation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Hold a goal as data: its normalized request, success conditions, "
            "initial state and evidence, constraints and exclusions, budgets, "
            "required permissions, failure and cancellation conditions, provenance "
            "and correlation id — including the fields the request did NOT supply."
        ),
        outputs=("goal_id", "success_conditions", "missing"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.decomposition",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Decompose a goal into tasks that each declare their action, inputs, "
            "expected outputs, preconditions, effects, dependencies, required "
            "capability, estimated cost and verification procedure."
        ),
        outputs=("tasks", "issues"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.dependency_ordering",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Order tasks by dependency depth, detect circular dependencies, and "
            "report a task whose prerequisite no other task produces."
        ),
        outputs=("order", "issues"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.plan_validation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Refuse to call a decomposition plannable when it has a cycle, a missing "
            "prerequisite, a task under an impossible constraint, or a goal with no "
            "measurable completion condition."
        ),
        outputs=("issues", "refusal"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.state_grounding",
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description=(
            "Ground a plan's preconditions and a goal's conditions in the best "
            "available state, distinguishing OBSERVED, INFERRED, PREDICTED, UNKNOWN, "
            "STALE and CONFLICTING facts and keeping their evidence references."
        ),
        probe="world",
        permissions=("filesystem:read",),
        notes=(
            "A real reading needs Phase 22's world model; "
            "without one every fact is an assertion."
        ),
        outputs=("facts", "staleness", "limitations"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.candidate_generation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Generate candidate plans from a validated decomposition — a "
            "deterministic dependency-order baseline and a bounded cost-aware beam — "
            "never inventing a plan the grounded state cannot support."
        ),
        outputs=("candidates", "expansions", "truncated"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.plan_ranking",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Compare candidates by a documented weighted score — feasibility, cost, "
            "risk, steps, unmet preconditions, expected verification — and name one."
        ),
        outputs=("comparison", "selected", "score"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.outcome_estimation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "State what each step is EXPECTED to do, and where that came from: a "
            "world-model projection, a scoped learned rule, the task's own declared "
            "effect, or nothing at all. A declaration is never dressed as a prediction."
        ),
        outputs=("estimates", "basis", "predicted"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.action_policy",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Decide which steps may run: the goal's exclusions first, then the shared "
            "permission and risk layer, then the goal's own approval conditions, then "
            "the effect class — and report which source decided."
        ),
        probe="permission",
        requires_permission=True,
        permissions=("planning.authorize",),
        outputs=("verdicts", "allowed", "needs_approval", "denied"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.bounded_execution",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Execute a selected plan through the existing Phase 4 executor, with a "
            "fresh-state gate in front of the step runner: a step whose preconditions "
            "no longer hold is DENIED rather than attempted."
        ),
        probe="executor",
        permissions=("planning.execute",),
        outputs=("run", "status", "revalidations"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.monitoring_revalidation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Re-read the state immediately before each step and answer PROCEED, "
            'OBSERVE or REFUSE — a three-valued verdict, because "cannot tell" is '
            'neither "yes" nor "no".'
        ),
        outputs=("revalidations", "verdict"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.completion_verification",
        state=CapabilityState.IMPLEMENTED,
        description=(
            'Answer "is it done?" only from a goal\'s success conditions checked '
            "against an observation: verified, contradicted, or unknown because "
            "nothing could observe. The end of the steps is never completion."
        ),
        outputs=("completion", "status", "observation"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.replanning",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Decide whether a deviation justifies replanning, bounded by the goal's "
            "own ceiling, and refuse to attempt the same plan again unchanged."
        ),
        outputs=("decision", "replans_used", "exhausted"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.persistence",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Persist goals and their decompositions through the shared JSON state "
            "store, one key per goal; restore reports what it skipped."
        ),
        outputs=("stored", "restore_report"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.world_state",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Read Phase 22's world state for grounding. Read-only: this layer never "
            "writes world state and never treats an available world model as a trained "
            "predictor."
        ),
        probe="world",
        requires_model=True,
        permissions=("filesystem:read",),
        notes="Unavailable without a wired world model; nothing is invented in its place.",
        outputs=("facts",),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.prediction",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Project a step's outcome from a wired predictor. When the provider "
            "reports model_unavailable, the estimate is UNAVAILABLE and says so rather "
            "than inventing a plausible state."
        ),
        probe="prediction",
        requires_model=True,
        notes=(
            "No trained predictive model ships with this build; "
            "a transition is rule-based and labelled."
        ),
        outputs=("estimate", "basis"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.learned_knowledge",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Consult Phase 23's scoped learned rules when estimating an action, "
            "honouring the rule's scope and its status: a retired, contradicted or "
            "out-of-scope rule is not borrowed."
        ),
        probe="rules",
        requires_model=True,
        notes="Available only once a rule exists for the action AND scope in question.",
        outputs=("estimate", "rule_id", "status"),
    ),
    PlanningCapabilityDefinition(
        capability_id="planning.parallel_execution",
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description=(
            "Mark read-only steps parallel-safe and let the executor's own wave run "
            "them together. A step that shares state with another is never "
            "parallelized: only read-only effects are declared parallel-safe."
        ),
        notes="Only READ_ONLY steps carry the flag; ordering still respects every dependency.",
        outputs=("order",),
    ),
    # NOTE: there is deliberately no ``planning.game_agent`` row here any more.
    # It existed as a placeholder while Phase 25 was still ahead, and Phase 25 has
    # now shipped that capability where it belongs — the ``embodied`` package
    # declares its own rows (``embodied.gridworld_environment``,
    # ``embodied.action_selection``, …) on the SAME registry. Keeping a "future"
    # row beside them would have this build answer "not built" and "available"
    # about the same thing.
    PlanningCapabilityDefinition(
        capability_id="planning.generalization",
        state=CapabilityState.FUTURE,
        description=(
            "Generalization, ARC and intelligence evaluation. NOT implemented by this "
            "phase: Phase 26 owns it."
        ),
        probe="deferred",
        notes="Deferred deliberately: Phase 26 owns it.",
        outputs=(),
    ),
)


def _availability(
    manager: Any | None, definition: PlanningCapabilityDefinition
) -> tuple[bool | None, str, str]:
    """Live availability, read from the manager — ``None`` means \"cannot tell\"."""
    if definition.probe == "deferred":
        return False, "deferred", definition.notes or "this phase does not implement it"
    if definition.probe == "deterministic":
        return True, "", "implemented in this build; no provider needed"
    if manager is None:
        return None, "", "no goal-planning manager is wired in this build"
    wired = getattr(manager, "wired", None)
    if not isinstance(wired, Mapping):
        return None, "", "the manager does not report what it is wired to"
    if definition.probe == "executor":
        if not wired.get("executor"):
            return (
                False,
                "",
                "no executor is wired, so a candidate plan cannot become work here",
            )
        return True, "workflow_executor", "execution runs through the existing Phase 4 executor"
    if definition.probe == "permission":
        if not wired.get("permissions"):
            return (
                False,
                "",
                "no permission layer is wired, so every consequential step still asks first",
            )
        return True, "permission_manager", "the shared permission/risk layer decides"
    if definition.probe == "world":
        if not wired.get("world_model"):
            return (
                False,
                "",
                "no world model is wired, so every grounded fact is an assertion or an unknown",
            )
        return True, "world_model", "Phase 22 state is read read-only for grounding"
    if definition.probe in {"prediction", "rules"}:
        providers = wired.get("estimator")
        if not isinstance(providers, Mapping):
            return None, "", "the estimator does not report its providers"
        key = "learned_rules" if definition.probe == "rules" else "world_model"
        if not providers.get(key):
            return (
                False,
                "none",
                "no provider is wired for this estimate, so the task's own declared "
                "effect is reported as a declaration rather than a prediction",
            )
        return True, key, "a provider is wired and is consulted before any declaration"
    return None, "", f"the probe {definition.probe!r} is not one this build knows"


def capability_rows(manager: Any | None = None) -> tuple[CapabilityStatus, ...]:
    """The table with LIVE availability, read from the manager itself."""
    rows: list[CapabilityStatus] = []
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(manager, definition)
        rows.append(
            CapabilityStatus(
                name=definition.capability_id,
                state=definition.state,
                available=available,
                provider=provider,
                reason=reason,
            )
        )
    return tuple(rows)


def register_planning_capabilities(registry: Any, manager: Any | None = None) -> int:
    """Declare the goal-planning capabilities on the ONE capability registry.

    Same door the other phases use, same live probe: the registry and the manager
    cannot disagree about this machine.
    """
    registered = 0
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(manager, definition)
        if available is None:
            availability = CapabilityAvailability.UNKNOWN
        else:
            availability = (
                CapabilityAvailability.AVAILABLE
                if available
                else CapabilityAvailability.UNAVAILABLE
            )
        if not reason:
            reason = (
                "implemented in this build; no provider needed"
                if definition.probe == "deterministic"
                else f"provider: {provider}"
            )
        registry.register_action(
            definition.capability_id,
            description=definition.description,
            capability_id=definition.capability_id,
            executor="goal_planning",
            verifier="plan_verifier",
            category="planning",
            outputs=definition.outputs,
            permissions=definition.permissions,
            required_models=("prediction",) if definition.requires_model else (),
            tags=("planning", "goal", "plan"),
            examples=_examples(definition.capability_id),
            availability=availability,
            availability_reason=reason,
        )
        registered += 1
    return registered


def _examples(capability_id: str) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        "planning.goal_representation": (
            "what exactly am I trying to do here",
            "what does 'done' mean for this",
        ),
        "planning.decomposition": ("break this into steps",),
        "planning.dependency_ordering": ("what has to happen first",),
        "planning.plan_validation": ("is this plan even possible",),
        "planning.state_grounding": ("what is true right now",),
        "planning.candidate_generation": ("give me a couple of ways to do this",),
        "planning.plan_ranking": ("which plan is best",),
        "planning.outcome_estimation": ("what do you expect to happen",),
        "planning.action_policy": ("is this allowed to run",),
        "planning.bounded_execution": ("go ahead and run the plan",),
        "planning.monitoring_revalidation": ("is this step still valid",),
        "planning.completion_verification": ("did that actually work",),
        "planning.replanning": ("that failed, now what",),
        "planning.persistence": ("remember this plan across restarts",),
        "planning.world_state": ("look at the current state",),
        "planning.prediction": ("what will happen if you do that",),
        "planning.learned_knowledge": ("have you learned anything about this before",),
        "planning.parallel_execution": ("what can run at the same time",),
        "planning.generalization": ("solve an ARC puzzle",),
    }.get(capability_id, ())
