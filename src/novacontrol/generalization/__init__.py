"""Generalization, ARC-style reasoning and intelligence evaluation — Phase 26.

One sentence for what this package is: **it measures whether a rule inferred
from examples still works on an example that was never shown, and refuses to
call anything else generalization.**

The cycle it runs is the one the phase names, and every arrow is a record:

    define task → establish baseline → present an unseen example → observe the
    input → identify structure → form a hypothesis → generate a candidate
    solution → verify it → measure performance → compare against the baseline
    → classify the failure → record the evidence → re-evaluate after improvement

It is built ON the layers before it rather than beside them:

  * Phase 8's ``DeterministicVerifier`` is the shape every verifier here follows.
    Nothing in this package executes a gated action — no command, no network, no
    write outside the state store — so there is no permission decision to ask for;
    each capability still DECLARES what it may touch (``permissions`` on its
    capability row) rather than leaving the question open
  * Phase 9's event bus and capability registry carry the announcements and the
    capability table — this package registers its own rows and no second one
  * Phase 12's pipeline vocabulary (a step states what success looks like) is why
    every task here declares a success condition and a verifier, and a task
    without one is refused at registration
  * Phase 15's evidence discipline is reused for the SHAPE of this phase's records
    (what a run was made with, and a reproduce block on every failure). Nothing
    here is recorded as an agent trajectory automatically — the trajectory
    recorder observes the core task lifecycle — so this package reuses the
    vocabulary and builds no second recorder rather than claiming to feed one
  * Phase 22's world-state and Phase 24's planning contracts are what the
    state-transition and planning families are expressed in
  * Phase 25's environment adapter is what the game-evaluation CONTRACTS in
    :mod:`novacontrol.generalization.game_eval` read — definitions and an
    adapter, not a game
  * Phase 14's rule holds throughout: a figure that was not measured is
    ``None``, never ``0.0``

The honest positions are stated at the same volume as the features:

  * the grid capability is a NAMED, ENUMERATED library of transformations, not a
    general ARC solver; tasks outside it are reported UNSUPPORTED
  * the held-out answers are hand-written, and the search never sees them
  * a rule is believed only after reproducing EVERY example exactly
  * two rules that both fit and disagree produce AMBIGUOUS, never a guess
  * there is no single intelligence score — the families are measured in
    different units, so a combined number would hide family failures
  * nothing here trains a model, changes a weight, downloads anything, or
    promotes a candidate into production
  * no mock or synthetic result is ever labelled real
  * gameplay is NOT claimed: this phase ships the contracts the dedicated
    post-Phase-26 game-playing evaluation will be measured with

``overview()`` is the machine-readable form of those statements; the live
per-machine state is
:meth:`novacontrol.generalization.manager.GeneralizationManager.status`.
"""

