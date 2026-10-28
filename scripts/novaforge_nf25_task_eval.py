"""NF-25 task-eval: judge fixtures, count false completions, and time the layer.

Run with ``PYTHONPATH=src python scripts/novaforge_nf25_task_eval.py``.

Everything here is a fixture. No window is read, no input is sent, and every
report says ``simulated`` unless a fixture says otherwise. The point of the run is
the honesty properties, not a score: a dispatch-only run must stay unverified, a
completion missing its declared evidence must stay unverified, and an earlier
cycle's screen must not prove the final one.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Mapping, Sequence  # noqa: E402

from novacontrol.novaforge.autonomy import MAX_CYCLES  # noqa: E402
from novacontrol.novaforge.control import CONTROL_KINDS  # noqa: E402
from novacontrol.novaforge.models import MissionConfig  # noqa: E402
from novacontrol.novaforge.tasks import (  # noqa: E402
    TaskRunner,
    TaskSpec,
    TaskVerdict,
    judge_task,
    task_progress,
)


def _stamp(age: float) -> str:
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() - age, UTC).isoformat()


def spec(**overrides: Any) -> TaskSpec:
    payload: dict[str, Any] = {
        "task_id": "nf25-eval",
        "mission": MissionConfig(
            name="open options",
            objective="Open the options menu and return to the main menu.",
            target_title="Sim Game",
            max_actions=4,
        ),
        "game": "Sim Game",
        "success_conditions": ("the main menu is visible again after the options screen",),
        "permitted_actions": ("key_press", "key_release"),
        "initial_scenes": ("menu",),
        "required_evidence": ("scene:menu",),
        "max_seconds": 60.0,
        "max_cycles": 3,
        "observation_max_age_seconds": 5.0,
        "recovery_limit": 1,
    }
    payload.update(overrides)
    return TaskSpec(**payload)


def run(
    *,
    phase: str,
    outcome: str,
    evidence: list[str],
    dispatched: int,
    outcomes: list[dict[str, Any]],
    scene: str = "menu",
    source: str = "simulated",
    cycles: int = 2,
    steps: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    rows = [
        {
            "index": index + 1,
            "state": {"state_id": f"state-{index + 1}", "scene": {"kind": scene}},
            "verification": {"outcomes": []},
            "objective": {
                "objective_id": "obj-1",
                "evidence": [{"reference": f"objective-evidence-{index + 1}"}],
            },
        }
        for index in range(cycles)
    ]
    if rows:
        rows[-1]["verification"]["outcomes"] = outcomes
        rows[-1]["plan"] = {"plan_id": "plan-1", "steps": [dict(row) for row in steps]}
        rows[-1]["approval"] = {"approved": True}
        rows[-1]["execution"] = {"status": "executed", "steps": [dict(row) for row in steps]}
    return {
        "run_id": "eval-run",
        "session_id": "eval-session",
        "phase": phase,
        "outcome": outcome,
        "reason": "fixture",
        "game": "Sim Game",
        "target": {"title": "Sim Game — Main Menu"},
        "objective": "Open the options menu and return to the main menu.",
        "objective_id": "obj-1",
        "evidence_source": source,
        "started_at": _stamp(10),
        "ended_at": _stamp(0),
        "limits": {"max_actions": 4, "max_cycles": 3},
        "actions_dispatched": dispatched,
        "recoveries": 0,
        "replans": 0,
        "verification_evidence": evidence,
        "cycles": rows,
    }


def verified(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "outcome_id": "o-1",
        "status": "verified_success",
        "observed_effect": "the options screen is visible",
        "evidence": [
            {"ref": "state:state-2", "observation_id": "obs-2", "accepted": True}
        ],
    }
    payload.update(overrides)
    return payload


CASES: list[tuple[str, TaskSpec, dict[str, Any], TaskVerdict]] = [
    (
        "verified completion with its evidence",
        spec(),
        run(
            phase="COMPLETED",
            outcome="objective_completed",
            evidence=["state:state-2", "objective:obj-1"],
            dispatched=2,
            outcomes=[verified()],
        ),
        TaskVerdict.VERIFIED_COMPLETION,
    ),
    (
        "completion missing a required tag",
        spec(required_evidence=("scene:menu", "hud:health")),
        run(
            phase="COMPLETED",
            outcome="objective_completed",
            evidence=["state:state-2"],
            dispatched=2,
            outcomes=[verified()],
        ),
        TaskVerdict.OUTCOME_UNKNOWN,
    ),
    (
        "dispatch-only run",
        spec(),
        run(
            phase="VERIFYING",
            outcome="outcome_unknown",
            evidence=[],
            dispatched=3,
            outcomes=[
                {
                    "outcome_id": "o-1",
                    "status": "outcome_unknown",
                    "observed_effect": "",
                    "evidence": [],
                }
            ],
        ),
        TaskVerdict.OUTCOME_UNKNOWN,
    ),
    (
        "progress without completion",
        spec(),
        run(
            phase="FAILED",
            outcome="session_timed_out",
            evidence=["state:state-2"],
            dispatched=2,
            outcomes=[verified()],
        ),
        TaskVerdict.PROGRESS_ONLY,
    ),
    (
        "stale screen evidence from an earlier cycle",
        spec(),
        run(
            phase="COMPLETED",
            outcome="objective_completed",
            evidence=[],
            dispatched=2,
            outcomes=[verified()],
            scene="death",
        ),
        TaskVerdict.OUTCOME_UNKNOWN,
    ),
    (
        "objective failed",
        spec(),
        run(
            phase="FAILED",
            outcome="objective_failed",
            evidence=[],
            dispatched=2,
            outcomes=[verified(status="verified_failure", observed_effect="no change")],
        ),
        TaskVerdict.OBJECTIVE_FAILED,
    ),
    (
        "an action outside the task's permitted set",
        spec(),
        run(
            phase="COMPLETED",
            outcome="objective_completed",
            evidence=["state:state-2", "objective:obj-1"],
            dispatched=2,
            outcomes=[verified()],
            steps=[
                {"index": 1, "kind": "key_press", "ok": True},
                {"index": 2, "kind": "mouse_move", "ok": True},
            ],
        ),
        TaskVerdict.SAFETY_STOP,
    ),
]


class _Gateway:
    """A scripted supervisor, so the measurement is the task layer's own work."""

    def __init__(self, state: Mapping[str, Any], run_payload: Mapping[str, Any]) -> None:
        self.state = state
        self.run_payload = run_payload

    def session(self, session_id: str) -> Any | None:
        return SimpleNamespace(session_id=session_id)

    def target_title(self, session_id: str) -> str:
        return "Sim Game — Main Menu"

    async def read_state(self) -> Mapping[str, Any]:
        return self.state

    def objective_picture(self, session_id: str) -> Mapping[str, Any]:
        return {"objective_id": "obj-1"}

    def allowed_actions(self) -> Sequence[str]:
        return tuple(sorted(CONTROL_KINDS))

    def resource_gate(self) -> Mapping[str, Any]:
        return {"allow": True}

    async def start_run(self, session_id, spec, *, approved, note):
        return {"started": True, "run_id": "eval-run", "limits": spec.autonomy_limits().to_dict()}

    async def wait_run(self, session_id, *, timeout):
        return {"finished": True, "status": self.run_payload}

    def run(self, session_id):
        return self.run_payload

    async def cancel(self, session_id, *, reason):
        return {"status": "cancelled", "cleanup": {"held": []}}

    def held_inputs(self) -> Sequence[Mapping[str, Any]] | None:
        return ()


