"""NovaForge NF-05 tests: compatibility, readiness, lifecycle, baselines, reports.

The evaluation lab composes the REAL NF-01 session manager and the REAL NF-04
benchmark lab over a deterministic desktop double. No game is launched, no
gameplay input exists to send, and no objective verifier exists — the tests pin
those absences instead of pretending otherwise.

Two properties are pinned repeatedly on purpose:

* a compatibility or readiness refusal is retained and explained, never hidden;
* a completed evaluation is data completeness, never an achieved objective.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import novacontrol.novaforge.games as games_module
from novacontrol.novaforge import (
    EVALUATIONS_NAMESPACE,
    EvaluationConflictError,
    EvaluationError,
    GameNotFoundError,
    NovaForgeBenchmarkLab,
    NovaForgeEvaluationLab,
    NovaForgeManager,
    game_catalog,
    missing_evidence,
    readiness_checks,
    readiness_summary,
    resolve_game,
)
from novacontrol.novaforge.benchmark import BenchmarkNotFoundError, resolve_scenario
from novacontrol.novaforge.games import SCENARIOS, evaluation_limitations
from novacontrol.persistence.json_store import JsonStateStore

from test_novaforge import FakeDesktop


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "game_id": "disposable-window",
        "scenario_id": "observation-baseline",
        "target_title": "NC FORGE TEST",
        "duration_seconds": 1,
        "frame_interval_seconds": 0.05,
        "confirm": True,
    }
    payload.update(overrides)
    return payload


def _checks(**overrides: Any) -> dict[str, Any]:
    scenario = resolve_scenario("observation-baseline")
    game = resolve_game("disposable-window")
    assert scenario is not None and game is not None
    options: dict[str, Any] = {
        "game": game,
        "mode": "observation_only",
        "scenario": scenario,
        "target": {"title": "NC FORGE TEST", "handle": 0x1234, "pid": 4242},
        "benchmark_valid": True,
        "benchmark_errors": [],
        "benchmark_warnings": ["NF-04 invokes no model or policy."],
        "capture_supported": True,
        "stop_available": True,
        "cleanup_available": True,
        "store_available": True,
        "confirm_offline": False,
        "conflicts": [],
        "expected_handle": None,
        "expected_pid": None,
    }
    options.update(overrides)
    return readiness_summary(readiness_checks(**options))


class GameRegistryTests(unittest.TestCase):
    def test_the_catalog_is_honest_about_control_and_launch(self) -> None:
        catalog = game_catalog()
        self.assertEqual(catalog["controllable_count"], 0)
        self.assertEqual(catalog["observation_only_count"], 2)
        self.assertEqual(catalog["runnable_count"], 2)
        self.assertIn("never that a game objective was achieved", catalog["note"])
        for record in catalog["games"]:
            with self.subTest(game=record["game_id"]):
                self.assertFalse(record["launch"]["available"])
                self.assertFalse(record["objective_verification"]["available"])
                self.assertTrue(record["objective_verification"]["reason"])
                self.assertNotIn("control", record["evaluation_modes"])

    def test_every_registry_record_is_internally_consistent(self) -> None:
        for record in SCENARIOS:
            with self.subTest(game=record.game_id):
                self.assertEqual(record.validate(), [])

    def test_every_scenario_the_registry_declares_is_runnable(self) -> None:
        for record in SCENARIOS:
            for scenario_id in record.benchmark.get("scenarios") or ():
                scenario = resolve_scenario(str(scenario_id))
                with self.subTest(game=record.game_id, scenario=scenario_id):
                    self.assertIsNotNone(scenario)
                    assert scenario is not None
                    self.assertTrue(scenario.supported)

    def test_observation_only_cannot_declare_control(self) -> None:
        game = resolve_game("disposable-window")
        assert game is not None
        broken = replace(game, evaluation_modes=("observation_only", "control"))
        self.assertTrue(any("cannot declare the control mode" in item for item in broken.validate()))

    def test_a_controllable_claim_without_input_or_verifier_is_refused(self) -> None:
        game = resolve_game("disposable-window")
        assert game is not None
        broken = replace(game, status="SUPPORTED", evaluation_modes=("control",))
        problems = broken.validate()
        self.assertTrue(any("requires an available input" in item for item in problems))
        self.assertTrue(any("requires an objective verifier" in item for item in problems))

    def test_a_launch_claim_is_refused(self) -> None:
        game = resolve_game("disposable-window")
        assert game is not None
        broken = replace(
            game,
            launch={"available": True, "state": "available", "reason": "", "detail": {}},
        )
        self.assertTrue(any("never launches a target" in item for item in broken.validate()))

    def test_unsupported_records_must_state_a_reason(self) -> None:
        game = resolve_game("gameplay-objective")
        assert game is not None
        broken = replace(game, status_reason=" ")
        self.assertTrue(any("must state its reason" in item for item in broken.validate()))

    def test_unknown_ids_and_versions_are_not_guessed(self) -> None:
        self.assertIsNone(resolve_game("nope"))
        self.assertIsNone(resolve_game("disposable-window", 99))
        self.assertIsNone(resolve_game("disposable-window", True))


class ReadinessTests(unittest.TestCase):
    def test_a_complete_observation_only_path_is_ready(self) -> None:
        summary = _checks()
        self.assertTrue(summary["ready"], summary["blocking"])
        self.assertEqual(summary["blocking"], [])
        verifier = next(item for item in summary["checks"] if item["id"] == "objective_verification")
        self.assertEqual(verifier["status"], "unavailable")
        self.assertFalse(verifier["required"])

    def test_the_readiness_check_set_is_the_declared_fourteen(self) -> None:
        """The report names fourteen checks; this is where that stays true.

        The order is the order the panel renders, and the counts are the verdict
        a reader quotes, so neither an added nor a dropped check may pass unseen.
        """
        summary = _checks()
        self.assertEqual(
            [item["id"] for item in summary["checks"]],
            [
                "compatibility",
                "evaluation_mode",
                "target_identity",
                "target_uniqueness",
                "capture",
                "observation_source",
                "objective_verification",
                "input_capability",
                "guardian_controls",
                "input_cleanup",
                "benchmark_configuration",
                "evidence_persistence",
                "offline_eligibility",
                "conflicts",
            ],
        )
        self.assertEqual(sum(summary["counts"].values()), 14)
        self.assertEqual(summary["counts"]["pass"], 13)
        self.assertEqual(summary["counts"]["fail"], 0)
        self.assertEqual(summary["counts"]["unavailable"], 1)

    def test_an_unidentified_target_blocks(self) -> None:
        summary = _checks(target={"title": "NC FORGE TEST"})
        self.assertFalse(summary["ready"])
        self.assertIn("target_identity", summary["blocking"])

    def test_a_changed_pinned_identity_blocks(self) -> None:
        summary = _checks(expected_handle=0x9999)
        self.assertFalse(summary["ready"])
        identity = next(item for item in summary["checks"] if item["id"] == "target_identity")
        self.assertEqual(identity["status"], "fail")
        self.assertIn("changed since it was validated", identity["reason"])

    def test_a_duplicate_title_blocks(self) -> None:
        summary = _checks(
            benchmark_valid=False,
            benchmark_errors=["2 windows match 'NC FORGE TEST'; use a more specific title"],
        )
        self.assertFalse(summary["ready"])
        self.assertIn("target_uniqueness", summary["blocking"])
        self.assertIn("benchmark_configuration", summary["blocking"])

    def test_control_mode_is_refused_with_the_missing_capabilities(self) -> None:
        summary = _checks(mode="control")
        self.assertFalse(summary["ready"])
        self.assertIn("evaluation_mode", summary["blocking"])
        self.assertIn("input_capability", summary["blocking"])
        mode = next(item for item in summary["checks"] if item["id"] == "evaluation_mode")
        self.assertIn("Only observation_only is available", mode["reason"])

    def test_offline_confirmation_is_required_for_a_game_window(self) -> None:
        game = resolve_game("offline-game-window")
        assert game is not None
        refused = _checks(game=game)
        self.assertFalse(refused["ready"])
        self.assertIn("offline_eligibility", refused["blocking"])
        confirmed = _checks(game=game, confirm_offline=True)
        self.assertTrue(confirmed["ready"], confirmed["blocking"])

    def test_missing_capture_or_persistence_blocks(self) -> None:
        capture = _checks(capture_supported=False)
        self.assertIn("capture", capture["blocking"])
        store = _checks(store_available=False)
        self.assertIn("evidence_persistence", store["blocking"])
        controls = _checks(stop_available=False)
        self.assertIn("guardian_controls", controls["blocking"])

    def test_a_conflict_blocks_and_names_it(self) -> None:
        summary = _checks(conflicts=["Benchmark run abc is still active."])
        self.assertFalse(summary["ready"])
        self.assertIn("conflicts", summary["blocking"])

    def test_an_unavailable_verifier_never_makes_a_run_successful(self) -> None:
        summary = _checks()
        verifier = next(item for item in summary["checks"] if item["id"] == "objective_verification")
        self.assertIn("No objective verifier", verifier["reason"])
        self.assertTrue(summary["ready"], "an unavailable verifier is not a required failure")


class _EvaluationFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.desktop = FakeDesktop()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.store = JsonStateStore(self.data_dir)

        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        self.manager = NovaForgeManager(
            data_dir=self.data_dir,
            capture=self.desktop.capture,
            geometry=self.desktop.geometry,
            window_lines=self.desktop.window_lines,
            release_inputs=self.desktop.release,
            capture_supported=True,
            frame_interval=0.05,
            pause_timeout=0.5,
            stop_timeout=4.0,
            publish=publish,
        )
        self.benchmark = NovaForgeBenchmarkLab(
            manager=self.manager, store=self.store, poll_interval=0.02
        )
        self.lab = NovaForgeEvaluationLab(
            benchmark=self.benchmark, store=self.store, publish=publish
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _run_to_end(self, request: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        record = await self.lab.start(request)
        return await self._wait_terminal(record["id"], timeout)

    async def _wait_terminal(self, evaluation_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.lab.get(evaluation_id)
            assert current is not None
            if current["status"] != "running":
                return current
            await asyncio.sleep(0.02)
        raise AssertionError(f"evaluation did not finish: {self.lab.get(evaluation_id)}")


class EvaluationPreflightTests(_EvaluationFixture):
    async def test_preflight_measures_identity_and_starts_nothing(self) -> None:
        result = await self.lab.preflight(_request())
        self.assertTrue(result["ready"], result["blocking"])
        self.assertEqual(result["target"]["width"], 1000)
        self.assertEqual(result["identity"]["handle"], self.desktop.window.handle)
        self.assertEqual(result["identity"]["pid"], self.desktop.window.pid)
        self.assertFalse(result["baseline"]["available"])
        self.assertIn("first completed evaluation", result["baseline"]["reason"])
        self.assertIsNone(self.manager.active())
        self.assertEqual(self.benchmark.history()["count"], 0)
        self.assertEqual(self.lab.history()["count"], 0)

    async def test_an_unknown_game_is_reported_not_raised(self) -> None:
        result = await self.lab.preflight(_request(game_id="no-such-game"))
        self.assertFalse(result["ready"])
        self.assertTrue(any("Unknown game" in item for item in result["errors"]))
        self.assertEqual(result["checks"], [])

    async def test_a_scenario_the_environment_does_not_declare_is_refused(self) -> None:
        game = resolve_game("disposable-window")
        assert game is not None
        self.assertFalse(game.supports_scenario("gameplay-objective"))
        result = await self.lab.preflight(_request(scenario_id="gameplay-objective"))
        self.assertFalse(result["ready"])
        self.assertTrue(any("does not declare" in item for item in result["errors"]))

    async def test_an_offline_requirement_changes_readiness(self) -> None:
        unconfirmed = await self.lab.preflight(_request(game_id="offline-game-window"))
        self.assertFalse(unconfirmed["ready"])
        self.assertIn("offline_eligibility", unconfirmed["blocking"])
        confirmed = await self.lab.preflight(
            _request(game_id="offline-game-window", confirm_offline=True)
        )
        self.assertTrue(confirmed["ready"], confirmed["blocking"])

    async def test_a_pinned_identity_that_changed_fails_preflight(self) -> None:
        first = await self.lab.preflight(_request())
        pinned = first["identity"]
        self.desktop.window.handle = 0x9999
        self.desktop.window.pid = 777
        result = await self.lab.preflight(
            _request(expected_handle=pinned["handle"], expected_pid=pinned["pid"])
        )
        self.assertFalse(result["ready"])
        self.assertIn("target_identity", result["blocking"])


class EvaluationLifecycleTests(_EvaluationFixture):
    async def test_a_completed_evaluation_is_data_completeness_only(self) -> None:
        finished = await self._run_to_end(_request())
        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertTrue(finished["valid"])
        self.assertEqual(finished["mode"], "observation_only")
        self.assertEqual(finished["game"]["status"], "OBSERVATION_ONLY")
        self.assertFalse(finished["objective"]["available"])
        self.assertFalse(finished["objective"]["verified"])
        self.assertIsNone(finished["objective"]["achieved"])
        self.assertTrue(finished["objective"]["data_complete"])
        self.assertIn("never that the game objective was achieved", finished["objective"]["reason"])
        self.assertTrue(finished["benchmark_run_id"])
        self.assertTrue(finished["session_id"])
        self.assertTrue(finished["evidence"]["timeline"].startswith("/novaforge/sessions/"))
        self.assertEqual(finished["baseline"]["role"], "baseline")
        self.assertIsNone(finished["comparison"])
        self.assertEqual(finished["persistence"]["state"], "verified")

        run = self.benchmark.get(finished["benchmark_run_id"])
        assert run is not None
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["session_id"], finished["session_id"])
        self.assertEqual(self.manager.get(finished["session_id"]).actions_executed, 0)

    async def test_the_first_evaluation_becomes_the_baseline(self) -> None:
        first = await self._run_to_end(_request())
        second = await self._run_to_end(_request())
        self.assertEqual(second["baseline"]["role"], "candidate")
        self.assertEqual(second["baseline"]["evaluation_id"], first["id"])
        comparison = second["comparison"]
        self.assertIsNotNone(comparison)
        assert comparison is not None
        self.assertTrue(comparison["available"])
        self.assertTrue(comparison["compatible"], comparison["reasons"])
        self.assertFalse(comparison["verdict"]["established"])
        self.assertIn("no model", comparison["verdict"]["reason"].lower())

        baseline = self.lab.baseline("disposable-window", "observation-baseline")
        self.assertTrue(baseline["available"])
        self.assertEqual(baseline["evaluation_id"], first["id"])
        self.assertEqual(baseline["samples"], 2)

    async def test_an_explicit_baseline_must_be_a_completed_same_scenario_run(self) -> None:
        first = await self._run_to_end(_request())
        count = self.lab.history()["count"]
        with self.assertRaises(EvaluationError):
            await self.lab.start(_request(baseline_id="no-such-evaluation"))
        self.assertEqual(self.lab.history()["count"], count, "a bad baseline creates no attempt")

        record = await self.lab.start(_request(baseline_id=first["id"]))
        self.assertTrue(record["baseline"]["explicit"])
        self.assertEqual(record["baseline"]["evaluation_id"], first["id"])
        await asyncio.sleep(0.01)

    async def test_start_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(EvaluationError):
            await self.lab.start(_request(confirm=False))
        self.assertEqual(self.lab.history()["count"], 0)
        self.assertIsNone(self.manager.active())

    async def test_a_second_start_is_a_conflict_and_is_retained(self) -> None:
        running = await self.lab.start(_request(duration_seconds=10, frame_interval_seconds=0.1))
        with self.assertRaises(EvaluationConflictError):
            await self.lab.start(_request())
        history = self.lab.history()
        self.assertEqual(history["count"], 2)
        refused = next(row for row in history["evaluations"] if row["id"] != running["id"])
        self.assertEqual(refused["status"], "invalid")
        self.assertIn("still active", refused["reason"])
        await self.lab.cancel(running["id"])

    async def test_cancellation_is_idempotent(self) -> None:
        running = await self.lab.start(_request(duration_seconds=10, frame_interval_seconds=0.1))
        first = await self.lab.cancel(running["id"])
        self.assertFalse(first["already_stopped"])
        self.assertEqual(first["evaluation"]["status"], "aborted")
        self.assertFalse(first["evaluation"]["valid"])
        self.assertIn("Cancelled by the operator", first["evaluation"]["reason"])
        second = await self.lab.cancel(running["id"])
        self.assertTrue(second["already_stopped"])

    async def test_an_unsupported_environment_is_retained_with_its_reason(self) -> None:
        with self.assertRaises(EvaluationError):
            await self.lab.start(
                {
                    "game_id": "gameplay-objective",
                    "target_title": "NC FORGE TEST",
                    "confirm": True,
                }
            )
        history = self.lab.history()
        self.assertEqual(history["count"], 1)
        row = history["evaluations"][0]
        self.assertEqual(row["status"], "unsupported")
        self.assertFalse(row["valid"])
        self.assertIn("decision pipeline", row["reason"])
        self.assertEqual(self.benchmark.history()["count"], 0, "nothing was started")
        report = self.lab.report(row["id"])
        self.assertEqual(report["status"], "unsupported")
        self.assertTrue(report["missing_evidence"])
        self.assertFalse(report["objective_verified"])

    async def test_a_refused_control_attempt_is_recorded_as_a_control_attempt(self) -> None:
        """A retained refusal states the mode that was ATTEMPTED.

        Recording a refused ``control`` evaluation as ``observation_only`` would
        tell the history (and the report built from it) that a different attempt
        was made than the one the caller asked for.
        """
        with self.assertRaises(EvaluationError):
            await self.lab.start({**_request(), "mode": "control"})
        rows = self.lab.history()["evaluations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "invalid")
        self.assertEqual(rows[0]["mode"], "control")
        self.assertEqual(self.lab.report(rows[0]["id"])["mode"], "control")
        self.assertIn("Only observation_only is available", rows[0]["reason"])
        self.assertEqual(self.benchmark.history()["count"], 0, "nothing was started")
        # A value that is not a mode cannot be stored as one; the refusal reason
        # is where that caller's mistake is named.
        with self.assertRaises(EvaluationError):
            await self.lab.start({**_request(), "mode": "banana"})
        latest = self.lab.history()["evaluations"][0]
        self.assertEqual(latest["mode"], "observation_only")
        self.assertIn("mode must be", latest["reason"])
        self.assertEqual(self.benchmark.history()["count"], 0, "nothing was started")

    async def test_a_target_lost_after_start_is_never_completed(self) -> None:
        running = await self.lab.start(_request(duration_seconds=10, frame_interval_seconds=0.1))
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not self.lab.get(running["id"])["session_id"]:
            await asyncio.sleep(0.01)
        self.desktop.window = None
        finished = await self._wait_terminal(running["id"], timeout=15.0)
        self.assertNotEqual(finished["status"], "completed")
        self.assertFalse(finished["valid"])
        self.assertIn(finished["status"], {"aborted", "invalid", "failed"})

    async def test_preflight_identity_is_enforced_at_start(self) -> None:
        first = await self.lab.preflight(_request())
        pinned = first["identity"]
        # A different window now carries the title; the operator's pin must refuse
        # the start instead of silently observing the wrong window.
        self.desktop.window.handle = 0x9999
        self.desktop.window.pid = 777
        with self.assertRaises(EvaluationError):
            await self.lab.start(
                _request(expected_handle=pinned["handle"], expected_pid=pinned["pid"])
            )
        history = self.lab.history()
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["evaluations"][0]["status"], "invalid")
        self.assertIn("changed since it was validated", history["evaluations"][0]["reason"])
        self.assertIsNone(self.manager.active())

    async def test_a_completed_evaluation_survives_restart_and_running_is_recovered(self) -> None:
        finished = await self._run_to_end(_request())
        restored = NovaForgeEvaluationLab(
            benchmark=NovaForgeBenchmarkLab(
                manager=self.manager, store=JsonStateStore(self.data_dir), poll_interval=0.02
            ),
            store=JsonStateStore(self.data_dir),
        )
        restored_record = restored.get(finished["id"])
        assert restored_record is not None
        self.assertEqual(restored_record["status"], "completed")
        self.assertTrue(restored_record["valid"])
        self.assertEqual(restored_record["benchmark_run_id"], finished["benchmark_run_id"])

        store = JsonStateStore(self.data_dir)
        payload = store.read(EVALUATIONS_NAMESPACE)
        payload["evaluations"].append(
            {
                "version": 1,
                "id": "interrupted-evaluation",
                "game": finished["game"],
                "scenario": finished["scenario"],
                "configuration": finished["configuration"],
                "status": "running",
                "valid": False,
                "reason": "",
                "benchmark_run_id": "gone",
                "created_at": "2026-10-18T09:00:00Z",
                "scenario_version": 1,
            }
        )
        store.write(EVALUATIONS_NAMESPACE, payload)
        recovered = NovaForgeEvaluationLab(
            benchmark=NovaForgeBenchmarkLab(
                manager=self.manager, store=JsonStateStore(self.data_dir), poll_interval=0.02
            ),
            store=JsonStateStore(self.data_dir),
        )
        interrupted = recovered.get("interrupted-evaluation")
        assert interrupted is not None
        self.assertEqual(interrupted["status"], "aborted")
        self.assertTrue(interrupted["interrupted"])
        self.assertFalse(interrupted["valid"])
        self.assertIn("restarted", interrupted["reason"])

    async def test_a_corrupt_stored_status_is_preserved_as_invalid(self) -> None:
        finished = await self._run_to_end(_request())
        store = JsonStateStore(self.data_dir)
        payload = store.read(EVALUATIONS_NAMESPACE)
        payload["evaluations"][0]["status"] = "fabricated-success"
        store.write(EVALUATIONS_NAMESPACE, payload)
        lab = NovaForgeEvaluationLab(
            benchmark=NovaForgeBenchmarkLab(
                manager=self.manager, store=JsonStateStore(self.data_dir), poll_interval=0.02
            ),
            store=JsonStateStore(self.data_dir),
        )
        record = lab.get(finished["id"])
        assert record is not None
        self.assertEqual(record["status"], "invalid")
        self.assertFalse(record["valid"])
        self.assertIn("unrecognised status", record["reason"])

    async def test_the_report_links_evidence_and_states_its_gaps(self) -> None:
        finished = await self._run_to_end(_request())
        report = self.lab.report(finished["id"])
        self.assertEqual(report["evaluation_id"], finished["id"])
        self.assertEqual(report["compatibility"]["status"], "OBSERVATION_ONLY")
        self.assertFalse(report["compatibility"]["control_capable"])
        self.assertFalse(report["objective_verified"])
        self.assertFalse(report["real_gameplay_control_exercised"])
        self.assertTrue(report["evidence"]["timeline"])
        self.assertTrue(report["evidence"]["benchmark_run"])
        self.assertTrue(report["limitations"])
        self.assertTrue(report["readiness"]["ready"])
        self.assertEqual(report["readiness"]["counts"]["fail"], 0)
        self.assertFalse(report["objective"]["available"])
        for gap in report["missing_evidence"]:
            self.assertIsInstance(gap, str)
            self.assertTrue(gap)

    async def test_no_gameplay_input_is_ever_sent(self) -> None:
        await self._run_to_end(_request())
        # The desktop double has one release seam (cleanup) and a capture seam;
        # there is no input runner to call, and the session counted zero actions.
        self.assertGreaterEqual(self.desktop.release_calls, 1)
        for record in self.lab.history()["evaluations"]:
            run = self.benchmark.get(record["benchmark_run_id"])
            assert run is not None
            session = self.manager.get(run["session_id"])
            assert session is not None
            self.assertEqual(session.actions_attempted, 0)
            self.assertEqual(session.actions_executed, 0)

    def test_evaluation_limitations_state_the_absence_of_control(self) -> None:
        game = resolve_game("offline-game-window")
        scenario = resolve_scenario("observation-baseline")
        assert game is not None and scenario is not None
        limitations = evaluation_limitations(game, scenario)
        self.assertTrue(any("never means a game objective" in item for item in limitations))
        self.assertTrue(any("gameplay input" in item.lower() for item in limitations))


class EvaluationAuditTests(_EvaluationFixture):
    """Defects and report claims from the NF-05 verification pass, each pinned.

    Every case here was reachable through the real surfaces (a stored record, a
    registry record, an omitting API caller, an operator stop) before its fix,
    or is a claim the phase report makes that had no test behind it.
    """

    async def test_a_partial_stored_record_is_reported_not_a_crash(self) -> None:
        """A record without evidence/limitations must still produce a report."""
        finished = await self._run_to_end(_request())
        store = JsonStateStore(self.data_dir)
        payload = store.read(EVALUATIONS_NAMESPACE)
        for row in payload["evaluations"]:
            row.pop("evidence", None)
            row.pop("limitations", None)
        store.write(EVALUATIONS_NAMESPACE, payload)
        lab = NovaForgeEvaluationLab(
            benchmark=NovaForgeBenchmarkLab(
                manager=self.manager, store=JsonStateStore(self.data_dir), poll_interval=0.02
            ),
            store=JsonStateStore(self.data_dir),
        )
        report = lab.report(finished["id"])
        self.assertEqual(report["evaluation_id"], finished["id"])
        self.assertFalse(report["objective_verified"])
        # The record's own evidence links are gone, and the report says exactly
        # that instead of inventing them or raising on the missing key.
        self.assertEqual(report["evidence"], {})
        self.assertTrue(report["limitations"], "the registry's limitations still apply")
        self.assertTrue(report["missing_evidence"])
        for gap in report["missing_evidence"]:
            self.assertIsInstance(gap, str)
            self.assertTrue(gap)

    async def test_the_retention_cap_drops_the_oldest_and_says_so(self) -> None:
        """History is bounded, and the retention block reports the bound it used."""
        capped = NovaForgeEvaluationLab(
            benchmark=self.benchmark, store=self.store, max_evaluations=2
        )
        for _ in range(3):
            with self.assertRaises(EvaluationError):
                # A refused configuration is the cheapest retained attempt: it
                # never starts a run, and each one is still recorded.
                await capped.start(_request(duration_seconds=0))
        history = capped.history()
        self.assertEqual(history["count"], 2, "the cap is enforced")
        self.assertEqual(history["total"], 2)
        self.assertEqual(history["retention"]["cap"], 2)
        self.assertEqual(history["retention"]["retained"], 2)
        self.assertTrue(history["retention"]["note"])
        # The surviving two are the NEWEST attempts, and the eviction happened in
        # the store too (a cap that only trims memory would restore the dropped
        # record on the next restart).
        stored = self.store.read(EVALUATIONS_NAMESPACE)["evaluations"]
        self.assertEqual(len(stored), 2)
        self.assertEqual(
            {row["id"] for row in stored},
            {row["id"] for row in history["evaluations"]},
        )

    async def test_a_persistence_state_is_measured_never_assumed(self) -> None:
        """verified / failed / not_persisted are three distinct answers.

        The report and every record surface the state, so a build with no store
        must say *not persisted*, and a store that silently drops the snapshot
        must say *failed* — neither may look like a successful write.
        """
        finished = await self._run_to_end(_request())
        self.assertEqual(finished["persistence"]["state"], "verified")

        # No store anywhere below the lab: the run is REFUSED (evidence that
        # cannot survive the process is a blocked check, not a warning), and the
        # retained attempt says memory-only rather than claiming a write.
        runs_before = self.benchmark.history()["count"]
        self.lab.store = None
        self.benchmark.store = None
        self.manager.store = None
        try:
            with self.assertRaises(EvaluationError) as refused:
                await self.lab.start(_request())
            refused_id = next(
                row["id"]
                for row in self.lab.history()["evaluations"]
                if row["status"] != "completed"
            )
            memory_only = self.lab.get(refused_id)
            assert memory_only is not None
        finally:
            self.lab.store = self.store
            self.benchmark.store = self.store
            self.manager.store = self.store
        self.assertEqual(memory_only["persistence"]["state"], "not_persisted")
        self.assertIn("in memory", memory_only["persistence"]["reason"])
        self.assertIn("Evidence persistence", str(refused.exception))
        self.assertEqual(
            self.benchmark.history()["count"], runs_before, "nothing was started"
        )

        # A root that cannot hold a file (its parent is a REGULAR file, which
        # refuses on every platform) drops the snapshot silently; the read-back
        # is what turns that into an honest `failed`.
        blocker = self.data_dir / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        self.lab.store = JsonStateStore(blocker / "child")  # type: ignore[assignment]
        try:
            dropped = await self._run_to_end(_request())
        finally:
            self.lab.store = self.store
        self.assertEqual(dropped["persistence"]["state"], "failed")
        self.assertIn("not readable back", dropped["persistence"]["reason"])
        self.assertEqual(dropped["status"], "completed", "recording never fails the run")
        # ... and the lab never lied about the store on the way: the dropped run
        # is still readable in memory for this process.
        self.assertIsNotNone(self.lab.get(dropped["id"]))

    async def test_a_registry_fault_becomes_a_blocking_row_not_a_crash(self) -> None:
        """A record that breaks a check's invariant must refuse, not 500."""
        bad = replace(
            resolve_game("disposable-window"),  # type: ignore[arg-type]
            status="UNSUPPORTED",
            status_reason="",
        )
        original = dict(games_module._GAMES_BY_ID)
        games_module._GAMES_BY_ID = {bad.game_id: [bad]}
        try:
            result = await self.lab.preflight(_request())
        finally:
            games_module._GAMES_BY_ID = original
        self.assertFalse(result["ready"])
        self.assertIn("readiness_check_builder", result["blocking"])
        row = next(item for item in result["checks"] if item["id"] == "readiness_check_builder")
        self.assertEqual(row["status"], "fail")
        self.assertTrue(row["required"])
        self.assertIn("needs a reason when it is fail", row["reason"])
        self.assertIsNone(self.manager.active(), "nothing was started")

    async def test_an_omitted_budget_records_the_resolved_configuration(self) -> None:
        """The record must describe the run that happened, not the gap in the request."""
        request = _request()
        request.pop("duration_seconds")
        request.pop("frame_interval_seconds")
        finished = await self._run_to_end(request)
        configuration = finished["configuration"]
        self.assertEqual(configuration["duration_seconds"], 10)
        self.assertEqual(configuration["frame_interval_seconds"], 0.5)
        run = self.benchmark.get(finished["benchmark_run_id"])
        assert run is not None
        self.assertEqual(
            run["configuration"]["duration_seconds"], configuration["duration_seconds"]
        )
        self.assertEqual(
            run["configuration"]["frame_interval_seconds"],
            configuration["frame_interval_seconds"],
        )

    async def test_two_processes_sharing_a_title_are_refused_end_to_end(self) -> None:
        """Requirement: duplicate titles are refused by the REAL probe, not a stub."""

        class TwoProcesses(FakeDesktop):
            async def window_lines(self) -> list[str]:
                # Same title, different processes: a listed target that cannot be
                # told apart is exactly what the uniqueness check exists for.
                return [
                    f"python|{self.window.title}",  # type: ignore[union-attr]
                    f"other-app|{self.window.title}",  # type: ignore[union-attr]
                ]

        desktop = TwoProcesses()
        manager = NovaForgeManager(
            data_dir=self.data_dir,
            capture=desktop.capture,
            geometry=desktop.geometry,
            window_lines=desktop.window_lines,
            release_inputs=desktop.release,
            capture_supported=True,
            frame_interval=0.05,
            stop_timeout=4.0,
        )
        lab = NovaForgeEvaluationLab(
            benchmark=NovaForgeBenchmarkLab(
                manager=manager, store=JsonStateStore(self.data_dir), poll_interval=0.02
            ),
            store=JsonStateStore(self.data_dir),
        )
        result = await lab.preflight(_request())
        row = next(item for item in result["checks"] if item["id"] == "target_uniqueness")
        self.assertEqual(row["status"], "fail")
        self.assertIn("More than one window matches", row["reason"])
        identity = next(item for item in result["checks"] if item["id"] == "target_identity")
        self.assertIn("refused first", identity["reason"])
        self.assertIn("windows match", identity["reason"])
        self.assertFalse(result["ready"])
        self.assertIn("target_uniqueness", result["blocking"])
        with self.assertRaises(EvaluationError):
            await lab.start(_request())
        self.assertEqual(lab.history()["count"], 1, "the refusal is retained")
        self.assertEqual(manager.active(), None)

    async def test_two_concurrent_starts_cannot_both_win(self) -> None:
        """One session slot, enforced under the lock rather than by luck."""
        requests = _request(duration_seconds=6, frame_interval_seconds=0.2)
        results = await asyncio.gather(
            self.lab.start(dict(requests)), self.lab.start(dict(requests)), return_exceptions=True
        )
        started = [item for item in results if not isinstance(item, Exception)]
        refused = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(started), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertIsInstance(refused[0], EvaluationConflictError)
        history = self.lab.history()
        self.assertEqual(history["count"], 2)
        self.assertEqual(sorted(row["status"] for row in history["evaluations"]), ["invalid", "running"])
        self.assertIsNotNone(self.manager.active())
        await self.lab.cancel(started[0]["id"])

    async def test_a_refused_configuration_does_not_blame_the_window(self) -> None:
        """A check that could not run must say so instead of failing or passing."""
        for value in (True, 2.9, 0, 1000):
            with self.subTest(duration_seconds=value):
                result = await self.lab.preflight(_request(duration_seconds=value))
                self.assertFalse(result["ready"])
                self.assertIn("benchmark_configuration", result["blocking"])
                rows = {item["id"]: item for item in result["checks"]}
                self.assertEqual(rows["target_uniqueness"]["status"], "unavailable")
                self.assertIn("was not judged", rows["target_uniqueness"]["reason"])
                self.assertIn("refused first", rows["target_identity"]["reason"])
                self.assertIn("between 1 and 300", rows["benchmark_configuration"]["reason"])
                self.assertIsNone(self.manager.active(), "nothing was started")

    async def test_a_cancel_after_the_benchmark_record_vanished_is_not_an_error(self) -> None:
        """Cancel answers with an ending, never with an exception."""
        running = await self.lab.start(_request(duration_seconds=10, frame_interval_seconds=0.1))

        class VanishingLab:
            def __init__(self, inner: NovaForgeBenchmarkLab) -> None:
                self._inner = inner
                self.__dict__.update(inner.__dict__)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            async def cancel(self, run_id: str) -> dict[str, Any]:
                raise BenchmarkNotFoundError(f"Unknown benchmark run: {run_id}")

        self.lab.benchmark = VanishingLab(self.benchmark)  # type: ignore[assignment]
        try:
            result = await self.lab.cancel(running["id"])
        finally:
            self.lab.benchmark = self.benchmark  # type: ignore[assignment]
        record = result["evaluation"]
        self.assertEqual(record["status"], "aborted")
        self.assertFalse(record["valid"])
        self.assertIn("did not reach a terminal state", record["reason"])

    async def test_an_operator_emergency_stop_ends_the_evaluation_and_cleans_up(self) -> None:
        """A safety intervention is a terminal ending, and cleanup still runs."""
        running = await self.lab.start(_request(duration_seconds=10, frame_interval_seconds=0.1))
        deadline = time.monotonic() + 4.0
        session_id = ""
        while time.monotonic() < deadline and not session_id:
            session_id = str(self.lab.get(running["id"])["session_id"])
            await asyncio.sleep(0.01)
        self.assertTrue(session_id, "the evaluation never reached a session")
        stop = await self.manager.emergency_stop(session_id)
        self.assertFalse(stop["already_stopped"])
        self.assertEqual(stop["end_reason"], "emergency_stop")
        self.assertTrue(stop["input_cleanup"]["ok"])
        self.assertEqual(stop["input_cleanup"]["held_after"], 0)
        interventions = [item["kind"] for item in stop["interventions"]]
        self.assertIn("emergency_stop", interventions)
        finished = await self._wait_terminal(running["id"], timeout=15.0)
        self.assertNotEqual(finished["status"], "completed")
        self.assertFalse(finished["valid"])
        self.assertFalse(finished["objective"]["verified"])
        self.assertGreaterEqual(self.desktop.release_calls, 1, "input cleanup did not run")

    async def test_stale_frames_cannot_produce_a_completed_evaluation(self) -> None:
        """An observation source that returns non-fresh frames is not a result."""
        self.desktop.stale = True
        finished = await self._run_to_end(_request(duration_seconds=1))
        self.assertNotEqual(finished["status"], "completed")
        self.assertFalse(finished["valid"])
        report = self.lab.report(finished["id"])
        self.assertFalse(report["objective_verified"])
        frames = (finished.get("metrics") or {}).get("frames") or {}
        self.assertNotEqual(frames.get("value"), None, "a measured zero is still a figure")
        self.assertIn("missing_evidence", report)

    def test_missing_evidence_names_each_absent_artifact(self) -> None:
        self.assertEqual(
            missing_evidence(benchmark=None, timeline=None, baseline=None),
            ["The benchmark run record for this evaluation is unavailable."],
        )
        bare = {"evidence": {}}
        gaps = missing_evidence(benchmark=bare, timeline=None, baseline=None)
        self.assertIn("No captured frame reference was retained for this run.", gaps)
        self.assertIn("No evidence-timeline endpoint was recorded.", gaps)
        self.assertTrue(any("derived from the session record" in item for item in gaps))
        self.assertTrue(any("persistence is unverified" in item for item in gaps))
        unreadable = missing_evidence(
            benchmark={}, timeline=None, baseline=None, session_id="abc123"
        )
        self.assertTrue(any("session abc123 could not be read" in item for item in unreadable))
        complete = missing_evidence(
            benchmark={
                "evidence": {
                    "frames": [{"frame_id": "f1"}],
                    "timeline_endpoint": "/novaforge/sessions/x/timeline",
                    "timeline_source": "recorded",
                    "persistence": {"ok": True},
                }
            },
            timeline={"limitations": []},
            baseline={"benchmark_run_id": "run-1"},
            session_id="x",
        )
        self.assertEqual(complete, [])

    def test_no_nf05_module_can_launch_or_install_a_target(self) -> None:
        """The phase's most important absence, pinned as code rather than prose.

        NF-05 launches nothing. A future edit that reached for a process call
        would have to delete this test to ship.
        """
        for name in ("games.py", "evaluation.py"):
            source = (Path("src/novacontrol/novaforge") / name).read_text(encoding="utf-8")
            for forbidden in ("subprocess", "Popen", "startfile", "os.system", "shell=True"):
                with self.subTest(module=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class UnknownIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_evaluation_and_game_ids_are_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            desktop = FakeDesktop()
            manager = NovaForgeManager(
                data_dir=tmp,
                capture=desktop.capture,
                geometry=desktop.geometry,
                window_lines=desktop.window_lines,
                capture_supported=True,
                frame_interval=0.05,
            )
            benchmark = NovaForgeBenchmarkLab(manager=manager, store=JsonStateStore(tmp))
            lab = NovaForgeEvaluationLab(benchmark=benchmark, store=JsonStateStore(tmp))
            with self.assertRaises(GameNotFoundError):
                lab.game("nope")
            with self.assertRaises(GameNotFoundError):
                lab.report("nope")
            with self.assertRaises(GameNotFoundError):
                await lab.cancel("nope")
            with self.assertRaises(GameNotFoundError):
                lab.baseline("nope", "observation-baseline")
            await benchmark.shutdown()


if __name__ == "__main__":
    unittest.main()
