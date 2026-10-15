"""Phase 25 benchmark — real embodied sessions, measured and self-checked.

Usage:
    PYTHONPATH=src python scripts/benchmark_embodied.py [--json] [--repeat N]

Each scenario builds a REAL :class:`~novacontrol.application.NovaControlApplication`
over a temporary data directory, creates a session in the owned grid-world
simulation and drives the whole loop: observe → decide through the Phase 24 goal
manager → send ONE authorized input through the input door → verify from a fresh
reading → recover or replan → report the ending. Nothing returns a canned answer,
and every scenario CHECKS ITS OWN EXPECTATION: a scenario that stops behaving
fails the run and the process exits non-zero.

Honest scope of what is real here:

* The application, the goal manager, the planner and ranker, the world model, the
  permission layer, the input door, the audit trail, the JSON state store and the
  grid-world simulation are the SHIPPING implementations, wired exactly as
  ``NovaControlApplication._build_embodied`` wires them.
* The only substitution is the provider probe: Ollama autodetection is disabled
  for the run so the measurement is of the embodied loop and not of a network
  timeout (a local chat provider plays no part in an embodied session).

Metrics: per scenario — the ending status, whether the objective was observed
met, how many inputs were sent, how many attempts were made, observations taken,
recoveries, refused inputs, failed attempts, stale readings, the trajectory
length, the audit rows, the wall clock of composing and of running, seconds per
sent input, and the size and cost of persisting the session record. A figure
that was not measured is reported as ``null``, never as zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# A benchmark of the embodied loop must not spend its first seconds on a
# local-model probe that no embodied session consults.
os.environ.setdefault("NOVACONTROL_DISABLE_OLLAMA", "1")
os.environ.setdefault("NOVACONTROL_DISABLE_CLOUD", "1")

from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.embodied.simulation import adapter_for  # noqa: E402

#: The corridor used to measure throughput: one long straight line of inputs.
#: Twenty cells plus the collect is 21 inputs, which fits inside the configured
#: 40-input ceiling — a corridor longer than the ceiling would measure the budget
#: rather than the loop.
CORRIDOR = {
    "width": 21,
    "height": 3,
    "start": [0, 1],
    "target": [20, 1],
    "walls": [],
}
CORRIDOR_ID = "gridworld:corridor"


@dataclass(frozen=True, slots=True)
class Scenario:
    """One measured workflow, with the outcome it must produce."""

    name: str
    scenario: str = ""
    environment_id: str = "gridworld"
    max_actions: int = 40
    #: The ending the session must reach, and the objective's verdict.
    expect_status: str = "completed"
    expect_met: bool = True
    #: Bounds on inputs actually sent, when the count is the point.
    min_actions: int | None = None
    max_actions_seen: int | None = None
    min_recoveries: int = 0
    min_rejected: int = 0
    min_failed: int = 0
    min_stale: int = 0
    max_failed: int | None = None
    note: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="open-east",
        scenario="open-east",
        min_actions=4,
        max_actions_seen=4,
        note="the baseline: a clear straight line, four inputs",
    ),
    Scenario(
        name="detour",
        scenario="detour",
        min_actions=4,
        max_actions_seen=14,
        min_recoveries=1,
        note="a wall east: the agent must remember it and route around",
    ),
    Scenario(
        name="drifting",
        scenario="drifting",
        min_recoveries=1,
        note="the environment disagrees with the first move (unexpected outcome)",
    ),
    Scenario(
        name="refusing",
        scenario="refusing",
        min_rejected=2,
        max_failed=0,
        min_recoveries=1,
        note="two refused inputs: the retry path, counted as refusals",
    ),
    Scenario(
        name="timing-out",
        scenario="timing-out",
        min_failed=1,
        min_recoveries=1,
        note="an input that never reports back; the state is re-read, never re-sent",
    ),
    Scenario(
        name="frozen",
        scenario="frozen",
        expect_status="budget_exhausted",
        expect_met=False,
        max_actions_seen=0,
        min_stale=1,
        min_recoveries=1,
        note="the environment stops answering: no input is sent on a stale reading",
    ),
    Scenario(
        name="fogged",
        scenario="fogged",
        expect_status="stuck",
        expect_met=False,
        max_actions_seen=0,
        note="regions only: the agent refuses to navigate or name what it sees",
    ),
    Scenario(
        name="tiny-budget",
        max_actions=2,
        expect_status="budget_exhausted",
        expect_met=False,
        min_actions=2,
        max_actions_seen=2,
        note="a budget that runs out is reported as exhausted, never as complete",
    ),
    Scenario(
        name="corridor-20",
        environment_id=CORRIDOR_ID,
        min_actions=21,
        max_actions_seen=21,
        note="a 20-cell corridor: throughput of the full loop, per sent input",
    ),
)


def _objective(scenario: Scenario) -> dict[str, Any]:
    return {
        "request": "collect the target",
        "success_conditions": ["env.target.collected=true"],
        "max_actions": scenario.max_actions,
    }


@dataclass
class Row:
    """One scenario's measurement, plus whether it matched its expectation."""

    name: str
    status: str
    objective_met: bool
    actions_sent: int
    attempts: int
    observations: int
    recoveries: int
    rejections: int
    failures: int
    stale: int
    verification_failures: int
    trajectory: int
    audit_rows: int
    compose_ms: float
    run_ms: float
    ms_per_input: float | None
    record_bytes: int
    save_ms: float | None
    expected: str
    failures_of_expectation: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures_of_expectation

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "status": self.status,
            "objective_met": self.objective_met,
            "actions_sent": self.actions_sent,
            "attempts": self.attempts,
            "observations": self.observations,
            "recoveries": self.recoveries,
            "rejections": self.rejections,
            "failures": self.failures,
            "stale_observations": self.stale,
            "verification_failures": self.verification_failures,
            "trajectory": self.trajectory,
            "audit_rows": self.audit_rows,
            "compose_ms": round(self.compose_ms, 3),
            "run_ms": round(self.run_ms, 3),
            "ms_per_input": (
                round(self.ms_per_input, 3) if self.ms_per_input is not None else None
            ),
            "record_bytes": self.record_bytes,
            "save_ms": round(self.save_ms, 3) if self.save_ms is not None else None,
            "expected": self.expected,
            "matches_expectation": self.ok,
        }
        if self.failures_of_expectation:
            payload["expectation_failures"] = list(self.failures_of_expectation)
        return payload


