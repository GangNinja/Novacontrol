"""What Phase 23 can do, classified — and registered with the ONE registry.

The classification says what each capability IS in this build rather than what the
phase is called, and it reuses Phase 21's capability vocabulary verbatim (two
vocabularies for the same question is how a capability table starts to lie):

    objective_create        IMPLEMENTED       a bounded question, with its ceilings
    environment_register    IMPLEMENTED       deterministic simulators, from a spec
    experiment_selection    IMPLEMENTED       three strategies, documented ranking
    hypothesis_tracking     IMPLEMENTED       scoped, versioned, evidence-counted
    knowledge_rules         IMPLEMENTED       scoped rules with their evidence
    knowledge_updates       IMPLEMENTED       versioned and reversible
    learning_evaluation     IMPLEMENTED       including the inconclusive results
    redundant_avoidance     IMPLEMENTED       duplicates refused before they run
    budget_enforcement      IMPLEMENTED       ceilings checked BEFORE the action
    permission_gate         IMPLEMENTED       the shared permission/risk layer
    persistence_restore     IMPLEMENTED       one key per objective
    prediction              PROVIDER_DEPENDENT a rule is read when one is declared
                                             or learned; nothing is fabricated
    world_observation       PARTIALLY_IMPL    read-only, and only when a reader is
                                             wired
    real_environment_actions PROVIDER_DEPENDENT needs an injected executor AND
                                             approval AND a permission layer
    counterfactual_replay   DEFERRED          Phase 24 territory
    model_training          DEFERRED          Phase 23 never launches training

Availability is a property of the MACHINE and is read live from the manager on
every call, never from a boot snapshot: whether an environment can act is answered
by the environment itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.interactive.models import EnvironmentKind, as_text
from novacontrol.perception.models import CapabilityState, CapabilityStatus

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "LearningCapabilityDefinition",
    "capability_rows",
    "register_learning_capabilities",
]

#: The schema version of this table, so a stored report can say what it read.
CAPABILITY_SCHEMA_VERSION = "phase23.1"


@dataclass(frozen=True, slots=True)
class LearningCapabilityDefinition:
    """One capability as this build ships it, plus how to probe it."""

    capability_id: str
    state: CapabilityState
    description: str
    #: ``deterministic`` needs nothing wired; ``world``, ``real``, ``prediction``
    #: and ``permission`` name collaborators that may or may not be present.
    probe: str = "deterministic"
    requires_model: bool = False
    requires_permission: bool = False
    notes: str = ""
    permissions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LearningCapabilityDefinition:
        return cls(
            capability_id=as_text(data.get("capability_id"), "interactive.unknown"),
            state=CapabilityState(as_text(data.get("state"), CapabilityState.FUTURE.value)),
            description=as_text(data.get("description")),
            probe=as_text(data.get("probe"), "deterministic"),
            requires_model=bool(data.get("requires_model", False)),
            requires_permission=bool(data.get("requires_permission", False)),
            notes=as_text(data.get("notes")),
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


CAPABILITY_TABLE: tuple[LearningCapabilityDefinition, ...] = (
    LearningCapabilityDefinition(
        capability_id="interactive.objective_create",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Turn a bounded question into a learning objective with its own ceilings, "
            "scope and budget."
        ),
        outputs=("objective_id", "budget", "scope"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.environment_register",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Register a deterministic simulator, declared as data: rules, candidate "
            "outcomes, failures, delays, misleading and incomplete observations."
        ),
        outputs=("environment_id", "actions", "candidates"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.experiment_selection",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Rank candidate experiments against the open hypotheses and pick one, with "
            "the reason every other candidate was refused."
        ),
        outputs=("experiment_id", "strategy", "information_value", "rejected"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.hypothesis_tracking",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Keep hypotheses scoped and versioned with their supporting, contradicting "
            "and inconclusive evidence separately counted."
        ),
        outputs=("hypotheses", "evidence", "versions"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.knowledge_rules",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Promote a hypothesis to a SCOPED rule only when its independent evidence "
            "meets the declared threshold, keeping the rule's support and its "
            "contradictions."
        ),
        outputs=("rules", "knowledge_updates"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.learning_evaluation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Measure what happened — hypotheses moved, agreement over evaluable "
            "predictions, duplicates avoided, rejections — and report the inconclusive "
            "results beside the rest."
        ),
        outputs=("metrics", "limitations"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.budget_enforcement",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Check every ceiling and permission requirement BEFORE an action, and "
            "record the refusal with its reason."
        ),
        permissions=("interactive.execute",),
        outputs=("verdict", "safety_decision", "usage"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.persistence",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Persist objectives, hypotheses, experiments, runs, evidence references, "
            "budget usage and rules through the shared JSON state store; restore "
            "reports what it skipped and never completes an interrupted experiment."
        ),
        outputs=("stored", "restore_report", "interrupted"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.prediction",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Predict an experiment's outcome from a declared rule, a learned rule, or a "
            "world model when one is wired; otherwise the run is recorded as "
            "prediction-unavailable and is not scored for agreement."
        ),
        probe="prediction",
        requires_model=True,
        notes="No predictive model ships with this build; a rule is read, never guessed.",
        outputs=("prediction", "basis", "comparison"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.world_observation",
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description=(
            "Observe Phase 22's world state through a READ-ONLY environment: this layer "
            "never writes world state and never acts on it."
        ),
        probe="world",
        permissions=("filesystem:read",),
        notes="Read-only by construction; the world environment exposes no action at all.",
        outputs=("observation",),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.real_environment_actions",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "Act in a real environment, when a caller has injected an executor and an "
            "operator has approved the specific experiment and the permission layer "
            "allows it."
        ),
        probe="real",
        requires_permission=True,
        notes="Unavailable in this build: no executor is injected by default.",
        permissions=("interactive.execute",),
        outputs=("result", "observation"),
    ),
    LearningCapabilityDefinition(
        capability_id="interactive.model_training",
        state=CapabilityState.FUTURE,
        description=(
            "Launch SFT/DPO/RLHF/RLVR or agentic-RL from an experiment. NOT implemented "
            "by this phase: a learned rule is a versioned record, and proposing a "
            "dataset is not a weight change."
        ),
        probe="deferred",
        requires_model=True,
        notes="Deferred deliberately (§8): interactive learning must not train anything.",
        outputs=(),
    ),
)


def _availability(
    manager: Any | None, definition: LearningCapabilityDefinition
) -> tuple[bool | None, str, str]:
    """Live availability, read from the manager — ``None`` means "cannot tell"."""
    if definition.probe == "deferred":
        return False, "deferred", definition.notes or "this phase does not implement it"
    if definition.probe == "deterministic":
        return True, "", "implemented in this build; no provider needed"
    if manager is None:
        return None, "", "no interactive learning manager is wired in this build"
    if definition.probe == "prediction":
        # Available means "a prediction can be MADE right now", not "a provider object
        # exists": an environment that declares a rule, or a rule this build has
        # learned, is what makes an expectation possible. Anything else is the honest
        # "unavailable", and the run is then not scored for agreement.
        # Read from the manager's OWN records rather than from ``status()``: the
        # status surface includes this table, so asking it here would recurse.
        sources: list[str] = []
        environments = getattr(manager, "environments", None)
        for item in environments.all() if environments is not None else ():
            reader = getattr(item, "declared_rule", None)
            if not callable(reader):
                continue
            declared = [action.name for action in item.actions() if reader(action.name)]
            if declared:
                sources.append(f"{item.environment_id} declares {', '.join(declared)}")
        book = getattr(manager, "book", None)
        rules = getattr(book, "rules", ()) if book is not None else ()
        if rules:
            sources.append(f"{len(rules)} learned rule(s)")
        if getattr(manager, "world_bridge", None) is not None:
            sources.append("a world-model bridge")
        if not sources:
            return (
                False,
                "none",
                "no environment declares a rule for its actions and no rule has been "
                "learned yet, so no prediction can be made",
            )
        detail = "; ".join(sources)
        return True, detail[:160], f"a prediction is available: {detail[:200]}"
    if definition.probe == "world":
        readers = [
            item
            for item in manager.environments.all()
            if item.kind is EnvironmentKind.WORLD and bool(item.status().get("reader_wired"))
        ]
        if not readers:
            return (
                False,
                "",
                "no read-only world environment is wired, so the world cannot be observed here",
            )
        return True, readers[0].environment_id, "read-only observation of Phase 22 state"
    if definition.probe == "real":
        executors = [
            item
            for item in manager.environments.all()
            if item.kind is not EnvironmentKind.SIMULATOR
            and bool(item.status().get("executor_wired"))
        ]
        if not executors:
            return (
                False,
                "",
                "no environment in this build has an executor wired, so nothing real can act",
            )
        return True, executors[0].environment_id, "a real environment is wired and gated"
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


def register_learning_capabilities(registry: Any, manager: Any | None = None) -> int:
    """Declare the interactive-learning capabilities on the ONE capability registry.

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
            executor="interactive_learning",
            verifier="learning_verifier",
            category="learning",
            outputs=definition.outputs,
            permissions=definition.permissions,
            required_models=("prediction",) if definition.requires_model else (),
            tags=("learning", "experiment", "hypothesis"),
            examples=_examples(definition.capability_id),
            availability=availability,
            availability_reason=reason,
        )
        registered += 1
    return registered


def _examples(capability_id: str) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        "interactive.objective_create": (
            "find out what the dial does",
            "learn what happens when the switch is pressed",
        ),
        "interactive.environment_register": ("add a test environment",),
        "interactive.experiment_selection": ("which experiment should we try",),
        "interactive.hypothesis_tracking": ("what do we think is going on",),
        "interactive.knowledge_rules": ("what have you learned here",),
        "interactive.learning_evaluation": ("did that experiment teach you anything",),
        "interactive.budget_enforcement": ("how many experiments may this use",),
        "interactive.persistence": ("remember this learning across restarts",),
        "interactive.prediction": ("what do you expect to happen",),
        "interactive.world_observation": ("look at what is in the room",),
        "interactive.real_environment_actions": ("try that on the real device",),
        "interactive.model_training": ("train on the experiments",),
    }.get(capability_id, ())