from novacontrol.generalization.capabilities import (
    CAPABILITY_SCHEMA_VERSION,
    CAPABILITY_TABLE,
    GeneralizationCapabilityDefinition,
    capability_rows,
    capability_table_rows,
    register_generalization_capabilities,
)
from novacontrol.generalization.evaluation import evaluate_task, verify
from novacontrol.generalization.failures import FAILURE_ADVICE, classify_failure, group_failures
from novacontrol.generalization.game_eval import (
    GAME_EVAL_CONTRACTS,
    GameEvalContract,
    contracts_payload,
    evaluate_embodied_sessions,
    summarise_embodied_session,
)
from novacontrol.generalization.grids import (
    MAX_GRID_CELLS,
    MAX_GRID_DIM,
    PALETTE,
    BBox,
    Grid,
    GridObject,
    InvalidGrid,
    grid_equals,
    parse_grid,
)
from novacontrol.generalization.manager import (
    FAMILIES,
    HARD_MAX_CANDIDATES,
    HARD_MAX_RESULTS_KEPT,
    HARD_MAX_RUNS,
    HARD_MAX_SECONDS,
    HARD_MAX_TASKS_PER_RUN,
    STORE_KEY,
    FamilyDefinition,
    GeneralizationManager,
    GeneralizationSettingsView,
    builtin_task_ids,
)
from novacontrol.generalization.metrics import (
    ComparisonReport,
    compare_results,
    family_metrics,
    metrics_by_family,
    weighted_aggregate,
)
from novacontrol.generalization.models import (
    FAILURE_CATEGORIES,
    PHASE,
    RUN_SCHEMA_VERSION,
    TASK_SCHEMA_VERSION,
    VERIFIERS,
    Difficulty,
    EvaluationRun,
    EvaluationTask,
    FailureCategory,
    FailureRecord,
    FamilyMetrics,
    ImprovementRecommendation,
    LeakageReport,
    ProviderPosture,
    RuleSource,
    RunStatus,
    Split,
    TaskExample,
    TaskKind,
    TaskResult,
    TaskStatus,
    VerificationRecord,
    validate_task,
)
from novacontrol.generalization.rules import (
    MAX_DEPTH,
    CandidateRule,
    RuleStep,
    fit_rules,
    rule_from_mapping,
    rule_to_mapping,
)
from novacontrol.generalization.solvers import BASELINES, SOLVERS, Budget, SolveOutcome, solve
from novacontrol.generalization.tasks import (
    BUILTIN_TASKS,
    CORPUS_VERSION,
    GRID_RULES_SUPPORTED,
    task_by_id,
    tasks_for,
    tasks_payload,
)
from novacontrol.generalization.transforms import (
    OPERATOR_NAMES,
    OPERATORS,
    GridPair,
    Operator,
    operator,
    operator_names,
)

#: What this phase deliberately does NOT implement, named so a reader never has
#: to infer the boundary from absence. Published by ``overview()["deferred"]``.
DEFERRED_PHASES: tuple[str, ...] = (
    "the dedicated post-Phase-26 game-playing evaluation",
    "a general ARC solver (this build ships a named transformation library)",
    "a single aggregate intelligence score (the families are not comparable)",
    "training a model, or promoting a candidate, from evaluation findings",
    "Phases 27–39",
)

#: The vocabularies, so a report, a test or a dashboard reads the same words the
#: implementation uses instead of restating them.
LIFECYCLE: dict[str, list[str]] = {
    "split": [member.value for member in Split],
    "difficulty": [member.value for member in Difficulty],
    "task_kind": [member.value for member in TaskKind],
    "task_status": [member.value for member in TaskStatus],
    "run_status": [member.value for member in RunStatus],
    "rule_source": [member.value for member in RuleSource],
    "failure_category": [member.value for member in FailureCategory],
    "provider_posture": [member.value for member in ProviderPosture],
}

#: The event vocabulary this package publishes on the application's ONE bus.
EVENT_TYPES: tuple[str, ...] = (
    "generalization.task_registered",
    "generalization.evaluation_started",
    "generalization.evaluation_completed",
    "generalization.failure_classified",
    "generalization.leakage_detected",
)


