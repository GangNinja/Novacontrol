"""NovaForge — workspace, telemetry, Guardian, evidence, gameplay action path.

Tagline: *Observe. Decide. Act. Verify. Evolve.*

NF-01 is the gaming workspace and session foundation: a backend-owned session
manager that observes one validated window, a mission configuration surface, a
real lifecycle (start/pause/resume/stop), and persisted history. NF-02 adds
bounded telemetry measured at the point each fact occurs, plus Guardian:
enforced limits, cancel/emergency stop, bounded finalize and input cleanup.

The gameplay path adds the acting and verifying half, built from the desktop
package's existing safety primitives rather than a second input stack:
``actions`` (a plan is a digest-bound proposal, an execution revalidates the
pinned window and injects through ``InputGuard``) and ``objective`` (an
independent verifier reads the window; only a verified objective completes a
session). A refusal is not a failure, an execution is not a success, and the
API says which is which rather than faking a verdict.

The package boundary is:

* :mod:`novacontrol.novaforge.models` — missions, sessions, frames, stages, and
  the state machine that raises on an illegal move.
* :mod:`novacontrol.novaforge.manager` — the session manager, wired to the SAME
  desktop capture/geometry seams and state store the rest of the application
  uses.
* :mod:`novacontrol.novaforge.telemetry` — bounded, monotonic measurement.
* :mod:`novacontrol.novaforge.guardian` — the explicit safety policy and status.
* :mod:`novacontrol.novaforge.timeline` — the ordered, bounded evidence timeline,
  evidence references, session comparison and evidence-tied failure
  classification. Replay is READ-ONLY: it never starts, resumes or sends input.

It is a subsystem of NovaControl, not a second brain: planning, perception and
execution stay in their existing packages.
"""

from .actions import (
    ACTION_KINDS,
    ActionInjector,
    ActionKind,
    ActionOutcome,
    ActionPolicy,
    ActionProposal,
    ActionRunner,
    ActionStatus,
    GameplayAction,
    ScreenInput,
    action_digest,
)
from .benchmark import (
    BENCHMARK_VERSION,
    BenchmarkConflictError,
    BenchmarkError,
    BenchmarkNotFoundError,
    BenchmarkScenario,
    BenchmarkValidationError,
    SCENARIO_SCHEMA_VERSION,
    aggregate_runs,
    collect_metrics,
    compare_runs,
    config_from_request,
    metric_catalog,
    resolve_scenario,
    scenario_payload,
)
from .benchmark_lab import NovaForgeBenchmarkLab, RUNS_NAMESPACE
from .coach import (
    COACH_VERSION,
    RECOMMENDATIONS_NAMESPACE,
    CoachError,
    CoachNotFoundError,
    Finding,
    NovaForgeCoach,
    RECOMMENDATION_RULES,
    RECOMMENDATION_STATUSES,
    Recommendation,
)
from .experiments import (
    EXPERIMENT_KINDS,
    EXPERIMENT_STATUSES,
    EXPERIMENT_VERSION,
    EXPERIMENTS_NAMESPACE,
    ExperimentConflictError,
    ExperimentError,
    ExperimentNotFoundError,
    ExperimentSpec,
    NovaForgeExperimentLab,
    VERDICTS,
)
from .evaluation import (
    EVALUATIONS_NAMESPACE,
    EVALUATION_STATUSES,
    MAX_EVALUATIONS_KEPT,
    NovaForgeEvaluationLab,
)
from .games import (
    CHECK_STATUSES,
    EVALUATION_MODES,
    EVALUATION_VERSION,
    GAME_SCHEMA_VERSION,
    GAME_STATUSES,
    EvaluationConflictError,
    EvaluationError,
    GameNotFoundError,
    GameRecord,
    evaluation_limitations,
    game_catalog,
    missing_evidence,
    readiness_checks,
    readiness_summary,
    resolve_game,
    supported_systems,
    usable_scenarios,
)
from .guardian import (
    GUARDIAN_VERSION,
    action_budget_enforced,
    guardian_state,
    input_path_available,
    input_safety_decision,
    safety_state,
)
from .objective import (
    OBJECTIVE_KINDS,
    ObjectiveDefinition,
    ObjectiveEvidence,
    ObjectiveKind,
    ObjectiveOutcome,
    ObjectiveRegistry,
    ObjectiveVerification,
    WindowTitleVerifier,
)
from .manager import NovaForgeManager, NovaForgeStatus
from .models import (
    FrameRecord,
    MissionConfig,
    MissionValidationError,
    ObservationRecord,
    SESSION_TERMINAL_STATES,
    SESSION_TRANSITIONS,
    SessionConflictError,
    SessionRecord,
    SessionState,
    SessionTransitionError,
    StageRecord,
    TargetWindow,
    utc_now,
)
from .telemetry import TelemetrySnapshot, gap_threshold_for
from .timeline import (
    MAX_EVENTS_PER_SESSION,
    TIMELINE_VERSION,
    TimelineEvent,
    TimelineLog,
    TimelineQueryError,
    classify,
    compare,
    derived_events,
    timeline_namespace,
)

