"""NovaForge NF-04 — benchmark scenarios, metrics and fair comparison.

This module is the *definition* half of the Forge Benchmark Lab. It contains no
runner and no I/O: a scenario is a validated specification, a metric is a
definition with a stated numerator/denominator, and a comparison is a set of
compatibility rules applied to persisted run records.

The honesty rules the rest of the phase depends on live here:

* **A scenario says whether its success criteria can be evaluated.** The
  observation scenarios configure no objective; the ``controlled-objective``
  scenario runs only through an NF-05 controlled evaluation, with an approved
  objective and plan. An autonomous gameplay score remains UNSUPPORTED with the
  reason, rather than being silently substituted by an observation run.
* **Missing is not zero.** A metric whose source did not measure it is a row
  with ``available=False`` and a reason. It is never ``0``.
* **Success is not completion.** A run can be ``completed`` (the observation
  ran for its budget and the data is complete) while ``objective_achieved`` is
  ``None``: no objective was configured, and NF-04 claims no gameplay success.
  A controlled run's verdict is recorded only when its verifier decided one.
* **Comparisons are refused, not fudged.** Different scenarios, versions,
  budgets, observation intervals, target windows or geometries produce
  ``compatible=False``. Compatible runs still get ``established=False``: one
  run per side cannot show variability, and NF-04 invokes no model or policy.
"""

from __future__ import annotations

import math
import platform
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

BENCHMARK_VERSION = 1
SCENARIO_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1

RUN_STATUSES = ("running", "completed", "failed", "aborted", "invalid")
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "aborted", "invalid"})
#: Only a completed run whose data met the scenario's completeness floor is
#: eligible for aggregates. The others are counted and their reasons are
#: reported, never dropped from the attempt count.
ELIGIBLE_STATUS = "completed"

MAX_DURATION_SECONDS = 300
MIN_DURATION_SECONDS = 1
MAX_INTERVAL_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 0.05
MAX_NOTES = 500

#: Sixteen significant figures are not a measurement; three decimals is the
#: resolution every source in NF-02 reports (ms, seconds, ratios).
ROUND_TO = 3


class BenchmarkError(ValueError):
    """Base class for benchmark request errors the API maps to a status."""


class BenchmarkValidationError(BenchmarkError):
    """A scenario or run request that cannot be run as asked."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = [str(item) for item in errors]
        super().__init__(" ".join(self.errors))


class BenchmarkNotFoundError(BenchmarkError):
    """An unknown scenario, version or run id."""


class BenchmarkConflictError(BenchmarkError):
    """A start that would overlap a live session or benchmark run."""


# ── Metric definitions ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric's definition, including the numbers behind it.

    ``kind`` is what the value IS:

    * ``measured`` — a counter or duration recorded at the point it happened.
    * ``derived`` — arithmetic over measured values (the formula is stated).
    * ``unsupported`` — no source exists; the value is always unavailable.
    """

    key: str
    label: str
    unit: str
    kind: str
    numerator: str
    denominator: str
    aggregation: str
    eligibility: str
    method: str
    rounding: str = f"{ROUND_TO} decimals"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "kind": self.kind,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "aggregation": self.aggregation,
            "eligibility": self.eligibility,
            "method": self.method,
            "rounding": self.rounding,
            "available": self.kind != "unsupported",
        }


METRIC_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec(
        key="frames",
        label="Fresh frames",
        unit="frames",
        kind="measured",
        numerator="fresh frames written and verified by the session",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="stale_frames",
        label="Stale captures",
        unit="captures",
        kind="measured",
        numerator="captures that returned without a fresh frame",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="capture_failures",
        label="Capture failures",
        unit="failures",
        kind="measured",
        numerator="capture attempts that raised",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="frame_gaps",
        label="Frame gaps",
        unit="gaps",
        kind="measured",
        numerator="inter-frame intervals above the session's gap threshold",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="max_gap_ms",
        label="Longest frame gap",
        unit="ms",
        kind="measured",
        numerator="longest observed inter-frame interval",
        denominator="one run",
        aggregation="median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="latency_median_ms",
        label="Capture latency (median)",
        unit="ms",
        kind="derived",
        numerator="nearest-rank 50th percentile of the newest latency window",
        denominator="latency samples in that window",
        aggregation="median of per-run medians across eligible runs",
        eligibility="run recorded at least one usable capture duration",
        method="Telemetry's documented nearest-rank percentile over its bounded window.",
    ),
    MetricSpec(
        key="latency_p95_ms",
        label="Capture latency (p95)",
        unit="ms",
        kind="derived",
        numerator="nearest-rank 95th percentile of the newest latency window",
        denominator="latency samples in that window (at least 20)",
        aggregation="median of per-run p95s across eligible runs",
        eligibility="run recorded at least 20 usable capture durations",
        method="Unavailable when fewer than 20 samples exist; a p95 over fewer is not reported.",
    ),
    MetricSpec(
        key="latency_max_ms",
        label="Capture latency (max)",
        unit="ms",
        kind="measured",
        numerator="slowest capture in the run",
        denominator="one run",
        aggregation="median across eligible runs",
        eligibility="run recorded at least one usable capture duration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="duration_seconds",
        label="Run duration",
        unit="s",
        kind="measured",
        numerator="wall-clock duration from the session's start to its end",
        denominator="one run",
        aggregation="median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Backend duration derived from the session's ISO timestamps.",
    ),
    MetricSpec(
        key="active_seconds",
        label="Active time",
        unit="s",
        kind="measured",
        numerator="time the session was RUNNING, measured with time.monotonic",
        denominator="one run",
        aggregation="median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="frame_yield",
        label="Frame yield",
        unit="ratio",
        kind="derived",
        numerator="fresh frames",
        denominator="observation opportunities (floor(active_seconds / interval) + 1)",
        aggregation="median across eligible runs",
        eligibility="completed and valid runs with a positive active duration",
        method="An opportunity is one scheduled capture slot; the ratio is capped at 1.0.",
    ),
    MetricSpec(
        key="observed_rate_hz",
        label="Observed frame rate",
        unit="Hz",
        kind="derived",
        numerator="fresh frames",
        denominator="active seconds",
        aggregation="median across eligible runs",
        eligibility="run has frames and at least 5 active seconds (telemetry's own floor)",
        method="Taken from telemetry's rate, which refuses to divide a short active window.",
    ),
    MetricSpec(
        key="target_checks",
        label="Target checks",
        unit="checks",
        kind="measured",
        numerator="geometry checks confirming the target still matches",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="target_losses",
        label="Target losses",
        unit="events",
        kind="measured",
        numerator="checks that found the target window gone",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="target_changes",
        label="Target changes",
        unit="events",
        kind="measured",
        numerator="checks that found the target replaced or moved",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the session's telemetry snapshot at finalize.",
    ),
    MetricSpec(
        key="timeline_events",
        label="Evidence events",
        unit="events",
        kind="measured",
        numerator="events retained in the session's evidence timeline",
        denominator="one run",
        aggregation="median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Read from the timeline's retention counter at finalize; retention is bounded.",
    ),
    MetricSpec(
        key="interventions",
        label="Guardian interventions",
        unit="events",
        kind="measured",
        numerator="recorded stop/cancel/emergency-stop interventions",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Counted from the session record's intervention list.",
    ),
    MetricSpec(
        key="errors",
        label="Session errors",
        unit="errors",
        kind="measured",
        numerator="errors recorded on the session",
        denominator="one run",
        aggregation="sum per run; median across eligible runs",
        eligibility="completed and valid runs recorded on the same configuration",
        method="Counted from the session record's bounded error list.",
    ),
    # ── Unsupported: no measurement source exists in this build ────────────
    MetricSpec(
        key="objective_success",
        label="Verified objective success",
        unit="ratio",
        kind="measured",
        numerator="runs whose objective a verifier decided successfully",
        denominator="runs with a decided verdict",
        aggregation="sum per run; median across comparable runs",
        eligibility="the session configured a supported objective and recorded a decided verdict",
        method=(
            "Read from the session's recorded objective verdict (verified_success = 1, "
            "verified_failure = 0). A run with no objective or an undecided verdict is "
            "UNAVAILABLE, never zero."
        ),
    ),
    MetricSpec(
        key="completion_time_seconds",
        label="Time to verified objective",
        unit="s",
        kind="derived",
        numerator="verdict time minus session start",
        denominator="one run with a decided verdict",
        aggregation="median across comparable runs",
        eligibility="the session recorded a decided objective verdict",
        method=(
            "The verifier's own observation timestamp minus the session's start. Reported "
            "only for a decided verdict; an unresolved run has no completion time."
        ),
    ),
    MetricSpec(
        key="actions_executed",
        label="Executed actions",
        unit="actions",
        kind="measured",
        numerator="actions whose input was sent and whose presses were released",
        denominator="one run",
        aggregation="sum per run; median across comparable runs",
        eligibility="a session with a wired action path",
        method=(
            "Counted from the session record's actions_executed. A refused or failed "
            "action never increments it, so a refusal is not counted as an action."
        ),
    ),
    MetricSpec(
        key="recovery_attempts",
        label="Recovery attempts",
        unit="attempts",
        kind="unsupported",
        numerator="retry/recovery attempts recorded by the session",
        denominator="one run",
        aggregation="not computed",
        eligibility="none",
        method="No retry or recovery path is attached to a NovaForge session.",
    ),
    MetricSpec(
        key="cpu_percent",
        label="CPU use",
        unit="%",
        kind="unsupported",
        numerator="CPU time attributable to the run",
        denominator="run duration",
        aggregation="not computed",
        eligibility="none",
        method=(
            "No per-run CPU probe is attached. A point-in-time host reading is recorded as "
            "environment metadata and is NOT run usage."
        ),
    ),
    MetricSpec(
        key="memory_mb",
        label="Memory use",
        unit="MB",
        kind="unsupported",
        numerator="memory attributable to the run",
        denominator="one run",
        aggregation="not computed",
        eligibility="none",
        method="No per-run memory probe is attached; a host snapshot is not run usage.",
    ),
    MetricSpec(
        key="gpu_percent",
        label="GPU use",
        unit="%",
        kind="unsupported",
        numerator="GPU utilization attributable to the run",
        denominator="run duration",
        aggregation="not computed",
        eligibility="none",
        method="No GPU provider reports per-run utilization in this build.",
    ),
)

