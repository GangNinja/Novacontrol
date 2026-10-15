"""Phase 23 workflows A-M through the REAL application surface.

These are not unit tests of the manager. Each one starts the actual FastAPI app
(``create_app()``) over an isolated data directory and drives it over HTTP, which is
the interface a user or a CLI reaches: ``POST /learning/objectives`` then
``POST /learning/objectives/{id}/run``, and read the record back. That is the only
thing that proves the endpoints, the application methods, the policy wiring and the
persistence agree with each other — an internal call could pass while the route
422s.

A simulator is used wherever a workflow needs to be reproducible, and every
assertion that touches a result ALSO checks that the response says it came from a
simulator: this build must never let a synthetic result read as real-world evidence.

Two later workflows do their setup IN-PROCESS, because their subject is a
non-simulator environment: ``POST /learning/environments`` deliberately accepts only
simulators declared as data, so a real environment is the EMBEDDER's door (§10).
Workflow L registers one on the live application's registry and then drives the same
HTTP surface, and workflow M writes a corrupt record into the real state store to
prove a restart survives one.
"""

from __future__ import annotations

import time
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:  # pragma: no cover - import guard mirrors the rest of the suite
    from fastapi.testclient import TestClient

    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
except Exception:  # pragma: no cover - httpx/fastapi missing
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    NovaControlApplication = None  # type: ignore[assignment]

from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.interactive import BridgedEnvironment


