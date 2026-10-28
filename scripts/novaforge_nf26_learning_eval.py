"""NF-26 learning eval: baseline versus adapted, with the honesty controls.

Run with ``PYTHONPATH=src python scripts/novaforge_nf26_learning_eval.py``.

Everything here is a deterministic fixture built in the shape of NF-25's judged
task report. No window is read, no input is sent, no model is loaded, and every
record says ``simulated``. The point of the run is not a score: it is that the
comparator claims an improvement when the evaluation data supports one, refuses
to when it does not, and never promotes a tuning-set advantage, a busier arm, or
a safety regression into an improvement.

The tuning arms (set A) are the data a candidate profile would be chosen from.
The evaluation arms (set B, disjoint runs) are the only data the verdict below
is drawn from.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novacontrol.novaforge.control import ControlPolicy  # noqa: E402
from novacontrol.novaforge.learning import (  # noqa: E402
    DEFAULT_MAX_RECORDS,
    MIN_TRIALS,
    ExperienceLabel,
    ExperienceRecord,
    ExperienceStore,
    LearningSnapshot,
    StrategyProfile,
    compare_strategies,
    metrics_for,
    wilson_interval,
)
from novacontrol.persistence.json_store import JsonStateStore  # noqa: E402
from novacontrol.novaforge.planner import PlanningContext, StrategyHint  # noqa: E402
from novacontrol.novaforge.tasks import TaskVerdict  # noqa: E402

SESSION = "nf26-eval"
GAME = "Sim Game"
OBJECTIVE = "Open the options menu and return to the main menu."
EVALUATION_DAY = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def task_report(
    *,
    run_id: str,
    verdict: str,
    code: str,
    dispatched: int = 2,
    responded: int = 1,
    progress: int = 1,
    recoveries: int = 0,
    seconds: float = 12.0,
    held: int | None = None,
) -> dict[str, Any]:
    """One judged NF-25 report, in the shape the task layer really produces."""
    started = EVALUATION_DAY - timedelta(seconds=seconds)
    return {
        "task_id": run_id,
        "session_id": SESSION,
        "status": "finished",
        "verdict": verdict,
        "code": code,
        "reason": "eval fixture",
        "game": GAME,
        "target": "Sim Game — Main Menu",
        "objective": OBJECTIVE,
        "success_conditions": ["the main menu is visible again"],
        "required_evidence": ["scene:menu"],
        "evidence_found": ["scene:menu", "state:state-1"],
        "missing_evidence": [],
        "initial": {"state_id": "state-1", "scene": "menu"},
        "limits": {"max_actions": 4, "max_cycles": 3},
        "resource_gate": {"allow": True},
        "progress": {
            "cycles": 1,
            "actions_dispatched": dispatched,
            "actions_responded": responded,
            "verified_successes": progress,
            "verified_failures": 0,
            "partial_successes": 0,
            "unverified_outcomes": 0,
            "recoveries": recoveries,
            "replans": 0,
            "evidence_refs": 2,
        },
        "stages": {},
        "verification": {
            "run_outcome": verdict,
            "run_phase": "COMPLETED",
            "verification_evidence": ["state:state-1"],
            "action_policy": {
                "permitted": ["key_press", "key_release"],
                "dispatched": ["key_press"],
                "unlabelled": 0,
                "violations": [],
            },
            "held_inputs_after": held,
        },
        "run": {
            "run_id": run_id,
            "phase": "COMPLETED",
            "outcome": verdict,
            "cycles": 1,
            "actions_dispatched": dispatched,
            "actions_responded": responded,
            "evidence_source": "simulated",
        },
        "evidence_source": "simulated",
        "simulated": True,
        "started_at": started.isoformat(),
        "ended_at": EVALUATION_DAY.isoformat(),
        "limitations": [],
    }


def make(
    strategy_id: str,
    *,
    arm: str,
    index: int,
    completions: int,
    failures: int,
    uncertain: int = 0,
    version: int = 1,
    dispatched: int = 2,
    responded: int = 1,
    progress: int = 1,
    recoveries: int = 0,
    seconds: float = 12.0,
) -> list[ExperienceRecord]:
    """One arm's trials: what its completions/failures/uncertain runs look like."""
    total = completions + failures + uncertain
    rows: list[ExperienceRecord] = []
    for position in range(total):
        if position < completions:
            verdict, code = TaskVerdict.VERIFIED_COMPLETION.value, "objective_completed"
        elif position < completions + failures:
            verdict, code = TaskVerdict.OBJECTIVE_FAILED.value, "objective_failed"
        else:
            verdict, code = TaskVerdict.PROGRESS_ONLY.value, "no_verified_completion"
        run_id = f"{arm}-{index}-{position}"
        built = ExperienceRecord.from_task_report(
            task_report(
                run_id=run_id,
                verdict=verdict,
                code=code,
                dispatched=dispatched,
                responded=responded,
                progress=progress,
                recoveries=recoveries,
                seconds=seconds,
            ),
            strategy_id=strategy_id,
            strategy_version=version if strategy_id != "baseline" else 0,
        )
        assert built is not None, run_id
        rows.append(built)
    return rows