_METRICS_BY_KEY: dict[str, MetricSpec] = {spec.key: spec for spec in METRIC_CATALOG}


def metric_spec(key: str) -> MetricSpec:
    """Look up one definition; an unknown key is a programming error."""
    try:
        return _METRICS_BY_KEY[key]
    except KeyError as exc:  # pragma: no cover - catalog tests prevent this
        raise KeyError(f"Unknown benchmark metric {key!r}.") from exc


def metrics_for(keys: Iterable[str]) -> list[MetricSpec]:
    return [metric_spec(key) for key in keys]


def metric_catalog() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in METRIC_CATALOG]


# ── Scenario specification ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    """A validated, versioned benchmark specification.

    Every field is part of the contract a run records, so a later catalog
    change cannot rewrite what an older run was asked to measure.
    """

    scenario_id: str
    version: int
    name: str
    objective: str
    kind: str
    evaluation: str
    supported: bool
    unsupported_reason: str
    target_requirement: str
    preconditions: tuple[str, ...]
    reset_procedure: str
    default_duration_seconds: int
    frame_interval_seconds: float
    min_frames: int
    min_latency_samples: int
    observation_source: str
    capabilities: Mapping[str, bool]
    success_criteria: str
    verifier: Mapping[str, Any]
    required_evidence: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    reproducibility: tuple[str, ...]
    metrics: tuple[str, ...]
    limitations: tuple[str, ...] = field(default_factory=tuple)
    #: True when the scenario's meaning depends on a configured objective. An
    #: observation scenario never sets this, so its data completeness can never
    #: be mistaken for an objective verdict.
    objective_required: bool = False

    @property
    def key(self) -> str:
        return f"{self.scenario_id}@{self.version}"

    def snapshot(self) -> dict[str, Any]:
        """The scenario as a RUN records it: definitions live in the catalog.

        A run stores what it must reproduce, not a copy of the metric
        definitions (each row already carries its label, unit and reason). The
        catalog endpoint serves the definitions, so a 60-run history does not
        carry 60 copies of the same prose.
        """
        data = self.to_dict()
        data.pop("metric_definitions", None)
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCENARIO_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "version": self.version,
            "key": self.key,
            "name": self.name,
            "objective": self.objective,
            "kind": self.kind,
            "evaluation": self.evaluation,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
            "target_requirement": self.target_requirement,
            "preconditions": list(self.preconditions),
            "reset_procedure": self.reset_procedure,
            "default_duration_seconds": self.default_duration_seconds,
            "frame_interval_seconds": self.frame_interval_seconds,
            "min_frames": self.min_frames,
            "min_latency_samples": self.min_latency_samples,
            "observation_source": self.observation_source,
            "capabilities": dict(self.capabilities),
            "success_criteria": self.success_criteria,
            "verifier": dict(self.verifier),
            "required_evidence": list(self.required_evidence),
            "failure_conditions": list(self.failure_conditions),
            "reproducibility": list(self.reproducibility),
            "metrics": list(self.metrics),
            "metric_definitions": [spec.to_dict() for spec in metrics_for(self.metrics)],
            "limitations": list(self.limitations),
            "objective_required": bool(self.objective_required),
        }


_OBSERVATION_CAPABILITIES: Mapping[str, bool] = {
    "observe": True,
    "capture": True,
    "act": False,
    "verify_objective": False,
}

_OBSERVATION_PRECONDITIONS: tuple[str, ...] = (
    "Screen capture is supported on this platform.",
    "The target window is open, visible, and uniquely identified by its title.",
    "No NovaForge session or benchmark run is active.",
)

_OBSERVATION_REPRODUCIBILITY: tuple[str, ...] = (
    "The run records the scenario version, resolved duration and observation interval.",
    "The run records the target's measured geometry and the platform.",
    "A repeated run is comparable only when version, duration, interval and target geometry match.",
)

_OBSERVATION_FAILURES: tuple[str, ...] = (
    "The target window disappears or is replaced during the run.",
    "Capture raises or returns a non-fresh result for the whole run.",
    "The application stops while the run is active (recovered as ABORTED).",
    "The observation budget ends before the scenario's minimum frames are recorded.",
)

SCENARIOS: tuple[BenchmarkScenario, ...] = (
    BenchmarkScenario(
        scenario_id="observation-baseline",
        version=1,
        name="Observation Baseline",
        objective="Measure a fixed-length window observation: frame yield, continuity and capture latency.",
        kind="observation",
        evaluation="observation_only",
        supported=True,
        unsupported_reason="",
        target_requirement="One already-open window whose title uniquely identifies it.",
        preconditions=_OBSERVATION_PRECONDITIONS,
        reset_procedure=(
            "None. The benchmark only observes: it never launches, restarts or modifies the target."
        ),
        default_duration_seconds=10,
        frame_interval_seconds=0.5,
        min_frames=4,
        min_latency_samples=1,
        observation_source="NovaForge window-cropped screen capture (primary display).",
        capabilities=_OBSERVATION_CAPABILITIES,
        success_criteria=(
            "Data completeness only: the observation ran for its budget and recorded at least "
            "4 fresh frames. This is NOT gameplay success and no objective is claimed."
        ),
        verifier={
            "available": False,
            "kind": "none",
            "reason": "No objective verifier is attached to a NovaForge observation session.",
        },
        required_evidence=("session record", "captured frame references", "telemetry snapshot"),
        failure_conditions=_OBSERVATION_FAILURES,
        reproducibility=_OBSERVATION_REPRODUCIBILITY,
        metrics=(
            "frames",
            "frame_yield",
            "frame_gaps",
            "max_gap_ms",
            "latency_median_ms",
            "duration_seconds",
            "active_seconds",
            "target_checks",
            "timeline_events",
        ),
        limitations=(
            "Measurements describe capture and observation behavior, not gameplay capability.",
            "Capture is primary-display only and cropped to the target window.",
            "No model or policy is invoked, so this scenario cannot compare models.",
        ),
    ),
    BenchmarkScenario(
        scenario_id="capture-latency",
        version=1,
        name="Capture Latency Distribution",
        objective="Collect enough capture durations for a defined median, p95 and maximum.",
        kind="capture",
        evaluation="observation_only",
        supported=True,
        unsupported_reason="",
        target_requirement="One already-open window; larger windows cost more to encode.",
        preconditions=_OBSERVATION_PRECONDITIONS,
        reset_procedure="None. Capture is read-only; the target is never relaunched or resized.",
        default_duration_seconds=30,
        frame_interval_seconds=0.25,
        min_frames=20,
        min_latency_samples=20,
        observation_source="NovaForge window-cropped screen capture (primary display).",
        capabilities=_OBSERVATION_CAPABILITIES,
        success_criteria=(
            "Data completeness only: at least 20 frames with usable capture durations were "
            "recorded, so the p95 is defined. This is NOT gameplay success."
        ),
        verifier={
            "available": False,
            "kind": "none",
            "reason": "Latency is measured; objective achievement is not evaluated.",
        },
        required_evidence=("session record", "captured frame references", "telemetry snapshot"),
        failure_conditions=_OBSERVATION_FAILURES,
        reproducibility=_OBSERVATION_REPRODUCIBILITY,
        metrics=(
            "frames",
            "latency_median_ms",
            "latency_p95_ms",
            "latency_max_ms",
            "frame_yield",
            "max_gap_ms",
            "stale_frames",
            "capture_failures",
        ),
        limitations=(
            "A p95 needs at least 20 latency samples; below that it is reported unavailable.",
            "Percentiles cover the telemetry's bounded newest window, which the payload names.",
            "Host load and window size affect latency; both are recorded as environment context.",
        ),
    ),
    BenchmarkScenario(
        scenario_id="observation-continuity",
        version=1,
        name="Observation Continuity",
        objective="Measure gaps, stale captures and target-window stability over a longer window.",
        kind="observation",
        evaluation="observation_only",
        supported=True,
        unsupported_reason="",
        target_requirement="One already-open, stationary window.",
        preconditions=_OBSERVATION_PRECONDITIONS,
        reset_procedure="None. The target is observed, never driven or relaunched.",
        default_duration_seconds=20,
        frame_interval_seconds=1.0,
        min_frames=10,
        min_latency_samples=1,
        observation_source="NovaForge window-cropped screen capture (primary display).",
        capabilities=_OBSERVATION_CAPABILITIES,
        success_criteria=(
            "Data completeness only: the run lasted its budget and recorded at least 10 fresh "
            "frames. This is NOT gameplay success."
        ),
        verifier={
            "available": False,
            "kind": "none",
            "reason": "Continuity is measured; objective achievement is not evaluated.",
        },
        required_evidence=("session record", "captured frame references", "telemetry snapshot"),
        failure_conditions=_OBSERVATION_FAILURES,
        reproducibility=_OBSERVATION_REPRODUCIBILITY,
        metrics=(
            "frames",
            "frame_gaps",
            "max_gap_ms",
            "stale_frames",
            "target_checks",
            "target_losses",
            "target_changes",
            "frame_yield",
        ),
        limitations=(
            "A paused session does not advance its frame count; pauses are reported in telemetry.",
            "Target checks detect a vanished or replaced window, not visual content changes.",
        ),
    ),
    BenchmarkScenario(
        scenario_id="controlled-objective",
        version=1,
        name="Controlled Objective Run",
        objective=(
            "Execute an operator-approved action plan in one validated window and measure the "
            "objective verdict a verifier reads from the target's own evidence."
        ),
        kind="gameplay",
        evaluation="controlled_objective",
        supported=True,
        unsupported_reason="",
        target_requirement=(
            "One already-open window with a configured, supported objective and a wired "
            "Guardian-enforced action path."
        ),
        preconditions=(
            "The operator opened and validated the target window; nothing is launched.",
            "A supported objective is configured for the session before any action.",
            "Every action is approved by digest and passes the Guardian gate.",
            "A time limit and an action budget are both configured.",
        ),
        reset_procedure=(
            "None automated. The operator prepares the target's starting state and must open a "
            "fresh session afterwards; NF-05 never resets or restarts the target."
        ),
        default_duration_seconds=20,
        frame_interval_seconds=0.5,
        min_frames=4,
        min_latency_samples=1,
        observation_source="NovaForge window-cropped capture plus the session's recorded actions.",
        capabilities={
            "observe": True,
            "capture": True,
            "act": True,
            "verify_objective": True,
        },
        success_criteria=(
            "A decided objective verdict: verified_success when the verifier's own read meets "
            "the objective, verified_failure when it demonstrably does not. An undecided run is "
            "inconclusive, never a success, and this scenario runs no model to compare."
        ),
        verifier={
            "available": True,
            "kind": "operator_configured",
            "reason": (
                "The session's objective verifier reads the target's own evidence (its window "
                "title or a game-written state artifact); the action record is never evidence."
            ),
        },
        required_evidence=(
            "session record",
            "action records",
            "objective verification result",
            "captured frame references",
        ),
        failure_conditions=(
            "The objective cannot be read (missing, stale, malformed or out-of-root evidence).",
            "An action is refused by the Guardian, the budget, or an approval mismatch.",
            "The target window disappears or is replaced during the run.",
            "The run ends before a verdict: it is inconclusive, not a success.",
        ),
        reproducibility=(
            "The run records the objective definition digest, the approved plan digest, the "
            "target identity and both budgets.",
            "A repeated run is comparable only when those digests, the scenario version and the "
            "window geometry match; a different objective is never comparable.",
        ),
        metrics=(
            "objective_success",
            "completion_time_seconds",
            "actions_executed",
            "frames",
            "duration_seconds",
            "errors",
            "interventions",
            "timeline_events",
        ),
        limitations=(
            "The plan is operator-authored: this scenario proves controlled execution, not "
            "autonomous play, and no model or policy chooses an action.",
            "An undecided verdict is reported as inconclusive; it is never converted to a "
            "failure or a success.",
            "The target is never launched or reset by this build; the operator prepares it.",
            "A verified success closes the session through the manager's own "
            "objective_verified path, so a fast success records fewer frames than the "
            "scenario's observation floor; its decided verdict, not the frame count, is "
            "the result (at least one fresh frame is still required).",
        ),
        objective_required=True,
    ),
    BenchmarkScenario(
        scenario_id="gameplay-objective",
        version=1,
        name="Gameplay Objective (unsupported)",
        objective="Score an in-game objective against a verifier across models or policies.",
        kind="gameplay",
        evaluation="objective_verified",
        supported=False,
        unsupported_reason=(
            "Autonomous gameplay scoring needs a decision pipeline that neither NF-04 nor "
            "NF-05 has: a controlled evaluation executes an OPERATOR-approved plan, so no "
            "agent chooses actions and no model is compared. Running this scenario would "
            "fabricate the decision half of the score."
        ),
        target_requirement="An action-capable session with an objective verifier.",
        preconditions=(
            "A decision pipeline that can select in-game actions.",
            "An input path with Guardian enforcement.",
            "A verifier that can confirm the objective independently of the session's report.",
        ),
        reset_procedure="A supported reset that returns the game to a known initial state.",
        default_duration_seconds=60,
        frame_interval_seconds=0.5,
        min_frames=1,
        min_latency_samples=0,
        observation_source="Would require gameplay capture plus action records; neither exists.",
        capabilities={
            "observe": True,
            "capture": True,
            "act": False,
            "verify_objective": False,
        },
        success_criteria=(
            "Not evaluable autonomously: no decision pipeline chooses actions to score."
        ),
        verifier={
            "available": False,
            "kind": "none",
            "reason": (
                "A controlled run verifies an objective through an operator-approved plan; "
                "an autonomous score would need a decision half this build does not have."
            ),
        },
        required_evidence=("action records", "objective verification result"),
        failure_conditions=(
            "Any run is unsupported until an autonomous decision pipeline exists; a "
            "controlled evaluation uses the controlled-objective scenario instead.",
        ),
        reproducibility=(
            "Blocked: a starting state cannot be reset and no model is run.",
        ),
        metrics=("objective_success", "completion_time_seconds", "actions_executed"),
        limitations=(
            "This entry is a catalog record of an unsupported capability, not a runnable scenario.",
        ),
    ),
)

_SCENARIOS_BY_ID: dict[str, list[BenchmarkScenario]] = {}
for _scenario in SCENARIOS:
    _SCENARIOS_BY_ID.setdefault(_scenario.scenario_id, []).append(_scenario)
for _versions in _SCENARIOS_BY_ID.values():
    _versions.sort(key=lambda item: item.version)


def latest_version(scenario_id: str) -> int | None:
    versions = _SCENARIOS_BY_ID.get(scenario_id)
    return versions[-1].version if versions else None


def resolve_scenario(scenario_id: str, version: Any = None) -> BenchmarkScenario | None:
    """Resolve an exact catalog entry; ``None`` means unknown, never a guess."""
    versions = _SCENARIOS_BY_ID.get(scenario_id)
    if not versions:
        return None
    if version in (None, ""):
        return versions[-1]
    if isinstance(version, bool):
        # ``int(True)`` is 1; a bool is not a version number.
        return None
    try:
        wanted = int(version)
    except (TypeError, ValueError):
        return None
    return next((item for item in versions if item.version == wanted), None)


def scenario_payload() -> dict[str, Any]:
    """The scenario catalog plus the metric definitions the UI labels rows with."""
    supported = [item for item in SCENARIOS if item.supported]
    return {
        "version": BENCHMARK_VERSION,
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "scenarios": [item.to_dict() for item in SCENARIOS],
        "supported_count": len(supported),
        "unsupported_count": len(SCENARIOS) - len(supported),
        "metric_catalog": metric_catalog(),
        "note": (
            "Observation and capture benchmarks are runnable directly. The controlled-objective "
            "scenario is run only through a confirmed NF-05 evaluation with an approved action "
            "plan; an autonomous gameplay score remains unsupported rather than approximated."
        ),
    }


# ── Run configuration ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """The resolved configuration of one run, recorded verbatim."""

    scenario_id: str
    version: int
    target_title: str
    duration_seconds: int
    frame_interval_seconds: float
    notes: str = ""
    model_id: str = ""
    policy_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "scenario_id": self.scenario_id,
            "version": self.version,
            "target_title": self.target_title,
            "duration_seconds": self.duration_seconds,
            "frame_interval_seconds": self.frame_interval_seconds,
            "notes": self.notes,
            "model_id": self.model_id,
            "policy_id": self.policy_id,
        }


def _optional_float(data: Mapping[str, Any], key: str) -> float | None:
    """An optional finite number; ``-1.0`` means the caller sent something else.

    A bool is not a duration. ``float(True)`` is ``1.0``, so without this check a
    JSON ``true`` would silently become a one-second observation interval.
    """
    value = data.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return -1.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    return number if math.isfinite(number) else -1.0


def _optional_int(data: Mapping[str, Any], key: str) -> int | None:
    """An optional whole number; ``-1`` means the caller sent something else.

    ``int(2.9)`` truncates and ``int(True)`` is ``1``. Both would record a run
    different from the one the caller asked for, so a non-integral or non-numeric
    value is refused rather than quietly rounded.
    """
    value = data.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return -1
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1
    if not math.isfinite(number) or not number.is_integer():
        return -1
    return int(number)


def config_from_request(
    payload: Mapping[str, Any],
) -> tuple[BenchmarkScenario | None, BenchmarkConfig | None, list[str]]:
    """Resolve a request into a scenario and config, or return explicit reasons.

    Defaults come from the scenario, so a caller may send only a scenario id and
    a target. Every override is recorded in the run and checked for fairness
    before it is accepted.
    """
    data = dict(payload or {})
    scenario_id = str(data.get("scenario_id", "") or "").strip()
    errors: list[str] = []
    if not scenario_id:
        errors.append("A benchmark scenario id is required.")
        return None, None, errors

    version = data.get("version")
    scenario = resolve_scenario(scenario_id, version)
    if scenario is None:
        known = latest_version(scenario_id)
        if known is None:
            errors.append(f"Unknown benchmark scenario {scenario_id!r}.")
        else:
            errors.append(
                f"Scenario {scenario_id!r} has no version {version!r}; "
                f"the catalog's latest is {known}."
            )
        return None, None, errors

    target = str(data.get("target_title", "") or "").strip()
    if not target:
        errors.append("A target window title is required.")
    elif len(target) > 200:
        errors.append("Target window title must be at most 200 characters.")

    duration = _optional_int(data, "duration_seconds")
    if duration is None:
        duration = scenario.default_duration_seconds
    if duration is not None and not (MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS):
        errors.append(
            f"Duration must be between {MIN_DURATION_SECONDS} and "
            f"{MAX_DURATION_SECONDS} seconds."
        )

    interval = _optional_float(data, "frame_interval_seconds")
    if interval is None:
        interval = scenario.frame_interval_seconds
    if interval is not None and not (MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        errors.append(
            f"Observation interval must be between {MIN_INTERVAL_SECONDS} and "
            f"{MAX_INTERVAL_SECONDS} seconds."
        )

    notes = str(data.get("notes", "") or "").strip()
    if len(notes) > MAX_NOTES:
        errors.append(f"Notes must be at most {MAX_NOTES} characters.")

    model_id = str(data.get("model_id", "") or "").strip()
    policy_id = str(data.get("policy_id", "") or "").strip()
    if model_id or policy_id:
        errors.append(
            "NF-04 runs observation-only benchmarks and invokes no model or policy, so "
            "selecting one would fabricate a comparison. Leave model_id and policy_id empty."
        )

    if scenario.supported is False:
        errors.append(scenario.unsupported_reason)

    if not errors and duration is not None and interval is not None:
        possible = int(duration / interval) + 1
        if possible < scenario.min_frames:
            errors.append(
                f"This scenario needs at least {scenario.min_frames} frames, but a "
                f"{duration}s run at a {interval}s interval can record at most {possible}. "
                "Increase the duration or shorten the interval."
            )
        if possible < scenario.min_latency_samples:
            errors.append(
                f"This scenario needs at least {scenario.min_latency_samples} latency "
                f"samples, but the configuration allows at most {possible} frames."
            )

    if errors:
        return scenario, None, errors
    assert duration is not None and interval is not None
    return (
        scenario,
        BenchmarkConfig(
            scenario_id=scenario.scenario_id,
            version=scenario.version,
            target_title=target,
            duration_seconds=duration,
            frame_interval_seconds=round(float(interval), 3),
            notes=notes,
            model_id=model_id,
            policy_id=policy_id,
        ),
        [],
    )


# ── Metric collection ───────────────────────────────────────────────────────


def _row(
    value: Any,
    *,
    source: str,
    available: bool = True,
    reason: str = "",
    kind: str = "measured",
    unit: str = "",
) -> dict[str, Any]:
    return {
        "value": value if available else None,
        "available": bool(available),
        # An unavailable row without a reason is the phase's core honesty
        # failure, so the contract is enforced here as a fallback as well as at
        # every call site: a new collector cannot quietly ship a bare "null".
        "reason": "" if available else (reason or "The source did not record this value."),
        "source": source,
        "kind": kind,
        "unit": unit,
    }


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    return None


def _unavailable(key: str, reason: str) -> dict[str, Any]:
    spec = metric_spec(key)
    return _row(
        None,
        source="none",
        available=False,
        reason=reason or spec.method,
        kind=spec.kind,
        unit=spec.unit,
    )


def collect_metrics(
    *,
    scenario: BenchmarkScenario,
    telemetry: Mapping[str, Any],
    events: int,
    errors: int,
    interventions: int,
    objective: Mapping[str, Any] | None = None,
    actions: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one run's metric rows from telemetry that was actually measured.

    The telemetry payload is NF-02's own snapshot, so this function adds no
    probe and cannot invent a value: a missing field becomes an unavailable row
    with the reason the source gave (or a stated absence). ``objective`` is the
    session's recorded verdict block and ``actions`` its executed count; both
    default to absent, so an observation-only run keeps reporting those metrics
    as unavailable with a reason instead of a fabricated zero.
    """
    capture = dict(telemetry.get("capture") or {})
    latency = dict(capture.get("latency_ms") or {})
    loop = dict(telemetry.get("loop") or {})
    session = dict(telemetry.get("session") or {})
    target = dict(telemetry.get("target") or {})

    active = _number(session.get("active_seconds"))
    configured_ms = _number(loop.get("configured_interval_ms"))
    interval = (float(configured_ms) / 1000.0) if configured_ms else None
    frames = _number(capture.get("frames"))
    expected = None
    if active is not None and active > 0 and interval and interval > 0:
        # One capture is attempted immediately, then one per interval of active
        # time. It is an opportunity count, not a promise: a slow capture wins.
        expected = int(float(active) / float(interval)) + 1

    rows: dict[str, dict[str, Any]] = {}
    for key in scenario.metrics:
        spec = metric_spec(key)
        if spec.kind == "unsupported":
            rows[key] = _unavailable(key, spec.method)
            continue

        if key == "frames":
            rows[key] = _row(
                frames,
                source="telemetry.capture.frames",
                available=frames is not None,
                reason="The telemetry snapshot did not include a frame count.",
                kind=spec.kind,
                unit=spec.unit,
            )
        elif key == "stale_frames":
            value = _number(capture.get("stale_frames"))
            rows[key] = _row(
                value, source="telemetry.capture.stale_frames",
                available=value is not None,
                reason="The telemetry snapshot did not include a stale-frame count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "capture_failures":
            value = _number(capture.get("capture_failures"))
            rows[key] = _row(
                value, source="telemetry.capture.capture_failures",
                available=value is not None,
                reason="The telemetry snapshot did not include a capture-failure count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "frame_gaps":
            value = _number(capture.get("frame_gaps"))
            rows[key] = _row(
                value, source="telemetry.capture.frame_gaps",
                available=value is not None,
                reason="The telemetry snapshot did not include a frame-gap count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "max_gap_ms":
            value = _number(capture.get("max_gap_ms"))
            rows[key] = _row(
                value, source="telemetry.capture.max_gap_ms",
                available=value is not None,
                reason="The telemetry snapshot did not include a longest-frame-gap figure.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "latency_median_ms":
            value = _number(latency.get("median_ms"))
            rows[key] = _row(
                value, source="telemetry.capture.latency_ms.median_ms",
                available=value is not None,
                # The p95 reason explains a p95, not a median: quoting it here said
                # "a p95 needs at least 20 samples" beside a missing median.
                reason="No usable capture-latency sample was recorded for this run.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "latency_p95_ms":
            value = _number(latency.get("p95_ms"))
            rows[key] = _row(
                value, source="telemetry.capture.latency_ms.p95_ms",
                available=value is not None,
                reason=str(latency.get("p95_reason") or spec.method),
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "latency_max_ms":
            value = _number(latency.get("max_ms"))
            rows[key] = _row(
                value, source="telemetry.capture.latency_ms.max_ms",
                available=value is not None,
                reason="The telemetry snapshot did not include a maximum capture latency.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "duration_seconds":
            value = _number(session.get("duration_seconds"))
            rows[key] = _row(
                value, source="session.duration_seconds",
                available=value is not None,
                reason="The session record did not report a duration.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "active_seconds":
            rows[key] = _row(
                active, source="telemetry.session.active_seconds",
                available=active is not None,
                reason="The telemetry snapshot did not include active observation time.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "frame_yield":
            if frames is None or expected in (None, 0):
                rows[key] = _row(
                    None, source="derived", available=False,
                    reason=(
                        "No frame count or no active observation time was recorded, so "
                        "the opportunity denominator is undefined."
                    ),
                    kind=spec.kind, unit=spec.unit,
                )
            else:
                rows[key] = _row(
                    round(min(float(frames) / float(expected), 1.0), ROUND_TO),
                    source="derived: frames / observation opportunities",
                    kind=spec.kind, unit=spec.unit,
                )
        elif key == "observed_rate_hz":
            value = _number(loop.get("observed_frame_rate_hz"))
            rows[key] = _row(
                value, source="telemetry.loop.observed_frame_rate_hz",
                available=value is not None,
                reason=str(loop.get("rate_reason") or spec.method),
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "target_checks":
            value = _number(target.get("checks"))
            rows[key] = _row(
                value, source="telemetry.target.checks",
                available=value is not None,
                reason="The telemetry snapshot did not include a target-check count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "target_losses":
            value = _number(target.get("losses"))
            rows[key] = _row(
                value, source="telemetry.target.losses",
                available=value is not None,
                reason="The telemetry snapshot did not include a target-loss count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "target_changes":
            value = _number(target.get("changes"))
            rows[key] = _row(
                value, source="telemetry.target.changes",
                available=value is not None,
                reason="The telemetry snapshot did not include a target-change count.",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "timeline_events":
            rows[key] = _row(
                int(events), source="timeline.retention.stored",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "interventions":
            rows[key] = _row(
                int(interventions), source="session.interventions",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "errors":
            rows[key] = _row(
                int(errors), source="session.errors",
                kind=spec.kind, unit=spec.unit,
            )
        elif key == "objective_success":
            achieved = (objective or {}).get("achieved")
            available = isinstance(achieved, bool)
            rows[key] = _row(
                1.0 if achieved is True else 0.0 if achieved is False else None,
                source="session.verification.achieved",
                available=available,
                reason=str((objective or {}).get("reason") or "No decided objective verdict was recorded for this run."),
                kind=spec.kind,
                unit=spec.unit,
            )
        elif key == "completion_time_seconds":
            value = _number((objective or {}).get("completion_time_seconds"))
            rows[key] = _row(
                value,
                source="session.verification.at - session.started_at",
                available=value is not None,
                reason=str((objective or {}).get("reason") or (
                    "The objective was not decided, so there is no completion time to measure."
                )),
                kind=spec.kind,
                unit=spec.unit,
            )
        elif key == "actions_executed":
            value = _number(actions)
            rows[key] = _row(
                value,
                source="session.counts.actions_executed",
                available=value is not None,
                reason=(
                    "This session has no recorded action count, so the executed-action "
                    "count is unavailable — it is not zero."
                ),
                kind=spec.kind,
                unit=spec.unit,
            )
        else:  # pragma: no cover - catalog tests keep this unreachable
            rows[key] = _unavailable(key, f"No collector exists for {key!r}.")
    return rows


def unsupported_metrics() -> list[dict[str, Any]]:
    """Every definition with no measurement source, for the payload's honesty block."""
    return [spec.to_dict() for spec in METRIC_CATALOG if spec.kind == "unsupported"]


# ── Aggregation ─────────────────────────────────────────────────────────────


def _mapping(value: Any) -> dict[str, Any]:
    """A JSON object as a dict; anything else is an absence, not a crash.

    Run records are read back from a hand-editable snapshot. A nested field that
    is not an object (a string where ``metrics`` belongs, say) must not take the
    whole history read down with an ``AttributeError``.
    """
    return dict(value) if isinstance(value, Mapping) else {}


def _numeric_values(
    runs: Sequence[Mapping[str, Any]], key: str
) -> tuple[list[float], str]:
    values: list[float] = []
    reason = ""
    for run in runs:
        row = _mapping(_mapping(run.get("metrics")).get(key))
        value = _number(row.get("value"))
        if row.get("available") and value is not None:
            values.append(float(value))
        elif not reason:
            reason = str(row.get("reason") or "No eligible run measured this metric.")
    return values, reason


def _summary(values: Sequence[float]) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "value": None,
            "min": None,
            "median": None,
            "mean": None,
            "max": None,
            "stdev": None,
        }
    ordered = sorted(values)
    return {
        "n": n,
        "value": round(statistics.median(ordered), ROUND_TO),
        "min": round(ordered[0], ROUND_TO),
        "median": round(statistics.median(ordered), ROUND_TO),
        "mean": round(statistics.fmean(ordered), ROUND_TO),
        "max": round(ordered[-1], ROUND_TO),
        # A single sample has no variability; None is that fact, not 0.0.
        "stdev": round(statistics.stdev(ordered), ROUND_TO) if n >= 2 else None,
    }


def eligible_runs(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        run
        for run in runs
        if run.get("status") == ELIGIBLE_STATUS and bool(run.get("valid"))
    ]


def _cohort_key(run: Mapping[str, Any]) -> str:
    """The basis a run was recorded on.

    Aggregation refuses to pool runs that comparison would also refuse. A ten
    second observation and a five minute one have different frame counts by
    construction; their median is not a measurement of anything.

    The basis is exactly the fields ``compare_runs`` checks: configuration,
    window title/geometry and operating system. The window's handle, pid and
    bounds are identity, not measurement basis — a target that was re-launched
    (new pid) or nudged across the screen would otherwise be excluded from an
    aggregate that comparison still calls compatible. A window that was
    RESIZED still splits the cohort, because width/height are in the basis.
    """
    environment = _mapping(run.get("environment"))
    platform = _mapping(environment.get("platform"))
    config = _mapping(run.get("configuration"))
    # Operator notes are context, not a measurement basis: comparison treats a
    # notes difference as a warning, so the cohort must not split on it.
    basis = {
        key: config.get(key)
        for key in (
            "scenario_id",
            "version",
            "target_title",
            "duration_seconds",
            "frame_interval_seconds",
        )
    }
    return jsonable(
        {
            "configuration": basis,
            # The same window basis comparison uses (_window_of), so the two
            # surfaces cannot disagree about what "the same basis" means.
            "window": _window_of(run),
            "system": platform.get("system"),
        }
    )


def aggregate_runs(
    runs: Sequence[Mapping[str, Any]], scenario: BenchmarkScenario
) -> dict[str, Any]:
    """Aggregate stored runs of ONE scenario, counting every attempt.

    Only runs recorded on the same basis (configuration, target geometry and
    operating system) enter a metric's sample: the newest eligible run defines
    the cohort. Invalid, failed, aborted and differently-configured runs never
    enter a sample; their count and reasons are still reported. The denominator
    policy is stated in the payload so a success rate can be recomputed rather
    than trusted.
    """
    ordered = sorted(runs, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    eligible = eligible_runs(ordered)
    # The newest ELIGIBLE run defines the cohort; a newer failed run cannot drag
    # the aggregate onto a configuration that produced no measurements.
    reference = eligible[0] if eligible else (ordered[0] if ordered else None)
    cohort_key = _cohort_key(reference) if reference is not None else ""
    cohort = [run for run in eligible if _cohort_key(run) == cohort_key]
    cohort_ids = {str(run.get("id")) for run in cohort}
    excluded: list[dict[str, Any]] = []
    for run in ordered:
        if str(run.get("id")) in cohort_ids:
            continue
        # An eligible run excluded here is excluded for its BASIS, not its data.
        # Saying so is what keeps a smaller sample from looking like a failure.
        by_configuration = run in eligible
        excluded.append(
            {
                "run_id": run.get("id"),
                "status": run.get("status"),
                "valid": bool(run.get("valid")),
                "excluded_by": "configuration" if by_configuration else "status",
                "reason": (
                    "Recorded on a different configuration, target geometry or operating "
                    "system; it is not pooled with this cohort."
                    if by_configuration
                    else (run.get("reason") or "No reason recorded.")
                ),
            }
        )
    stats: dict[str, Any] = {}
    for key in scenario.metrics:
        spec = metric_spec(key)
        values, reason = _numeric_values(cohort, key)
        summary = _summary(values)
        summary.update(
            {
                "key": key,
                "label": spec.label,
                "unit": spec.unit,
                "kind": spec.kind,
                "available": bool(values),
                "reason": ""
                if values
                else (reason or "No eligible run in this cohort measured this metric."),
            }
        )
        stats[key] = summary

    counts = {status: 0 for status in RUN_STATUSES}
    for run in ordered:
        status = str(run.get("status") or "invalid")
        counts[status if status in counts else "invalid"] += 1

    configuration = _mapping((reference or {}).get("configuration"))
    environment = _mapping((reference or {}).get("environment"))
    window = _mapping(environment.get("window"))
    platform = _mapping(environment.get("platform"))
    return {
        "version": BENCHMARK_VERSION,
        "scenario": {
            "scenario_id": scenario.scenario_id,
            "version": scenario.version,
            "name": scenario.name,
            "evaluation": scenario.evaluation,
        },
        "attempts": len(ordered),
        "eligible": len(eligible),
        "status_counts": counts,
        "excluded": excluded,
        "excluded_count": len(excluded),
        "configuration_excluded_count": sum(
            1 for item in excluded if item["excluded_by"] == "configuration"
        ),
        "mixed_configurations": bool(eligible) and len(cohort) < len(eligible),
        "cohort": {
            "configuration": configuration,
            "window": window,
            "platform_system": platform.get("system"),
            "newest_run_id": (reference or {}).get("id"),
            "run_count": len(cohort),
        },
        "sample_size": len(cohort),
        "variability": (
            "Only one eligible run in this configuration: no variability can be shown."
            if len(cohort) == 1
            else (
                "No eligible runs in this configuration: nothing can be aggregated."
                if not cohort
                else "Standard deviation uses the sample formula over eligible runs in "
                "the same configuration."
            )
        ),
        "denominator_policy": (
            "A run enters a metric's sample only when it is COMPLETED, valid and recorded "
            "on the cohort's configuration, target geometry and operating system. Failed, "
            "aborted, invalid and differently-configured attempts are counted in "
            "status_counts and listed in excluded, never folded into a metric as zero."
        ),
        "objective_verified": any(
            bool(_mapping(run.get("objective")).get("decided")) for run in cohort
        ),
        "metrics": stats,
        "objective_note": (
            "These runs configured an objective: `objective_verified` is true when at "
            "least one eligible run recorded a decided verdict, and `objective_success` is "
            "the achieved rate over decided verdicts. An undecided run is unavailable, "
            "never zero."
            if scenario.objective_required
            else (
                "No objective verifier is attached to these runs: this scenario configures "
                "no objective for a benchmark session, so these are observation metrics and "
                "a completed run is data completeness, not a won game."
            )
        ),
    }


# ── Fair comparison ─────────────────────────────────────────────────────────


def _config_of(run: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(run.get("configuration"))


def _window_of(run: Mapping[str, Any]) -> dict[str, Any]:
    """The window basis: title and geometry, never identity fields.

    A handle, pid or screen position identifies WHICH window was observed; it
    does not change what capture costs. Including them here made a side of two
    runs against a re-launched target look like a mixed selection while the
    per-field check below said the geometry was identical.
    """
    environment = _mapping(run.get("environment"))
    window = _mapping(environment.get("window"))
    return {key: window.get(key) for key in ("title", "width", "height", "scale")}


def _side(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    first = runs[0]
    scenario = _mapping(first.get("scenario"))
    config = _config_of(first)
    eligible = eligible_runs(runs)
    metrics: dict[str, Any] = {}
    for key in scope_metric_keys(runs):
        spec = metric_spec(key)
        values, reason = _numeric_values(eligible, key)
        summary = _summary(values)
        summary.update(
            {
                "key": key,
                "label": spec.label,
                "unit": spec.unit,
                "kind": spec.kind,
                "available": bool(values),
                "reason": ""
                if values
                else (reason or "No eligible run on this side measured this metric."),
                "values": [round(value, ROUND_TO) for value in values],
            }
        )
        metrics[key] = summary
    return {
        "run_ids": [str(run.get("id")) for run in runs],
        "n": len(runs),
        "eligible": len(eligible),
        "statuses": {
            status: sum(1 for run in runs if run.get("status") == status)
            for status in RUN_STATUSES
        },
        "scenario": {
            "scenario_id": scenario.get("scenario_id"),
            "version": scenario.get("version"),
            "name": scenario.get("name"),
            "evaluation": scenario.get("evaluation"),
        },
        "configuration": config,
        "target": _mapping(first.get("target")),
        "environment": _mapping(first.get("environment")),
        "metrics": metrics,
        "evidence": {
            "timeline_endpoints": [
                str(_mapping(run.get("evidence")).get("timeline_endpoint") or "")
                for run in runs
            ],
            "session_ids": [str(run.get("session_id") or "") for run in runs],
        },
        "valid": len(eligible) == len(runs),
    }


def scope_metric_keys(runs: Sequence[Mapping[str, Any]]) -> list[str]:
    """The metric keys every compared run declares, in catalog order.

    A key with no catalog definition is skipped rather than raising: a stored
    record from a newer build (or a hand-edited one) must not break a
    comparison that otherwise has every definition it needs.
    """
    if not runs:
        return []
    scenario = _mapping(runs[0].get("scenario"))
    keys = scenario.get("metrics")
    if not isinstance(keys, (list, tuple)):
        return []
    return [str(key) for key in keys if str(key) in _METRICS_BY_KEY]


def _same(left: Any, right: Any) -> bool:
    return bool(left == right)


def _compatibility(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Blocking reasons and warnings for comparing two persisted run sets."""
    reasons: list[str] = []
    warnings: list[str] = []
    left_first, right_first = left[0], right[0]
    left_scenario = _mapping(left_first.get("scenario"))
    right_scenario = _mapping(right_first.get("scenario"))

    for side_name, runs in (("left", left), ("right", right)):
        if not all(run.get("status") == ELIGIBLE_STATUS and bool(run.get("valid")) for run in runs):
            bad = next(
                run
                for run in runs
                if run.get("status") != ELIGIBLE_STATUS or not bool(run.get("valid"))
            )
            reasons.append(
                f"The {side_name} run {bad.get('id')} is {bad.get('status')} and "
                f"{'valid' if bad.get('valid') else 'not valid'}; only completed valid "
                "runs are comparable."
            )
        if len({jsonable(_config_of(run)) for run in runs}) > 1:
            reasons.append(
                f"The {side_name} selection mixes different configurations; compare runs "
                "with the same budget and observation interval."
            )
        # A side is one measurement basis. Checking only the first run of each
        # side let a selection mix two window sizes (or two operating systems)
        # and still report deltas as if it were one basis.
        if len({jsonable(_window_of(run)) for run in runs}) > 1:
            reasons.append(
                f"The {side_name} selection mixes different target geometry; capture "
                "cost is not comparable across it."
            )
        if len(
            {
                str(_mapping(_mapping(run.get("environment")).get("platform")).get("system") or "")
                for run in runs
            }
        ) > 1:
            reasons.append(
                f"The {side_name} selection mixes different operating systems; capture "
                "backends are not comparable."
            )

    if not _same(left_scenario.get("scenario_id"), right_scenario.get("scenario_id")):
        reasons.append(
            "Different scenarios are not comparable: "
            f"{left_scenario.get('scenario_id')!r} vs {right_scenario.get('scenario_id')!r}."
        )
    if not _same(left_scenario.get("version"), right_scenario.get("version")):
        reasons.append(
            "Different scenario versions are not comparable: "
            f"{left_scenario.get('version')!r} vs {right_scenario.get('version')!r}."
        )

    left_config, right_config = _config_of(left_first), _config_of(right_first)
    for field_name, label in (
        ("duration_seconds", "execution budget"),
        ("frame_interval_seconds", "observation interval"),
    ):
        if not _same(left_config.get(field_name), right_config.get(field_name)):
            reasons.append(
                f"The runs used a different {label} "
                f"({left_config.get(field_name)!r} vs {right_config.get(field_name)!r}); "
                "their measurements are not on the same basis."
            )

    left_window, right_window = _window_of(left_first), _window_of(right_first)
    if not _same(left_window.get("title"), right_window.get("title")):
        reasons.append(
            "The runs observed different windows "
            f"({left_window.get('title')!r} vs {right_window.get('title')!r}); capture cost "
            "is not comparable across targets."
        )
    for field_name in ("width", "height", "scale"):
        left_value, right_value = left_window.get(field_name), right_window.get(field_name)
        if left_value is None or right_value is None:
            if (left_value is None) != (right_value is None):
                reasons.append(
                    f"One run did not record the window {field_name}, so geometry cannot "
                    "be confirmed equal."
                )
            continue
        if not _same(left_value, right_value):
            reasons.append(
                f"The window geometry differs ({field_name}: {left_value!r} vs "
                f"{right_value!r}); capture cost is not comparable."
            )

    left_platform = _mapping(_mapping(left_first.get("environment")).get("platform"))
    right_platform = _mapping(_mapping(right_first.get("environment")).get("platform"))
    if left_platform.get("system") and right_platform.get("system"):
        if not _same(left_platform.get("system"), right_platform.get("system")):
            reasons.append(
                "The runs were recorded on different operating systems; capture backends "
                "are not comparable."
            )

    if left_config.get("notes") != right_config.get("notes"):
        warnings.append("The runs carry different operator notes; the measurements are unaffected.")
    if any(run.get("interrupted") for run in list(left) + list(right)):
        warnings.append(
            "At least one run is marked interrupted; its session evidence may be incomplete."
        )
    return reasons, warnings


def jsonable(value: Any) -> str:
    """A stable key for a small JSON-ish mapping (dataclasses are already dicts)."""
    import json

    return json.dumps(value, sort_keys=True, default=str)


def _unique_runs(runs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One entry per run id, in the order given.

    A selection that names the same run twice is ONE sample. Counting it twice
    reported ``sample: 2`` and a standard deviation of ``0.0`` from a single
    measurement — variability that does not exist.
    """
    seen: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for run in runs:
        run_id = str(run.get("id"))
        if run_id in seen:
            continue
        seen.add(run_id)
        unique.append(run)
    return unique


def compare_runs(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare two selections of persisted runs under explicit fairness rules."""
    left = _unique_runs(left)
    right = _unique_runs(right)
    if not left or not right:
        raise BenchmarkValidationError(["Both sides of a comparison need at least one run."])
    if {str(run.get("id")) for run in left} & {str(run.get("id")) for run in right}:
        raise BenchmarkValidationError(
            ["A run cannot be compared with itself or appear on both sides."]
        )

    reasons, warnings = _compatibility(left, right)
    left_side, right_side = _side(left), _side(right)
    compatible = not reasons

    rows: list[dict[str, Any]] = []
    for key in scope_metric_keys(left):
        left_metric = dict(left_side["metrics"].get(key) or {})
        right_metric = dict(right_side["metrics"].get(key) or {})
        left_value = left_metric.get("value")
        right_value = right_metric.get("value")
        delta = None
        delta_reason = ""
        if not compatible:
            delta_reason = "No delta: the runs are not compatible."
        elif left_value is None or right_value is None:
            delta_reason = (
                str(left_metric.get("reason") or right_metric.get("reason"))
                or "At least one side did not measure this metric."
            )
        else:
            delta = round(float(right_value) - float(left_value), ROUND_TO)
        rows.append(
            {
                "key": key,
                "label": left_metric.get("label") or key,
                "unit": left_metric.get("unit") or "",
                "kind": left_metric.get("kind") or "measured",
                "left": left_metric,
                "right": right_metric,
                "delta": delta,
                "delta_reason": delta_reason,
            }
        )

    sample = {"left": left_side["n"], "right": right_side["n"]}
    single = sample["left"] < 2 or sample["right"] < 2
    verdict_reason = (
        "NF-04 invokes no model or policy, so no model can be declared superior. "
        "These figures describe observation and capture behavior."
    )
    if single:
        verdict_reason += (
            " At least one side has a single run, which shows no variability; a higher "
            "or lower value is a measurement, not an established difference."
        )
    return {
        "version": BENCHMARK_VERSION,
        "compatible": compatible,
        "reasons": reasons,
        "warnings": warnings,
        "left": left_side,
        "right": right_side,
        "metrics": rows,
        "sample": sample,
        "variability": {
            "left_stdev_available": any(
                metric.get("stdev") is not None for metric in left_side["metrics"].values()
            ),
            "right_stdev_available": any(
                metric.get("stdev") is not None for metric in right_side["metrics"].values()
            ),
            "note": (
                "Standard deviation is computed per metric when a side has at least two "
                "eligible runs. It is None, never 0.0, for a single sample."
            ),
        },
        "verdict": {
            "established": False,
            "label": "not_established",
            "reason": verdict_reason,
        },
        "objective_verified": False,
        "note": (
            "Only runs with the same scenario, version, budget, interval, target geometry "
            "and operating system produce deltas. Everything else is refused above."
        ),
    }


def environment_snapshot() -> dict[str, Any]:
    """Host metadata recorded with every run, without claiming usage."""
    uname = platform.uname()
    return {
        "platform": {
            "system": uname.system,
            "release": uname.release,
            "machine": uname.machine,
            "processor": uname.processor,
            "python": platform.python_version(),
            "cpu_count": _cpu_count(),
        },
        "usage": {
            "available": False,
            "reason": (
                "No per-run CPU, memory, GPU or NPU measurement is attached. The host "
                "metadata above is context, not usage, and is never reported as a metric."
            ),
        },
    }


def _cpu_count() -> int | None:
    import os

    count = os.cpu_count()
    return int(count) if count else None


def run_limitations(scenario: BenchmarkScenario) -> list[str]:
    """Limitations that apply to every run of this scenario."""
    return [
        "Only COMPLETED and valid runs recorded on the same configuration, target "
        "geometry and operating system enter an aggregate; every attempt is still counted.",
        (
            "An objective verdict is claimed only when the session's verifier decided one; "
            "an undecided run is inconclusive, never success."
            if scenario.objective_required
            else "Objective achievement is never claimed for this scenario: it configures "
            "no objective."
        ),
        *scenario.limitations,
    ]
