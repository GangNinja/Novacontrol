"""Phase 7 on the request path: the flows, the telemetry, and the API surface.

The specification's four example flows are the acceptance criteria here, stated
as what each one must NOT do as much as what it must:

  * "Open Chrome." — the machine's own layers carry it out, with NO model
    selected and NO model consulted;
  * "What's using so much RAM?" — answered from this machine's readings, again
    with nothing selected;
  * a multi-step task — the planner runs and a model IS part of the route;
  * a screenshot question — the vision route, with the vision model chosen by
    CAPABILITY rather than by name.

And the run is measured: one telemetry row per request, with per-stage
durations, the memory it moved, which model and provider were selected, whether
the model was needed at all, and no chain-of-thought anywhere in it.

The application is booted with no language model available (the patches below),
and with a model manager backed by a fake runtime, so every assertion here is
about NovaControl's own decisions and never about what happens to be installed
on the machine running the suite.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from novacontrol.application import NovaControlApplication
from novacontrol.intelligence.telemetry import REQUEST_STAGES
from novacontrol.models import KeepAlivePolicy, KeepAliveSettings, ModelManager
from test_model_phase7 import CHAT, GIB, VISION, FakeMonitor, FakeProvider
from test_web_api import _IsolatedApiTestCase


def _pipeline_provider() -> FakeProvider:
    """A runtime holding exactly the two models the flows need."""
    return FakeProvider(
        installed={CHAT: 6 * GIB, VISION: 3 * GIB},
        capabilities={
            CHAT: ("completion", "tools", "thinking"),
            VISION: ("completion", "vision"),
        },
    )


class _PipelineCase(unittest.IsolatedAsyncioTestCase):
    """An isolated application with no language model and a fake runtime."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = [
            # No local model: the brain resolves to its Echo fallback, so a
            # request that reaches a model is fast and deterministic, and a
            # request that does NOT reach one is the only reason it is fast.
            mock.patch("novacontrol.application.build_ollama_provider", return_value=None),
            mock.patch(
                "novacontrol.application.build_llm_provider_from_environment",
                return_value=None,
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.app = NovaControlApplication(data_dir=Path(self._tmp.name) / "data")
        self.provider = _pipeline_provider()
        self.app.model_manager = ModelManager(self.provider, monitor=FakeMonitor())

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    async def _ask(self, text: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.app.handle_request(text, **kwargs)
        return dict(response.payload)

    def _rows(self) -> list[dict[str, Any]]:
        return list(self.app.intelligence.telemetry.to_dict()["requests"]["recent"])


class RequestFlowTests(_PipelineCase):
    """The four flows, judged by what they did NOT need."""

    async def test_a_direct_command_needs_no_model(self) -> None:
        response = await self.app.handle_request("Open Chrome.")
        row = self._rows()[-1]
        self.assertTrue(row["fast_path"])
        # No model was SELECTED — which is the claim flow 1 makes. The provider
        # reads "local" because that is the decision layer's word for the
        # deterministic path, and an empty model beside it is what distinguishes
        # "ran locally" from "ran on a local model".
        self.assertEqual(row["model"], "")
        self.assertEqual(row["provider"], "local")
        self.assertNotEqual(response.route, "chat")

    async def test_a_reading_from_this_machine_needs_no_model(self) -> None:
        response = await self.app.handle_request("What's using so much RAM?")
        row = self._rows()[-1]
        self.assertTrue(row["fast_path"])
        self.assertEqual(row["model"], "")
        self.assertEqual(response.route, "system")
        # The answer is this machine's OWN reading, not a model's recollection.
        self.assertIn("GB", response.summary)

    async def test_a_multi_step_task_is_not_a_fast_path(self) -> None:
        """A task that needs sequencing cannot be carried out by one local rule."""
        await self._ask("find my nova control project and run the tests")
        row = self._rows()[-1]
        self.assertFalse(row["fast_path"])

    async def test_the_specifications_third_flow_reaches_the_planner(self) -> None:
        """Example 3: three clauses, and all three of them have to happen.

        *"Find my NovaControl project, run the tests and explain why they fail"*
        decomposes into three actions, and the intent table has no executor for
        the first two — so this used to fall through to the unhandled-capability
        branch and be classified from its FIRST clause alone, quietly dropping the
        test run and the explanation the request also asked for. The planner is
        the only layer that can carry all three out, and the decision says so.
        """
        payload = await self._ask(
            "Find my NovaControl project, run the tests and explain why they fail."
        )
        routed = dict(payload.get("nlu") or {})
        self.assertEqual(routed.get("handler"), "plan")
        self.assertIn("plan", payload)
        steps = list((payload["plan"] or {}).get("steps") or [])
        self.assertTrue(steps)
        # Tool discovery and the verification surface travel with the plan, which
        # is what makes this the planner route rather than a single handler call.
        self.assertIn("tools", payload)

    async def test_the_context_layer_is_measured_where_it_does_its_work(self) -> None:
        """A bare reference has to be resolved from what is already known.

        The stage reports the CONTEXT layer's own cost, so a request that the
        cheap layers resolve without it reports ``count: 0`` — "not reached" —
        rather than a fabricated zero. Both halves are asserted here, because a
        stage that is named in the readout and never populated is exactly the
        defect this replaced.
        """
        await self._ask("Open Chrome.")
        resolved_by_rule = self.app.intelligence.telemetry.to_dict()["stages_ms"]
        self.assertEqual(resolved_by_rule["context"]["count"], 0)

        await self._ask("send it to him")
        measured = self.app.intelligence.telemetry.to_dict()["stages_ms"]["context"]
        self.assertGreater(measured["count"], 0)
        self.assertIsNotNone(measured["avg"])

    async def test_a_screenshot_question_takes_the_vision_route(self) -> None:
        response = await self.app.handle_request(
            "Look at my screen and tell me what this error says"
        )
        self.assertEqual(response.route, "vision")
        row = self._rows()[-1]
        self.assertFalse(row["fast_path"])

    async def test_every_request_is_measured_end_to_end(self) -> None:
        await self._ask("Open Chrome.")
        summary = self.app.intelligence.telemetry.to_dict()
        self.assertEqual(sorted(summary["stages_ms"]), sorted(REQUEST_STAGES))
        used = {name for name, block in summary["stages_ms"].items() if block["count"]}
        self.assertIn("nlu", used)
        self.assertIn("decision", used)
        self.assertIn("response", used)
        self.assertEqual(summary["requests"]["count"], 1)
        self.assertEqual(summary["requests"]["total_ms"]["count"], 1)

    async def test_a_failed_handler_is_still_measured(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("handler exploded")

        # EVERY handler fails, so the assertion does not depend on which route
        # this phrasing happens to take.
        handlers = dict(self.app._HANDLERS)
        try:
            self.app._HANDLERS = dict.fromkeys(handlers, boom)
            with self.assertRaises(RuntimeError):
                await self.app.handle_request("Open Chrome.")
        finally:
            self.app._HANDLERS = handlers
        row = self._rows()[-1]
        self.assertFalse(row["success"])
        self.assertGreaterEqual(row["total_ms"], 0.0)

    async def test_a_request_that_never_reported_is_still_counted(self) -> None:
        """A crash before the handler ran must not lose the request."""
        with (
            mock.patch.object(
                self.app.intelligence,
                "understand_async",
                side_effect=RuntimeError("nlu died"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await self.app.handle_request("anything at all")
        # Nothing was filed yet: the trace is still open. The NEXT request closes
        # it, so the count stays honest rather than growing by one per crash.
        self.assertEqual(self._rows(), [])
        await self._ask("Open Chrome.")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["success"])
        self.assertTrue(rows[1]["fast_path"])


class ModelResidencyOnTheRequestPathTests(_PipelineCase):
    """The manager's load/unload policy, exercised by the handlers that use a model.

    The specification asks for the residency rules to be applied to REQUESTS, not
    only to an operator pressing "load": the vision model is made room for before
    it is used, a model mid-answer is protected from an eviction, and the
    configured keep-alive policy runs when the use finishes.
    """

    async def _capture_stub(self, name: str = "probe.txt", body: str = "Welcome") -> Any:
        source = self.app.data_dir / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(body, encoding="utf-8")

        async def fake_capture(*, save_path: str = "vision_screen.png") -> dict[str, Any]:
            del save_path
            return {"screenshot": str(source), "captured": True, "vision_model": True}

        self.app.vision.capture_screen = fake_capture  # type: ignore[method-assign]
        return source

    async def test_a_vision_request_makes_room_for_the_model_it_needs(self) -> None:
        """Example 4, with the specification's own switch in front of it."""
        from novacontrol.brain.models import BrainRequest

        self.provider.resident = {CHAT: 6 * GIB}
        # Two gigabytes free and a three-gigabyte VLM: alongside the resident chat
        # model it does not fit, and with that model released it does.
        self.app.model_manager = ModelManager(
            self.provider, monitor=FakeMonitor(available=2 * GIB)
        )
        await self._capture_stub()
        with mock.patch.object(self.app, "_vision_pipeline_model", return_value=VISION):
            route, payload = await self.app._handle_vision(
                BrainRequest(text="what is on my screen?"), "what is on my screen?"
            )
        self.assertEqual(route, "vision")
        # The chat model was released to make room, the VLM was loaded and the
        # runtime was asked to confirm it — the six steps, on the automatic path.
        self.assertEqual(self.provider.unloads, [CHAT])
        self.assertEqual(self.provider.loads, [VISION])
        self.assertTrue(payload["model_lifecycle"]["loaded"])
        self.assertEqual(self.app.model_manager.active_models(), ())

    async def test_a_model_that_will_not_fit_is_reported_and_the_model_is_skipped(self) -> None:
        """A refusal is a measurement: answer from the text, say why, do not page."""
        from novacontrol.brain.models import BrainRequest
        from novacontrol.vision.models import VisionResult

        self.app.model_manager = ModelManager(
            self.provider, monitor=FakeMonitor(available=1 * GIB)
        )
        await self._capture_stub()
        allowed: list[bool] = []

        async def fake_analyze(source: str, **kwargs: Any) -> VisionResult:
            del source
            allowed.append(bool(kwargs.get("allow_vlm", True)))
            return VisionResult(summary="Read from the screen's text.")

        with (
            mock.patch.object(self.app, "_vision_pipeline_model", return_value=VISION),
            mock.patch.object(self.app, "analyze_image", fake_analyze),
        ):
            _, payload = await self.app._handle_vision(
                BrainRequest(text="what does this say?"), "what does this say?"
            )
        self.assertEqual(allowed, [False])  # the VLM was not consulted
        self.assertTrue(payload["model_lifecycle"]["refused"])
        self.assertEqual(self.provider.loads, [])
        self.assertIn("not loaded", payload["message"])

    async def test_an_answer_in_progress_is_not_evicted_and_the_policy_runs_after(self) -> None:
        """A chat model is marked IN USE for the duration of the answer it is writing."""
        from types import SimpleNamespace

        self.provider.resident = {CHAT: 6 * GIB}
        self.app.model_manager.keep_alive = KeepAliveSettings(
            policy=KeepAlivePolicy.IMMEDIATE
        )
        seen: list[tuple[str, ...]] = []

        async def fake_chat(request: Any) -> Any:
            del request
            seen.append(self.app.model_manager.active_models())
            return SimpleNamespace(payload={"answer": "ok"})

        with (
            mock.patch.object(type(self.app.brain), "model_configured", True),
            mock.patch.object(type(self.app.brain), "model_name", CHAT),
            mock.patch.object(self.app.brain, "chat", fake_chat),
        ):
            route, _payload = await self.app._handle_chat(
                mock.MagicMock(name="request"), "hello there"
            )
        self.assertEqual(route, "chat")
        # Marked in use DURING the call (so an eviction cannot take it) and
        # released after it, where ``immediate`` acts on the release.
        self.assertEqual(seen, [(CHAT,)])
        self.assertEqual(self.app.model_manager.active_models(), ())
        self.assertEqual(self.provider.unloads, [CHAT])


class ModelSurfaceTests(_PipelineCase):
    """The readouts, and that they say where each capability claim came from."""

    async def test_status_carries_the_capability_table_without_probing(self) -> None:
        self.provider.probed.clear()
        status = self.app.status()
        self.assertIn("models", status)
        models = status["models"]
        self.assertEqual(models["provider"], "fake")
        self.assertEqual(models["keep_alive"]["policy"], "warm")
        self.assertTrue(models["registry"]["models"])
        # No runtime probe on the request path: this readout is embedded in every
        # BrainRequest context, so asking the runtime here would be a round trip
        # per request.
        self.assertEqual(self.provider.probed, [])

    async def test_the_measured_readout_is_available_off_the_event_loop(self) -> None:
        report = await self.app.model_status()
        for key in ("manager", "pipeline", "selections", "local_models"):
            with self.subTest(key=key):
                self.assertIn(key, report)
        self.assertIn("hardware", report["manager"])
        self.assertEqual(
            report["manager"]["hardware"]["available_ram_bytes"], 8 * GIB
        )

    async def test_no_vision_model_means_no_model_is_pinned(self) -> None:
        """The pipeline must fall through rather than name a model that is not there."""
        text_only = FakeProvider(
            installed={CHAT: 6 * GIB},
            capabilities={CHAT: ("completion", "tools")},
        )
        self.app.model_manager = ModelManager(text_only, monitor=FakeMonitor())
        self.assertEqual(self.app._selected_vision_model(), "")
        report = self.app._vision_pipeline_report()
        self.assertEqual(report["vision_model"], "")

    async def test_an_undeclared_installed_vlm_is_selected_by_measurement(self) -> None:
        """A model pulled five minutes ago, with nothing declared about it."""
        provider = FakeProvider(
            installed={CHAT: 6 * GIB, "brand-new-vl:3b": 2 * GIB},
            capabilities={
                CHAT: ("completion", "tools"),
                "brand-new-vl:3b": ("completion", "vision"),
            },
        )
        self.app.model_manager = ModelManager(provider, monitor=FakeMonitor())
        self.assertEqual(self.app._selected_vision_model(), "brand-new-vl:3b")

    async def test_the_routing_report_answers_each_kind_of_work(self) -> None:
        report = await self.app.model_status()
        self.assertEqual(report["selections"]["vision"]["model"], VISION)
        self.assertEqual(report["selections"]["reasoning"]["model"], CHAT)
        self.assertEqual(report["selections"]["vision"]["route"], "local")

    async def test_an_explicit_load_goes_through_the_manager(self) -> None:
        result = await self.app.load_model(VISION)
        self.assertTrue(result["loaded"])
        self.assertEqual(self.provider.loads, [VISION])
        self.assertIn("manager", result)
        self.assertEqual(
            [step["step"] for step in result["manager"]["steps"]],
            ["check_memory", "check_resident", "estimate", "evict", "load", "verify"],
        )

    async def test_a_refused_load_reports_the_measurement_behind_it(self) -> None:
        self.app.model_manager = ModelManager(
            self.provider,
            monitor=FakeMonitor(available=1 * GIB),
        )
        result = await self.app.load_model(CHAT)
        self.assertFalse(result["loaded"])
        self.assertTrue(result["refused"])
        self.assertEqual(self.provider.loads, [])
        self.assertIn("estimate", [step["step"] for step in result["manager"]["steps"]])

    async def test_unloading_keeps_a_model_an_active_task_is_using(self) -> None:
        self.provider.resident = {CHAT: 6 * GIB}
        self.app.model_manager.begin_activity(CHAT)
        held = await self.app.unload_model(CHAT)
        self.assertFalse(held["unloaded"])
        self.assertEqual(self.provider.unloads, [])
        self.app.model_manager.end_activity(CHAT)
        self.assertTrue((await self.app.unload_model(CHAT))["unloaded"])


class IntelligenceEndpointTests(_IsolatedApiTestCase):
    """/intelligence is where the numbers are meant to be readable."""

    def _extra_patchers(self) -> list[Any]:
        # Same two patches as the flows above: no language model may be reached
        # from a test, so a request that needs one is answered by the fallback.
        return [
            mock.patch("novacontrol.application.build_ollama_provider", return_value=None),
            mock.patch(
                "novacontrol.application.build_llm_provider_from_environment",
                return_value=None,
            ),
        ]

    def _isolated_application(self, **kwargs: Any) -> NovaControlApplication:
        app = super()._isolated_application(**kwargs)
        app.model_manager = ModelManager(_pipeline_provider(), monitor=FakeMonitor())
        return app

    def test_the_endpoint_reports_models_telemetry_and_no_reasoning(self) -> None:
        response = self._client.get("/intelligence")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("models", body)
        models = body["models"]
        self.assertEqual(models["provider"], "fake")
        self.assertTrue(models["registry"]["models"])
        # The measured half, and the answers a reader actually wants: which model
        # would serve each kind of work, and what this machine has.
        self.assertIn("hardware", models)
        self.assertIn("selection", models)
        self.assertEqual(models["selection"]["vision"]["model"], VISION)
        self.assertIn("runtime", models)
        # The stage timings and request rows ride along with the existing
        # telemetry block, so the endpoint gains the numbers rather than a second
        # endpoint gaining them.
        summary = body["telemetry"]
        self.assertIn("stages_ms", summary)
        self.assertIn("requests", summary)
        text = repr(body).lower()
        for forbidden in ("chain_of_thought", "reasoning_text", "chain-of-thought"):
            with self.subTest(key=forbidden):
                self.assertNotIn(forbidden, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