def summarise(label: str, comparison: Any, expected: str) -> dict[str, Any]:
    row = {
        "case": label,
        "verdict": comparison.verdict,
        "expected": expected,
        "ok": comparison.verdict == expected,
        "reason": comparison.reason,
        "primary_metric": comparison.primary_metric,
        "baseline_decided": comparison.sample.get("baseline_decided"),
        "candidate_decided": comparison.sample.get("candidate_decided"),
        "baseline_interval": list(comparison.baseline.completion_interval or ()),
        "candidate_interval": list(comparison.candidate.completion_interval or ()),
        "deltas": dict(comparison.metric_deltas),
        "safety_ok": comparison.safety_ok,
        "safety_notes": list(comparison.safety_notes),
        "live_evidence": comparison.live_evidence,
        "evidence_ids": len(comparison.evidence_ids),
    }
    print(
        f"{'ok ' if row['ok'] else 'FAIL'} {label}: {comparison.verdict} "
        f"(expected {expected}) decided {row['baseline_decided']}/{row['candidate_decided']} "
        f"safety_ok={comparison.safety_ok}"
    )
    print(f"     {comparison.reason}")
    return row


def label_honesty() -> dict[str, Any]:
    """The uncertainty policy, exercised: what must never become a label."""
    checks: list[dict[str, Any]] = []

    def check(name: str, built: ExperienceRecord | None, expected: str | None) -> None:
        label = built.label if built is not None else None
        checks.append(
            {"case": name, "label": label, "expected": expected, "ok": label == expected}
        )

    check(
        "a report that never ran",
        ExperienceRecord.from_task_report(
            {"task_id": "t", "status": "refused", "verdict": ""}
        ),
        None,
    )
    check(
        "a dispatch-only run",
        ExperienceRecord.from_task_report(
            task_report(
                run_id="dispatch-only",
                verdict=TaskVerdict.OUTCOME_UNKNOWN.value,
                code="outcome_unknown",
                progress=0,
            ),
            # The run payload carries the SUBMITTED steps: a report claiming two
            # dispatches while the run shows none is contradictory and the record
            # is refused rather than labelled (pinned by the record tests).
            run={
                "run_id": "dispatch-only",
                "phase": "VERIFYING",
                "cycles": [
                    {
                        "index": 1,
                        "state": {"state_id": "state-1", "scene": {"kind": "menu"}},
                        "execution": {"steps": [{"kind": "key_press"}, {"kind": "key_press"}]},
                    }
                ],
            },
        ),
        ExperienceLabel.UNCERTAIN.value,
    )
    check(
        "progress without completion",
        ExperienceRecord.from_task_report(
            task_report(
                run_id="progress-only",
                verdict=TaskVerdict.PROGRESS_ONLY.value,
                code="no_verified_completion",
            ),
            strategy_id="candidate",
            strategy_version=1,
        ),
        ExperienceLabel.UNCERTAIN.value,
    )
    check(
        "a verified completion",
        ExperienceRecord.from_task_report(
            task_report(
                run_id="completed",
                verdict=TaskVerdict.VERIFIED_COMPLETION.value,
                code="objective_completed",
            )
        ),
        ExperienceLabel.VERIFIED_SUCCESS.value,
    )
    check(
        "a judged failure",
        ExperienceRecord.from_task_report(
            task_report(
                run_id="failed",
                verdict=TaskVerdict.OBJECTIVE_FAILED.value,
                code="objective_failed",
            )
        ),
        ExperienceLabel.VERIFIED_FAILURE.value,
    )
    check(
        "a safety stop",
        ExperienceRecord.from_task_report(
            task_report(
                run_id="safety",
                verdict=TaskVerdict.SAFETY_STOP.value,
                code="action_policy_exceeded",
            )
        ),
        ExperienceLabel.UNCERTAIN.value,
    )
    false_labels = sum(
        1
        for row in checks
        if row["case"] in {"a report that never ran", "a dispatch-only run"}
        and row["label"] in {ExperienceLabel.VERIFIED_SUCCESS.value, ExperienceLabel.VERIFIED_FAILURE.value}
    )
    for row in checks:
        print(f"{'ok ' if row['ok'] else 'FAIL'} label: {row['case']} -> {row['label']}")
    return {"checks": checks, "false_labels": false_labels}