def _check(scenario: Scenario, row: Row) -> list[str]:
    """Every way this run failed to match what the scenario declares."""
    problems: list[str] = []
    if row.status != scenario.expect_status:
        problems.append(f"status {row.status!r}, expected {scenario.expect_status!r}")
    if row.objective_met is not scenario.expect_met:
        problems.append(f"objective_met {row.objective_met}, expected {scenario.expect_met}")
    if scenario.min_actions is not None and row.actions_sent < scenario.min_actions:
        problems.append(f"{row.actions_sent} inputs sent, expected at least {scenario.min_actions}")
    if scenario.max_actions_seen is not None and row.actions_sent > scenario.max_actions_seen:
        problems.append(
            f"{row.actions_sent} inputs sent, expected at most {scenario.max_actions_seen}"
        )
    if row.recoveries < scenario.min_recoveries:
        problems.append(f"{row.recoveries} recoveries, expected at least {scenario.min_recoveries}")
    if row.rejections < scenario.min_rejected:
        problems.append(f"{row.rejections} refusals, expected at least {scenario.min_rejected}")
    if row.failures < scenario.min_failed:
        problems.append(f"{row.failures} failures, expected at least {scenario.min_failed}")
    if scenario.max_failed is not None and row.failures > scenario.max_failed:
        problems.append(f"{row.failures} failures, expected at most {scenario.max_failed}")
    if row.stale < scenario.min_stale:
        problems.append(f"{row.stale} stale readings, expected at least {scenario.min_stale}")
    if row.actions_sent != row.audit_rows:
        problems.append(
            f"{row.actions_sent} inputs sent but {row.audit_rows} audit rows: the trail must "
            "hold every input exactly once"
        )
    return problems


