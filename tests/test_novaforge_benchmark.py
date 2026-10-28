"""NovaForge NF-04 tests: scenarios, metrics, aggregation, fairness, runner.

The runner tests use the REAL session manager with the same injected desktop
double NF-01/NF-02 use (a fake window plus a capture that writes a real PNG).
No game is launched and no gameplay input exists to send.

Two properties are pinned repeatedly on purpose, because they are the phase's
honesty contract:

* a missing measurement is an unavailable row with a reason, never a zero;
* a completed run is *data completeness*, never objective success.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from novacontrol.novaforge import (
    BENCHMARK_VERSION,
    BenchmarkConflictError,
    BenchmarkNotFoundError,
    BenchmarkValidationError,
    MissionConfig,
    NovaForgeBenchmarkLab,
    NovaForgeManager,
    RUNS_NAMESPACE,
    aggregate_runs,
    collect_metrics,
    compare_runs,
    config_from_request,
    resolve_scenario,
    scenario_payload,
)
from novacontrol.persistence.json_store import JsonStateStore

from test_novaforge import FakeDesktop, _wait_for, _mission


def _telemetry(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session": {"duration_seconds": 5.0, "active_seconds": 5.0},
        "capture": {
            "frames": 9,
            "stale_frames": 1,
            "capture_failures": 0,
            "frame_gaps": 2,
            "max_gap_ms": 120.0,
            "latency_ms": {
                "samples": 9,
                "median_ms": 8.0,
                "p95_ms": None,
                "p95_reason": "Only 9 latency sample(s) are available; a p95 needs at least 20.",
                "max_ms": 12.0,
            },
        },
        "loop": {
            "configured_interval_ms": 500.0,
            "observed_frame_rate_hz": None,
            "rate_reason": "Less than 5s of active time has elapsed.",
        },
        "target": {"checks": 8, "losses": 0, "changes": 1},
    }
    payload.update(overrides)
    return payload


def _row(value: Any, *, available: bool = True, reason: str = "", unit: str = "") -> dict[str, Any]:
    return {
        "value": value if available else None,
        "available": available,
        "reason": "" if available else reason,
        "source": "fixture",
        "kind": "measured" if available else "unsupported",
        "unit": unit,
    }


def _stored_run(
    run_id: str,
    *,
    status: str = "completed",
    valid: bool = True,
    metrics: dict[str, dict[str, Any]] | None = None,
    duration: int = 10,
    interval: float = 0.5,
    title: str = "NC FORGE TEST - disposable",
    width: int = 1000,
    version: int = 1,
    scenario_id: str = "observation-baseline",
    reason: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    scenario = resolve_scenario(scenario_id, version)
    assert scenario is not None
    payload = scenario.to_dict()
    defaults = {
        "frames": _row(10, unit="frames"),
        "frame_yield": _row(0.8, unit="ratio"),
        "frame_gaps": _row(0, unit="gaps"),
        "max_gap_ms": _row(100.0, unit="ms"),
        "latency_median_ms": _row(8.0, unit="ms"),
        "duration_seconds": _row(float(duration), unit="s"),
        "active_seconds": _row(float(duration), unit="s"),
        "target_checks": _row(10, unit="checks"),
        "timeline_events": _row(12, unit="events"),
    }
    if metrics is not None:
        defaults.update(metrics)
    return {
        "version": BENCHMARK_VERSION,
        "id": run_id,
        "scenario": payload,
        "configuration": {
            "schema_version": 1,
            "scenario_id": scenario_id,
            "version": version,
            "target_title": title,
            "duration_seconds": duration,
            "frame_interval_seconds": interval,
            "notes": "",
            "model_id": "",
            "policy_id": "",
        },
        "status": status,
        "valid": valid,
        "reason": reason,
        "session_id": f"session-{run_id}",
        "created_at": created_at or f"2026-10-18T10:00:0{len(run_id) % 10}Z",
        "started_at": "2026-10-18T10:00:00Z",
        "ended_at": "2026-10-18T10:00:10Z",
        "duration_seconds": float(duration),
        "target": {"title": title, "width": width, "height": 600, "scale": 1.0},
        "environment": {
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "window": {"title": title, "width": width, "height": 600, "scale": 1.0},
        },
        "metrics": defaults,
        "evidence": {
            "session_available": True,
            "timeline_endpoint": f"/novaforge/sessions/session-{run_id}/timeline",
        },
        "interrupted": False,
    }


class ScenarioCatalogTests(unittest.TestCase):
    def test_catalog_pins_supported_and_unsupported_scenarios(self) -> None:
        payload = scenario_payload()
        self.assertEqual(payload["version"], BENCHMARK_VERSION)
        by_id = {item["scenario_id"]: item for item in payload["scenarios"]}
        self.assertTrue(by_id["observation-baseline"]["supported"])
        self.assertTrue(by_id["capture-latency"]["supported"])
        self.assertTrue(by_id["observation-continuity"]["supported"])
        # The controlled scenario is run only through a confirmed evaluation.
        self.assertTrue(by_id["controlled-objective"]["supported"])
        self.assertTrue(by_id["controlled-objective"]["objective_required"])
        self.assertEqual(by_id["controlled-objective"]["evaluation"], "controlled_objective")
        self.assertFalse(by_id["gameplay-objective"]["supported"])
        self.assertFalse(by_id["gameplay-objective"]["objective_required"])
        self.assertIn("decision pipeline", by_id["gameplay-objective"]["unsupported_reason"])
        self.assertEqual(payload["supported_count"], 4)
        self.assertEqual(payload["unsupported_count"], 1)
        self.assertTrue(payload["metric_catalog"])

    def test_every_scenario_metric_has_a_definition(self) -> None:
        catalog = {spec["key"] for spec in scenario_payload()["metric_catalog"]}
        for item in scenario_payload()["scenarios"]:
            for key in item["metrics"]:
                with self.subTest(scenario=item["scenario_id"], metric=key):
                    self.assertIn(key, catalog)

    def test_runnable_scenarios_declare_the_verdict_they_can_actually_reach(self) -> None:
        """An observation run claims no objective; a controlled run requires one.

        The two kinds must not share a shape: an observation scenario that
        declared a verifier would let data completeness look like a won game,
        and a controlled scenario without one would have nothing to verify.
        """
        for item in scenario_payload()["scenarios"]:
            if not item["supported"]:
                continue
            with self.subTest(scenario=item["scenario_id"]):
                if item["objective_required"]:
                    self.assertEqual(item["evaluation"], "controlled_objective")
                    self.assertTrue(item["verifier"]["available"])
                    self.assertIn("verified_failure", item["success_criteria"])
                    self.assertTrue(item["capabilities"]["act"])
                    self.assertTrue(item["capabilities"]["verify_objective"])
                    continue
                self.assertEqual(item["evaluation"], "observation_only")
                self.assertFalse(item["verifier"]["available"])
                self.assertIn("NOT gameplay success", item["success_criteria"])
                self.assertFalse(item["capabilities"]["act"])
                self.assertFalse(item["capabilities"]["verify_objective"])

    def test_scenario_detail_is_versioned(self) -> None:
        scenario = resolve_scenario("observation-baseline", 1)
        assert scenario is not None
        self.assertEqual(scenario.key, "observation-baseline@1")
        self.assertIsNone(resolve_scenario("observation-baseline", 99))
        self.assertIsNone(resolve_scenario("no-such-scenario"))

    def test_metric_rounding_and_units_are_declared(self) -> None:
        for spec in scenario_payload()["metric_catalog"]:
            with self.subTest(metric=spec["key"]):
                self.assertTrue(spec["unit"])
                self.assertTrue(spec["method"])
                self.assertTrue(spec["numerator"])
                self.assertTrue(spec["denominator"])
                self.assertTrue(spec["rounding"])


class ScenarioConfigValidationTests(unittest.TestCase):
    def test_defaults_come_from_the_scenario(self) -> None:
        scenario, config, errors = config_from_request(
            {"scenario_id": "observation-baseline", "target_title": "Window"}
        )
        assert scenario is not None and config is not None
        self.assertEqual(errors, [])
        self.assertEqual(config.duration_seconds, scenario.default_duration_seconds)
        self.assertEqual(config.frame_interval_seconds, scenario.frame_interval_seconds)
        self.assertEqual(config.version, 1)

    def test_unknown_scenario_is_named(self) -> None:
        _, config, errors = config_from_request(
            {"scenario_id": "nope", "target_title": "Window"}
        )
        self.assertIsNone(config)
        self.assertIn("Unknown benchmark scenario", errors[0])

    def test_unknown_version_is_refused_not_substituted(self) -> None:
        _, config, errors = config_from_request(
            {"scenario_id": "observation-baseline", "version": 9, "target_title": "Window"}
        )
        self.assertIsNone(config)
        self.assertIn("no version", errors[0])
        self.assertIn("latest is 1", errors[0])

    def test_unsupported_scenario_is_refused_with_its_reason(self) -> None:
        _, config, errors = config_from_request(
            {"scenario_id": "gameplay-objective", "target_title": "Game"}
        )
        self.assertIsNone(config)
        self.assertTrue(any("decision pipeline" in item for item in errors))

    def test_model_or_policy_selection_is_refused(self) -> None:
        _, config, errors = config_from_request(
            {
                "scenario_id": "observation-baseline",
                "target_title": "Window",
                "model_id": "some-model",
            }
        )
        self.assertIsNone(config)
        self.assertIn("fabricate a comparison", errors[0])

    def test_budget_too_short_for_the_scenario_is_refused(self) -> None:
        _, config, errors = config_from_request(
            {
                "scenario_id": "capture-latency",
                "target_title": "Window",
                "duration_seconds": 2,
                "frame_interval_seconds": 0.25,
            }
        )
        self.assertIsNone(config)
        self.assertTrue(any("at least 20 frames" in item for item in errors))

    def test_blank_target_and_out_of_range_values_are_refused(self) -> None:
        _, config, errors = config_from_request(
            {
                "scenario_id": "observation-baseline",
                "target_title": "",
                "duration_seconds": 0,
                "frame_interval_seconds": 0.001,
            }
        )
        self.assertIsNone(config)
        self.assertEqual(len(errors), 3, errors)

    def test_booleans_and_fractional_seconds_are_refused_not_coerced(self) -> None:
        cases = [
            ({"duration_seconds": True}, "Duration"),
            ({"duration_seconds": 2.9}, "Duration"),
            ({"frame_interval_seconds": True}, "observation interval"),
            ({"frame_interval_seconds": "abc"}, "observation interval"),
        ]
        for overrides, label in cases:
            with self.subTest(overrides=overrides):
                _, config, errors = config_from_request(
                    {
                        "scenario_id": "observation-baseline",
                        "target_title": "Window",
                        # A one-second budget at 0.05 s still clears the frame floor,
                        # so the only possible refusal is the value itself.
                        "frame_interval_seconds": 0.05,
                        **overrides,
                    }
                )
                self.assertIsNone(config)
                self.assertTrue(
                    any(label.lower() in item.lower() for item in errors),
                    errors,
                )

    def test_a_boolean_version_is_not_a_version_number(self) -> None:
        self.assertIsNone(resolve_scenario("observation-baseline", True))
        _, config, errors = config_from_request(
            {"scenario_id": "observation-baseline", "version": True, "target_title": "Window"}
        )
        self.assertIsNone(config)
        self.assertTrue(any("no version" in item for item in errors))

    def test_notes_are_bounded(self) -> None:
        _, config, errors = config_from_request(
            {
                "scenario_id": "observation-baseline",
                "target_title": "Window",
                "notes": "x" * 501,
            }
        )
        self.assertIsNone(config)
        self.assertIn("Notes must be at most", errors[0])


class MetricCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = resolve_scenario("observation-baseline")
        assert self.scenario is not None

    def test_measured_and_derived_values(self) -> None:
        rows = collect_metrics(
            scenario=self.scenario,
            telemetry=_telemetry(),
            events=12,
            errors=1,
            interventions=0,
        )
        self.assertEqual(rows["frames"]["value"], 9)
        self.assertEqual(rows["frames"]["source"], "telemetry.capture.frames")
        # 5 active seconds at 0.5s = 11 opportunities; 9 / 11 = 0.818.
        self.assertAlmostEqual(rows["frame_yield"]["value"], 0.818, places=3)
        self.assertEqual(rows["timeline_events"]["value"], 12)
        self.assertEqual(rows["max_gap_ms"]["value"], 120.0)

    def test_missing_p95_stays_unavailable_with_its_reason(self) -> None:
        rows = collect_metrics(
            scenario=resolve_scenario("capture-latency"),
            telemetry=_telemetry(
                capture={
                    "frames": 9,
                    "stale_frames": 0,
                    "capture_failures": 0,
                    "frame_gaps": 0,
                    "max_gap_ms": 10.0,
                    "latency_ms": {
                        "samples": 9,
                        "median_ms": 8.0,
                        "p95_ms": None,
                        "p95_reason": "Only 9 latency sample(s) are available; a p95 needs at least 20.",
                        "max_ms": 12.0,
                    },
                }
            ),
            events=1,
            errors=0,
            interventions=0,
        )
        self.assertIsNone(rows["latency_p95_ms"]["value"])
        self.assertFalse(rows["latency_p95_ms"]["available"])
        self.assertIn("at least 20", rows["latency_p95_ms"]["reason"])

    def test_missing_frame_count_never_becomes_zero(self) -> None:
        rows = collect_metrics(
            scenario=self.scenario,
            telemetry={"session": {}, "capture": {}, "loop": {}, "target": {}},
            events=0,
            errors=0,
            interventions=0,
        )
        for key in ("frames", "frame_gaps", "max_gap_ms", "latency_median_ms"):
            with self.subTest(metric=key):
                self.assertIsNone(rows[key]["value"])
                self.assertFalse(rows[key]["available"])

    def test_every_unavailable_row_carries_a_reason(self) -> None:
        for scenario_id in (
            "observation-baseline",
            "capture-latency",
            "observation-continuity",
            "controlled-objective",
        ):
            scenario = resolve_scenario(scenario_id)
            assert scenario is not None
            rows = collect_metrics(
                scenario=scenario,
                telemetry={"session": {}, "capture": {}, "loop": {}, "target": {}},
                events=0,
                errors=0,
                interventions=0,
            )
            for key, row in rows.items():
                if row["available"]:
                    continue
                with self.subTest(scenario=scenario_id, metric=key):
                    self.assertTrue(row["reason"], f"{key} is unavailable with an empty reason")
                    self.assertIsNone(row["value"], f"{key} is unavailable but carries a value")

    def test_a_missing_median_does_not_borrow_the_p95_reason(self) -> None:
        rows = collect_metrics(
            scenario=resolve_scenario("capture-latency"),
            telemetry=_telemetry(
                capture={
                    "frames": 0,
                    "latency_ms": {
                        "samples": 0,
                        "median_ms": None,
                        "p95_ms": None,
                        "p95_reason": "Only 0 latency sample(s) are available; a p95 needs at least 20.",
                        "max_ms": None,
                    },
                }
            ),
            events=0,
            errors=0,
            interventions=0,
        )
        reason = rows["latency_median_ms"]["reason"]
        self.assertTrue(reason)
        self.assertNotIn("p95", reason)
        self.assertIn("latency", reason)

    def test_frame_yield_is_unavailable_without_active_time(self) -> None:
        rows = collect_metrics(
            scenario=self.scenario,
            telemetry=_telemetry(session={"duration_seconds": 0.0, "active_seconds": 0.0}),
            events=0,
            errors=0,
            interventions=0,
        )
        self.assertFalse(rows["frame_yield"]["available"])
        self.assertIn("denominator is undefined", rows["frame_yield"]["reason"])

    def test_an_undecided_objective_is_a_row_not_a_zero(self) -> None:
        rows = collect_metrics(
            scenario=resolve_scenario("gameplay-objective"),
            telemetry=_telemetry(),
            events=0,
            errors=0,
            interventions=0,
        )
        self.assertIsNone(rows["objective_success"]["value"])
        self.assertFalse(rows["objective_success"]["available"])
        self.assertIn("No decided objective verdict", rows["objective_success"]["reason"])
        self.assertIsNone(rows["completion_time_seconds"]["value"])
        self.assertFalse(rows["completion_time_seconds"]["available"])

    def test_a_decided_verdict_is_measured_and_a_failure_is_zero(self) -> None:
        scenario = resolve_scenario("controlled-objective")
        assert scenario is not None
        success = collect_metrics(
            scenario=scenario,
            telemetry=_telemetry(),
            events=0,
            errors=0,
            interventions=0,
            objective={
                "achieved": True,
                "completion_time_seconds": 1.25,
                "reason": "the window title reads READY",
            },
            actions=3,
        )
        self.assertTrue(success["objective_success"]["available"])
        self.assertEqual(success["objective_success"]["value"], 1.0)
        self.assertTrue(success["completion_time_seconds"]["available"])
        self.assertEqual(success["completion_time_seconds"]["value"], 1.25)
        self.assertTrue(success["actions_executed"]["available"])
        self.assertEqual(success["actions_executed"]["value"], 3.0)

        failure = collect_metrics(
            scenario=scenario,
            telemetry=_telemetry(),
            events=0,
            errors=0,
            interventions=0,
            objective={"achieved": False, "completion_time_seconds": 2.0, "reason": "no"},
        )
        self.assertTrue(failure["objective_success"]["available"])
        self.assertEqual(failure["objective_success"]["value"], 0.0, "a measured failure is 0, not unavailable")
        self.assertFalse(failure["actions_executed"]["available"], "an unconfigured action path is not zero")


class AggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = resolve_scenario("observation-baseline")
        assert self.scenario is not None

    def test_a_controlled_cohort_reports_the_verdict_it_recorded(self) -> None:
        """An aggregate must not claim "no verifier" over runs that had one."""
        controlled = resolve_scenario("controlled-objective")
        assert controlled is not None
        success = _stored_run("ctrl1", scenario_id="controlled-objective")
        success["objective"] = {"decided": True, "achieved": True}
        failure = _stored_run("ctrl2", scenario_id="controlled-objective")
        failure["objective"] = {"decided": True, "achieved": False}

        aggregate = aggregate_runs([success, failure], controlled)
        self.assertTrue(aggregate["objective_verified"])
        self.assertIn("decided verdict", aggregate["objective_note"])

        undecided = _stored_run("ctrl3", scenario_id="controlled-objective")
        undecided["objective"] = {"decided": False, "achieved": None}
        aggregate = aggregate_runs([undecided], controlled)
        self.assertFalse(aggregate["objective_verified"])

    def test_only_completed_valid_runs_enter_the_sample(self) -> None:
        runs = [
            _stored_run("aaaa1", metrics={"frames": _row(10, unit="frames")}),
            _stored_run("aaaa2", metrics={"frames": _row(12, unit="frames")}, interval=0.5),
            _stored_run("aaaa3", status="failed", valid=False, reason="capture crashed"),
            _stored_run("aaaa4", status="invalid", valid=False, reason="too few frames"),
            _stored_run("aaaa5", status="aborted", valid=False, reason="cancelled"),
        ]
        result = aggregate_runs(runs, self.scenario)
        self.assertEqual(result["attempts"], 5)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual(result["status_counts"]["completed"], 2)
        self.assertEqual(result["status_counts"]["failed"], 1)
        self.assertEqual(result["status_counts"]["invalid"], 1)
        self.assertEqual(result["status_counts"]["aborted"], 1)
        self.assertEqual(result["metrics"]["frames"]["value"], 11.0)
        self.assertIsNotNone(result["metrics"]["frames"]["stdev"])
        self.assertEqual(result["excluded_count"], 3)
        self.assertTrue(all(item["reason"] for item in result["excluded"]))

    def test_aggregate_pools_only_the_newest_configuration(self) -> None:
        runs = [
            _stored_run(
                "mix1",
                duration=10,
                metrics={"frames": _row(10, unit="frames")},
                created_at="2026-10-18T10:00:00Z",
            ),
            _stored_run(
                "mix2",
                duration=300,
                interval=1.0,
                metrics={"frames": _row(500, unit="frames")},
                created_at="2026-10-18T11:00:00Z",
            ),
        ]
        result = aggregate_runs(runs, self.scenario)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["sample_size"], 1, "different budgets cannot share a sample")
        self.assertTrue(result["mixed_configurations"])
        self.assertEqual(result["configuration_excluded_count"], 1)
        self.assertEqual(result["cohort"]["configuration"]["duration_seconds"], 300)
        self.assertEqual(result["metrics"]["frames"]["value"], 500.0)
        excluded = result["excluded"][0]
        self.assertEqual(excluded["excluded_by"], "configuration")
        self.assertIn("different configuration", excluded["reason"])

    def test_a_notes_difference_does_not_split_the_cohort(self) -> None:
        first = _stored_run("note1", metrics={"frames": _row(10, unit="frames")})
        second = _stored_run("note2", metrics={"frames": _row(12, unit="frames")})
        second["configuration"]["notes"] = "a different operator note"
        result = aggregate_runs([first, second], self.scenario)
        self.assertEqual(result["sample_size"], 2)
        self.assertFalse(result["mixed_configurations"])

    def test_the_cohort_ignores_window_identity_but_not_geometry(self) -> None:
        """A re-launched window is the same basis; a resized one is not.

        Comparison never treated handle/pid/bounds as a measurement basis, so
        aggregation must not either — otherwise a target that was re-launched
        between two runs was "excluded_by: configuration" from a cohort the
        comparison still called compatible. Width is geometry and still splits.
        """
        first = _stored_run(
            "ident1", created_at="2026-10-18T10:00:00Z", metrics={"frames": _row(10)}
        )
        second = _stored_run(
            "ident2", created_at="2026-10-18T11:00:00Z", metrics={"frames": _row(12)}
        )
        first["environment"]["window"].update(
            {"handle": 111, "pid": 42, "bounds": [0, 0, 1000, 600]}
        )
        second["environment"]["window"].update(
            {"handle": 222, "pid": 43, "bounds": [40, 40, 1040, 640]}
        )
        same_basis = aggregate_runs([first, second], self.scenario)
        self.assertEqual(same_basis["sample_size"], 2, same_basis["excluded"])
        self.assertFalse(same_basis["mixed_configurations"])
        self.assertTrue(compare_runs([first], [second])["compatible"])

        resized = _stored_run(
            "ident3", created_at="2026-10-18T12:00:00Z", width=1200, metrics={"frames": _row(14)}
        )
        different = aggregate_runs([first, resized], self.scenario)
        self.assertEqual(different["sample_size"], 1)
        self.assertTrue(different["mixed_configurations"])
        self.assertEqual(different["excluded"][0]["excluded_by"], "configuration")

    def test_single_sample_reports_no_variability_instead_of_zero(self) -> None:
        result = aggregate_runs([_stored_run("bbbb1")], self.scenario)
        self.assertEqual(result["sample_size"], 1)
        self.assertIsNone(result["metrics"]["frames"]["stdev"])
        self.assertIn("Only one eligible run", result["variability"])

    def test_no_eligible_runs_still_reports_attempts(self) -> None:
        result = aggregate_runs([_stored_run("cccc1", status="failed", valid=False)], self.scenario)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["sample_size"], 0)
        self.assertFalse(result["metrics"]["frames"]["available"])
        self.assertEqual(result["excluded_count"], 1)
        for key, row in result["metrics"].items():
            if not row["available"]:
                with self.subTest(metric=key):
                    self.assertTrue(row["reason"], "an unavailable aggregate row needs a reason")

    def test_metric_unavailable_in_some_runs_uses_the_available_samples(self) -> None:
        scenario = resolve_scenario("capture-latency")
        assert scenario is not None
        runs = [
            _stored_run(
                "dddd1",
                scenario_id="capture-latency",
                interval=0.25,
                duration=30,
                metrics={"latency_p95_ms": _row(20.0, unit="ms")},
            ),
            _stored_run(
                "dddd2",
                scenario_id="capture-latency",
                interval=0.25,
                duration=30,
                metrics={
                    "latency_p95_ms": _row(
                        None,
                        available=False,
                        reason="Only 9 samples; a p95 needs 20.",
                        unit="ms",
                    )
                },
            ),
        ]
        result = aggregate_runs(runs, scenario)
        self.assertEqual(result["metrics"]["latency_p95_ms"]["n"], 1)
        self.assertEqual(result["metrics"]["latency_p95_ms"]["value"], 20.0)
        self.assertIsNone(result["metrics"]["latency_p95_ms"]["stdev"])


class FairComparisonTests(unittest.TestCase):
    def test_identical_configuration_is_compatible_and_reports_deltas(self) -> None:
        left = [_stored_run("eeee1", metrics={"frames": _row(10, unit="frames")})]
        right = [_stored_run("eeee2", metrics={"frames": _row(14, unit="frames")})]
        result = compare_runs(left, right)
        self.assertTrue(result["compatible"], result["reasons"])
        frames = next(row for row in result["metrics"] if row["key"] == "frames")
        self.assertEqual(frames["delta"], 4.0)
        self.assertFalse(result["verdict"]["established"])
        self.assertIn("single run", result["verdict"]["reason"])

    def test_repeated_runs_expose_sample_count_and_variability(self) -> None:
        left = [
            _stored_run("ffff1", metrics={"frames": _row(10, unit="frames")}),
            _stored_run("ffff2", metrics={"frames": _row(12, unit="frames")}),
        ]
        right = [_stored_run("gggg1", metrics={"frames": _row(10, unit="frames")})]
        result = compare_runs(left, right)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["sample"], {"left": 2, "right": 1})
        frames = next(row for row in result["metrics"] if row["key"] == "frames")
        self.assertEqual(frames["left"]["value"], 11.0)
        self.assertEqual(frames["left"]["values"], [10.0, 12.0])
        self.assertIsNotNone(frames["left"]["stdev"])
        self.assertIsNone(frames["right"]["stdev"])

    def test_different_budget_is_refused(self) -> None:
        result = compare_runs(
            [_stored_run("hhhh1", duration=10)],
            [_stored_run("hhhh2", duration=30)],
        )
        self.assertFalse(result["compatible"])
        self.assertTrue(any("execution budget" in item for item in result["reasons"]))
        frames = next(row for row in result["metrics"] if row["key"] == "frames")
        self.assertIsNone(frames["delta"])
        self.assertIn("not compatible", frames["delta_reason"])

    def test_different_interval_scenario_and_version_are_refused(self) -> None:
        interval_mismatch = compare_runs(
            [_stored_run("iiii1", interval=0.5)],
            [_stored_run("iiii2", interval=0.25)],
        )
        self.assertFalse(interval_mismatch["compatible"])
        self.assertTrue(any("observation interval" in item for item in interval_mismatch["reasons"]))

        scenario_mismatch = compare_runs(
            [_stored_run("jjjj1")],
            [_stored_run("jjjj2", scenario_id="capture-latency", interval=0.5, duration=10)],
        )
        self.assertFalse(scenario_mismatch["compatible"])
        self.assertTrue(any("Different scenarios" in item for item in scenario_mismatch["reasons"]))

        newer = _stored_run("kkkk2")
        newer["scenario"]["version"] = 2
        newer["configuration"]["version"] = 2
        version_mismatch = compare_runs([_stored_run("kkkk1")], [newer])
        self.assertFalse(version_mismatch["compatible"])
        self.assertTrue(any("scenario versions" in item for item in version_mismatch["reasons"]))

    def test_different_window_or_geometry_is_refused(self) -> None:
        other_title = compare_runs(
            [_stored_run("llll1")],
            [_stored_run("llll2", title="Another window")],
        )
        self.assertFalse(other_title["compatible"])
        self.assertTrue(any("different windows" in item for item in other_title["reasons"]))

        other_geometry = compare_runs(
            [_stored_run("mmmm1")],
            [_stored_run("mmmm2", width=1920)],
        )
        self.assertFalse(other_geometry["compatible"])
        self.assertTrue(any("window geometry" in item for item in other_geometry["reasons"]))

    def test_a_side_mixing_window_geometry_is_refused(self) -> None:
        result = compare_runs(
            [
                _stored_run("mixgeo1", width=1000),
                _stored_run("mixgeo2", width=1920),
            ],
            [_stored_run("mixgeo3")],
        )
        self.assertFalse(result["compatible"])
        self.assertTrue(
            any("mixes different target geometry" in item for item in result["reasons"]),
            result["reasons"],
        )

    def test_a_failed_run_cannot_be_compared(self) -> None:
        result = compare_runs(
            [_stored_run("nnnn1")],
            [_stored_run("nnnn2", status="failed", valid=False)],
        )
        self.assertFalse(result["compatible"])
        self.assertTrue(any("only completed valid runs" in item for item in result["reasons"]))

    def test_self_comparison_is_refused(self) -> None:
        run = _stored_run("oooo1")
        with self.assertRaises(BenchmarkValidationError):
            compare_runs([run], [run])

    def test_a_run_counts_once_when_a_side_names_it_twice(self) -> None:
        """One measurement cannot become ``sample: 2`` with a ``0.0`` stdev."""
        repeated = _stored_run("qqqq1", metrics={"frames": _row(10)})
        other = _stored_run("qqqq2", metrics={"frames": _row(12)})
        result = compare_runs([repeated, repeated], [other])
        self.assertEqual(result["sample"], {"left": 1, "right": 1})
        self.assertEqual(result["left"]["run_ids"], ["qqqq1"])
        self.assertIsNone(result["left"]["metrics"]["frames"]["stdev"])

    def test_a_relaunched_window_is_not_a_mixed_geometry_side(self) -> None:
        first = _stored_run("rrrr1", metrics={"frames": _row(10)})
        second = _stored_run("rrrr2", metrics={"frames": _row(12)})
        first["environment"]["window"].update({"handle": 1, "pid": 10, "bounds": [0, 0, 1000, 600]})
        second["environment"]["window"].update({"handle": 2, "pid": 20, "bounds": [5, 5, 1005, 605]})
        result = compare_runs([first, second], [_stored_run("rrrr3")])
        self.assertTrue(result["compatible"], result["reasons"])
        self.assertEqual(result["sample"]["left"], 2)

    def test_missing_side_is_refused(self) -> None:
        with self.assertRaises(BenchmarkValidationError):
            compare_runs([], [_stored_run("pppp1")])


class BenchmarkRunnerTests(unittest.IsolatedAsyncioTestCase):
    """The real manager observes a disposable fake window; the lab composes it."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manager(self, **overrides: Any) -> NovaForgeManager:
        options: dict[str, Any] = {
            "data_dir": self.data_dir,
            "capture": self.desktop.capture,
            "geometry": self.desktop.geometry,
            "window_lines": self.desktop.window_lines,
            "release_inputs": self.desktop.release,
            "frame_interval": 0.05,
            "pause_timeout": 2.0,
            "stop_timeout": 4.0,
        }
        options.update(overrides)

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        options.setdefault("publish", publish)
        return NovaForgeManager(**options)

    def lab(self, manager: NovaForgeManager, **overrides: Any) -> NovaForgeBenchmarkLab:
        options: dict[str, Any] = {
            "manager": manager,
            "store": JsonStateStore(self.data_dir),
            "poll_interval": 0.02,
        }
        options.update(overrides)
        return NovaForgeBenchmarkLab(**options)

    def _request(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "scenario_id": "observation-baseline",
            "target_title": "NC FORGE TEST",
            "duration_seconds": 1,
            "frame_interval_seconds": 0.05,
        }
        payload.update(overrides)
        return payload

    async def _wait_run(
        self, lab: NovaForgeBenchmarkLab, run_id: str, timeout: float = 12.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = lab.get(run_id)
            if current and current["status"] != "running":
                return current
            await asyncio.sleep(0.02)
        raise AssertionError(f"run did not finish: {lab.get(run_id)}")

    async def _run_to_end(self, lab: NovaForgeBenchmarkLab, request: dict[str, Any], timeout: float = 12.0):
        run = await lab.start(request)
        return await self._wait_run(lab, run["id"], timeout)

    async def _session_id(
        self, lab: NovaForgeBenchmarkLab, run: dict[str, Any], timeout: float = 3.0
    ) -> str:
        """The session id the background runner assigned to the run."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = lab.get(run["id"]) or {}
            if current.get("session_id"):
                return str(current["session_id"])
            if current.get("status") not in (None, "running"):
                raise AssertionError(f"run ended before a session started: {current.get('reason')}")
            await asyncio.sleep(0.01)
        raise AssertionError("the runner did not start a session in time")

    async def test_a_real_run_records_metrics_evidence_and_no_objective(self) -> None:
        lab = self.lab(self.manager())
        run = await self._run_to_end(lab, self._request())

        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertTrue(run["valid"])
        self.assertEqual(run["scenario"]["scenario_id"], "observation-baseline")
        self.assertEqual(run["configuration"]["duration_seconds"], 1)
        self.assertEqual(run["configuration"]["frame_interval_seconds"], 0.05)
        self.assertGreater(run["metrics"]["frames"]["value"], 0)
        self.assertGreater(run["duration_seconds"], 0)
        self.assertTrue(run["evidence"]["frames"])
        self.assertTrue(run["evidence"]["timeline_endpoint"].startswith("/novaforge/sessions/"))
        self.assertEqual(run["evidence"]["gaps"], [])
        self.assertIsNone(run["objective"]["achieved"])
        self.assertFalse(run["objective"]["configured"])
        self.assertFalse(run["objective"]["available"])
        self.assertFalse(run["objective"]["decided"])
        self.assertEqual(run["objective"]["outcome"], "unsupported")
        self.assertIn("No objective was configured", run["objective"]["reason"])
        self.assertTrue(run["objective"]["data_complete"])
        self.assertFalse(run["model"]["available"])
        self.assertIsNone(run["model"]["id"])
        self.assertIn("No model is invoked", run["model"]["reason"])

        history = lab.history()
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["active_run_id"], None)
        self.assertEqual(history["runs"][0]["status"], "completed")

        aggregate = lab.aggregate("observation-baseline")
        self.assertEqual(aggregate["sample_size"], 1)
        self.assertFalse(aggregate["objective_verified"])
        self.assertIsNone(aggregate["metrics"]["frames"]["stdev"])

        with self.assertRaises(BenchmarkValidationError):
            lab.compare([run["id"]], [run["id"]])

    async def test_a_fast_verified_success_is_completed_not_invalid(self) -> None:
        """A decided verdict ends the session before the observation floor.

        The manager closes a session the moment an objective is verified, so a
        fast success records fewer frames than the scenario's floor. The floor
        used to mark that run INVALID — the one run that answered the question
        — although the verdict, not the frame count, is what the run measured.
        The floor still binds an undecided run, and a decided run still needs one
        fresh frame: a verdict with nothing observed is not a measurement.
        """

        async def probe(_handle: int) -> Any:
            return self.desktop.window

        manager = self.manager(objective_probe=probe)
        lab = self.lab(manager)
        run = await lab.start(
            {
                "scenario_id": "controlled-objective",
                "target_title": "NC FORGE TEST",
                "duration_seconds": 10,
                "frame_interval_seconds": 0.5,
                "max_actions": 1,
            }
        )
        session_id = await self._session_id(lab, run)
        await _wait_for(
            lambda: bool(manager.get(session_id) and manager.get(session_id).frames),
            message="the first fresh frame",
        )
        manager.set_objective(
            session_id, {"kind": "window_title", "parameter": "READY"}
        )
        # The target retitles itself: the same handle and process, a new label.
        # That title change IS the objective being met.
        window = self.desktop.window
        assert window is not None
        window.title = "NC FORGE TEST - READY"
        verdict = await manager.verify_objective(session_id)
        self.assertEqual(verdict["outcome"], "verified_success", verdict)

        finished = await self._wait_run(lab, run["id"])
        scenario = resolve_scenario("controlled-objective")
        assert scenario is not None
        frames = finished["metrics"]["frames"]["value"]
        self.assertLess(frames, scenario.min_frames, "the run must end below the floor")
        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertTrue(finished["valid"], finished["reason"])
        self.assertTrue(finished["objective"]["configured"])
        self.assertTrue(finished["objective"]["decided"])
        self.assertEqual(finished["objective"]["outcome"], "verified_success")
        self.assertTrue(finished["objective"]["achieved"])
        self.assertEqual(finished["metrics"]["objective_success"]["value"], 1.0)
        self.assertTrue(finished["metrics"]["completion_time_seconds"]["available"])
        self.assertIn("decided the objective", finished["reason"])

    async def test_a_decided_failure_is_completed_but_not_a_success(self) -> None:
        """A verifier that says NO is a decided result, not an invalid run."""

        async def probe(_handle: int) -> Any:
            return self.desktop.window

        manager = self.manager(objective_probe=probe)
        lab = self.lab(manager)
        run = await lab.start(
            {
                "scenario_id": "controlled-objective",
                "target_title": "NC FORGE TEST",
                "duration_seconds": 10,
                "frame_interval_seconds": 0.5,
                "max_actions": 1,
            }
        )
        session_id = await self._session_id(lab, run)
        await _wait_for(
            lambda: bool(manager.get(session_id) and manager.get(session_id).frames),
            message="the first fresh frame",
        )
        manager.set_objective(
            session_id, {"kind": "window_title", "parameter": "NEVER APPEARS"}
        )
        verdict = await manager.verify_objective(session_id)
        self.assertEqual(verdict["outcome"], "verified_failure", verdict)

        finished = await self._wait_run(lab, run["id"])
        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertTrue(finished["valid"], finished["reason"])
        self.assertTrue(finished["objective"]["decided"])
        self.assertFalse(finished["objective"]["achieved"])
        self.assertEqual(finished["metrics"]["objective_success"]["value"], 0.0)

    async def test_a_missing_duration_is_unavailable_not_zero(self) -> None:
        """The record's duration must agree with the metric row that says it is absent."""
        manager = self.manager()
        lab = self.lab(manager)
        # NovaForgeManager is slotted, so the seam is patched on the class.
        original = NovaForgeManager.telemetry_payload

        def without_duration(
            this: NovaForgeManager, session_id: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            payload = original(this, session_id, *args, **kwargs)
            payload["telemetry"]["session"]["duration_seconds"] = None
            return payload

        with mock.patch.object(NovaForgeManager, "telemetry_payload", without_duration):
            run = await self._run_to_end(lab, self._request())

        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertIsNone(run["duration_seconds"])
        self.assertFalse(run["metrics"]["duration_seconds"]["available"])
        self.assertIn("duration", run["metrics"]["duration_seconds"]["reason"])
        self.assertIsNone(lab.history()["runs"][0]["duration_seconds"])

    async def test_a_store_that_cannot_be_read_back_does_not_fail_the_run(self) -> None:
        """Recording cannot raise: an unreadable store is REPORTED, not propagated."""
        class UnreadableStore(JsonStateStore):
            def read(self, name: str) -> dict[str, Any]:
                raise OSError("the snapshot cannot be read")

        lab = self.lab(self.manager(), store=UnreadableStore(self.data_dir))
        run = await self._run_to_end(lab, self._request())
        self.assertEqual(run["status"], "completed", run["reason"])
        self.assertEqual(run["persistence"]["state"], "failed")
        self.assertIn("could not be read back", run["persistence"]["reason"])

    async def test_a_corrupt_stored_version_cannot_break_the_aggregate(self) -> None:
        """A hand-edited snapshot is preserved as invalid, never a 500 from a read."""
        store = JsonStateStore(self.data_dir)
        scenario = resolve_scenario("observation-baseline")
        assert scenario is not None
        snapshot = scenario.snapshot()
        snapshot["version"] = "one"
        store.write(
            RUNS_NAMESPACE,
            {
                "version": BENCHMARK_VERSION,
                "runs": [
                    {
                        "version": BENCHMARK_VERSION,
                        "id": "corrupt-version",
                        "scenario": snapshot,
                        "configuration": {
                            "scenario_id": "observation-baseline",
                            "version": "one",
                            "target_title": "Window",
                            "duration_seconds": 10,
                            "frame_interval_seconds": 0.5,
                        },
                        "status": "completed",
                        "valid": True,
                        "created_at": "2026-10-18T09:00:00Z",
                        "metrics": {},
                    }
                ],
            },
        )
        lab = self.lab(self.manager())
        preserved = lab.get("corrupt-version")
        assert preserved is not None
        self.assertEqual(preserved["status"], "invalid")
        self.assertFalse(preserved["valid"])
        self.assertIn("version is not an integer", preserved["reason"])
        self.assertEqual(lab.history()["count"], 1, "the attempt is still visible")
        aggregate = lab.aggregate("observation-baseline")
        self.assertEqual(aggregate["attempts"], 0)
        self.assertEqual(aggregate["sample_size"], 0)

    async def test_the_run_is_persisted_and_restored(self) -> None:
        manager = self.manager()
        lab = self.lab(manager)
        run = await self._run_to_end(lab, self._request())

        stored = JsonStateStore(self.data_dir).read(RUNS_NAMESPACE)
        self.assertEqual(stored["version"], BENCHMARK_VERSION)
        self.assertEqual(len(stored["runs"]), 1)
        self.assertEqual(stored["runs"][0]["id"], run["id"])

        self.assertEqual(run["persistence"]["state"], "verified")

        restored = self.lab(self.manager())
        restored_run = restored.get(run["id"])
        assert restored_run is not None
        self.assertEqual(restored_run["status"], "completed")
        self.assertEqual(restored_run["metrics"]["frames"]["value"], run["metrics"]["frames"]["value"])

    async def test_a_store_that_drops_the_snapshot_is_reported_not_hidden(self) -> None:
        class DroppingStore(JsonStateStore):
            def write(self, name: str, payload: dict[str, Any]) -> None:
                return None

        lab = self.lab(self.manager(), store=DroppingStore(self.data_dir))
        run = await self._run_to_end(lab, self._request())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["persistence"]["state"], "failed")
        self.assertIn("not readable back", run["persistence"]["reason"])

    async def test_a_run_left_running_by_a_restart_is_recovered_as_aborted(self) -> None:
        store = JsonStateStore(self.data_dir)
        scenario = resolve_scenario("observation-baseline")
        assert scenario is not None
        store.write(
            RUNS_NAMESPACE,
            {
                "version": BENCHMARK_VERSION,
                "runs": [
                    {
                        "version": BENCHMARK_VERSION,
                        "id": "interrupted-run",
                        "scenario": scenario.to_dict(),
                        "configuration": {
                            "scenario_id": "observation-baseline",
                            "version": 1,
                            "target_title": "Window",
                            "duration_seconds": 10,
                            "frame_interval_seconds": 0.5,
                        },
                        "status": "running",
                        "valid": False,
                        "reason": "",
                        "session_id": "gone-session",
                        "created_at": "2026-10-18T09:00:00Z",
                        "started_at": "2026-10-18T09:00:00Z",
                        "ended_at": "",
                        "metrics": {},
                    }
                ],
            },
        )
        lab = self.lab(self.manager())
        recovered = lab.get("interrupted-run")
        assert recovered is not None
        self.assertEqual(recovered["status"], "aborted")
        self.assertFalse(recovered["valid"])
        self.assertTrue(recovered["interrupted"])
        self.assertIn("restarted", recovered["reason"])
        self.assertIsNone(lab.active_run())

    async def test_cancellation_aborts_the_run_and_stops_the_session(self) -> None:
        manager = self.manager()
        lab = self.lab(manager)
        run = await lab.start(self._request(duration_seconds=10, frame_interval_seconds=0.1))
        session_id = await self._session_id(lab, run)
        await _wait_for(
            lambda: len(manager.get(session_id).frames) >= 2,
            message="two frames before cancelling",
        )
        result = await lab.cancel(run["id"])
        self.assertFalse(result["already_stopped"])
        finished = result["run"]
        self.assertEqual(finished["status"], "aborted")
        self.assertFalse(finished["valid"])
        self.assertIn("Cancelled by the operator", finished["reason"])
        record = manager.get(finished["session_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIn(record.state.value, {"STOPPED"})

        second = await lab.cancel(run["id"])
        self.assertTrue(second["already_stopped"])

    async def test_duplicate_start_and_active_session_are_conflicts(self) -> None:
        manager = self.manager()
        lab = self.lab(manager)
        run = await lab.start(self._request(duration_seconds=10, frame_interval_seconds=0.1))
        with self.assertRaises(BenchmarkConflictError):
            await lab.start(self._request())
        await lab.cancel(run["id"])

        session = await manager.start(_mission())
        try:
            with self.assertRaises(BenchmarkConflictError):
                await lab.start(self._request())
        finally:
            await manager.stop(session["session_id"])

    async def test_invalid_target_creates_no_run(self) -> None:
        manager = self.manager()
        lab = self.lab(manager)
        preflight = await lab.preflight(self._request(target_title="No Such Window"))
        self.assertFalse(preflight["valid"])
        self.assertTrue(any("No open window matches" in item for item in preflight["errors"]))
        with self.assertRaises(BenchmarkValidationError):
            await lab.start(self._request(target_title="No Such Window"))
        self.assertEqual(lab.history()["count"], 0)

    async def test_unsupported_scenario_is_refused_before_starting_anything(self) -> None:
        lab = self.lab(self.manager())
        preflight = await lab.preflight(
            {"scenario_id": "gameplay-objective", "target_title": "NC FORGE TEST"}
        )
        self.assertFalse(preflight["valid"])
        self.assertFalse(self.desktop.captures)
        with self.assertRaises(BenchmarkValidationError):
            await lab.start({"scenario_id": "gameplay-objective", "target_title": "NC FORGE TEST"})
        self.assertEqual(lab.history()["count"], 0)

    async def test_unknown_scenario_and_run_ids_are_not_found(self) -> None:
        lab = self.lab(self.manager())
        with self.assertRaises(BenchmarkNotFoundError):
            lab.scenario("nope")
        with self.assertRaises(BenchmarkNotFoundError):
            lab.aggregate("nope")
        with self.assertRaises(BenchmarkNotFoundError):
            await lab.cancel("nope")
        with self.assertRaises(BenchmarkNotFoundError):
            lab.compare(["nope"], ["also-nope"])

    async def test_shutdown_finalizes_an_active_run_before_the_manager(self) -> None:
        manager = self.manager()
        lab = self.lab(manager)
        run = await lab.start(self._request(duration_seconds=10, frame_interval_seconds=0.1))
        session_id = await self._session_id(lab, run)
        await _wait_for(
            lambda: len(manager.get(session_id).frames) > 0,
            message="first frame before shutdown",
        )
        await lab.shutdown()
        stored = lab.get(run["id"])
        assert stored is not None
        self.assertEqual(stored["status"], "aborted")
        self.assertFalse(stored["valid"])
        self.assertIn("shut down", stored["reason"])

    async def test_per_session_interval_is_honoured(self) -> None:
        manager = self.manager(frame_interval=1.0)
        session = await manager.start(_mission(frame_interval_seconds=0.05))
        session_id = session["session_id"]
        await _wait_for(
            lambda: len(manager.get(session_id).frames) >= 2,
            message="two frames at the session interval",
        )
        record = manager.get(session_id)
        assert record is not None
        self.assertEqual(record.telemetry.frame_interval_ms, 50.0)
        await manager.stop(session_id)

    async def test_mission_validation_rejects_an_out_of_range_interval(self) -> None:
        mission = MissionConfig("n", "o", "t", frame_interval_seconds=0.001)
        self.assertTrue(mission.errors())
        valid = MissionConfig("n", "o", "t", frame_interval_seconds=0.25)
        self.assertEqual(valid.errors(), [])


class BenchmarkEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_started_and_finished_events_carry_the_run_and_session(self) -> None:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        manager = NovaForgeManager(
            data_dir=self.data_dir,
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            frame_interval=0.05,
            stop_timeout=4.0,
        )
        lab = NovaForgeBenchmarkLab(manager=manager, publish=publish, poll_interval=0.02)
        run = await lab.start(
            {
                "scenario_id": "observation-baseline",
                "target_title": "NC FORGE TEST",
                "duration_seconds": 1,
                "frame_interval_seconds": 0.05,
            }
        )
        await _wait_for(
            lambda: lab.get(run["id"])["status"] != "running",
            message="run terminal",
            timeout=12.0,
        )
        types = [item[0] for item in self.events]
        self.assertIn("novaforge.benchmark.started", types)
        self.assertIn("novaforge.benchmark.finished", types)
        finished = next(payload for name, payload in self.events if name.endswith("finished"))
        self.assertEqual(finished["benchmark_run_id"], run["id"])
        self.assertTrue(finished["session_id"])
        self.assertEqual(finished["status"], "completed")
