"""NovaForge NF-04 — the bounded benchmark runner and run store.

The lab is a thin orchestrator over the *existing* session manager: it validates
a scenario, starts one real observation session, waits for it under a deadline,
then reads the session's own telemetry, timeline and record into a benchmark
run. It never captures, decisions, or sends input itself, and it never starts a
second session manager.

Properties this module is responsible for:

* One run at a time. A start is refused while a NovaForge session or another
  benchmark run is live, so two runs cannot contend for the same window.
* Every attempt is recorded: started, completed, failed, aborted and invalid.
  An interrupted or invalid attempt is never counted as successful.
* A run is persisted before the response and re-persisted on every terminal
  transition. A run left ``running`` by a restart is recovered as ABORTED with
  the gap stated, not silently dropped or reported complete.
* Cancellation travels the session manager's stop path, so input cleanup and
  Guardian's own intervention record still happen.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from novacontrol.persistence.json_store import JsonStateStore

from .benchmark import (
    BENCHMARK_VERSION,
    BenchmarkConfig,
    BenchmarkConflictError,
    BenchmarkNotFoundError,
    BenchmarkScenario,
    BenchmarkValidationError,
    ELIGIBLE_STATUS,
    TERMINAL_RUN_STATUSES,
    aggregate_runs,
    collect_metrics,
    compare_runs,
    config_from_request,
    environment_snapshot,
    resolve_scenario,
    run_limitations,
    scenario_payload,
    unsupported_metrics,
)
from .manager import NovaForgeManager
from .models import (
    SESSION_TERMINAL_STATES,
    MissionValidationError,
    SessionConflictError,
    SessionRecord,
    SessionState,
    SessionTransitionError,
    utc_now,
)

RUNS_NAMESPACE = "novaforge_benchmarks"
MAX_RUNS_KEPT = 60
#: The runner's own poll cadence. It is recorded on every run because it bounds
#: how late a terminal state or a cancellation request can be observed.
POLL_INTERVAL_SECONDS = 0.1
#: After the session's own budget plus the manager's stop timeout, the runner
#: stops waiting and aborts the run. Without this the benchmark could wait
#: forever on a session that never reaches a terminal state.
DEADLINE_GRACE_SECONDS = 5.0

Publish = Callable[[str, dict[str, Any]], Awaitable[None]]


def _scenario_version(scenario: Any) -> int | None:
    """A stored scenario version, or ``None`` when the record does not carry one.

    Every reader of a stored run filters on this figure, so an unreadable value
    has to be an absence rather than an exception out of ``/aggregate``.
    """
    if not isinstance(scenario, Mapping):
        return None
    value = scenario.get("version")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _duration_value(value: Any) -> float | None:
    """A measured duration, or ``None`` when no usable figure was recorded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 3)


@dataclass(slots=True)
class _RunHandle:
    run_id: str
    cancel_requested: bool = False