def _register_corridor(nova: NovaControlApplication) -> None:
    nova.embodied.register_adapter(adapter_for(CORRIDOR, environment_id=CORRIDOR_ID))


async def run_scenario(nova: NovaControlApplication, scenario: Scenario) -> Row:
    """Drive one scenario through the real manager and measure it."""
    # The audit trail is shared and cumulative, so this scenario is measured by
    # the rows that appeared while it ran, not by the total.
    audit_before = await nova.embodied.audit_log.read()
    started = time.perf_counter()
    state = nova.embodied.create_session(
        environment_id=scenario.environment_id,
        objective=_objective(scenario),
        scenario=scenario.scenario or None,
    )
    compose_ms = (time.perf_counter() - started) * 1000.0
    run_started = time.perf_counter()
    result = await nova.embodied.run_session(state.session_id, max_actions=scenario.max_actions)
    run_ms = (time.perf_counter() - run_started) * 1000.0
    view = nova.embodied.view(state.session_id)
    evaluation = view["evaluation"]
    record = json.dumps(view)
    save_ms: float | None = None
    if nova.embodied.status()["wired"]["persistence"]:
        save_started = time.perf_counter()
        nova.embodied.save(state.session_id)
        save_ms = (time.perf_counter() - save_started) * 1000.0
    audit_after = await nova.embodied.audit_log.read()
    audit_rows = len(audit_after) - len(audit_before)
    audit_here = {
        str(entry.output.get("session_id", "")) for entry in audit_after[len(audit_before) :]
    }
    sent = int(result.actions_taken)
    row = Row(
        name=scenario.name,
        status=result.status.value,
        objective_met=bool(result.verdict.met),
        actions_sent=sent,
        attempts=int(evaluation["actions_attempted"]),
        observations=int(evaluation["observations"]),
        recoveries=int(result.recoveries),
        rejections=int(evaluation["actions_rejected"]),
        failures=int(evaluation["actions_failed"]),
        stale=int(evaluation["stale_observations"]),
        verification_failures=int(evaluation["verification_failures"]),
        trajectory=len(result.trajectory),
        audit_rows=audit_rows,
        compose_ms=compose_ms,
        run_ms=run_ms,
        ms_per_input=(run_ms / sent) if sent else None,
        record_bytes=len(record.encode("utf-8")),
        save_ms=save_ms,
        expected=(
            f"status={scenario.expect_status} met={scenario.expect_met} "
            f"actions>={scenario.min_actions} <={scenario.max_actions_seen}"
        ),
    )
    row.failures_of_expectation = _check(scenario, row)
    if audit_here and audit_here != {state.session_id}:
        row.failures_of_expectation.append(
            f"audit rows name {sorted(audit_here)}, expected only {state.session_id}"
        )
    # Free the live-session slot for the next scenario, and prove the cancel
    # really landed: a cosmetic cancel would fail the next create_session.
    nova.embodied.cancel(state.session_id, "benchmark cleanup")
    return row


