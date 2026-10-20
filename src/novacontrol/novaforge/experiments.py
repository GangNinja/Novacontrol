"""NovaForge NF-06 — controlled experiments on the NF-04/NF-05 pipeline.

An experiment is a *declared* comparison: which recorded runs form the
baseline, what single variable changes, what the arms share, which metric
decides, and what would count as a regression. The lab then runs both arms
through the EXISTING evaluation lab (which drives the existing benchmark lab),
and reports the result under NF-04's own fairness rules.

The rules this module exists to enforce:

* **Approval is mandatory and bound to the configuration.** ``run`` refuses an
  experiment that is not approved, and refuses one whose spec digest differs
  from the digest that was approved. Changing the budget, interval, scenario,
  target, metric or repeats after approval requires approving again.
* **Nothing here is a second runner.** Every arm is a normal NF-05 evaluation
  over a normal NF-04 benchmark run; this module only sequences them, polls
  their records, and reads the comparison NF-04 already computes.
* **No model weights, no policy changes, no gameplay input.** The evaluation
  lab runs in ``observation_only`` mode; this module never asks for control
  mode, never launches a game and never sends input.
* **A configuration change is judged honestly.** NF-04 refuses a pooled delta
  across a different budget or observation interval ("not on the same basis"),
  so an experiment that changes one of those reports ``not_comparable`` with
  NF-04's own reasons, plus per-arm descriptive figures that are labelled as
  descriptions and never as effects.
* **A small sample cannot establish an effect.** A single run per side yields
  ``inconclusive`` with ``limited_to_single_run`` set, never "improved".
* **A broken guardrail is a regression.** A guardrail breach overrides a
  favourable primary delta, because the change is then not a win.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from novacontrol.persistence.json_store import JsonStateStore

from .benchmark import (
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    SCENARIOS,
    BenchmarkError,
    metric_spec,
    resolve_scenario,
)
from .models import utc_now

EXPERIMENT_VERSION = 1
EXPERIMENTS_NAMESPACE = "novaforge_experiments"
MAX_EXPERIMENTS_KEPT = 60
MAX_HISTORY = 40
MAX_REPEATS = 3
MIN_REPEATS = 1

#: Poll cadence while an arm runs. The evaluation lab already runs the session
#: in the background; this is only how often this module reads its record.
POLL_SECONDS = 0.1

#: How long past the declared budget an arm may take before it is cancelled.
#: A bounded run finalizes its store write after the budget, so the slack is
#: real work, not padding.
ARM_SLACK_SECONDS = 30.0

#: A hard ceiling for one experiment, whatever the repeats add up to.
MAX_TOTAL_SECONDS = 900.0

EXPERIMENT_STATUSES = (
    "proposed",
    "approved",
    "blocked",
    "running",
    "completed",
    "failed",
    "cancelled",
    "rejected",
)

#: Statuses after which an experiment is history and never re-runs.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "rejected"})

EXPERIMENT_KINDS = ("repeat", "configuration_change")

VARIABLES = ("none", "duration_seconds", "frame_interval_seconds")

VERDICTS = ("improved", "regressed", "inconclusive", "not_comparable")

#: The direction that counts as better, declared here because the NF-04 metric
#: catalog describes what a metric IS, not which way an operator wants it to go.
#: Only metrics with an unambiguous direction may decide an experiment.
METRIC_DIRECTIONS: dict[str, str] = {
    "frames": "increase",
    "frame_yield": "increase",
    "observed_rate_hz": "increase",
    "stale_frames": "decrease",
    "capture_failures": "decrease",
    "frame_gaps": "decrease",
    "max_gap_ms": "decrease",
    "latency_median_ms": "decrease",
    "latency_p95_ms": "decrease",
    "latency_max_ms": "decrease",
    "target_losses": "decrease",
    "target_changes": "decrease",
    "interventions": "decrease",
    "errors": "decrease",
}

PRIMARY_METRICS = tuple(METRIC_DIRECTIONS)


class ExperimentError(ValueError):
    """A spec or request that cannot be run as asked."""

    def __init__(self, errors: Sequence[str] | str) -> None:
        self.errors = [errors] if isinstance(errors, str) else [str(item) for item in errors]
        super().__init__(" ".join(self.errors))


class ExperimentNotFoundError(ExperimentError):
    """An unknown experiment id."""


class ExperimentConflictError(ExperimentError):
    """A transition that would overlap a live experiment or evaluation."""


def _digest(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """The declared configuration an approval is bound to."""

    experiment_id: str
    created_at: str
    subject: dict[str, Any]
    game_id: str
    game_version: str
    scenario_id: str
    scenario_version: int
    target_title: str
    kind: str
    variable: str
    baseline_configuration: dict[str, Any]
    change: dict[str, Any]
    baseline_run_ids: tuple[str, ...]
    repeat_count: int
    primary_metric: str
    threshold: float
    secondary_metrics: tuple[str, ...]
    guardrails: tuple[dict[str, Any], ...]
    confirm_offline: bool
    notes: str = ""
    abort: dict[str, Any] = field(default_factory=dict)
    evidence_required: tuple[str, ...] = ()

    @property
    def primary_direction(self) -> str:
        return METRIC_DIRECTIONS.get(self.primary_metric, "increase")

    def candidate_configuration(self) -> dict[str, Any]:
        merged = dict(self.baseline_configuration)
        merged.update(self.change)
        return merged

    def digest(self) -> str:
        """The identity of the approved configuration.

        Deliberately excludes ``experiment_id``, ``created_at`` and ``notes``:
        those describe the proposal, not what would run. Everything that would
        change the runs is included, so approving and then editing a repeat,
        a threshold or a guardrail invalidates the approval.
        """
        return _digest(
            {
                "game_id": self.game_id,
                "game_version": self.game_version,
                "scenario_id": self.scenario_id,
                "scenario_version": self.scenario_version,
                "target_title": self.target_title,
                "kind": self.kind,
                "variable": self.variable,
                "baseline_configuration": self.baseline_configuration,
                "change": self.change,
                "baseline_run_ids": list(self.baseline_run_ids),
                "repeat_count": self.repeat_count,
                "primary_metric": self.primary_metric,
                "threshold": self.threshold,
                "secondary_metrics": list(self.secondary_metrics),
                "guardrails": [dict(item) for item in self.guardrails],
                "confirm_offline": self.confirm_offline,
                "abort": dict(self.abort),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "subject": dict(self.subject),
            "game_id": self.game_id,
            "game_version": self.game_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "target_title": self.target_title,
            "kind": self.kind,
            "variable": self.variable,
            "baseline_configuration": dict(self.baseline_configuration),
            "change": dict(self.change),
            "candidate_configuration": self.candidate_configuration(),
            "baseline_run_ids": list(self.baseline_run_ids),
            "repeat_count": self.repeat_count,
            "primary_metric": self.primary_metric,
            "primary_direction": self.primary_direction,
            "threshold": self.threshold,
            "secondary_metrics": list(self.secondary_metrics),
            "guardrails": [dict(item) for item in self.guardrails],
            "confirm_offline": self.confirm_offline,
            "notes": self.notes,
            "abort": dict(self.abort),
            "evidence_required": list(self.evidence_required),
            "digest": self.digest(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExperimentSpec":
        data = dict(payload or {})
        return cls(
            experiment_id=str(data.get("experiment_id") or ""),
            created_at=str(data.get("created_at") or ""),
            subject=_mapping(data.get("subject")),
            game_id=str(data.get("game_id") or ""),
            game_version=str(data.get("game_version") or ""),
            scenario_id=str(data.get("scenario_id") or ""),
            scenario_version=int(data.get("scenario_version") or 0),
            target_title=str(data.get("target_title") or ""),
            kind=str(data.get("kind") or "repeat"),
            variable=str(data.get("variable") or "none"),
            baseline_configuration=_mapping(data.get("baseline_configuration")),
            change=_mapping(data.get("change")),
            baseline_run_ids=tuple(str(item) for item in data.get("baseline_run_ids") or []),
            repeat_count=int(data.get("repeat_count") or 1),
            primary_metric=str(data.get("primary_metric") or "frames"),
            threshold=float(data.get("threshold") or 0.0),
            secondary_metrics=tuple(str(item) for item in data.get("secondary_metrics") or []),
            guardrails=tuple(
                dict(item) for item in data.get("guardrails") or [] if isinstance(item, Mapping)
            ),
            confirm_offline=bool(data.get("confirm_offline")),
            notes=str(data.get("notes") or ""),
            abort=_mapping(data.get("abort")),
            evidence_required=tuple(str(item) for item in data.get("evidence_required") or []),
        )


#: Defaults applied when a recommendation becomes an experiment. The threshold
#: is an ABSOLUTE delta in the metric's own unit: a count metric needs any
#: change (0), while a millisecond metric needs a change larger than noise the
#: NF-04 report already documents (15 ms on this build's captured runs).
EVIDENCE_REQUIRED = (
    "A completed, eligible benchmark run per arm (NF-04 eligibility rules).",
    "The NF-05 evaluation record for each arm, with its evidence block.",
    "The NF-04 comparison for the two arms, or its refusal with reasons.",
    "The session timeline for each arm, for the operator to inspect.",
)

GUARDRAIL_DEFAULTS: dict[str, list[dict[str, Any]]] = {
    "capture_failure": [{"metric": "capture_failures", "direction": "max", "bound": 0}],
    "stale_observation": [{"metric": "capture_failures", "direction": "max", "bound": 0}],
    "capture_latency": [
        {"metric": "capture_failures", "direction": "max", "bound": 0},
        {"metric": "stale_frames", "direction": "max", "bound": 0},
    ],
    "observation_discontinuity": [
        {"metric": "target_losses", "direction": "max", "bound": 0}
    ],
    "target_loss": [{"metric": "target_losses", "direction": "max", "bound": 0}],
    "target_changed": [{"metric": "target_changes", "direction": "max", "bound": 0}],
}


def default_threshold(metric: str) -> float:
    try:
        unit = metric_spec(metric).unit
    except KeyError:  # pragma: no cover - guarded by PRIMARY_METRICS
        return 0.0
    return 0.0 if unit != "ms" else 15.0


class NovaForgeExperimentLab:
    """Proposes, approves, runs and judges controlled experiments."""

    def __init__(
        self,
        *,
        evaluations: Any,
        manager: Any | None = None,
        store: JsonStateStore | None = None,
        publish: Any | None = None,
        permission: Callable[[], Mapping[str, Any]] | None = None,
        max_experiments: int = MAX_EXPERIMENTS_KEPT,
    ) -> None:
        self.evaluations = evaluations
        self.benchmark = getattr(evaluations, "benchmark", None)
        self.manager = manager if manager is not None else getattr(evaluations, "manager", None)
        self.store = (
            store
            if store is not None
            else getattr(evaluations, "store", None)
            or getattr(self.manager, "store", None)
        )
        self.publish = publish
        self.permission = permission
        self.max_experiments = max(1, int(max_experiments))
        self._experiments: dict[str, dict[str, Any]] = {}
        self._cancelled: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_evaluation: dict[str, str] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        payload: dict[str, Any] = {}
        if self.store is not None:
            try:
                payload = self.store.read(EXPERIMENTS_NAMESPACE)
            except Exception:  # noqa: BLE001 - an unreadable store has no state
                payload = {}
        rows = payload.get("experiments") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        recovered = 0
        for item in rows:
            if not isinstance(item, dict) or not str(item.get("id") or ""):
                continue
            record = dict(item)
            if str(record.get("status") or "") == "running":
                # A process that died mid-run left a claimed experiment. It is
                # NOT resumed: its arms are incomplete and the honest state is a
                # failure with the reason recorded.
                record["status"] = "failed"
                record["interrupted"] = True
                record["reason"] = (
                    "The application restarted while this experiment was running; its "
                    "arms are incomplete and cannot be resumed. Propose the experiment "
                    "again to re-run it."
                )
                record["ended_at"] = record.get("ended_at") or utc_now()
                record.setdefault("history", []).append(
                    {"status": "failed", "at": record["ended_at"], "note": record["reason"]}
                )
                recovered += 1
            self._experiments[str(record["id"])] = record
        self._trim()
        if recovered:
            self._persist()

    def _trim(self) -> None:
        if len(self._experiments) <= self.max_experiments:
            return
        ordered = sorted(
            self._experiments.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        keep = {str(item.get("id")) for item in ordered[: self.max_experiments]}
        for experiment_id in list(self._experiments):
            if experiment_id not in keep:
                del self._experiments[experiment_id]

    def _persist(self, *, verify_id: str = "") -> dict[str, Any]:
        record = self._experiments.get(verify_id) if verify_id else None
        if self.store is None:
            state = {
                "state": "not_persisted",
                "reason": "No state store is attached; this experiment exists for this process only.",
            }
            if record is not None:
                record["persistence"] = state
            return state
        self._trim()
        ordered = sorted(
            self._experiments.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        try:
            self.store.write(
                EXPERIMENTS_NAMESPACE, {"version": EXPERIMENT_VERSION, "experiments": ordered}
            )
        except Exception as exc:  # noqa: BLE001 - recording cannot fail the work
            state = {"state": "failed", "reason": f"The store raised {type(exc).__name__}: {exc}"}
            if record is not None:
                record["persistence"] = state
            return state
        try:
            stored = self.store.read(EXPERIMENTS_NAMESPACE)
        except Exception as exc:  # noqa: BLE001 - verification must not raise either
            state = {
                "state": "failed",
                "reason": f"The store could not be read back: {type(exc).__name__}: {exc}",
            }
            if record is not None:
                record["persistence"] = state
            return state
        rows = stored.get("experiments") if isinstance(stored, dict) else None
        present = True
        if verify_id:
            present = isinstance(rows, list) and any(
                isinstance(item, dict) and str(item.get("id")) == verify_id for item in rows
            )
        state = {
            "state": "verified" if present else "failed",
            "reason": ""
            if present
            else "The store accepted the write but the experiment is not readable back.",
        }
        if record is not None:
            record["persistence"] = state
        return state

    # -- read surfaces -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What this lab may run, and what it will refuse."""
        return {
            "version": EXPERIMENT_VERSION,
            "available": True,
            "kinds": list(EXPERIMENT_KINDS),
            "variables": list(VARIABLES),
            "primary_metrics": [
                {
                    "key": key,
                    "label": metric_spec(key).label,
                    "unit": metric_spec(key).unit,
                    "direction": direction,
                }
                for key, direction in METRIC_DIRECTIONS.items()
            ],
            "bounds": {
                "repeats": {"min": MIN_REPEATS, "max": MAX_REPEATS},
                "duration_seconds": {"min": MIN_DURATION_SECONDS, "max": MAX_DURATION_SECONDS},
                "arm_slack_seconds": ARM_SLACK_SECONDS,
                "max_total_seconds": MAX_TOTAL_SECONDS,
                "note": (
                    "Each arm is a bounded NF-05 evaluation; this lab only sequences "
                    "them and reads their records."
                ),
            },
            "approval": {
                "required": True,
                "bound_to": "spec digest (scenario, target, variable, repeats, metric, threshold, guardrails)",
                "invalidation": (
                    "Any change to those fields changes the digest, and the previous "
                    "approval no longer authorizes a run."
                ),
            },
            "runner": "The NF-05 evaluation lab over the NF-04 benchmark lab.",
            "mode": "observation_only",
            "unsupported": [
                {
                    "variable": "model_id",
                    "reason": (
                        "No model is invoked by a NovaForge session, so two models cannot "
                        "be compared here."
                    ),
                },
                {
                    "variable": "policy_id",
                    "reason": "No policy runs in this build; there is nothing to switch.",
                },
            ],
            "comparability": (
                "NF-04 refuses a pooled delta across a different execution budget, "
                "observation interval, scenario version, target geometry or operating "
                "system. An experiment that changes one of those reports not_comparable "
                "with NF-04's reasons and per-arm figures that are descriptions, not effects."
            ),
            "retained": len(self._experiments),
            "cap": self.max_experiments,
            "note": (
                "Nothing here trains, tunes or applies a configuration; an approval "
                "authorizes a bounded observation, and nothing else."
            ),
        }

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        record = self._experiments.get(str(experiment_id or "").strip())
        return dict(record) if record is not None else None

    def _require(self, experiment_id: str) -> dict[str, Any]:
        record = self._experiments.get(str(experiment_id or "").strip())
        if record is None:
            raise ExperimentNotFoundError(f"Unknown experiment: {experiment_id!r}")
        return record

    def history(self, *, limit: int = 20, status: str = "") -> dict[str, Any]:
        wanted = str(status or "").strip()
        if wanted and wanted not in EXPERIMENT_STATUSES:
            raise ExperimentError([f"Unknown experiment status {wanted!r}."])
        ordered = sorted(
            self._experiments.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        if wanted:
            ordered = [item for item in ordered if item.get("status") == wanted]
        limit = max(1, min(int(limit), 200))
        rows = [self._summary(record) for record in ordered[:limit]]
        active = self._running()
        return {
            "version": EXPERIMENT_VERSION,
            "experiments": rows,
            "count": len(rows),
            "total": len(ordered),
            "active_experiment_id": active.get("id") if active is not None else None,
            "retention": {
                "cap": self.max_experiments,
                "retained": len(self._experiments),
                "note": "The newest experiments are kept; the oldest are dropped at the cap.",
            },
            "note": (
                "Every proposal, approval, run, cancellation and result is retained here. "
                "A rejected or blocked experiment is kept too: it records a decision."
            ),
        }

    def _running(self) -> dict[str, Any] | None:
        for record in self._experiments.values():
            if record.get("status") == "running":
                return record
        return None

    def _summary(self, record: Mapping[str, Any]) -> dict[str, Any]:
        spec = _mapping(record.get("spec"))
        results = _mapping(record.get("results"))
        verdict = _mapping(results.get("verdict"))
        return {
            "id": record.get("id"),
            "status": record.get("status"),
            "reason": record.get("reason") or "",
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "game_id": spec.get("game_id"),
            "scenario": {
                "scenario_id": spec.get("scenario_id"),
                "version": spec.get("scenario_version"),
            },
            "target_title": spec.get("target_title"),
            "kind": spec.get("kind"),
            "variable": spec.get("variable"),
            "primary_metric": spec.get("primary_metric"),
            "repeat_count": spec.get("repeat_count"),
            "recommendation_id": _mapping(spec.get("subject")).get("recommendation_id"),
            "finding_id": _mapping(spec.get("subject")).get("finding_id"),
            "approved": bool(record.get("approval")),
            "verdict": {
                "label": verdict.get("label"),
                "reason": verdict.get("reason"),
            }
            if verdict
            else None,
            "evaluations": [str(item.get("evaluation_id") or "") for item in record.get("evaluations") or []],
            "interrupted": bool(record.get("interrupted")),
        }

    # -- proposal ------------------------------------------------------------

    def propose(
        self,
        payload: Mapping[str, Any],
        *,
        coach: Any | None = None,
    ) -> dict[str, Any]:
        """Create a proposed experiment, from a recommendation or explicitly."""
        data = dict(payload or {})
        errors: list[str] = []
        recommendation: dict[str, Any] | None = None
        analysis: dict[str, Any] | None = None
        recommendation_id = str(data.get("recommendation_id") or "").strip()
        if recommendation_id:
            if coach is None:
                raise ExperimentError(
                    ["No Coach is attached, so a recommendation cannot be resolved."]
                )
            analysis = coach.analyse(
                session_id=str(data.get("session_id") or ""),
                evaluation_id=str(data.get("evaluation_id") or ""),
            )
            recommendation = next(
                (
                    item
                    for item in analysis.get("recommendations") or []
                    if str(item.get("recommendation_id")) == recommendation_id
                ),
                None,
            )
            if recommendation is None:
                raise ExperimentNotFoundError(
                    f"Recommendation {recommendation_id!r} is not part of this analysis."
                )

        subject = _mapping(data.get("subject"))
        if analysis is not None:
            subject = {
                "session_id": str(_mapping(analysis.get("subject")).get("session_id") or ""),
                "evaluation_id": str(
                    _mapping(analysis.get("subject")).get("evaluation_id") or ""
                ),
                "finding_id": str((recommendation or {}).get("finding_id") or ""),
                "recommendation_id": recommendation_id,
            }
        elif recommendation_id:
            subject = {
                **subject,
                "recommendation_id": recommendation_id,
            }

        evaluation = self._subject_evaluation(subject)
        benchmark_run = self._subject_run(subject, evaluation)
        configuration = _mapping(
            (evaluation or {}).get("configuration")
            or (benchmark_run or {}).get("configuration")
        )
        scenario = _mapping((evaluation or {}).get("scenario") or (benchmark_run or {}).get("scenario"))
        game = _mapping((evaluation or {}).get("game") or (benchmark_run or {}).get("game"))
        target = _mapping(
            ((evaluation or {}).get("identity") or (benchmark_run or {}).get("target"))
        )

        defaults = _mapping((recommendation or {}).get("experiment"))
        if recommendation is not None and not defaults.get("kind") and not data.get("kind"):
            raise ExperimentError(
                [
                    "This recommendation needs an operator change (or a capability this "
                    "build lacks) rather than a benchmark run, so it cannot become an "
                    "experiment."
                ]
            )
        requested_kind = str(data.get("kind") or defaults.get("kind") or "repeat").strip().lower()
        requested_variable = str(
            data.get("variable") or defaults.get("variable") or "none"
        ).strip().lower()
        game_id = str(data.get("game_id") or game.get("game_id") or "").strip()
        scenario_id = str(
            data.get("scenario_id") or scenario.get("scenario_id") or "observation-baseline"
        ).strip()
        scenario_version = data.get("scenario_version", data.get("version"))
        if scenario_version in (None, ""):
            scenario_version = scenario.get("version")
        primary_metric = str(
            data.get("primary_metric")
            or defaults.get("primary_metric")
            or (recommendation or {}).get("metric")
            or "frames"
        ).strip()
        resolved = resolve_scenario(scenario_id, scenario_version)
        # A finding can be about a symptom the subject's scenario does not
        # measure (an observation-baseline run records no capture_failures).
        # The recommendation's rule names the scenario that DOES measure its
        # metric, and the planner moves there only when the subject's scenario
        # cannot see the symptom. The metric is never swapped silently.
        scenario_switched = False
        if resolved is not None and primary_metric not in resolved.metrics:
            preferred = str(defaults.get("scenario_id") or "").strip()
            alternate = resolve_scenario(preferred) if preferred else None
            if alternate is not None and primary_metric in alternate.metrics:
                resolved = alternate
                scenario_id = alternate.scenario_id
                scenario_version = alternate.version
                scenario_switched = True
        if resolved is None:
            errors.append(f"Unknown benchmark scenario {scenario_id!r}.")
        version = resolved.version if resolved is not None else 0
        scenario_metrics = set(resolved.metrics) if resolved is not None else set()
        baseline_configuration = {
            "duration_seconds": int(
                data.get("baseline_duration_seconds")
                or configuration.get("duration_seconds")
                or (resolved.default_duration_seconds if resolved is not None else 0)
            ),
            "frame_interval_seconds": float(
                data.get("baseline_frame_interval_seconds")
                or configuration.get("frame_interval_seconds")
                or (resolved.frame_interval_seconds if resolved is not None else 1.0)
            ),
        }
        change = _mapping(data.get("change"))
        if not change and requested_kind == "configuration_change":
            if "duration_seconds" in data:
                change = {"duration_seconds": int(data["duration_seconds"])}
            elif "frame_interval_seconds" in data:
                change = {"frame_interval_seconds": float(data["frame_interval_seconds"])}
        threshold_raw = data.get("threshold", defaults.get("threshold"))
        threshold = (
            default_threshold(primary_metric)
            if threshold_raw in (None, "")
            else float(threshold_raw)
        )
        repeats = int(data.get("repeat_count") or (2 if requested_kind == "repeat" else 1))
        guardrails = data.get("guardrails")
        explicit_guardrails = guardrails is not None
        if guardrails is None:
            guardrails = GUARDRAIL_DEFAULTS.get(
                str((recommendation or {}).get("category") or ""), []
            )
        # A DEFAULT guardrail the chosen scenario cannot measure is dropped (and
        # the run's limitations say so). An EXPLICIT one is kept so validation can
        # refuse it with a reason instead of silently ignoring what was asked.
        guardrails = [
            dict(item)
            for item in guardrails or []
            if isinstance(item, Mapping)
            and (explicit_guardrails or str(item.get("metric") or "") in scenario_metrics)
        ]
        secondary = data.get("secondary_metrics")
        if secondary is None:
            secondary = [
                key
                for key in ("frames", "capture_failures", "latency_median_ms", "frame_yield")
                if key != primary_metric and (not scenario_metrics or key in scenario_metrics)
            ][:3]
        baseline_ids = [str(item) for item in data.get("baseline_run_ids") or []]
        if not baseline_ids and not scenario_switched and evaluation:
            candidate = str(evaluation.get("benchmark_run_id") or "")
            if candidate and self._eligible(candidate):
                baseline_ids = [candidate]
        if not baseline_ids and not scenario_switched and benchmark_run is not None:
            run_id = str(benchmark_run.get("id") or "")
            if run_id and self._eligible(run_id):
                baseline_ids = [run_id]
        if not change and requested_kind == "configuration_change":
            # A configuration change has to name a VALUE. When the recommendation
            # came from the incompatible-baseline category, both sides of the
            # refused comparison are RECORDED (the subject's own configuration and
            # the historical baseline NF-05 kept), so the change is taken from
            # them rather than invented or left as an empty block.
            derived_change, derived_override, derived_run, derived_reason = self._recorded_change(
                requested_variable, evaluation, configuration
            )
            if derived_change:
                change = derived_change
                baseline_configuration = {**baseline_configuration, **derived_override}
                if derived_run and derived_run not in baseline_ids:
                    baseline_ids = [derived_run]
            elif derived_reason:
                errors.append(derived_reason)
        abort = _mapping(data.get("abort"))
        duration = int(baseline_configuration.get("duration_seconds") or 0)
        abort.setdefault("max_seconds_per_arm", max(15.0, duration + ARM_SLACK_SECONDS))
        abort.setdefault("max_total_seconds", MAX_TOTAL_SECONDS)
        abort.setdefault(
            "cancel_on",
            [
                "The evaluation lab reports the scenario unsupported.",
                "The target window is lost or its identity changes.",
                "The operator cancels the experiment.",
            ],
        )

        game_version = str(
            data.get("game_version")
            or game.get("version")
            or _mapping((evaluation or {}).get("configuration")).get("game_version")
            or ""
        )
        spec = ExperimentSpec(
            experiment_id=_digest({"at": utc_now(), "subject": subject, "metric": primary_metric})[:12],
            created_at=utc_now(),
            subject=subject,
            game_id=game_id,
            game_version=game_version,
            scenario_id=scenario_id,
            scenario_version=int(version or 0),
            target_title=str(
                data.get("target_title") or target.get("title") or configuration.get("target_title") or ""
            ).strip(),
            kind=requested_kind,
            variable=requested_variable,
            baseline_configuration=baseline_configuration,
            change=change,
            baseline_run_ids=tuple(baseline_ids),
            repeat_count=repeats,
            primary_metric=primary_metric,
            threshold=threshold,
            secondary_metrics=tuple(str(item) for item in secondary),
            guardrails=tuple(
                dict(item) for item in guardrails or [] if isinstance(item, Mapping)
            ),
            confirm_offline=bool(
                data.get(
                    "confirm_offline",
                    _mapping((evaluation or {}).get("configuration")).get("confirm_offline"),
                )
            ),
            notes=str(data.get("notes") or ""),
            abort=abort,
            evidence_required=EVIDENCE_REQUIRED,
        )
        errors.extend(self.validate_spec(spec))
        now = utc_now()
        record: dict[str, Any] = {
            "version": EXPERIMENT_VERSION,
            "id": spec.experiment_id,
            "status": "blocked" if errors else "proposed",
            "reason": " ".join(errors),
            "errors": list(errors),
            "created_at": now,
            "started_at": "",
            "ended_at": "",
            "spec": spec.to_dict(),
            "identity": {
                key: target[key]
                for key in ("title", "handle", "pid")
                if target.get(key) not in (None, "")
            },
            "approval": None,
            "evaluations": [],
            "arms": {"baseline": [], "candidate": []},
            "results": None,
            "history": [
                {
                    "status": "blocked" if errors else "proposed",
                    "at": now,
                    "note": " ".join(errors) if errors else "Proposed; awaiting approval.",
                }
            ],
            "interrupted": False,
        }
        self._experiments[spec.experiment_id] = record
        self._trim()
        self._persist(verify_id=spec.experiment_id)
        self._publish_sync(
            "novaforge.experiment.proposed",
            {
                "experiment_id": spec.experiment_id,
                "status": record["status"],
                "kind": spec.kind,
                "variable": spec.variable,
                "primary_metric": spec.primary_metric,
            },
        )
        return dict(record)

    def validate_spec(self, spec: ExperimentSpec) -> list[str]:
        """Everything that would make the experiment unsupported, with reasons."""
        errors: list[str] = []
        if spec.kind not in EXPERIMENT_KINDS:
            errors.append(f"kind must be one of {', '.join(EXPERIMENT_KINDS)}.")
        if spec.variable not in VARIABLES:
            errors.append(f"variable must be one of {', '.join(VARIABLES)}.")
        if spec.kind == "repeat" and spec.variable != "none":
            errors.append("A repeat experiment changes no variable; set variable to 'none'.")
        if spec.kind == "configuration_change":
            if spec.variable == "none":
                errors.append(
                    "A configuration-change experiment must name the ONE variable it changes."
                )
            if set(spec.change) != {spec.variable}:
                errors.append(
                    "The change must set exactly the declared variable "
                    f"({spec.variable!r}); got {sorted(spec.change)!r}."
                )
            if spec.variable in {"duration_seconds", "frame_interval_seconds"}:
                unknown = set(spec.change) - {spec.variable}
                if unknown:  # pragma: no cover - the equality above already catches it
                    errors.append(f"The change also sets {sorted(unknown)!r}.")
        if not spec.game_id:
            errors.append(
                "A game or environment id is required: an experiment runs through the "
                "NF-05 evaluation lab, which needs one."
            )
        scenario = resolve_scenario(spec.scenario_id, spec.scenario_version or None)
        if scenario is None:
            errors.append(f"Unknown benchmark scenario {spec.scenario_id!r}.")
        else:
            if spec.scenario_version and spec.scenario_version != scenario.version:
                errors.append(
                    f"Scenario {spec.scenario_id!r} has no version {spec.scenario_version}."
                )
            if not scenario.supported:
                errors.append(
                    f"Scenario {scenario.key} is unsupported: {scenario.unsupported_reason}"
                )
        if not spec.target_title:
            errors.append("A target window title is required; identity is revalidated before the run.")
        measured = set(scenario.metrics) if scenario is not None else set()
        if (
            scenario is not None
            and spec.primary_metric in METRIC_DIRECTIONS
            and spec.primary_metric not in measured
        ):
            alternatives = sorted(
                item.key
                for item in SCENARIOS
                if item.supported and spec.primary_metric in set(item.metrics)
            )
            errors.append(
                f"Scenario {scenario.key} does not measure {spec.primary_metric!r}, so it "
                "cannot decide this experiment."
                + (f" Use {alternatives[0]!r}, which does." if alternatives else "")
            )
        for key in spec.secondary_metrics:
            if measured and key not in measured:
                errors.append(
                    f"Scenario {scenario.key if scenario is not None else spec.scenario_id!r} "
                    f"does not measure the secondary metric {key!r}."
                )
        for guardrail in spec.guardrails:
            metric = str(guardrail.get("metric") or "")
            if measured and metric and metric not in measured:
                errors.append(
                    f"Scenario {scenario.key if scenario is not None else spec.scenario_id!r} "
                    f"does not measure the guardrail metric {metric!r}, so the bound could "
                    "never be checked."
                )
        if spec.primary_metric not in METRIC_DIRECTIONS:
            errors.append(
                f"{spec.primary_metric!r} cannot decide an experiment; choose one of "
                f"{', '.join(PRIMARY_METRICS)} (each has a declared direction)."
            )
        if spec.secondary_metrics and spec.primary_metric in spec.secondary_metrics:
            errors.append("The primary metric cannot also be a secondary metric.")
        for key in spec.secondary_metrics:
            if key not in METRIC_DIRECTIONS and key not in {"timeline_events", "target_checks"}:
                errors.append(f"Unknown secondary metric {key!r}.")
        if spec.repeat_count < MIN_REPEATS or spec.repeat_count > MAX_REPEATS:
            errors.append(f"repeat_count must be between {MIN_REPEATS} and {MAX_REPEATS}.")
        if spec.threshold < 0:
            errors.append("threshold is an absolute delta and cannot be negative.")
        duration = spec.candidate_configuration().get("duration_seconds")
        if not isinstance(duration, int) or not (MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS):
            errors.append(
                f"duration_seconds must be an integer between {MIN_DURATION_SECONDS} and "
                f"{MAX_DURATION_SECONDS}; got {duration!r}."
            )
        interval = spec.candidate_configuration().get("frame_interval_seconds")
        interval_value = float("nan")
        if isinstance(interval, (int, float, str)) and not isinstance(interval, bool):
            try:
                interval_value = float(interval)
            except ValueError:
                interval_value = float("nan")
        if not (0.05 <= interval_value <= 10.0):
            errors.append(
                f"frame_interval_seconds must be between 0.05 and 10.0; got {interval!r}."
            )
        for guardrail in spec.guardrails:
            metric = str(guardrail.get("metric") or "")
            direction = str(guardrail.get("direction") or "")
            bound = _number(guardrail.get("bound"))
            if metric not in METRIC_DIRECTIONS:
                errors.append(f"Unknown guardrail metric {metric!r}.")
            if direction not in {"max", "min"}:
                errors.append(f"Guardrail {metric!r} needs direction 'max' or 'min'.")
            if bound is None:
                errors.append(f"Guardrail {metric!r} needs a numeric bound.")
        if spec.variable == "none" and spec.change:
            errors.append("A repeat experiment cannot declare a change.")
        for run_id in spec.baseline_run_ids:
            if not self._eligible(run_id):
                errors.append(
                    f"Baseline run {run_id!r} is not a completed, valid benchmark run on "
                    "this lab, so it cannot be a baseline."
                )
                continue
            run = self._run(run_id) or {}
            baseline_scenario = _mapping(run.get("scenario"))
            if (
                str(baseline_scenario.get("scenario_id") or "") != spec.scenario_id
                or int(baseline_scenario.get("version") or 0) != int(spec.scenario_version or 0)
            ):
                errors.append(
                    f"Baseline run {run_id!r} was recorded on "
                    f"{baseline_scenario.get('scenario_id')!r}@{baseline_scenario.get('version')} "
                    f"rather than {spec.scenario_id!r}@{spec.scenario_version}; a baseline is "
                    "only meaningful on the same scenario and version."
                )
        return errors

    # -- approval ------------------------------------------------------------

    async def approve(
        self, experiment_id: str, *, note: str = "", approved_by: str = "operator"
    ) -> dict[str, Any]:
        """Bind an approval to the CURRENT spec digest."""
        record = self._require(experiment_id)
        status = str(record.get("status") or "")
        if status in TERMINAL_STATUSES or status == "running":
            raise ExperimentError(
                [
                    f"Experiment {experiment_id} is {status}; a terminal or running "
                    "experiment cannot be approved. Propose a new experiment instead."
                ]
            )
        if status == "blocked" or record.get("errors"):
            raise ExperimentError(
                [
                    "This experiment is blocked and cannot be approved until its reasons "
                    "are resolved: "
                    + (" ".join(str(item) for item in record.get("errors") or []) or record.get("reason") or "")
                ]
            )
        spec = ExperimentSpec.from_dict(_mapping(record.get("spec")))
        digest = spec.digest()
        now = utc_now()
        record["approval"] = {
            "approved_at": now,
            "approved_by": str(approved_by or "operator"),
            "spec_digest": digest,
            "note": str(note or "")[:300],
        }
        record["status"] = "approved"
        record["reason"] = ""
        record["history"] = [
            *(record.get("history") or []),
            {"status": "approved", "at": now, "note": str(note or "")[:300]},
        ][-MAX_HISTORY:]
        self._persist(verify_id=experiment_id)
        self._publish_sync(
            "novaforge.experiment.approved",
            {"experiment_id": experiment_id, "spec_digest": digest},
        )
        return dict(record)

    async def reject(self, experiment_id: str, *, note: str = "") -> dict[str, Any]:
        record = self._require(experiment_id)
        status = str(record.get("status") or "")
        if status in TERMINAL_STATUSES or status == "running":
            raise ExperimentError([f"Experiment {experiment_id} is {status}; it cannot be rejected now."])
        now = utc_now()
        record["status"] = "rejected"
        record["reason"] = str(note or "Rejected by the operator.")
        record["ended_at"] = now
        record["history"] = [
            *(record.get("history") or []),
            {"status": "rejected", "at": now, "note": record["reason"]},
        ][-MAX_HISTORY:]
        self._persist(verify_id=experiment_id)
        return dict(record)

    # -- execution -----------------------------------------------------------

    async def run(self, experiment_id: str) -> dict[str, Any]:
        """Start the arms as a background task and return the claimed record."""
        record = self._require(experiment_id)
        status = str(record.get("status") or "")
        if status == "running":
            raise ExperimentConflictError(
                [f"Experiment {experiment_id} is already running; one runs at a time."]
            )
        if status == "blocked":
            errors = [str(item) for item in record.get("errors") or []]
            if errors:
                raise ExperimentError(
                    [
                        "This experiment is blocked and cannot run until its reasons are "
                        "resolved: " + " ".join(errors)
                    ]
                )
            # A readiness refusal at RUN time does not revoke the approval: the
            # configuration is unchanged, so reopening the window (or letting the
            # other session finish) is enough to try the same approved experiment
            # again. A proposal blocked for its own errors was never approved.
        elif status != "approved":
            raise ExperimentError(
                [
                    f"Experiment {experiment_id} is {status}; it must be approved before it "
                    "runs (approval is bound to this configuration)."
                ]
            )
        spec = ExperimentSpec.from_dict(_mapping(record.get("spec")))
        approved = str(_mapping(record.get("approval")).get("spec_digest") or "")
        digest = spec.digest()
        if not approved or approved != digest:
            raise ExperimentError(
                [
                    "The experiment configuration changed after it was approved, so the "
                    "approval no longer applies. Approve the current configuration again."
                ]
            )
        running = self._running()
        if running is not None and str(running.get("id")) != experiment_id:
            raise ExperimentConflictError(
                [f"Experiment {running.get('id')} is running; one experiment runs at a time."]
            )

        # Revalidation is a REFUSAL, not a warning: target identity, scenario
        # compatibility, budget and Guardian readiness are asked again, because
        # the world can change between approving and running.
        preflight = await self._preflight(spec, record)
        if not preflight["ready"]:
            record["status"] = "blocked"
            record["reason"] = " ".join(preflight["reasons"]) or "The experiment cannot run."
            record["preflight"] = preflight
            record["history"] = [
                *(record.get("history") or []),
                {"status": "blocked", "at": utc_now(), "note": record["reason"]},
            ][-MAX_HISTORY:]
            self._persist(verify_id=experiment_id)
            return dict(record)

        now = utc_now()
        record["status"] = "running"
        record["started_at"] = now
        record["preflight"] = preflight
        record["reason"] = ""
        record["history"] = [
            *(record.get("history") or []),
            {"status": "running", "at": now, "note": "Both arms started."},
        ][-MAX_HISTORY:]
        self._persist(verify_id=experiment_id)
        self._publish_sync(
            "novaforge.experiment.started",
            {"experiment_id": experiment_id, "arms": ["baseline", "candidate"]},
        )
        task = asyncio.create_task(self._execute(experiment_id))
        self._tasks[experiment_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(experiment_id, None))
        return dict(record)

    async def _preflight(
        self, spec: ExperimentSpec, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        reasons: list[str] = []
        checks: list[dict[str, Any]] = []
        budget = dict(spec.baseline_configuration)
        budget.update(spec.change)
        payload = {
            "game_id": spec.game_id,
            "game_version": spec.game_version or None,
            "scenario_id": spec.scenario_id,
            "scenario_version": spec.scenario_version or None,
            "mode": "observation_only",
            "target_title": spec.target_title,
            "duration_seconds": budget.get("duration_seconds"),
            "frame_interval_seconds": budget.get("frame_interval_seconds"),
            "confirm_offline": spec.confirm_offline,
        }
        rows = _mapping(record.get("arms"))
        baseline_ids = [
            str(item.get("benchmark_run_id") or "") for item in (rows.get("baseline") or [])
        ]
        # A pinned identity is what the operator measured; the evaluation lab
        # enforces it and refuses a title owned by a different window.
        identity = _mapping(record.get("identity"))
        for key in ("handle", "pid"):
            value = identity.get(key)
            if value not in (None, ""):
                payload[f"expected_{key}"] = value
        try:
            preflight = await self.evaluations.preflight(payload)
        except Exception as exc:  # noqa: BLE001 - a refusal, never a crash
            return {
                "ready": False,
                "reasons": [f"The readiness preflight could not run: {type(exc).__name__}: {exc}"],
                "checks": [],
                "evaluations": len(baseline_ids),
            }
        checks = [dict(item) for item in preflight.get("checks") or []]
        if not preflight.get("ready"):
            reasons.extend(str(item) for item in preflight.get("errors") or [])
            reasons.extend(
                f"{check.get('label')}: {check.get('reason')}"
                for check in checks
                if check.get("required") and check.get("status") != "pass" and check.get("reason")
            )
        permission = self._permission()
        if permission.get("checked") and not permission.get("allowed", True):
            reasons.append(
                "The permission layer refused this observation: "
                + str(permission.get("reason") or "no reason reported")
            )
        if not spec.baseline_run_ids and not reasons:
            # No recorded baseline means the first arm is the baseline. That is
            # allowed, and it is stated rather than hidden.
            pass
        return {
            "ready": not reasons,
            "reasons": reasons,
            "checks": checks,
            "blocking": list(preflight.get("blocking") or []),
            "target": _mapping(preflight.get("target")),
            "identity": _mapping(preflight.get("identity")),
            "budget": budget,
            "permission": permission,
            "note": (
                "Revalidated at run time: scenario/version, target geometry, budget, "
                "Guardian-owned controls and one-session-slot conflicts."
            ),
        }

    def _permission(self) -> dict[str, Any]:
        if self.permission is None:
            return {
                "checked": False,
                "allowed": True,
                "reason": (
                    "No permission layer is attached to this lab; the operator approval "
                    "and the evaluation lab's readiness gate are what authorize the run."
                ),
            }
        try:
            verdict = dict(self.permission() or {})
        except Exception as exc:  # noqa: BLE001 - a broken gate refuses, never crashes
            return {
                "checked": True,
                "allowed": False,
                "reason": f"The permission check raised {type(exc).__name__}: {exc}",
            }
        verdict.setdefault("checked", True)
        verdict.setdefault("allowed", bool(verdict.get("allowed", False)))
        return verdict

    async def _execute(self, experiment_id: str) -> None:
        record = self._experiments.get(experiment_id)
        if record is None:  # pragma: no cover - the record cannot vanish mid-run
            return
        record.setdefault("arms", {"baseline": [], "candidate": []})
        spec = ExperimentSpec.from_dict(_mapping(record.get("spec")))
        errors: list[str] = []
        overall_deadline = asyncio.get_running_loop().time() + min(
            float(_mapping(spec.abort).get("max_total_seconds") or MAX_TOTAL_SECONDS),
            MAX_TOTAL_SECONDS,
        )
        arms = (
            ("baseline", dict(spec.baseline_configuration)),
            ("candidate", spec.candidate_configuration()),
        )
        aborted = ""
        try:
            for arm_name, configuration in arms:
                if self._stopped(experiment_id):
                    aborted = self._cancelled.get(experiment_id) or "Cancelled by the operator."
                    break
                for index in range(spec.repeat_count):
                    if self._stopped(experiment_id):
                        aborted = self._cancelled.get(experiment_id) or "Cancelled by the operator."
                        break
                    if asyncio.get_running_loop().time() >= overall_deadline:
                        aborted = "The experiment exceeded its overall time budget."
                        break
                    outcome = await self._run_arm(
                        experiment_id, spec, arm_name, index, configuration, overall_deadline
                    )
                    record["arms"][arm_name].append(outcome)
                    record["evaluations"].append(
                        {
                            "evaluation_id": outcome.get("evaluation_id"),
                            "benchmark_run_id": outcome.get("benchmark_run_id"),
                            "arm": arm_name,
                            "status": outcome.get("status"),
                        }
                    )
                    self._persist(verify_id=experiment_id)
                    if outcome.get("stop_experiment"):
                        aborted = str(outcome.get("reason") or "")
                        break
                if aborted:
                    break
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            aborted = "The experiment task was cancelled by the application."
        except Exception as exc:  # noqa: BLE001 - a run failure is a recorded result
            errors.append(f"The experiment runner raised {type(exc).__name__}: {exc}")

        record = self._experiments.get(experiment_id)
        if record is None:  # pragma: no cover
            return
        record["results"] = self.assess(spec, record)
        if errors:
            record["errors"] = [*(record.get("errors") or []), *errors]
        if aborted:
            record["status"] = "cancelled" if self._stopped(experiment_id) else "failed"
            record["reason"] = aborted
            record["history"] = [
                *(record.get("history") or []),
                {"status": record["status"], "at": utc_now(), "note": aborted},
            ][-MAX_HISTORY:]
        else:
            started = [item for side in record["arms"].values() for item in side]
            if not started:
                record["status"] = "failed"
                record["reason"] = "No arm started, so no experiment was executed."
            else:
                record["status"] = "completed"
                record["reason"] = str(
                    _mapping(record["results"]).get("verdict", {}).get("reason") or "Completed."
                )
            record["history"] = [
                *(record.get("history") or []),
                {
                    "status": record["status"],
                    "at": utc_now(),
                    "note": record["reason"],
                },
            ][-MAX_HISTORY:]
        record["ended_at"] = utc_now()
        self._persist(verify_id=experiment_id)
        self._publish_sync(
            "novaforge.experiment.finished",
            {
                "experiment_id": experiment_id,
                "status": record["status"],
                "verdict": _mapping(_mapping(record.get("results")).get("verdict")).get("label"),
            },
        )

    def _stopped(self, experiment_id: str) -> bool:
        return experiment_id in self._cancelled

    async def _run_arm(
        self,
        experiment_id: str,
        spec: ExperimentSpec,
        arm_name: str,
        index: int,
        configuration: Mapping[str, Any],
        overall_deadline: float,
    ) -> dict[str, Any]:
        payload = {
            "confirm": True,
            "game_id": spec.game_id,
            "game_version": spec.game_version or None,
            "scenario_id": spec.scenario_id,
            "scenario_version": spec.scenario_version or None,
            "mode": "observation_only",
            "target_title": spec.target_title,
            "duration_seconds": configuration.get("duration_seconds"),
            "frame_interval_seconds": configuration.get("frame_interval_seconds"),
            "confirm_offline": spec.confirm_offline,
            # The note is IDENTICAL for every run of the experiment. NF-04 records
            # the configuration verbatim and treats the whole of it as the run's
            # basis, so a per-run "arm #2" note made two runs of the same
            # configuration look like a mixed selection and refused the
            # comparison. The arm and index live in this experiment's record.
            "notes": (f"NF-06 experiment {experiment_id}" + (f": {spec.notes}" if spec.notes else "")),
        }
        started = utc_now()
        outcome: dict[str, Any] = {
            "arm": arm_name,
            "index": index,
            "configuration": dict(configuration),
            "started_at": started,
            "evaluation_id": "",
            "benchmark_run_id": "",
            "session_id": "",
            "status": "not_started",
            "valid": False,
            "reason": "",
            "ended_at": "",
        }
        try:
            evaluation = await self.evaluations.start(payload)
        except Exception as exc:  # noqa: BLE001 - a refused start is a recorded outcome
            outcome.update(
                {
                    "status": "failed_to_start",
                    "reason": f"The evaluation did not start: {type(exc).__name__}: {exc}",
                    "ended_at": utc_now(),
                    "stop_experiment": True,
                }
            )
            return outcome
        evaluation_id = str(evaluation.get("id") or "")
        outcome["evaluation_id"] = evaluation_id
        outcome["benchmark_run_id"] = str(evaluation.get("benchmark_run_id") or "")
        outcome["session_id"] = str(evaluation.get("session_id") or "")
        self._active_evaluation[experiment_id] = evaluation_id
        try:
            final = await self._await_evaluation(
                evaluation_id,
                configuration,
                overall_deadline,
                experiment_id=experiment_id,
            )
        finally:
            self._active_evaluation.pop(experiment_id, None)
        outcome.update(
            {
                "status": str(final.get("status") or "unknown"),
                "valid": bool(final.get("valid")),
                "reason": str(final.get("reason") or ""),
                "benchmark_run_id": str(final.get("benchmark_run_id") or outcome["benchmark_run_id"]),
                "session_id": str(final.get("session_id") or outcome["session_id"]),
                "ended_at": str(final.get("ended_at") or utc_now()),
                "duration_seconds": final.get("duration_seconds"),
                "metrics": dict(final.get("metrics") or {}),
                "guardian": dict(final.get("guardian") or {}),
            }
        )
        if outcome["status"] == "unsupported":
            outcome["stop_experiment"] = True
            outcome["reason"] = (
                outcome["reason"]
                or "The evaluation lab reports this scenario unsupported in this environment."
            )
        return outcome

    async def _await_evaluation(
        self,
        evaluation_id: str,
        configuration: Mapping[str, Any],
        overall_deadline: float,
        *,
        experiment_id: str,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        duration = float(configuration.get("duration_seconds") or 0)
        deadline = min(
            loop.time() + duration + ARM_SLACK_SECONDS,
            overall_deadline,
        )
        while True:
            record = self.evaluations.get(evaluation_id)
            if record is None:
                return {
                    "status": "aborted",
                    "valid": False,
                    "reason": "The evaluation record disappeared while it was running.",
                }
            status = str(record.get("status") or "")
            if status != "running":
                return dict(record)
            if self._stopped(experiment_id):
                try:
                    await self.evaluations.cancel(evaluation_id)
                except Exception:  # noqa: BLE001 - cancellation is best-effort, then recorded
                    pass
                return {
                    **dict(record),
                    "status": "cancelled",
                    "reason": self._cancelled.get(experiment_id)
                    or "Cancelled by the operator.",
                }
            if loop.time() >= deadline:
                try:
                    await self.evaluations.cancel(evaluation_id)
                except Exception:  # noqa: BLE001 - the timeout is recorded either way
                    pass
                return {
                    **dict(record),
                    "status": "timed_out",
                    "valid": False,
                    "reason": (
                        "The arm did not finish within its declared budget plus the "
                        f"{ARM_SLACK_SECONDS:.0f}s finalize slack, so it was cancelled."
                    ),
                }
            await asyncio.sleep(POLL_SECONDS)

    async def wait(self, experiment_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Await completion for callers that are not a polling UI."""
        record = self._require(experiment_id)
        task = self._tasks.get(experiment_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:  # pragma: no cover - reported, not swallowed
                pass
        return self.get(experiment_id) or dict(record)

    async def cancel(self, experiment_id: str, *, reason: str = "") -> dict[str, Any]:
        record = self._require(experiment_id)
        status = str(record.get("status") or "")
        if status in TERMINAL_STATUSES:
            payload = dict(record)
            payload["already_stopped"] = True
            return payload
        note = str(reason or "Cancelled by the operator.")[:300]
        self._cancelled[experiment_id] = note
        active = self._active_evaluation.get(experiment_id)
        if active:
            try:
                await self.evaluations.cancel(active)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                note = f"{note} The active evaluation could not be cancelled: {exc}"
        if status != "running":
            now = utc_now()
            record["status"] = "cancelled"
            record["reason"] = note
            record["ended_at"] = now
            record["history"] = [
                *(record.get("history") or []),
                {"status": "cancelled", "at": now, "note": note},
            ][-MAX_HISTORY:]
            self._persist(verify_id=experiment_id)
        return dict(record)

    async def shutdown(self) -> None:
        for experiment_id, task in list(self._tasks.items()):
            self._cancelled.setdefault(experiment_id, "The application is shutting down.")
            task.cancel()
        self._tasks.clear()

    # -- comparison ----------------------------------------------------------

    def assess(
        self, spec: ExperimentSpec, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Judge the recorded arms. Never invents a figure it did not measure."""
        arms = _mapping(record.get("arms"))
        baseline_rows = [dict(item) for item in arms.get("baseline") or []]
        candidate_rows = [dict(item) for item in arms.get("candidate") or []]
        baseline_ids = [str(item.get("benchmark_run_id") or "") for item in baseline_rows]
        candidate_ids = [str(item.get("benchmark_run_id") or "") for item in candidate_rows]
        baseline_ids = [item for item in baseline_ids if item]
        candidate_ids = [item for item in candidate_ids if item]

        failed = [
            row
            for row in baseline_rows + candidate_rows
            if str(row.get("status") or "") not in {"completed"}
        ]
        interventions = 0
        for row in baseline_rows + candidate_rows:
            guardian = _mapping(row.get("guardian"))
            entries = guardian.get("interventions")
            interventions += len(entries) if isinstance(entries, list) else 0

        comparison: dict[str, Any] | None = None
        comparison_error = ""
        benchmark = self.benchmark
        if benchmark is None:
            comparison_error = (
                "No benchmark lab is attached, so the arms cannot be compared."
            )
        elif baseline_ids and candidate_ids:
            try:
                comparison = dict(benchmark.compare(baseline_ids, candidate_ids))
            except BenchmarkError as exc:
                comparison_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a refused comparison is a result
                comparison_error = f"The comparison could not be computed: {type(exc).__name__}: {exc}"
        else:
            comparison_error = (
                "No comparison is possible: "
                + ("the baseline arm " if not baseline_ids else "")
                + ("the candidate arm " if not candidate_ids else "")
                + "produced no benchmark run."
            ).strip()

        rows = {
            str(item.get("key")): dict(item) for item in (comparison or {}).get("metrics") or []
        }
        sample = {
            "baseline": len(baseline_ids),
            "candidate": len(candidate_ids),
            "eligible_baseline": sum(1 for item in baseline_rows if item.get("valid")),
            "eligible_candidate": sum(1 for item in candidate_rows if item.get("valid")),
        }
        compatible = bool((comparison or {}).get("compatible"))
        reasons = [str(item) for item in (comparison or {}).get("reasons") or []]
        if comparison_error and not comparison:
            reasons = [comparison_error]

        primary = self._metric_row(
            spec.primary_metric, rows, baseline_ids, candidate_ids, spec
        )
        guardrails = self._guardrails(spec, rows, candidate_ids)

        label, reason, limited = self._verdict(
            spec=spec,
            primary=primary,
            compatible=compatible,
            reasons=reasons,
            sample=sample,
            guardrails=guardrails,
        )
        limitations = [
            "No objective verifier exists in this build: a result describes observation "
            "quality and never a gameplay outcome.",
            "An arm is one bounded observation; the operator is responsible for the window "
            "and for the offline confirmation.",
        ]
        if spec.kind == "configuration_change" and not compatible:
            limitations.append(
                "The arms were recorded on different run configurations, so NF-04 refuses "
                "a pooled delta. The per-arm figures below are descriptions, not an effect."
            )
        if int(sample["eligible_baseline"]) < 2 or int(sample["eligible_candidate"]) < 2:
            limitations.append(
                "At least one arm has fewer than two eligible runs; a single measurement "
                "shows no variability, so no difference can be established from it."
            )
        if failed:
            limitations.append(
                f"{len(failed)} run(s) did not complete; they are listed and excluded from "
                "the comparison."
            )
        return {
            "version": EXPERIMENT_VERSION,
            "experiment_id": spec.experiment_id,
            "generated_at": utc_now(),
            "spec": spec.to_dict(),
            "arms": {"baseline": baseline_rows, "candidate": candidate_rows},
            "sample": sample,
            "comparison": (
                {
                    "available": True,
                    "compatible": compatible,
                    "reasons": reasons,
                    "warnings": [str(item) for item in (comparison or {}).get("warnings") or []],
                    "metrics": list((comparison or {}).get("metrics") or []),
                    "sample": dict((comparison or {}).get("sample") or {}),
                    "variability": dict((comparison or {}).get("variability") or {}),
                }
                if comparison
                else {
                    "available": False,
                    "compatible": False,
                    "reasons": reasons,
                    "warnings": [],
                    "metrics": [],
                    "sample": {},
                    "variability": {},
                }
            ),
            "primary": primary,
            "declared_baseline": {
                "run_ids": list(spec.baseline_run_ids),
                "pooled": False,
                "reason": (
                    "These recorded runs are the historical reference the proposal named. "
                    "They are NOT pooled into the comparison: the baseline arm is measured "
                    "now, on the same machine, so the two sides share one basis. A pooled "
                    "delta against older runs would mix environments."
                ),
            },
            "secondary": [
                self._metric_row(key, rows, baseline_ids, candidate_ids, spec)
                for key in spec.secondary_metrics
                if key != spec.primary_metric
            ],
            "guardrails": guardrails,
            "failed_runs": [
                {
                    "arm": row.get("arm"),
                    "index": row.get("index"),
                    "status": row.get("status"),
                    "reason": row.get("reason"),
                    "evaluation_id": row.get("evaluation_id"),
                }
                for row in failed
            ],
            "interventions": interventions,
            "verdict": {
                "label": label,
                "reason": reason,
                "improvement_claimed": label == "improved",
                "gameplay_claimed": False,
                "limited_to_single_run": limited,
                "objective_verified": False,
            },
            "limitations": limitations,
            "note": (
                "Verdicts: improved/regressed require a compatible comparison, at least two "
                "eligible runs per side, a delta beyond the declared threshold in the "
                "declared direction, and no guardrail breach. Anything else is inconclusive "
                "or not_comparable, with the reason stated."
            ),
        }

    def _metric_row(
        self,
        key: str,
        rows: Mapping[str, Mapping[str, Any]],
        baseline_ids: Sequence[str],
        candidate_ids: Sequence[str],
        spec: ExperimentSpec,
    ) -> dict[str, Any]:
        row = dict(rows.get(key) or {})
        left = _mapping(row.get("left"))
        right = _mapping(row.get("right"))
        delta = _number(row.get("delta"))
        source = "nf04_comparison"
        reason = str(row.get("delta_reason") or "")
        if not row:
            # NF-04 refused the comparison, so the only honest figures are each
            # arm's own recorded values, taken per run and summarized.
            left = self._descriptive(baseline_ids, key)
            right = self._descriptive(candidate_ids, key)
            source = "per_arm_description"
            if left.get("value") is not None and right.get("value") is not None:
                delta = round(float(right["value"]) - float(left["value"]), 3)
                reason = (
                    "Descriptive difference between the arms' own recorded values. NF-04 "
                    "did not certify these runs as one basis, so this is not an effect."
                )
            else:
                reason = "At least one arm did not measure this metric."
        elif delta is None:
            # NF-04 answered with a row but recorded NO delta (the arms are not one
            # comparable basis, or one arm did not measure this metric). Its figures
            # are each arm's own description, never a certified effect.
            source = "per_arm_description"
            reason = (
                (reason + " " if reason else "")
                + "The figures below are each arm's own recorded description, not an "
                "effect."
            )
        relative = None
        if (
            delta is not None
            and left.get("value") not in (None, 0)
            and _number(left.get("value")) not in (None, 0.0)
        ):
            relative = round(delta / float(left["value"]) * 100.0, 2)
        return {
            "key": key,
            "label": str(row.get("label") or metric_spec(key).label),
            "unit": str(row.get("unit") or metric_spec(key).unit),
            "direction": METRIC_DIRECTIONS.get(key, "increase"),
            "threshold": spec.threshold if key == spec.primary_metric else None,
            "baseline": left,
            "candidate": right,
            "delta": delta,
            "relative_percent": relative,
            "source": source,
            "reason": reason,
            "certified": source == "nf04_comparison" and delta is not None,
        }

    def _recorded_change(
        self,
        variable: str,
        evaluation: Mapping[str, Any] | None,
        subject_configuration: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        """A configuration change whose value comes from RECORDED runs.

        Returns ``(change, baseline override, baseline run id, refusal reason)``.
        The subject's run and the baseline it could not be compared with are both
        recorded, so a refusal here must mean the two do not differ on this
        variable — never that a number was guessed.
        """
        if variable not in VARIABLES or variable == "none":
            return {}, {}, "", ""
        subject_value = _number(subject_configuration.get(variable))
        if subject_value is None:
            return (
                {},
                {},
                "",
                f"The subject's own configuration does not record {variable!r}, so the "
                "value must be declared explicitly.",
            )
        baseline = _mapping((evaluation or {}).get("baseline"))
        baseline_configuration = _mapping(baseline.get("configuration"))
        baseline_run_id = str(baseline.get("benchmark_run_id") or "")
        if not baseline_configuration and baseline_run_id:
            baseline_configuration = _mapping(
                (self._run(baseline_run_id) or {}).get("configuration")
            )
        baseline_value = _number(baseline_configuration.get(variable))
        if baseline_value is None or baseline_value == subject_value:
            return (
                {},
                {},
                "",
                "No recorded baseline differs from this subject on "
                f"{variable!r}, so there is no measured value to change to; declare "
                "the experiment explicitly with the value that should be measured.",
            )
        cast = int if variable == "duration_seconds" else float
        return (
            {variable: cast(subject_value)},
            {variable: cast(baseline_value)},
            baseline_run_id,
            "",
        )

    def _descriptive(self, run_ids: Sequence[str], key: str) -> dict[str, Any]:
        values: list[float] = []
        labels = ""
        unit = ""
        for run_id in run_ids:
            run = self._run(run_id)
            metric = _mapping(_mapping((run or {}).get("metrics")).get(key))
            value = _number(metric.get("value")) if metric.get("available") else None
            labels = labels or str(metric.get("label") or "")
            unit = unit or str(metric.get("unit") or "")
            if value is not None:
                values.append(value)
        return {
            "value": _median(values),
            "samples": len(values),
            "values": values,
            "label": labels,
            "unit": unit,
            "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else None,
            "available": bool(values),
            "reason": (
                ""
                if values
                else "The recorded runs carry no value for this metric."
            ),
        }

    def _guardrails(
        self,
        spec: ExperimentSpec,
        rows: Mapping[str, Mapping[str, Any]],
        candidate_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for guardrail in spec.guardrails:
            key = str(guardrail.get("metric") or "")
            direction = str(guardrail.get("direction") or "max")
            bound = _number(guardrail.get("bound"))
            row = dict(rows.get(key) or {})
            right = _mapping(row.get("right"))
            value = _number(right.get("value"))
            if value is None:
                value = _number(self._descriptive(candidate_ids, key).get("value"))
            if value is None:
                results.append(
                    {
                        "metric": key,
                        "direction": direction,
                        "bound": bound,
                        "value": None,
                        "passed": None,
                        "reason": "The candidate arm did not measure this metric.",
                    }
                )
                continue
            violated = (bound is not None and value > bound) if direction == "max" else (
                bound is not None and value < bound
            )
            results.append(
                {
                    "metric": key,
                    "direction": direction,
                    "bound": bound,
                    "value": value,
                    "passed": not violated,
                    "reason": (
                        f"{key} = {value} breaches the {direction} bound {bound}."
                        if violated
                        else f"{key} = {value} is within the {direction} bound {bound}."
                    ),
                }
            )
        return results

    def _verdict(
        self,
        *,
        spec: ExperimentSpec,
        primary: Mapping[str, Any],
        compatible: bool,
        reasons: Sequence[str],
        sample: Mapping[str, Any],
        guardrails: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str, bool]:
        breaches = [item for item in guardrails if item.get("passed") is False]
        if breaches:
            names = ", ".join(
                f"{item.get('metric')} = {item.get('value')} (bound {item.get('bound')})"
                for item in breaches
            )
            return (
                "regressed",
                f"A regression guardrail was breached: {names}. A favourable primary "
                "delta does not outweigh a declared bound.",
                False,
            )
        if not compatible:
            joined = " ".join(str(item) for item in reasons) or "The comparison was refused."
            return "not_comparable", joined, False
        delta = _number(primary.get("delta"))
        if delta is None:
            return (
                "inconclusive",
                "The primary metric was not measured on both arms, so no difference can be read.",
                False,
            )
        # Eligibility, not the number of attempts, is what a second measurement
        # means: a run that failed or was invalidated is not variability, so the
        # gate counts runs whose record is valid. Counting attempts here once let
        # a falsified arm (2 tries, 1 eligible) claim an effect from a single value.
        baseline_n = int(sample.get("eligible_baseline", sample.get("baseline") or 0) or 0)
        candidate_n = int(sample.get("eligible_candidate", sample.get("candidate") or 0) or 0)
        limited = baseline_n < 2 or candidate_n < 2
        if limited:
            return (
                "inconclusive",
                "Fewer than two eligible runs on at least one arm "
                f"({baseline_n} baseline, {candidate_n} candidate). The measured difference "
                "is reported, but a single value shows no variability and cannot establish "
                "an effect.",
                True,
            )
        if spec.variable == "none" or spec.kind == "repeat":
            return (
                "inconclusive",
                "This experiment repeats one configuration; it declares no variable, so any "
                "difference is repeatability rather than an effect.",
                False,
            )
        if abs(delta) < spec.threshold:
            return (
                "inconclusive",
                f"The difference ({delta}) is within the declared threshold "
                f"({spec.threshold}) for {spec.primary_metric}.",
                False,
            )
        favourable = (
            delta > 0 if METRIC_DIRECTIONS.get(spec.primary_metric, "increase") == "increase"
            else delta < 0
        )
        if favourable:
            return (
                "improved",
                f"{spec.primary_metric} moved {delta} in the declared direction across "
                f"{baseline_n} eligible baseline and {candidate_n} eligible candidate "
                "run(s), beyond the declared threshold, with every guardrail intact. This "
                "describes observation quality, not a gameplay outcome.",
                False,
            )
        return (
            "regressed",
            f"{spec.primary_metric} moved {delta} against the declared direction across "
            f"{baseline_n} eligible baseline and {candidate_n} eligible candidate "
            "run(s), beyond the declared threshold.",
            False,
        )

    # -- helpers -------------------------------------------------------------

    def _run(self, run_id: str) -> dict[str, Any] | None:
        if not run_id or self.benchmark is None:
            return None
        try:
            run = self.benchmark.get(run_id)
        except Exception:  # noqa: BLE001 - an unreadable run is simply not used
            return None
        return dict(run) if run is not None else None

    def _eligible(self, run_id: str) -> bool:
        run = self._run(run_id)
        return bool(run and run.get("status") == "completed" and run.get("valid"))

    def _subject_evaluation(self, subject: Mapping[str, Any]) -> dict[str, Any] | None:
        evaluation_id = str(subject.get("evaluation_id") or "")
        if not evaluation_id:
            return None
        try:
            record = self.evaluations.get(evaluation_id)
        except Exception:  # noqa: BLE001 - a missing subject is not a proposal failure
            return None
        return dict(record) if record is not None else None

    def _subject_run(
        self, subject: Mapping[str, Any], evaluation: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        run_id = str((evaluation or {}).get("benchmark_run_id") or "")
        if not run_id:
            return None
        return self._run(run_id)

    def _publish_sync(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            result = self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - an announcer cannot fail a transition
            return
        if not asyncio.iscoroutine(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(result)


__all__ = [
    "ARM_SLACK_SECONDS",
    "EXPERIMENT_KINDS",
    "EXPERIMENT_STATUSES",
    "EXPERIMENTS_NAMESPACE",
    "EXPERIMENT_VERSION",
    "ExperimentConflictError",
    "ExperimentError",
    "ExperimentNotFoundError",
    "ExperimentSpec",
    "MAX_REPEATS",
    "METRIC_DIRECTIONS",
    "NovaForgeExperimentLab",
    "PRIMARY_METRICS",
    "VERDICTS",
    "VARIABLES",
    "default_threshold",
]
