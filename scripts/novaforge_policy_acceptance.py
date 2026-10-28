"""NF-10 acceptance: a state-dependent policy vs a fixed script, live.

This script runs on Windows and uses the DISPOSABLE harness window
(``scripts/forge_harness_window.ps1 -Task stages``), never a game. The window's
title names its stage, so both the policy and the objective verifier read the
same measured fact:

    STAGE IDLE  --A-->  STAGE ARMED  --B-->  STAGE COMPLETE
    STAGE FAULT --R-->  STAGE IDLE            (recovery)

Every trial starts from a freshly launched window, so initial conditions are
equivalent for the candidate and the baseline:

* candidate: ``HarnessStagesPolicy`` (state-dependent);
* baseline:  ``FixedKeyPolicy`` (one key, every observation).

Each trial goes through the REAL guarded path — session, plan/digest, Guardian,
injected key, re-observation, independent verification — and the trial record is
built from the resulting ``LoopResult``. Nothing here fabricates a metric: a
value that was not measured is ``None``.

Run::

    python scripts/novaforge_policy_acceptance.py --trials 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.desktop.capture import capture_supported_here
from novacontrol.desktop.controller import LocalDesktopRunner
from novacontrol.novaforge import (
    ClosedLoopRunner,
    FixedKeyPolicy,
    HarnessStagesPolicy,
    LoopLimits,
    NovaForgeManager,
    ObservationBuilder,
    PolicyTrial,
    ScreenInput,
    compare_policies,
    summarise_trials,
    trial_rows,
)

TARGET = "NC FORGE HARNESS"
GOAL = "COMPLETE"
HARNESS = Path(__file__).with_name("forge_harness_window.ps1")


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


async def wait_for(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


def launch_harness(seconds: int):
    import subprocess

    return subprocess.Popen(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(HARNESS), "-Seconds", str(seconds),
            "-KeepOpen", "-Task", "stages",
        ],
        cwd=str(HARNESS.parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def stop_harness(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except Exception:  # pragma: no cover - a wedged host
        process.kill()


async def harness_lines(runner: LocalDesktopRunner) -> list[str]:
    return [line for line in await runner.window_lines() or [] if TARGET.lower() in line.lower()]


async def wait_for_window(runner: LocalDesktopRunner, timeout: float = 30.0) -> list[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        lines = await harness_lines(runner)
        if lines:
            return lines
        await asyncio.sleep(0.2)
    return []


def observation_builder(manager: NovaForgeManager) -> ObservationBuilder:
    """The SAME measured seams the application's loop uses, no model call."""
    return ObservationBuilder(
        probe=manager.window_probe,
        capture=manager.capture_frame if manager.capture_supported else None,
        ocr=None,
    )


async def run_trial(
    manager: NovaForgeManager,
    runner: LocalDesktopRunner,
    *,
    policy: Any,
    trial: int,
    pre_fault: bool = False,
) -> tuple[PolicyTrial, dict[str, Any]]:
    process = launch_harness(seconds=120)
    try:
        lines = await wait_for_window(runner)
        if not lines:
            raise RuntimeError("the disposable harness window did not open")
        described = await manager.start(
            {
                "name": f"NF-10 {policy.name} trial {trial}",
                "objective": "Reach the COMPLETE stage of the disposable harness",
                "target_title": TARGET,
                "time_limit_seconds": 120,
                "max_actions": 8,
            }
        )
        session_id = described["session_id"]
        record = manager.get(session_id)
        assert record is not None
        validated = await wait_for(lambda: bool(record.target_validated), timeout=20.0)
        if not validated:
            raise RuntimeError("the session did not confirm its target identity")
        manager.set_objective(
            session_id, {"kind": "window_title", "parameter": GOAL}
        )
        if pre_fault:
            # Controlled failure through the SAME guarded path: one wrong key
            # puts the harness in FAULT before the policy is asked to decide.
            planned = manager.plan_action(session_id, {"kind": "key", "key": "space"})
            if planned.get("status") != "proposed":
                raise RuntimeError(f"the pre-fault action was refused: {planned}")
            executed = await manager.execute_action(
                session_id,
                {
                    "proposal_id": planned["proposal"]["proposal_id"],
                    "digest": planned["proposal"]["digest"],
                },
            )
            if executed.get("status") != "executed":
                raise RuntimeError(f"the pre-fault action did not execute: {executed}")
            emit("pre_fault", session=session_id, status=executed.get("status"))
        loop = ClosedLoopRunner(
            gateway=manager,
            observations=observation_builder(manager),
            policy=policy,
            limits=LoopLimits(max_actions=8, deadline_seconds=60.0),
        )
        result = await loop.run(session_id)
        trial_record = PolicyTrial.from_loop(result, trial=trial)
        payload = result.to_dict()
        await manager.emergency_stop(session_id)
        return trial_record, payload
    finally:
        stop_harness(process)


