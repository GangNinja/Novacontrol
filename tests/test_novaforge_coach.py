"""NovaForge NF-06 tests: the Forge Coach and controlled experiments.

The tests run the REAL NF-01 session manager, the REAL NF-04 benchmark lab and
the REAL NF-05 evaluation lab over a deterministic desktop double. No game is
launched, no gameplay input exists, and no objective verifier exists — the
absences are pinned instead of papered over.

The properties pinned here are the ones that make a coaching layer honest:

* a finding names its evidence and says whether it is observed, derived, a
  hypothesis or unknown;
* a record with nothing in it answers ``insufficient_evidence``;
* the earliest failure is a *candidate* in sequence order, never a cause;
* recommendations are ranked, traceable and measured by a metric the scenario
  can actually observe;
* an experiment cannot run without an approval bound to its exact
  configuration, and changing that configuration invalidates the approval;
* a result that cannot be compared honestly says ``not_comparable``;
* analysis writes nothing, and no experiment trains or applies anything.
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.novaforge import (
    EXPERIMENTS_NAMESPACE,
    RECOMMENDATIONS_NAMESPACE,
    CoachError,
    CoachNotFoundError,
    ExperimentConflictError,
    ExperimentError,
    ExperimentNotFoundError,
    ExperimentSpec,
    NovaForgeBenchmarkLab,
    NovaForgeCoach,
    NovaForgeEvaluationLab,
    NovaForgeExperimentLab,
    NovaForgeManager,
    RECOMMENDATION_RULES,
    SessionRecord,
    MissionConfig,
    resolve_scenario,
)
from novacontrol.novaforge.benchmark import metric_spec
from novacontrol.novaforge.coach import FINDING_KINDS, SEVERITIES, EVIDENCE_STRENGTHS
from novacontrol.novaforge.objective import ObjectiveDefinition
from novacontrol.persistence.json_store import JsonStateStore

from test_novaforge import FakeDesktop

TARGET = "NC FORGE TEST"


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "game_id": "disposable-window",
        "scenario_id": "observation-baseline",
        "target_title": TARGET,
        "duration_seconds": 1,
        "frame_interval_seconds": 0.05,
        "confirm": True,
    }
    payload.update(overrides)
    return payload


class _EmptyManager:
    """A manager-shaped stub whose session recorded nothing at all.

    The Coach only needs ``get``, ``timeline_payload`` and ``telemetry_payload``;
    answering with genuinely empty records is how ``insufficient_evidence`` is
    exercised without pretending a real session produced nothing.
    """

    def __init__(self) -> None:
        self.record = SessionRecord(
            mission=MissionConfig("Empty", "Observe nothing.", TARGET)
        )

    def get(self, session_id: str) -> Any:
        return self.record if session_id == self.record.session_id else None

    def timeline_payload(self, session_id: str, *, limit: int = 100) -> dict[str, Any]:
        return {
            "source": "derived",
            "events": [],
            "retention": {"stored": 0, "dropped": 0},
            "persistence": {"state": "not_stored", "reason": "no event log"},
            "classification": {"outcome": "unclassified"},
        }

    def telemetry_payload(self, session_id: str) -> dict[str, Any]:
        return {"telemetry": {"capture": {"frames": 0}, "session": {}, "target": {}}}


class VerifiedObjectiveCoachTests(unittest.TestCase):
    """NF-07's verifier must reach the analysis: a decided verdict is a RESULT.

    The structural finding used to be appended unconditionally, with evidence
    asserting ``objective_verification: available False``. After NF-07 added the
    verifier that statement is false about the build AND about a session whose
    objective was just confirmed, so a verified success was analysed as "No
    objective was verified by this session". The three states are now distinct,
    and this regression pins the first: a decided verdict is quoted, never denied.
    """

    def _analyse(self, verification: dict[str, Any], objective: Any = None) -> dict[str, Any]:
        manager = _EmptyManager()
        manager.record.verification = dict(verification)
        manager.record.objective = objective
        with TemporaryDirectory() as tmp:
            coach = NovaForgeCoach(manager=manager, store=JsonStateStore(Path(tmp)))
            return coach.analyse(session_id=manager.record.session_id)

    def test_a_verified_success_is_a_result_not_a_missing_verifier(self) -> None:
        analysis = self._analyse(
            {
                "outcome": "verified_success",
                "verified": True,
                "achieved": True,
                "decided": True,
                "reason": "Read 'NC FORGE HARNESS - READY' from window 42 (pid 7): satisfied.",
            },
            ObjectiveDefinition.from_payload(
                {"kind": "window_title", "parameter": "READY", "mode": "contains"}
            ),
        )
        by_category = {item["category"]: item for item in analysis["findings"]}
        self.assertNotIn("objective_verifier_missing", by_category)
        finding = by_category["objective_verified"]
        self.assertIn("SUCCESS", finding["title"])
        self.assertIn("READY", finding["explanation"])
        earliest = analysis["earliest_recoverable_error"]["reason"].lower()
        self.assertIn("objective", earliest)
        self.assertNotIn("no objective verifier", earliest)

    def test_an_undecided_objective_is_not_reported_as_verified(self) -> None:
        analysis = self._analyse(
            {},
            ObjectiveDefinition.from_payload(
                {"kind": "window_title", "parameter": "READY", "mode": "contains"}
            ),
        )
        by_category = {item["category"]: item for item in analysis["findings"]}
        self.assertIn("objective_unverified", by_category)
        self.assertNotIn("objective_verified", by_category)
        self.assertIn("never decided", by_category["objective_unverified"]["title"])


class _CoachFixture(unittest.IsolatedAsyncioTestCase):
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
            manager=self.manager, store=self.store, poll_interval=0.02, publish=publish
        )
        self.evaluations = NovaForgeEvaluationLab(
            benchmark=self.benchmark, store=self.store, publish=publish
        )
        self.coach = NovaForgeCoach(
            manager=self.manager,
            benchmark=self.benchmark,
            evaluations=self.evaluations,
            store=self.store,
            publish=publish,
        )
        self.experiments = NovaForgeExperimentLab(
            evaluations=self.evaluations,
            store=self.store,
            publish=publish,
            permission=lambda: {
                "checked": True,
                "allowed": True,
                "source": "fixture",
                "reason": "The fixture permission layer allows observation.",
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _evaluate(self, timeout: float = 25.0, **overrides: Any) -> dict[str, Any]:
        record = await self.evaluations.start(_request(**overrides))
        return await self._wait_evaluation(str(record["id"]), timeout)

    async def _wait_evaluation(self, evaluation_id: str, timeout: float = 25.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.evaluations.get(evaluation_id)
            assert current is not None
            if current["status"] != "running":
                return current
            await asyncio.sleep(0.03)
        raise AssertionError(f"evaluation did not finish: {self.evaluations.get(evaluation_id)}")

    async def _run_experiment(self, experiment_id: str, timeout: float = 60.0) -> dict[str, Any]:
        await self.experiments.approve(experiment_id, note="fixture approval")
        await self.experiments.run(experiment_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.experiments.get(experiment_id)
            assert current is not None
            if current["status"] not in {"running", "approved"}:
                return current
            await asyncio.sleep(0.05)
        raise AssertionError(f"experiment did not finish: {self.experiments.get(experiment_id)}")


class CoachAnalysisTests(_CoachFixture):
    async def test_findings_cite_evidence_and_use_the_declared_vocabularies(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))

        self.assertTrue(analysis["read_only"])
        self.assertEqual(analysis["subject"]["evaluation_id"], evaluation["id"])
        self.assertEqual(analysis["subject"]["session_id"], evaluation["session_id"])
        self.assertTrue(analysis["findings"])
        for finding in analysis["findings"]:
            with self.subTest(category=finding["category"]):
                self.assertIn(finding["kind"], FINDING_KINDS)
                self.assertIn(finding["severity"], SEVERITIES)
                self.assertIn(finding["confidence"], EVIDENCE_STRENGTHS)
                self.assertTrue(finding["severity_basis"])
                self.assertTrue(finding["confidence_basis"])
                self.assertTrue(finding["evidence"])
                # Every reference is bounded: an event id/seq, a metric value or
                # a record pointer — never a payload copy.
                for ref in finding["evidence"]:
                    self.assertIn("kind", ref)

    async def test_the_subject_reports_the_pinned_target_identity(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        target = analysis["subject"]["target"]
        # The record holds a TargetWindow, so an analysis that reported "not
        # recorded" here was dropping an identity it had. The title is the one
        # the window actually carries, not the mission's requested title.
        window = self.desktop.window
        assert window is not None
        self.assertEqual(target["title"], window.title)
        self.assertIsInstance(target["handle"], int)
        self.assertIsInstance(target["pid"], int)
        self.assertGreaterEqual(target["width"], 0)

    async def test_observed_and_derived_findings_are_told_apart(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        by_category = {item["category"]: item for item in analysis["findings"]}

        limit = by_category["time_limit_reached"]
        self.assertEqual(limit["kind"], "derived")
        self.assertFalse(limit["direct"])

        structural = by_category["objective_verifier_missing"]
        self.assertEqual(structural["kind"], "observed")
        self.assertTrue(structural["direct"])
        self.assertEqual(structural["severity"], "info")
        self.assertIn("No objective was verified", structural["title"])

    async def test_a_clean_run_says_there_is_no_earliest_failure_without_claiming_success(
        self,
    ) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        earliest = analysis["earliest_recoverable_error"]

        self.assertFalse(earliest["available"])
        self.assertIsNone(earliest["candidate"])
        self.assertFalse(earliest["causation_claimed"])
        self.assertIn("not the same as success", earliest["reason"])
        self.assertIn("no objective verifier", earliest["reason"].lower())

    async def test_a_capture_failure_is_the_earliest_candidate_and_later_symptoms_follow(
        self,
    ) -> None:
        self.desktop.fail = True
        evaluation = await self._evaluate(duration_seconds=2)
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        earliest = analysis["earliest_recoverable_error"]

        self.assertTrue(earliest["available"], analysis["findings"])
        self.assertEqual(earliest["candidate"]["type"], "capture.failed")
        self.assertFalse(earliest["causation_claimed"])
        self.assertIn("Temporal order is not causation", earliest["uncertainty"])
        self.assertTrue(earliest["basis"])
        # The finding behind it is an OBSERVED fact with a metric agreeing.
        capture = next(
            item for item in analysis["findings"] if item["category"] == "capture_failure"
        )
        self.assertEqual(capture["kind"], "observed")
        self.assertIn(capture["confidence"], {"moderate", "strong"})
        self.assertTrue(
            any(ref.get("kind") == "metric" for ref in capture["evidence"]),
            capture["evidence"],
        )

    async def test_a_benign_time_limit_notice_is_not_dressed_up_as_a_fault(self) -> None:
        """NF-01 writes the limit notice to the error list on purpose."""
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        categories = [item["category"] for item in analysis["findings"]]

        self.assertNotIn("error_recorded", categories)
        self.assertIn("time_limit_reached", categories)
        self.assertEqual(
            analysis["earliest_recoverable_error"]["available"], False
        )

    async def test_insufficient_evidence_is_an_answer_not_a_diagnosis(self) -> None:
        manager = _EmptyManager()
        coach = NovaForgeCoach(manager=manager, store=self.store)
        analysis = coach.analyse(session_id=manager.record.session_id)

        self.assertTrue(analysis["insufficient_evidence"]["state"])
        self.assertIn("no timeline events", analysis["insufficient_evidence"]["reason"])
        self.assertTrue(analysis["insufficient_evidence"]["what_would_help"])
        # Only the structural limitation may be stated: no symptom is invented.
        self.assertEqual(
            [item["category"] for item in analysis["findings"]],
            ["objective_verifier_missing"],
        )
        self.assertEqual(analysis["recommendations"], [])

    async def test_unknown_subjects_and_missing_arguments_are_refused(self) -> None:
        with self.assertRaises(CoachNotFoundError):
            self.coach.analyse(session_id="does-not-exist")
        with self.assertRaises(CoachNotFoundError):
            self.coach.analyse(evaluation_id="does-not-exist")
        with self.assertRaises(CoachError):
            self.coach.analyse()

    async def test_recommendations_are_ranked_traceable_and_measured(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        findings = {item["finding_id"]: item for item in analysis["findings"]}
        recommendations = analysis["recommendations"]

        self.assertTrue(recommendations)
        self.assertEqual(
            [item["priority"] for item in recommendations],
            list(range(1, len(recommendations) + 1)),
        )
        for recommendation in recommendations:
            with self.subTest(category=recommendation["category"]):
                self.assertIn(recommendation["finding_id"], findings)
                self.assertTrue(recommendation["evidence"])
                self.assertTrue(recommendation["success_criterion"])
                self.assertTrue(recommendation["expected_effect"])
                self.assertTrue(recommendation["metric"])
                metric_spec(recommendation["metric"])  # exists in the NF-04 catalog
                self.assertIn(recommendation["status"], {"proposed"})
                self.assertTrue(recommendation["requires_approval"])
        # Priority follows the severity band: a medium finding outranks a low one.
        severities = [findings[item["finding_id"]]["severity"] for item in recommendations]
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        self.assertEqual(severities, sorted(severities, key=lambda item: order[item]))

    async def test_every_declared_rule_names_a_metric_its_scenario_measures(self) -> None:
        for rule in RECOMMENDATION_RULES:
            with self.subTest(category=rule.category):
                metric_spec(rule.metric)
                if not rule.experiment_kind:
                    continue
                scenario_id = rule.scenario or "observation-baseline"
                scenario = resolve_scenario(scenario_id)
                assert scenario is not None
                self.assertIn(rule.metric, scenario.metrics)
                self.assertTrue(scenario.supported)

    async def test_analysis_never_modifies_a_recorded_subject(self) -> None:
        evaluation = await self._evaluate()
        evaluation_id = str(evaluation["id"])
        run_id = str(evaluation["benchmark_run_id"])
        session_id = str(evaluation["session_id"])
        # The telemetry VIEW recomputes "age since last frame" on every read, so
        # the persisted session record (which carries the telemetry snapshot) is
        # what must be byte-identical, together with the timeline, the evaluation
        # and the benchmark run.
        def snapshot() -> tuple[str, str, str]:
            timeline = self.manager.timeline_payload(session_id)
            # The payload label is a read-time wall clock, not recorded state.
            timeline.pop("generated_at", None)
            return (
                json.dumps(self.evaluations.get(evaluation_id), sort_keys=True),
                json.dumps(self.benchmark.get(run_id), sort_keys=True),
                json.dumps(
                    {
                        "session": self.manager.get(session_id).to_dict(),
                        "timeline": timeline,
                    },
                    sort_keys=True,
                ),
            )

        before = snapshot()
        self.coach.analyse(evaluation_id=evaluation_id)
        self.coach.analyse(session_id=session_id)
        self.assertEqual(before, snapshot())

    async def test_analysis_is_recomputed_and_never_cached(self) -> None:
        """A second call reflects the store's current recommendation status."""
        evaluation = await self._evaluate()
        first = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        recommendation = first["recommendations"][0]
        self.coach.set_recommendation_status(
            recommendation["recommendation_id"], "approved", note="operator"
        )
        second = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        statuses = {
            item["recommendation_id"]: item["status"] for item in second["recommendations"]
        }
        self.assertEqual(statuses[recommendation["recommendation_id"]], "approved")


