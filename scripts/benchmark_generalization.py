"""Phase 26 benchmark — the generalization evaluation, driven through the real API.

Usage:
    PYTHONPATH=src python scripts/benchmark_generalization.py [--json] [--scenario NAME]

Each scenario builds a REAL :class:`~novacontrol.application.NovaControlApplication`
over a temporary data directory, serves it through ``create_app()`` and speaks to
it over HTTP — the same routes a user or a CLI reaches — then CHECKS ITS OWN
EXPECTATION against the response. A scenario that stops behaving fails the run
and the process exits non-zero, so this script is a measurement rather than a
printout.

The nine workflows the phase names (A–I), plus one more:

===========  ==============================================================
scenario     what it must show
===========  ==============================================================
A            a simple task is inferred from examples, verified, and read back
B            an unseen colour/dimension variant is solved by the RULE, and the
             provenance of every answer is ``inferred`` (not a lookup)
C            ambiguous examples are reported AMBIGUOUS, never scored as a match
D            a task outside the library is reported UNSUPPORTED, never answered
E            baseline and candidate run over the SAME held-out task set, and
             the reported scores are recomputed from the reported results
F            two identical runs fingerprint identically, with different ids
G            a tiny search budget terminates inside its limit and says so
H            a worse candidate is flagged as a regression and the baseline
             record is unchanged afterwards
I            the counts on the response equal the counts recomputed from the
             per-task results, per family, and the export agrees
leakage      a held-out task whose answer is already in the fitting set is
             REFUSED with 422 rather than measured
===========  ==============================================================

Honest scope of what is real here:

* The application, the task corpus, the operator library, the bounded search,
  the verifiers, the metrics, the leakage check, the persistence and the routes
  are the SHIPPING implementations — nothing is stubbed and no answer is canned.
* No model takes part: Ollama autodetection is disabled so the measurement is of
  the deterministic benchmark, and every answer's provenance says ``inferred``.

Metrics: per scenario — the verdict, the HTTP codes exercised, the counts
reported, the wall clock, and the run ids it produced. A figure that was not
measured is reported as ``null``, never as zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

# A benchmark of the deterministic benchmark must not spend its first seconds on
# a local-model probe that no solver here consults.
os.environ.setdefault("NOVACONTROL_DISABLE_OLLAMA", "1")
os.environ.setdefault("NOVACONTROL_DISABLE_CLOUD", "1")

from novacontrol.api.app import create_app  # noqa: E402
from novacontrol.application import NovaControlApplication  # noqa: E402
from novacontrol.browser import NoopBrowserRunner  # noqa: E402
from novacontrol.desktop import NoopDesktopRunner  # noqa: E402

try:  # pragma: no cover - import guard mirrors the test suite
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx missing
    print("the benchmark needs fastapi.testclient (httpx); install it first", file=sys.stderr)
    raise SystemExit(2) from None


@dataclass(frozen=True, slots=True)
class Check:
    """One expectation, so a scenario's verdict is readable at a glance."""

    label: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The row ``--json`` prints — spelled out because a slots dataclass has
        no ``__dict__`` to serialise (reading one raised AttributeError, so the
        machine-readable form of this benchmark could not run at all)."""
        return {"label": self.label, "ok": self.ok, "detail": self.detail}


@dataclass
class Scenario:
    """One measured workflow: what it must show, and what it produced."""

    name: str
    description: str
    run: Callable[[Any], tuple[list[Check], dict[str, Any]]]
    checks: list[Check] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "description": self.description,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "checks": [check.to_dict() for check in self.checks],
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------- #
# The scenarios
# --------------------------------------------------------------------------- #


def _scenario_a(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """A simple grid transformation, traced end to end."""
    listed = client.get("/generalization/tasks?split=held_out")
    run = client.post(
        "/generalization/evaluate", json={"task_ids": ["gt_rotate90"], "split": "any"}
    ).json()
    view = client.get(f"/generalization/runs/{run['run_id']}").json()
    result = run["results"][0]
    checks = [
        Check(
            "the task list is reachable and withholds its keys",
            listed.status_code == 200 and listed.json()["answers_withheld"],
        ),
        Check("the run completed", run["status"] == "completed", run["status"]),
        Check("the one task was solved", run["solved"] == 1, f"solved={run['solved']}"),
        Check("the match was exact", result["exact_match"] is True),
        Check(
            "the answer was verified",
            bool(result["verification"] and result["verification"]["verified"]),
        ),
        Check(
            "the answer came from inference", result["source"] == "inferred", str(result["source"])
        ),
        Check("reading the run back agrees", view["solved"] == run["solved"]),
    ]
    evidence = {
        "run_id": run["run_id"],
        "task_id": result["task_id"],
        "candidate": result["candidate"],
        "cells_correct": result["cells_correct"],
        "cells_total": result["cells_total"],
    }
    return checks, evidence


def _scenario_b(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """An unseen variation must be solved by the rule, not by a stored answer."""
    run = client.post(
        "/generalization/evaluate",
        json={
            "task_ids": ["gt_rotate90", "ho_rotate180_non_square", "ho_recolour_bigger_grid"],
            "split": "any",
        },
    ).json()
    results = run["results"]
    checks = [
        Check("three tasks were evaluated", run["tasks_evaluated"] == 3),
        Check("all three were solved", run["solved"] == 3, f"solved={run['solved']}"),
        Check(
            "every answer is provenance-tagged as inferred",
            all(item["source"] == "inferred" for item in results),
            ",".join(sorted({item["source"] for item in results})),
        ),
        Check(
            "the held-out tasks are actually held out",
            sum(1 for item in results if item["split"] == "held_out") == 2,
        ),
        Check(
            "no task id appears in the transformation library",
            _library_mentions_no_task_id(),
        ),
    ]
    evidence = {
        "run_id": run["run_id"],
        "task_ids": [item["task_id"] for item in results],
        "candidates": [item["candidate"] for item in results],
    }
    return checks, evidence


def _library_mentions_no_task_id() -> bool:
    """The adversarial read: no corpus task id may appear in the search code."""
    root = Path(__file__).resolve().parents[1] / "src" / "novacontrol" / "generalization"
    from novacontrol.generalization.tasks import BUILTIN_TASKS

    ids = {task.task_id for task in BUILTIN_TASKS}
    for name in ("transforms.py", "rules.py", "solvers.py"):
        source = (root / name).read_text(encoding="utf-8")
        if any(task_id in source for task_id in ids):
            return False
    return True


def _scenario_c(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """Ambiguity is reported, never resolved by a guess."""
    run = client.post(
        "/generalization/evaluate", json={"task_ids": ["ho_ambiguous_double_reading"]}
    ).json()
    result = run["results"][0]
    checks = [
        Check("the task is AMBIGUOUS", result["status"] == "ambiguous", result["status"]),
        Check(
            "the failure is multiple plausible rules",
            result["failure"] == "multiple_plausible_rules",
            str(result["failure"]),
        ),
        Check(
            "an ambiguous answer is never scored as a match",
            result["exact_match"] is None,
            str(result["exact_match"]),
        ),
        Check("the reason names the competing rules", "rules" in (result["failure_reason"] or "")),
        Check("the run did not count it as solved", run["solved"] == 0),
    ]
    evidence = {
        "run_id": run["run_id"],
        "status": result["status"],
        "failure": result["failure"],
        "reason": result["failure_reason"],
    }
    return checks, evidence


def _scenario_d(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """An unsupported task reports the limitation instead of answering."""
    run = client.post(
        "/generalization/evaluate", json={"task_ids": ["ho_unsupported_row_order"]}
    ).json()
    result = run["results"][0]
    checks = [
        Check("the task is UNSUPPORTED", result["status"] == "unsupported", result["status"]),
        Check(
            "the category is no-candidate-rule-found",
            result["failure"] == "no_candidate_rule_found",
            str(result["failure"]),
        ),
        Check("no answer was produced", result["predicted"] is None, str(result["predicted"])),
        Check("it is not counted as solved", run["solved"] == 0),
        Check("the library itself is enumerable and bounded", len(_operator_names()) <= 30),
    ]
    evidence = {
        "run_id": run["run_id"],
        "status": result["status"],
        "reason": result["failure_reason"],
        "operator_count": len(_operator_names()),
    }
    return checks, evidence


def _operator_names() -> list[str]:
    from novacontrol.generalization.transforms import OPERATOR_NAMES

    return list(OPERATOR_NAMES)


def _scenario_e(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """Baseline and candidate on the same held-out set, scores recomputed."""
    seed = 2026
    candidate = client.post(
        "/generalization/evaluate", json={"split": "held_out", "seed": seed}
    ).json()
    baseline = client.post(
        "/generalization/evaluate",
        json={"split": "held_out", "seed": seed, "solver": "identity", "baseline": "identity"},
    ).json()
    report = client.get(
        f"/generalization/compare?baseline={baseline['run_id']}&candidate={candidate['run_id']}"
    ).json()

    statuses = [item["status"] for item in candidate["results"]]
    same_task_set = [item["task_id"] for item in candidate["results"]] == [
        item["task_id"] for item in baseline["results"]
    ]
    recomputed = statuses.count("solved")
    families_match = {row["family_id"] for row in candidate["metrics"]} == {
        row["family_id"] for row in report["candidate_metrics"]
    }
    checks = [
        Check("both runs covered the same task set", same_task_set),
        Check(
            "the reported score equals the recomputed score",
            candidate["solved"] == recomputed,
            f"{candidate['solved']} vs {recomputed}",
        ),
        Check("the comparison counts every task", report["tasks_compared"] == len(statuses)),
        Check(
            "the candidate beats identity on at least one task",
            len(report["improved"]) > 0,
            f"improved={len(report['improved'])}",
        ),
        Check("the baseline record was preserved", report["baseline_preserved"] is True),
        Check("the comparison's families match the run's own", families_match),
        Check(
            "per-family metrics are reported, not one blended score", len(candidate["metrics"]) >= 5
        ),
    ]
    evidence = {
        "baseline_run_id": baseline["run_id"],
        "candidate_run_id": candidate["run_id"],
        "candidate_solved": candidate["solved"],
        "baseline_solved": baseline["solved"],
        "improved": report["improved"],
        "regressed": report["regressed"],
        "families": sorted(row["family_id"] for row in candidate["metrics"]),
    }
    return checks, evidence


def _scenario_f(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """Two identical runs must be indistinguishable by outcome."""
    body = {"split": "held_out", "seed": 777, "max_candidates": 200}
    first = client.post("/generalization/evaluate", json=body).json()
    second = client.post("/generalization/evaluate", json=body).json()
    first_export = client.get(f"/generalization/runs/{first['run_id']}/export").json()
    second_export = client.get(f"/generalization/runs/{second['run_id']}/export").json()
    checks = [
        Check(
            "the fingerprints match", first_export["fingerprint"] == second_export["fingerprint"]
        ),
        Check("the run ids differ", first["run_id"] != second["run_id"]),
        Check("the seed is recorded", first_export["reproduce"]["seed"] == 777),
        Check(
            "the frozen settings are identical",
            first_export["reproduce"]["settings"] == second_export["reproduce"]["settings"],
        ),
        Check(
            "the per-task outcomes are identical",
            [(r["task_id"], r["status"]) for r in first["results"]]
            == [(r["task_id"], r["status"]) for r in second["results"]],
        ),
        Check(
            "the reproduce block names the solver", first_export["reproduce"]["solver"] == "infer"
        ),
    ]
    evidence = {
        "first_run_id": first["run_id"],
        "second_run_id": second["run_id"],
        "fingerprint": first_export["fingerprint"],
        "seed": 777,
        "tasks": first["tasks_evaluated"],
    }
    return checks, evidence


def _scenario_g(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """A small budget must terminate and report honestly what it did."""
    started = time.monotonic()
    response = client.post(
        "/generalization/evaluate",
        json={
            "task_ids": ["gt_rotate90", "ho_rotate180_non_square"],
            "max_candidates": 1,
            "max_seconds": 0.05,
        },
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    run = response.json()
    results = run["results"]
    statuses = {item["status"] for item in results}
    allowed = {"solved", "budget_exhausted", "unsupported", "failed", "ambiguous"}
    checks = [
        Check("the request was accepted", response.status_code == 200, str(response.status_code)),
        Check(
            "every task ended in a declared state", statuses <= allowed, ",".join(sorted(statuses))
        ),
        Check(
            "an unsolved task carries a classified failure",
            all(item["failure"] for item in results if item["status"] != "solved"),
        ),
        Check(
            "a spent budget is never reported as a wrong answer silently",
            all(
                item["failure"] != "rule_fits_train_fails_test"
                for item in results
                if item["status"] == "budget_exhausted"
            ),
        ),
        Check("it finished inside the ceiling", elapsed_ms < 5000.0, f"{elapsed_ms:.0f}ms"),
        Check(
            "the candidate ceiling the run froze with is the one requested",
            run["settings"]["max_candidates"] == 1,
            str(run["settings"]["max_candidates"]),
        ),
    ]
    evidence = {
        "run_id": run["run_id"],
        "statuses": sorted(statuses),
        "elapsed_ms": elapsed_ms,
        "frozen_max_candidates": run["settings"]["max_candidates"],
        "frozen_max_seconds": run["settings"]["max_seconds"],
    }
    return checks, evidence


def _scenario_h(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """A regression must be detected, and the baseline left alone."""
    good = client.post(
        "/generalization/evaluate", json={"task_ids": ["gt_rotate90"], "split": "any"}
    ).json()
    weak = client.post(
        "/generalization/evaluate",
        json={"task_ids": ["gt_rotate90"], "split": "any", "solver": "no_answer"},
    ).json()
    report = client.get(
        f"/generalization/compare?baseline={good['run_id']}&candidate={weak['run_id']}"
    ).json()
    reread = client.get(f"/generalization/runs/{good['run_id']}").json()
    checks = [
        Check(
            "the regression is named",
            "gt_rotate90" in report["regressed"],
            ",".join(report["regressed"]),
        ),
        Check(
            "the report warns about it",
            any("regressions were detected" in note for note in report["notes"]),
        ),
        Check("the baseline record is unchanged", reread["solved"] == good["solved"]),
        Check("the baseline run still exists", reread["run_id"] == good["run_id"]),
        Check("the weak candidate solved nothing", weak["solved"] == 0, str(weak["solved"])),
    ]
    evidence = {
        "baseline_run_id": good["run_id"],
        "candidate_run_id": weak["run_id"],
        "regressed": report["regressed"],
        "baseline_solved_after": reread["solved"],
    }
    return checks, evidence


def _scenario_i(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """The API's own numbers must be recomputable from the API's own results."""
    run = client.post("/generalization/evaluate", json={"split": "held_out"}).json()
    results = run["results"]
    statuses = [item["status"] for item in results]
    counted: dict[str, int] = {}
    for item in results:
        counted[item["family_id"]] = counted.get(item["family_id"], 0) + 1
    per_family = {row["family_id"]: row["tasks"] for row in run["metrics"]}
    failures = client.get(f"/generalization/failures?run={run['run_id']}").json()
    recommendations = client.get(f"/generalization/recommendations?run={run['run_id']}").json()
    export = client.get(f"/generalization/runs/{run['run_id']}/export").json()
    checks = [
        Check("every evaluated task is in the results", run["tasks_evaluated"] == len(results)),
        Check(
            "solved equals the count of solved results", run["solved"] == statuses.count("solved")
        ),
        Check("ambiguous equals its count", run["ambiguous"] == statuses.count("ambiguous")),
        Check("unsupported equals its count", run["unsupported"] == statuses.count("unsupported")),
        Check("per-family task counts add up", per_family == counted, f"{per_family} vs {counted}"),
        Check(
            "the failure list matches the reported failures",
            failures["count"] == len(run["failures"]),
        ),
        Check(
            "every failure names a category and a reason",
            all(item["category"] and item["reason"] for item in failures["failures"]),
        ),
        Check(
            "recommendations advise without executing",
            recommendations["executes_anything"] is False,
        ),
        Check("the export agrees with the run", export["run"]["solved"] == run["solved"]),
        Check("the export carries a fingerprint", bool(export["fingerprint"])),
        Check("the export names the corpus it measured", bool(export["corpus"]["version"])),
        Check("held-out tasks were actually measured", run["held_out_tasks"] > 0),
    ]
    evidence = {
        "run_id": run["run_id"],
        "tasks": run["tasks_evaluated"],
        "solved": run["solved"],
        "held_out": run["held_out_tasks"],
        "held_out_solved": run["held_out_solved"],
        "failures": failures["count"],
        "families": sorted(counted),
    }
    return checks, evidence


def _scenario_leakage(client: Any) -> tuple[list[Check], dict[str, Any]]:
    """A dirty held-out split is REFUSED, not measured."""
    # Copy a TRAIN task's question and answer into a brand-new HELD_OUT task.
    train = client.get("/generalization/tasks?split=train").json()["tasks"][0]
    whole = client.get(f"/generalization/tasks/{train['task_id']}").json()
    dirty = {
        "task_id": "leaky_duplicate",
        "kind": whole["kind"],
        "family_id": whole["family_id"],
        "split": "held_out",
        "difficulty": whole["difficulty"],
        "request": whole["request"],
        "success_condition": whole["success_condition"],
        "verifier": whole["verifier"],
        "examples": whole["examples"],
        "test_input": whole["test_input"],
        "expected_output": whole["expected_output"],
    }
    # Registering is allowed — the refusal belongs to the MEASUREMENT, where it
    # can say what was wrong with the split rather than silently dropping a task.
    registered = client.post("/generalization/tasks", json=dirty)
    duplicate = client.post("/generalization/tasks", json=dirty)
    refused = client.post("/generalization/evaluate", json={"split": "held_out"})
    splits = client.get("/generalization/splits").json()
    checks = [
        Check(
            "the leakage check reports the split as dirty",
            splits["leakage"]["clean"] is False,
            str(splits["leakage"]["reason"]),
        ),
        Check(
            "the shared answer is named",
            bool(splits["leakage"]["shared_answers"]),
            "; ".join(splits["leakage"]["shared_answers"][:2]),
        ),
        Check(
            "the evaluation is REFUSED with 422",
            refused.status_code == 422,
            str(refused.status_code),
        ),
        Check(
            "the refusal explains itself",
            "not clean" in str(refused.json().get("detail", "")),
            str(refused.json().get("detail", ""))[:160],
        ),
        Check(
            "registering the task succeeded",
            registered.status_code == 200,
            str(registered.status_code),
        ),
        Check(
            "its id cannot be registered twice",
            duplicate.status_code == 422
            and "already in use" in str(duplicate.json().get("detail", "")),
            str(duplicate.json().get("detail", ""))[:160],
        ),
    ]
    evidence = {
        "registered_status": registered.status_code,
        "duplicate_status": duplicate.status_code,
        "evaluate_status": refused.status_code,
        "shared_answers": splits["leakage"]["shared_answers"],
        "clean": splits["leakage"]["clean"],
    }
    return checks, evidence


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("A", "a simple grid transformation, inferred and verified", _scenario_a),
    Scenario("B", "an unseen variation solved by the rule, not a lookup", _scenario_b),
    Scenario("C", "ambiguous examples report ambiguity", _scenario_c),
    Scenario("D", "an unsupported task reports the limitation", _scenario_d),
    Scenario("E", "baseline and candidate on the same held-out set", _scenario_e),
    Scenario("F", "the same configuration twice is reproducible", _scenario_f),
    Scenario("G", "a small budget terminates and says so", _scenario_g),
    Scenario("H", "a regression is detected and the baseline preserved", _scenario_h),
    Scenario("I", "the reported numbers match the evaluated outputs", _scenario_i),
    Scenario("leakage", "a dirty held-out split is refused", _scenario_leakage),
)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def measure(selected: list[str]) -> tuple[list[Scenario], dict[str, Any]]:
    """Run each scenario against one real application over its real routes."""
    chosen = [item for item in SCENARIOS if not selected or item.name in selected]
    with TemporaryDirectory() as directory:

        def _isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = directory
            return NovaControlApplication(**kwargs)

        with (
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=_isolated),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
            TestClient(create_app()) as client,
        ):
            # The corpus as it stands BEFORE anything runs: the leakage
            # scenario deliberately dirties a split at the very end, so a
            # single status read afterwards would report that as if it were
            # the benchmark's starting state.
            status_before = client.get("/generalization/status").json()
            results: list[Scenario] = []
            for scenario in chosen:
                started = time.monotonic()
                checks, evidence = scenario.run(client)
                scenario.elapsed_ms = (time.monotonic() - started) * 1000.0
                scenario.checks = checks
                scenario.evidence = evidence
                results.append(scenario)
            # Read afterwards too: the counters the application kept while
            # these scenarios ran, and the phase's own honesty statement.
            status = client.get("/generalization/status").json()
            overview = client.get("/generalization/overview").json()
    meta = {
        "phase": "26",
        "seed_policy": "each scenario states its own seed; none is random",
        "model_provider": "none (NOVACONTROL_DISABLE_OLLAMA=1)",
        "corpus_version": status_before["corpus"]["corpus_version"],
        "corpus_counts": status_before["corpus"]["counts"],
        "leakage_clean": status_before["corpus"]["leakage"]["clean"],
        "families": len(status_before["families"]),
        "operators": len(status_before["operators"]),
        "solvers": status_before["solvers"],
        "baselines": status_before["baselines"],
        "runs_after": status["runs"],
        "counters": status["counters"],
        "gameplay_claimed": overview["claims_gameplay"],
        "aggregate_score_exists": overview["single_intelligence_score"],
        "python": sys.version.split()[0],
    }
    return results, meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of rows")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only the named scenario (A..I, leakage); may be repeated",
    )
    args = parser.parse_args(argv)

    rows, meta = measure(list(args.scenario))
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
        print(f"Phase 26 generalization benchmark — {len(rows)} scenarios\n")
        for row in rows:
            mark = "PASS" if row.ok else "FAIL"
            print(f"  [{mark}] {row.name}: {row.description} ({(row.elapsed_ms or 0.0):.0f}ms)")
            for check in row.checks:
                if not check.ok:
                    print(f"         ✗ {check.label} — {check.detail}")
        print(
            f"\n{len(rows) - len(failed)}/{len(rows)} scenarios matched their declared outcome"
            + (f"; FAILED: {', '.join(row.name for row in failed)}" if failed else "")
        )
        print(
            f"corpus {meta['corpus_version']} {meta['corpus_counts']} | "
            f"leakage clean={meta['leakage_clean']} | "
            f"{meta['families']} families | {meta['operators']} operators | "
            f"gameplay_claimed={meta['gameplay_claimed']} | "
            f"aggregate_score_exists={meta['aggregate_score_exists']}"
        )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
    sys.exit(main())