__all__ = [
    "ACTION_KINDS",
    "ActionInjector",
    "ActionKind",
    "ActionOutcome",
    "ActionPolicy",
    "ActionProposal",
    "ActionRunner",
    "ActionStatus",
    "GameplayAction",
    "ScreenInput",
    "action_digest",
    "OBJECTIVE_KINDS",
    "ObjectiveDefinition",
    "ObjectiveEvidence",
    "ObjectiveKind",
    "ObjectiveOutcome",
    "ObjectiveRegistry",
    "ObjectiveVerification",
    "WindowTitleVerifier",
    "action_budget_enforced",
    "input_path_available",
    "BENCHMARK_VERSION",
    "BenchmarkConflictError",
    "BenchmarkError",
    "BenchmarkNotFoundError",
    "BenchmarkScenario",
    "BenchmarkValidationError",
    "CHECK_STATUSES",
    "EVALUATIONS_NAMESPACE",
    "EVALUATION_MODES",
    "EVALUATION_STATUSES",
    "EVALUATION_VERSION",
    "EvaluationConflictError",
    "EvaluationError",
    "GAME_SCHEMA_VERSION",
    "GAME_STATUSES",
    "GameNotFoundError",
    "GameRecord",
    "MAX_EVALUATIONS_KEPT",
    "COACH_VERSION",
    "CoachError",
    "CoachNotFoundError",
    "EXPERIMENT_KINDS",
    "EXPERIMENT_STATUSES",
    "EXPERIMENT_VERSION",
    "EXPERIMENTS_NAMESPACE",
    "ExperimentConflictError",
    "ExperimentError",
    "ExperimentNotFoundError",
    "ExperimentSpec",
    "Finding",
    "NovaForgeCoach",
    "NovaForgeBenchmarkLab",
    "NovaForgeEvaluationLab",
    "NovaForgeExperimentLab",
    "RECOMMENDATIONS_NAMESPACE",
    "RECOMMENDATION_RULES",
    "RECOMMENDATION_STATUSES",
    "Recommendation",
    "VERDICTS",
    "RUNS_NAMESPACE",
    "evaluation_limitations",
    "game_catalog",
    "missing_evidence",
    "readiness_checks",
    "supported_systems",
    "usable_scenarios",
    "readiness_summary",
    "resolve_game",
    "SCENARIO_SCHEMA_VERSION",
    "aggregate_runs",
    "collect_metrics",
    "compare_runs",
    "config_from_request",
    "metric_catalog",
    "resolve_scenario",
    "scenario_payload",
    "GUARDIAN_VERSION",
    "MAX_EVENTS_PER_SESSION",
    "TIMELINE_VERSION",
    "TimelineEvent",
    "TimelineLog",
    "TimelineQueryError",
    "classify",
    "compare",
    "derived_events",
    "timeline_namespace",
    "TelemetrySnapshot",
    "gap_threshold_for",
    "guardian_state",
    "input_safety_decision",
    "safety_state",
    "FrameRecord",
    "MissionConfig",
    "MissionValidationError",
    "NovaForgeManager",
    "NovaForgeStatus",
    "ObservationRecord",
    "SESSION_TERMINAL_STATES",
    "SESSION_TRANSITIONS",
    "SessionConflictError",
    "SessionRecord",
    "SessionState",
    "SessionTransitionError",
    "StageRecord",
    "TargetWindow",
    "utc_now",
]