class CoachRecommendationStoreTests(_CoachFixture):
    async def test_status_is_persisted_with_its_history_and_reloads(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        recommendation = analysis["recommendations"][0]

        record = self.coach.set_recommendation_status(
            recommendation["recommendation_id"], "approved", note="looks worth trying"
        )
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["persistence"]["state"], "verified")
        self.assertEqual(record["history"][-1]["status"], "approved")

        reopened = NovaForgeCoach(
            manager=self.manager,
            benchmark=self.benchmark,
            evaluations=self.evaluations,
            store=self.store,
        )
        rows = reopened.recommendations()["recommendations"]
        self.assertEqual([item["recommendation_id"] for item in rows],
                         [recommendation["recommendation_id"]])
        self.assertEqual(rows[0]["status"], "approved")

    async def test_an_unknown_status_or_recommendation_is_refused(self) -> None:
        with self.assertRaises(CoachNotFoundError):
            self.coach.set_recommendation_status("rec-nope", "approved")
        evaluation = await self._evaluate()
        recommendation = self.coach.analyse(
            evaluation_id=str(evaluation["id"])
        )["recommendations"][0]
        with self.assertRaises(CoachError):
            self.coach.set_recommendation_status(
                recommendation["recommendation_id"], "shipped-it"
            )
        with self.assertRaises(CoachError):
            self.coach.recommendations(status="shipped-it")

    async def test_only_the_status_is_stored_and_the_cap_is_enforced(self) -> None:
        evaluation = await self._evaluate()
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        self.coach.set_recommendation_status(
            analysis["recommendations"][0]["recommendation_id"], "rejected"
        )
        stored = self.store.read(RECOMMENDATIONS_NAMESPACE)
        row = stored["recommendations"][0]
        self.assertNotIn("action", row)
        self.assertNotIn("expected_effect", row)
        self.assertNotIn("evidence", row)
        self.assertIn("recommendation_id", row)