async def main() -> int:
    parser = argparse.ArgumentParser(description="NF-10 live policy acceptance")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--output",
        default="",
        help="optional path for the JSON evidence (default: print only)",
    )
    args = parser.parse_args()
    trials = max(1, int(args.trials))

    if not capture_supported_here() or sys.platform != "win32":
        emit("platform", ok=False, detail=f"the disposable-window harness needs Windows; this is {sys.platform}")
        return 2

    runner = LocalDesktopRunner()
    worker = runner.input_worker()
    candidate_trials: list[PolicyTrial] = []
    baseline_trials: list[PolicyTrial] = []
    candidate_runs: list[dict[str, Any]] = []
    baseline_runs: list[dict[str, Any]] = []

    with TemporaryDirectory(prefix="nc_forge_nf10_") as tmp:
        manager = NovaForgeManager(
            data_dir=Path(tmp),
            capture=runner.capture_screen,
            geometry=runner.window_geometry,
            window_lines=runner.window_lines,
            release_inputs=runner.release_all_inputs,
            action_input=ScreenInput(send=worker.send_input),
            objective_probe=runner.window_by_handle,
            capture_supported=True,
            frame_interval=0.25,
            pause_timeout=5.0,
            stop_timeout=8.0,
        )
        try:
            for index in range(trials):
                record, payload = await run_trial(
                    manager, runner, policy=HarnessStagesPolicy(), trial=index + 1
                )
                candidate_trials.append(record)
                candidate_runs.append(payload)
                emit("trial", policy=record.policy, trial=record.trial,
                     outcome=record.outcome, actions=record.actions_executed,
                     transitions=record.observed_transitions,
                     elapsed_s=record.elapsed_seconds, reason=record.reason)

            recovery_record, recovery_payload = await run_trial(
                manager, runner, policy=HarnessStagesPolicy(), trial=1, pre_fault=True
            )
            emit("trial", policy="HarnessStagesPolicy (recovery)", trial=1,
                 outcome=recovery_record.outcome,
                 actions=recovery_record.actions_executed,
                 transitions=recovery_record.observed_transitions,
                 elapsed_s=recovery_record.elapsed_seconds,
                 reason=recovery_record.reason)
            emit("recovery", trial=recovery_record.to_dict())

            for index in range(trials):
                record, payload = await run_trial(
                    manager, runner, policy=FixedKeyPolicy(key="space"), trial=index + 1
                )
                baseline_trials.append(record)
                baseline_runs.append(payload)
                emit("trial", policy=record.policy, trial=record.trial,
                     outcome=record.outcome, actions=record.actions_executed,
                     transitions=record.observed_transitions,
                     elapsed_s=record.elapsed_seconds, reason=record.reason)
        finally:
            await manager.shutdown()
            await runner.close()

    comparison = compare_policies(baseline_trials, candidate_trials)
    candidate_summary = summarise_trials(candidate_trials).to_dict()
    baseline_summary = summarise_trials(baseline_trials).to_dict()
    emit("candidate", summary=candidate_summary)
    emit("baseline", summary=baseline_summary)
    emit("comparison", data=comparison)
    emit("rows", candidate=trial_rows(candidate_trials), baseline=trial_rows(baseline_trials))

    recovery_ok = (
        recovery_record.outcome == "verified_success"
        and recovery_record.actions_executed >= 3
        and recovery_record.observed_transitions >= 3
    )
    emit("recovery_verdict", ok=recovery_ok, trial=recovery_record.to_dict())
    evidence = {
        "task": {
            "target": TARGET,
            "goal": f"window title contains {GOAL}",
            "stages": {
                "IDLE": "A -> ARMED",
                "ARMED": "B -> COMPLETE",
                "FAULT": "R -> IDLE",
                "wrong key": "FAULT",
            },
            "limits": {"max_actions": 8, "deadline_seconds": 60.0},
            "trials_per_policy": trials,
        },
        "candidate": candidate_summary,
        "baseline": baseline_summary,
        "comparison": comparison,
        "raw_candidate": candidate_runs,
        "raw_baseline": baseline_runs,
        "recovery": recovery_record.to_dict(),
        "raw_recovery": recovery_payload,
    }
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        emit("written", path=str(path))

    candidate_ok = (
        candidate_summary["outcomes"]["verified_success"] == trials
        and all(trial.observed_transitions > 0 for trial in candidate_trials)
        and all(trial.actions_executed >= 2 for trial in candidate_trials)
    )
    baseline_failed = (
        baseline_summary["outcomes"]["verified_success"] == 0
        and baseline_summary["outcomes"]["verified_failure"] >= 1
    )
    emit(
        "verdict",
        candidate_state_dependent=candidate_ok,
        baseline_did_not_complete=baseline_failed,
        note="A candidate failure is reported as a failure; nothing is retried for the report.",
    )
    return 0 if (candidate_ok and baseline_failed and recovery_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
