"""NovaForge NF-05 — controlled, baseline-first game evaluation.

An evaluation is not a second runner. It composes two existing things:

* **NF-04's benchmark lab** owns the bounded observation session (start, deadline,
  cancellation, telemetry/timeline collection, persistence and restart recovery).
* **NF-05's compatibility registry** decides whether an environment may be
  evaluated at all, in which mode, and on what evidence.

This module adds the evaluation record: which game, which scenario, the readiness
checks that were judged, the benchmark run and session underneath, the baseline
it is compared with, and the per-game report. It starts nothing itself, sends no
input and never claims a verified objective.

Honesty rules:

* **A refusal is not a run.** A readiness failure is answered with its blocking
  checks; only an attempt against a KNOWN environment is retained (as
  ``unsupported`` or ``invalid``), because hiding attempted evaluations would be
  the same lie as hiding failed ones.
* **Baseline-first.** The first completed evaluation of a game/scenario/version is
  the baseline; later runs are candidates and are compared through NF-04's own
  compatibility rules. An incompatible baseline yields a refusal, never a delta.
* **COMPLETED is data completeness.** The objective block is always
  ``available: false`` with the reason, and ``verified`` is always false.
* **One session slot.** The benchmark lab enforces one active run, so an
  evaluation can never overlap another evaluation or a Mission Control session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from novacontrol.persistence.json_store import JsonStateStore

from .benchmark import (
    BenchmarkConflictError,
    BenchmarkNotFoundError,
    BenchmarkScenario,
    BenchmarkValidationError,
    resolve_scenario,
)
from .benchmark_lab import NovaForgeBenchmarkLab
from .games import (
    EVALUATION_MODES,
    EVALUATION_VERSION,
    EvaluationConflictError,
    EvaluationError,
    GameNotFoundError,
    GameRecord,
    evaluation_limitations,
    game_catalog,
    latest_game_version,
    missing_evidence,
    readiness_checks,
    readiness_summary,
    resolve_game,
)
from .manager import NovaForgeManager
from .models import utc_now

EVALUATIONS_NAMESPACE = "novaforge_evaluations"
MAX_EVALUATIONS_KEPT = 60

#: Evaluation statuses. ``unsupported`` is a retained refusal of a registered
#: environment whose compatibility status forbids running it.
EVALUATION_STATUSES = (
    "running",
    "completed",
    "failed",
    "aborted",
    "invalid",
    "unsupported",
)
TERMINAL_EVALUATION_STATUSES = frozenset(EVALUATION_STATUSES) - {"running"}

#: Benchmark status -> evaluation status. The two lifecycles are the same one;
#: this table is the only translation between their vocabularies.
_BENCHMARK_TO_EVALUATION = {
    "completed": "completed",
    "failed": "failed",
    "aborted": "aborted",
    "invalid": "invalid",
}

Publish = Callable[[str, dict[str, Any]], Awaitable[None]]


def _version_of(value: Any) -> int | None:
    """A stored version number, or ``None`` when the record does not carry one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _requested_mode(preflight: Mapping[str, Any]) -> str:
    """The mode the request ASKED for, when it is one this build knows.

    A retained record must describe the attempt that was made: a refused
    ``control`` evaluation recorded as ``observation_only`` would tell a reader
    of the history and the report that something was attempted which was not.
    An unrecognised value is not a mode and cannot be stored as one; the refusal
    reason already names the value the caller sent.
    """
    requested = str(preflight.get("mode") or "observation_only").strip().lower()
    return requested if requested in EVALUATION_MODES else "observation_only"


@dataclass(slots=True)
class _StartHandle:
    evaluation_id: str