class ExperimentProposalTests(_CoachFixture):
    async def test_an_unsupported_spec_is_blocked_with_reasons_instead_of_running(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "configuration_change",
                "variable": "duration_seconds",
                "change": {"duration_seconds": 2, "frame_interval_seconds": 0.1},
                "primary_metric": "frames",
            }
        )
        self.assertEqual(proposal["status"], "blocked")
        self.assertTrue(proposal["errors"])
        self.assertIn("exactly the declared variable", " ".join(proposal["errors"]))
        with self.assertRaises(ExperimentError):
            await self.experiments.approve(proposal["id"])

    async def test_an_unsupported_scenario_and_unknown_metric_are_refused(self) -> None:
        blocked = self.experiments.propose(
            {
                "game_id": "gameplay-objective",
                "scenario_id": "gameplay-objective",
                "target_title": TARGET,
                "kind": "repeat",
                "primary_metric": "objective_success",
            }
        )
        self.assertEqual(blocked["status"], "blocked")
        text = " ".join(blocked["errors"])
        self.assertIn("unsupported", text.lower())
        self.assertIn("cannot decide an experiment", text)

        metric = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "primary_metric": "vibes",
            }
        )
        self.assertEqual(metric["status"], "blocked")
        self.assertIn("vibes", " ".join(metric["errors"]))

    async def test_a_single_variable_change_is_accepted_and_recorded(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "capture-latency",
                "target_title": TARGET,
                "kind": "configuration_change",
                "variable": "frame_interval_seconds",
                "change": {"frame_interval_seconds": 0.1},
                "baseline_duration_seconds": 2,
                "baseline_frame_interval_seconds": 0.05,
                "primary_metric": "latency_median_ms",
                "repeat_count": 1,
                "threshold": 10.0,
                "guardrails": [],
            }
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        spec = proposal["spec"]
        self.assertEqual(spec["change"], {"frame_interval_seconds": 0.1})
        self.assertEqual(spec["baseline_configuration"],
                         {"duration_seconds": 2, "frame_interval_seconds": 0.05})
        self.assertEqual(spec["candidate_configuration"],
                         {"duration_seconds": 2, "frame_interval_seconds": 0.1})
        self.assertEqual(spec["primary_direction"], "decrease")
        self.assertTrue(spec["digest"])
        self.assertEqual(spec["abort"]["max_seconds_per_arm"],
                         max(15.0, 2 + 30.0))

    async def test_a_repeat_must_declare_no_change_and_bounded_repeats(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "change": {"duration_seconds": 2},
                "primary_metric": "frames",
                "repeat_count": 9,
            }
        )
        self.assertEqual(proposal["status"], "blocked")
        text = " ".join(proposal["errors"])
        self.assertIn("cannot declare a change", text)
        self.assertIn("repeat_count must be between", text)

    async def test_a_recommendation_becomes_a_proposal_on_a_scenario_that_measures_it(
        self,
    ) -> None:
        self.desktop.fail = True
        evaluation = await self._evaluate(duration_seconds=2)
        analysis = self.coach.analyse(evaluation_id=str(evaluation["id"]))
        recommendation = next(
            item for item in analysis["recommendations"]
            if item["category"] == "capture_failure"
        )
        proposal = self.experiments.propose(
            {
                "recommendation_id": recommendation["recommendation_id"],
                "evaluation_id": evaluation["id"],
            },
            coach=self.coach,
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        spec = proposal["spec"]
        self.assertEqual(spec["scenario_id"], "capture-latency")
        self.assertEqual(spec["primary_metric"], "capture_failures")
        self.assertEqual(spec["kind"], "repeat")
        # The subject's baseline was recorded on a DIFFERENT scenario, so it is
        # not carried over: a baseline is only meaningful on the same scenario.
        self.assertEqual(spec["baseline_run_ids"], [])
        self.assertEqual(spec["subject"]["finding_id"], recommendation["finding_id"])

    async def test_an_incompatible_baseline_becomes_a_change_taken_from_recorded_runs(
        self,
    ) -> None:
        # Two runs of the same scenario on different budgets: NF-05 keeps the
        # OLDEST as the baseline and refuses to pool the two, which is exactly
        # the category whose experiment must declare ONE variable — and whose two
        # values are both already recorded.
        await self._evaluate(duration_seconds=2)
        subject = await self._evaluate(duration_seconds=4)
        analysis = self.coach.analyse(evaluation_id=str(subject["id"]))
        recommendation = next(
            item
            for item in analysis["recommendations"]
            if item["category"] == "incompatible_baseline"
        )
        self.assertEqual(recommendation["experiment"]["kind"], "configuration_change")
        self.assertEqual(recommendation["experiment"]["change_from"], "recorded_baseline")

        proposal = self.experiments.propose(
            {
                "recommendation_id": recommendation["recommendation_id"],
                "evaluation_id": subject["id"],
            },
            coach=self.coach,
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        spec = proposal["spec"]
        # The candidate arm re-measures the subject's own value; the baseline arm
        # uses the recorded baseline's. Neither number is invented.
        self.assertEqual(spec["change"], {"duration_seconds": 4})
        self.assertEqual(spec["baseline_configuration"]["duration_seconds"], 2)
        self.assertEqual(spec["variable"], "duration_seconds")
        self.assertEqual(spec["kind"], "configuration_change")

    async def test_a_recorded_change_with_no_measured_value_is_refused_with_a_reason(
        self,
    ) -> None:
        # A configuration change whose two recorded sides agree on the variable
        # has no value to change to: the record is BLOCKED with that reason
        # instead of an experiment being proposed around an invented number.
        blocked = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "configuration_change",
                "variable": "frame_interval_seconds",
                "primary_metric": "frames",
            }
        )
        self.assertEqual(blocked["status"], "blocked")
        text = " ".join(blocked["errors"])
        self.assertIn("declared explicitly", text)
        self.assertIn("The change must set exactly the declared variable", text)

    async def test_a_recommendation_without_a_runnable_experiment_is_refused(self) -> None:
        class _StubCoach:
            def analyse(self, **_: Any) -> dict[str, Any]:
                return {
                    "subject": {"session_id": "s1", "evaluation_id": ""},
                    "recommendations": [
                        {
                            "recommendation_id": "rec-000000000000",
                            "finding_id": "fnd-000000000000",
                            "category": "persistence_failure",
                            "metric": "timeline_events",
                            "experiment": {
                                "kind": "",
                                "variable": "none",
                                "supported": False,
                                "reason": "Needs an operator change.",
                            },
                        }
                    ],
                }

        with self.assertRaises(ExperimentError) as caught:
            self.experiments.propose(
                {
                    "recommendation_id": "rec-000000000000",
                    "game_id": "disposable-window",
                    "scenario_id": "observation-baseline",
                    "target_title": TARGET,
                },
                coach=_StubCoach(),
            )
        self.assertIn("operator change", str(caught.exception))

    async def test_an_unknown_recommendation_id_is_not_found(self) -> None:
        class _StubCoach:
            def analyse(self, **_: Any) -> dict[str, Any]:
                return {"subject": {"session_id": "s1"}, "recommendations": []}

        with self.assertRaises(ExperimentNotFoundError):
            self.experiments.propose(
                {"recommendation_id": "rec-nope", "session_id": "s1"},
                coach=_StubCoach(),
            )

    async def test_a_proposal_without_a_coach_cannot_resolve_a_recommendation(self) -> None:
        with self.assertRaises(ExperimentError):
            self.experiments.propose({"recommendation_id": "rec-anything"})


