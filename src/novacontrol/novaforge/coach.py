"""NovaForge NF-06 — the Forge Coach: evidence-backed post-session analysis.

The Coach reads what NF-01…NF-05 already recorded and answers three questions
without inventing anything:

* **What happened?** Every finding names the recorded events, metrics and
  records it was derived from. A finding is an *observed fact* (the record
  states it), a *derived metric* (arithmetic over recorded values), a
  *hypothesis* (a plausible explanation with no confirming record) or
  *unknown* (the evidence is insufficient to say).
* **What is the earliest failure the evidence supports?** The first recorded
  failure signature in sequence order, with the later symptoms that follow it,
  the counter-evidence, and an explicit statement that temporal order is not
  causation.
* **What is worth trying next?** A prioritized recommendation that names the
  metric to observe, the success criterion, the risk and the preconditions —
  and that can be turned into a controlled experiment by NF-06's planner.

The rules this module follows, each of which exists because the alternative
silently lies:

* **Analysis never writes evidence.** Findings are recomputed from the recorded
  session/evaluation/benchmark records on every read; nothing is cached, so a
  stale diagnosis cannot outlive the evidence it was derived from. The only
  persisted state here is a recommendation's STATUS (proposed/approved/…),
  which is not evidence.
* **A missing figure is ``None``, never zero.** An unmeasured latency is not a
  0 ms latency, and an unsupported capability is not a failure.
* **No verifier exists, so no success is claimed.** These sessions observe; the
  Coach never says a goal was achieved, a game was played, or a score improved.
* **Correlation is not causation.** A finding that reaches beyond the record
  says ``hypothesis`` and lists its alternative explanations.
* **Insufficient evidence is an answer.** A session with no recorded events,
  frames or telemetry returns ``insufficient_evidence`` with what would help,
  rather than a diagnosis built from nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.persistence.json_store import JsonStateStore

from .benchmark import metric_spec
from .models import utc_now
from .timeline import TimelineEvent, summary_text

COACH_VERSION = 1
ANALYSIS_VERSION = 1

#: Where a recommendation's status lives. Findings are NOT persisted: they are
#: derived from evidence that already has an owner, and a cached diagnosis would
#: be a second source of truth about the same record.
RECOMMENDATIONS_NAMESPACE = "novaforge_coach_recommendations"
MAX_RECOMMENDATIONS_KEPT = 200
MAX_RECOMMENDATION_HISTORY = 20
MAX_FINDINGS = 24
MAX_RECOMMENDATIONS = 12
MAX_LATER_SYMPTOMS = 8

#: The finding vocabulary. ``observed`` is stated by the record, ``derived`` is
#: arithmetic over recorded values, ``hypothesis`` is a plausible explanation
#: without a confirming record, and ``unknown`` is the honest name for "the
#: evidence cannot take this further".
FINDING_KINDS = ("observed", "derived", "hypothesis", "unknown")

#: How much evidence stands behind a finding. The basis is stated per finding.
EVIDENCE_STRENGTHS = ("strong", "moderate", "weak", "insufficient")

#: Severity bands, with the criteria that assign them:
#: critical — evidence integrity is lost or nothing usable was recorded;
#: high     — the observation loop failed, ended unexpectedly, or lost its target;
#: medium   — a measurable degradation that biases what the run measured;
#: low      — a configuration choice worth revisiting;
#: info     — a structural property of this build, not a fault.
SEVERITIES = ("critical", "high", "medium", "low", "info")

RECOMMENDATION_STATUSES = (
    "proposed",
    "approved",
    "rejected",
    "running",
    "completed",
    "failed",
    "inconclusive",
)

_SEVERITY_WEIGHT = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_STRENGTH_WEIGHT = {"strong": 0, "moderate": 1, "weak": 2, "insufficient": 3}

#: Failure signatures in the order they are recognised. The category, severity
#: and kind of the finding a signature produces are declared here.
_FAILURE_SIGNATURES: tuple[tuple[str, str, str, str], ...] = (
    ("capture.failed", "capture_failure", "high", "observed"),
    ("target.lost", "target_loss", "high", "observed"),
    ("target.changed", "target_changed", "high", "observed"),
    ("target.unreadable", "target_unreadable", "medium", "observed"),
    ("session.failed", "unexpected_exception", "critical", "observed"),
    ("timeline.persistence_failed", "persistence_failure", "critical", "observed"),
    ("error.recorded", "error_recorded", "medium", "observed"),
    ("frame.rejected", "stale_observation", "medium", "observed"),
)

_WHATS_NEXT = (
    "Analyse a session that recorded frames, telemetry readings or timeline events.",
)

#: Recorded "errors" that are configured bounds firing, not faults. NF-01 writes
#: the time-limit notice to the error list on purpose (so a bounded stop is never
#: mistaken for success), and NF-03 records it as ``error.recorded``. Retelling it
#: as a fault would invent a bottleneck out of the intended behaviour, so the
#: Coach filters exactly these locations out of its failure analysis.
_BENIGN_ERROR_WHERE = frozenset({"limits"})


class CoachError(ValueError):
    """An analysis request that cannot be answered."""


class CoachNotFoundError(CoachError):
    """An unknown session, evaluation or recommendation id."""


#: The only recommendation id shape this store accepts on first use.
_RECOMMENDATION_ID = re.compile(r"^rec-[0-9a-f]{12}$")


def _digest(*parts: Any) -> str:
    """A stable, short id derived from the inputs — never random.

    A recommendation id must survive a page reload and a restart, because the
    operator's approval is stored against it; a uuid would break that link.
    """
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _target_identity(value: Any) -> dict[str, Any]:
    """The pinned target window as a plain mapping, or {} when none was recorded.

    ``SessionRecord.target`` is a ``TargetWindow`` dataclass, not a mapping, so
    treating it as one silently emptied the subject's target and the panel said
    "not recorded" about an identity the record does hold.
    """
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return {}
    try:
        payload = to_dict()
    except Exception:  # noqa: BLE001 - a subject read must not fail the analysis
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _event_ref(event: Any) -> dict[str, Any]:
    """A bounded reference to a timeline event; never the payload itself."""
    return {
        "kind": "event",
        "event_id": str(getattr(event, "event_id", "")),
        "seq": int(getattr(event, "seq", 0)),
        "type": str(getattr(event, "type", "")),
        "summary": summary_text(event),
    }


def _refs(*items: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in items if item]


def _strength_for(*, signals: int, recorded: bool) -> tuple[str, str]:
    """The evidence-strength vocabulary, applied by a documented rule.

    ``strong`` requires either three or more independent recorded signals, or a
    directly recorded cause that telemetry agrees with. A single recorded event
    with no corroborating measurement is ``moderate``; a single correlated
    signal is ``weak``; nothing measured is ``insufficient``.
    """
    if signals <= 0:
        return "insufficient", "No recorded signal supports this finding."
    if signals >= 3:
        return "strong", f"{signals} recorded signals agree."
    if signals == 2:
        return "strong", "A recorded cause and a measured figure agree."
    if recorded:
        return "moderate", "One recorded event states it directly."
    return "weak", "One correlated signal; it alone does not establish the cause."


@dataclass(frozen=True, slots=True)
class Finding:
    """One evidence-backed statement about a recorded session or evaluation."""

    finding_id: str
    category: str
    title: str
    severity: str
    severity_basis: str
    kind: str
    confidence: str
    confidence_basis: str
    explanation: str
    evidence: tuple[dict[str, Any], ...]
    alternatives: tuple[str, ...]
    limitations: tuple[str, ...]
    recommendation_id: str = ""

    @property
    def direct(self) -> bool:
        return self.kind == "observed"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.severity not in SEVERITIES:
            problems.append(f"Unknown severity {self.severity!r}.")
        if self.kind not in FINDING_KINDS:
            problems.append(f"Unknown finding kind {self.kind!r}.")
        if self.confidence not in EVIDENCE_STRENGTHS:
            problems.append(f"Unknown evidence strength {self.confidence!r}.")
        if not self.title.strip() or not self.explanation.strip():
            problems.append("A finding needs a title and an explanation.")
        if not self.severity_basis.strip() or not self.confidence_basis.strip():
            problems.append("A finding needs a stated severity and confidence basis.")
        if self.kind == "observed" and not self.evidence:
            problems.append("An observed finding needs at least one evidence reference.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "title": self.title,
            "severity": self.severity,
            "severity_basis": self.severity_basis,
            "kind": self.kind,
            "direct": self.direct,
            "confidence": self.confidence,
            "confidence_basis": self.confidence_basis,
            "explanation": self.explanation,
            "evidence": [dict(item) for item in self.evidence],
            "alternatives": list(self.alternatives),
            "limitations": list(self.limitations),
            "recommendation_id": self.recommendation_id,
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A testable next step derived from one finding.

    Every recommendation names the metric that would show an effect and the
    criterion that would count as one; none of them claims a score improvement,
    because no verifier exists in this build.
    """

    recommendation_id: str
    finding_id: str
    category: str
    priority: int
    target_issue: str
    action: str
    expected_effect: str
    metric: str
    metric_label: str
    success_criterion: str
    risk: str
    effort: str
    preconditions: tuple[str, ...]
    requires_approval: bool
    experiment: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    status: str = "proposed"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.status not in RECOMMENDATION_STATUSES:
            problems.append(f"Unknown recommendation status {self.status!r}.")
        if not self.action.strip() or not self.target_issue.strip():
            problems.append("A recommendation needs a target issue and an action.")
        if not self.metric.strip() or not self.success_criterion.strip():
            problems.append("A recommendation needs a metric and a success criterion.")
        if not self.evidence:
            problems.append("A recommendation must be traceable to evidence.")
        if self.priority < 1:
            problems.append("Priority is a 1-based rank.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "finding_id": self.finding_id,
            "category": self.category,
            "priority": self.priority,
            "target_issue": self.target_issue,
            "action": self.action,
            "expected_effect": self.expected_effect,
            "metric": self.metric,
            "metric_label": self.metric_label,
            "success_criterion": self.success_criterion,
            "risk": self.risk,
            "effort": self.effort,
            "preconditions": list(self.preconditions),
            "requires_approval": self.requires_approval,
            "experiment": dict(self.experiment),
            "evidence": [dict(item) for item in self.evidence],
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class _Rule:
    """The declared action behind one finding category.

    The table exists so a recommendation is never invented per request: the
    category decides the action, the metric and the experiment shape.
    """

    category: str
    action: str
    expected_effect: str
    metric: str
    success_criterion: str
    risk: str
    effort: str
    experiment_kind: str = ""
    variable: str = "none"
    #: The scenario that measures ``metric``, when the subject's scenario does
    #: not. An empty value means "the scenario the finding came from".
    scenario: str = ""
    preconditions: tuple[str, ...] = (
        "A validated, already-open target window.",
        "Operator approval before anything runs.",
    )


#: One row per finding category. ``metric`` names a benchmark metric key that
#: exists in the NF-04 catalog; the planner refuses a recommendation whose
#: metric this build cannot measure.
RECOMMENDATION_RULES: tuple[_Rule, ...] = (
    _Rule(
        category="capture_failure",
        action=(
            "Re-run the same scenario and configuration on the same target to establish "
            "whether the capture failure repeats, before changing anything."
        ),
        expected_effect=(
            "A second observation either reproduces the failure (a repeatable defect) or "
            "shows it as transient; both answers are measurable."
        ),
        metric="capture_failures",
        success_criterion="capture_failures is 0 in every repeat of the same configuration.",
        risk="None beyond the observation itself: no input is sent and no game is launched.",
        effort="One bounded run per repeat at the same budget and interval.",
        experiment_kind="repeat",
        scenario="capture-latency",
    ),
    _Rule(
        category="stale_observation",
        action=(
            "Repeat the run at the same configuration and compare stale_frames and "
            "frame_yield; only if the stale count repeats, adjust the observation "
            "interval in a separate configuration-change experiment."
        ),
        expected_effect=(
            "Separates a one-off stale capture from a cadence that is too fast for this "
            "host and target."
        ),
        metric="stale_frames",
        success_criterion="stale_frames is 0 (or identical) across repeats of one configuration.",
        risk="A faster interval can raise capture load; the interval stays within the scenario's bounds.",
        effort="One bounded run per repeat; a follow-up configuration change is a second experiment.",
        experiment_kind="repeat",
        scenario="capture-latency",
    ),
    _Rule(
        category="observation_discontinuity",
        action=(
            "Repeat the configuration to measure whether the longest frame gap is "
            "reproducible, and inspect the timeline around the gap before tuning anything."
        ),
        expected_effect=(
            "Distinguishes a systematic stall from a single scheduling hiccup, which is "
            "what decides whether a timeout change is justified."
        ),
        metric="max_gap_ms",
        success_criterion="max_gap_ms stays within the scenario's own gap threshold across repeats.",
        risk="None: this only observes an already-open window.",
        effort="One bounded run per repeat.",
        experiment_kind="repeat",
        scenario="observation-continuity",
    ),
    _Rule(
        category="capture_latency",
        action=(
            "Repeat the run to establish whether the capture latency distribution is "
            "stable, and only then consider a slower observation interval as a separate "
            "single-variable experiment."
        ),
        expected_effect=(
            "A measured latency distribution is the only basis on which a cadence change "
            "can be justified; the delta will show it or not."
        ),
        metric="latency_median_ms",
        success_criterion="latency_median_ms is stable across repeats of one configuration.",
        risk="A slower cadence can miss change in the observed window; it is bounded by the scenario.",
        effort="One bounded run per repeat plus one configuration-change run per arm.",
        experiment_kind="repeat",
        scenario="capture-latency",
    ),
    _Rule(
        category="target_loss",
        action=(
            "Repeat the observation on the same target and record whether the target loss "
            "recurs before any configuration change."
        ),
        expected_effect=(
            "A repeated target loss points at the environment (the window closing), a "
            "one-off at a manual interruption; the run status shows which."
        ),
        metric="target_losses",
        success_criterion="target_losses is 0 in every repeat of one configuration.",
        risk="None beyond observing an already-open window.",
        effort="One bounded run per repeat.",
        experiment_kind="repeat",
        scenario="observation-continuity",
    ),
    _Rule(
        category="target_changed",
        action=(
            "Repeat the observation with the target identity pinned and confirm the "
            "window handle and pid are stable for the whole run."
        ),
        expected_effect=(
            "Establishes whether the target identity is stable under observation or "
            "changes between runs (which would make cross-run comparison meaningless)."
        ),
        metric="target_changes",
        success_criterion="target_changes is 0 across repeats.",
        risk="None beyond observing an already-open window.",
        effort="One bounded run per repeat.",
        experiment_kind="repeat",
        scenario="observation-continuity",
    ),
    _Rule(
        category="persistence_failure",
        action=(
            "Fix the evidence store before any further experiment: an evaluation whose "
            "snapshot cannot be read back cannot support an improvement claim."
        ),
        expected_effect=(
            "The next run's persistence state reads verified rather than failed; that is "
            "checkable without running a comparison."
        ),
        metric="timeline_events",
        success_criterion="The next evaluation reports persistence.state = verified.",
        risk="None: this repairs recording, it does not change what is measured.",
        effort="Operator action on the state store's path and permissions.",
    ),
    _Rule(
        category="unexpected_exception",
        action=(
            "Re-run the same configuration after locating the recorded error, because "
            "an observation loop that ends FAILED records no usable distribution."
        ),
        expected_effect=(
            "A clean run either reproduces the failure (with the recording location now "
            "known) or shows the exception was environmental."
        ),
        metric="frames",
        success_criterion="The repeat reaches COMPLETED with at least the scenario's minimum frames.",
        risk="None beyond the observation itself.",
        effort="One bounded run.",
        experiment_kind="repeat",
    ),
    _Rule(
        category="error_recorded",
        action=(
            "Repeat the configuration and check whether the same recording location "
            "reports an error again; treat a repeated error at the same location as the "
            "subject of the next experiment."
        ),
        expected_effect=(
            "Turns one recorded error into either a reproducible defect or a transient "
            "condition, which changes what may be recommended next."
        ),
        metric="frames",
        success_criterion="The repeat completes with no error recorded at the same location.",
        risk="None beyond the observation itself.",
        effort="One bounded run.",
        experiment_kind="repeat",
    ),
    _Rule(
        category="time_limit_reached",
        action=(
            "Compare the recorded frame count and yield against the scenario's own minimum "
            "before changing the budget; a longer budget is a configuration change and "
            "must be declared as the experiment's single variable."
        ),
        expected_effect=(
            "Shows whether the budget was the binding constraint or the run recorded the "
            "expected amount of evidence in the time it had."
        ),
        metric="frames",
        success_criterion="frames is at or above the scenario's minimum in every repeat.",
        risk="A longer budget occupies the one session slot for longer.",
        effort="One bounded run per repeat, plus one longer run per arm if the budget changes.",
        experiment_kind="repeat",
    ),
    _Rule(
        category="guardian_cancellation",
        action=(
            "Repeat the observation only after confirming who ended the previous session "
            "and why; a safety intervention is an operator decision, not a defect to tune "
            "around."
        ),
        expected_effect=(
            "A repeat under the same conditions either shows no further intervention or "
            "repeats the condition that required one."
        ),
        # ``interventions`` is in the NF-04 catalog but NO scenario collects it,
        # so it cannot decide an experiment. Fresh frames are the measurable
        # effect of a run that was cut short by an operator stop.
        metric="frames",
        success_criterion=(
            "The repeat reaches the scenario's minimum fresh frames instead of being "
            "stopped early."
        ),
        risk="None: no input is sent by any runnable scenario.",
        effort="One bounded run.",
        experiment_kind="repeat",
    ),
    _Rule(
        category="insufficient_frames",
        action=(
            "Repeat the run with the scenario's own default budget rather than a shortened "
            "one: the scenario declares a minimum number of fresh frames and a budget below "
            "it cannot satisfy it."
        ),
        expected_effect=(
            "A run at the declared budget reaches the minimum frame count, which is what "
            "makes its measurements eligible at all."
        ),
        metric="frame_yield",
        success_criterion="frames is at or above the scenario minimum and the run is eligible.",
        risk="A longer budget occupies the one session slot for longer.",
        effort="One bounded run at the scenario default (no configuration change needed).",
        experiment_kind="repeat",
    ),
    _Rule(
        category="incompatible_baseline",
        action=(
            "Restate the intended comparison as an explicit experiment with ONE declared "
            "variable and repeat both arms, so the incompatibility is the subject of the "
            "experiment instead of an obstacle to it."
        ),
        expected_effect=(
            "Each arm is measured on the declared variable, so the result states the "
            "basis difference between them instead of pooling two incomparable runs. "
            "NF-04 will NOT certify a delta across a budget or interval change — the "
            "figures are reported per arm, and the verdict says so."
        ),
        metric="frame_yield",
        success_criterion=(
            "Both arms complete and each arm's own recorded value is reported; the "
            "declared basis difference is stated rather than pooled into a delta."
        ),
        risk="A different basis cannot support a pooled delta; the result will say so.",
        effort="One bounded run per arm.",
        experiment_kind="configuration_change",
        variable="duration_seconds",
    ),
    _Rule(
        category="single_sample",
        action=(
            "Repeat the same configuration enough times to show variability before treating "
            "any difference as an effect."
        ),
        expected_effect=(
            "Two or more eligible runs per side turn a single measurement into a "
            "distribution and allow a standard deviation to be reported."
        ),
        metric="frame_yield",
        success_criterion="At least two eligible runs per side with a measurable stdev.",
        risk="None: repeats of a configuration are the cheapest experiment available.",
        effort="One bounded run per repeat (2–3 repeats per arm).",
        experiment_kind="repeat",
    ),
)
#: ``objective_verifier_missing`` deliberately has NO rule: a structural
#: limitation is not a testable next step, and turning it into a recommendation
#: would offer the operator an "experiment" that cannot change anything.


class NovaForgeCoach:
    """The read-only analyst plus the recommendation-status store.

    The class holds no cached analysis: every ``analyse`` call recomputes from
    the records it is handed, so the answer always matches the evidence. The one
    thing it persists is a recommendation's lifecycle status, because an
    operator's approval is state that must survive a restart.
    """

    def __init__(
        self,
        *,
        manager: Any,
        benchmark: Any | None = None,
        evaluations: Any | None = None,
        store: JsonStateStore | None = None,
        publish: Any | None = None,
        max_recommendations: int = MAX_RECOMMENDATIONS_KEPT,
    ) -> None:
        self.manager = manager
        self.benchmark = benchmark if benchmark is not None else getattr(manager, "benchmark", None)
        self.evaluations = evaluations
        self.store = (
            store
            if store is not None
            else getattr(manager, "store", None)
        )
        self.publish = publish
        self.max_recommendations = max(1, int(max_recommendations))
        self._recommendations: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence (recommendation status only) ----------------------------

    def _load(self) -> None:
        payload: dict[str, Any] = {}
        if self.store is not None:
            try:
                payload = self.store.read(RECOMMENDATIONS_NAMESPACE)
            except Exception:  # noqa: BLE001 - an unreadable store has no state
                payload = {}
        stored = payload.get("recommendations")
        if not isinstance(stored, list):
            return
        for item in stored:
            if not isinstance(item, dict):
                continue
            recommendation_id = str(item.get("recommendation_id") or "")
            if not recommendation_id:
                continue
            status = str(item.get("status") or "proposed")
            if status not in RECOMMENDATION_STATUSES:
                # A hand-edited snapshot must not invent a lifecycle state.
                status = "proposed"
            record = dict(item)
            record["status"] = status
            self._recommendations[recommendation_id] = record
        self._trim()

    def _trim(self) -> None:
        if len(self._recommendations) <= self.max_recommendations:
            return
        ordered = sorted(
            self._recommendations.values(),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        keep = {str(item.get("recommendation_id")) for item in ordered[: self.max_recommendations]}
        for recommendation_id in list(self._recommendations):
            if recommendation_id not in keep:
                del self._recommendations[recommendation_id]

    def _persist(self, *, verify_id: str = "") -> dict[str, Any]:
        record = self._recommendations.get(verify_id) if verify_id else None
        state: dict[str, Any]
        if self.store is None:
            state = {
                "state": "not_persisted",
                "reason": (
                    "No state store is attached; recommendation statuses exist for this "
                    "process only."
                ),
            }
            if record is not None:
                record["persistence"] = state
            return state
        self._trim()
        ordered = sorted(
            self._recommendations.values(),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        try:
            self.store.write(
                RECOMMENDATIONS_NAMESPACE,
                {"version": COACH_VERSION, "recommendations": ordered},
            )
        except Exception as exc:  # noqa: BLE001 - recording cannot fail the work
            state = {"state": "failed", "reason": f"The store raised {type(exc).__name__}: {exc}"}
            if record is not None:
                record["persistence"] = state
            return state
        try:
            stored = self.store.read(RECOMMENDATIONS_NAMESPACE)
        except Exception as exc:  # noqa: BLE001 - verification must not raise either
            state = {
                "state": "failed",
                "reason": f"The store could not be read back: {type(exc).__name__}: {exc}",
            }
            if record is not None:
                record["persistence"] = state
            return state
        rows = stored.get("recommendations") if isinstance(stored, dict) else None
        present = isinstance(rows, list) and any(
            isinstance(item, dict) and str(item.get("recommendation_id")) == verify_id
            for item in rows
        ) if verify_id else True
        state = {
            "state": "verified" if present else "failed",
            "reason": ""
            if present
            else "The store accepted the write but the status is not readable back.",
        }
        if record is not None:
            record["persistence"] = state
        return state

    # -- read surfaces -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the Coach can and cannot do — its own honest capability card."""
        return {
            "version": COACH_VERSION,
            "name": "Forge Coach",
            "analysis": {
                "read_only": True,
                "sources": [
                    "NF-01 session record (state, target, frames, interventions)",
                    "NF-02 telemetry snapshot (bounded distributions)",
                    "NF-03 evidence timeline and its failure classification",
                    "NF-04 benchmark run (metrics, eligibility, environment)",
                    "NF-05 evaluation record and its readiness verdict",
                ],
                "finding_kinds": list(FINDING_KINDS),
                "severities": list(SEVERITIES),
                "evidence_strengths": list(EVIDENCE_STRENGTHS),
                "caches": False,
                "cache_note": (
                    "Findings are recomputed from the recorded evidence on every read; "
                    "nothing derived is cached, so a stale diagnosis cannot outlive its "
                    "evidence."
                ),
            },
            "recommendations": {
                "statuses": list(RECOMMENDATION_STATUSES),
                "retained": len(self._recommendations),
                "cap": self.max_recommendations,
                "note": (
                    "Only a recommendation's STATUS is persisted. Its text is derived "
                    "from evidence and regenerated on each analysis."
                ),
            },
            "experiments": self._experiment_status(),
            "limits": [
                "No objective verifier exists: no analysis and no experiment can claim a "
                "gameplay improvement, a score, or a completed objective.",
                "No action path exists: findings about actions, policies or in-game "
                "decisions cannot be produced from this build's evidence.",
                "Analysis reads recorded evidence; it cannot observe a live session.",
                "A session with no recorded events, frames or telemetry answers "
                "insufficient_evidence rather than diagnosing from nothing.",
            ],
            "note": (
                "Every finding names the events and metrics behind it, distinguishes an "
                "observed fact from a derived metric and a hypothesis, and states its "
                "alternatives and limitations."
            ),
        }

    def _experiment_status(self) -> dict[str, Any]:
        status = getattr(self.evaluations, "status", None)
        if callable(status):
            try:
                return dict(status())
            except Exception:  # noqa: BLE001 - a status read must not fail the card
                pass
        return {
            "available": False,
            "reason": "No experiment lab is attached to this Coach.",
        }

    def recommendations(self, *, limit: int = 50, status: str = "") -> dict[str, Any]:
        """Persisted recommendation statuses, newest first."""
        wanted = str(status or "").strip()
        if wanted and wanted not in RECOMMENDATION_STATUSES:
            raise CoachError([f"Unknown recommendation status {wanted!r}."])
        ordered = sorted(
            self._recommendations.values(),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        if wanted:
            ordered = [item for item in ordered if item.get("status") == wanted]
        limit = max(1, min(int(limit), 200))
        rows = [dict(item) for item in ordered[:limit]]
        return {
            "version": COACH_VERSION,
            "recommendations": rows,
            "count": len(rows),
            "total": len(ordered),
            "retention": {
                "cap": self.max_recommendations,
                "retained": len(self._recommendations),
                "note": (
                    "The newest statuses are kept. Recommendation text is regenerated by "
                    "an analysis, never stored."
                ),
            },
            "note": (
                "A status here is operator state (proposed/approved/rejected/…). It is not "
                "evidence and it changes no recorded result."
            ),
        }

    def set_recommendation_status(
        self, recommendation_id: str, status: str, *, note: str = ""
    ) -> dict[str, Any]:
        """Record an operator decision about one recommendation."""
        wanted = str(status or "").strip().lower()
        if wanted not in RECOMMENDATION_STATUSES:
            raise CoachError(
                [
                    f"Unknown recommendation status {status!r}; expected one of "
                    f"{', '.join(RECOMMENDATION_STATUSES)}."
                ]
            )
        key = str(recommendation_id or "").strip()
        record = self._recommendations.get(key)
        if record is None:
            # A recommendation id is DERIVED (``rec-`` + 12 hex characters of a
            # digest over the subject and the rule), never random, so the panel
            # can record a decision the first time it sees one. Only that shape
            # is accepted: an arbitrary string is not a recommendation, and a
            # well-formed id is still only a status row — never evidence.
            if not _RECOMMENDATION_ID.match(key):
                raise CoachNotFoundError(f"Unknown recommendation: {recommendation_id!r}")
            record = {
                "recommendation_id": key,
                "created_at": utc_now(),
                "history": [],
            }
            self._recommendations[key] = record
        now = utc_now()
        history = list(record.get("history") or [])
        history.append({"status": wanted, "at": now, "note": str(note or "")[:300]})
        record.update(
            {
                "status": wanted,
                "updated_at": now,
                "note": str(note or "")[:300],
                "history": history[-MAX_RECOMMENDATION_HISTORY:],
            }
        )
        state = self._persist(verify_id=str(record.get("recommendation_id")))
        payload = dict(record)
        payload["persistence"] = state
        return payload

    # -- analysis ------------------------------------------------------------

    def analyse(
        self, *, session_id: str = "", evaluation_id: str = "", limit: int = 100
    ) -> dict[str, Any]:
        """The full evidence-backed analysis for one recorded subject."""
        session = str(session_id or "").strip()
        evaluation: dict[str, Any] | None = None
        if evaluation_id:
            evaluation = self._evaluation(str(evaluation_id))
            if not session:
                session = str(evaluation.get("session_id") or "")
        if not session and evaluation is None:
            raise CoachError(
                ["An analysis needs a session_id or an evaluation_id."]
            )
        if session:
            record = self.manager.get(session)
            if record is None:
                raise CoachNotFoundError(f"Unknown session: {session!r}")
        else:  # pragma: no cover - evaluation without a session is handled below
            raise CoachError(["The selected evaluation has no recorded session to analyse."])
        if evaluation is None:
            evaluation = self._evaluation_for_session(session)
        return analyse_session(
            self.manager,
            record,
            evaluation=evaluation,
            benchmark=self._benchmark_for(evaluation),
            recommendation_status=self._status_index(),
            limit=limit,
        )

    def _status_index(self) -> dict[str, str]:
        return {
            str(item.get("recommendation_id")): str(item.get("status") or "proposed")
            for item in self._recommendations.values()
        }

    def _evaluation(self, evaluation_id: str) -> dict[str, Any]:
        lab = self.evaluations
        if lab is None:
            raise CoachError(["No evaluation lab is attached; pass session_id instead."])
        record = lab.get(evaluation_id)
        if record is None:
            raise CoachNotFoundError(f"Unknown evaluation: {evaluation_id!r}")
        return dict(record)

    def _evaluation_for_session(self, session_id: str) -> dict[str, Any] | None:
        lab = self.evaluations
        if lab is None:
            return None
        try:
            payload = lab.history(limit=100)
        except Exception:  # noqa: BLE001 - an unreadable history is not an analysis failure
            return None
        for row in payload.get("evaluations") or []:
            if str(row.get("session_id") or "") == session_id:
                full = lab.get(str(row.get("id") or ""))
                return dict(full) if full is not None else dict(row)
        return None

    def _benchmark_for(self, evaluation: Mapping[str, Any] | None) -> dict[str, Any] | None:
        lab = self.benchmark
        if lab is None or not evaluation:
            return None
        run_id = str(evaluation.get("benchmark_run_id") or "")
        if not run_id:
            return None
        try:
            run = lab.get(run_id)
        except Exception:  # noqa: BLE001 - a missing run is reported, not raised
            return None
        return dict(run) if run is not None else None


# ── the analysis itself ─────────────────────────────────────────────────


def _finding(
    *,
    subject: str,
    category: str,
    title: str,
    severity: str,
    severity_basis: str,
    kind: str,
    confidence: str,
    confidence_basis: str,
    explanation: str,
    evidence: Iterable[Mapping[str, Any]],
    alternatives: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> Finding:
    finding = Finding(
        finding_id=f"fnd-{_digest(subject, category)[:12]}",
        category=category,
        title=title,
        severity=severity,
        severity_basis=severity_basis,
        kind=kind,
        confidence=confidence,
        confidence_basis=confidence_basis,
        explanation=explanation,
        evidence=tuple(dict(item) for item in evidence),
        alternatives=tuple(str(item) for item in alternatives),
        limitations=tuple(str(item) for item in limitations),
    )
    problems = finding.validate()
    if problems:  # pragma: no cover - a programming error, not a data condition
        raise ValueError("; ".join(problems))
    return finding


def _telemetry_facts(telemetry: Mapping[str, Any]) -> dict[str, Any]:
    """The measured figures the analysis is allowed to use, or ``None``."""
    capture = _mapping(telemetry.get("capture"))
    session = _mapping(telemetry.get("session"))
    target = _mapping(telemetry.get("target"))
    loop = _mapping(telemetry.get("loop"))
    latency = _mapping(capture.get("latency_ms"))
    return {
        "frames": _number(capture.get("frames")),
        "stale_frames": _number(capture.get("stale_frames")),
        "capture_failures": _number(capture.get("capture_failures")),
        "frame_gaps": _number(capture.get("frame_gaps")),
        "max_gap_ms": _number(capture.get("max_gap_ms")),
        "gap_threshold_ms": _number(capture.get("gap_threshold_ms")),
        "latency_median_ms": _number(latency.get("median_ms")),
        "latency_max_ms": _number(latency.get("max_ms")),
        "latency_p95_ms": _number(latency.get("p95_ms")),
        "latency_samples": _number(latency.get("samples")),
        "configured_interval_ms": _number(loop.get("configured_interval_ms")),
        "observed_rate_hz": _number(loop.get("observed_frame_rate_hz")),
        "active_seconds": _number(session.get("active_seconds")),
        "end_reason": str(session.get("end_reason") or ""),
        "target_losses": _number(target.get("losses")),
        "target_changes": _number(target.get("changes")),
        "target_unreadable": _number(target.get("unreadable")),
    }


def analyse_session(
    manager: Any,
    record: Any,
    *,
    evaluation: Mapping[str, Any] | None = None,
    benchmark: Mapping[str, Any] | None = None,
    recommendation_status: Mapping[str, str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Analyse one recorded session (optionally through its evaluation).

    ``record`` is the manager's ``SessionRecord``; ``evaluation`` and
    ``benchmark`` are the NF-05/NF-04 records when the subject came from one.
    Nothing here mutates any of them.
    """
    status_index = dict(recommendation_status or {})
    session_id = str(getattr(record, "session_id", ""))
    timeline = manager.timeline_payload(session_id, limit=max(1, min(int(limit), 240)))
    events = list(timeline.get("events") or [])
    event_objects = _event_objects(events)
    telemetry_payload = manager.telemetry_payload(session_id)
    telemetry = _mapping(telemetry_payload.get("telemetry"))
    facts = _telemetry_facts(telemetry)
    classification = _mapping(timeline.get("classification"))
    recorded_timeline = str(timeline.get("source") or "") == "recorded"
    frames_available = int(facts["frames"] or 0)
    interventions = list(getattr(record, "interventions", []) or [])
    errors = list(getattr(record, "errors", []) or [])

    findings: list[Finding] = []
    subject = session_id

    # -- capture failures -------------------------------------------------
    capture_events = [event for event in event_objects if event.type == "capture.failed"]
    if capture_events or (facts["capture_failures"] or 0) > 0:
        signals = len(capture_events) + (1 if (facts["capture_failures"] or 0) > 0 else 0)
        strength, basis = _strength_for(signals=signals, recorded=bool(capture_events))
        findings.append(
            _finding(
                subject=subject,
                category="capture_failure",
                title="Capture failed during the observation",
                severity="high",
                severity_basis=(
                    "The observation loop could not read the target window on at least one "
                    "tick; frames that would have been evidence do not exist."
                ),
                kind="observed",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"{int(facts['capture_failures'] or 0)} capture failure(s) were measured and "
                    f"{len(capture_events)} recorded on the timeline."
                ),
                evidence=_refs(
                    *[_event_ref(event) for event in capture_events[:3]],
                    {"kind": "metric", "metric": "capture_failures",
                     "value": int(facts["capture_failures"] or 0)},
                ),
                alternatives=(
                    "The target window was minimized or occluded for one tick.",
                    "The desktop capture seam failed transiently on this host.",
                ),
                limitations=(
                    "The record states that the capture failed; it does not state why the "
                    "underlying seam refused.",
                ),
            )
        )

    # -- stale observations ------------------------------------------------
    stale = int(facts["stale_frames"] or 0)
    if stale > 0:
        frames = int(facts["frames"] or 0)
        ratio = (stale / (stale + frames)) if (stale + frames) else None
        ratio_text = f"{ratio:.0%} of capture attempts" if ratio is not None else "a share"
        signals = 1 + (1 if any(event.type == "frame.rejected" for event in event_objects) else 0)
        strength, basis = _strength_for(
            signals=signals, recorded=any(event.type == "frame.rejected" for event in event_objects)
        )
        findings.append(
            _finding(
                subject=subject,
                category="stale_observation",
                title="Captured frames were not fresh",
                severity="medium",
                severity_basis=(
                    "A stale frame is excluded from the observation, so it reduces the "
                    "evidence the run produced without stopping it."
                ),
                kind="derived",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"{stale} stale capture(s) against {frames} fresh frame(s) ({ratio_text}). "
                    "The count is measured; the cause is not."
                ),
                evidence=_refs(
                    {"kind": "metric", "metric": "stale_frames", "value": stale},
                    *[
                        _event_ref(event)
                        for event in event_objects
                        if event.type == "frame.rejected"
                    ][:2],
                ),
                alternatives=(
                    "The capture cadence is faster than this host can service the window.",
                    "The window content did not change between ticks (a freshness rule, not a fault).",
                ),
                limitations=(
                    "Freshness is judged by the capture seam's own rule; the analysis does not "
                    "restate that rule as a defect.",
                ),
            )
        )

    # -- observation discontinuity ----------------------------------------
    gaps = int(facts["frame_gaps"] or 0)
    threshold = facts["gap_threshold_ms"]
    if gaps > 0 or facts["max_gap_ms"] is not None:
        max_gap = facts["max_gap_ms"]
        strength, basis = _strength_for(signals=1 if max_gap is not None else 0, recorded=False)
        findings.append(
            _finding(
                subject=subject,
                category="observation_discontinuity",
                title="The observation had frame gaps",
                severity="medium",
                severity_basis=(
                    "A gap means the run did not observe the window for a measurable "
                    "interval, which bounds what the run can claim to have seen."
                ),
                kind="derived",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"{gaps} gap(s) exceeded the declared threshold"
                    + (f" of {threshold:.0f} ms" if threshold is not None else "")
                    + (f"; the longest was {max_gap:.0f} ms." if max_gap is not None else ".")
                ),
                evidence=_refs(
                    {"kind": "metric", "metric": "max_gap_ms", "value": max_gap},
                    {"kind": "metric", "metric": "frame_gaps", "value": gaps},
                ),
                alternatives=(
                    "The host was busy with another process for that interval.",
                    "The target window's content changed such that no frame was recorded.",
                ),
                limitations=(
                    "A gap is measured between successful frames while the session ran; a "
                    "pause resets it and is not a gap.",
                ),
            )
        )

    # -- capture latency ---------------------------------------------------
    median = facts["latency_median_ms"]
    interval = facts["configured_interval_ms"]
    if median is not None and (interval is None or median > interval):
        signals = 1 + (1 if facts["latency_max_ms"] is not None else 0)
        strength, basis = _strength_for(signals=signals, recorded=False)
        findings.append(
            _finding(
                subject=subject,
                category="capture_latency",
                title="Capture latency is at or above the observation interval",
                severity="medium",
                severity_basis=(
                    "When a capture takes longer than the configured interval, the "
                    "configured cadence is not what the run achieved."
                ),
                kind="derived",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"Median capture latency {median:.1f} ms"
                    + (f" against a {interval:.0f} ms interval" if interval is not None else "")
                    + (
                        f"; the maximum was {facts['latency_max_ms']:.0f} ms."
                        if facts["latency_max_ms"] is not None
                        else "."
                    )
                ),
                evidence=_refs(
                    {"kind": "metric", "metric": "latency_median_ms", "value": median},
                    {"kind": "metric", "metric": "configured_interval_ms", "value": interval},
                ),
                alternatives=(
                    "The interval was configured below what this host can sustain.",
                    "A one-off scheduling stall inflated the median's window.",
                ),
                limitations=(
                    "The comparison of a latency against an interval is arithmetic over two "
                    "measured figures; it is not a frame-rate verdict.",
                ),
            )
        )

    # -- target loss / identity change ------------------------------------
    target_loss_events = [event for event in event_objects if event.type == "target.lost"]
    if target_loss_events or (facts["target_losses"] or 0) > 0:
        signals = len(target_loss_events) + (1 if (facts["target_losses"] or 0) > 0 else 0)
        strength, basis = _strength_for(signals=signals, recorded=bool(target_loss_events))
        findings.append(
            _finding(
                subject=subject,
                category="target_loss",
                title="The pinned target window was lost",
                severity="high",
                severity_basis=(
                    "Observations after a loss describe a different (or absent) window, so "
                    "the run's later measurements are not about the same subject."
                ),
                kind="observed",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"{int(facts['target_losses'] or 0)} target loss(es) were measured and "
                    f"{len(target_loss_events)} recorded."
                ),
                evidence=_refs(
                    *[_event_ref(event) for event in target_loss_events[:3]],
                    {"kind": "metric", "metric": "target_losses",
                     "value": int(facts["target_losses"] or 0)},
                ),
                alternatives=("The window was closed by the operator.",),
                limitations=("The record states the loss; it does not state who closed the window.",),
            )
        )
    target_change_events = [event for event in event_objects if event.type == "target.changed"]
    if target_change_events or (facts["target_changes"] or 0) > 0:
        strength, basis = _strength_for(
            signals=len(target_change_events) + 1, recorded=bool(target_change_events)
        )
        findings.append(
            _finding(
                subject=subject,
                category="target_changed",
                title="The target window identity changed during the run",
                severity="high",
                severity_basis=(
                    "A changed identity means the observations before and after it may "
                    "describe different windows."
                ),
                kind="observed",
                confidence=strength,
                confidence_basis=basis,
                explanation=(
                    f"{int(facts['target_changes'] or 0)} identity change(s) were measured."
                ),
                evidence=_refs(
                    *[_event_ref(event) for event in target_change_events[:3]],
                    {"kind": "metric", "metric": "target_changes",
                     "value": int(facts["target_changes"] or 0)},
                ),
                alternatives=("The window was re-launched by the operator between ticks.",),
                limitations=("Identity is the handle/pid pair the session pinned; a "
                             "re-launched window is a different target by design.",),
            )
        )

    # -- how the session ended --------------------------------------------
    end_reason = str(facts["end_reason"] or getattr(record, "end_reason", "") or "")
    state = str(getattr(getattr(record, "state", None), "value", "") or "")
    if end_reason == "time_limit":
        findings.append(
            _finding(
                subject=subject,
                category="time_limit_reached",
                title="The session ended on its configured time limit",
                severity="low",
                severity_basis=(
                    "A configured limit ending is not a fault; it is a configuration "
                    "choice whose adequacy the frame count can answer."
                ),
                kind="derived",
                confidence="moderate",
                confidence_basis="The record states the end reason and the telemetry the count.",
                explanation=(
                    f"The session ended after {facts['active_seconds'] or 0:.1f} active second(s) "
                    f"with {int(facts['frames'] or 0)} fresh frame(s)."
                ),
                evidence=_refs(
                    {"kind": "metric", "metric": "frames", "value": int(facts["frames"] or 0)},
                    *[_event_ref(event) for event in event_objects if event.type == "session.stopped"][:1],
                ),
                alternatives=(
                    "The limit was intentionally short for a smoke observation.",
                    "The limit was reached before the scenario's minimum frame count, which "
                    "makes the run ineligible rather than wrong.",
                ),
                limitations=("No objective is verified by reaching a time limit.",),
            )
        )
    elif end_reason in {"cancelled", "emergency_stop"}:
        findings.append(
            _finding(
                subject=subject,
                category="guardian_cancellation",
                title=f"The session was stopped through the Guardian path ({end_reason})",
                severity="info",
                severity_basis=(
                    "An operator stop is a decision, not a defect; it is reported because it "
                    "bounds what the evidence can describe."
                ),
                kind="observed",
                confidence="strong",
                confidence_basis="The session record states the end reason.",
                explanation="The run ended by operator control rather than by its own budget.",
                evidence=_refs(
                    *[_event_ref(event) for event in event_objects if event.type == "guardian.intervention"][:2],
                    {"kind": "session", "session_id": session_id},
                ),
                alternatives=(),
                limitations=("A stopped run's distribution describes only the part that ran.",),
            )
        )
    elif end_reason == "error" or state == "FAILED":
        findings.append(
            _finding(
                subject=subject,
                category="unexpected_exception",
                title="The observation loop ended with an error",
                severity="critical",
                severity_basis=(
                    "A FAILED loop recorded no complete distribution, so the run cannot "
                    "support any comparison."
                ),
                kind="observed",
                confidence="strong",
                confidence_basis="The session record states the ending.",
                explanation="The session is FAILED; its measurements are partial by definition.",
                evidence=_refs(
                    *[_event_ref(event) for event in event_objects if event.type == "session.failed"][:2],
                    {"kind": "session", "session_id": session_id},
                ),
                alternatives=(),
                limitations=("The record states the ending; the cause is only recorded when "
                             "the loop annotated it.",),
            )
        )

    # -- recorded errors ---------------------------------------------------
    error_events = [
        event
        for event in event_objects
        if event.type == "error.recorded"
        and str(event.payload.get("where") or "") not in _BENIGN_ERROR_WHERE
    ]
    if error_events:
        unknown_where = [
            event
            for event in error_events
            if str(event.payload.get("where") or "") not in {
                "capture", "target", "limits", "capture_loop", "stop", "reconcile",
                "timeline_persist", "recovery",
            }
        ]
        # The record's error list and the timeline event are the SAME fact, so
        # they are one signal here; counting both would inflate a single error
        # into "corroborated" evidence.
        strength, basis = _strength_for(
            signals=1 + (1 if len(error_events) > 1 else 0), recorded=True
        )
        findings.append(
            _finding(
                subject=subject,
                category="error_recorded",
                title=f"{len(error_events) or len(errors)} error(s) were recorded",
                severity="medium" if not unknown_where else "high",
                severity_basis=(
                    "A recorded error names where it happened; an unclassified location "
                    "cannot be attributed to a known subsystem."
                ),
                kind="observed",
                confidence=strength,
                confidence_basis=basis,
                explanation="Locations: "
                + ", ".join(
                    sorted(
                        {
                            str(event.payload.get("where") or "unspecified")
                            for event in error_events
                        }
                    )
                ),
                evidence=_refs(
                    *[_event_ref(event) for event in error_events[:3]],
                    {"kind": "session", "session_id": session_id},
                ),
                alternatives=("A transient environmental condition.",),
                limitations=("An error's message is a label; the analysis does not re-derive "
                             "a cause the record never wrote down.",),
            )
        )
    persistence = _mapping(timeline.get("persistence"))
    if str(persistence.get("state") or "") == "failed":
        findings.append(
            _finding(
                subject=subject,
                category="persistence_failure",
                title="Timeline persistence could not be verified",
                severity="critical",
                severity_basis=(
                    "Evidence that cannot be read back cannot be cited, so an improvement "
                    "claim built on this session would be unverifiable."
                ),
                kind="observed",
                confidence="strong",
                confidence_basis="The timeline payload states the persistence result.",
                explanation=str(persistence.get("reason") or "The snapshot was not read back."),
                evidence=_refs(
                    *[_event_ref(event) for event in event_objects if event.type == "timeline.persistence_failed"][:2],
                    {"kind": "timeline_persistence", "state": str(persistence.get("state") or "")},
                ),
                alternatives=(),
                limitations=("The session's measurements may still be valid; it is the "
                             "persisted copy that was not verified.",),
            )
        )

    # -- evaluation-level findings (only when an evaluation was selected) ---
    if evaluation:
        findings.extend(_evaluation_findings(subject, evaluation, benchmark))

    # -- structural limits -------------------------------------------------
    findings.append(
        _finding(
            subject=subject,
            category="objective_verifier_missing",
            title="No objective was verified by this session",
            severity="info",
            severity_basis=(
                "This build has no objective verifier, so no finding and no experiment may "
                "claim that an objective was achieved."
            ),
            kind="observed",
            confidence="strong",
            confidence_basis="The capability registry states that no verifier exists.",
            explanation=(
                "Completion means the observation recorded its required data; it is never a "
                "won objective or a gameplay score."
            ),
            evidence=_refs({"kind": "capability", "name": "objective_verification", "available": False}),
            alternatives=(),
            limitations=("A future phase with a real verifier would add this finding as a "
                         "measurable result instead of a limitation.",),
        )
    )

    findings = findings[:MAX_FINDINGS]
    recommendations = _recommendations_for(findings, subject, status_index)
    earliest = _earliest_recoverable_error(event_objects, findings, facts)

    evidence_state = {
        "timeline_source": str(timeline.get("source") or "unknown"),
        "timeline_events": len(events),
        "recorded_events": sum(1 for event in event_objects if not event.derived),
        "frames": int(facts["frames"] or 0),
        "frames_available": frames_available,
        "telemetry_available": bool(telemetry.get("session")),
        "errors": len(error_events) or len(errors),
        "interventions": len(interventions),
        "classification": str(classification.get("outcome") or "unknown"),
    }
    insufficient = _insufficient_evidence_reason(evidence_state, findings)
    return {
        "version": ANALYSIS_VERSION,
        "analysis_id": f"ana-{_digest(subject, evidence_state['timeline_events'], evaluation or {})[:12]}",
        "generated_at": utc_now(),
        "read_only": True,
        "subject": {
            "session_id": session_id,
            "evaluation_id": str((evaluation or {}).get("id") or ""),
            "game": _mapping((evaluation or {}).get("game")),
            "scenario": _mapping((evaluation or {}).get("scenario")),
            "benchmark_run_id": str((evaluation or {}).get("benchmark_run_id") or ""),
            "state": state,
            "end_reason": end_reason,
            "mission": str(getattr(getattr(record, "mission", None), "name", "") or ""),
            "target": _target_identity(getattr(record, "target", None)),
            "created_at": str(getattr(record, "created_at", "") or ""),
        },
        "evidence": {
            **evidence_state,
            "timeline_events_note": (
                "The newest events are kept (NF-03 retention); a dropped count means the "
                "beginning of the session is no longer part of its timeline."
            ),
            "frames_note": (
                "Frames are referenced, never loaded: a finding cites a frame sequence "
                "number and the frame endpoint resolves it on demand."
            ),
        },
        "findings": [finding.to_dict() for finding in findings],
        "recommendations": [item.to_dict() for item in recommendations],
        "earliest_recoverable_error": earliest,
        "insufficient_evidence": insufficient,
        "classification": classification,
        "limitations": _analysis_limitations(recorded_timeline, interventions, evaluation),
        "methodology": (
            "Findings are derived from the recorded timeline, telemetry snapshot, session "
            "record and—when the subject came from one—the NF-04 benchmark run and NF-05 "
            "evaluation record. Nothing is cached, no record is modified, and no live "
            "session is observed by an analysis."
        ),
        "note": (
            "A finding marked observed is stated by the record, derived is arithmetic over "
            "recorded values, hypothesis is a plausible explanation without a confirming "
            "record, and unknown means the evidence cannot take it further."
        ),
    }


def _evaluation_findings(
    subject: str,
    evaluation: Mapping[str, Any],
    benchmark: Mapping[str, Any] | None,
) -> list[Finding]:
    """Findings that only an NF-05 evaluation (or an NF-04 run) can support."""
    found: list[Finding] = []
    status = str(evaluation.get("status") or "unknown")
    reason = str(evaluation.get("reason") or "")
    objective = _mapping(evaluation.get("objective"))
    evidence = _mapping(evaluation.get("evidence"))
    gaps = [str(item) for item in (evidence.get("gaps") or [])]
    if status == "invalid" and "fresh frame" in reason.lower():
        found.append(
            _finding(
                subject=subject,
                category="insufficient_frames",
                title="The run recorded fewer fresh frames than the scenario requires",
                severity="medium",
                severity_basis=(
                    "The run is excluded from aggregates and baselines, which is what makes "
                    "its measurements unusable rather than merely short."
                ),
                kind="observed",
                confidence="strong",
                confidence_basis="The evaluation record states the reason.",
                explanation=reason,
                evidence=_refs(
                    {"kind": "evaluation", "evaluation_id": str(evaluation.get("id") or ""),
                     "status": status},
                ),
                alternatives=("The budget was deliberately shortened for a smoke run.",),
                limitations=("The reason states the scenario's minimum; it does not state "
                             "why the host produced fewer frames.",),
            )
        )
    comparison = _mapping(evaluation.get("comparison"))
    if comparison and not comparison.get("compatible"):
        reasons = [str(item) for item in (comparison.get("reasons") or [])] or [
            "The comparison was refused without a recorded reason."
        ]
        found.append(
            _finding(
                subject=subject,
                category="incompatible_baseline",
                title="The baseline comparison was refused",
                severity="medium",
                severity_basis=(
                    "A refused comparison produces no delta, so nothing about change can be "
                    "concluded from this pair."
                ),
                kind="observed",
                confidence="strong",
                confidence_basis="The comparison payload lists the reasons.",
                explanation=" ".join(reasons),
                evidence=_refs(
                    {"kind": "comparison", "compatible": False, "reasons": reasons},
                ),
                alternatives=("The baseline was recorded under a different configuration "
                              "on purpose (for example a shorter smoke run).",),
                limitations=("The refusal is NF-04's fairness rule, not a fault in this run.",),
            )
        )
    elif comparison and comparison.get("compatible"):
        sample = _mapping(comparison.get("sample"))
        if int(sample.get("left") or 0) < 2 or int(sample.get("right") or 0) < 2:
            found.append(
                _finding(
                    subject=subject,
                    category="single_sample",
                    title="The comparison has a single run on at least one side",
                    severity="low",
                    severity_basis=(
                        "One run shows no variability, so a difference is a measurement and "
                        "not an established effect."
                    ),
                    kind="derived",
                    confidence="moderate",
                    confidence_basis="The comparison payload states the sample sizes.",
                    explanation=(
                        f"Sample sizes: baseline {int(sample.get('left') or 0)}, candidate "
                        f"{int(sample.get('right') or 0)}."
                    ),
                    evidence=_refs({"kind": "sample", **sample}),
                    alternatives=(),
                    limitations=("A repeat count of two or more per side would remove this "
                                 "limitation.",),
                )
            )
    if gaps:
        found.append(
            _finding(
                subject=subject,
                category="evidence_gaps",
                title=f"{len(gaps)} evidence gap(s) were reported for this evaluation",
                severity="low",
                severity_basis=(
                    "A named gap is a caveat on what the report can cite, not a failure of "
                    "the run itself."
                ),
                kind="observed",
                confidence="moderate",
                confidence_basis="The evaluation's own evidence block names each gap.",
                explanation=" ".join(gaps[:3]),
                evidence=_refs({"kind": "evaluation_evidence", "gaps": gaps[:5]}),
                alternatives=(),
                limitations=("A gap means the artifact was absent when the report was built; "
                             "later phases may attach it.",),
            )
        )
    if benchmark:
        metrics = _mapping(benchmark.get("metrics"))
        frames = _mapping(metrics.get("frames"))
        if frames and frames.get("available") and _number(frames.get("value")) == 0:
            found.append(
                _finding(
                    subject=subject,
                    category="stale_observation",
                    title="The run recorded no fresh frames at all",
                    severity="high",
                    severity_basis=(
                        "Zero fresh frames means the run has no observation evidence, only "
                        "a session record."
                    ),
                    kind="derived",
                    confidence="strong",
                    confidence_basis="The benchmark metric is measured and reads zero.",
                    explanation="frames = 0 for this run; no frame evidence can be cited.",
                    evidence=_refs({"kind": "metric", "metric": "frames", "value": 0}),
                    alternatives=("The target was covered before the run started.",),
                    limitations=("Zero is a measurement here: the run attempted and recorded none.",),
                )
            )
    if not objective.get("available"):
        # The structural finding above already states the missing verifier, so
        # this evaluation-specific note is only added when a report tried to
        # carry an objective that does not exist.
        pass
    return found


def _insufficient_evidence_reason(
    evidence_state: Mapping[str, Any], findings: Sequence[Finding]
) -> dict[str, Any]:
    """Whether the analysis has enough recorded evidence to say anything.

    A session whose timeline is empty AND whose telemetry recorded no frames and
    no target check has nothing to analyse. The distinction matters: returning a
    diagnosis there would be the exact fabrication this layer exists to prevent.
    """
    meaningful = (
        int(evidence_state.get("timeline_events") or 0) > 0
        or int(evidence_state.get("frames") or 0) > 0
        or int(evidence_state.get("errors") or 0) > 0
        or int(evidence_state.get("interventions") or 0) > 0
    )
    if meaningful:
        return {
            "state": False,
            "reason": "",
            "findings": len(findings),
        }
    return {
        "state": True,
        "reason": (
            "The session recorded no timeline events, no frames, no errors and no "
            "interventions, so there is no evidence to analyse."
        ),
        "what_would_help": list(_WHATS_NEXT),
        "findings": len(findings),
    }


def _earliest_recoverable_error(
    events: Sequence[Any],
    findings: Sequence[Finding],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    """The earliest failure signature the evidence supports — never a root cause."""
    signature_types = {item[0]: item for item in _FAILURE_SIGNATURES}
    ordered = list(events)
    candidate = None
    for index, event in enumerate(ordered):
        if event.type not in signature_types:
            continue
        if (
            event.type == "error.recorded"
            and str(event.payload.get("where") or "") in _BENIGN_ERROR_WHERE
        ):
            continue
        # The manager records a failure twice on the same tick: a generic
        # ``error.recorded`` and then the specific event (``capture.failed``,
        # ``target.lost``). They are ONE fact, so the specific event is the
        # candidate — naming the generic wrapper would hide the category the
        # phase asks about.
        if index + 1 < len(ordered):
            following = ordered[index + 1]
            same_tick = (
                event.elapsed_ms is not None
                and following.elapsed_ms is not None
                and abs(float(following.elapsed_ms) - float(event.elapsed_ms)) <= 250.0
            )
            if (
                event.type == "error.recorded"
                and following.type in signature_types
                and following.type != "error.recorded"
                and same_tick
            ):
                continue
        candidate = event
        break
    if candidate is None:
        return {
            "available": False,
            "candidate": None,
            "reason": (
                "No failure signature was recorded, so there is no earliest recoverable "
                "error to name. That is not the same as success: no objective verifier "
                "exists in this build."
            ),
            "later_symptoms": [],
            "counter_evidence": [],
            "causation_claimed": False,
        }
    _event_type, category, severity, kind = signature_types[candidate.type]
    finding = next((item for item in findings if item.category == category), None)
    later = [
        _event_ref(event)
        for event in events
        if event.seq > candidate.seq
        and event.type in signature_types
        and not (
            event.type == "error.recorded"
            and str(event.payload.get("where") or "") in _BENIGN_ERROR_WHERE
        )
    ][:MAX_LATER_SYMPTOMS]
    frames_after = sum(
        1 for event in events if event.seq > candidate.seq and event.type == "frame.captured"
    )
    counter: list[str] = []
    if frames_after:
        counter.append(
            f"{frames_after} frame(s) were captured after it, so the observation loop "
            "continued past this event."
        )
    if not later:
        counter.append("No further failure signature follows it in the recorded timeline.")
    if str(facts.get("end_reason") or "") == "time_limit":
        counter.append(
            "The session ended on its configured time limit, which is a bounded stop "
            "rather than a crash."
        )
    return {
        "available": True,
        "candidate": {
            **_event_ref(candidate),
            "elapsed_ms": getattr(candidate, "elapsed_ms", None),
            "severity": severity,
            "kind": kind,
            "finding_id": finding.finding_id if finding is not None else "",
        },
        "basis": (
            "The earliest recorded event whose type is a failure signature. Sequence order "
            "makes it the earliest; nothing in the record makes it the cause."
        ),
        "later_symptoms": later,
        "counter_evidence": counter,
        "causation_claimed": False,
        "uncertainty": (
            "Temporal order is not causation. This is the earliest supported candidate; "
            "the later symptoms may share a different cause, and events not recorded here "
            "cannot be considered."
        ),
        "alternatives": list(finding.alternatives) if finding is not None else [],
    }


def _recommendations_for(
    findings: Sequence[Finding],
    subject: str,
    status_index: Mapping[str, str],
) -> list[Recommendation]:
    """One recommendation per actionable finding, ranked by a declared rule.

    Priority is the finding's severity band first, then its evidence strength,
    then the order the rules are declared in — which puts evidence integrity and
    capture problems ahead of configuration tuning. A finding with no action
    (the structural limits) produces no recommendation.
    """
    rules = {rule.category: rule for rule in RECOMMENDATION_RULES}
    declared = {rule.category: index for index, rule in enumerate(RECOMMENDATION_RULES)}
    ranked = sorted(
        (finding for finding in findings if finding.category in rules),
        key=lambda finding: (
            _SEVERITY_WEIGHT.get(finding.severity, 9),
            _STRENGTH_WEIGHT.get(finding.confidence, 9),
            declared.get(finding.category, 99),
        ),
    )
    rows: list[Recommendation] = []
    for rank, finding in enumerate(ranked, start=1):
        if len(rows) >= MAX_RECOMMENDATIONS:
            break
        rule = rules[finding.category]
        recommendation_id = f"rec-{_digest(subject, finding.category, rule.action)[:12]}"
        metric_key = rule.metric
        try:
            spec = metric_spec(metric_key)
            metric_label = spec.label
        except KeyError:  # pragma: no cover - the constant table is checked by tests
            metric_label = metric_key
        rows.append(
            Recommendation(
                recommendation_id=recommendation_id,
                finding_id=finding.finding_id,
                category=finding.category,
                priority=rank,
                target_issue=finding.title,
                action=rule.action,
                expected_effect=rule.expected_effect,
                metric=metric_key,
                metric_label=metric_label,
                success_criterion=rule.success_criterion,
                risk=rule.risk,
                effort=rule.effort,
                preconditions=rule.preconditions,
                requires_approval=True,
                experiment={
                    "kind": rule.experiment_kind,
                    "variable": rule.variable,
                    "scenario_id": rule.scenario,
                    "primary_metric": metric_key,
                    "supported": rule.experiment_kind in {"repeat", "configuration_change"},
                    # A configuration change has to name a value. The one category
                    # that asks for it compares against a RECORDED baseline, so the
                    # planner takes both values from the two recorded runs instead of
                    # asking an operator to retype a number the evidence already has.
                    "change_from": (
                        "recorded_baseline"
                        if rule.experiment_kind == "configuration_change"
                        else ""
                    ),
                    "reason": (
                        ""
                        if rule.experiment_kind
                        else "This finding needs an operator change (or a capability this "
                        "build lacks), not a benchmark run."
                    ),
                },
                evidence=finding.evidence,
                status=str(status_index.get(recommendation_id) or "proposed"),
            )
        )
    return rows


def _analysis_limitations(
    recorded_timeline: bool,
    interventions: Sequence[Any],
    evaluation: Mapping[str, Any] | None,
) -> list[str]:
    limitations = [
        "An analysis reads recorded evidence; it cannot observe a live session and it "
        "modifies no record.",
        "No objective verifier exists in this build, so no analysis states that a goal "
        "was achieved, a game was played well, or a score changed.",
        "Temporal order alone never establishes causation; a hypothesis is labelled as one.",
    ]
    if not recorded_timeline:
        limitations.append(
            "The timeline is DERIVED from the session record (this session predates the "
            "event log), so its order follows recorded timestamps, which can tie."
        )
    if not interventions:
        limitations.append(
            "No Guardian intervention was recorded for this session, so nothing about "
            "safety behaviour can be concluded from it."
        )
    if evaluation is not None and not _mapping(evaluation.get("comparison")):
        limitations.append(
            "This evaluation has no comparison, so no before/after statement applies to it."
        )
    return limitations


def _event_objects(events: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Rehydrate the resolved event payloads into typed events for analysis.

    The manager resolves evidence references for display; analysis needs the
    event's own fields (type, seq, payload, derived), so the resolved page is
    mapped back onto :class:`TimelineEvent`. A malformed row is skipped rather
    than repaired.
    """
    objects: list[Any] = []
    for item in events:
        data = dict(item)
        envelope = _mapping(data.get("event"))
        if envelope:
            data = {**data, **envelope}
        event = TimelineEvent.from_dict(
            {
                "type": data.get("type"),
                "seq": data.get("seq"),
                "session_id": data.get("session_id"),
                "event_id": data.get("event_id"),
                "category": data.get("category"),
                "severity": data.get("severity"),
                "source": data.get("source"),
                "at": data.get("at"),
                "elapsed_ms": data.get("elapsed_ms"),
                "payload": data.get("payload"),
                "evidence": data.get("evidence"),
                "correlation_id": data.get("correlation_id"),
                "derived": data.get("derived"),
                "version": data.get("version"),
            }
        )
        if event is None:
            continue
        objects.append(event)
    return objects


__all__ = [
    "ANALYSIS_VERSION",
    "COACH_VERSION",
    "CoachError",
    "CoachNotFoundError",
    "EVIDENCE_STRENGTHS",
    "FINDING_KINDS",
    "Finding",
    "MAX_RECOMMENDATIONS_KEPT",
    "NovaForgeCoach",
    "RECOMMENDATION_RULES",
    "RECOMMENDATION_STATUSES",
    "RECOMMENDATIONS_NAMESPACE",
    "Recommendation",
    "SEVERITIES",
    "analyse_session",
]