class NovaForgeEvaluationLab:
    """Runs baseline-first evaluations over the NF-04 benchmark lab."""

    def __init__(
        self,
        *,
        benchmark: NovaForgeBenchmarkLab,
        manager: NovaForgeManager | None = None,
        store: JsonStateStore | None = None,
        publish: Publish | None = None,
        max_evaluations: int = MAX_EVALUATIONS_KEPT,
    ) -> None:
        self.benchmark = benchmark
        self.manager = manager if manager is not None else benchmark.manager
        self.store = (
            store
            if store is not None
            else getattr(benchmark, "store", None) or getattr(self.manager, "store", None)
        )
        self.publish = publish
        self.max_evaluations = max(1, int(max_evaluations))
        self._evaluations: dict[str, dict[str, Any]] = {}
        self._start_lock: asyncio.Lock | None = None
        self._load()

    # -- persistence ---------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        return self._start_lock

    def _load(self) -> None:
        payload: dict[str, Any] = {}
        if self.store is not None:
            try:
                payload = self.store.read(EVALUATIONS_NAMESPACE)
            except Exception:  # noqa: BLE001 - an unreadable store has no state
                payload = {}
        stored = payload.get("evaluations")
        if not isinstance(stored, list):
            return
        recovered = False
        for item in stored:
            if not isinstance(item, dict):
                continue
            evaluation_id = str(item.get("id") or "")
            game = item.get("game")
            if not evaluation_id or not isinstance(game, dict):
                continue
            record = dict(item)
            if record.get("status") not in EVALUATION_STATUSES:
                record["status"] = "invalid"
                record["valid"] = False
                record["reason"] = (
                    "The stored evaluation had an unrecognised status; it is preserved "
                    "as invalid."
                )
                recovered = True
            if _version_of((record.get("scenario") or {}).get("version")) is None:
                record["status"] = "invalid"
                record["valid"] = False
                record["reason"] = (
                    "The stored evaluation's scenario version is not an integer; it is "
                    "preserved as invalid."
                )
                recovered = True
            if record.get("status") == "running":
                # The process that owned the benchmark run is gone; its report may
                # be partial, but the evaluation is never resurrected as active.
                record["status"] = "aborted"
                record["valid"] = False
                record["interrupted"] = True
                record["ended_at"] = record.get("ended_at") or utc_now()
                record["reason"] = (
                    "The application restarted while this evaluation was active. Its "
                    "benchmark evidence is whatever was persisted before the restart; "
                    "the evaluation is incomplete and excluded from baselines."
                )
                recovered = True
            self._evaluations[evaluation_id] = record
        self._trim()
        if recovered:
            self._persist()

    def _persist(self, *, verify_id: str = "") -> None:
        record = self._evaluations.get(verify_id) if verify_id else None
        if self.store is None:
            if record is not None:
                record["persistence"] = {
                    "state": "not_persisted",
                    "reason": (
                        "No state store is attached; this evaluation exists in memory for "
                        "this process only."
                    ),
                }
            return
        self._trim()
        ordered = sorted(
            self._evaluations.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        try:
            self.store.write(
                EVALUATIONS_NAMESPACE,
                {"version": EVALUATION_VERSION, "evaluations": ordered},
            )
        except Exception as exc:  # noqa: BLE001 - recording cannot fail the work
            if record is not None:
                record["persistence"] = {
                    "state": "failed",
                    "reason": f"The store raised {type(exc).__name__}: {exc}",
                }
            return
        if record is None:
            return
        try:
            stored = self.store.read(EVALUATIONS_NAMESPACE)
        except Exception as exc:  # noqa: BLE001 - verification must not raise either
            record["persistence"] = {
                "state": "failed",
                "reason": f"The store could not be read back: {type(exc).__name__}: {exc}",
            }
            return
        rows = stored.get("evaluations") if isinstance(stored, dict) else None
        present = isinstance(rows, list) and any(
            isinstance(item, dict) and str(item.get("id")) == verify_id for item in rows
        )
        record["persistence"] = {
            "state": "verified" if present else "failed",
            "reason": ""
            if present
            else (
                "The store accepted the write but the evaluation is not readable back; a "
                "later write may replace it."
            ),
        }

    def _trim(self) -> None:
        if len(self._evaluations) <= self.max_evaluations:
            return
        ordered = sorted(
            self._evaluations.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        keep = {str(record.get("id")) for record in ordered[: self.max_evaluations]}
        for evaluation_id in list(self._evaluations):
            if evaluation_id not in keep:
                del self._evaluations[evaluation_id]

    # -- read surfaces -------------------------------------------------------

    def games(self) -> dict[str, Any]:
        return game_catalog()

    def game(self, game_id: str, version: Any = None) -> dict[str, Any]:
        game = resolve_game(game_id, version)
        if game is None:
            raise GameNotFoundError(
                f"Unknown game or environment: {game_id!r} {version!r}"
            )
        payload = game.to_dict()
        payload["scenarios"] = [
            {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "name": scenario.name,
                "default_duration_seconds": scenario.default_duration_seconds,
                "frame_interval_seconds": scenario.frame_interval_seconds,
                "min_frames": scenario.min_frames,
                "min_latency_samples": scenario.min_latency_samples,
            }
            for scenario in self._scenarios_for(game)
        ]
        payload["validation"] = game.validate()
        payload["limitations"] = (
            evaluation_limitations(game, self._scenarios_for(game)[0])
            if self._scenarios_for(game)
            else list(game.known_limitations)
        )
        return payload

    def _scenarios_for(self, game: GameRecord) -> list[BenchmarkScenario]:
        found: list[BenchmarkScenario] = []
        for scenario_id in game.benchmark.get("scenarios") or ():
            scenario = resolve_scenario(str(scenario_id))
            if scenario is not None and scenario.supported:
                found.append(scenario)
        return found

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        record = self._evaluations.get(evaluation_id)
        if record is None:
            return None
        self._sync(record)
        return dict(record)

    def history(
        self, *, limit: int = 20, game_id: str = ""
    ) -> dict[str, Any]:
        for record in list(self._evaluations.values()):
            self._sync(record)
        ordered = sorted(
            self._evaluations.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        if game_id:
            ordered = [
                item
                for item in ordered
                if str((item.get("game") or {}).get("game_id")) == game_id
            ]
        limit = max(1, min(int(limit), 100))
        rows = [self._summary(record) for record in ordered[:limit]]
        return {
            "version": EVALUATION_VERSION,
            "evaluations": rows,
            "count": len(rows),
            "total": len(ordered),
            "retention": {
                "cap": self.max_evaluations,
                "retained": len(self._evaluations),
                "note": (
                    "History is bounded; the oldest records are dropped at the cap. Every "
                    "retained attempt is listed, including failed and unsupported ones."
                ),
            },
            "note": (
                "Only COMPLETED and valid evaluations enter a baseline or a comparison. A "
                "completed evaluation is data completeness, not an achieved objective."
            ),
        }

    # -- preflight -----------------------------------------------------------

    async def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload or {})
        errors: list[str] = []
        game_id = str(data.get("game_id", "") or "").strip()
        if not game_id:
            errors.append("A game or environment id is required.")
        game = resolve_game(game_id, data.get("game_version"))
        if game is None and game_id:
            known = latest_game_version(game_id)
            errors.append(
                f"Unknown game or environment {game_id!r}."
                + (f" The registry's latest version is {known}." if known else "")
            )
        mode = str(data.get("mode", "") or "observation_only").strip().lower()
        if mode not in {"observation_only", "control"}:
            errors.append("mode must be 'observation_only' or 'control'.")
        scenario_id = str(data.get("scenario_id", "") or "").strip() or "observation-baseline"
        scenario = resolve_scenario(scenario_id, data.get("scenario_version"))
        if scenario is None:
            errors.append(f"Unknown benchmark scenario {scenario_id!r}.")

        benchmark_preflight: dict[str, Any] = {}
        if game is not None and scenario is not None and not errors:
            if not game.supports_scenario(scenario.scenario_id):
                errors.append(
                    f"{game.display_name!r} does not declare scenario {scenario.scenario_id!r}."
                )
            if not game.runnable:
                errors.append(game.status_reason)
            benchmark_request = {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "target_title": data.get("target_title"),
                "duration_seconds": data.get("duration_seconds"),
                "frame_interval_seconds": data.get("frame_interval_seconds"),
                "notes": data.get("notes", ""),
            }
            for key in ("expected_handle", "expected_pid"):
                if data.get(key) not in (None, ""):
                    benchmark_request[key] = data.get(key)
            try:
                benchmark_preflight = await self.benchmark.preflight(benchmark_request)
            except Exception as exc:  # noqa: BLE001 - preflight must answer, not raise
                benchmark_preflight = {
                    "valid": False,
                    "errors": [f"The benchmark preflight could not run: {type(exc).__name__}: {exc}"],
                    "warnings": [],
                    "target": None,
                    "conflicts": [],
                }

        target = dict(benchmark_preflight.get("target") or {}) or None
        conflicts = list(benchmark_preflight.get("conflicts") or [])
        benchmark_errors = [str(item) for item in benchmark_preflight.get("errors") or []]
        checks: list[dict[str, Any]] = []
        if game is not None and scenario is not None:
            try:
                checks = readiness_checks(
                    game=game,
                    mode=mode,
                    scenario=scenario,
                    target=target,
                    benchmark_valid=bool(benchmark_preflight.get("valid")),
                    benchmark_errors=benchmark_errors,
                    benchmark_warnings=[
                        str(item) for item in benchmark_preflight.get("warnings") or []
                    ],
                    capture_supported=bool(getattr(self.manager, "capture_supported", False)),
                    stop_available=all(
                        callable(getattr(self.manager, name, None))
                        for name in ("stop", "cancel", "emergency_stop")
                    ),
                    cleanup_available=callable(getattr(self.manager, "release_inputs", None)),
                    store_available=self.store is not None,
                    confirm_offline=bool(data.get("confirm_offline")),
                    conflicts=conflicts,
                    expected_handle=data.get("expected_handle"),
                    expected_pid=data.get("expected_pid"),
                )
            except Exception as exc:  # noqa: BLE001 - a fault refuses the run, never a 500
                # A registry record that violates a check's own invariant must
                # become a BLOCKING row with the fault named. Refusing is the
                # only honest outcome; a crash here would answer a readiness
                # question with an error page and no reasons at all.
                checks = [
                    {
                        "id": "readiness_check_builder",
                        "label": "Readiness assessment",
                        "status": "fail",
                        "required": True,
                        "evidence": "",
                        "reason": (
                            "The readiness assessment could not be built for this "
                            f"environment: {type(exc).__name__}: {exc}"
                        ),
                    }
                ]
        summary: dict[str, Any] = readiness_summary(checks) if checks else {
            "ready": False,
            "blocking": ["compatibility"],
            "counts": {},
            "checks": [],
            "note": "The request could not be evaluated, so no readiness check ran.",
        }
        baseline = self.baseline_payload(game, scenario) if game and scenario else None
        return {
            "version": EVALUATION_VERSION,
            "ready": bool(summary["ready"] and not errors),
            "errors": errors,
            "blocking": list(summary["blocking"]),
            "checks": list(summary["checks"]),
            "counts": dict(summary["counts"]),
            "game": game.to_dict() if game is not None else None,
            "scenario": scenario.to_dict() if scenario is not None else None,
            "mode": mode,
            "benchmark": benchmark_preflight,
            "target": target,
            "identity": (
                {
                    "title": target.get("title"),
                    "handle": target.get("handle"),
                    "pid": target.get("pid"),
                }
                if target
                else None
            ),
            "baseline": baseline,
            "limitations": (
                evaluation_limitations(game, scenario)
                if game is not None and scenario is not None
                else ["No environment could be resolved, so nothing can be evaluated."]
            ),
            "note": (
                "Preflight starts nothing. Start requires an explicit confirmation and a "
                "passing readiness check."
            ),
        }

    # -- baseline ------------------------------------------------------------

    def _completed_for(
        self, game_id: str, scenario_id: str, version: int
    ) -> list[dict[str, Any]]:
        completed = [
            record
            for record in self._evaluations.values()
            if record.get("status") == "completed"
            and bool(record.get("valid"))
            and str((record.get("game") or {}).get("game_id")) == game_id
            and str((record.get("scenario") or {}).get("scenario_id")) == scenario_id
            and _version_of((record.get("scenario") or {}).get("version")) == version
        ]
        return sorted(completed, key=lambda item: str(item.get("created_at") or ""))

    def baseline_payload(
        self, game: GameRecord | None, scenario: Any
    ) -> dict[str, Any]:
        """The current baseline for one environment/scenario/version."""
        if game is None or scenario is None:
            return {
                "available": False,
                "reason": "No environment or scenario was resolved.",
                "evaluation_id": "",
                "benchmark_run_id": "",
            }
        completed = self._completed_for(game.game_id, scenario.scenario_id, scenario.version)
        if not completed:
            return {
                "available": False,
                "role": "baseline",
                "reason": (
                    "No completed evaluation exists yet; the first completed evaluation of "
                    "this environment and scenario becomes the baseline."
                ),
                "evaluation_id": "",
                "benchmark_run_id": "",
                "configuration": None,
                "window": None,
                "samples": 0,
            }
        reference = completed[0]
        benchmark = self.benchmark.get(str(reference.get("benchmark_run_id") or "")) or {}
        return {
            "available": True,
            "role": "candidate",
            "reason": "This completed evaluation is the baseline for its environment and scenario.",
            "evaluation_id": str(reference.get("id")),
            "benchmark_run_id": str(reference.get("benchmark_run_id") or ""),
            "created_at": reference.get("created_at"),
            "configuration": dict(reference.get("configuration") or {}),
            "window": dict((benchmark.get("environment") or {}).get("window") or {}),
            "samples": len(completed),
            "policy": (
                "The OLDEST completed, valid evaluation of the same environment, scenario and "
                "version is the baseline. A baseline recorded on a different configuration, "
                "target geometry or operating system is reported as incompatible, not pooled."
            ),
        }

    def baseline(self, game_id: str, scenario_id: str, version: Any = None) -> dict[str, Any]:
        game = resolve_game(game_id)
        if game is None:
            raise GameNotFoundError(f"Unknown game or environment: {game_id!r}")
        scenario = resolve_scenario(scenario_id, version)
        if scenario is None:
            raise GameNotFoundError(f"Unknown benchmark scenario: {scenario_id!r}")
        payload = self.baseline_payload(game, scenario)
        payload["game"] = game.to_dict()
        payload["scenario"] = scenario.to_dict()
        return payload

    def _explicit_baseline(
        self, baseline_id: str, game: GameRecord, scenario: Any
    ) -> dict[str, Any]:
        record = self._evaluations.get(baseline_id)
        if record is None:
            raise EvaluationError([f"Unknown baseline evaluation: {baseline_id}"])
        if record.get("status") != "completed" or not record.get("valid"):
            raise EvaluationError(
                [
                    f"Baseline {baseline_id} is {record.get('status')} and cannot be a "
                    "baseline; only completed valid evaluations can."
                ]
            )
        same_game = str((record.get("game") or {}).get("game_id")) == game.game_id
        same_scenario = (
            str((record.get("scenario") or {}).get("scenario_id")) == scenario.scenario_id
            and _version_of((record.get("scenario") or {}).get("version")) == scenario.version
        )
        if not same_game or not same_scenario:
            raise EvaluationError(
                [
                    "The selected baseline belongs to a different environment or scenario; "
                    "a baseline is only meaningful for the same one."
                ]
            )
        return {
            "available": True,
            "role": "candidate",
            "explicit": True,
            "evaluation_id": str(record.get("id")),
            "benchmark_run_id": str(record.get("benchmark_run_id") or ""),
            "created_at": record.get("created_at"),
            "configuration": dict(record.get("configuration") or {}),
            "reason": "Explicitly selected baseline.",
            "policy": "An explicitly selected baseline must be completed and valid.",
        }

    # -- start ---------------------------------------------------------------

    async def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, then start ONE evaluation through the benchmark lab.

        A readiness refusal creates a retained ``invalid`` (or ``unsupported``)
        record for a KNOWN environment, then raises: an attempted evaluation is
        evidence, and hiding it would make the history disagree with what the
        operator saw.
        """
        data = dict(payload or {})
        # Explicit confirmation is a request fact, not a UI courtesy: an API caller
        # must state that a real observation session may start.
        if data.get("confirm") is not True:
            raise EvaluationError(
                ["An evaluation must be explicitly confirmed (confirm: true) before it starts."]
            )
        preflight = await self.preflight(data)
        game = resolve_game(str(data.get("game_id", "") or ""), data.get("game_version"))
        scenario = resolve_scenario(
            str(data.get("scenario_id", "") or "observation-baseline"),
            data.get("scenario_version"),
        )
        if game is None or scenario is None:
            raise EvaluationError(preflight["errors"] or ["The request cannot be evaluated."])

        if not preflight["ready"]:
            blocking = [
                check
                for check in preflight["checks"]
                if check.get("required") and check.get("status") != "pass"
            ]
            reasons = list(preflight["errors"]) + [
                f"{check['label']}: {check['reason']}"
                for check in blocking
                if check.get("reason")
            ]
            status = "unsupported" if not game.runnable else "invalid"
            self._record_refusal(
                game=game,
                scenario=scenario,
                payload=data,
                preflight=preflight,
                status=status,
                reasons=reasons,
            )
            # A conflict is answered as a CONFLICT: the same request would be
            # valid once the active run/session ends.
            if "conflicts" in set(preflight.get("blocking") or []):
                raise EvaluationConflictError(reasons or ["Another run is active."])
            raise EvaluationError(reasons or ["The evaluation did not pass preflight."])

        baseline: dict[str, Any]
        explicit = str(data.get("baseline_id", "") or "").strip()
        if explicit:
            baseline = self._explicit_baseline(explicit, game, scenario)
        else:
            baseline = self.baseline_payload(game, scenario)
            if not baseline.get("available"):
                baseline = {**baseline, "role": "baseline"}

        async with self._lock():
            conflicts = [
                item
                for item in (
                    self._active_evaluation(),
                    self.benchmark.active_run(),
                    self.manager.active(),
                )
                if item is not None
            ]
            if conflicts:
                reason = (
                    "Another evaluation or NovaForge session is active; one session slot is "
                    "shared, so nothing can start until it ends."
                )
                self._record_refusal(
                    game=game,
                    scenario=scenario,
                    payload=data,
                    preflight=preflight,
                    status="invalid",
                    reasons=[reason],
                )
                raise EvaluationConflictError([reason])

            evaluation_id = uuid4().hex
            now = utc_now()
            record: dict[str, Any] = {
                "version": EVALUATION_VERSION,
            "id": evaluation_id,
            "game": game.to_dict(),
            "mode": _requested_mode(preflight),
            "scenario": {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "name": scenario.name,
                "evaluation": scenario.evaluation,
            },
            "configuration": {
                "game_id": game.game_id,
                "game_version": game.version,
                "scenario_id": scenario.scenario_id,
                "scenario_version": scenario.version,
                "target_title": str(data.get("target_title") or "").strip(),
                    "duration_seconds": self._effective(data, preflight, "duration_seconds"),
                    "frame_interval_seconds": self._effective(
                        data, preflight, "frame_interval_seconds"
                    ),
                    "notes": str(data.get("notes") or "").strip(),
                    "confirm_offline": bool(data.get("confirm_offline")),
                    "confirmed": True,
                },
                "readiness": {
                    "ready": True,
                    "counts": dict(preflight["counts"]),
                    "checks": [dict(check) for check in preflight["checks"]],
                },
                "identity": dict(preflight.get("identity") or {}),
                "status": "running",
                "valid": False,
                "reason": "",
                "benchmark_run_id": "",
                "session_id": "",
                "baseline": baseline,
                "comparison": None,
                "objective": {
                    "available": False,
                    "verified": False,
                    "achieved": None,
                    "reason": (
                        "No objective verifier is attached to a NovaForge session. A "
                        "completed evaluation means its data was recorded, never that the "
                        "game objective was achieved."
                    ),
                },
                "metrics": {},
                "evidence": {
                    "benchmark_run": "",
                    "session": "",
                    "timeline": "",
                    "telemetry": "",
                    "report": f"/novaforge/evaluations/{evaluation_id}/report",
                    "frames": [],
                    "gaps": ["The evaluation has not finished recording evidence yet."],
                },
                "guardian": {},
                "classification": {},
                "errors": [],
                "limitations": evaluation_limitations(game, scenario),
                "interrupted": False,
                "created_at": now,
                "started_at": now,
                "ended_at": "",
                "duration_seconds": None,
            }
            self._evaluations[evaluation_id] = record
            self._trim()
            self._persist()
            benchmark_payload = {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "target_title": str(data.get("target_title") or "").strip(),
                "duration_seconds": data.get("duration_seconds"),
                "frame_interval_seconds": data.get("frame_interval_seconds"),
                "notes": str(data.get("notes") or "").strip(),
            }
            identity = dict(preflight.get("identity") or {})
            # A caller-pinned identity is the one that must be enforced: it is what
            # the operator measured, and the manager refuses a title now owned by a
            # different window. Without a pin the freshly validated identity is used.
            if data.get("expected_handle") not in (None, ""):
                identity["handle"] = data["expected_handle"]
            if data.get("expected_pid") not in (None, ""):
                identity["pid"] = data["expected_pid"]
            if identity.get("handle") not in (None, ""):
                benchmark_payload["expected_handle"] = identity["handle"]
            if identity.get("pid") not in (None, ""):
                benchmark_payload["expected_pid"] = identity["pid"]
            record["identity"] = identity
            try:
                run = await self.benchmark.start(benchmark_payload)
            except BenchmarkConflictError as exc:
                self._finish_without_run(record, status="invalid", reason=str(exc))
                raise
            except BenchmarkValidationError as exc:
                self._finish_without_run(record, status="invalid", reason=str(exc))
                raise
            except Exception as exc:  # noqa: BLE001 - a start failure is a retained attempt
                self._finish_without_run(
                    record,
                    status="invalid",
                    reason=f"Benchmark start raised {type(exc).__name__}: {exc}",
                )
                raise EvaluationError([record["reason"]]) from exc

            record["benchmark_run_id"] = str(run.get("id") or "")
            record["session_id"] = str(run.get("session_id") or "")
            record["evidence"] = {
                **record["evidence"],
                "benchmark_run": f"/novaforge/benchmarks/runs/{record['benchmark_run_id']}",
                "session": (
                    f"/novaforge/sessions/{record['session_id']}"
                    if record["session_id"]
                    else ""
                ),
                "timeline": f"/novaforge/sessions/{record['session_id']}/timeline"
                if record["session_id"]
                else "",
                "telemetry": f"/novaforge/sessions/{record['session_id']}/telemetry"
                if record["session_id"]
                else "",
                "gaps": ["The evaluation is running; evidence is finalized when it ends."],
            }
            record["limitations"] = evaluation_limitations(game, scenario)
            self._persist(verify_id=evaluation_id)

        await self._publish(
            "novaforge.evaluation.started",
        {
            "evaluation_id": evaluation_id,
            "game_id": game.game_id,
            "game_version": game.version,
            "scenario": scenario.key,
            "benchmark_run_id": record["benchmark_run_id"],
            "session_id": record["session_id"],
            "mode": str(record.get("mode") or "observation_only"),
        },
        )
        return dict(record)

    @staticmethod
    def _effective(
        payload: Mapping[str, Any], preflight: Mapping[str, Any], key: str
    ) -> Any:
        """The value the run will actually use, not the gap the caller left.

        An API caller may omit the budget or the observation interval; the
        benchmark lab resolves its own defaults, so the evaluation record must
        carry those resolved numbers — otherwise a run that measured 10 s would
        be persisted as ``duration_seconds: null`` and its baseline would
        describe a configuration that never ran.
        """
        value = payload.get(key)
        if value not in (None, ""):
            return value
        config = (preflight.get("benchmark") or {}).get("config") or {}
        return config.get(key)

    def _active_evaluation(self) -> dict[str, Any] | None:
        for record in self._evaluations.values():
            if record.get("status") == "running":
                return record
        return None

    def _record_refusal(
        self,
        *,
        game: GameRecord,
        scenario: Any,
        payload: Mapping[str, Any],
        preflight: Mapping[str, Any],
        status: str,
        reasons: Sequence[str],
    ) -> dict[str, Any]:
        now = utc_now()
        record: dict[str, Any] = {
            "version": EVALUATION_VERSION,
            "id": uuid4().hex,
            "game": game.to_dict(),
            "mode": _requested_mode(preflight),
            "scenario": {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "name": scenario.name,
                "evaluation": scenario.evaluation,
            },
            "configuration": {
                "game_id": game.game_id,
                "game_version": game.version,
                "scenario_id": scenario.scenario_id,
                "scenario_version": scenario.version,
                "target_title": str(payload.get("target_title") or "").strip(),
                "duration_seconds": self._effective(payload, preflight, "duration_seconds"),
                "frame_interval_seconds": self._effective(
                    payload, preflight, "frame_interval_seconds"
                ),
                "notes": str(payload.get("notes") or "").strip(),
                "confirm_offline": bool(payload.get("confirm_offline")),
            },
            "readiness": {
                "ready": False,
                "counts": dict(preflight.get("counts") or {}),
                "checks": [dict(check) for check in preflight.get("checks") or []],
            },
            "identity": dict(preflight.get("identity") or {}),
            "status": status,
            "valid": False,
            "reason": " ".join(str(item) for item in reasons) or "The evaluation was refused.",
            "benchmark_run_id": "",
            "session_id": "",
            "baseline": dict(preflight.get("baseline") or {}) or None,
            "comparison": None,
            "objective": {
                "available": False,
                "verified": False,
                "achieved": None,
                "reason": "The evaluation did not run, so no objective was evaluated.",
            },
            "metrics": {},
            "evidence": {
                "benchmark_run": "",
                "session": "",
                "timeline": "",
                "telemetry": "",
                "report": "",
                "frames": [],
                "gaps": ["The evaluation never started, so it recorded no evidence."],
            },
            "guardian": {},
            "classification": {},
            "errors": [],
            "limitations": evaluation_limitations(game, scenario),
            "interrupted": False,
            "created_at": now,
            "started_at": now,
            "ended_at": now,
            "duration_seconds": None,
        }
        self._evaluations[record["id"]] = record
        self._trim()
        self._persist(verify_id=record["id"])
        return record

    def _finish_without_run(self, record: dict[str, Any], *, status: str, reason: str) -> None:
        record["status"] = status
        record["valid"] = False
        record["reason"] = reason
        record["ended_at"] = record.get("ended_at") or utc_now()
        record["evidence"]["gaps"] = ["The evaluation never started, so it recorded no evidence."]
        self._persist(verify_id=str(record.get("id")))

    # -- synchronisation -----------------------------------------------------

    def _sync(self, record: dict[str, Any]) -> None:
        """Read the benchmark run's ending into the evaluation, once it is terminal."""
        if record.get("status") != "running":
            return
        benchmark_id = str(record.get("benchmark_run_id") or "")
        run = self.benchmark.get(benchmark_id) if benchmark_id else None
        if run is None:
            record["status"] = "aborted"
            record["valid"] = False
            record["reason"] = (
                "The benchmark run record for this evaluation is no longer available, so "
                "the attempt is incomplete and excluded from baselines."
            )
            record["ended_at"] = record.get("ended_at") or utc_now()
            self._persist(verify_id=str(record.get("id")))
            return
        if run.get("status") == "running":
            record["metrics"] = dict(run.get("metrics") or {})
            record["session_id"] = str(run.get("session_id") or record.get("session_id") or "")
            return

        mapped = _BENCHMARK_TO_EVALUATION.get(str(run.get("status")), "invalid")
        record["status"] = mapped
        record["valid"] = mapped == "completed" and bool(run.get("valid"))
        record["reason"] = (
            f"Benchmark run {benchmark_id} ended {run.get('status')}: "
            f"{run.get('reason') or 'no reason recorded'}"
        )
        record["ended_at"] = str(run.get("ended_at") or utc_now())
        record["duration_seconds"] = run.get("duration_seconds")
        record["metrics"] = dict(run.get("metrics") or {})
        record["session_id"] = str(run.get("session_id") or record.get("session_id") or "")
        record["guardian"] = dict(run.get("guardian") or {})
        record["classification"] = dict(run.get("classification") or {})
        record["errors"] = [dict(item) for item in run.get("errors") or []]
        record["limitations"] = list(
            dict.fromkeys(
                [str(item) for item in record.get("limitations") or []]
                + [str(item) for item in run.get("limitations") or []]
            )
        )
        evidence = dict(run.get("evidence") or {})
        record["evidence"] = {
            "benchmark_run": f"/novaforge/benchmarks/runs/{benchmark_id}",
            "session": f"/novaforge/sessions/{record['session_id']}"
            if record["session_id"]
            else "",
            "timeline": str(evidence.get("timeline_endpoint") or ""),
            "telemetry": str(evidence.get("telemetry_endpoint") or ""),
            "report": f"/novaforge/evaluations/{record.get('id')}/report",
            "frames": [
                dict(frame) for frame in evidence.get("frames") or []
                if isinstance(frame, dict)
            ],
            "timeline_events": evidence.get("timeline_events"),
            "timeline_source": evidence.get("timeline_source"),
            "persistence": dict(evidence.get("persistence") or {}),
            "gaps": [str(item) for item in evidence.get("gaps") or []],
        }
        record["objective"] = {
            "available": False,
            "verified": False,
            "achieved": None,
            "data_complete": mapped == "completed",
            "reason": (
                "No objective verifier is attached to a NovaForge session. A completed "
                "evaluation means its data was recorded, never that the game objective "
                "was achieved."
            ),
        }
        record["comparison"] = self._comparison(record)
        self._persist(verify_id=str(record.get("id")))

    def _comparison(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        baseline = dict(record.get("baseline") or {})
        baseline_run = str(baseline.get("benchmark_run_id") or "")
        run_id = str(record.get("benchmark_run_id") or "")
        if not baseline_run or not run_id or baseline.get("role") == "baseline":
            return None
        if record.get("status") != "completed" or not record.get("valid"):
            return {
                "available": False,
                "compatible": False,
                "reasons": [
                    "No comparison is possible: this evaluation is not completed and valid."
                ],
            }
        try:
            comparison = self.benchmark.compare([baseline_run], [run_id])
        except Exception as exc:  # noqa: BLE001 - a refused comparison is a result
            return {
                "available": False,
                "compatible": False,
                "reasons": [
                    f"The baseline comparison could not be computed: {type(exc).__name__}: {exc}"
                ],
            }
        return {
            "available": True,
            "baseline_evaluation_id": baseline.get("evaluation_id"),
            "baseline_benchmark_run_id": baseline_run,
            "compatible": bool(comparison.get("compatible")),
            "reasons": list(comparison.get("reasons") or []),
            "warnings": list(comparison.get("warnings") or []),
            "sample": dict(comparison.get("sample") or {}),
            "metrics": list(comparison.get("metrics") or []),
            "verdict": dict(comparison.get("verdict") or {}),
        }

    # -- cancel / shutdown ---------------------------------------------------

    async def cancel(self, evaluation_id: str) -> dict[str, Any]:
        record = self._evaluations.get(evaluation_id)
        if record is None:
            raise GameNotFoundError(f"Unknown evaluation: {evaluation_id}")
        self._sync(record)
        if record.get("status") != "running":
            return {"evaluation": dict(record), "already_stopped": True}
        benchmark_id = str(record.get("benchmark_run_id") or "")
        if benchmark_id:
            try:
                await self.benchmark.cancel(benchmark_id)
            except BenchmarkNotFoundError:
                # The benchmark record vanished between the sync above and the
                # cancel; the _sync below reports the attempt aborted with that
                # reason instead of answering a cancel with an error.
                pass
        self._sync(record)
        if record.get("status") == "running":  # pragma: no cover - a stuck benchmark
            record["status"] = "aborted"
            record["valid"] = False
            record["reason"] = (
                "The evaluation was cancelled, but the benchmark run did not reach a "
                "terminal state within the bounded wait. It is excluded from baselines."
            )
            record["ended_at"] = utc_now()
            self._persist(verify_id=evaluation_id)
        return {"evaluation": dict(record), "already_stopped": False}

    async def shutdown(self) -> None:
        """Read the final benchmark state of every running evaluation and persist it."""
        for record in list(self._evaluations.values()):
            self._sync(record)
        self._persist()

    # -- report --------------------------------------------------------------

    def report(self, evaluation_id: str) -> dict[str, Any]:
        record = self._evaluations.get(evaluation_id)
        if record is None:
            raise GameNotFoundError(f"Unknown evaluation: {evaluation_id}")
        self._sync(record)
        benchmark = (
            self.benchmark.get(str(record.get("benchmark_run_id") or ""))
            if record.get("benchmark_run_id")
            else None
        )
        timeline: dict[str, Any] | None = None
        session_id = str(record.get("session_id") or "")
        if session_id:
            try:
                timeline = self.manager.timeline_payload(session_id, limit=1)
            except Exception:  # noqa: BLE001 - evidence unavailable is reported, not raised
                timeline = None
        baseline = dict(record.get("baseline") or {}) or None
        scenario = resolve_scenario(
            str((record.get("scenario") or {}).get("scenario_id")),
            (record.get("scenario") or {}).get("version"),
        )
        game = resolve_game(
            str((record.get("game") or {}).get("game_id")),
            (record.get("game") or {}).get("version"),
        )
        gaps = list(
            missing_evidence(
                benchmark=benchmark,
                timeline=timeline,
                baseline=baseline,
                session_id=session_id,
            )
        )
        # A stored record may be older than this module or edited by hand; the
        # report reads it defensively so a partial record is REPORTED (missing
        # evidence, fewer limitations) instead of answering with a 500.
        stored_evidence = record.get("evidence")
        stored_evidence = stored_evidence if isinstance(stored_evidence, dict) else {}
        for gap in stored_evidence.get("gaps") or []:
            gaps.append(str(gap))
        if benchmark is not None:
            for gap in benchmark.get("evidence", {}).get("gaps") or []:
                gaps.append(str(gap))
        limitations = [str(item) for item in (record.get("limitations") or [])]
        if game is not None and scenario is not None:
            for line in evaluation_limitations(game, scenario):
                if line not in limitations:
                    limitations.append(line)
        record["limitations"] = limitations
        return {
            "version": EVALUATION_VERSION,
            "report_id": f"report-{evaluation_id}",
            "generated_at": utc_now(),
            "evaluation_id": evaluation_id,
            "game": dict(record.get("game") or {}),
            "compatibility": {
                "status": str((record.get("game") or {}).get("status")),
                "reason": str((record.get("game") or {}).get("status_reason")),
                "control_capable": bool(
                    (record.get("game") or {}).get("control_capable")
                ),
            },
            "mode": str(record.get("mode") or "observation_only"),
            "scenario": dict(record.get("scenario") or {}),
            "configuration": dict(record.get("configuration") or {}),
            "readiness": dict(record.get("readiness") or {}),
            "status": record.get("status"),
            "valid": bool(record.get("valid")),
            "reason": str(record.get("reason") or ""),
            "objective": dict(record.get("objective") or {}),
            "benchmark_run_id": record.get("benchmark_run_id"),
            "session_id": record.get("session_id"),
            "metrics": dict(record.get("metrics") or {}),
            "evidence": dict(record.get("evidence") or {}),
            "baseline": baseline,
            "comparison": record.get("comparison"),
            "guardian": dict(record.get("guardian") or {}),
            "classification": dict(record.get("classification") or {}),
            "limitations": list(dict.fromkeys(limitations)),
            "missing_evidence": list(dict.fromkeys(gaps)),
            "objective_verified": False,
            "real_gameplay_control_exercised": False,
            "note": (
                "This report describes one bounded observation evaluation. No gameplay "
                "input was sent and no objective verifier exists, so it makes no claim "
                "about gameplay capability, wins or completion."
            ),
        }

    def _summary(self, record: Mapping[str, Any]) -> dict[str, Any]:
        game = dict(record.get("game") or {})
        scenario = dict(record.get("scenario") or {})
        baseline = dict(record.get("baseline") or {})
        return {
            "id": record.get("id"),
            "game": {
                "game_id": game.get("game_id"),
                "version": game.get("version"),
                "display_name": game.get("display_name"),
                "status": game.get("status"),
            },
            "scenario": {
                "scenario_id": scenario.get("scenario_id"),
                "version": scenario.get("version"),
                "name": scenario.get("name"),
            },
            "mode": record.get("mode"),
            "status": record.get("status"),
            "valid": bool(record.get("valid")),
            "reason": str(record.get("reason") or ""),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "duration_seconds": record.get("duration_seconds"),
            "benchmark_run_id": record.get("benchmark_run_id") or "",
            "session_id": record.get("session_id") or "",
            "baseline_role": baseline.get("role") or "",
            "baseline_evaluation_id": baseline.get("evaluation_id") or "",
            "comparison_compatible": (
                (record.get("comparison") or {}).get("compatible")
                if record.get("comparison")
                else None
            ),
            "objective_verified": False,
            "metrics": dict(record.get("metrics") or {}),
            "interrupted": bool(record.get("interrupted")),
        }

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - an observer cannot fail an evaluation
            return
