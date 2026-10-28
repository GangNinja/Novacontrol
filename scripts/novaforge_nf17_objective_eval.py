"""NF-17 offline evaluation — what objective understanding gets right, measured.

The evaluation set is a **labelled sequence of NF-16 game states** for the
``nc-forge-mission@1`` fixture, because a game state is exactly where NF-17
starts: the phase reads states, it never captures pixels. Every expectation is
written before the run, so each metric below has a ground truth:

* objective discovery precision and recall (does a reading name the objective
  that is actually on screen, and only that one?),
* progress-state accuracy (value, target and the no-number cases),
* completion-verification accuracy and the false-completion rate (the number
  that must be 0: nothing may be completed without explicit evidence),
* unknown/ambiguous handling (a case that must NOT produce a verdict),
* per-interpretation latency,
* the service's own dedup/ordering guarantees.

There is no live game evaluation in this script. Launching, focusing or driving
a game is a Scope J safety decision made by a person against a real target; the
report records that as ``NOT_TESTED``.

Usage::

    PYTHONPATH=src python scripts/novaforge_nf17_objective_eval.py [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novaforge.objective_service import ObjectiveService
from novacontrol.novaforge.objectives import (
    ObjectiveStatus,
    default_objective_registry,
    interpret,
)
from novacontrol.novaforge.state import (
    GameState,
    HudKind,
    HudReading,
    HudStatus,
    ObservationRef,
    ProfileMatch,
    builtin_profiles,
)

EVAL_JSON = Path("data/novaforge_nf17_objective_eval.json")


def _profile() -> Any:
    return next(item for item in builtin_profiles() if item.profile_id == "nc-forge-mission@1")


def _stamp(seq: int) -> str:
    return (datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seq)).isoformat()


def _state(
    seq: int,
    *,
    session: str = "nf17-eval",
    objective: str | None = "Collect the audit tokens",
    objective_status: HudStatus = HudStatus.RECOGNIZED,
    objective_reason: str = "",
    counter: float | None = 1.0,
    counter_status: HudStatus = HudStatus.RECOGNIZED,
    counter_reason: str = "",
    notification: str = "",
    notification_status: HudStatus = HudStatus.ABSENT,
    screen: tuple[str, ...] = (),
    matched: bool = True,
) -> GameState:
    """One labelled input state. The fixture never contains pixels."""
    hud: list[HudReading] = []
    if objective is not None:
        hud.append(
            HudReading(
                element_id="objective",
                kind=HudKind.PROMPT,
                label="Objective line",
                status=objective_status,
                text=objective if objective_status is HudStatus.RECOGNIZED else "",
                reason=objective_reason,
            )
        )
    if counter is not None:
        hud.append(
            HudReading(
                element_id="progress",
                kind=HudKind.NUMBER,
                label="Progress counter",
                status=counter_status,
                text="" if counter is None else f"{counter:g}",
                value=counter if counter_status is HudStatus.RECOGNIZED else None,
                reason=counter_reason,
            )
        )
    hud.append(
        HudReading(
            element_id="notification",
            kind=HudKind.NOTIFICATION,
            label="Outcome banner",
            status=notification_status,
            text=notification if notification_status is HudStatus.RECOGNIZED else "",
        )
    )
    return GameState(
        game="NC FORGE MISSION",
        observation=ObservationRef(
            observation_id=f"{session}:{seq:04d}",
            session_id=session,
            game="NC FORGE MISSION",
            seq=seq,
            at=_stamp(seq),
            width=1280,
            height=720,
            backend="fixture",
        ),
        sequence=seq,
        timestamp=_stamp(seq),
        hud=tuple(hud),
        text=tuple({"text": line, "line": index} for index, line in enumerate(screen)),
        profile=(
            ProfileMatch(
                profile=_profile(),
                reason_code="profile_matched",
                reason="the fixture title matched",
            )
            if matched
            else ProfileMatch(reason_code="no_profile_for_title", reason="no title matched")
        ),
    )


def _current(result: Any) -> list[Any]:
    return [item for item in result.objectives if item.status is not ObjectiveStatus.SUPERSEDED]


def _by_description(result: Any, description: str) -> Any:
    for item in _current(result):
        if item.description == description:
            return item
    return None


def evaluate_fixtures() -> dict[str, Any]:
    """Run the labelled sequence with handwritten expectations before the run."""
    registry = default_objective_registry()
    checks: list[dict[str, Any]] = []
    latencies: list[float] = []
    # Discovery is scored on DISTINCT descriptions across the run, so one
    # objective observed on three frames is one hit, not three.
    expected_descriptions = {"Collect the audit tokens", "Reach the roof"}
    discovered: set[str] = set()
    discovered_items = 0
    discovered_matched = 0
    non_completion_guards = 0

    def check(category: str, name: str, actual: Any, expected: Any) -> None:
        checks.append(
            {
                "category": category,
                "check": name,
                "ok": actual == expected,
                "expected": expected,
                "actual": actual,
            }
        )

    def read(state: GameState, previous: Any) -> Any:
        nonlocal discovered_items, discovered_matched
        started = time.perf_counter()
        result = interpret(state, previous, registry=registry, now=_fixed(state))
        latencies.append(round((time.perf_counter() - started) * 1000.0, 3))
        for item in _current(result):
            discovered_items += 1
            discovered.add(item.description)
            if item.description in expected_descriptions:
                discovered_matched += 1
        return result

    # ── Sequence 1: discovery → progress → completion ──────────────────────
    s1 = _state(1, counter=1.0)
    r1 = read(s1, None)
    statuses = [item.status for item in _current(r1)]
    check("discovery", "frame 1 reads one objective", len(_current(r1)), 1)
    check("discovery", "frame 1 description", _current(r1)[0].description if _current(r1) else None,
          "Collect the audit tokens")
    check("discovery", "frame 1 is explicit", _current(r1)[0].explicit if _current(r1) else None, True)
    check("discovery", "frame 1 status", statuses[0] if statuses else None,
          ObjectiveStatus.IN_PROGRESS)
    check("progress", "frame 1 value", _progress_value(r1), 1.0)
    check("progress", "frame 1 target", _progress_target(r1), 3.0)
    non_completion_guards += 1
    check("progress", "frame 1 has no completion condition met",
          ObjectiveStatus.COMPLETED in statuses, False)

    s2 = _state(2, counter=2.0)
    r2 = read(s2, r1)
    check("progress", "frame 2 value", _progress_value(r2), 2.0)
    check("progress", "frame 2 fraction", _progress_fraction(r2), 0.5)
    check("progress", "frame 2 start is stable", _progress_start(r2), 1.0)
    check("progress", "frame 2 emits one progress event",
          [event.kind.value for event in r2.events], ["progress"])
    check("progress", "frame 2 stays in progress",
          _by_description(r2, "Collect the audit tokens").status,
          ObjectiveStatus.IN_PROGRESS)

    s3 = _state(3, counter=3.0)
    r3 = read(s3, r2)
    objective3 = _by_description(r3, "Collect the audit tokens")
    check("outcome", "frame 3 status", objective3.status, ObjectiveStatus.COMPLETED)
    check("outcome", "frame 3 evidence names the counter",
          "counter_target" in {item.kind.value for item in objective3.evidence}, True)
    check("outcome", "frame 3 event", [event.kind.value for event in r3.events], ["completed"])
    check("outcome", "frame 3 is one completion",
          len([item for item in _current(r3) if item.status is ObjectiveStatus.COMPLETED]), 1)

    # ── Sequence 2: a fresh session that must fail, not complete ───────────
    s4 = _state(1, session="nf17-eval-failure", counter=2.0)
    r4 = read(s4, r3)
    # A completed objective belongs to its session; a different session starts
    # its own list and does not inherit (or re-close) that verdict.
    check("outcome", "a new session does not inherit the old verdict",
          [event.kind.value for event in r4.events], ["discovered"])
    f5 = _state(
        2,
        session="nf17-eval-failure",
        counter=2.0,
        notification="Mission failed",
        notification_status=HudStatus.RECOGNIZED,
    )
    r5 = read(f5, r4)
    failed = _by_description(r5, "Collect the audit tokens")
    check("outcome", "failure text fails the objective",
          failed.status if failed else None, ObjectiveStatus.FAILED)
    check("outcome", "failure is not a completion",
          failed.status is ObjectiveStatus.COMPLETED if failed else True, False)

    # ── Ambiguity: an unreadable counter must not produce a number ─────────
    a1 = _state(
        10,
        session="nf17-eval-ambiguous",
        counter_status=HudStatus.AMBIGUOUS,
        counter_reason="two values were read",
    )
    ar1 = read(a1, None)
    ao = _by_description(ar1, "Collect the audit tokens")
    check("unknown", "an ambiguous counter has no progress",
          ao.progress if ao else "missing", None)
    check("unknown", "the reason names the ambiguity",
          "two values were read" in (ao.reason if ao else ""), True)
    non_completion_guards += 1
    check("unknown", "an ambiguous counter is not completed",
          ao.status is ObjectiveStatus.COMPLETED if ao else True, False)

    # ── Disappearance: not completion, and nothing else is invented ────────
    g1 = _state(20, session="nf17-eval-vanish", counter=2.0)
    gr1 = read(g1, None)
    g2 = _state(21, session="nf17-eval-vanish", objective_status=HudStatus.ABSENT, counter=2.0)
    gr2 = read(g2, gr1)
    objective = _by_description(gr2, "Collect the audit tokens")
    check("outcome", "a vanished objective is not_verifiable",
          objective.status if objective else None, ObjectiveStatus.NOT_VERIFIABLE)
    non_completion_guards += 1
    check("outcome", "a vanished objective is not completed",
          objective.status is ObjectiveStatus.COMPLETED if objective else True, False)
    check("outcome", "the disappearance is explained",
          "not completion" in (objective.reason if objective else ""), True)

    # ── Conflict: both outcome messages in one reading is no verdict ───────
    c1 = _state(
        30,
        session="nf17-eval-conflict",
        notification="Mission complete / Mission failed",
        notification_status=HudStatus.RECOGNIZED,
    )
    cr1 = read(c1, None)
    co = _by_description(cr1, "Collect the audit tokens")
    check("outcome", "a conflict is reported", len(cr1.diagnostics.conflicts), 1)
    check("outcome", "a conflict claims no verdict",
          co.status if co else None, ObjectiveStatus.ACTIVE)
    check("outcome", "the conflict says no verdict",
          "no verdict" in (co.reason if co else ""), True)

    # ── Precision guard: ordinary interface text is not an objective ───────
    p1 = _state(
        40,
        session="nf17-eval-generic",
        matched=False,
        objective=None,
        counter=None,
        screen=("Press E to interact", "You are carrying 3 tokens", "Ammo 12"),
    )
    pr1 = read(p1, None)
    check("discovery", "ordinary text yields no objective", len(_current(pr1)), 0)
    check("discovery", "the reading says why nothing was read",
          any("named itself" in reason for reason in pr1.reasons), True)

    # ── A generic self-naming line IS an objective (recall) ────────────────
    n1 = _state(
        41,
        session="nf17-eval-generic-2",
        matched=False,
        objective=None,
        counter=None,
        screen=("Objective: Reach the roof", "Press E to interact"),
    )
    nr1 = read(n1, None)
    check("discovery", "a self-naming line is read", len(_current(nr1)), 1)
    check("discovery", "the generic objective keeps its text",
          _current(nr1)[0].description if _current(nr1) else None, "Reach the roof")

    def score(category: str) -> tuple[int, int]:
        rows = [row for row in checks if row["category"] == category]
        return sum(1 for row in rows if row["ok"]), len(rows)

    discovery_ok, discovery_checks = score("discovery")
    progress_ok, progress_checks = score("progress")
    outcome_ok, outcome_checks = score("outcome")
    unknown_ok, unknown_checks = score("unknown")
    passed = sum(1 for row in checks if row["ok"])

    false_completions = 0
    for row in checks:
        if row["check"] in {
            "frame 1 has no completion condition met",
            "failure is not a completion",
            "an ambiguous counter is not completed",
            "a vanished objective is not completed",
        } and row["actual"] is True:
            false_completions += 1
    non_completion_guards += 1  # "failure is not a completion" is one too
    return {
        "evaluation": "offline_fixture",
        "input": "labelled NF-16 states for nc-forge-mission@1 (no pixels, no OCR)",
        "cases": len(checks),
        "passed": passed,
        "accuracy": round(passed / len(checks), 4) if checks else None,
        "checks_by_category": {
            "discovery": {"passed": discovery_ok, "total": discovery_checks},
            "progress": {"passed": progress_ok, "total": progress_checks},
            "outcome": {"passed": outcome_ok, "total": outcome_checks},
            "unknown": {"passed": unknown_ok, "total": unknown_checks},
        },
        "discovery_precision": (
            round(discovered_matched / discovered_items, 4) if discovered_items else None
        ),
        "discovery_recall": (
            round(len(discovered & expected_descriptions) / len(expected_descriptions), 4)
            if expected_descriptions
            else None
        ),
        "progress_accuracy": (
            round(progress_ok / progress_checks, 4) if progress_checks else None
        ),
        "completion_accuracy": round(outcome_ok / outcome_checks, 4) if outcome_checks else None,
        "false_completion_rate": (
            round(false_completions / non_completion_guards, 4)
            if non_completion_guards
            else None
        ),
        "false_completions_observed": false_completions,
        "unknown_handling": round(unknown_ok / unknown_checks, 4) if unknown_checks else None,
        "latency_ms": {
            "samples": len(latencies),
            "average": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "facts": checks,
    }


def evaluate_service() -> dict[str, Any]:
    """The service's own guarantees, measured through its public surface."""
    service = ObjectiveService()
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        checks.append({"check": name, "ok": actual == expected, "expected": expected, "actual": actual})

    async def run() -> None:
        first = await service.observe(_state(1, counter=1.0))
        duplicate = await service.observe(_state(1, counter=1.0))
        check("a duplicate returns the same picture", duplicate is first, True)
        check("a duplicate is counted", service.status()["counts"]["duplicates"], 1)
        check("a duplicate is not a second application", service.status()["counts"]["received"], 1)
        check("one applied reading fills one bounded history row",
              len(service.status()["history"]), 1)
        check("the picture is the first reading",
              service.status()["state"]["objectives"][0]["progress"]["value"], 1.0)
        check("interpretation latency is measured",
              isinstance(service.status()["measured"]["interpretation_ms"]["last"], float), True)
        check("no confidence is reported",
              service.status()["state"]["objectives"][0]["confidence"], None)

    asyncio.run(run())
    passed = sum(1 for row in checks if row["ok"])
    return {
        "evaluation": "service_guarantees",
        "cases": len(checks),
        "passed": passed,
        "facts": checks,
    }


