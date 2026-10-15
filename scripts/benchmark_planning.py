"""Phase 24 benchmark — thirteen deterministic planning scenarios, self-checked.

Usage:
    PYTHONPATH=src python scripts/benchmark_planning.py [--json]

Each scenario builds a REAL :class:`~novacontrol.planning.goal_manager.GoalManager`
over a real :class:`~novacontrol.planning.executor.WorkflowExecutor` and runs the
whole pipeline — ground, decompose, plan with both planners, rank, authorise,
execute under the fresh-state gate, verify, reassess — and then CHECKS ITS OWN
EXPECTATION. Nothing returns a canned answer: a scenario that stops behaving
fails the run and the process exits non-zero.

Honest scope of the substitutions, all of them deliberate:

* The manager, both planners, the ranker, the estimator, the monitor, the action
  policy, the executor, the verification engine and the JSON goal repository are
  the shipping implementations.
* The world model is a plain mapping handed to ``StateGroundingService`` — the
  shape that module's own docstring documents for exactly this purpose, so the
  grounding, staleness and merging logic under test is the real one.
* The STEP HANDLER is a deterministic in-process function that records what it
  was asked and makes its declared effect true in the mapping. This is the only
  substitution that replaces real work, and it is unavoidable: a benchmark must
  not open a real browser, phone or file.

Metrics: per scenario — whether a plan was produced, which planner won, how many
candidates and expansions the comparison looked at, the plan's step count, the
run status, whether it executed and verified, the revalidation count, and the
wall-clock cost of planning and of running. A figure that was not measured is
reported as ``null``, never as zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.planning.candidates import PlanningBudget
from novacontrol.planning.estimation import OutcomeEstimator
from novacontrol.planning.executor import StepContext, WorkflowExecutor
from novacontrol.planning.goal_manager import GoalManager, JsonGoalRepository
from novacontrol.planning.goals import SuccessCondition, Task
from novacontrol.planning.grounding import StateGroundingService
from novacontrol.planning.models import (
    PlanStep,
    StepEffect,
    VerificationMethod,
    VerificationSpec,
)
from novacontrol.planning.policy import ActionPolicy
from novacontrol.planning.ranking import PlanRanker
from novacontrol.reliability import VerificationEngine

#: The one named check every benchmark step attaches, so a step that ran is
#: VERIFIED rather than merely executed — a run cannot report ``completed``
#: while a step went unchecked (``workflow.unverified_steps`` is not empty).
BENCH_CHECK = "bench_step_reported"


def _bench_check(spec: VerificationSpec, output: Mapping[str, Any]) -> tuple[bool, str]:
    """The check the benchmark's steps ask for: did the step report at all?"""
    if not isinstance(output, Mapping) or "ok" not in output:
        return False, "the step reported no result at all"
    if not output["ok"]:
        return False, str(output.get("detail") or "the step reported a failure")
    return True, f"step {output.get('step', '?')} reported a result"


def _verification() -> VerificationSpec:
    return VerificationSpec(
        method=VerificationMethod.CALLABLE,
        target=BENCH_CHECK,
        description="the benchmark's own check on the step's result",
    )


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _task(task_id: str, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "id": task_id,
        "title": task_id.replace("_", " ").title(),
        "action": f"bench_{task_id}",
        "verification": _verification(),
        "estimated_seconds": 0.01,
        "estimated_cost": 1.0,
    }
    base.update(overrides)
    return Task(**base)


class _MemoryStore:
    """The two methods the goal repository uses from the JSON state store."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def read(self, name: str) -> dict[str, Any]:
        payload = self.values.get(name)
        return dict(payload) if isinstance(payload, dict) else {}

    def write(self, name: str, payload: dict[str, Any]) -> None:
        self.values[name] = dict(payload)


@dataclass
class Result:
    """One scenario's metrics and verdict."""

    key: str
    name: str
    failures: list[str] = field(default_factory=list)
    planned: bool | None = None
    planner: str = ""
    candidates: int | None = None
    expansions: int | None = None
    steps: int | None = None
    run_status: str = ""
    executed: bool | None = None
    verified: bool | None = None
    revalidations: int | None = None
    planning_ms: float | None = None
    run_ms: float | None = None
    bases: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures

    def fail(self, *reasons: str) -> Result:
        self.failures.extend(reason for reason in reasons if reason)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "ok": self.ok,
            "failures": list(self.failures),
            "planned": self.planned,
            "planner": self.planner,
            "candidates": self.candidates,
            "expansions": self.expansions,
            "steps": self.steps,
            "run_status": self.run_status,
            "executed": self.executed,
            "verified": self.verified,
            "revalidations": self.revalidations,
            "planning_ms": None if self.planning_ms is None else round(self.planning_ms, 3),
            "run_ms": None if self.run_ms is None else round(self.run_ms, 3),
            "estimate_bases": dict(self.bases),
        }