def latency() -> dict[str, Any]:
    """The learning layer's own cost, and what it adds to one planning decision."""
    payload = task_report(
        run_id="latency",
        verdict=TaskVerdict.VERIFIED_COMPLETION.value,
        code="objective_completed",
    )
    loops = 500
    started = time.perf_counter()
    for _ in range(loops):
        ExperienceRecord.from_task_report(payload, strategy_id="candidate", strategy_version=1)
    build_ms = (time.perf_counter() - started) * 1000 / loops

    records = make("baseline", arm="b", index=0, completions=3, failures=3)
    started = time.perf_counter()
    for _ in range(loops):
        metrics_for(records, strategy_id="baseline")
    metrics_ms = (time.perf_counter() - started) * 1000 / loops

    candidate = make("candidate", arm="c", index=0, completions=5, failures=1)
    started = time.perf_counter()
    for _ in range(loops):
        compare_strategies(records, candidate)
    compare_ms = (time.perf_counter() - started) * 1000 / loops

    profile = StrategyProfile(
        strategy_id="candidate",
        version=1,
        preferred_kinds=("key_press",),
        avoided_kinds=("key_hold",),
        preference_weight=0.5,
        max_actions_per_plan=2,
        max_state_age_seconds=4.0,
        evidence=("exp:1",),
        rationale="latency fixture",
    )
    started = time.perf_counter()
    for _ in range(loops * 2):
        hint = profile.hint()
        hint.usable
        hint.errors()
    hint_ms = (time.perf_counter() - started) * 2000 / loops

    # The per-plan cost the hint adds: the planner validates it (``_active_hint``)
    # and records it on the plan. Measured with and without, on the same context.
    def context(strategy: StrategyHint | None) -> PlanningContext:
        return PlanningContext(
            session_id=SESSION,
            game=GAME,
            target=None,
            state=None,
            objective_state=None,
            knowledge=(),
            gate={"allow": True, "state": "allowed", "reason": "eval"},
            budget_remaining=4,
            state_age_seconds=1.0,
            control_policy=ControlPolicy(),
            strategy=strategy,
        )

    plain = context(None)
    hinted = context(profile.hint())
    started = time.perf_counter()
    for _ in range(loops * 2):
        row = plain.strategy
        row.errors() if row is not None else None
    plain_ms = (time.perf_counter() - started) * 1000 / (loops * 2)
    started = time.perf_counter()
    for _ in range(loops * 2):
        row = hinted.strategy
        row.errors() if row is not None else None
        row.to_dict()
    hinted_ms = (time.perf_counter() - started) * 1000 / (loops * 2)

    return {
        "iterations": loops,
        "record_build_ms": round(build_ms, 4),
        "metrics_ms": round(metrics_ms, 4),
        "comparison_ms": round(compare_ms, 4),
        "profile_and_hint_build_ms": round(hint_ms, 4),
        "planning_context_plain_ms": round(plain_ms, 5),
        "planning_context_hinted_ms": round(hinted_ms, 5),
        "planning_hint_delta_ms": round(hinted_ms - plain_ms, 5),
        "note": (
            "Fixture-only measurements: no capture, no model, no input. The "
            "planning delta is the validation + traceability work a hint adds to "
            "one planning decision, not a full plan (the real planner path is "
            "exercised by tests/test_novaforge_nf26.py)."
        ),
    }