class _RecordingExecutor:
    """The executor a real environment acts through, recording what it was asked."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.values = {"flag": "up"} if values is None else dict(values)

    async def __call__(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, dict(parameters)))
        return dict(self.values)


def _real_environment(
    environment_id: str, executor: _RecordingExecutor, **overrides: Any
) -> BridgedEnvironment:
    """A real environment whose actions are declared, with no rule of its own."""
    action = str(overrides.pop("action", "wave"))
    spec: dict[str, Any] = {
        "name": action,
        "description": f"{action} the device.",
        "estimated_seconds": 0.01,
        **overrides,
    }
    return BridgedEnvironment(
        environment_id,
        action_specs=[spec],
        executor=executor,
        observer=lambda: {"flag": "down"},
        candidates={action: ({"flag": "up"}, {"flag": "down"})},
    )


def _dial_body() -> dict[str, Any]:
    return {"question": "What does turning the dial do to the level?", "environment_id": "sim.dial"}


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class _LearningWorkflowCase(unittest.TestCase):
    """One real application per test, over a temp data dir, with no device runners."""

    def setUp(self) -> None:
        if TestClient is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
        self._nova: Any = None
        self._patchers = [
            mock.patch(
                "novacontrol.api.app.NovaControlApplication",
                side_effect=self._isolated_application,
            ),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self._client = TestClient(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> Any:
        kwargs["data_dir"] = self._tmp.name
        app = NovaControlApplication(**kwargs)  # type: ignore[arg-type]
        self._nova = app
        return app

    # ── helpers ──────────────────────────────────────────────────────────
    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/learning/objectives", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _run(self, objective_id: str, **body: Any) -> dict[str, Any]:
        response = self._client.post(f"/learning/objectives/{objective_id}/run", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _steps(self, cycle: dict[str, Any]) -> list[str]:
        return [str(item["kind"]) for item in cycle.get("steps") or []]

    def _action_events(self) -> list[str]:
        """Every learning event the REAL bus recorded, in order.

        The application schedules its announcements onto the event loop, so a
        synchronous read can arrive before the last one lands. Wait (bounded) for the
        history to stop growing rather than racing it — a slow event is waited for; a
        genuinely missing one still fails.
        """
        deadline = time.monotonic() + 2.0
        recent = self._nova.event_bus.recent(limit=200)
        while time.monotonic() < deadline:
            time.sleep(0.02)
            current = self._nova.event_bus.recent(limit=200)
            if len(current) == len(recent):
                break
            recent = current
        return [str(event.type) for event in recent if str(event.type).startswith("learning.")]


class WorkflowASimpleTransitionTests(_LearningWorkflowCase):
    """A: learn an unknown state transition from observations."""

    def test_an_unknown_transition_is_learned_and_recorded_with_its_evidence(self) -> None:
        objective = self._create(_dial_body())
        self.assertEqual(objective["status"], "open")
        self.assertEqual(objective["environment_id"], "sim.dial")

        body = self._run(objective["objective_id"])
        cycle = body["cycle"]

        # Observed the initial state, proposed explanations, ran bounded experiments,
        # observed results and recorded an evidence-backed conclusion.
        kinds = self._steps(cycle)
        self.assertIn("observe", kinds)
        self.assertIn("form_hypothesis", kinds)
        self.assertIn("select_experiment", kinds)
        self.assertIn("check_safety", kinds)
        self.assertIn("observe_result", kinds)
        self.assertIn("compare", kinds)
        self.assertIn("update_knowledge", kinds)
        self.assertIn("evaluate", kinds)

        self.assertEqual(cycle["status"], "achieved")
        self.assertTrue(cycle["simulated"], "a simulator result must say so")
        self.assertEqual(len(body["runs"]), 3, "three independent observations, not one")
        self.assertEqual(len(body["rules"]), 1)
        self.assertEqual(body["rules"][0]["status"], "provisional")
        self.assertIn("independent observation", cycle["stop_reason"])

        statuses = {item["status"] for item in body["hypotheses"]}
        self.assertIn("supported", statuses)
        self.assertIn("contradicted", statuses)

    def test_the_record_survives_a_restart_from_the_same_data_dir(self) -> None:
        objective = self._create(_dial_body())
        self._run(objective["objective_id"])
        # The app persists on stop(); the TestClient context manager drives lifespan.
        self._client.__exit__(None, None, None)
        self._client = TestClient(create_app())
        self._client.__enter__()

        status = self._client.get("/learning/status").json()
        self.assertGreaterEqual(status["restored"]["objectives"], 1)
        objectives = self._client.get("/learning/objectives").json()["objectives"]
        self.assertEqual(len(objectives), 1)
        self.assertEqual(objectives[0]["status"], "achieved")
        # The LEARNED knowledge is durable too: the rule comes back and answers a new
        # question with no experiment — a restart that forgot it would make the record
        # survive while the learning did not.
        self.assertEqual(len(status["rules"]), 1, status["rules"])
        self.assertEqual(status["rules"][0]["status"], "provisional")
        reused = self._create(
            {"question": "what does turning the dial do?", "environment_id": "sim.dial"}
        )
        after = self._run(reused["objective_id"])
        self.assertEqual(after["runs"], [])
        self.assertEqual(after["cycle"]["status"], "achieved")

    def test_rules_from_several_objectives_merge_on_restart(self) -> None:
        """A payload written before a later rule must not erase that rule on restore."""
        spec = {
            "environment_id": "sim.knob",
            "initial_state": {"setting": 0},
            "rules": {"twist": {"setting": 5}},
            "hidden": ["twist"],
            "candidates": {"twist": [{"setting": 5}, {"setting": 0}]},
            "actions": {
                "twist": {
                    "description": "Twist the knob.",
                    "estimated_seconds": 0.01,
                    "parameters": ["replicate"],
                }
            },
        }
        self.assertEqual(self._client.post("/learning/environments", json=spec).status_code, 200)
        earlier = self._create(
            {"question": "what does twisting the knob do?", "environment_id": "sim.knob"}
        )
        later = self._create(_dial_body())
        self._run(later["objective_id"])  # dial learned; its payload has only dial
        self._run(earlier["objective_id"])  # knob learned; this payload has both
        # Restore walks creation order, so the earlier objective's payload (both
        # rules) is read first and the later one's (dial only) last.
        self._client.__exit__(None, None, None)
        self._client = TestClient(create_app())
        self._client.__enter__()

        status = self._client.get("/learning/status").json()
        self.assertEqual(len(status["rules"]), 2, status["rules"])
        # A simulator declared over the API is a SESSION declaration, so a caller that
        # wants to keep asking about it re-declares it; what persisted on its own is
        # the record the capability table names — objectives, hypotheses, experiments,
        # runs, evidence references, budget usage and rules.
        self.assertEqual(self._client.post("/learning/environments", json=spec).status_code, 200)
        for question, environment in (
            ("what does turning the dial do?", "sim.dial"),
            ("what does twisting the knob do?", "sim.knob"),
        ):
            objective = self._create({"question": question, "environment_id": environment})
            body = self._run(objective["objective_id"])
            self.assertEqual(body["runs"], [], question)
            self.assertEqual(body["cycle"]["status"], "achieved", question)


class WorkflowBCompetingHypothesesTests(_LearningWorkflowCase):
    """B: two explanations that predict different results for the same action."""

    def test_one_experiment_distinguishes_competing_hypotheses(self) -> None:
        objective = self._create(
            {
                "question": "Does pressing the switch turn the lamp on?",
                "environment_id": "sim.switch",
            }
        )
        body = self._run(objective["objective_id"])

        hypotheses = body["hypotheses"]
        self.assertEqual(len(hypotheses), 2)
        by_status = {item["status"]: item for item in hypotheses}
        self.assertEqual(set(by_status), {"supported", "contradicted"})
        # The relative support CHANGED: one holds, the other is contradicted.
        self.assertEqual(by_status["supported"]["independent_support"], 1)
        self.assertEqual(by_status["contradicted"]["independent_contradiction"], 1)
        # One decisive experiment was enough, and it was the action that could tell
        # them apart.
        self.assertEqual(len(body["runs"]), 1)
        self.assertEqual(body["runs"][0]["experiment_id"][:4], "exp_")
        self.assertEqual(body["rules"], [])

    def test_the_preview_names_the_action_without_executing_anything(self) -> None:
        objective = self._create(
            {"question": "what does pressing do?", "environment_id": "sim.switch"}
        )
        response = self._client.post(
            f"/learning/objectives/{objective['objective_id']}/preview", json={}
        )
        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertFalse(preview["executed"])
        self.assertIn(preview["chosen"]["experiment"]["action"], {"press"})
        self.assertEqual(len(preview["candidates"]), 1)
        # Nothing ran: the environment never executed the action.
        environment = self._nova.learning.environments.get("sim.switch")
        self.assertEqual(environment.executed, 0)  # type: ignore[attr-defined]


class WorkflowCFailedPredictionTests(_LearningWorkflowCase):
    """C: the environment contradicts the prediction it declared."""

    def test_a_contradicted_prediction_is_preserved_and_not_reported_as_success(self) -> None:
        objective = self._create(
            {
                "question": "What does charging do to the battery?",
                "environment_id": "sim.misleading",
            }
        )
        body = self._run(objective["objective_id"])
        run = body["runs"][0]

        # The environment declared a rule, so a real prediction existed and was
        # compared - not fabricated, and not silently dropped.
        self.assertEqual(run["prediction"]["basis"], "environment_rule")
        self.assertEqual(run["prediction"]["expected"], {"battery": 100})
        self.assertEqual(run["comparison"]["kind"], "contradicted")
        self.assertEqual(run["observation"]["values"], {"battery": 0})

        # The hypothesis that matched the declared rule is contradicted; the
        # observation is what the record believes.
        by_status = {item["status"]: item for item in body["hypotheses"]}
        self.assertIn("contradicted", by_status)
        self.assertIn("supported", by_status)
        # No rule is learned from a contradicted expectation.
        self.assertEqual(body["rules"], [])


class WorkflowDIncompleteObservationTests(_LearningWorkflowCase):
    """D: a partial reading must not be invented into an outcome."""

    def test_an_incomplete_observation_is_inconclusive_and_stops_honestly(self) -> None:
        objective = self._create(
            {"question": "What does scanning do to the reading?", "environment_id": "sim.blind"}
        )
        body = self._run(objective["objective_id"])

        self.assertEqual(body["cycle"]["status"], "unjustified")
        self.assertEqual(len(body["runs"]), 2, "a bounded re-observation, then stop")
        for run in body["runs"]:
            self.assertFalse(run["observation"]["complete"])
            self.assertNotIn("reading", run["observation"]["values"])
            self.assertIn(run["comparison"]["kind"], {"inconclusive", "unable_to_evaluate"})
        for hypothesis in body["hypotheses"]:
            self.assertNotIn(hypothesis["status"], {"supported", "contradicted"})
        self.assertEqual(body["rules"], [])
        self.assertIn("no verdict", body["cycle"]["stop_reason"])

    def test_a_stop_is_still_an_event_and_not_a_silent_return(self) -> None:
        objective = self._create(
            {"question": "What does scanning do to the reading?", "environment_id": "sim.blind"}
        )
        self._run(objective["objective_id"])
        events = self._action_events()
        self.assertIn("learning.experiment_started", events)
        self.assertIn("learning.experiment_completed", events)
        self.assertIn("learning.hypothesis_updated", events)


class WorkflowERedundantExplorationTests(_LearningWorkflowCase):
    """E: existing evidence that already answers the question."""

    def test_a_learned_rule_answers_the_next_question_with_no_experiment(self) -> None:
        first = self._create(_dial_body())
        self._run(first["objective_id"])
        second = self._create(
            {"question": "remind me what turning the dial does", "environment_id": "sim.dial"}
        )
        body = self._run(second["objective_id"])

        self.assertEqual(body["cycle"]["status"], "achieved")
        self.assertEqual(body["runs"], [], "no experiment was run for an answered question")
        self.assertIn("no experiment was run", body["cycle"]["stop_reason"])
        self.assertIn("answered from the learned rule", body["cycle"]["stop_reason"])

    def test_an_answered_intervention_is_refused_rather_than_repeated(self) -> None:
        objective = self._create(_dial_body())
        first = self._run(objective["objective_id"])
        self.assertEqual(len(first["runs"]), 3)

        # A second run of the SAME objective cannot repeat an answered intervention
        # (its action declares replicates, so it may only re-run as a NEW replicate,
        # and the cycle's own bound has been reached).
        second = self._run(objective["objective_id"], max_experiments=1)
        self.assertEqual(
            len(second["runs"]), len(first["runs"]), "a terminal objective runs nothing more"
        )
        self.assertIn("already", second["cycle"]["stop_reason"])
        self.assertEqual(second["cycle"]["metrics"]["rules"]["total"], 1)
        # The answer is reusable: a NEW objective on the same action costs nothing.
        third = self._create(
            {"question": "what does the dial do again?", "environment_id": "sim.dial"}
        )
        reused = self._run(third["objective_id"])
        self.assertEqual(reused["runs"], [])
        metrics = self._client.get("/learning/metrics").json()
        self.assertIn("duplicate_experiments", metrics)


class WorkflowFSafetyAndBudgetTests(_LearningWorkflowCase):
    """F: a refusal happens BEFORE the action, is recorded, and is not a completion."""

    def _destructive_simulator(self) -> str:
        spec = {
            "environment_id": "sim.wipe",
            "initial_state": {"data": "present"},
            "rules": {"wipe": {"data": "gone"}},
            "candidates": {"wipe": [{"data": "gone"}, {"data": "present"}]},
            "actions": {
                "wipe": {
                    "description": "Wipe the data.",
                    "destructive": True,
                    "reversible": False,
                }
            },
        }
        response = self._client.post("/learning/environments", json=spec)
        self.assertEqual(response.status_code, 200, response.text)
        return str(response.json()["environment_id"])

    def test_a_destructive_experiment_is_refused_before_it_can_run(self) -> None:
        environment_id = self._destructive_simulator()
        objective = self._create(
            {"question": "what does wiping do?", "environment_id": environment_id}
        )
        body = self._run(objective["objective_id"])
        cycle = body["cycle"]

        self.assertEqual(cycle["status"], "stopped")
        self.assertIn("refused", cycle["stop_reason"])
        self.assertEqual(body["runs"], [], "nothing ran")
        # The refusal is RECORDED, with its reason, as a rejected experiment.
        rejected = [item for item in body["experiments"] if item["status"] == "rejected"]
        self.assertTrue(rejected, body["experiments"])
        self.assertIn("approval", rejected[0]["rejection_reason"])
        # And no completion was announced for it.
        events = self._action_events()
        self.assertNotIn("learning.experiment_completed", events)
        self.assertIn("learning.experiment_rejected", events)
        environment = self._nova.learning.environments.get(environment_id)
        self.assertEqual(environment.executed, 0)  # type: ignore[attr-defined]

    def test_a_budget_ceiling_stops_the_cycle_before_a_further_action(self) -> None:
        objective = self._create({**_dial_body(), "budget": {"max_experiments": 1}})
        body = self._run(objective["objective_id"], max_experiments=6)

        self.assertEqual(body["cycle"]["status"], "exhausted")
        self.assertEqual(len(body["runs"]), 1, "exactly the one experiment that was afforded")
        self.assertIn("ceiling", body["cycle"]["stop_reason"])
        self.assertEqual(body["cycle"]["metrics"]["usage"]["experiments"], 1)

    def test_an_unknown_environment_is_refused_at_creation(self) -> None:
        response = self._client.post(
            "/learning/objectives", json={"question": "what?", "environment_id": "sim.nope"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("sim.nope", response.json()["detail"])
        self.assertIn("sim.dial", response.json()["detail"])


class WorkflowGRecoveryTests(_LearningWorkflowCase):
    """G: an interrupted experiment is never completed by a restart."""

    def test_the_resumable_gate_is_honest_before_anything_is_interrupted(self) -> None:
        objective = self._create(_dial_body())
        self._run(objective["objective_id"])
        gate = self._client.get(
            f"/learning/objectives/{objective['objective_id']}/resumable"
        ).json()
        self.assertFalse(gate["resumable"])
        self.assertIn("no interrupted experiment", gate["reason"])

    def test_resuming_an_achieved_objective_returns_the_record_and_not_a_rerun(self) -> None:
        objective = self._create(_dial_body())
        first = self._run(objective["objective_id"])
        response = self._client.post(
            f"/learning/objectives/{objective['objective_id']}/resume", json={}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["cycle"]["metrics"].get("resumed", True))
        self.assertEqual(len(body["runs"]), len(first["runs"]), "resume ran nothing")


class WorkflowHProvenanceTests(_LearningWorkflowCase):
    """H: a conclusion traced back to the observations behind it."""

    def test_evidence_names_its_role_kind_and_the_experiment_behind_it(self) -> None:
        objective = self._create(_dial_body())
        body = self._run(objective["objective_id"])
        evidence = body["evidence"]
        self.assertTrue(evidence)

        observations = [item for item in evidence if item["kind"] == "observation"]
        predictions = [item for item in evidence if item["kind"] == "prediction"]
        self.assertTrue(observations, "an observation must be recorded")
        self.assertTrue(predictions, "the expectation is kept, and kept SEPARATE")

        trace = self._client.get(f"/learning/evidence/{observations[0]['evidence_id']}")
        self.assertEqual(trace.status_code, 200)
        traced = trace.json()
        self.assertEqual(traced["evidence"]["kind"], "observation")
        self.assertIn("limitations", traced)
        # The prediction is never counted as support for the hypothesis that made it.
        self.assertNotEqual(observations[0]["evidence_id"], predictions[0]["evidence_id"])
        for item in evidence:
            self.assertIn(
                item["role"],
                {"supports", "contradicts", "inconclusive"},
            )
        unknown = self._client.get("/learning/evidence/ev_not_a_real_id")
        self.assertEqual(unknown.status_code, 404)


class WorkflowILearningImprovesTests(_LearningWorkflowCase):
    """I: performance before and after the rule, on cases the caller held out."""

    def test_the_same_question_costs_fewer_experiments_once_a_rule_exists(self) -> None:
        first = self._create(_dial_body())
        learned = self._run(first["objective_id"])
        experiments_before = len(learned["runs"])
        rule_id = learned["rules"][0]["rule_id"]

        second = self._create(
            {
                "question": "what does turning the dial do to the level?",
                "environment_id": "sim.dial",
            }
        )
        after = self._run(second["objective_id"])
        experiments_after = len(after["runs"])
        self.assertEqual(experiments_after, 0)
        self.assertLess(experiments_after, experiments_before)

        response = self._client.post(
            "/learning/transfer",
            json={
                "rule_id": rule_id,
                "held_out": [
                    {"action": "turn", "expected": {"level": 3}},
                    {"action": "turn", "expected": {"level": 9}},
                ],
                "experiments_before": experiments_before,
                "experiments_after": experiments_after,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        transfer = response.json()
        self.assertTrue(transfer["held_out"])
        self.assertEqual(transfer["cases"], 2)
        self.assertEqual(transfer["agreed"], 1)
        self.assertEqual(transfer["agreement_rate"], 0.5)
        self.assertEqual(transfer["improvement"], 3.0)
        self.assertIn("fewer experiments", transfer["claim"])
        self.assertTrue(any("{'level': 9}" in text for text in transfer["mismatches"]))
        # The honest limit: this build cannot verify the cases were held out.
        self.assertTrue(
            any("cannot verify" in text for text in transfer["limitations"]),
            transfer["limitations"],
        )

    def test_a_transfer_check_needs_a_rule_and_cases(self) -> None:
        self.assertEqual(
            self._client.post(
                "/learning/transfer",
                json={"rule_id": "rule:nope", "held_out": [{"action": "turn"}]},
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post("/learning/transfer", json={"held_out": []}).status_code, 422
        )


class WorkflowJProviderUnavailabilityTests(_LearningWorkflowCase):
    """J: without an optional provider, the supported work still works and the rest is honest."""

    def test_prediction_is_reported_honestly_and_deterministic_learning_still_works(self) -> None:
        status = self._client.get("/learning/status").json()
        providers = {row["name"]: row for row in status["provider_report"]}
        self.assertTrue(providers["learned_rule"]["available"], providers)
        self.assertFalse(providers["world_model"]["available"], providers)
        # The fallback provider is honest about itself: it can never predict.
        self.assertFalse(providers["none"]["available"], providers)

        capabilities = {row["name"]: row for row in status["capabilities"]["capabilities"]}
        # No learned world model and no installed provider: the honest answer, not a
        # plausible-looking one.
        self.assertEqual(capabilities["interactive.prediction"]["state"], "provider_dependent")
        self.assertEqual(capabilities["interactive.model_training"]["state"], "future")
        self.assertFalse(
            capabilities["interactive.model_training"]["available"],
            "a deferred capability must not read as available",
        )
        self.assertFalse(
            capabilities["interactive.real_environment_actions"]["available"],
            "no environment in this build can act; saying otherwise would overstate it",
        )

        # Deterministic work still works with no provider at all.
        objective = self._create(_dial_body())
        body = self._run(objective["objective_id"])
        self.assertEqual(body["cycle"]["status"], "achieved")
        self.assertTrue(body["cycle"]["simulated"])

    def test_an_environment_that_declares_nothing_is_reported_rather_than_guessed(self) -> None:
        objective = self._create(
            {"question": "What does poking do to the gauge?", "environment_id": "sim.opaque"}
        )
        body = self._run(objective["objective_id"])
        self.assertEqual(body["runs"], [])
        self.assertEqual(body["hypotheses"], [])
        self.assertIn("no testable hypothesis", body["cycle"]["stop_reason"])

    def test_the_world_environment_is_registered_read_only(self) -> None:
        environments = self._client.get("/learning/environments").json()["environments"]
        world = [item for item in environments if item["environment_id"] == "world.readonly"]
        self.assertEqual(len(world), 1)
        self.assertTrue(world[0]["read_only"])
        self.assertEqual(
            world[0]["actions"], [], "an environment that can act on the world is forbidden"
        )


class WorkflowKUserFacingTests(_LearningWorkflowCase):
    """K: the final response a user reads, from the real entry point."""

    def test_the_run_response_summarizes_the_work_and_its_limits(self) -> None:
        objective = self._create(_dial_body())
        body = self._run(objective["objective_id"])
        summary = body["summary"]

        self.assertIn("Learning objective achieved", summary)
        self.assertIn("sim.dial (a simulator)", summary)
        self.assertIn("What happened, in order:", summary)
        self.assertIn("form_hypothesis", summary)
        self.assertIn("Limitation:", summary)
        self.assertIn("simulator", summary.lower())
        # The summary is built from the record, so every step it names is one of them.
        for step in body["cycle"]["steps"]:
            self.assertIn(str(step["detail"]), summary)

    def test_the_status_surface_answers_before_anything_runs(self) -> None:
        status = self._client.get("/learning/status").json()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["availability"], "working")
        self.assertEqual(
            status["strategies"],
            ["uncertainty_first", "information_value", "bounded_random"],
        )
        self.assertEqual(status["thresholds"]["support"], 3)
        self.assertTrue(status["environments"]["simulators"])
        self.assertIn("interactive_learning", self._client.get("/status").json()["app"])

    def test_a_blank_question_or_unknown_strategy_is_a_422(self) -> None:
        blank = self._client.post(
            "/learning/objectives", json={"question": "   ", "environment_id": "sim.dial"}
        )
        self.assertEqual(blank.status_code, 422)
        self.assertIn("question", blank.json()["detail"])
        strategy = self._client.post(
            "/learning/objectives",
            json={"question": "what?", "environment_id": "sim.dial", "strategy": "telepathy"},
        )
        self.assertEqual(strategy.status_code, 422)
        self.assertIn("uncertainty_first", strategy.json()["detail"])

    def test_an_unknown_objective_is_a_404_on_every_door(self) -> None:
        for method, path, body in (
            ("get", "/learning/objectives/obj_nope", None),
            ("post", "/learning/objectives/obj_nope/preview", {}),
            ("post", "/learning/objectives/obj_nope/run", {}),
            ("post", "/learning/objectives/obj_nope/approve", {}),
            ("post", "/learning/objectives/obj_nope/reject", {}),
            ("post", "/learning/objectives/obj_nope/cancel", {}),
            ("get", "/learning/objectives/obj_nope/resumable", None),
            ("post", "/learning/objectives/obj_nope/resume", {}),
        ):
            kwargs = {"json": body} if body is not None else {}
            response = getattr(self._client, method)(path, **kwargs)
            self.assertEqual(response.status_code, 404, f"{method} {path}: {response.text}")

    def test_the_strategy_comparison_is_available_and_labelled(self) -> None:
        objective = self._create(
            {"question": "What does turning the dial do?", "environment_id": "sim.dial"}
        )
        response = self._client.post(
            f"/learning/objectives/{objective['objective_id']}/preview",
            json={"strategy": "information_value"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["strategy"], "information_value")
        self.assertIn("heuristic", " ".join(response.json()["limitations"]).lower())


class WorkflowLRealActionApprovalTests(_LearningWorkflowCase):
    """L: a REAL action runs only with an executor, the permission layer AND approval.

    The permission layer exercised here is the application's own (``self._nova.risk``,
    the same ``PermissionManager`` the safety gate is wired to), so this pins the
    wiring and not a stand-in.
    """

    def _register(self, environment: BridgedEnvironment) -> str:
        self._nova.learning.environments.register(environment)
        return environment.environment_id

    def _objective(self, environment_id: str) -> dict[str, Any]:
        return self._create(
            {"question": "what does waving the device do?", "environment_id": environment_id}
        )

    def test_an_unapproved_real_action_is_pending_and_not_a_terminal_stop(self) -> None:
        # An executor that reports NOTHING observed: the hypothesis stays open, so
        # every cycle below must decide again rather than answering from a record.
        executor = _RecordingExecutor(values={})
        environment_id = self._register(_real_environment("dev.real", executor))
        objective = self._objective(environment_id)

        body = self._run(objective["objective_id"])
        cycle = body["cycle"]

        # A missing approval is a pending decision, NOT "this question cannot be
        # tested": the objective stays open so the approval that follows can be spent.
        self.assertEqual(cycle["status"], "open", cycle["stop_reason"])
        self.assertIn("approval", cycle["stop_reason"])
        self.assertEqual(executor.calls, [], "nothing may run without an approval")
        self.assertEqual(body["runs"], [])
        # The refusal is recorded and announced rather than silently dropped.
        rejected = [item for item in body["experiments"] if item["status"] == "rejected"]
        self.assertTrue(rejected, body["experiments"])
        self.assertIn("learning.experiment_rejected", self._action_events())
        self.assertGreaterEqual(self._nova.learning.safety_refusals, 1)
        # And a real environment is never "resumable": its state cannot be verified.
        gate = self._client.get(
            f"/learning/objectives/{objective['objective_id']}/resumable"
        ).json()
        self.assertFalse(gate["resumable"])
        self.assertIn("not a simulator", gate["reason"])

        # Approve, then run again: the approval is what was missing. The cycle's own
        # bound (1) is honored, so the objective comes back OPEN rather than spent.
        approved = self._client.post(
            f"/learning/objectives/{objective['objective_id']}/approve", json={"reason": "go"}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()["single_use"])
        second = self._run(objective["objective_id"], max_experiments=1)
        self.assertEqual(len(executor.calls), 1, "the approved action runs exactly once")
        self.assertEqual(executor.calls[0][0], "wave")
        self.assertEqual(len(second["runs"]), 1)
        self.assertEqual(second["runs"][0]["status"], "completed")
        self.assertFalse(second["runs"][0]["simulated"])
        self.assertIn("bound was reached", second["cycle"]["stop_reason"])

        # The approval was CONSUMED: the next cycle asks for a new one and runs
        # nothing. If a stale approval leaked, the executor would be called twice —
        # which is exactly what this assertion is here to notice.
        third = self._run(objective["objective_id"], max_experiments=1)
        self.assertEqual(len(executor.calls), 1, "one approval authorizes one cycle")
        self.assertEqual(third["cycle"]["status"], "open", third["cycle"]["stop_reason"])
        self.assertIn("approval", third["cycle"]["stop_reason"])

        # A NEW approval is what authorizes the next experiment.
        approved_again = self._client.post(
            f"/learning/objectives/{objective['objective_id']}/approve", json={"reason": "go"}
        )
        self.assertEqual(approved_again.status_code, 200, approved_again.text)
        fourth = self._run(objective["objective_id"], max_experiments=1)
        self.assertEqual(len(executor.calls), 2, "a fresh approval authorizes one more")
        self.assertEqual(fourth["runs"][0]["status"], "completed")

    def test_a_real_action_reports_its_own_observation(self) -> None:
        """An executor's returned values ARE the post-action observation (§7)."""
        executor = _RecordingExecutor(values={"flag": "up"})
        environment_id = self._register(_real_environment("dev.reporter", executor))
        objective = self._objective(environment_id)
        self._client.post(
            f"/learning/objectives/{objective['objective_id']}/approve", json={"reason": "go"}
        )

        body = self._run(objective["objective_id"])
        run = body["runs"][0]

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["observation"]["values"], {"flag": "up"})
        self.assertTrue(run["observation"]["complete"])
        self.assertEqual(run["observation"]["source"], "dev.reporter")
        # The declared candidate that matched is the one that got the evidence.
        statuses = {item["status"] for item in body["hypotheses"]}
        self.assertIn("supported", statuses)

    def test_a_real_action_without_a_permission_layer_is_stopped_not_merely_unjustified(
        self,
    ) -> None:
        executor = _RecordingExecutor()
        environment_id = self._register(_real_environment("dev.nogate", executor))
        objective = self._objective(environment_id)
        gate = self._nova.learning.gate
        original = gate.permissions
        gate.permissions = None
        try:
            body = self._run(objective["objective_id"], approval=True)
        finally:
            gate.permissions = original

        self.assertEqual(body["cycle"]["status"], "stopped", body["cycle"]["stop_reason"])
        self.assertIn("no permission layer is wired", body["cycle"]["stop_reason"])
        self.assertEqual(executor.calls, [], "no gate is not permission")

    def test_a_destructive_real_action_is_refused_even_with_an_approval(self) -> None:
        executor = _RecordingExecutor()
        environment_id = self._register(
            _real_environment(
                "dev.danger",
                executor,
                action="wipe",
                destructive=True,
                reversible=False,
            )
        )
        objective = self._objective(environment_id)
        body = self._run(objective["objective_id"], approval=True)

        self.assertEqual(body["cycle"]["status"], "stopped", body["cycle"]["stop_reason"])
        self.assertIn("never approved", body["cycle"]["stop_reason"])
        self.assertEqual(executor.calls, [])


