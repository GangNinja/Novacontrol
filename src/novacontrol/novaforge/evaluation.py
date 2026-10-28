"""NovaForge NF-05 — controlled, baseline-first game evaluation.

An evaluation is not a second runner. It composes two existing things:

* **NF-04's benchmark lab** owns the bounded observation session (start, deadline,
  cancellation, telemetry/timeline collection, persistence and restart recovery).
* **NF-05's compatibility registry** decides whether an environment may be
  evaluated at all, in which mode, and on what evidence.

This module adds the evaluation record: which game, which scenario, the readiness
checks that were judged, the benchmark run and session underneath, the baseline
it is compared with, and the per-game report. It never chooses an action, and it
never decides an objective itself.

A **controlled** evaluation is an operator-authored plan, approved by digest and
executed through NF-07's Guardian-gated action path; the verdict comes from the
session's independent verifier. An **observation-only** evaluation still sends no
input and configures no objective.

Honesty rules:

* **A refusal is not a run.** A readiness failure is answered with its blocking
  checks; only an attempt against a KNOWN environment is retained (as
  ``unsupported`` or ``invalid``), because hiding attempted evaluations would be
  the same lie as hiding failed ones.
* **Baseline-first.** The first completed evaluation of a game/scenario/version is
  the baseline; later runs are candidates and are compared through NF-04's own
  compatibility rules. An incompatible baseline yields a refusal, never a delta.
* **Two modes, two meanings.** An observation-only evaluation configures no
  objective, so its completion is data completeness, never a won game. A
  controlled evaluation is COMPLETED when its run record is complete and
  ``verified`` only when the verifier decided the objective; an undecided run is
  inconclusive, never a success.
* **A controlled plan's ending is the plan's.** The plan ends when its last
  approved action executes, and a session whose budget is spent closes on its own
  ``action_budget`` bound; a verified success closes via ``objective_verified``.
  Both are completed controlled runs — only a stop outside the runner is aborted.
* **One session slot.** The benchmark lab enforces one active run, so an
  evaluation can never overlap another evaluation or a Mission Control session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from novacontrol.persistence.json_store import JsonStateStore

from .actions import ActionPolicy, GameplayAction
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
from .models import SESSION_TERMINAL_STATES, MissionValidationError, utc_now
from .objective import ObjectiveDefinition, utc_stamp

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

#: A controlled evaluation's approval is single-use and short-lived: it names
#: one plan, one objective, one target identity and one set of limits. The
#: operator must re-run preflight to approve anything else.
CONTROL_APPROVAL_TTL_SECONDS = 600.0
MAX_PLAN_ACTIONS = 8
MAX_CONTROL_DURATION_SECONDS = 300
DEFAULT_ACTION_INTERVAL_SECONDS = 0.5
DEFAULT_VERIFY_TIMEOUT_SECONDS = 15.0
MAX_VERIFY_TIMEOUT_SECONDS = 60.0
#: How long the control runner waits for the benchmark run to report the
#: session it started. The benchmark starts a session asynchronously.
SESSION_WAIT_SECONDS = 15.0
#: How long the control runner waits for the session's OWN observation loop to
#: confirm the target identity. Input is refused until it does, so a plan must
#: not race the first tick: the session validated the window asynchronously and
#: the runner used to execute into that refusal.
TARGET_VALIDATION_WAIT_SECONDS = 15.0


def _canonical_action(action: GameplayAction) -> dict[str, Any]:
    """An action as the digest and the record see it: no per-parse identifier.

    ``action_id`` is generated when a payload is parsed, so including it would
    make the same plan digest differently on every preflight — an approval that
    can never match the thing it approved.
    """
    data = action.to_dict()
    data.pop("action_id", None)
    return data


def _plan_digest(
    *,
    game: GameRecord,
    scenario: BenchmarkScenario,
    identity: Mapping[str, Any] | None,
    objective: ObjectiveDefinition,
    actions: Sequence[GameplayAction],
    duration_seconds: Any,
    max_actions: Any,
    action_interval_seconds: float,
) -> str:
    """The one digest an operator approves for a controlled evaluation.

    It covers the environment and scenario versions, the exact target identity,
    the objective definition, every action in order and the two bounds. Anything
    a person would want to read before approving is in here, so changing it
    invalidates the approval instead of silently running something else.
    """
    body = {
        "mode": "control",
        "game": {"game_id": game.game_id, "version": game.version},
        "scenario": {"scenario_id": scenario.scenario_id, "version": scenario.version},
        "target": {
            "title": str((identity or {}).get("title") or ""),
            "handle": (identity or {}).get("handle"),
            "pid": (identity or {}).get("pid"),
        },
        "objective": objective.to_dict(),
        "actions": [_canonical_action(item) for item in actions],
        "limits": {
            "duration_seconds": duration_seconds,
            "max_actions": max_actions,
            "action_interval_seconds": round(float(action_interval_seconds), 3),
        },
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _int_or_none(value: Any) -> int | None:
    """A strict optional integer: ``True`` and ``"2.5"`` are not numbers here."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _seconds_between(start: Any, end: Any) -> float | None:
    """``end - start`` in seconds from two ISO stamps, or ``None``."""
    try:
        begin = datetime.fromisoformat(str(start))
        finish = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    seconds = (finish - begin).total_seconds()
    if seconds < 0:
        return None
    return round(seconds, 3)