def storage(records: list[ExperienceRecord]) -> dict[str, Any]:
    """Round-trip through the application's own JSON store, bounded and counted."""
    with tempfile.TemporaryDirectory() as root:
        store = JsonStateStore(root)
        ledger = ExperienceStore(store, max_records=400, max_changes=50)
        started = time.perf_counter()
        wrote = ledger.write(LearningSnapshot(records=tuple(records)))
        write_ms = (time.perf_counter() - started) * 1000
        path = Path(root) / f"{ledger.namespace}.json"
        size = path.stat().st_size if path.exists() else 0
        started = time.perf_counter()
        snapshot = ledger.read()
        read_ms = (time.perf_counter() - started) * 1000
        original = [item.to_dict() for item in records]
        reloaded = [item.to_dict() for item in snapshot.records]
        # ``recorded_at`` is when the layer heard about the run; the row does not
        # need a second write to make it stable, so the round trip must be exact.
        rebuilt = ExperienceRecord.from_dict(original[1]) if len(original) > 1 else None
    return {
        "records": len(records),
        "wrote": wrote,
        "reloaded": len(reloaded),
        "exact_round_trip": bool(original) and [item for item in reloaded] == original,
        "dropped": snapshot.dropped,
        "duplicates": snapshot.duplicates,
        "bytes_total": size,
        "bytes_per_record": round(size / max(1, len(records)), 1),
        "write_ms": round(write_ms, 3),
        "read_ms": round(read_ms, 3),
        "a_rebuilt_record_is_valid": bool(rebuilt is not None and rebuilt.valid),
        "note": (
            "Compact rows only: ids, references, counts and metrics. No frames, no "
            "screenshots, no OCR text are stored by this layer."
        ),
    }


def memory() -> dict[str, Any]:
    """What the layer holds in memory: the record set at its cap, and one hint.

    Fixture-only and includes the list itself, because that is what the store
    holds. It deliberately does NOT include the JSON text the store writes (the
    storage run measures those bytes) or anything a game or model would hold.
    """
    tracemalloc.start()
    records = [
        ExperienceRecord.from_task_report(
            task_report(
                run_id=f"mem-{index}",
                verdict=TaskVerdict.VERIFIED_COMPLETION.value,
                code="objective_completed",
            ),
            strategy_id="candidate",
            strategy_version=1,
        )
        for index in range(DEFAULT_MAX_RECORDS)
    ]
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    profile = StrategyProfile(
        strategy_id="candidate",
        version=1,
        preferred_kinds=("key_press",),
        avoided_kinds=("key_hold",),
        preference_weight=0.5,
        max_actions_per_plan=2,
        max_state_age_seconds=4.0,
        evidence=("exp:1",),
        rationale="memory fixture",
    )
    tracemalloc.start()
    hint = profile.hint()
    hint.to_dict()
    hint.errors()
    hint_retained, hint_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "records": len(records),
        "retained_bytes": retained,
        "peak_bytes": peak,
        "bytes_per_record": round(retained / max(1, len(records)), 1),
        "hint_retained_bytes": hint_retained,
        "hint_peak_bytes": hint_peak,
        "note": (
            "Fixture-only: the record set at DEFAULT_MAX_RECORDS plus its list, and "
            "one hint with its validated dict. No frames, models or game memory are "
            "included, and an unmeasured figure is not reported as zero."
        ),
    }