def _runner_iteration_ms(iterations: int = 50) -> float:
    state = {
        "analyzed": True,
        "reason": "",
        "state": {
            "state_id": "state-1",
            "sequence": 1,
            "timestamp": _stamp(0.2),
            "scene": {"kind": "menu", "status": "observed", "evidence": ["GAME MENU"]},
            "observation": {"observation_id": "obs-1"},
        },
    }
    payload = CASES[0][2]

    async def one() -> None:
        gateway = _Gateway(state, payload)
        await TaskRunner(gateway).run(spec(), "eval-session", approved=True)

    async def measure() -> float:
        started = time.perf_counter()
        for _ in range(iterations):
            await one()
        return (time.perf_counter() - started) * 1000 / iterations

    return asyncio.run(measure())


def main() -> int:
    failures = 0
    false_completions = 0
    rows: list[dict[str, Any]] = []
    for label, task, payload, expected in CASES:
        report = judge_task(task, payload)
        ok = report.verdict == expected.value
        if report.completed and expected is not TaskVerdict.VERIFIED_COMPLETION:
            false_completions += 1
        if not ok:
            failures += 1
        rows.append(
            {
                "case": label,
                "expected": expected.value,
                "verdict": report.verdict,
                "code": report.code,
                "ok": ok,
                "dispatched": task_progress(payload)["actions_dispatched"],
                "responded": task_progress(payload)["actions_responded"],
                "missing_evidence": report.missing_evidence,
            }
        )

    # ── latency: the pure parts a task adds on top of the supervisor ──────
    task = spec()
    loops = 500
    started = time.perf_counter()
    for _ in range(loops):
        task.errors()
    validate_ms = (time.perf_counter() - started) * 1000 / loops

    payload = CASES[0][2]
    started = time.perf_counter()
    for _ in range(loops):
        judge_task(task, payload)
    judge_ms = (time.perf_counter() - started) * 1000 / loops

    runner_ms = _runner_iteration_ms()

    result = {
        "cases": rows,
        "checked": len(rows),
        "failures": failures,
        "false_completions": false_completions,
        "latency": {
            "spec_validation_ms": round(validate_ms, 4),
            "judge_ms": round(judge_ms, 4),
            "iterations": loops,
            "runner_end_to_end_ms": round(runner_ms, 4),
            "runner_note": (
                "Scripted supervisor: this is the task layer's own overhead, not a "
                "capture or model latency."
            ),
        },
        "fixture_note": (
            "All fixtures are simulated evidence; this run is not a live-game result."
        ),
    }
    out = Path("data/novaforge_nf25_task_eval.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for row in rows:
        print(
            f"{'ok ' if row['ok'] else 'FAIL'} {row['case']}: {row['verdict']} "
            f"({row['code']}) dispatched={row['dispatched']} responded={row['responded']}"
        )
    print(
        f"fixtures: {len(rows) - failures}/{len(rows)} as expected; "
        f"false completions: {false_completions}"
    )
    print(
        f"latency: validate {validate_ms:.4f} ms, judge {judge_ms:.4f} ms "
        f"over {loops} iterations; runner end-to-end {runner_ms:.4f} ms"
    )
    print(f"report: {out}")
    return 0 if failures == 0 and false_completions == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
