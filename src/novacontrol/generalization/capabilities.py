"""What Phase 26 can do, classified — and registered with the ONE registry.

The phase's own vocabulary is WORKING / PARTIAL / PROVIDER_DEPENDENT / MOCK /
UNAVAILABLE / DEFERRED. This build already HAS that vocabulary, spelled
``CapabilityState`` (Phase 21's), so the mapping is stated once here rather than
a second enum being invented:

    WORKING              → IMPLEMENTED
    PARTIAL              → PARTIALLY_IMPLEMENTED
    PROVIDER_DEPENDENT   → PROVIDER_DEPENDENT
    MOCK                 → MOCK
    UNAVAILABLE          → UNAVAILABLE
    DEFERRED             → FUTURE

The absences are stated as loudly as the presences, because the two things this
phase must not do are exactly the two a capability table would hide: there is NO
single aggregate intelligence score (the families are measured in different
units), and a GENERAL ARC solver is not shipped — a named library of
transformations is. A capability may be WORKING only when an end-to-end
evaluation has actually completed with it, which is why the probe reads the
manager's own run records rather than a constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novacontrol.intelligence.intent import CapabilityAvailability
from novacontrol.perception.models import CapabilityState, CapabilityStatus

__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "GeneralizationCapabilityDefinition",
    "capability_rows",
    "capability_table_rows",
    "register_generalization_capabilities",
]

CAPABILITY_SCHEMA_VERSION = "phase26.1"


@dataclass(frozen=True, slots=True)
class GeneralizationCapabilityDefinition:
    """One capability as this build ships it, plus how to probe it."""

    capability_id: str
    state: CapabilityState
    description: str
    #: ``manager`` reads the live evaluation manager; ``deferred`` is a
    #: deliberate absence; ``provider`` names a model that must be wired.
    probe: str = "manager"
    requires_model: bool = False
    permissions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "state": self.state.value,
            "description": self.description,
            "probe": self.probe,
            "requires_model": self.requires_model,
            "notes": self.notes,
            "permissions": list(self.permissions),
            "outputs": list(self.outputs),
        }


CAPABILITY_TABLE: tuple[GeneralizationCapabilityDefinition, ...] = (
    GeneralizationCapabilityDefinition(
        capability_id="generalization.task_corpus",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "A shipped corpus of tasks across six families, each declaring its split, its "
            "difficulty, its success condition and its verifier."
        ),
        outputs=("tasks", "splits", "families"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.split_control",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Training/development and held-out splits kept apart, with a three-way leakage "
            "check (overlapping ids, shared answers, examples already used while tuning) run "
            "before every evaluation — and a dirty split refuses the run."
        ),
        outputs=("leakage_report", "clean"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.grid_parsing",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Grid input parsing and validation: rectangles only, palette 0–9 only, bounded "
            "size, with every refusal naming the defect."
        ),
        outputs=("grid", "refusal_reason"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.grid_abstraction",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Connected-component analysis over grids: objects with size, colour, bounding "
            "box and normalized shape, plus symmetry, colour counts and translation."
        ),
        outputs=("objects", "symmetries", "color_counts"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.operator_library",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "An enumerated, bounded library of grid transformation operators — rotation, "
            "reflection, translation, recolouring, selection, extraction, repetition, "
            "gravity, movement, counting and completion — with no task id and no answer "
            "anywhere in it."
        ),
        outputs=("operator_catalogue",),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.rule_induction",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Infer a rule from examples: every operator proposes parameters, and a candidate "
            "is believed only after reproducing EVERY example exactly."
        ),
        outputs=("rule", "candidates_examined", "candidates_fitting"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.compositional_search",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Composition of two supported transformations, fitted to the residual of a first "
            "step rather than to the cross product of the library, with hard candidate and "
            "depth ceilings."
        ),
        outputs=("rule", "search_depth"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.ambiguity_reporting",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "When more than one rule fits the examples and they disagree on the unseen "
            "input, the answer is AMBIGUOUS with the competing rules named — never a guess "
            "dressed as an inference."
        ),
        outputs=("status", "competing_rules"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.unsupported_reporting",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "A task outside the supported library is reported UNSUPPORTED with the reason, "
            "rather than answered with something plausible."
        ),
        outputs=("status", "reason"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.baselines",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Named baselines measured on the SAME tasks with the same budget: identity, a "
            "seeded random operator, and a decline-to-answer floor."
        ),
        outputs=("baseline", "candidate", "delta"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.held_out_evaluation",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Evaluation on held-out variants: the same rule families with unseen colours, "
            "dimensions and arrangements, plus ambiguity and unsupported probes."
        ),
        outputs=("held_out_solve_rate", "per_family"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.failure_classification",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Eleven failure categories, separating no-rule-found from "
            "fitted-but-missed, ambiguity from failure, a defective transformation from an "
            "infrastructure error, and a broken evaluation setup from a model failure."
        ),
        outputs=("failures", "failure_groups", "recommendations"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.reproducibility",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Seeded, deterministic evaluation: the frozen settings, the seed, the task ids "
            "and a fingerprint over outcomes only (never timings), so two identical runs "
            "hash the same."
        ),
        outputs=("fingerprint", "seed", "settings"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.regression_comparison",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Two stored runs compared task by task, naming regressions and preserving the "
            "baseline record — an improved candidate that breaks a solved task is detected."
        ),
        outputs=("movements", "regressed", "baseline_preserved"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.transfer_measurement",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Controlled task variation: a verified rule re-applied to an unfamiliar "
            "configuration, with the source of every answer recorded (a supported "
            "deterministic rule, a previously verified reusable rule, a fresh hypothesis, a "
            "model proposal, or a mock)."
        ),
        outputs=("source", "rule_family"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.game_eval_contracts",
        state=CapabilityState.IMPLEMENTED,
        description=(
            "Reusable evaluation contracts for the Phase 25 environment: objective "
            "completion, action efficiency, observation freshness, recovery success, "
            "constraint violations, time to completion, initial-condition spread, "
            "reproducibility and resource use — definitions and an adapter, not a game."
        ),
        outputs=("contracts", "session_metrics", "spread"),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.arc_general_solver",
        state=CapabilityState.PARTIALLY_IMPLEMENTED,
        description=(
            "A subset of ARC-style transformations, not a general ARC solver: the supported "
            "families are enumerated and the rest is reported UNSUPPORTED."
        ),
        outputs=("supported_rules", "unsupported_tasks"),
        notes=(
            "the library covers rotation, reflection, translation, recolouring, selection, "
            "extraction, repetition, gravity, movement, counting, completion and one "
            "composition; arbitrary ARC tasks are NOT solved and are reported as such"
        ),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.single_intelligence_score",
        state=CapabilityState.UNAVAILABLE,
        description=(
            "One aggregate score for general intelligence. Deliberately not implemented: "
            "the families are measured in different units, so a combined number would hide "
            "family failures behind a mean."
        ),
        probe="deferred",
        notes=(
            "per-family metrics are always reported; an aggregate is available only from "
            "generalization.weighted_aggregate with an explicitly supplied normalization "
            "and weighting"
        ),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.model_proposed_rules",
        state=CapabilityState.PROVIDER_DEPENDENT,
        description=(
            "An optional model may propose candidate transformations; every proposal is "
            "verified against all examples by the same evaluator before it counts."
        ),
        probe="provider",
        requires_model=True,
        outputs=("proposal", "verified"),
        notes=(
            "no model is required: with none wired, the deterministic path answers and the "
            "provider posture is reported as unavailable"
        ),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.synthetic_results",
        state=CapabilityState.UNAVAILABLE,
        description=(
            "Synthetic or mock benchmark results. Not shipped: a result is either produced "
            "by the real solver on a real task, or there is no result."
        ),
        probe="deferred",
        notes="no mock provider is registered for this phase, so nothing can be labelled real",
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.training_from_findings",
        state=CapabilityState.FUTURE,
        description=(
            "Training a model from evaluation findings. Deliberately NOT done here: the "
            "benchmark exports failures and recommendations, and starting SFT/DPO/RLHF/RLVR "
            "remains a separate, explicitly authorized act."
        ),
        probe="deferred",
        notes=(
            "no weights are modified by running an evaluation, and no dataset is written as "
            "a side effect"
        ),
    ),
    GeneralizationCapabilityDefinition(
        capability_id="generalization.gameplaying",
        state=CapabilityState.FUTURE,
        description=(
            "Playing a game. The dedicated post-Phase-26 game-playing evaluation is the next "
            "milestone; this phase ships the contracts it will be measured with."
        ),
        probe="deferred",
        notes="no gameplay is claimed anywhere in this phase",
    ),
)


def _availability(
    definition: GeneralizationCapabilityDefinition, manager: Any | None
) -> tuple[bool | None, str, str]:
    """Whether the MACHINE has this capability right now, and on what."""
    if definition.probe == "deferred":
        return False, "none", definition.notes or "deferred to a later phase"
    if definition.probe == "provider":
        if manager is None:
            return None, "", "no evaluation manager is wired, so no provider could be asked"
        posture = str(getattr(manager, "_provider_posture", lambda: "not_required")())
        if posture == "available":
            return True, "model provider", "a model provider is configured and may propose rules"
        return (
            None,
            "",
            "no model provider is wired; proposals are optional, so the deterministic path "
            "answers and no verification is claimed for a model",
        )
    if manager is None:
        return False, "none", "no generalization evaluation manager is wired in this build"
    enabled = bool(getattr(manager.settings, "enabled", False))
    return (
        enabled,
        "generalization manager",
        "the corpus is registered and evaluation is enabled"
        if enabled
        else "generalization evaluation is disabled by configuration",
    )


def capability_rows(manager: Any | None = None) -> tuple[CapabilityStatus, ...]:
    """The table with LIVE availability, read from the manager itself."""
    rows: list[CapabilityStatus] = []
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(definition, manager)
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


def capability_table_rows(manager: Any | None = None) -> tuple[dict[str, Any], ...]:
    """The table as plain rows, for a status payload."""
    rows: list[dict[str, Any]] = []
    for definition, status in zip(CAPABILITY_TABLE, capability_rows(manager), strict=False):
        payload = definition.to_dict()
        payload["available"] = status.available
        payload["availability_reason"] = status.reason
        rows.append(payload)
    return tuple(rows)


def register_generalization_capabilities(registry: Any, manager: Any | None = None) -> int:
    """Declare the generalization capabilities on the ONE capability registry."""
    registered = 0
    for definition in CAPABILITY_TABLE:
        available, provider, reason = _availability(definition, manager)
        if available is None:
            availability = CapabilityAvailability.UNKNOWN
        else:
            availability = (
                CapabilityAvailability.AVAILABLE
                if available
                else CapabilityAvailability.UNAVAILABLE
            )
        registry.register_action(
            definition.capability_id,
            description=definition.description,
            capability_id=definition.capability_id,
            executor="generalization",
            verifier="generalization_evaluator",
            category="generalization",
            outputs=definition.outputs,
            permissions=definition.permissions,
            required_models=("llm",) if definition.requires_model else (),
            tags=("generalization", "evaluation", "benchmark"),
            examples=_examples(definition.capability_id),
            availability=availability,
            availability_reason=reason or f"provider: {provider}",
        )
        registered += 1
    return registered


def _examples(capability_id: str) -> tuple[str, ...]:
    """A phrase a person would use for each capability — for discovery."""
    return {
        "generalization.task_corpus": ("show me the benchmark tasks",),
        "generalization.split_control": ("is the held-out set clean",),
        "generalization.grid_parsing": ("read this grid",),
        "generalization.grid_abstraction": ("what shapes are in this grid",),
        "generalization.operator_library": ("which grid transformations do you have",),
        "generalization.rule_induction": ("work out the rule from these examples",),
        "generalization.compositional_search": ("combine two transformations",),
        "generalization.ambiguity_reporting": ("which rule is it really",),
        "generalization.unsupported_reporting": ("can you solve this puzzle",),
        "generalization.baselines": ("how does that compare to guessing",),
        "generalization.held_out_evaluation": ("how well do you generalise",),
        "generalization.failure_classification": ("why did that task fail",),
        "generalization.reproducibility": ("run the same benchmark again",),
        "generalization.regression_comparison": ("did the new solver regress",),
        "generalization.transfer_measurement": ("where did that answer come from",),
        "generalization.game_eval_contracts": ("how will gameplay be measured",),
        "generalization.arc_general_solver": ("solve any ARC puzzle",),
        "generalization.single_intelligence_score": ("what is your intelligence score",),
        "generalization.model_proposed_rules": ("let the model propose a rule",),
        "generalization.synthetic_results": ("show me a mock result",),
        "generalization.training_from_findings": ("train on these failures",),
        "generalization.gameplaying": ("play a game",),
    }.get(capability_id, ())