async def measure(repeat: int, only: list[str]) -> tuple[list[Row], dict[str, Any]]:
    """Run every scenario (and the repeat loop) over ONE real application."""
    with TemporaryDirectory() as directory:
        boot_started = time.perf_counter()
        nova = NovaControlApplication(data_dir=Path(directory))
        boot_ms = (time.perf_counter() - boot_started) * 1000.0
        try:
            _register_corridor(nova)
            rows: list[Row] = []
            for scenario in SCENARIOS:
                if only and scenario.name not in only:
                    continue
                rows.append(await run_scenario(nova, scenario))

            timings: list[float] = []
            sent_per_run: list[int] = []
            for _ in range(max(0, repeat)):
                state = nova.embodied.create_session(
                    objective=_objective(SCENARIOS[0]), scenario="open-east"
                )
                started = time.perf_counter()
                result = await nova.embodied.run_session(state.session_id)
                timings.append((time.perf_counter() - started) * 1000.0)
                sent_per_run.append(int(result.actions_taken))
                nova.embodied.cancel(state.session_id, "benchmark cleanup")

            meta: dict[str, Any] = {
                "boot_ms": round(boot_ms, 3),
                "repeat": repeat,
                "repeat_run_ms": {
                    "min": round(min(timings), 3) if timings else None,
                    "median": round(statistics.median(timings), 3) if timings else None,
                    "max": round(max(timings), 3) if timings else None,
                    "stdev": round(statistics.stdev(timings), 3) if len(timings) > 1 else None,
                    "inputs_per_run": sent_per_run,
                },
                "action_interval_ms": nova.embodied.settings.action_interval_ms,
                "max_actions": nova.embodied.settings.max_actions,
                "persist": nova.embodied.status()["wired"]["persistence"],
                "audit_log": nova.embodied.status()["wired"]["audit_log"],
            }
        finally:
            await nova.stop()
    return rows, meta


def _print_rows(rows: list[Row], meta: dict[str, Any]) -> None:
    print("Phase 25 embodied benchmark")
    print("=" * 96)
    print(
        f"boot {meta['boot_ms']:.1f} ms | pace {meta['action_interval_ms']} ms/input | "
        f"persist={meta['persist']} | audit={meta['audit_log']}"
    )
    header = (
        f"{'scenario':<12} {'ending':<17} {'met':<5} {'sent':>4} {'att':>4} {'obs':>4} "
        f"{'rec':>4} {'ref':>4} {'fail':>4} {'stale':>5} {'traj':>5} "
        f"{'run ms':>8} {'ms/in':>7} {'rec B':>7} {'save ms':>8}  ok"
    )
    print(header)
    print("-" * 96)
    for row in rows:
        print(
            f"{row.name:<12} {row.status:<17} {str(row.objective_met):<5} "
            f"{row.actions_sent:>4} {row.attempts:>4} {row.observations:>4} "
            f"{row.recoveries:>4} {row.rejections:>4} {row.failures:>4} {row.stale:>5} "
            f"{row.trajectory:>5} {row.run_ms:>8.1f} "
            f"{(row.ms_per_input if row.ms_per_input is not None else float('nan')):>7.1f} "
            f"{row.record_bytes:>7} "
            f"{(row.save_ms if row.save_ms is not None else float('nan')):>8.2f}  "
            f"{'ok' if row.ok else 'FAILED'}"
        )
        if not row.ok:
            for problem in row.failures_of_expectation:
                print(f"             ! {problem}")
        if SCENARIOS[[item.name for item in SCENARIOS].index(row.name)].note:
            print(
                f"             {SCENARIOS[[item.name for item in SCENARIOS].index(row.name)].note}"
            )
    distribution = meta["repeat_run_ms"]
    if distribution["median"] is not None:
        print("-" * 96)
        print(
            f"baseline repeated {meta['repeat']}x over {len(distribution['inputs_per_run'])} runs: "
            f"min {distribution['min']:.1f} ms, median {distribution['median']:.1f} ms, "
            f"max {distribution['max']:.1f} ms"
            + (f", stdev {distribution['stdev']:.1f} ms" if distribution["stdev"] else "")
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the measurements as JSON")
    parser.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="how many times to repeat the baseline scenario for a timing distribution",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only the named scenario(s); may be given more than once",
    )
    args = parser.parse_args(argv)

    rows, meta = asyncio.run(measure(max(0, args.repeat), list(args.scenario)))
    failed = [row for row in rows if not row.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "meta": meta,
                    "scenarios": [row.to_dict() for row in rows],
                    "matched": len(rows) - len(failed),
                    "failed": [row.name for row in failed],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_rows(rows, meta)
        print(
            f"\n{len(rows) - len(failed)}/{len(rows)} scenarios matched their declared outcome"
            + (f"; FAILED: {', '.join(row.name for row in failed)}" if failed else "")
        )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
    sys.exit(main())