class ExperimentApprovalTests(_CoachFixture):
    async def _proposed(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "game_id": "disposable-window",
            "scenario_id": "observation-baseline",
            "target_title": TARGET,
            "kind": "repeat",
            "variable": "none",
            "primary_metric": "frames",
            "repeat_count": 1,
        }
        payload.update(overrides)
        proposal = self.experiments.propose(payload)
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        return proposal

    async def test_running_without_approval_is_refused(self) -> None:
        proposal = await self._proposed()
        with self.assertRaises(ExperimentError) as caught:
            await self.experiments.run(proposal["id"])
        self.assertIn("must be approved", str(caught.exception))
        self.assertEqual(self.experiments.get(proposal["id"])["status"], "proposed")

    async def test_approval_is_bound_to_the_configuration_and_invalidated_by_a_change(
        self,
    ) -> None:
        proposal = await self._proposed()
        approved = await self.experiments.approve(proposal["id"], note="ok")
        digest = approved["approval"]["spec_digest"]
        self.assertEqual(digest, approved["spec"]["digest"])

        # A tampered/re-edited spec is exactly what "the configuration changed"
        # means; the stored approval no longer authorizes it.
        self.experiments._experiments[proposal["id"]]["spec"]["repeat_count"] = 3
        with self.assertRaises(ExperimentError) as caught:
            await self.experiments.run(proposal["id"])
        self.assertIn("changed after it was approved", str(caught.exception))

    async def test_a_terminal_or_running_experiment_cannot_be_approved(self) -> None:
        proposal = await self._proposed()
        await self.experiments.reject(proposal["id"], note="not worth it")
        with self.assertRaises(ExperimentError):
            await self.experiments.approve(proposal["id"])
        self.assertEqual(
            [item["status"] for item in self.experiments.get(proposal["id"])["history"]],
            ["proposed", "rejected"],
        )

    async def test_a_rejected_experiment_is_never_run(self) -> None:
        proposal = await self._proposed()
        await self.experiments.reject(proposal["id"], note="no")
        with self.assertRaises(ExperimentError):
            await self.experiments.run(proposal["id"])

    async def test_a_readiness_refusal_blocks_the_run_without_claiming_execution(self) -> None:
        proposal = await self._proposed()
        await self.experiments.approve(proposal["id"])
        self.desktop.window = None
        blocked = await self.experiments.run(proposal["id"])
        self.assertEqual(blocked["status"], "blocked")
        # The refusal is the revalidated preflight's own verdict: not ready, with
        # the checks that failed named, and a reason the operator can act on.
        self.assertFalse(blocked["preflight"]["ready"])
        self.assertIn("target_identity", blocked["preflight"]["blocking"])
        self.assertIn("no open window", json.dumps(blocked["preflight"]).lower())
        # Nothing was executed, so there is nothing to hold a result.
        self.assertIsNone(blocked["results"])
        self.assertEqual(blocked["arms"]["baseline"], [])
        self.assertEqual(blocked["evaluations"], [])

    async def test_a_permission_refusal_blocks_the_run(self) -> None:
        proposal = await self._proposed()
        await self.experiments.approve(proposal["id"])
        self.experiments.permission = lambda: {
            "checked": True, "allowed": False, "reason": "policy says no",
        }
        blocked = await self.experiments.run(proposal["id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("policy says no", blocked["reason"])

    async def test_a_readiness_refusal_does_not_revoke_the_approval(self) -> None:
        proposal = await self._proposed()
        await self.experiments.approve(proposal["id"], note="fixture approval")
        window = self.desktop.window
        self.assertIsNotNone(window)
        self.desktop.window = None
        try:
            blocked = await self.experiments.run(proposal["id"])
        finally:
            self.desktop.window = window
        self.assertEqual(blocked["status"], "blocked")
        # The refusal is the last ATTEMPT, not a revoked decision: the approval
        # still binds the same configuration, so the run is refused while the
        # window is gone and allowed again once it is back.
        self.assertEqual(blocked["approval"]["spec_digest"], blocked["spec"]["digest"])

        retried = await self.experiments.run(proposal["id"])
        self.assertEqual(retried["status"], "running")
        finished = await self.experiments.wait(str(proposal["id"]), timeout=90.0)
        self.assertEqual(finished["status"], "completed", finished["reason"])
        statuses = [str(item.get("status")) for item in finished["history"]]
        self.assertEqual(statuses[-1], "completed")
        self.assertEqual(statuses.count("blocked"), 1)
        # The refusal is recorded where it happened, before the retry.
        self.assertGreater(statuses.index("blocked"), statuses.index("approved"))
        last_running = max(index for index, status in enumerate(statuses) if status == "running")
        self.assertGreater(last_running, statuses.index("blocked"))

    async def test_a_proposal_blocked_for_its_own_errors_is_never_retried(self) -> None:
        blocked = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "change": {"frame_interval_seconds": 0.1},
            }
        )
        self.assertEqual(blocked["status"], "blocked")
        with self.assertRaises(ExperimentError) as caught:
            await self.experiments.run(blocked["id"])
        self.assertIn("cannot run until its reasons are resolved", str(caught.exception))

    async def test_only_one_experiment_runs_at_a_time(self) -> None:
        first = await self._proposed(duration_seconds=3, primary_metric="frames")
        second = await self._proposed(duration_seconds=1, primary_metric="frames")
        await self.experiments.approve(first["id"])
        await self.experiments.approve(second["id"])
        try:
            await self.experiments.run(first["id"])
            with self.assertRaises(ExperimentConflictError):
                await self.experiments.run(second["id"])
        finally:
            await self.experiments.cancel(first["id"], reason="fixture teardown")
            await self.experiments.wait(first["id"], timeout=30)