class Bench:
    """A real GoalManager over a real executor, plus the scenario's bookkeeping.

    ``run_step`` is the ONLY substitute for real work: it records the call, makes
    the step's declared effects true in the world mapping, and reports success.
    Everything else — ordering, gating, revalidation, verification, completion —
    is the shipping code path.
    """

    def __init__(
        self,
        key: str,
        name: str,
        *,
        facts: Mapping[str, Any] | None = None,
        budget: PlanningBudget | None = None,
        goal_budget: dict[str, Any] | None = None,
    ) -> None:
        self.result = Result(key=key, name=name)
        self.world: dict[str, Any] = dict(facts or {})
        self.calls: list[str] = []
        self.approved: set[str] = set()
        #: Seconds each step spends, for the one scenario that must outlast a limit.
        self.step_sleep: float = 0.0
        self._goal_budget = dict(goal_budget or {})
        #: The most recent plan report per goal, so a check can re-read it.
        self._reports: dict[str, dict[str, Any]] = {}
        self.executor = WorkflowExecutor(
            step_handler=self.run_step,
            verifier=VerificationEngine(callables={BENCH_CHECK: _bench_check}),
            confirmation=self.confirm,
        )
        self.manager = GoalManager(
            executor=self.executor,
            grounding=StateGroundingService(world=self.world),
            estimator=OutcomeEstimator(),
            ranker=PlanRanker(),
            policy=ActionPolicy(),
            repository=JsonGoalRepository(_MemoryStore()),
            budget=budget or PlanningBudget(),
        )

    # -- the one substitution -------------------------------------------------

    def run_step(self, step: PlanStep, context: StepContext) -> dict[str, Any]:
        """Do the step's declared work, deterministically, in the world mapping."""
        self.calls.append(step.id)
        if self.step_sleep:
            # Only the budget scenario sets this, and only so that a real clock can
            # pass a real limit: every other leg runs instantly.
            time.sleep(self.step_sleep)
        for effect in (step.parameters or {}).get("effects") or ():
            key, _, value = str(effect).partition("=")
            if key.strip():
                self.world[key.strip()] = _coerce(value)
        return {"ok": True, "step": step.id}

    def confirm(self, step: PlanStep) -> bool:
        """Approval is by step id, exactly as the application's own channel is."""
        return step.id in self.approved

    # -- driving --------------------------------------------------------------

    def goal(self, request: str, condition: str, **overrides: Any) -> str:
        """Create a goal whose one required success condition is ``condition``."""
        budget = dict(self._goal_budget)
        budget.update(overrides.pop("budget", {}) or {})
        goal = self.manager.create(
            request,
            success_conditions=(
                SuccessCondition(key=condition, expected=True, description=condition),
            ),
            **budget,
            **overrides,
        )
        return goal.goal_id

    def plan(self, goal_id: str, tasks: Sequence[Task] | None = None) -> dict[str, Any]:
        """PLAN, timing it and recording what the comparison looked at."""
        started = time.perf_counter()
        report = self.manager.plan(goal_id, tasks=tasks)
        self.result.planning_ms = (time.perf_counter() - started) * 1000.0
        payload = report.to_dict()
        self.result.planned = report.planned
        self.result.planner = (payload.get("selected") or {}).get("planner", "") or ""
        self.result.candidates = len(payload.get("candidates") or ())
        self.result.expansions = sum(
            int(row.get("expansions", 0)) for row in payload.get("planners") or ()
        )
        plan = (payload.get("selected") or {}).get("plan") or {}
        self.result.steps = len(plan.get("steps") or ())
        for row in (payload.get("estimates") or {}).get("steps") or ():
            basis = str(row.get("basis", ""))
            self.result.bases[basis] = self.result.bases.get(basis, 0) + 1
        self._reports[goal_id] = payload
        return payload

    def report(self, goal_id: str) -> dict[str, Any]:
        return self._reports.get(goal_id, {})

    async def execute(self, goal_id: str, approved: Sequence[str] = ()) -> dict[str, Any]:
        """EXECUTE the selected plan, timing it and recording the run's verdict."""
        self.approved = {str(item) for item in approved}
        started = time.perf_counter()
        run = await self.manager.execute(goal_id, approved=tuple(approved))
        self.result.run_ms = (time.perf_counter() - started) * 1000.0
        self.result.run_status = run.status
        self.result.executed = run.executed
        self.result.verified = run.verified
        self.result.revalidations = len(run.revalidations)
        return run.to_dict()