class WorkflowMRestoreAndRecordHonestyTests(_LearningWorkflowCase):
    """M: a corrupt stored record is skipped and REPORTED, and a refusal is recorded."""

    def _restart(self) -> None:
        """Stop and start the REAL app against the same data directory."""
        self._client.__exit__(None, None, None)
        self._client = TestClient(create_app())
        self._client.__enter__()

    def test_a_corrupt_stored_record_is_skipped_and_reported_at_boot(self) -> None:
        objective = self._create(_dial_body())
        self._run(objective["objective_id"])
        repository = self._nova.learning.repository
        # A record that could not have been produced by this module: `objective` is
        # not even a mapping. This used to take the whole restore — and therefore
        # application startup — down with an AttributeError.
        self._nova.state_store.write(
            repository.key("obj_corrupt"),
            {"objective": "not-a-mapping", "runs": ["nope", 7], "hypotheses": [None]},
        )
        repository.remember("obj_corrupt")

        self._restart()

        status = self._client.get("/learning/status").json()
        restored = status["restored"]
        self.assertEqual(restored["objectives"], 1, restored)
        self.assertEqual(restored["skipped"], 1, restored)
        self.assertEqual(restored["unreadable"][0]["objective_id"], "obj_corrupt")
        self.assertIn("objective mapping", restored["unreadable"][0]["reason"])
        # The good objective's record — and the rule it learned — still came back.
        objectives = self._client.get("/learning/objectives").json()["objectives"]
        self.assertEqual([item["objective_id"] for item in objectives], [objective["objective_id"]])
        self.assertEqual(len(status["rules"]), 1, status["rules"])

    def test_a_refused_promotion_is_recorded_and_is_not_announced_as_a_change(self) -> None:
        objective = self._create(_dial_body())
        # One experiment: the support threshold (3) is not reached, so no rule may appear.
        first = self._run(objective["objective_id"], max_experiments=1)
        self.assertEqual(first["cycle"]["rules"], [], first["cycle"]["rules"])
        refusals = [
            item
            for item in self._nova.learning.book.updates
            if item.change.value == "no_change" and "not promoted" in item.reason
        ]
        self.assertTrue(refusals, "the refusal must be a RECORD, not a comment")
        self.assertIn("below the threshold", refusals[-1].reason)
        self.assertNotIn(
            "learning.knowledge_updated",
            self._action_events(),
            "nothing changed, so nothing may be announced as a change",
        )

        # Finish the cycle: now a rule IS learned, and the change IS announced.
        self._run(objective["objective_id"])
        self.assertIn("learning.knowledge_updated", self._action_events())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