class ExperimentExecutionTests(_CoachFixture):
    async def test_a_repeat_runs_both_arms_and_reports_repeatability_not_an_effect(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 2,
            }
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        finished = await self._run_experiment(str(proposal["id"]))
        results = finished["results"]

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(results["sample"]["baseline"], 2)
        self.assertEqual(results["sample"]["candidate"], 2)
        self.assertEqual(results["sample"]["eligible_baseline"], 2)
        self.assertEqual(results["sample"]["eligible_candidate"], 2)
        self.assertEqual(results["verdict"]["label"], "inconclusive")
        self.assertIn("declares no variable", results["verdict"]["reason"])
        self.assertFalse(results["verdict"]["improvement_claimed"])
        self.assertFalse(results["verdict"]["gameplay_claimed"])
        self.assertFalse(results["verdict"]["objective_verified"])
        self.assertEqual(results["primary"]["source"], "nf04_comparison")
        self.assertTrue(results["primary"]["certified"])
        self.assertIsNotNone(results["primary"]["delta"])
        self.assertEqual(len(finished["evaluations"]), 4)
        self.assertEqual(
            [item["status"] for item in finished["evaluations"]],
            ["completed"] * 4,
        )

    async def test_a_single_run_per_side_is_limited_and_never_improved(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        finished = await self._run_experiment(str(proposal["id"]))
        verdict = finished["results"]["verdict"]
        self.assertEqual(verdict["label"], "inconclusive")
        self.assertTrue(verdict["limited_to_single_run"])
        self.assertNotEqual(verdict["label"], "improved")

    async def test_a_configuration_change_reports_not_comparable_with_nf04_reasons(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "capture-latency",
                "target_title": TARGET,
                "kind": "configuration_change",
                "variable": "frame_interval_seconds",
                "change": {"frame_interval_seconds": 0.1},
                "baseline_duration_seconds": 4,
                "baseline_frame_interval_seconds": 0.05,
                # `frames` is measured by every runnable scenario, so both arms
                # have a figure and "not comparable" can only be about the delta.
                # The budget must clear the scenario's 20-frame validity floor on
                # a slow machine: 2s recorded only 17 frames here and invalidated
                # the arm, which is a property of the host, not of the comparison.
                "primary_metric": "frames",
                "repeat_count": 1,
                "guardrails": [],
            }
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        finished = await self._run_experiment(str(proposal["id"]), timeout=120.0)
        results = finished["results"]

        self.assertEqual(finished["status"], "completed", finished["reason"])
        self.assertEqual(results["verdict"]["label"], "not_comparable")
        self.assertFalse(results["verdict"]["improvement_claimed"])
        self.assertTrue(results["comparison"]["reasons"])
        self.assertIn("observation interval", " ".join(results["comparison"]["reasons"]))
        # The figures are still shown, but labelled as descriptions.
        self.assertEqual(results["primary"]["source"], "per_arm_description")
        self.assertFalse(results["primary"]["certified"])
        self.assertIn("not an effect", results["primary"]["reason"])
        # Each arm is reported on its own. A side that could not be measured says
        # so with its reason — the row never turns an absent figure into a zero.
        self.assertIsNotNone(results["primary"]["baseline"].get("value"))
        for side in ("baseline", "candidate"):
            payload = results["primary"][side]
            if payload.get("value") is None:
                with self.subTest(side=side):
                    self.assertFalse(payload.get("available"))
                    self.assertTrue(payload.get("reason"))
                    self.assertEqual(payload.get("values"), [])
        self.assertFalse(results["verdict"]["gameplay_claimed"])
        self.assertFalse(results["verdict"]["objective_verified"])
        self.assertIn(
            "different run configurations",
            " ".join(results["limitations"]),
        )

    async def test_an_invalidated_attempt_is_not_a_second_measurement(self) -> None:
        # Two tries with only one valid run is ONE measurement: the guard has
        # to count eligible runs, or a falsified arm can claim an effect.
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "configuration_change",
                "variable": "frame_interval_seconds",
                "change": {"frame_interval_seconds": 0.1},
                "baseline_frame_interval_seconds": 0.05,
                "baseline_duration_seconds": 2,
                "primary_metric": "frames",
                "repeat_count": 2,
            }
        )
        self.assertEqual(proposal["status"], "proposed", proposal["errors"])
        spec = ExperimentSpec.from_dict(proposal["spec"])
        label, reason, limited = self.experiments._verdict(
            spec=spec,
            primary={"delta": 25.0},
            compatible=True,
            reasons=[],
            sample={
                "baseline": 2,
                "candidate": 2,
                "eligible_baseline": 1,
                "eligible_candidate": 2,
            },
            guardrails=[],
        )
        self.assertEqual(label, "inconclusive")
        self.assertTrue(limited)
        self.assertIn("eligible", reason)

        # With two eligible runs on each side the threshold gate can speak.
        label, _reason, limited = self.experiments._verdict(
            spec=spec,
            primary={"delta": 25.0},
            compatible=True,
            reasons=[],
            sample={
                "baseline": 2,
                "candidate": 2,
                "eligible_baseline": 2,
                "eligible_candidate": 2,
            },
            guardrails=[],
        )
        self.assertFalse(limited)
        self.assertIn(label, {"improved", "regressed"})

    async def test_a_guardrail_breach_is_a_regression_even_with_a_favourable_delta(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 2,
            }
        )
        finished = await self._run_experiment(str(proposal["id"]))
        spec = ExperimentSpec.from_dict(finished["spec"])
        breaching = replace(
            spec,
            guardrails=(
                {"metric": "frames", "direction": "min", "bound": 10_000.0},
            ),
        )
        results = self.experiments.assess(breaching, finished)
        self.assertEqual(results["verdict"]["label"], "regressed")
        self.assertIn("guardrail was breached", results["verdict"]["reason"])
        self.assertEqual(results["guardrails"][0]["passed"], False)
        self.assertFalse(results["verdict"]["improvement_claimed"])

    async def test_a_guardrail_the_arm_never_measured_does_not_pass_silently(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
                "guardrails": [
                    {"metric": "stale_frames", "direction": "max", "bound": 0}
                ],
            }
        )
        self.assertEqual(proposal["status"], "blocked")
        self.assertIn("does not measure", " ".join(proposal["errors"]))

    async def test_a_partial_failure_is_listed_and_never_pooled_as_success(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        await self.experiments.approve(proposal["id"])
        await self.experiments.run(proposal["id"])
        # Break the capture seam after the baseline arm started its run.
        self.desktop.fail = True
        finished = await self.experiments.wait(str(proposal["id"]), timeout=60)
        results = finished["results"]
        self.assertNotEqual(results["verdict"]["label"], "improved")
        if results["failed_runs"]:
            self.assertTrue(all(item["status"] != "completed" for item in results["failed_runs"]))
            self.assertTrue(
                any("did not complete" in line for line in results["limitations"])
            )

    async def test_cancelling_before_the_run_marks_it_cancelled_and_blocks_execution(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        await self.experiments.approve(proposal["id"])
        cancelled = await self.experiments.cancel(proposal["id"], reason="operator stop")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("operator stop", cancelled["reason"])
        with self.assertRaises(ExperimentError):
            await self.experiments.run(proposal["id"])
        again = await self.experiments.cancel(proposal["id"])
        self.assertTrue(again["already_stopped"])

    async def test_cancelling_during_a_run_stops_the_arm_and_records_it(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 2,
                "baseline_duration_seconds": 4,
            }
        )
        await self.experiments.approve(proposal["id"])
        await self.experiments.run(proposal["id"])
        await asyncio.sleep(0.4)
        await self.experiments.cancel(proposal["id"], reason="operator stop")
        finished = await self.experiments.wait(str(proposal["id"]), timeout=45)
        self.assertEqual(finished["status"], "cancelled")
        self.assertIn("operator stop", finished["reason"])
        self.assertTrue(
            any(
                item["status"] == "cancelled"
                for item in finished["evaluations"]
            ),
            finished["evaluations"],
        )

    async def test_history_survives_a_restart_and_a_running_record_is_not_resumed(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        stored = self.store.read(EXPERIMENTS_NAMESPACE)
        record = stored["experiments"][0]
        self.assertEqual(record["id"], proposal["id"])
        record["status"] = "running"
        record["arms"]["baseline"] = [
            {"arm": "baseline", "index": 0, "status": "not_started"}
        ]
        self.store.write(EXPERIMENTS_NAMESPACE, stored)

        reopened = NovaForgeExperimentLab(
            evaluations=self.evaluations, store=self.store
        )
        recovered = reopened.get(proposal["id"])
        assert recovered is not None
        self.assertEqual(recovered["status"], "failed")
        self.assertTrue(recovered["interrupted"])
        self.assertIn("restarted", recovered["reason"])
        self.assertEqual(recovered["history"][-1]["status"], "failed")

    async def test_an_experiment_cannot_be_rerun_after_it_finished(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        finished = await self._run_experiment(str(proposal["id"]))
        self.assertEqual(finished["status"], "completed")
        with self.assertRaises(ExperimentError):
            await self.experiments.run(proposal["id"])
        with self.assertRaises(ExperimentError):
            await self.experiments.approve(proposal["id"])

    async def test_the_status_card_states_what_is_unsupported(self) -> None:
        status = self.experiments.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["mode"], "observation_only")
        self.assertTrue(status["approval"]["required"])
        unsupported = {item["variable"] for item in status["unsupported"]}
        self.assertEqual(unsupported, {"model_id", "policy_id"})
        self.assertIn("not_comparable", status["comparability"])
        # Nothing on this lab can train, tune or apply a configuration.
        for forbidden in ("train", "train_model", "apply_configuration", "set_model", "optimize"):
            self.assertFalse(hasattr(self.experiments, forbidden), forbidden)

    async def test_results_state_that_no_gameplay_improvement_is_claimed(self) -> None:
        proposal = self.experiments.propose(
            {
                "game_id": "disposable-window",
                "scenario_id": "observation-baseline",
                "target_title": TARGET,
                "kind": "repeat",
                "variable": "none",
                "primary_metric": "frames",
                "repeat_count": 1,
            }
        )
        finished = await self._run_experiment(str(proposal["id"]))
        verdict = finished["results"]["verdict"]
        self.assertFalse(verdict["gameplay_claimed"])
        self.assertFalse(verdict["objective_verified"])
        self.assertIn("observation quality", " ".join(finished["results"]["limitations"]))
        self.assertFalse(finished["results"]["declared_baseline"]["pooled"])


if __name__ == "__main__":
    unittest.main()