@dataclass(slots=True)
class _ControlPlan:
    """A parsed controlled-evaluation request, before anything is approved."""

    objective: ObjectiveDefinition | None = None
    objective_error: str = ""
    actions: list[GameplayAction] = field(default_factory=list)
    plan_errors: list[str] = field(default_factory=list)
    max_actions: int | None = None
    duration_seconds: int | None = None
    action_interval_seconds: float = DEFAULT_ACTION_INTERVAL_SECONDS
    verify_timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS

    @property
    def errors(self) -> list[str]:
        problems = list(self.plan_errors)
        if self.objective_error:
            problems.insert(0, self.objective_error)
        return problems


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
        #: Single-use approvals minted by preflight and consumed by start. In
        #: memory on purpose: an approval that outlives the process is not an
        #: approval of a window that still exists.
        self._approvals: dict[str, dict[str, Any]] = {}
        self._control_tasks: dict[str, asyncio.Task[None]] = {}
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
        """The registry with the LIVE capability overlay.

        The catalog's declared rows describe what each environment requires; a
        reader of this endpoint needs what THIS host has, so the manager's own
        report replaces them.
        """
        return game_catalog(runtime=self._runtime_capabilities() or None)

    def game(self, game_id: str, version: Any = None) -> dict[str, Any]:
        game = resolve_game(game_id, version)
        if game is None:
            raise GameNotFoundError(
                f"Unknown game or environment: {game_id!r} {version!r}"
            )
        payload = game.to_dict(runtime=self._runtime_capabilities() or None)
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

    # -- controlled evaluation: plan, approval and guarded execution --------

    def _runtime_capabilities(self) -> dict[str, Any]:
        capabilities = getattr(self.manager, "capabilities", None)
        if not callable(capabilities):
            return {}
        try:
            return dict(capabilities() or {})
        except Exception:  # noqa: BLE001 - a capability report that raises is absent
            return {}

    def _parse_control_plan(
        self,
        data: Mapping[str, Any],
        game: GameRecord,
        scenario: BenchmarkScenario,
        identity: Mapping[str, Any] | None,
    ) -> _ControlPlan:
        """Read a controlled request into a plan, or into the reasons it cannot be one.

        Nothing here executes, injects or approves. It is a strict parse: a
        missing objective, an empty plan, an unlisted key, a plan longer than its
        budget or an unbounded run is a REFUSAL with a reason, never a default.
        """
        plan = _ControlPlan()
        raw_objective = data.get("objective")
        if not isinstance(raw_objective, Mapping) or not raw_objective:
            plan.objective_error = (
                "A controlled evaluation needs an explicit objective definition; without "
                "one there is nothing to verify."
            )
        else:
            try:
                plan.objective = ObjectiveDefinition.from_payload(raw_objective)
            except ValueError as exc:
                plan.objective_error = str(exc)
            else:
                problems = plan.objective.errors()
                if problems:
                    plan.objective_error = " ".join(problems)

        raw_actions = data.get("action_plan")
        policy = getattr(self.manager, "action_policy", None) or ActionPolicy()
        if not isinstance(raw_actions, list) or not raw_actions:
            plan.plan_errors.append(
                "A controlled evaluation needs an action plan with at least one action."
            )
        else:
            if len(raw_actions) > MAX_PLAN_ACTIONS:
                plan.plan_errors.append(
                    f"An action plan may contain at most {MAX_PLAN_ACTIONS} actions "
                    f"(asked for {len(raw_actions)})."
                )
            for index, raw in enumerate(raw_actions[:MAX_PLAN_ACTIONS], start=1):
                if not isinstance(raw, Mapping):
                    plan.plan_errors.append(f"Action {index} is not an object.")
                    continue
                try:
                    action = GameplayAction.from_payload(dict(raw))
                except ValueError as exc:
                    plan.plan_errors.append(f"Action {index}: {exc}")
                    continue
                problems = action.validate(policy)
                if problems:
                    plan.plan_errors.append(
                        f"Action {index} ({action.kind.value}): " + " ".join(problems)
                    )
                    continue
                plan.actions.append(action)

        plan.max_actions = _int_or_none(data.get("max_actions"))
        if plan.max_actions is None:
            plan.plan_errors.append(
                "A controlled evaluation needs an explicit action budget (max_actions); "
                "an unbounded plan is not approveable."
            )
        elif not (1 <= plan.max_actions <= 10_000):
            plan.plan_errors.append("max_actions must be between 1 and 10000.")
        elif plan.actions and len(plan.actions) > plan.max_actions:
            plan.plan_errors.append(
                f"The plan has {len(plan.actions)} actions but the budget allows "
                f"{plan.max_actions}."
            )

        duration = _int_or_none(data.get("duration_seconds"))
        if duration is None:
            duration = scenario.default_duration_seconds
        if not (1 <= duration <= MAX_CONTROL_DURATION_SECONDS):
            plan.plan_errors.append(
                f"A controlled run's duration must be between 1 and "
                f"{MAX_CONTROL_DURATION_SECONDS} seconds (asked for {duration})."
            )
        plan.duration_seconds = duration

        interval = _float_or_none(data.get("action_interval_seconds"))
        interval = DEFAULT_ACTION_INTERVAL_SECONDS if interval is None else interval
        if not (0.05 <= interval <= 5.0):
            plan.plan_errors.append(
                "action_interval_seconds must be between 0.05 and 5."
            )
        plan.action_interval_seconds = interval

        timeout = _float_or_none(data.get("verify_timeout_seconds"))
        timeout = DEFAULT_VERIFY_TIMEOUT_SECONDS if timeout is None else timeout
        if not (0.5 <= timeout <= MAX_VERIFY_TIMEOUT_SECONDS):
            plan.plan_errors.append(
                f"verify_timeout_seconds must be between 0.5 and "
                f"{int(MAX_VERIFY_TIMEOUT_SECONDS)}."
            )
        plan.verify_timeout_seconds = timeout
        return plan

    @staticmethod
    def _control_states() -> list[dict[str, Any]]:
        """The six states the panel must show separately, all initially empty.

        A completed run and a verified objective are different rows here on
        purpose: collapsing them is exactly the confusion this phase exists to
        remove.
        """
        return [
            {"stage": stage, "state": "pending", "at": "", "reason": ""}
            for stage in (
                "proposal",
                "approval",
                "execution",
                "verification",
                "refusal",
                "cleanup",
            )
        ]

    def _control_block(
        self, plan: _ControlPlan, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return {
            "mode": "control",
            "plan_digest": "",
            "objective": plan.objective.to_dict() if plan.objective else None,
            "objective_digest": plan.objective.digest() if plan.objective else "",
            "actions": [_canonical_action(action) for action in plan.actions],
            "limits": {
                "duration_seconds": plan.duration_seconds,
                "max_actions": plan.max_actions,
                "action_interval_seconds": round(plan.action_interval_seconds, 3),
                "verify_timeout_seconds": round(plan.verify_timeout_seconds, 3),
            },
            "target": dict(identity or {}),
            "approval": {},
            "verifier": "",
            "state": "refused" if plan.errors else "ready",
            "steps": [],
            "states": self._control_states(),
            "verdict": "pending",
            "reason": "; ".join(plan.errors) if plan.errors else "",
            "session_id": "",
            "benchmark_run_id": "",
        }

    @staticmethod
    def _set_control_state(
        control: dict[str, Any], stage: str, state: str, reason: str = ""
    ) -> None:
        rows = control.get("states")
        if not isinstance(rows, list):
            rows = []
            control["states"] = rows
        for row in rows:
            if isinstance(row, dict) and row.get("stage") == stage:
                row["state"] = state
                row["at"] = utc_stamp()
                row["reason"] = str(reason)
                return
        rows.append(
            {"stage": stage, "state": str(state), "at": utc_stamp(), "reason": str(reason)}
        )

    def _mint_approval(self, control: dict[str, Any]) -> dict[str, Any]:
        """Mint the single-use approval a controlled start must echo back."""
        digest = str(control.get("plan_digest") or "")
        now = time.time()
        self._prune_approvals()
        approval = {
            "digest": digest,
            "issued_at": _stamp(now),
            "expires_at": _stamp(now + CONTROL_APPROVAL_TTL_SECONDS),
            "expires_at_epoch": now + CONTROL_APPROVAL_TTL_SECONDS,
            "single_use": True,
            "requires_confirmation": True,
            "objective_digest": str(control.get("objective_digest") or ""),
            "actions": len(control.get("actions") or []),
        }
        self._approvals[digest] = approval
        return dict(approval)

    def _prune_approvals(self) -> None:
        now = time.time()
        for key in [
            key
            for key, entry in self._approvals.items()
            if float(entry.get("expires_at_epoch") or 0) <= now
        ]:
            self._approvals.pop(key, None)

    def _consume_approval(self, digest: str, expected: str) -> dict[str, Any]:
        """Consume the approval for this exact plan, or refuse with the reason."""
        self._prune_approvals()
        if not digest:
            raise EvaluationError(
                [
                    "A controlled evaluation needs the plan digest from a passing "
                    "preflight; approval is not implied by starting."
                ]
            )
        entry = self._approvals.pop(digest, None)
        if entry is None:
            raise EvaluationError(
                [
                    "No unexpired approval was issued for this digest. Validate readiness "
                    "again and approve the digest it returns for this exact plan."
                ]
            )
        if digest != expected:
            raise EvaluationError(
                [
                    "The approval does not match this plan. The objective, actions, target "
                    "or limits changed since it was issued; validate again."
                ]
            )
        return entry

    def _control_objective(self, control: Mapping[str, Any]) -> ObjectiveDefinition | None:
        payload = control.get("objective")
        if not isinstance(payload, Mapping):
            return None
        return ObjectiveDefinition.from_dict(payload)

    def _control_actions(self, control: Mapping[str, Any]) -> list[GameplayAction]:
        actions: list[GameplayAction] = []
        for entry in control.get("actions") or []:
            if not isinstance(entry, Mapping):
                continue
            action = GameplayAction.from_dict(dict(entry))
            if action is not None:
                actions.append(action)
        return actions

    def _revalidate_control(self, record: Mapping[str, Any]) -> list[str]:
        """Re-derive the approval from the stored plan, every time before acting.

        This is the check that makes a stored approval mean something: the plan,
        objective, target identity, limits and expiry are all re-read and the
        digest recomputed. A modified record cannot execute on an old approval.
        """
        control = dict(record.get("control") or {})
        if not control:
            return ["This evaluation has no controlled plan to revalidate."]
        problems: list[str] = []
        game = resolve_game(
            str((record.get("game") or {}).get("game_id")),
            (record.get("game") or {}).get("version"),
        )
        scenario = resolve_scenario(
            str((record.get("scenario") or {}).get("scenario_id")),
            (record.get("scenario") or {}).get("version"),
        )
        objective = self._control_objective(control)
        actions = self._control_actions(control)
        limits = dict(control.get("limits") or {})
        if game is None or scenario is None:
            return [
                "The evaluation's environment or scenario is no longer resolvable, so its "
                "approval cannot be revalidated."
            ]
        if objective is None or not actions:
            return [
                "The approved objective or action plan is not readable, so the approval "
                "cannot be revalidated."
            ]
        expected = _plan_digest(
            game=game,
            scenario=scenario,
            identity=dict(control.get("target") or {}),
            objective=objective,
            actions=actions,
            duration_seconds=limits.get("duration_seconds"),
            max_actions=limits.get("max_actions"),
            action_interval_seconds=float(limits.get("action_interval_seconds") or 0.0),
        )
        if expected != control.get("plan_digest"):
            problems.append(
                "The approved plan digest no longer matches the stored plan; the plan, "
                "objective, target or limits changed after approval."
            )
        approval = dict(control.get("approval") or {})
        if approval.get("digest") != control.get("plan_digest"):
            problems.append("The stored approval does not name this plan's digest.")
        expires = approval.get("expires_at_epoch")
        if expires not in (None, "") and time.time() > float(expires):
            problems.append(
                "The approval has expired; a controlled run must be approved and started "
                "together."
            )

        session_id = str(control.get("session_id") or record.get("session_id") or "")
        live = self.manager.get(session_id) if session_id else None
        if session_id and live is None:
            problems.append("The approved session is no longer available.")
        if live is not None:
            if live.state in SESSION_TERMINAL_STATES:
                problems.append(
                    f"The session is {live.state.value}; no further action is allowed."
                )
            target = live.target
            identity = dict(control.get("target") or {})
            if target is not None and identity.get("handle") not in (None, ""):
                if int(target.handle) != int(identity["handle"]):
                    problems.append(
                        "The session's target handle no longer matches the approved window."
                    )
            if target is not None and identity.get("pid") not in (None, "", 0):
                if int(target.pid) != int(identity["pid"]):
                    problems.append(
                        "The session's target process no longer matches the approved window."
                    )
            if (
                live.objective is not None
                and live.objective.digest() != objective.digest()
            ):
                problems.append(
                    "The session's objective no longer matches the approved definition."
                )
            budget = live.mission.max_actions
            if budget is not None and budget < len(actions):
                problems.append(
                    f"The session's action budget ({budget}) is smaller than the approved "
                    f"plan ({len(actions)})."
                )
            policy = getattr(self.manager, "action_policy", None) or ActionPolicy()
            for index, action in enumerate(actions, start=1):
                issues = action.validate(policy)
                if issues:
                    problems.append(f"Action {index}: " + " ".join(issues))
        return problems

    async def _wait_for_session(self, record: Mapping[str, Any]) -> str:
        """Wait, bounded, for the benchmark run to report the session it started."""
        run_id = str((record.get("control") or {}).get("benchmark_run_id") or "")
        if not run_id:
            return ""
        deadline = time.monotonic() + SESSION_WAIT_SECONDS
        while time.monotonic() < deadline:
            run = self.benchmark.get(run_id) or {}
            session_id = str(run.get("session_id") or "")
            if session_id:
                return session_id
            if str(run.get("status") or "") != "running":
                return ""
            await asyncio.sleep(0.05)
        return ""

    async def _await_target_validated(self, session_id: str) -> str:
        """Wait for the session's own target confirmation; "" when it arrived.

        The observation loop validates the pinned window on its first tick, after
        the session exists. ``plan_action`` refuses input on an unconfirmed
        target, so a plan that started executing the instant the session id
        appeared was refused by a race rather than by the plan. Waiting here is
        bounded and never assumes success: an ended, paused or never-confirmed
        session returns the reason it is not usable.
        """
        deadline = time.monotonic() + TARGET_VALIDATION_WAIT_SECONDS
        while True:
            session = self.manager.get(session_id)
            if session is None:
                return (
                    "The session record disappeared before its observation loop confirmed "
                    "the target identity, so no input was sent."
                )
            if bool(getattr(session, "target_validated", False)):
                return ""
            state = getattr(session, "state", "")
            state_value = str(getattr(state, "value", state) or "")
            if state_value == "PAUSED":
                return (
                    "The session paused before it confirmed the target identity "
                    f"({getattr(session, 'pause_reason', '') or 'no reason recorded'}); "
                    "input is not allowed on an unconfirmed target."
                )
            if state_value in {"FAILED", "STOPPED", "COMPLETED"}:
                return (
                    "The session ended before its observation loop confirmed the target "
                    f"identity ({state_value}), so no input was sent."
                )
            if time.monotonic() >= deadline:
                return (
                    "The observation loop did not confirm the target identity within "
                    f"{TARGET_VALIDATION_WAIT_SECONDS:.0f} seconds; input is not allowed on "
                    "an unconfirmed target."
                )
            await asyncio.sleep(0.05)

    async def _control_fail(
        self,
        record: dict[str, Any],
        reason: str,
        *,
        stop_session: bool = True,
        stage: str = "refusal",
    ) -> None:
        """Record a controlled run's refusal without ever calling it a result."""
        control = record.get("control")
        if not isinstance(control, dict):
            return
        control["state"] = "refused"
        control["reason"] = str(reason)
        control["verdict"] = "refused"
        self._set_control_state(control, stage, "refused", reason)
        errors = record.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append({"message": str(reason)[:500], "where": "control", "at": utc_now()})
        session_id = str(control.get("session_id") or record.get("session_id") or "")
        if session_id:
            with contextlib.suppress(Exception):
                self.manager.report_control_event(
                    session_id,
                    "control.refused",
                    {"reason": reason, "plan_digest": control.get("plan_digest")},
                    severity="error",
                )
            if stop_session:
                with contextlib.suppress(Exception):
                    await self.manager.stop(session_id, reason="evaluation_control_refused")
        self._persist(verify_id=str(record.get("id")))

    async def _await_verdict(
        self, session_id: str, *, timeout_seconds: float, action_id: str = ""
    ) -> dict[str, Any]:
        """Poll the evidence quietly, then record ONE verdict.

        A game may write its state a moment after receiving a key, so the plan
        runner waits. It does not record a row per poll: those rows would describe
        the clock, not the objective.
        """
        deadline = time.monotonic() + max(0.5, timeout_seconds)
        while True:
            record = self.manager.get(session_id)
            if record is None:
                break
            if record.state in SESSION_TERMINAL_STATES:
                break
            try:
                probe = await self.manager.probe_objective(session_id, action_id=action_id)
            except Exception:  # noqa: BLE001 - the recording call below reports it
                break
            if bool(probe.get("decided")):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        return await self.manager.verify_objective(
            session_id, reason="control_plan_completed", action_id=action_id
        )

    async def _run_control(self, evaluation_id: str) -> None:
        """Execute the approved plan through NF-07's gates, then record a verdict.

        This runner never chooses an action: every step is the operator's, already
        approved and digest-bound. Between steps it re-derives the digest and
        re-checks the live session, because a stored approval describes the plan
        that WAS approved, not whatever the record says now.
        """
        record = self._evaluations.get(evaluation_id)
        if record is None or not isinstance(record.get("control"), dict):
            return
        control = record["control"]
        objective = self._control_objective(control)
        if objective is None:
            await self._control_fail(record, "The approved objective is not readable.")
            return
        self._set_control_state(control, "proposal", "recorded", "The operator authored this plan.")

        session_id = await self._wait_for_session(record)
        if not session_id:
            await self._control_fail(
                record,
                "The benchmark run did not start a session within the bounded wait, so the "
                "approved plan was never executed.",
                stop_session=False,
            )
            return
        control["session_id"] = session_id
        record["session_id"] = record.get("session_id") or session_id

        problems = self._revalidate_control(record)
        if problems:
            await self._control_fail(record, " ".join(problems))
            return
        try:
            attached = self.manager.set_objective(session_id, objective.to_dict())
        except Exception as exc:  # noqa: BLE001 - a refusal is the result
            await self._control_fail(
                record, f"The objective could not be attached: {type(exc).__name__}: {exc}"
            )
            return
        if not attached.get("supported"):
            await self._control_fail(
                record,
                str(attached.get("reason") or "No ready verifier can check this objective."),
            )
            return
        control["verifier"] = str(attached.get("verifier") or "")
        control["verifier_version"] = int(attached.get("verifier_version") or 0)
        control["state"] = "approved"
        self._set_control_state(
            control,
            "approval",
            "approved",
            f"Operator approved plan digest {control.get('plan_digest', '')[:16]}…",
        )
        with contextlib.suppress(Exception):
            self.manager.report_control_event(
                session_id,
                "control.approved",
                {
                    "plan_digest": control.get("plan_digest"),
                    "objective_digest": control.get("objective_digest"),
                    "objective": objective.to_dict(),
                    "actions": len(control.get("actions") or []),
                    "limits": dict(control.get("limits") or {}),
                    "target": dict(control.get("target") or {}),
                    "approval": dict(control.get("approval") or {}),
                },
            )
        self._persist(verify_id=evaluation_id)

        unconfirmed = await self._await_target_validated(session_id)
        if unconfirmed:
            await self._control_fail(record, unconfirmed)
            return

        actions = self._control_actions(control)
        interval = float((control.get("limits") or {}).get("action_interval_seconds") or 0.0)
        self._set_control_state(control, "execution", "running", "Executing the approved plan.")
        refusal = ""
        for index, action in enumerate(actions, start=1):
            if record.get("status") != "running":
                refusal = "The evaluation ended before the plan finished."
                break
            problems = self._revalidate_control(record)
            if problems:
                refusal = " ".join(problems)
                break
            live = self.manager.get(session_id)
            if live is None or live.state in SESSION_TERMINAL_STATES:
                refusal = (
                    "The session ended before the plan finished, so the remaining actions "
                    "were not executed."
                )
                break
            planned = self.manager.plan_action(session_id, _canonical_action(action))
            step: dict[str, Any] = {
                "index": index,
                "action": _canonical_action(action),
                "status": str(planned.get("status") or ""),
                "proposal_id": "",
                "digest": "",
                "reason": "",
                "at": utc_stamp(),
            }
            if planned.get("status") != "proposed":
                step["reason"] = str(
                    (planned.get("outcome") or {}).get("reason")
                    or "The action was refused before execution."
                )
                control["steps"].append(step)
                refusal = step["reason"]
                break
            proposal = dict(planned.get("proposal") or {})
            step["proposal_id"] = str(proposal.get("proposal_id") or "")
            step["digest"] = str(proposal.get("digest") or "")
            try:
                outcome = await self.manager.execute_action(
                    session_id,
                    {"proposal_id": step["proposal_id"], "digest": step["digest"]},
                )
            except Exception as exc:  # noqa: BLE001 - recorded as the step's refusal
                step["status"] = "failed"
                step["reason"] = f"Execution raised {type(exc).__name__}: {exc}"
                control["steps"].append(step)
                refusal = step["reason"]
                break
            result = dict(outcome.get("outcome") or {})
            step["status"] = str(outcome.get("status") or "")
            step["reason"] = str(result.get("reason") or "")
            step["elapsed_ms"] = result.get("elapsed_ms")
            step["remaining_actions"] = outcome.get("remaining_actions")
            verification = outcome.get("verification")
            if isinstance(verification, Mapping):
                step["verification"] = {
                    "outcome": verification.get("outcome"),
                    "achieved": verification.get("achieved"),
                }
            control["steps"].append(step)
            self._persist(verify_id=evaluation_id)
            if step["status"] != "executed":
                refusal = step["reason"] or "The action did not execute."
                break
            if index < len(actions) and interval > 0:
                await asyncio.sleep(interval)

        if refusal:
            self._set_control_state(control, "execution", "refused", refusal)
            self._set_control_state(
                control,
                "verification",
                "skipped",
                "The plan was refused, so there is no plan outcome to verify.",
            )
            await self._control_fail(record, refusal)
            return

        self._set_control_state(control, "execution", "completed", "Every approved action executed.")
        self._set_control_state(control, "verification", "running", "Reading the objective's evidence.")
        self._persist(verify_id=evaluation_id)
        timeout = float((control.get("limits") or {}).get("verify_timeout_seconds") or 0.0)
        try:
            verdict = await self._await_verdict(session_id, timeout_seconds=timeout)
        except Exception as exc:  # noqa: BLE001 - a verifier fault is recorded, not raised
            await self._control_fail(
                record,
                f"The objective could not be read after the plan: {type(exc).__name__}: {exc}",
                stop_session=False,
                stage="verification",
            )
            return
        verdict_outcome = str(verdict.get("outcome") or "unknown")
        if bool(verdict.get("decided")):
            control["verdict"] = verdict_outcome
        else:
            control["verdict"] = "inconclusive"
        control["state"] = "verified" if bool(verdict.get("decided")) else "inconclusive"
        control["verification"] = {
            "outcome": verdict_outcome,
            "achieved": verdict.get("achieved"),
            "decided": bool(verdict.get("decided")),
            "reason": str(verdict.get("reason") or ""),
            "at": str(verdict.get("at") or ""),
            "verifier": str(verdict.get("verifier") or ""),
            "verifier_version": int(verdict.get("verifier_version") or 0),
            "evidence": list(verdict.get("evidence") or []),
        }
        self._set_control_state(
            control,
            "verification",
            verdict_outcome,
            str(verdict.get("reason") or "No reason was recorded."),
        )
        if not bool(verdict.get("decided")):
            self._set_control_state(
                control,
                "refusal",
                "not_applicable",
                "Nothing was refused; the objective was simply not decided within the run.",
            )
        else:
            # A completed plan with a decided verdict refused nothing. Leaving the
            # stage at its initial ``pending`` asks the reader what is still owed on
            # a run that is over, so it is closed as not applicable instead.
            self._set_control_state(
                control,
                "refusal",
                "not_applicable",
                "Nothing was refused; the plan ran to a decided verdict.",
            )
        self._persist(verify_id=evaluation_id)

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
        identity = (
            {
                "title": target.get("title"),
                "handle": target.get("handle"),
                "pid": target.get("pid"),
            }
            if target
            else None
        )
        runtime = self._runtime_capabilities()
        verifier = dict(runtime.get("objective_verification") or {})
        ready_kinds = [str(item) for item in verifier.get("ready_kinds") or [] if str(item)]
        plan: _ControlPlan | None = None
        control: dict[str, Any] | None = None
        approval: dict[str, Any] | None = None
        objective_supported = False
        objective_reason = ""
        if mode == "control" and game is not None and scenario is not None:
            plan = self._parse_control_plan(data, game, scenario, identity)
            control = self._control_block(plan, identity)
            if plan.objective is not None:
                objective_supported = plan.objective.kind.value in ready_kinds and not plan.objective.errors()
                if not objective_supported:
                    objective_reason = (
                        f"The {plan.objective.kind.value!r} objective is not checkable on this "
                        "host. Ready verifier kinds: "
                        + (", ".join(ready_kinds) or "none")
                        + ". "
                        + str(verifier.get("reason") or "")
                    ).strip()
            if plan.errors:
                errors.extend(problem for problem in plan.errors if problem not in errors)

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
                    runtime_input=bool((runtime.get("action_execution") or {}).get("available")),
                    verifier_kinds=ready_kinds,
                    objective=plan.objective.to_dict() if plan and plan.objective else None,
                    objective_supported=objective_supported,
                    objective_reason=objective_reason,
                    plan_errors=plan.errors if plan else (),
                    plan_length=len(plan.actions) if plan else 0,
                    action_budget=plan.max_actions if plan else None,
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
        ready = bool(summary["ready"] and not errors)
        if control is not None:
            control["state"] = "ready" if ready else "refused"
            control["reason"] = "" if ready else " ".join(errors) or "A required check failed."
            self._set_control_state(
                control,
                "proposal",
                "recorded",
                "The operator authored this plan; nothing is executed by a preflight.",
            )
            self._set_control_state(
                control,
                "approval",
                "required" if ready else "not_issued",
                (
                    "Starting this plan requires its digest. Approving it is a separate, "
                    "explicit act."
                    if ready
                    else "Readiness refused the plan, so no approval was issued."
                ),
            )
            if ready and plan is not None and plan.objective is not None:
                # ``ready`` cannot be true unless both were resolved above, and
                # the digest is the thing that would be unsafe to compute from a
                # half-known environment, so this is a real invariant, not a cast.
                assert game is not None and scenario is not None
                control["plan_digest"] = _plan_digest(
                    game=game,
                    scenario=scenario,
                    identity=identity,
                    objective=plan.objective,
                    actions=plan.actions,
                    duration_seconds=plan.duration_seconds,
                    max_actions=plan.max_actions,
                    action_interval_seconds=plan.action_interval_seconds,
                )
                control["verifier"] = str(
                    next(
                        (
                            str(row.get("verifier") or "")
                            for row in verifier.get("kinds") or []
                            if row.get("kind") == plan.objective.kind.value and row.get("available")
                        ),
                        "",
                    )
                )
                approval = self._mint_approval(control)
                control["approval"] = dict(approval)
                self._set_control_state(
                    control,
                    "approval",
                    "issued",
                    "A single-use approval was issued for this exact plan digest.",
                )
        baseline = self.baseline_payload(
            game,
            scenario,
            mode=mode,
            control_digest=str((control or {}).get("plan_digest") or ""),
        ) if game and scenario else None
        return {
            "version": EVALUATION_VERSION,
            "ready": ready,
            "errors": errors,
            "blocking": list(summary["blocking"]),
            "checks": list(summary["checks"]),
            "counts": dict(summary["counts"]),
            "game": game.to_dict(runtime=runtime or None) if game is not None else None,
            "scenario": scenario.to_dict() if scenario is not None else None,
            "mode": mode,
            "benchmark": benchmark_preflight,
            "target": target,
            "identity": identity,
            "control": control,
            "approval": approval,
            "runtime_capabilities": {
                "available": bool(ready_kinds),
                "ready_kinds": ready_kinds,
                "input_available": bool((runtime.get("action_execution") or {}).get("available")),
                "reason": str(verifier.get("reason") or ""),
            },
            "baseline": baseline,
            "limitations": (
                evaluation_limitations(game, scenario, mode=mode)
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
        self,
        game_id: str,
        scenario_id: str,
        version: int,
        *,
        mode: str = "",
        control_digest: str = "",
    ) -> list[dict[str, Any]]:
        completed = [
            record
            for record in self._evaluations.values()
            if record.get("status") == "completed"
            and bool(record.get("valid"))
            and str((record.get("game") or {}).get("game_id")) == game_id
            and str((record.get("scenario") or {}).get("scenario_id")) == scenario_id
            and _version_of((record.get("scenario") or {}).get("version")) == version
            and (
                not mode
                or str(record.get("mode") or "observation_only") == mode
            )
            and (
                not control_digest
                or str((record.get("control") or {}).get("plan_digest") or "")
                == control_digest
            )
        ]
        return sorted(completed, key=lambda item: str(item.get("created_at") or ""))

    def baseline_payload(
        self,
        game: GameRecord | None,
        scenario: Any,
        *,
        mode: str = "",
        control_digest: str = "",
    ) -> dict[str, Any]:
        """The current baseline for one environment/scenario/version.

        A controlled run is only comparable with the same plan and objective, so
        the digest is part of the baseline key. Pooling an observation run with a
        controlled one would compare "we watched" to "we acted", which is exactly
        the dishonest delta this layer must never produce.
        """
        if game is None or scenario is None:
            return {
                "available": False,
                "reason": "No environment or scenario was resolved.",
                "evaluation_id": "",
                "benchmark_run_id": "",
            }
        completed = self._completed_for(
            game.game_id,
            scenario.scenario_id,
            scenario.version,
            mode=mode,
            control_digest=control_digest,
        )
        if not completed:
            return {
                "available": False,
                "role": "baseline",
                "reason": (
                    "No completed evaluation exists yet for this exact environment, "
                    "scenario, mode"
                    + (" and approved plan" if control_digest else "")
                    + "; the first completed one becomes the baseline."
                ),
                "evaluation_id": "",
                "benchmark_run_id": "",
                "configuration": None,
                "window": None,
                "samples": 0,
                "mode": mode or "any",
                "plan_digest": control_digest,
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
            "mode": mode or str(reference.get("mode") or "observation_only"),
            "plan_digest": control_digest,
            "objective_digest": str((reference.get("control") or {}).get("objective_digest") or ""),
            "policy": (
                "The OLDEST completed, valid evaluation of the same environment, scenario and "
                "version is the baseline. A baseline recorded on a different configuration, "
                "target geometry or operating system is reported as incompatible, not pooled."
            ),
        }

    def baseline(
        self,
        game_id: str,
        scenario_id: str,
        version: Any = None,
        *,
        mode: str = "",
        plan_digest: str = "",
    ) -> dict[str, Any]:
        game = resolve_game(game_id)
        if game is None:
            raise GameNotFoundError(f"Unknown game or environment: {game_id!r}")
        scenario = resolve_scenario(scenario_id, version)
        if scenario is None:
            raise GameNotFoundError(f"Unknown benchmark scenario: {scenario_id!r}")
        payload = self.baseline_payload(
            game, scenario, mode=mode, control_digest=plan_digest
        )
        payload["game"] = game.to_dict(runtime=self._runtime_capabilities() or None)
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
        # must state that a real session may start.
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
        mode = _requested_mode(preflight)

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
                control=preflight.get("control"),
            )
            # A conflict is answered as a CONFLICT: the same request would be
            # valid once the active run/session ends.
            if "conflicts" in set(preflight.get("blocking") or []):
                raise EvaluationConflictError(reasons or ["Another run is active."])
            raise EvaluationError(reasons or ["The evaluation did not pass preflight."])

        control: dict[str, Any] | None = None
        if mode == "control":
            proposed = preflight.get("control")
            if not isinstance(proposed, Mapping) or not proposed.get("plan_digest"):
                self._record_refusal(
                    game=game,
                    scenario=scenario,
                    payload=data,
                    preflight=preflight,
                    status="invalid",
                    reasons=[
                        "The controlled plan was not approved: preflight returned no plan digest."
                    ],
                    control=preflight.get("control"),
                )
                raise EvaluationError(
                    ["The controlled plan was not approved: no plan digest was issued."]
                )
            control = dict(proposed)
            # A single-use approval, bound to the exact digest preflight issued.
            # A modified plan, objective, target or limit cannot ride on it.
            try:
                approval = self._consume_approval(
                    str(data.get("approval_digest", "") or "").strip(),
                    str(control.get("plan_digest") or ""),
                )
            except EvaluationError as exc:
                self._record_refusal(
                    game=game,
                    scenario=scenario,
                    payload=data,
                    preflight=preflight,
                    status="invalid",
                    reasons=exc.reasons,
                    control=proposed,
                )
                raise
            control["approval"] = dict(approval)
            control["verified"] = False
            self._set_control_state(
                control,
                "approval",
                "approved",
                "The operator echoed this plan digest; the approval is single-use.",
            )

        baseline: dict[str, Any]
        explicit = str(data.get("baseline_id", "") or "").strip()
        if explicit:
            baseline = self._explicit_baseline(explicit, game, scenario)
        else:
            baseline = self.baseline_payload(
                game,
                scenario,
                mode=mode,
                control_digest=str((control or {}).get("plan_digest") or ""),
            )
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
                    control=preflight.get("control"),
                )
                raise EvaluationConflictError([reason])

            evaluation_id = uuid4().hex
            now = utc_now()
            if control is not None:
                objective_block: dict[str, Any] = {
                    "available": True,
                    "configured": True,
                    "verified": False,
                    "achieved": None,
                    "outcome": "in_progress",
                    "decided": False,
                    "objective": control.get("objective"),
                    "verifier": str(control.get("verifier") or ""),
                    "reason": (
                        "The approved plan is running; no objective verdict has been "
                        "recorded yet. A running session is not a satisfied objective."
                    ),
                }
            else:
                objective_block = {
                    "available": False,
                    "configured": False,
                    "verified": False,
                    "achieved": None,
                    "outcome": "unsupported",
                    "decided": False,
                    "objective": None,
                    "verifier": "",
                    "reason": (
                        "An observation-only evaluation configures no objective and "
                        "attaches no verifier. A completed evaluation means its data was "
                        "recorded, never that a game objective was achieved."
                    ),
                }
            record: dict[str, Any] = {
                "version": EVALUATION_VERSION,
                "id": evaluation_id,
                "game": game.to_dict(runtime=self._runtime_capabilities() or None),
                "mode": mode,
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
                "control": control,
                "objective": objective_block,
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
                "limitations": evaluation_limitations(game, scenario, mode=mode),
                "interrupted": False,
                "created_at": now,
                "started_at": now,
                "ended_at": "",
                "duration_seconds": None,
            }
            self._evaluations[evaluation_id] = record
            self._trim()
            self._persist()
            benchmark_payload: dict[str, Any] = {
                "scenario_id": scenario.scenario_id,
                "version": scenario.version,
                "target_title": str(data.get("target_title") or "").strip(),
                "duration_seconds": (
                    (control or {}).get("limits", {}).get("duration_seconds")
                    if control is not None
                    else data.get("duration_seconds")
                ),
                "frame_interval_seconds": data.get("frame_interval_seconds"),
                "notes": str(data.get("notes") or "").strip(),
            }
            if control is not None:
                # The session's own bound: the plan cannot exceed it, and the
                # Guardian refuses the next action once it is spent.
                benchmark_payload["max_actions"] = (control.get("limits") or {}).get(
                    "max_actions"
                )
                control["benchmark_run_id"] = ""
            identity = dict(preflight.get("identity") or {})
            # A caller-pinned identity is the one that must be enforced: it is what
            # the operator measured, and the manager refuses a title now owned by a
            # different window. Without a pin the freshly validated identity is used.
            if data.get("expected_handle") not in (None, ""):
                identity["handle"] = data["expected_handle"]
            if data.get("expected_pid") not in (None, ""):
                identity["pid"] = data["expected_pid"]
            if control is not None:
                approved_objective = self._control_objective(control)
                if approved_objective is None:
                    self._finish_without_run(
                        record,
                        status="invalid",
                        reason=(
                            "The approved objective is not readable, so the plan cannot be "
                            "bound to it; nothing was executed."
                        ),
                    )
                    raise EvaluationError([record["reason"]])
                control["target"] = dict(identity)
                recomputed = _plan_digest(
                    game=game,
                    scenario=scenario,
                    identity=identity,
                    objective=approved_objective,
                    actions=self._control_actions(control),
                    duration_seconds=(control.get("limits") or {}).get("duration_seconds"),
                    max_actions=(control.get("limits") or {}).get("max_actions"),
                    action_interval_seconds=float(
                        (control.get("limits") or {}).get("action_interval_seconds") or 0.0
                    ),
                )
                if recomputed != control.get("plan_digest"):
                    # The plan that was approved and the plan about to run are not
                    # the same object. Refuse rather than execute on a stale approval.
                    self._finish_without_run(
                        record,
                        status="invalid",
                        reason=(
                            "The plan changed between approval and start (the target, "
                            "objective, actions or limits differ), so the approval does "
                            "not cover it; nothing was executed."
                        ),
                    )
                    raise EvaluationError([record["reason"]])
            if identity.get("handle") not in (None, ""):
                benchmark_payload["expected_handle"] = identity["handle"]
            if identity.get("pid") not in (None, ""):
                benchmark_payload["expected_pid"] = identity["pid"]
            record["identity"] = identity

            # The session carries a title objective only when the approved plan
            # says so; a file objective is attached by the control runner once the
            # session exists (its evidence source is a file, not the window).
            if control is None and scenario.objective_required:
                self._finish_without_run(
                    record,
                    status="invalid",
                    reason=(
                        "This scenario requires a configured objective; start it as a "
                        "controlled evaluation instead."
                    ),
                )
                raise EvaluationError([record["reason"]])
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
            record["limitations"] = evaluation_limitations(game, scenario, mode=mode)
            if control is not None:
                control["benchmark_run_id"] = record["benchmark_run_id"]
                control["session_id"] = record["session_id"]
                if not record["session_id"]:
                    # The benchmark starts the session asynchronously; the control
                    # runner waits for it and re-checks the approval first.
                    control["session_id"] = ""
            self._persist(verify_id=evaluation_id)
            if control is not None:
                task = asyncio.create_task(
                    self._run_control(evaluation_id),
                    name=f"novaforge-control-{evaluation_id}",
                )
                self._control_tasks[evaluation_id] = task

                def _drop_control_task(_task: Any, key: str = evaluation_id) -> None:
                    self._control_tasks.pop(key, None)

                task.add_done_callback(_drop_control_task)

        await self._publish(
            "novaforge.evaluation.started",
            {
                "evaluation_id": evaluation_id,
                "game_id": game.game_id,
                "game_version": game.version,
                "scenario": scenario.key,
                "benchmark_run_id": record["benchmark_run_id"],
                "session_id": record["session_id"],
                "mode": mode,
                "plan_digest": str((control or {}).get("plan_digest") or ""),
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
        control: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        mode = _requested_mode(preflight)
        control_block = dict(control) if isinstance(control, Mapping) else None
        if control_block is not None:
            control_block["state"] = "refused"
            control_block["verdict"] = "refused"
            control_block["reason"] = " ".join(str(item) for item in reasons)
            self._set_control_state(
                control_block,
                "approval",
                "not_granted",
                "The evaluation was refused before an approval could be used.",
            )
        record: dict[str, Any] = {
            "version": EVALUATION_VERSION,
            "id": uuid4().hex,
            "game": game.to_dict(runtime=self._runtime_capabilities() or None),
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
            "control": control_block,
            "objective": {
                "available": bool((control_block or {}).get("objective")),
                "configured": bool((control_block or {}).get("objective")),
                "verified": False,
                "achieved": None,
                "outcome": "unsupported",
                "decided": False,
                "objective": (control_block or {}).get("objective"),
                "reason": (
                    "The evaluation did not run, so no objective was evaluated: "
                    + (" ".join(str(item) for item in reasons) or "it was refused.")
                ),
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
            "limitations": evaluation_limitations(game, scenario, mode=mode),
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
        control = record.get("control")
        if isinstance(control, dict):
            control["state"] = "refused"
            control["verdict"] = "refused"
            control["reason"] = reason
            self._set_control_state(control, "execution", "not_started", reason)
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
            live = self.manager.get(record["session_id"]) if record["session_id"] else None
            if live is not None:
                record["objective"] = self._objective_from_session(live, record.get("control"))
                self._sync_control(record, live)
            return

        mapped = _BENCHMARK_TO_EVALUATION.get(str(run.get("status")), "invalid")
        # A refused plan is not a completed plan, whatever the session's own
        # ending was. The benchmark can legitimately finish first (its time
        # limit is a planned ending), so the control block's refusal — not the
        # benchmark status — decides that this controlled evaluation aborted.
        control = record.get("control")
        refused = (
            str(control.get("state") or "") == "refused"
            if isinstance(control, dict)
            else False
        )
        if refused:
            mapped = "aborted"
        record["status"] = mapped
        record["valid"] = mapped == "completed" and bool(run.get("valid"))
        record["reason"] = (
            "The controlled plan was refused: "
            + str((control or {}).get("reason") or "no reason recorded")
            if refused
            else (
                f"Benchmark run {benchmark_id} ended {run.get('status')}: "
                f"{run.get('reason') or 'no reason recorded'}"
            )
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
        run_objective = dict(run.get("objective") or {})
        if run_objective.get("configured"):
            record["objective"] = {
                **run_objective,
                "verified": bool(run_objective.get("decided")),
                "data_complete": mapped == "completed",
            }
        else:
            controlled = str(record.get("mode") or "") == "control"
            record["objective"] = {
                "available": False,
                "configured": False,
                "verified": False,
                "achieved": None,
                "outcome": "unsupported",
                "decided": False,
                "objective": None,
                "data_complete": mapped == "completed",
                "reason": (
                    "The controlled plan ended before an objective was attached, so no "
                    "verdict was recorded; the run is not evidence of success."
                    if controlled
                    else (
                        "An observation-only evaluation configures no objective, so a "
                        "completed evaluation means its data was recorded — never that a "
                        "game objective was achieved."
                    )
                ),
            }
        live = self.manager.get(record["session_id"]) if record["session_id"] else None
        if live is not None:
            self._sync_control(record, live)
        else:
            self._sync_control(record, None)
        record["comparison"] = self._comparison(record)
        self._persist(verify_id=str(record.get("id")))

    def _objective_from_session(
        self, session: Any, control: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """The live session's objective verdict in the evaluation's shape."""
        objective = getattr(session, "objective", None)
        if objective is None:
            return {
                "available": False,
                "configured": False,
                "verified": False,
                "achieved": None,
                "outcome": "unsupported",
                "decided": False,
                "objective": None,
                "reason": "The session has no configured objective.",
            }
        verification = dict(getattr(session, "verification", {}) or {})
        outcome = str(verification.get("outcome") or "in_progress")
        decided = bool(verification.get("decided")) or outcome in {
            "verified_success",
            "verified_failure",
        }
        completion = None
        if decided:
            completion = _seconds_between(
                getattr(session, "started_at", ""), verification.get("at")
            )
        return {
            "available": bool(
                (control or {}).get("approval") or getattr(session, "capabilities", {}).get("objective_verifier")
            ),
            "configured": True,
            "verified": decided,
            "achieved": verification.get("achieved") if decided else None,
            "outcome": outcome,
            "decided": decided,
            "objective": objective.to_dict(),
            "verifier": str(verification.get("verifier") or ""),
            "verifier_version": int(verification.get("verifier_version") or 0),
            "at": str(verification.get("at") or ""),
            "completion_time_seconds": completion,
            "evidence": list(verification.get("evidence") or []),
            "reason": str(verification.get("reason") or (
                "The objective is configured but has not been decided; a running session "
                "is not a satisfied objective."
            )),
        }

    def _sync_control(self, record: dict[str, Any], session: Any) -> None:
        """Keep the control block's states in step with the session that ran it."""
        control = record.get("control")
        if not isinstance(control, dict):
            return
        if session is not None:
            live_id = str(getattr(session, "session_id", "") or "")
            if live_id:
                control["session_id"] = live_id
                record["session_id"] = record.get("session_id") or live_id
        objective = dict(record.get("objective") or {})
        if objective.get("decided"):
            control["verdict"] = str(objective.get("outcome") or "unknown")
            control["state"] = "verified"
            self._set_control_state(
                control,
                "verification",
                str(objective.get("outcome") or "unknown"),
                str(objective.get("reason") or ""),
            )
        elif str(control.get("state") or "") in {"approved", "inconclusive"}:
            control["verdict"] = "inconclusive"
            control["state"] = "inconclusive"
        if str(record.get("status") or "") != "running":
            cleanup = dict(getattr(session, "input_cleanup", {}) or {}) if session is not None else {}
            ok = cleanup.get("ok")
            self._set_control_state(
                control,
                "cleanup",
                "completed" if ok is True else "failed" if ok is False else "unknown",
                str(cleanup.get("note") or (
                    "The session record is no longer available, so input cleanup could not "
                    "be read back; the run is incomplete."
                    if session is None
                    else "Input cleanup is recorded when the session closes."
                )),
            )

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
        mode = str(record.get("mode") or "observation_only")
        control = dict(record.get("control") or {})
        baseline_mode = str(baseline.get("mode") or "")
        if baseline_mode and baseline_mode != mode:
            return {
                "available": True,
                "compatible": False,
                "reasons": [
                    f"Not comparable: the baseline ran {baseline_mode} and this evaluation "
                    f"ran {mode}. Watching and acting are different measurements."
                ],
                "metrics": [],
            }
        baseline_digest = str(baseline.get("plan_digest") or "")
        if mode == "control" and baseline_digest != str(control.get("plan_digest") or ""):
            return {
                "available": True,
                "compatible": False,
                "reasons": [
                    "Not comparable: the approved plan or objective differs from the "
                    "baseline's. A different plan is a different measurement."
                ],
                "metrics": [],
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
        # Stop the plan runner FIRST: a cancellation must not race one more action
        # into a session that is being closed.
        task = self._control_tasks.pop(evaluation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        control = record.get("control")
        if isinstance(control, dict):
            self._set_control_state(
                control, "execution", "cancelled", "The operator cancelled the evaluation."
            )
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
        """Stop the plan runners, read every final benchmark state and persist it."""
        tasks = list(self._control_tasks.values())
        self._control_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
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
        objective = dict(record.get("objective") or {})
        gaps = list(
            missing_evidence(
                benchmark=benchmark,
                timeline=timeline,
                baseline=baseline,
                session_id=session_id,
                objective=objective,
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
        mode = str(record.get("mode") or "observation_only")
        limitations = [str(item) for item in (record.get("limitations") or [])]
        if game is not None and scenario is not None:
            for line in evaluation_limitations(game, scenario, mode=mode):
                if line not in limitations:
                    limitations.append(line)
        record["limitations"] = limitations
        control = dict(record.get("control") or {}) or None
        decided = bool(objective.get("decided"))
        executed_steps = [
            step
            for step in (control or {}).get("steps") or []
            if isinstance(step, Mapping) and step.get("status") == "executed"
        ]
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
            "mode": mode,
            "scenario": dict(record.get("scenario") or {}),
            "configuration": dict(record.get("configuration") or {}),
            "readiness": dict(record.get("readiness") or {}),
            "status": record.get("status"),
            "valid": bool(record.get("valid")),
            "reason": str(record.get("reason") or ""),
            "objective": objective,
            "control": control,
            "approval": dict((control or {}).get("approval") or {}),
            "plan_digest": str((control or {}).get("plan_digest") or ""),
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
            "objective_verified": decided,
            "objective_outcome": str(objective.get("outcome") or "unsupported"),
            "verdict": (
                str(objective.get("outcome"))
                if decided
                else "inconclusive" if objective.get("configured") else "observation_only"
            ),
            "real_gameplay_control_exercised": bool(executed_steps),
            "note": (
                "This report describes one controlled evaluation: an operator-approved "
                "plan executed through the Guardian, and an objective read by a verifier "
                "from the target's own evidence. A run that ended without a decided verdict "
                "is inconclusive, never a success."
                if mode == "control"
                else (
                    "This report describes one bounded observation evaluation. No gameplay "
                    "input was sent and no objective was configured, so it makes no claim "
                    "about gameplay capability, wins or completion."
                )
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
            "objective_verified": bool((record.get("objective") or {}).get("decided")),
            "objective_outcome": str((record.get("objective") or {}).get("outcome") or "unsupported"),
            "objective_achieved": (record.get("objective") or {}).get("achieved"),
            "control_state": str((record.get("control") or {}).get("state") or ""),
            "plan_digest": str((record.get("control") or {}).get("plan_digest") or ""),
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