def overview() -> dict[str, object]:
    """What this phase ships and what it refuses to claim — cheap and read-only."""
    return {
        "phase": PHASE,
        "task_schema_version": TASK_SCHEMA_VERSION,
        "run_schema_version": RUN_SCHEMA_VERSION,
        "corpus_version": CORPUS_VERSION,
        "cycle": [
            "define_task",
            "establish_baseline",
            "present_unseen_example",
            "observe_input",
            "identify_structure",
            "form_hypothesis",
            "generate_candidate_solution",
            "verify_solution",
            "measure_performance",
            "compare_against_baseline",
            "classify_failure",
            "record_evidence",
            "re_evaluate_after_improvement",
        ],
        "lifecycle": {key: list(value) for key, value in LIFECYCLE.items()},
        "families": [item.to_dict() for item in FAMILIES],
        "operators": list(OPERATOR_NAMES),
        "grid_rules_supported": list(GRID_RULES_SUPPORTED),
        "solvers": list(SOLVERS),
        "baselines": list(BASELINES),
        "verifiers": list(VERIFIERS),
        "failure_categories": list(FAILURE_CATEGORIES),
        "event_types": list(EVENT_TYPES),
        "capabilities": [row.capability_id for row in CAPABILITY_TABLE],
        "budgets": {
            "max_tasks_per_run": HARD_MAX_TASKS_PER_RUN,
            "max_candidates": HARD_MAX_CANDIDATES,
            "max_seconds": HARD_MAX_SECONDS,
            "max_results_kept": HARD_MAX_RESULTS_KEPT,
            "keep_runs": HARD_MAX_RUNS,
            "note": "a configuration may ask for less; it can never ask for more",
        },
        "integration": {
            "phase8": "every verifier follows the planner's deterministic verifier shape",
            "phase9": "the ONE event bus and the ONE capability registry",
            "phase12": "a task without a success condition is refused at registration",
            "phase14": "an unmeasured figure is None, never 0.0",
            "phase15": "runs and failures are evidence for the existing trajectory layer",
            "phase22": "state transitions are expressed in the world-state vocabulary",
            "phase24": "planning tasks are bounded searches with the same ceilings",
            "phase25": "the game-eval contracts read the embodied session record",
            "persistence": "the application's JsonStateStore, one key for the runs",
        },
        # The honesty flags: each one is a thing this build does NOT do.
        "general_arc_solver": False,
        "hardcodes_benchmark_answers": False,
        "sees_held_out_answers_during_search": False,
        "leaks_held_out_answers_into_tuning": False,
        "single_intelligence_score": False,
        "fabricates_benchmark_scores": False,
        "labels_mock_results_as_real": False,
        "trains_anything": False,
        "downloads_models": False,
        "modifies_production_weights": False,
        "auto_starts_training": False,
        "claims_gameplay": False,
        "bypasses_permissions": False,
        "deferred": list(DEFERRED_PHASES),
    }


__all__ = [
    "BASELINES",
    "BUILTIN_TASKS",
    "CAPABILITY_SCHEMA_VERSION",
    "CAPABILITY_TABLE",
    "CORPUS_VERSION",
    "DEFERRED_PHASES",
    "EVENT_TYPES",
    "FAILURE_ADVICE",
    "FAILURE_CATEGORIES",
    "FAMILIES",
    "GAME_EVAL_CONTRACTS",
    "GRID_RULES_SUPPORTED",
    "HARD_MAX_CANDIDATES",
    "HARD_MAX_RESULTS_KEPT",
    "HARD_MAX_RUNS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_TASKS_PER_RUN",
    "LIFECYCLE",
    "MAX_DEPTH",
    "MAX_GRID_CELLS",
    "MAX_GRID_DIM",
    "OPERATORS",
    "OPERATOR_NAMES",
    "PALETTE",
    "PHASE",
    "RUN_SCHEMA_VERSION",
    "SOLVERS",
    "STORE_KEY",
    "TASK_SCHEMA_VERSION",
    "VERIFIERS",
    "BBox",
    "Budget",
    "CandidateRule",
    "ComparisonReport",
    "Difficulty",
    "EvaluationRun",
    "EvaluationTask",
    "FailureCategory",
    "FailureRecord",
    "FamilyDefinition",
    "FamilyMetrics",
    "GameEvalContract",
    "GeneralizationCapabilityDefinition",
    "GeneralizationManager",
    "GeneralizationSettingsView",
    "Grid",
    "GridObject",
    "GridPair",
    "ImprovementRecommendation",
    "InvalidGrid",
    "LeakageReport",
    "Operator",
    "ProviderPosture",
    "RuleSource",
    "RuleStep",
    "RunStatus",
    "SolveOutcome",
    "Split",
    "TaskExample",
    "TaskKind",
    "TaskResult",
    "TaskStatus",
    "VerificationRecord",
    "builtin_task_ids",
    "capability_rows",
    "capability_table_rows",
    "classify_failure",
    "compare_results",
    "contracts_payload",
    "evaluate_embodied_sessions",
    "evaluate_task",
    "family_metrics",
    "fit_rules",
    "grid_equals",
    "group_failures",
    "metrics_by_family",
    "operator",
    "operator_names",
    "overview",
    "parse_grid",
    "register_generalization_capabilities",
    "rule_from_mapping",
    "rule_to_mapping",
    "solve",
    "summarise_embodied_session",
    "task_by_id",
    "tasks_for",
    "tasks_payload",
    "validate_task",
    "verify",
    "weighted_aggregate",
]