class NovaForgeBenchmarkLab:
    """Runs benchmark scenarios and keeps their persisted history."""

    def __init__(
        self,
        *,
        manager: NovaForgeManager,
        store: JsonStateStore | None = None,
        publish: Publish | None = None,
        max_runs: int = MAX_RUNS_KEPT,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.manager = manager
        self.store = store if store is not None else getattr(manager, "store", None)
        self.publish = publish
        self.max_runs = max(1, int(max_runs))
        self.poll_interval = max(0.02, float(poll_interval))
        self._runs: dict[str, dict[str, Any]] = {}
        self._active_id: str | None = None
        self._handle: _RunHandle | None = None
        self._task: asyncio.Task[None] | None = None
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
                payload = self.store.read(RUNS_NAMESPACE)
            except Exception:  # noqa: BLE001 - an unreadable store has no state, not a boot failure
                payload = {}
        stored = payload.get("runs")
        if not isinstance(stored, list):
            return
        recovered = False
        for item in stored:
            if not isinstance(item, dict):
                continue
            run_id = str(item.get("id") or "")
            if not run_id or not isinstance(item.get("scenario"), dict):
                continue
            run = dict(item)
            if _scenario_version(run["scenario"]) is None:
                # A hand-edited or foreign snapshot can carry a version no
                # reader can resolve; preserving it as invalid keeps /history
                # and /aggregate answering instead of raising out of a read.
                run["status"] = "invalid"
                run["valid"] = False
                run["reason"] = (
                    "The stored run's scenario version is not an integer; it is "
                    "preserved as invalid."
                )
                recovered = True
            if run.get("status") not in TERMINAL_RUN_STATUSES | {"running"}:
                run["status"] = "invalid"
                run["valid"] = False
                run["reason"] = (
                    "The stored run had an unrecognised status; it is preserved as invalid."
                )
                recovered = True
            if run.get("status") == "running":
                # The process that owned this run is gone. Its session may have
                # persisted partial evidence; the run is ABORTED, never upgraded
                # to a completed result.
                run["status"] = "aborted"
                run["valid"] = False
                run["interrupted"] = True
                run["ended_at"] = run.get("ended_at") or utc_now()
                run["reason"] = (
                    "The application restarted while this benchmark run was active. "
                    "Its evidence is whatever the session recorded before the restart; "
                    "the run is not complete and is excluded from aggregates."
                )
                recovered = True
            self._runs[run_id] = run
        self._trim()
        if recovered:
            self._persist()

    def _persist(self, *, verify_run_id: str = "") -> None:
        """Write the run store, and verify a terminal run is readable back.

        ``JsonStateStore`` deliberately drops a snapshot it cannot replace, so a
        successful ``write`` call is not proof the record exists. A run that
        reports where it was persisted (or why it was not) cannot disappear
        without anyone noticing.
        """
        run = self._runs.get(verify_run_id) if verify_run_id else None
        if self.store is None:
            if run is not None:
                run["persistence"] = {
                    "state": "not_persisted",
                    "reason": (
                        "No state store is attached; this run exists in memory for this "
                        "process only."
                    ),
                }
            return
        self._trim()
        ordered = sorted(
            self._runs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True
        )
        try:
            self.store.write(
                RUNS_NAMESPACE,
                {"version": BENCHMARK_VERSION, "runs": ordered},
            )
        except Exception as exc:  # noqa: BLE001 - recording must never fail the run
            if run is not None:
                run["persistence"] = {
                    "state": "failed",
                    "reason": f"The store raised {type(exc).__name__}: {exc}",
                }
            return
        if run is None:
            return
        try:
            stored = self.store.read(RUNS_NAMESPACE)
        except Exception as exc:  # noqa: BLE001 - verification must not fail the run
            run["persistence"] = {
                "state": "failed",
                "reason": f"The store could not be read back: {type(exc).__name__}: {exc}",
            }
            return
        rows = stored.get("runs") if isinstance(stored, dict) else None
        present = isinstance(rows, list) and any(
            isinstance(item, dict) and str(item.get("id")) == verify_run_id for item in rows
        )
        run["persistence"] = {
            "state": "verified" if present else "failed",
            "reason": ""
            if present
            else (
                "The store accepted the write but the run is not readable back; a later "
                "write may replace it."
            ),
        }

    def _trim(self) -> None:
        if len(self._runs) <= self.max_runs:
            return
        ordered = sorted(
            self._runs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True
        )
        keep = {str(run.get("id")) for run in ordered[: self.max_runs]}
        for run_id in list(self._runs):
            if run_id not in keep:
                del self._runs[run_id]

    # -- read surfaces -------------------------------------------------------

    def active_run(self) -> dict[str, Any] | None:
        if self._active_id is None:
            return None
        run = self._runs.get(self._active_id)
        return run if run is not None and run.get("status") == "running" else None

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def scenarios(self) -> dict[str, Any]:
        return scenario_payload()

    def scenario(self, scenario_id: str, version: Any = None) -> dict[str, Any]:
        found = resolve_scenario(scenario_id, version)
        if found is None:
            raise BenchmarkNotFoundError(
                f"Unknown benchmark scenario or version: {scenario_id!r} {version!r}"
            )
        payload = found.to_dict()
        payload["unsupported_metrics"] = unsupported_metrics()
        return payload

    def history(
        self, *, limit: int = 20, scenario_id: str = ""
    ) -> dict[str, Any]:
        ordered = sorted(
            self._runs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True
        )
        if scenario_id:
            ordered = [
                run
                for run in ordered
                if str((run.get("scenario") or {}).get("scenario_id")) == scenario_id
            ]
        limit = max(1, min(int(limit), 100))
        rows = [self._summary(run) for run in ordered[:limit]]
        active = self.active_run()
        return {
            "version": BENCHMARK_VERSION,
            "runs": rows,
            "count": len(rows),
            "total": len(ordered),
            "retention": {
                "cap": self.max_runs,
                "retained": len(self._runs),
                "note": (
                    "History is bounded; the oldest records are dropped when the cap is "
                    "reached. Evictions are not counted in this payload."
                ),
            },
            "active_run_id": active.get("id") if active is not None else None,
            "note": (
                "Newest first. Every retained attempt is listed; only COMPLETED and valid "
                "runs enter aggregates."
            ),
        }

    def aggregate(self, scenario_id: str, version: Any = None) -> dict[str, Any]:
        scenario = resolve_scenario(scenario_id, version)
        if scenario is None:
            raise BenchmarkNotFoundError(
                f"Unknown benchmark scenario or version: {scenario_id!r} {version!r}"
            )
        runs = [
            run
            for run in self._runs.values()
            if str((run.get("scenario") or {}).get("scenario_id")) == scenario.scenario_id
            and _scenario_version(run.get("scenario")) == scenario.version
        ]
        return aggregate_runs(runs, scenario)

    def compare(self, left_ids: Sequence[str], right_ids: Sequence[str]) -> dict[str, Any]:
        def pick(ids: Sequence[str], side: str) -> list[dict[str, Any]]:
            if not ids:
                raise BenchmarkValidationError([f"The {side} side needs at least one run id."])
            picked: list[dict[str, Any]] = []
            for run_id in ids:
                run = self._runs.get(run_id)
                if run is None:
                    raise BenchmarkNotFoundError(f"Unknown benchmark run: {run_id}")
                picked.append(run)
            return picked

        return compare_runs(pick(left_ids, "left"), pick(right_ids, "right"))

    # -- preflight and start -------------------------------------------------

    async def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a scenario, its configuration, and the live target window."""
        scenario, config, errors = config_from_request(payload)
        result: dict[str, Any] = {
            "valid": False,
            "errors": list(errors),
            "warnings": [],
            "scenario": scenario.to_dict() if scenario is not None else None,
            "config": config.to_dict() if config is not None else None,
            "target": None,
            "conflicts": [],
            "runnable": False,
            "capture_supported": bool(getattr(self.manager, "capture_supported", False)),
            "limitations": (
                run_limitations(scenario)
                if scenario is not None
                else ["An unknown scenario cannot be run."]
            ),
        }
        if scenario is None or config is None:
            return result

        if not result["capture_supported"]:
            errors.append("Screen capture is not implemented on this platform.")
        active_run = self.active_run()
        if active_run is not None:
            result["conflicts"].append(
                f"Benchmark run {active_run.get('id')} is still active; wait for it to finish."
            )
        active_session = self.manager.active()
        if active_session is not None:
            result["conflicts"].append(
                f"Session {active_session.session_id} is {active_session.state.value}; "
                "a benchmark run uses the one session slot, so stop it first."
            )
        errors.extend(result["conflicts"])

        if errors:
            result["errors"] = errors
            return result

        validation = await self.manager.validate(self._mission(scenario, config))
        if validation.get("target") is not None:
            result["target"] = validation["target"]
        errors.extend(str(item) for item in validation.get("errors") or [])
        result["errors"] = errors
        result["valid"] = not errors
        result["runnable"] = not errors
        result["warnings"] = [
            "NF-04 invokes no model or policy: this run measures capture and observation "
            "behavior. Model comparison is unavailable and is shown as such.",
            "The run ends with the scenario's time limit. A time limit ending is data "
            "completeness, not gameplay success.",
        ]
        result["expected_frames"] = int(config.duration_seconds / config.frame_interval_seconds) + 1
        return result

    async def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and start one bounded observation run.

        Returns as soon as the run record exists. Execution continues in a
        background task; the UI polls the run detail, and the run itself is
        written to the store at every terminal transition.
        """
        # A conflict is answered as a CONFLICT, not as field validation: the
        # same request would be valid once the active run/session ends.
        active_run = self.active_run()
        if active_run is not None:
            raise BenchmarkConflictError(
                f"Benchmark run {active_run.get('id')} is still active; wait for it to finish."
            )
        active_session = self.manager.active()
        if active_session is not None:
            raise BenchmarkConflictError(
                f"Session {active_session.session_id} is {active_session.state.value}; "
                "a benchmark run uses the one session slot, so stop it first."
            )
        preflight = await self.preflight(payload)
        if not preflight["valid"]:
            raise BenchmarkValidationError(list(preflight["errors"]))
        scenario = resolve_scenario(
            str(payload.get("scenario_id", "")),
            payload.get("version"),
        )
        _resolved, config, errors = config_from_request(payload)
        if scenario is None or config is None:  # pragma: no cover - preflight checked this
            raise BenchmarkValidationError(errors or ["The benchmark configuration is invalid."])

        async with self._lock():
            if self.active_run() is not None:
                raise BenchmarkConflictError(
                    "Another benchmark run is active; wait for it to finish before starting one."
                )
            if self.manager.active() is not None:
                raise BenchmarkConflictError(
                    "A NovaForge session is active; stop it before starting a benchmark run."
                )
            now = utc_now()
            run_id = uuid4().hex
            # The identity the operator validated in preflight, if the caller pins
            # it. The manager refuses a title that now belongs to another window.
            identity: dict[str, Any] = {}
            for key in ("expected_handle", "expected_pid"):
                value = payload.get(key)
                if value not in (None, ""):
                    identity[key] = value
            run: dict[str, Any] = {
                "version": BENCHMARK_VERSION,
                "id": run_id,
                "identity": identity,
                "scenario": scenario.snapshot(),
                "configuration": config.to_dict(),
                "status": "running",
                "valid": False,
                "reason": "",
                "session_id": "",
                "created_at": now,
                "started_at": now,
                "ended_at": "",
                "duration_seconds": None,
                "target": dict(preflight.get("target") or {}),
                "environment": environment_snapshot(),
                "model": {
                    "available": False,
                    "id": None,
                    "reason": (
                        "No model is invoked. NF-04 benchmarks observation and capture; a "
                        "model identifier would fabricate a comparison."
                    ),
                },
                "policy": {
                    "available": False,
                    "id": None,
                    "reason": "No action policy is invoked: NF-04 sends no input.",
                },
                "objective": {
                    "evaluation": scenario.evaluation,
                    "achieved": None,
                    "verifier": dict(scenario.verifier),
                    "success_criteria": scenario.success_criteria,
                    "data_complete": False,
                },
                "metrics": {},
                "metric_rows": 0,
                "evidence": {
                    "session": "",
                    "session_available": False,
                    "timeline_endpoint": "",
                    "telemetry_endpoint": "",
                    "frames": [],
                    "observations": [],
                    "interventions": [],
                    "gaps": ["The run has not finished recording evidence yet."],
                },
                "guardian": {},
                "classification": {},
                "errors": [],
                "limitations": run_limitations(scenario),
                "interrupted": False,
                "runner": {
                    "poll_interval_ms": round(self.poll_interval * 1000.0, 1),
                    "polls": 0,
                    "collection_ms": None,
                    "finalize_ms": None,
                    "deadline_hit": False,
                    "cancelled": False,
                    "note": "Waiting for the session to start.",
                },
            }
            self._runs[run_id] = run
            self._active_id = run_id
            self._handle = _RunHandle(run_id=run_id)
            self._trim()
            self._persist()
            self._task = asyncio.create_task(self._run(run_id), name=f"novaforge-benchmark-{run_id}")
        await self._publish(
            "novaforge.benchmark.started",
            {
                "benchmark_run_id": run_id,
                "scenario": scenario.key,
                "target": config.target_title,
                "duration_seconds": config.duration_seconds,
            },
        )
        return dict(run)

    # -- cancellation and shutdown ------------------------------------------

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """Request an abort through the session's stop path and wait, bounded."""
        run = self._runs.get(run_id)
        if run is None:
            raise BenchmarkNotFoundError(f"Unknown benchmark run: {run_id}")
        if run.get("status") != "running":
            return {"run": dict(run), "already_stopped": True}
        if self._handle is not None and self._handle.run_id == run_id:
            self._handle.cancel_requested = True
        task = self._task
        if task is not None and not task.done():
            timeout = float(getattr(self.manager, "stop_timeout", 5.0)) + DEADLINE_GRACE_SECONDS
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:  # pragma: no cover - a stuck session is reported below
                run = self._runs.get(run_id, run)
                return {"run": dict(run), "already_stopped": False}
        return {"run": dict(self._runs.get(run_id, run)), "already_stopped": False}

    async def shutdown(self) -> None:
        """Close an active run before the manager shuts the session down.

        The lab stops its session through the normal stop path (so input cleanup
        and the intervention record happen), waits a bounded time for the
        runner to finalize, and always persists what it has.
        """
        run = self.active_run()
        if run is None:
            self._task = None
            return
        run_id = str(run.get("id"))
        run["runner"]["shutdown_requested"] = True
        if self._handle is not None and self._handle.run_id == run_id:
            self._handle.cancel_requested = True
        task = self._task
        if task is not None and not task.done():
            timeout = float(getattr(self.manager, "stop_timeout", 5.0)) + DEADLINE_GRACE_SECONDS
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except Exception:  # noqa: BLE001 - shutdown must keep going
                pass
        if run_id in self._runs and self._runs[run_id].get("status") == "running":
            self._finish(
                run_id,
                status="aborted",
                reason=(
                    "The application shut down while this benchmark run was active. "
                    "The run is incomplete and excluded from aggregates."
                ),
            )
        self._persist()
        self._task = None

    # -- the runner ----------------------------------------------------------

    def _mission(
        self,
        scenario: BenchmarkScenario,
        config: BenchmarkConfig,
        identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        mission: dict[str, Any] = {
            "name": f"Benchmark: {scenario.name}",
            "objective": scenario.objective,
            "target_title": config.target_title,
            "success_criteria": scenario.success_criteria,
            "time_limit_seconds": config.duration_seconds,
            "frame_interval_seconds": config.frame_interval_seconds,
        }
        # Carried only when the caller pinned the identity it validated. A
        # benchmark started from the panel keeps title matching alone.
        for key in ("expected_handle", "expected_pid"):
            value = (identity or {}).get(key)
            if value not in (None, ""):
                mission[key] = value
        return mission

    async def _run(self, run_id: str) -> None:
        run = self._runs[run_id]
        session_id = ""
        try:
            scenario = resolve_scenario(
                str((run.get("scenario") or {}).get("scenario_id")),
                (run.get("scenario") or {}).get("version"),
            )
            config = self._config(run)
            if scenario is None or config is None:  # pragma: no cover - start validated this
                self._finish(
                    run_id,
                    status="invalid",
                    reason="The stored scenario or configuration could not be resolved.",
                )
                return
            try:
                started = await self.manager.start(
                    self._mission(scenario, config, dict(run.get("identity") or {}))
                )
            except MissionValidationError as exc:
                reasons = list(getattr(exc, "errors", None) or [str(exc)])
                self._finish(
                    run_id,
                    status="invalid",
                    reason="Start was refused: " + "; ".join(str(item) for item in reasons),
                )
                return
            except (SessionConflictError, SessionTransitionError) as exc:
                self._finish(run_id, status="invalid", reason=f"Start was refused: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - recorded as an invalid attempt
                self._finish(
                    run_id,
                    status="invalid",
                    reason=f"Start raised {type(exc).__name__}: {exc}",
                )
                return

            session_id = str(started.get("session_id") or "")
            run["session_id"] = session_id
            run["target"] = dict(started.get("target") or run.get("target") or {})
            run["environment"]["window"] = self._window(run["target"])
            run["evidence"]["session"] = session_id
            run["evidence"]["session_available"] = True
            run["evidence"]["timeline_endpoint"] = f"/novaforge/sessions/{session_id}/timeline"
            run["evidence"]["telemetry_endpoint"] = f"/novaforge/sessions/{session_id}/telemetry"
            run["evidence"]["gaps"] = [
                "The run is still active; evidence is finalized when it ends."
            ]
            run["runner"]["note"] = "Session started."
            self._persist()
            await self._publish(
                "novaforge.benchmark.session_started",
                {
                    "benchmark_run_id": run_id,
                    "scenario": scenario.key,
                    "session_id": session_id,
                },
            )
            await self._wait(run_id, session_id, config)
            self._finalize(run_id)
        except asyncio.CancelledError:
            if run_id in self._runs and self._runs[run_id].get("status") == "running":
                if session_id:
                    try:
                        await self.manager.stop(session_id, reason="benchmark_cancelled")
                    except Exception:  # noqa: BLE001 - the record is still written below
                        pass
                self._finish(
                    run_id,
                    status="aborted",
                    reason=(
                        "The benchmark runner was cancelled while it was active; the run is "
                        "incomplete and excluded from aggregates."
                    ),
                )
            raise
        except Exception as exc:  # noqa: BLE001 - a runner failure is a FAILED run
            if session_id:
                try:
                    await self.manager.stop(session_id, reason="benchmark_runner_error")
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
            self._finish(
                run_id,
                status="failed",
                reason=f"Benchmark runner error: {type(exc).__name__}: {exc}",
            )
        finally:
            if self._active_id == run_id:
                self._active_id = None
            self._handle = None
            self._task = None
            self._persist()
            finished = self._runs.get(run_id) or {}
            await self._publish(
                "novaforge.benchmark.finished",
                {
                    "benchmark_run_id": run_id,
                    "session_id": str(finished.get("session_id") or ""),
                    "status": str(finished.get("status") or ""),
                    "valid": bool(finished.get("valid")),
                },
            )

    async def _wait(
        self, run_id: str, session_id: str, config: BenchmarkConfig
    ) -> None:
        """Wait for the session's terminal state, its budget, or a cancellation."""
        run = self._runs[run_id]
        deadline = (
            time.monotonic()
            + float(config.duration_seconds)
            + float(getattr(self.manager, "stop_timeout", 5.0))
            + DEADLINE_GRACE_SECONDS
        )
        polls = 0
        while True:
            record = self.manager.get(session_id)
            if record is None:
                run["runner"]["note"] = "The session record disappeared while the run was active."
                run["runner"]["deadline_hit"] = True
                return
            if record.state in SESSION_TERMINAL_STATES:
                return
            handle = self._handle
            if handle is not None and handle.cancel_requested:
                run["runner"]["cancelled"] = True
                run["runner"]["note"] = "Cancel requested; stopping the session."
                try:
                    await self.manager.stop(session_id, reason="benchmark_cancelled")
                except Exception as exc:  # noqa: BLE001 - reported by the terminal state
                    run["runner"]["note"] = (
                        f"Cancel requested but the stop raised {type(exc).__name__}: {exc}"
                    )
                return
            if time.monotonic() >= deadline:
                run["runner"]["deadline_hit"] = True
                run["runner"]["note"] = (
                    "The runner deadline passed; the session is being aborted rather than "
                    "waited on forever."
                )
                try:
                    await self.manager.cancel(session_id)
                except Exception as exc:  # noqa: BLE001 - the record still says aborted
                    run["runner"]["note"] = (
                        f"The runner deadline passed and the cancel raised "
                        f"{type(exc).__name__}: {exc}"
                    )
                return
            if record.state is SessionState.PAUSED and not record.frames:
                pause_reason = str(record.pause_reason or "")
                if pause_reason in {"target_lost", "target_changed", "target_unreadable"}:
                    run["runner"]["note"] = (
                        f"The target window was {pause_reason.replace('_', ' ')} before a "
                        "frame could be captured."
                    )
                    run["runner"]["invalid_reason"] = run["runner"]["note"]
                    try:
                        await self.manager.stop(session_id, reason="benchmark_target_lost")
                    except Exception:  # noqa: BLE001 - the terminal state is still recorded
                        pass
                    return
            await asyncio.sleep(self.poll_interval)
            polls += 1
            run["runner"]["polls"] = polls
            if polls % 20 == 0:
                self._persist()

    def _finalize(self, run_id: str) -> None:
        """Read the session's own evidence into the run record and classify it."""
        run = self._runs[run_id]
        session_id = str(run.get("session_id") or "")
        record = self.manager.get(session_id) if session_id else None
        collected = time.monotonic()
        if record is None:
            self._finish(
                run_id,
                status="invalid",
                reason=(
                    "The benchmark session record is unavailable, so no result can be "
                    "produced for this attempt."
                ),
            )
            return

        scenario = resolve_scenario(
            str((run.get("scenario") or {}).get("scenario_id")),
            (run.get("scenario") or {}).get("version"),
        )
        config = self._config(run)
        if scenario is None or config is None:  # pragma: no cover - start validated this
            self._finish(run_id, status="invalid", reason="The run's scenario is unresolvable.")
            return

        try:
            snapshot = self.manager.telemetry_payload(session_id)
            timeline = self.manager.timeline_payload(session_id, limit=1)
        except Exception as exc:  # noqa: BLE001 - evidence unavailable is an invalid run
            self._finish(
                run_id,
                status="invalid",
                reason=f"The session's telemetry or timeline could not be read: {type(exc).__name__}: {exc}",
            )
            return

        telemetry = dict(snapshot.get("telemetry") or {})
        capture = dict(telemetry.get("capture") or {})
        latency = dict(capture.get("latency_ms") or {})
        frames = int(capture.get("frames") or 0)
        latency_samples = int(latency.get("samples") or 0)
        retention = dict(timeline.get("retention") or {})
        events = int(retention.get("stored") or 0)

        metrics = collect_metrics(
            scenario=scenario,
            telemetry=telemetry,
            events=events,
            errors=len(record.errors),
            interventions=len(record.interventions),
        )
        run["metrics"] = metrics
        run["metric_rows"] = len(metrics)
        run["telemetry"] = telemetry
        run["guardian"] = dict(snapshot.get("guardian") or {})
        run["classification"] = dict(timeline.get("classification") or {})
        run["errors"] = [dict(item) for item in record.errors[-10:]]
        # A missing duration is UNAVAILABLE, never 0.0: the telemetry row says so
        # already, and the record's own figure must not contradict it.
        session_block = telemetry.get("session")
        recorded_duration = (
            session_block.get("duration_seconds") if isinstance(session_block, dict) else None
        )
        run["duration_seconds"] = _duration_value(recorded_duration)
        run["ended_at"] = record.ended_at or utc_now()

        evidence = run["evidence"]
        evidence.update(
            {
                "session": session_id,
                "session_available": True,
                "timeline_endpoint": f"/novaforge/sessions/{session_id}/timeline",
                "telemetry_endpoint": f"/novaforge/sessions/{session_id}/telemetry",
                "timeline_events": events,
                "timeline_dropped": int(retention.get("dropped") or 0),
                "timeline_source": timeline.get("source"),
                "persistence": dict(timeline.get("persistence") or {}),
                "frames": [
                    {
                        "seq": frame.seq,
                        "captured_at": frame.captured_at,
                        "endpoint": f"/novaforge/frames/{session_id}?seq={frame.seq}",
                    }
                    for frame in record.frames[-3:]
                ],
                "observations": [
                    {"index": observation.index, "at": observation.at}
                    for observation in record.observations[-3:]
                ],
                "interventions": [dict(item) for item in record.interventions[-5:]],
                "gaps": [],
            }
        )
        gaps = evidence["gaps"]
        if not evidence["frames"]:
            gaps.append(
                "No captured frame is retained for this run; the metrics still come from "
                "its measured telemetry."
            )
        if timeline.get("source") != "recorded":
            gaps.append(
                "The timeline is derived from the session record rather than an NF-03 event log."
            )
        if not (dict(timeline.get("persistence") or {}).get("ok")):
            gaps.append(
                "Timeline persistence is unverified: "
                + str(dict(timeline.get("persistence") or {}).get("reason") or "no reason reported")
            )

        runner = run["runner"]
        runner["collection_ms"] = round((time.monotonic() - collected) * 1000.0, 2)
        runner["deadline_hit"] = bool(runner.get("deadline_hit"))
        runner["cancelled"] = bool(runner.get("cancelled"))

        status, reason = self._terminal_status(record, run, scenario, frames, latency_samples)
        self._finish(run_id, status=status, reason=reason, scenario=scenario)
        runner["finalize_ms"] = round((time.monotonic() - collected) * 1000.0, 2)

    def _terminal_status(
        self,
        record: SessionRecord,
        run: Mapping[str, Any],
        scenario: BenchmarkScenario,
        frames: int,
        latency_samples: int,
    ) -> tuple[str, str]:
        """Map the session's real ending onto a benchmark status — never guessed."""
        runner = dict(run.get("runner") or {})
        state = record.state
        end_reason = str(record.end_reason or "")
        if runner.get("shutdown_requested"):
            return (
                "aborted",
                "The application shut down while this benchmark run was active. The run is "
                "incomplete and excluded from aggregates.",
            )
        if runner.get("cancelled") or end_reason == "benchmark_cancelled":
            return (
                "aborted",
                "Cancelled by the operator. The session was stopped through its normal stop "
                "path, so input cleanup and the intervention record still happened; the run "
                "is excluded from aggregates.",
            )
        if runner.get("deadline_hit"):
            return (
                "aborted",
                "The runner's deadline passed before the session reached a terminal state; "
                "the attempt was aborted rather than reported complete.",
            )
        invalid_reason = str(runner.get("invalid_reason") or "")
        if invalid_reason:
            return "invalid", invalid_reason
        if state is SessionState.FAILED:
            last = record.errors[-1] if record.errors else {}
            detail = str(last.get("message") or last.get("error") or "no error message recorded")
            return "failed", f"The session failed: {detail}"
        if state not in {SessionState.STOPPED, SessionState.COMPLETED}:
            return (
                "aborted",
                f"The session ended in {state.value} "
                f"({end_reason or 'no end reason'}); no completed observation was recorded.",
            )
        if end_reason not in {"time_limit", "completed"}:
            return (
                "aborted",
                "The session was stopped outside the benchmark runner "
                f"(end reason: {end_reason or 'none'}); the attempt is incomplete.",
            )
        if frames < scenario.min_frames:
            return (
                "invalid",
                f"Only {frames} fresh frame(s) were recorded; this scenario requires at "
                f"least {scenario.min_frames}. The attempt is excluded from aggregates.",
            )
        if latency_samples < scenario.min_latency_samples:
            return (
                "invalid",
                f"Only {latency_samples} usable latency sample(s) were recorded; this "
                f"scenario requires at least {scenario.min_latency_samples}.",
            )
        return (
            "completed",
            "The observation ran to its budget and recorded the required data. No objective "
            "is claimed: NF-04 has no verifier.",
        )

    def _finish(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        scenario: BenchmarkScenario | None = None,
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        if run.get("status") in TERMINAL_RUN_STATUSES:
            return
        run["status"] = status
        run["valid"] = status == ELIGIBLE_STATUS
        run["reason"] = reason
        run["ended_at"] = run.get("ended_at") or utc_now()
        run["objective"] = {
            **dict(run.get("objective") or {}),
            "achieved": None,
            "data_complete": status == ELIGIBLE_STATUS,
            "reason": (
                "No objective verifier exists in NF-04; data completeness is not gameplay "
                "success."
            ),
        }
        if scenario is not None:
            run["limitations"] = run_limitations(scenario)
        self._persist(verify_run_id=run_id)

    # -- helpers -------------------------------------------------------------

    def _config(self, run: Mapping[str, Any]) -> BenchmarkConfig | None:
        data = dict(run.get("configuration") or {})
        try:
            return BenchmarkConfig(
                scenario_id=str(data["scenario_id"]),
                version=int(data["version"]),
                target_title=str(data["target_title"]),
                duration_seconds=int(data["duration_seconds"]),
                frame_interval_seconds=float(data["frame_interval_seconds"]),
                notes=str(data.get("notes") or ""),
                model_id=str(data.get("model_id") or ""),
                policy_id=str(data.get("policy_id") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _window(self, target: Mapping[str, Any]) -> dict[str, Any]:
        bounds = list(target.get("bounds") or [])
        return {
            "title": str(target.get("title") or ""),
            "handle": target.get("handle"),
            "pid": target.get("pid"),
            "width": target.get("width"),
            "height": target.get("height"),
            "scale": target.get("scale"),
            "bounds": bounds,
        }

    def _summary(self, run: Mapping[str, Any]) -> dict[str, Any]:
        metrics = dict(run.get("metrics") or {})

        def value(key: str) -> Any:
            row = dict(metrics.get(key) or {})
            return row.get("value") if row.get("available") else None

        scenario = dict(run.get("scenario") or {})
        evidence = dict(run.get("evidence") or {})
        return {
            "id": run.get("id"),
            "scenario": {
                "scenario_id": scenario.get("scenario_id"),
                "version": scenario.get("version"),
                "name": scenario.get("name"),
                "kind": scenario.get("kind"),
                "evaluation": scenario.get("evaluation"),
            },
            "status": run.get("status"),
            "valid": bool(run.get("valid")),
            "reason": run.get("reason") or "",
            "session_id": run.get("session_id") or "",
            "created_at": run.get("created_at"),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "duration_seconds": run.get("duration_seconds"),
            "target_title": str((run.get("target") or {}).get("title") or ""),
            "metrics": {
                "frames": value("frames"),
                "latency_median_ms": value("latency_median_ms"),
                "latency_p95_ms": value("latency_p95_ms"),
                "frame_yield": value("frame_yield"),
                "max_gap_ms": value("max_gap_ms"),
            },
            "evidence": {
                "session_available": bool(evidence.get("session_available")),
                "timeline_endpoint": str(evidence.get("timeline_endpoint") or ""),
                "gaps": list(evidence.get("gaps") or []),
            },
            "interrupted": bool(run.get("interrupted")),
        }

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - an observer cannot fail a run
            return
