"""Measure NF-06 Coach and experiment overhead on THIS machine.

    python scripts/benchmark_novaforge_coach.py --rounds 5 --duration 1

What is measured, not assumed:

1. **Analysis cost** — a full evidence-backed analysis of one recorded
   evaluation, repeated, so the read-only recomputation is visible; plus the
   capability card, which reads nothing.
2. **Recommendation decisions** — recording a status and listing them.
3. **Proposal and approval** — the pure bookkeeping around an experiment,
   separated from the runs it starts.
4. **Experiment wall time** — both arms through the NF-05 evaluation lab, so
   the two evaluation budgets plus overrun are visible.
5. **Read and report cost** — ``get``, ``history`` and ``results``.
6. **Restart recovery** — new Coach/experiment objects over the same store,
   reading back the statuses and experiments they persisted.
7. **Snapshot size** — both persisted namespaces.

The script uses a deterministic 320x200 PNG capture seam, never a game. It
prints what it measured here and invents no improvement score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from novacontrol.novaforge import (  # noqa: E402
    COACH_VERSION,
    EXPERIMENTS_NAMESPACE,
    EXPERIMENT_VERSION,
    RECOMMENDATIONS_NAMESPACE,
    NovaForgeBenchmarkLab,
    NovaForgeCoach,
    NovaForgeEvaluationLab,
    NovaForgeExperimentLab,
    NovaForgeManager,
)
from novacontrol.persistence import JsonStateStore  # noqa: E402

WINDOW_TITLE = "NF-06 COACH - deterministic"


class BenchWindow:
    title = "NF-06 COACH - deterministic"
    handle = 0xE0C6
    pid = 6611
    bounds = (0, 0, 320, 200)
    client = (0, 0, 320, 200)
    scale = 1.0
    foreground = False


class BenchDesktop:
    """Writes a real 320x200 PNG per capture, exactly like the test double."""

    def __init__(self) -> None:
        self.window: BenchWindow | None = BenchWindow()
        self.capture_count = 0
        # A failing backend is how a capture-failure finding gets real evidence
        # without pretending a window disappeared mid-evaluation.
        self.fail = False

    async def window_lines(self) -> list[str]:
        return [f"python|{WINDOW_TITLE}"]

    async def geometry(self, _title: str) -> BenchWindow | None:
        return self.window

    async def capture(self, path: str) -> dict[str, Any]:
        self.capture_count += 1
        if self.fail:
            raise RuntimeError("capture backend exploded")
        image = Image.new("RGB", (320, 200), (12, 18, 30))
        image.save(path, format="PNG")
        return {
            "fresh": True,
            "verification": "frame written now",
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_ms": 1.5,
            "capture_mode": "coach-benchmark",
        }

    async def release(self) -> tuple[int, int]:
        return 0, 0


def _request(duration: int, interval: float) -> dict[str, Any]:
    return {
        "game_id": "disposable-window",
        "game_version": 1,
        "scenario_id": "observation-baseline",
        "target_title": "NF-06 COACH",
        "duration_seconds": duration,
        "frame_interval_seconds": interval,
        "notes": "nf06 overhead measurement",
        "confirm": True,
    }


def _proposal(duration: int, interval: float) -> dict[str, Any]:
    """A repeat of one configuration, with the arm budget the caller chose.

    Without an explicit budget the spec default (10 s per arm) applies, which
    would fold two 10 s evaluations into the measured wall time and make the
    overhead look like the runner's.
    """
    return {
        "game_id": "disposable-window",
        "scenario_id": "observation-baseline",
        "target_title": "NF-06 COACH",
        "kind": "repeat",
        "variable": "none",
        "baseline_duration_seconds": duration,
        "baseline_frame_interval_seconds": interval,
        "primary_metric": "frames",
        "repeat_count": 1,
    }


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples), 3)


async def measure(rounds: int, duration: int, interval: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="nf06-coach-") as tmp:
        root = Path(tmp)
        desktop = BenchDesktop()
        manager = NovaForgeManager(
            data_dir=root,
            capture=desktop.capture,
            geometry=desktop.geometry,
            window_lines=desktop.window_lines,
            release_inputs=desktop.release,
            frame_interval=interval,
            pause_timeout=2.0,
            stop_timeout=4.0,
        )
        store = JsonStateStore(root)
        benchmarks = NovaForgeBenchmarkLab(manager=manager, store=store, poll_interval=0.05)
        evaluations = NovaForgeEvaluationLab(benchmark=benchmarks, manager=manager, store=store)
        coach = NovaForgeCoach(
            manager=manager,
            benchmark=benchmarks,
            evaluations=evaluations,
            store=store,
        )
        experiments = NovaForgeExperimentLab(
            evaluations=evaluations,
            manager=manager,
            store=store,
            permission=lambda: {
                "checked": True,
                "allowed": True,
                "source": "benchmark",
                "reason": "the benchmark permission layer allows observation.",
            },
        )

        # One real observation evaluation is the subject every analysis reads.
        evaluation = await evaluations.start(_request(duration, interval))
        while True:
            current = evaluations.get(evaluation["id"])
            if current and current["status"] != "running":
                break
            await asyncio.sleep(0.02)
        evaluation_id = str(current["id"])

        status_samples: list[float] = []
        analysis_samples: list[float] = []
        analysis: dict[str, Any] = {}
        for _ in range(rounds):
            started = time.perf_counter()
            coach.status()
            status_samples.append((time.perf_counter() - started) * 1000.0)

            started = time.perf_counter()
            analysis = coach.analyse(evaluation_id=evaluation_id)
            analysis_samples.append((time.perf_counter() - started) * 1000.0)

        # Analysis is deterministic because it is recomputed from the record.
        repeat = coach.analyse(evaluation_id=evaluation_id)
        deterministic = [f["finding_id"] for f in analysis["findings"]] == [
            f["finding_id"] for f in repeat["findings"]
        ]

        recommendations = analysis["recommendations"]
        write_samples: list[float] = []
        list_samples: list[float] = []
        if recommendations:
            target = recommendations[0]["recommendation_id"]
            for _ in range(rounds):
                started = time.perf_counter()
                coach.set_recommendation_status(target, "approved", note="benchmark")
                write_samples.append((time.perf_counter() - started) * 1000.0)

                started = time.perf_counter()
                coach.recommendations(limit=50)
                list_samples.append((time.perf_counter() - started) * 1000.0)

        proposal_started = time.perf_counter()
        proposal = experiments.propose(_proposal(duration, interval))
        propose_ms = (time.perf_counter() - proposal_started) * 1000.0
        experiment_id = str(proposal["id"])

        approve_started = time.perf_counter()
        await experiments.approve(experiment_id, note="benchmark approval")
        approve_ms = (time.perf_counter() - approve_started) * 1000.0

        started = time.perf_counter()
        await experiments.run(experiment_id)
        while True:
            record = experiments.get(experiment_id)
            if record and record["status"] not in {"running", "approved"}:
                break
            await asyncio.sleep(0.02)
        run_wall_ms = (time.perf_counter() - started) * 1000.0

        get_started = time.perf_counter()
        experiments.get(experiment_id)
        get_ms = (time.perf_counter() - get_started) * 1000.0

        history_started = time.perf_counter()
        history = experiments.history(limit=20)
        history_ms = (time.perf_counter() - history_started) * 1000.0

        results_started = time.perf_counter()
        results = (experiments.get(experiment_id) or {}).get("results") or {}
        results_ms = (time.perf_counter() - results_started) * 1000.0

        # A recommendation-carried proposal, on the capture-failure scenario.
        desktop.fail = True
        failed = await evaluations.start(_request(duration, interval))
        while True:
            failed_current = evaluations.get(failed["id"])
            if failed_current and failed_current["status"] != "running":
                break
            await asyncio.sleep(0.02)
        desktop.fail = False
        failed_analysis = coach.analyse(evaluation_id=str(failed_current["id"]))
        capture_rec = next(
            (
                item
                for item in failed_analysis["recommendations"]
                if item["category"] == "capture_failure"
            ),
            None,
        )
        recommendation_proposal: dict[str, Any] | None = None
        if capture_rec is not None:
            recommendation_proposal = experiments.propose(
                {
                    "recommendation_id": capture_rec["recommendation_id"],
                    "evaluation_id": failed_current["id"],
                },
                coach=coach,
            )

        recommendation_snapshot = store.read(RECOMMENDATIONS_NAMESPACE)
        experiment_snapshot = store.read(EXPERIMENTS_NAMESPACE)
        recommendation_bytes = len(
            json.dumps(recommendation_snapshot, default=str).encode("utf-8")
        )
        experiment_bytes = len(json.dumps(experiment_snapshot, default=str).encode("utf-8"))

        restart_started = time.perf_counter()
        restart_coach = NovaForgeCoach(
            manager=manager,
            benchmark=benchmarks,
            evaluations=evaluations,
            store=store,
        )
        restart_experiments = NovaForgeExperimentLab(
            evaluations=evaluations,
            manager=manager,
            store=store,
        )
        restart_statuses = restart_coach.recommendations(limit=50)
        restart_history = restart_experiments.history(limit=20)
        restart_ms = (time.perf_counter() - restart_started) * 1000.0

        verdict = (results.get("verdict") or {})
        return {
            "coach_version": COACH_VERSION,
            "experiment_version": EXPERIMENT_VERSION,
            "platform_note": "deterministic 320x200 PNG capture seam; not a real game",
            "config": {
                "rounds": rounds,
                "duration_seconds": duration,
                "interval_seconds": interval,
            },
            "evaluation": {
                "id": evaluation_id,
                "status": current["status"],
                "findings": len(analysis["findings"]),
                "recommendations": len(recommendations),
                "earliest_available": bool(
                    (analysis.get("earliest_recoverable_error") or {}).get("available")
                ),
                "insufficient_evidence": bool(
                    (analysis.get("insufficient_evidence") or {}).get("state")
                ),
                "deterministic_findings": deterministic,
            },
            "analysis_ms": {
                "status": _median_ms(status_samples),
                "analyse": _median_ms(analysis_samples),
                "min": round(min(analysis_samples), 3),
                "max": round(max(analysis_samples), 3),
            },
            "recommendations_ms": {
                "write_status": _median_ms(write_samples) if write_samples else None,
                "list": _median_ms(list_samples) if list_samples else None,
                "persisted": restart_statuses["count"],
            },
            "experiment": {
                "experiment_id": experiment_id,
                "status": record["status"],
                "propose_ms": round(propose_ms, 3),
                "approve_ms": round(approve_ms, 3),
                "run_wall_ms": round(run_wall_ms, 2),
                "arms": {
                    "baseline": len((record.get("arms") or {}).get("baseline") or []),
                    "candidate": len((record.get("arms") or {}).get("candidate") or []),
                },
                "verdict": verdict.get("label"),
                "improvement_claimed": verdict.get("improvement_claimed"),
                "gameplay_claimed": verdict.get("gameplay_claimed"),
                "objective_verified": verdict.get("objective_verified"),
                "primary_source": (results.get("primary") or {}).get("source"),
                "comparison_compatible": (results.get("comparison") or {}).get("compatible"),
                "comparison_reasons": len((results.get("comparison") or {}).get("reasons") or []),
                "recommendation_proposal_status": (
                    recommendation_proposal or {}
                ).get("status"),
                "recommendation_proposal_metric": (
                    (recommendation_proposal or {}).get("spec") or {}
                ).get("primary_metric"),
            },
            "read_path_ms": {
                "get": round(get_ms, 3),
                "history": round(history_ms, 3),
                "results": round(results_ms, 3),
                "restart_restore": round(restart_ms, 3),
            },
            "history": {
                "count": history["count"],
                "total": history["total"],
                "cap": history["retention"]["cap"],
            },
            "snapshot_bytes": {
                "recommendations": recommendation_bytes,
                "experiments": experiment_bytes,
            },
            "restart_experiments": restart_history["count"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5, help="analysis repetitions")
    parser.add_argument("--duration", type=int, default=1, help="budget per arm run, seconds")
    parser.add_argument("--interval", type=float, default=0.1, help="observation interval, seconds")
    parser.add_argument("--json", type=Path, default=None, help="write the raw records here")
    args = parser.parse_args()

    result = asyncio.run(measure(max(1, args.rounds), max(1, args.duration), args.interval))
    print(json.dumps(result, indent=2, default=str))
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