def _progress_value(result: Any) -> float | None:
    objective = _by_description(result, "Collect the audit tokens")
    return objective.progress.value if objective is not None and objective.progress else None


def _progress_target(result: Any) -> float | None:
    objective = _by_description(result, "Collect the audit tokens")
    return objective.progress.target if objective is not None and objective.progress else None


def _progress_start(result: Any) -> float | None:
    objective = _by_description(result, "Collect the audit tokens")
    return objective.progress.start if objective is not None and objective.progress else None


def _progress_fraction(result: Any) -> float | None:
    objective = _by_description(result, "Collect the audit tokens")
    return objective.progress.fraction if objective is not None and objective.progress else None


def _fixed(state: GameState) -> datetime:
    return datetime.fromisoformat(state.timestamp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=EVAL_JSON)
    args = parser.parse_args()

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": "uncommitted working tree",
        "fixtures": evaluate_fixtures(),
        "service": evaluate_service(),
    }
    report["live"] = {
        "status": "NOT_TESTED",
        "reason": (
            "No game was launched, focused or driven: live objective evaluation is a "
            "Scope J safety decision made by a person against a real target, and this "
            "phase sends no input."
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    fixtures = report["fixtures"]
    service = report["service"]
    print(
        f"fixtures: {fixtures['passed']}/{fixtures['cases']} checks passed "
        f"(accuracy {fixtures['accuracy']}, false completions "
        f"{fixtures['false_completions_observed']})"
    )
    print(
        f"discovery precision {fixtures['discovery_precision']} / recall "
        f"{fixtures['discovery_recall']} · progress {fixtures['progress_accuracy']} · "
        f"outcome {fixtures['completion_accuracy']} · unknown {fixtures['unknown_handling']}"
    )
    print(
        f"latency: avg {fixtures['latency_ms']['average']} ms, "
        f"max {fixtures['latency_ms']['max']} ms"
    )
    for row in fixtures["facts"]:
        if not row["ok"]:
            print(f"  FAILED {row['check']}: expected {row['expected']!r}, got {row['actual']!r}")
    print(f"service: {service['passed']}/{service['cases']} guarantees held")
    for row in service["facts"]:
        if not row["ok"]:
            print(f"  FAILED {row['check']}: expected {row['expected']!r}, got {row['actual']!r}")
    print(f"report: {args.json}")
    ok = fixtures["passed"] == fixtures["cases"] and service["passed"] == service["cases"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