def main() -> int:
    print(f"NF-26 learning eval - fixtures, simulated evidence (MIN_TRIALS={MIN_TRIALS})\n")

    # ── tuning data (set A): where the candidate profile would be chosen ──
    tuning_baseline = make("baseline", arm="a-base", index=1, completions=0, failures=6)
    tuning_candidate = make("candidate", arm="a-cand", index=1, completions=6, failures=0)

    # ── evaluation data (set B): disjoint runs, the only data a verdict uses ──
    evaluation_baseline = make("baseline", arm="b-base", index=7, completions=0, failures=6)
    evaluation_candidate = make("candidate", arm="b-cand", index=7, completions=6, failures=0)

    # ── honesty control 1: an arm that just sends more actions, and fails ──
    busy = make(
        "candidate", arm="b-busy", index=13, completions=0, failures=6, dispatched=6, responded=0
    )

    # ── honesty control 2: the advantage exists on the tuning data only ──
    even_baseline = make("baseline", arm="c-base", index=19, completions=3, failures=3)
    even_candidate = make("candidate", arm="c-cand", index=19, completions=3, failures=3)

    # ── honesty control 3: more completions, but a safety metric got worse ──
    safety_row = ExperienceRecord.from_task_report(
        task_report(
            run_id="d-safety",
            verdict=TaskVerdict.SAFETY_STOP.value,
            code="action_policy_exceeded",
            progress=0,
        ),
        strategy_id="candidate",
        strategy_version=1,
    )
    assert safety_row is not None
    risky = [*make("candidate", arm="d-cand", index=25, completions=6, failures=0), safety_row]
    risk_baseline = make("baseline", arm="d-base", index=25, completions=1, failures=5)

    rows: list[dict[str, Any]] = []
    rows.append(
        summarise(
            "tuning set A: candidate vs baseline",
            compare_strategies(tuning_baseline, tuning_candidate),
            "improvement",
        )
    )
    rows.append(
        summarise(
            "evaluation set B: candidate vs baseline",
            compare_strategies(evaluation_baseline, evaluation_candidate),
            "improvement",
        )
    )
    rows.append(
        summarise(
            "evaluation set B: a busier arm that fails more",
            compare_strategies(evaluation_baseline, busy),
            "no_difference",
        )
    )
    rows.append(
        summarise(
            "evaluation set C: an advantage that exists only in tuning",
            compare_strategies(even_baseline, even_candidate),
            "no_difference",
        )
    )
    rows.append(
        summarise(
            "evaluation set D: more completions, one safety stop",
            compare_strategies(risk_baseline, risky),
            "regression",
        )
    )
    rows.append(
        summarise(
            "a two-trial arm is not evidence",
            compare_strategies(
                make("baseline", arm="e-base", index=31, completions=0, failures=2),
                make("candidate", arm="e-cand", index=31, completions=2, failures=0),
            ),
            "insufficient_evidence",
        )
    )

    labels = label_honesty()
    print()
    measured = latency()
    metrics = metrics_for(evaluation_candidate, strategy_id="candidate")
    interval = wilson_interval(
        metrics.verified_successes, max(1, metrics.decided)
    )
    print(
        f"evaluation candidate: {metrics.verified_successes}/{metrics.decided} completions, "
        f"successes/min {metrics.verified_progress_per_minute}, "
        f"unnecessary actions {metrics.unnecessary_actions}, "
        f"mean {metrics.mean_task_seconds}s, Wilson {interval}"
    )

    stored = storage([*tuning_baseline, *tuning_candidate, *evaluation_baseline, *evaluation_candidate])
    print(
        f"store: {stored['reloaded']}/{stored['records']} rows, exact round trip "
        f"{stored['exact_round_trip']}, {stored['bytes_per_record']} bytes/record, "
        f"dropped {stored['dropped']}"
    )
    held = memory()
    print(
        f"memory: {held['records']} records retained {held['retained_bytes']} bytes "
        f"({held['bytes_per_record']} bytes/record, peak {held['peak_bytes']}), "
        f"one hint {held['hint_retained_bytes']} bytes"
    )

    failures = sum(1 for row in rows if not row["ok"])
    false_labels = labels["false_labels"]
    live_claims = sum(1 for row in rows if row["live_evidence"])
    result = {
        "schema": "nf26.eval.1",
        "cases": rows,
        "labels": labels,
        "latency": measured,
        "storage": stored,
        "memory": held,
        "checked": len(rows),
        "failures": failures,
        "false_labels": false_labels,
        "live_evidence_claims": live_claims,
        "fixture_note": (
            "All arms are deterministic fixtures built from NF-25's judged report "
            "shape with evidence_source=simulated. This is a simulation-based "
            "demonstration of the evaluation method, NOT a real-game result: no "
            "game was launched, observed or driven."
        ),
    }
    out = Path("data/novaforge_nf26_learning_eval.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(
        f"\ncases: {len(rows) - failures}/{len(rows)} as expected; false labels: "
        f"{false_labels}; live-evidence claims: {live_claims}"
    )
    print(
        f"latency: record {measured['record_build_ms']} ms, metrics "
        f"{measured['metrics_ms']} ms, comparison {measured['comparison_ms']} ms, "
        f"hint build {measured['profile_and_hint_build_ms']} ms, planning hint delta "
        f"{measured['planning_hint_delta_ms']} ms"
    )
    print(f"report: {out}")
    return 0 if failures == 0 and false_labels == 0 and live_claims == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