def _plan_steps(report: Mapping[str, Any]) -> list[str]:
    selected = report.get("selected") or {}
    plan = selected.get("plan") or {}
    return [str(step.get("id", "")) for step in plan.get("steps") or ()]


def _step_flags(report: Mapping[str, Any]) -> dict[str, bool]:
    selected = report.get("selected") or {}
    plan = selected.get("plan") or {}
    return {
        str(step.get("id", "")): bool(step.get("parallel_safe")) for step in plan.get("steps") or ()
    }


def _issue_kinds(report: Mapping[str, Any]) -> set[str]:
    decomposition = report.get("decomposition") or {}
    return {str(issue.get("kind", "")) for issue in decomposition.get("issues") or ()}


# --------------------------------------------------------------------------- #
# A — the happy path: one step runs, is verified, and the goal completes.
# --------------------------------------------------------------------------- #


async def scenario_a() -> Result:
    bench = Bench("A", "a single-step goal completes and is verified")
    goal_id = bench.goal("build the artifact", "build.done")
    report = bench.plan(goal_id, [_task("build", effects=("build.done=True",))])
    run = await bench.execute(goal_id)

    bench.result.fail(
        "" if report["planned"] else f"expected a plan, got refusal: {report['refusal']}",
        ""
        if run["status"] == "completed"
        else f"expected completed, got {run['status']}: {run['reason']}",
        "" if run["verified"] else "a completed run must be verified, not merely executed",
        "" if bench.calls == ["build"] else f"expected one step to run, saw {bench.calls}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# B — dependency order is respected at plan time AND at run time.
# --------------------------------------------------------------------------- #


async def scenario_b() -> Result:
    bench = Bench("B", "dependency order is respected at plan and run time")
    goal_id = bench.goal("ship the release", "release.shipped")
    report = bench.plan(
        goal_id,
        [
            _task(
                "publish",
                depends_on=("prepare",),
                preconditions=("prepared=True",),
                effects=("release.shipped=True",),
                estimated_cost=5.0,
            ),
            _task("prepare", effects=("prepared=True",), estimated_cost=1.0),
        ],
    )
    run = await bench.execute(goal_id)

    bench.result.fail(
        ""
        if _plan_steps(report) == ["prepare", "publish"]
        else f"expected dependency order ['prepare', 'publish'], got {_plan_steps(report)}",
        ""
        if bench.calls == ["prepare", "publish"]
        else f"expected that order to RUN, got {bench.calls}",
        ""
        if run["status"] == "completed"
        else f"expected completed, got {run['status']}: {run['reason']}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# C — parallel-safety is DECLARED from the effect, never assumed.
# --------------------------------------------------------------------------- #


async def scenario_c() -> Result:
    bench = Bench("C", "only read-only steps may share a parallel wave")
    goal_id = bench.goal("inspect and update", "update.done")
    report = bench.plan(
        goal_id,
        [
            _task("look_a", effects=("saw_a=True",)),
            _task("look_b", effects=("saw_b=True",)),
            _task(
                "update",
                effect=StepEffect.LOCAL_WRITE,
                effects=("update.done=True",),
            ),
        ],
    )
    flags = _step_flags(report)

    bench.result.fail(
        ""
        if not flags.get("update")
        else "a local write was marked parallel-safe; the model should refuse that",
        ""
        if flags.get("look_a") and flags.get("look_b")
        else f"read-only steps should be the ones allowed to share a wave, got {flags}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# D — a prerequisite nothing produces is refused, and the refusal names it.
# --------------------------------------------------------------------------- #


async def scenario_d() -> Result:
    bench = Bench("D", "a missing prerequisite is refused, with its name")
    goal_id = bench.goal("finish the job", "job.done")
    report = bench.plan(
        goal_id,
        [_task("finish", preconditions=("gate.open=True",), effects=("job.done=True",))],
    )

    bench.result.fail(
        ""
        if not report["planned"]
        else "a plan whose prerequisite nothing satisfies must be refused",
        ""
        if "gate.open" in str(report["refusal"])
        else f"the refusal must name the missing fact, got: {report['refusal']}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# E — a dependency cycle is reported before anything is planned.
# --------------------------------------------------------------------------- #


async def scenario_e() -> Result:
    bench = Bench("E", "a dependency cycle is refused and named")
    goal_id = bench.goal("do the impossible", "x.done")
    report = bench.plan(
        goal_id,
        [_task("a", depends_on=("b",)), _task("b", depends_on=("a",))],
    )
    kinds = _issue_kinds(report)

    bench.result.fail(
        "" if "cycle" in kinds else f"expected a cycle issue, got {sorted(kinds)}",
        "" if not report["planned"] else "a cyclic decomposition must not produce a plan",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# F — work whose result is already known is skipped, not repeated.
# --------------------------------------------------------------------------- #


async def scenario_f() -> Result:
    bench = Bench(
        "F",
        "work whose result is already known is skipped",
        facts={"build.done": True, "mirror.done": True},
    )
    goal_id = bench.goal("build and mirror", "mirror.done")
    report = bench.plan(
        goal_id,
        [
            _task("build", effects=("build.done=True",)),
            _task("mirror", effects=("mirror.done=True",)),
        ],
    )
    run = await bench.execute(goal_id)
    steps = _plan_steps(report)

    bench.result.fail(
        "" if not steps else f"work already known must be skipped, not re-planned: {steps}",
        "" if not bench.calls else f"nothing needed to run, but the runner saw {bench.calls}",
        ""
        if run["status"] == "completed"
        else f"an already-satisfied goal is complete, got {run['status']}: {run['reason']}",
        "" if not run["executed"] else "nothing ran, so the run must not claim it did",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# G — state contradicted between planning and running blocks the step.
# --------------------------------------------------------------------------- #


async def scenario_g() -> Result:
    bench = Bench("G", "a contradicted precondition blocks the run", facts={"gate.open": True})
    goal_id = bench.goal("pass the gate", "goal.done")
    report = bench.plan(
        goal_id,
        [_task("pass", preconditions=("gate.open=True",), effects=("goal.done=True",))],
    )
    if not report["planned"]:
        return bench.result.fail(f"the plan must be producible first: {report['refusal']}")
    # The world moves UNDER the plan: the fact the step needs is now false.
    bench.world["gate.open"] = False
    run = await bench.execute(goal_id)

    bench.result.fail(
        ""
        if run["status"] == "blocked"
        else f"a contradicted precondition must block the run, got {run['status']}",
        "" if not bench.calls else f"no step may run on contradicted state, saw {bench.calls}",
        "" if run["status"] != "completed" else "a refused step must never read as completed",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# H — a consequential step waits for a person; one approval is enough.
# --------------------------------------------------------------------------- #


async def _scenario_h_leg(approved: Sequence[str]) -> Result:
    label = "with the caller's approval" if approved else "without an approval"
    bench = Bench("H", f"a destructive step needs approval ({label})")
    goal_id = bench.goal("clear the scratch space", "scratch.cleared")
    report = bench.plan(
        goal_id,
        [_task("clear", effect=StepEffect.DESTRUCTIVE, effects=("scratch.cleared=True",))],
    )
    needs = list((report.get("policy") or {}).get("needs_approval") or ())
    run = await bench.execute(goal_id, approved=approved)

    bench.result.fail(
        ""
        if needs == ["clear"]
        else f"a destructive step must be reported as needing approval, got {needs}",
        ""
        if approved
        else (
            ""
            if run["status"] == "denied"
            else f"an unapproved destructive step must be denied, got {run['status']}"
        ),
        ""
        if approved
        else ("" if not bench.calls else f"nothing may run without approval, saw {bench.calls}"),
        ""
        if not approved
        else (
            ""
            if run["status"] == "completed" and run["executed"]
            else f"an approved step must run, got {run['status']}: {run['reason']}"
        ),
    )
    return bench.result


# --------------------------------------------------------------------------- #
# I — a goal nobody can measure is refused rather than guessed at.
# --------------------------------------------------------------------------- #


async def scenario_i() -> Result:
    bench = Bench("I", "an unmeasurable goal is refused")
    goal = bench.manager.create("make it better")
    report = bench.plan(goal.goal_id, [_task("tweak", effects=("better=True",))])
    kinds = _issue_kinds(report)

    bench.result.fail(
        ""
        if not report["planned"]
        else "a goal with no measurable completion condition must not be planned",
        "" if "unmeasurable" in kinds else f"expected an unmeasurable issue, got {sorted(kinds)}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# J — a narrow request is one short plan, and its estimate says who said so.
# --------------------------------------------------------------------------- #


async def scenario_j() -> Result:
    bench = Bench("J", "a narrow goal is one short, declared plan")
    goal_id = bench.goal("read the temperature", "temp.read")
    report = bench.plan(goal_id, [_task("read", effects=("temp.read=True",))])
    steps = _plan_steps(report)

    bench.result.fail(
        "" if len(steps) == 1 else f"a simple request deserves a one-step plan, got {steps}",
        ""
        if set(bench.result.bases) == {"rule_based"}
        else f"with no predictor wired every estimate is a declaration, got {bench.result.bases}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# K — the candidate search respects its ceilings and says it was truncated.
# --------------------------------------------------------------------------- #


async def scenario_k() -> Result:
    bench = Bench(
        "K",
        "the candidate search respects its ceilings",
        budget=PlanningBudget(max_expansions=2, max_candidates=2),
    )
    goal_id = bench.goal("do many independent things", "all.done")
    report = bench.plan(
        goal_id,
        [
            _task(f"t{index}", effects=(f"t{index}.done=True",), estimated_cost=float(index + 1))
            for index in range(8)
        ],
    )

    for planner in report.get("planners") or ():
        if planner.get("planner") != "cost_aware_beam":
            continue
        if int(planner.get("expansions", 0)) > 2:
            bench.result.fail(f"the beam exceeded its expansion ceiling: {planner['expansions']}")
        if not planner.get("truncated"):
            bench.result.fail("a search that hit a ceiling must say it was truncated")
    if not (report.get("planners") or ()):
        bench.result.fail("no planner reported anything at all")
    return bench.result


# --------------------------------------------------------------------------- #
# L — replanning is counted and stops at the goal's own ceiling.
# --------------------------------------------------------------------------- #


async def scenario_l() -> Result:
    bench = Bench("L", "replanning stops at the goal's own ceiling", goal_budget={"max_replans": 1})
    goal_id = bench.goal("retry until it works", "retry.done")
    first = bench.plan(goal_id, [_task("attempt", effects=("retry.done=True",))])
    if not first["planned"]:
        return bench.result.fail(f"the first plan must succeed: {first['refusal']}")
    await bench.execute(goal_id)

    second = bench.manager.plan(goal_id)
    third = bench.manager.plan(goal_id)

    bench.result.fail(
        "" if second.planned else f"the first replan is allowed: {second.refusal}",
        "" if not third.planned else "a third attempt must be refused by the ceiling",
        ""
        if "ceiling is reached" in third.refusal
        else f"the refusal must name the ceiling, got: {third.refusal}",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# M — a run that finishes past its stated seconds budget says so.
# --------------------------------------------------------------------------- #


async def scenario_m() -> Result:
    """A limit that is broken must be named, even when the work succeeded.

    The budget is checked before each step AND once after the last one, and this is
    the leg that proves the second reading exists: the goal's step really runs (the
    budget is not exhausted when its gate asks), it outlasts the budget, and the run
    must therefore report the BREACH rather than a bare ``completed`` — while still
    carrying the completion verdict it earned, so neither half is lost.
    """
    bench = Bench("M", "a run that overruns its stated budget reports the breach")
    goal_id = bench.goal("spend the budget", "spend.done", budget={"max_seconds": 1.0})
    # Declared ABOVE the budget on purpose: the estimate/budget mismatch is visible at
    # plan time and must be reported there without refusing the plan, because a
    # declared effect is a declaration rather than a prediction.
    plan_report = bench.plan(
        goal_id,
        [_task("spend", effects=("spend.done=True",), estimated_seconds=600.0)],
    )
    # The limit is a wall clock, so the leg has to take real time: a generous budget
    # with a step that certainly outlasts it.
    bench.step_sleep = 1.5
    run = await bench.execute(goal_id)
    notes = " ".join(str(item) for item in plan_report.get("limitations") or ())

    bench.result.fail(
        ""
        if run["status"] == "budget_exhausted"
        else f"a run past its stated budget must say so, got {run['status']}",
        ""
        if bench.calls == ["spend"]
        else f"the step itself must still have run, saw {bench.calls}",
        "" if run["verified"] else "the completion verdict must survive the breach",
        ""
        if "budget" in str(run["reason"])
        else f"the reason must name the limit: {run['reason']!r}",
        # Plan time reports the mismatch between the estimate and the budget; it does
        # not refuse on it, because a declared effect is a declaration, not a clock.
        ""
        if "estimate" in notes
        else "the plan report must name the estimate/budget mismatch it can see",
    )
    return bench.result


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

#: The scenario table, in reporting order. Each entry is (key, name, coroutine
#: factory) — the factory takes no arguments, so the table stays data.
SCENARIOS: tuple[tuple[str, str, Any], ...] = (
    ("A", "a single-step goal completes and is verified", scenario_a),
    ("B", "dependency order is respected at plan and run time", scenario_b),
    ("C", "only read-only steps may share a parallel wave", scenario_c),
    ("D", "a missing prerequisite is refused, with its name", scenario_d),
    ("E", "a dependency cycle is refused and named", scenario_e),
    ("F", "work whose result is already known is skipped", scenario_f),
    ("G", "a contradicted precondition blocks the run", scenario_g),
    ("H", "a destructive step needs approval", _scenario_h_leg),
    ("I", "an unmeasurable goal is refused", scenario_i),
    ("J", "a narrow goal is one short, declared plan", scenario_j),
    ("K", "the candidate search respects its ceilings", scenario_k),
    ("L", "replanning stops at the goal's own ceiling", scenario_l),
    ("M", "a run that overruns its stated budget reports the breach", scenario_m),
)


async def run_all() -> list[Result]:
    results: list[Result] = []
    for key, name, factory in SCENARIOS:
        if key == "H":
            # Two legs: the refusal without approval, and the completion with it.
            results.append(await _scenario_h_leg(()))
            results.append(await _scenario_h_leg(("clear",)))
            continue
        try:
            results.append(await factory())
        except Exception as exc:  # noqa: BLE001 - a scenario that throws is a failure
            results.append(Result(key=key, name=name, failures=[f"raised {exc!r}"]))
    return results


def _summarise(results: list[Result]) -> dict[str, Any]:
    times = [item.planning_ms for item in results if item.planning_ms is not None]
    runs = [item.run_ms for item in results if item.run_ms is not None]
    bases: dict[str, int] = {}
    for item in results:
        for basis, count in item.bases.items():
            bases[basis] = bases.get(basis, 0) + count
    return {
        "scenarios": [item.to_dict() for item in results],
        "totals": {
            "scenarios": len(results),
            "passed": sum(1 for item in results if item.ok),
            "failed": sum(1 for item in results if not item.ok),
            "candidates_compared": sum(item.candidates or 0 for item in results),
            "planner_expansions": sum(item.expansions or 0 for item in results),
            "revalidations": sum(item.revalidations or 0 for item in results),
            "planning_ms_mean": round(statistics.fmean(times), 3) if times else None,
            "planning_ms_max": round(max(times), 3) if times else None,
            "run_ms_mean": round(statistics.fmean(runs), 3) if runs else None,
            "estimate_bases": bases,
        },
    }


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 24 planning benchmark")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    results = await run_all()
    summary = _summarise(results)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print("Phase 24 planning benchmark — fourteen legs across thirteen scenarios")
        print(
            f"{'':2}  {'pln':4}  {'planner':17}  {'cand':4}  {'stp':3}  "
            f"{'run':17}  {'plan ms':>8}  result"
        )
        for item in results:
            status = "PASS" if item.ok else "FAIL: " + "; ".join(item.failures)
            print(
                f"{item.key:2}  {str(item.planned):4}  {item.planner[:17]:17}  "
                f"{str(item.candidates):4}  {str(item.steps):3}  "
                f"{item.run_status[:17]:17}  "
                f"{(item.planning_ms if item.planning_ms is not None else 0.0):8.2f}  {status}"
            )
        totals = summary["totals"]
        print(
            f"\n{totals['passed']}/{totals['scenarios']} scenarios passed; "
            f"{totals['candidates_compared']} candidates compared, "
            f"{totals['planner_expansions']} search expansions, "
            f"{totals['revalidations']} step revalidations; "
            f"planning mean {totals['planning_ms_mean']} ms / max {totals['planning_ms_max']} ms; "
            f"estimate bases {totals['estimate_bases']}"
        )
    return 0 if summary["totals"]["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(asyncio.run(main()))
