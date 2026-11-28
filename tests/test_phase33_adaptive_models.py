"""Phase 33 — adaptive model and compute intelligence.

These tests exercise the real routing layer over fake providers: capability
gating, context/privacy/cost/hardware eligibility, measured ranking, classified
fallback, concurrency limits, policy learning and rollback. No paid provider and
no real model download is involved; the live verification script uses the real
machine for hardware/selection measurements.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from novacontrol.models.adaptive import (
    MIN_POLICY_SUPPORT,
    AdaptiveModelRouter,
    AdaptiveTaskClassifier,
    FailureKind,
    ModelEndpoint,
    ProviderKind,
    ProviderRecord,
    ProviderRegistry,
    RoutingPreferences,
    classify_failure,
    failure_is_retryable,
)
from novacontrol.models.manager import ModelManager
from novacontrol.models.profiles import ModelCapability, ModelProfile, ModelRegistry

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]


class FakeMonitor:
    def __init__(self, available_bytes: int = 16 * 1024**3) -> None:
        self.available_bytes = available_bytes

    def available_ram_bytes(self) -> int:
        return self.available_bytes

    def total_ram_bytes(self) -> int:
        return self.available_bytes

    def fits(self, required: int | None) -> bool:
        return required is None or required <= self.available_bytes


class FakeManager:
    def __init__(self, profiles: list[ModelProfile], monitor: FakeMonitor | None = None) -> None:
        self.monitor = monitor or FakeMonitor()
        self.registry = ModelRegistry(profiles, with_defaults=False)
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.active: list[str] = []

    def provider_name(self) -> str:
        return "ollama"

    def acquire(self, model: str, *, force: bool = False) -> Any:
        self.acquired.append(model)
        return None

    def begin_activity(self, model: str) -> None:
        self.active.append(model)

    def release_after_use(self, model: str) -> bool:
        self.released.append(model)
        return True


class FakeAdapter:
    def __init__(
        self,
        *,
        text: str = "ok",
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[list[dict[str, str]]] = []
        self.usage = {"requests": 0, "prompt_tokens": 4, "completion_tokens": 2}
        self.active = 0
        self.max_active = 0

    async def complete(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.usage["requests"] += 1
            if self.error is not None:
                raise self.error
            return self.text
        finally:
            self.active -= 1


def _profiles() -> list[ModelProfile]:
    return [
        ModelProfile(
            name="tiny:2b",
            capabilities=frozenset({ModelCapability.TEXT}),
            provider="ollama",
            context_length=4096,
            parameters_b=2,
            memory_bytes=1024**3,
            declared=True,
        ),
        ModelProfile(
            name="coder:7b",
            capabilities=frozenset(
                {
                    ModelCapability.TEXT,
                    ModelCapability.CODING,
                    ModelCapability.REASONING,
                }
            ),
            provider="ollama",
            context_length=32768,
            parameters_b=7,
            memory_bytes=4 * 1024**3,
            declared=True,
        ),
        ModelProfile(
            name="vision:7b",
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
            provider="ollama",
            context_length=8192,
            parameters_b=7,
            memory_bytes=5 * 1024**3,
            declared=True,
        ),
        ModelProfile(
            name="embed:1b",
            capabilities=frozenset({ModelCapability.EMBEDDINGS}),
            provider="ollama",
            context_length=None,
            declared=True,
        ),
    ]


def _router(
    *,
    monitor: FakeMonitor | None = None,
    prefs: RoutingPreferences | None = None,
    learning: Any = None,
) -> tuple[AdaptiveModelRouter, FakeManager]:
    manager = FakeManager(_profiles(), monitor=monitor)
    router = AdaptiveModelRouter(
        manager,  # type: ignore[arg-type]
        preferences=prefs
        or RoutingPreferences(allow_cloud=True, prefer_local=True, max_concurrency=1),
        learning=learning,
    )
    router._adapters["ollama"] = FakeAdapter(text="local answer")
    return router, manager


def _cloud(
    router: AdaptiveModelRouter,
    *,
    model: str = "cloud-small",
    adapter: FakeAdapter | None = None,
    price: float | None = None,
    profile: ModelProfile | None = None,
) -> FakeAdapter:
    fake = adapter or FakeAdapter(text="cloud answer")
    router.register_cloud_model(
        provider_id="cloud:test",
        model=model,
        label="Test Cloud",
        adapter=fake,
        profile=profile,
        pricing_per_1m_input=price,
        pricing_per_1m_output=price,
    )
    return fake


class FailureClassificationTests(unittest.TestCase):
    def test_failures_are_classified_and_only_transient_ones_retry(self) -> None:
        timeout = classify_failure(TimeoutError("read timed out"))
        self.assertEqual(timeout, FailureKind.TIMEOUT)
        self.assertTrue(failure_is_retryable(timeout))
        rate = classify_failure("HTTP 429: rate limit exceeded")
        self.assertEqual(rate, FailureKind.RATE_LIMIT)
        self.assertTrue(failure_is_retryable(rate))
        auth = classify_failure("401 invalid api key")
        self.assertEqual(auth, FailureKind.AUTHENTICATION)
        self.assertFalse(failure_is_retryable(auth))
        context = classify_failure("maximum context length is 8192 tokens")
        self.assertEqual(context, FailureKind.CONTEXT_LENGTH)
        self.assertFalse(failure_is_retryable(context))
        overload = classify_failure("503 model is busy")
        self.assertEqual(overload, FailureKind.MODEL_OVERLOAD)
        self.assertTrue(failure_is_retryable(overload))
        self.assertEqual(classify_failure(asyncio.CancelledError()), FailureKind.CANCELLED)
        self.assertFalse(failure_is_retryable(FailureKind.CANCELLED))


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.registry = ProviderRegistry(
            clock=lambda: self.now, failure_threshold=2, cooldown_seconds=30
        )
        self.registry.register(
            ProviderRecord(
                provider_id="cloud:test",
                label="Test",
                kind=ProviderKind.CLOUD,
                models=("m",),
                auth_required=True,
            )
        )

    def test_registration_controls_and_cooldown_are_real_state(self) -> None:
        self.assertTrue(self.registry.available("cloud:test"))
        self.registry.set_enabled("cloud:test", False)
        self.assertFalse(self.registry.available("cloud:test"))
        self.registry.set_enabled("cloud:test", True)
        self.assertTrue(self.registry.available("cloud:test"))
        self.registry.record_failure("cloud:test", FailureKind.TIMEOUT, error="timed out")
        self.assertTrue(self.registry.available("cloud:test"))
        self.registry.record_failure("cloud:test", FailureKind.TIMEOUT, error="timed out")
        self.assertFalse(self.registry.available("cloud:test"))
        self.now += 31
        self.assertTrue(self.registry.available("cloud:test"))
        self.registry.record_success("cloud:test", latency_ms=250)
        self.assertEqual(self.registry.health("cloud:test").successes, 1)
        self.assertAlmostEqual(self.registry.health("cloud:test").mean_latency_ms or 0, 250)

    def test_snapshot_carries_no_credential(self) -> None:
        self.registry.record_success("cloud:test", latency_ms=10)
        rows = self.registry.snapshot()
        self.assertEqual(rows[0]["provider_id"], "cloud:test")
        self.assertNotIn("api_key", str(rows).lower())
        self.assertIn("health", rows[0])

    def test_re_registration_preserves_the_user_switch(self) -> None:
        self.registry.set_enabled("cloud:test", False)
        self.registry.register(
            ProviderRecord(
                provider_id="cloud:test",
                label="Refreshed label",
                kind=ProviderKind.CLOUD,
                models=("m",),
                auth_required=True,
            )
        )
        self.assertFalse(self.registry.get("cloud:test").enabled)
        self.assertFalse(self.registry.available("cloud:test"))


class TaskClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = AdaptiveTaskClassifier()

    def test_simple_task_requests_text_and_prefers_low_latency(self) -> None:
        profile = self.classifier.classify("say hello")
        self.assertEqual(profile.category, "chat")
        self.assertIn(ModelCapability.TEXT, profile.capabilities)
        self.assertIn(profile.latency_sensitivity, {"high", "balanced"})
        self.assertLess(profile.min_context_tokens, 8192)

    def test_coding_and_build_tasks_require_coding_and_context(self) -> None:
        coding = self.classifier.classify("debug this failing test in the module")
        self.assertEqual(coding.category, "code")
        self.assertIn(ModelCapability.CODING, coding.capabilities)
        build = self.classifier.classify("build and test a full application")
        self.assertEqual(build.category, "build")
        self.assertIn(ModelCapability.CODING, build.capabilities)
        self.assertIn(ModelCapability.REASONING, build.capabilities)
        self.assertGreaterEqual(build.min_context_tokens, 2048)

    def test_vision_and_structured_requirements_come_from_the_request(self) -> None:
        vision = self.classifier.classify("describe this screenshot", has_image=True)
        self.assertTrue(vision.requires_vision)
        self.assertIn(ModelCapability.VISION, vision.capabilities)
        structured = self.classifier.classify(
            "return a summary", requires_structured_output=True
        )
        self.assertTrue(structured.requires_structured_output)
        self.assertIn(ModelCapability.STRUCTURED_OUTPUT, structured.capabilities)

    def test_deterministic_estimate_is_reported_without_a_model_call(self) -> None:
        profile = self.classifier.classify("what is my RAM usage?")
        self.assertTrue(profile.deterministic)
        self.assertEqual(profile.category, "status")


class SelectionTests(unittest.TestCase):
    def test_discovery_pruning_removes_an_uninstalled_local_model(self) -> None:
        router, _ = _router()
        self.assertIn("coder:7b", {endpoint.model for endpoint in router.endpoints()})
        removed = router.prune_local_models("ollama", {"tiny:2b"})
        self.assertEqual(removed, 3)
        self.assertEqual({endpoint.model for endpoint in router.endpoints()}, {"tiny:2b"})

    def test_capability_matching_selects_coding_and_vision_models(self) -> None:
        router, _ = _router()
        coding = router.route("debug this failing test")
        self.assertEqual(coding.model, "coder:7b")
        self.assertTrue(
            any(
                ModelCapability.CODING.value in row["capabilities"]
                for row in router.status()["models"]
            )
        )  # catalogue is capability-based, not name-based
        vision = router.route("what is on my screen", has_image=True)
        self.assertEqual(vision.model, "vision:7b")
        self.assertEqual(vision.route, "local")

    def test_context_length_rejection_is_explicit(self) -> None:
        router, _ = _router()
        decision = router.route("summarise this large pdf document")
        self.assertFalse(
            any(
                candidate.endpoint.model == "tiny:2b" and candidate.eligible
                for candidate in decision.candidates
            )
        )
        tiny = next(c for c in decision.candidates if c.endpoint.model == "tiny:2b")
        self.assertIn("context", " ".join(tiny.rejected_because))
        self.assertTrue(decision.found)

    def test_privacy_rejects_cloud_even_when_it_scores_higher(self) -> None:
        router, _ = _router()
        _cloud(router)
        # Remove local candidates so only the cloud model would be eligible.
        for endpoint in list(router.endpoints()):
            if router.providers.get(endpoint.provider_id).kind is ProviderKind.LOCAL:
                router._endpoints.pop((endpoint.provider_id, endpoint.model), None)
        decision = router.route("write a private journal entry", local_only=True)
        self.assertEqual(decision.route, "none")
        self.assertIn("privacy", " ".join(decision.rationale).lower())

    def test_disabled_provider_and_cost_cap_are_enforced(self) -> None:
        router, _ = _router()
        _cloud(router, price=1000.0)
        router.configure_preferences(allow_cloud=True, max_cost_per_request=0.0)
        for endpoint in list(router.endpoints()):
            if router.providers.get(endpoint.provider_id).kind is ProviderKind.LOCAL:
                router._endpoints.pop((endpoint.provider_id, endpoint.model), None)
        decision = router.route("say hello")
        self.assertEqual(decision.route, "none")
        self.assertIn("exceeds the cap", " ".join(decision.rationale))
        router.configure_preferences(max_cost_per_request=None)
        router.set_provider_enabled("cloud:test", False)
        self.assertEqual(router.route("say hello").route, "none")

    def test_hardware_pressure_rejects_a_local_model_that_does_not_fit(self) -> None:
        router, _ = _router(monitor=FakeMonitor(available_bytes=2 * 1024**3))
        decision = router.route("debug this failing test")
        rejected = {
            candidate.endpoint.model: candidate.rejected_because
            for candidate in decision.candidates
        }
        self.assertIn("bytes but only", " ".join(rejected["coder:7b"]))
        self.assertNotEqual(decision.model, "coder:7b")

    def test_latency_budget_rejects_a_measured_slow_candidate(self) -> None:
        router, _ = _router()
        _cloud(router)
        endpoint = next(
            item for item in router.endpoints() if item.provider_id == "cloud:test"
        )
        router._endpoints[(endpoint.provider_id, endpoint.model)] = replace(
            endpoint, latency_ms=5000
        )
        router.configure_preferences(latency_budget_ms=1000)
        decision = router.route("say hello")
        cloud = next(
            item for item in decision.candidates if item.endpoint.provider_id == "cloud:test"
        )
        self.assertFalse(cloud.eligible)
        self.assertIn("exceeds the budget", " ".join(cloud.rejected_because))
        self.assertTrue(decision.found)

    def test_unknown_price_is_not_treated_as_free(self) -> None:
        router, _ = _router()
        _cloud(router, price=None)
        decision = router.route("say hello")
        cloud = next(
            candidate
            for candidate in decision.candidates
            if candidate.endpoint.provider_id == "cloud:test"
        )
        self.assertTrue(cloud.eligible)
        self.assertIn("price unknown", " ".join(cloud.reasons))

    def test_unknown_context_is_described_as_an_estimate_not_a_hard_limit(self) -> None:
        router, _ = _router()
        router.register_provider(
            ProviderRecord(
                provider_id="unknownctx",
                label="Unknown context",
                kind=ProviderKind.LOCAL,
                models=("unknown:1b",),
            )
        )
        router.register_endpoint(
            ModelEndpoint(
                provider_id="unknownctx",
                model="unknown:1b",
                profile=ModelProfile(
                    name="unknown:1b",
                    capabilities=frozenset({ModelCapability.TEXT}),
                    provider="unknownctx",
                    context_length=None,
                    declared=True,
                ),
            )
        )
        decision = router.route("summarise this large pdf document")
        candidate = next(
            item for item in decision.candidates if item.endpoint.model == "unknown:1b"
        )
        self.assertTrue(candidate.eligible)
        self.assertIn("estimate, not a reported hard limit", " ".join(candidate.reasons))

    def test_selection_latency_is_small(self) -> None:
        router, _ = _router()
        _cloud(router)
        started = time.perf_counter()
        for _ in range(100):
            router.route("say hello", record=False)
        elapsed_ms = (time.perf_counter() - started) * 1000 / 100
        self.assertLess(elapsed_ms, 25.0, f"selection took {elapsed_ms:.2f} ms")


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_falls_back_to_a_compatible_cloud_model(self) -> None:
        router, _ = _router()
        failing = FakeAdapter(error=TimeoutError("read timed out"))
        router._adapters["ollama"] = failing
        _cloud(
            router,
            adapter=FakeAdapter(text="cloud answer"),
            profile=ModelProfile(
                name="cloud-small",
                capabilities=frozenset(
                    {ModelCapability.TEXT, ModelCapability.CODING}
                ),
                provider="cloud:test",
                declared=True,
            ),
        )
        outcome = await router.complete(
            [{"role": "user", "content": "debug this failing test"}],
            text="debug this failing test",
        )
        self.assertTrue(outcome.answered)
        self.assertTrue(outcome.fallback_used)
        self.assertEqual(outcome.text, "cloud answer")
        self.assertEqual(outcome.attempts[0].failure_kind, "timeout")
        self.assertEqual(outcome.attempts[1].provider_id, "cloud:test")

    async def test_authentication_failure_does_not_fall_back(self) -> None:
        router, _ = _router()
        router._adapters["ollama"] = FakeAdapter(error=RuntimeError("401 invalid api key"))
        cloud = _cloud(router, adapter=FakeAdapter(text="cloud answer"))
        outcome = await router.complete(
            [{"role": "user", "content": "debug this failing test"}],
            text="debug this failing test",
        )
        self.assertFalse(outcome.answered)
        self.assertEqual(outcome.attempts[0].failure_kind, "authentication")
        self.assertEqual(cloud.calls, [])

    async def test_empty_response_is_a_malformed_retryable_failure(self) -> None:
        router, _ = _router()
        router._adapters["ollama"] = FakeAdapter(text="   ")
        _cloud(router, adapter=FakeAdapter(text="recovered"))
        outcome = await router.complete(
            [{"role": "user", "content": "say hello"}], text="say hello"
        )
        self.assertTrue(outcome.answered)
        self.assertEqual(outcome.attempts[0].failure_kind, "malformed_response")
        self.assertEqual(outcome.text, "recovered")

    async def test_no_compatible_model_reports_the_limitation(self) -> None:
        router, _ = _router()
        for endpoint in list(router.endpoints()):
            if ModelCapability.VISION in endpoint.capabilities:
                router._endpoints.pop((endpoint.provider_id, endpoint.model), None)
        task = router.classifier.classify("describe this screenshot", has_image=True)
        outcome = await router.complete(
            [{"role": "user", "content": "describe this screenshot"}],
            text="describe this screenshot",
            task=task,
        )
        self.assertFalse(outcome.answered)
        self.assertIn("no compatible model", outcome.error)

    async def test_concurrency_limit_serializes_provider_calls(self) -> None:
        router, _ = _router()
        slow = FakeAdapter(text="slow", delay=0.02)
        router._adapters["ollama"] = slow
        await asyncio.gather(
            *[
                router.complete([{"role": "user", "content": "say hello"}], text="say hello")
                for _ in range(3)
            ]
        )
        self.assertEqual(slow.max_active, 1)

    async def test_local_only_completion_never_calls_a_cloud_adapter(self) -> None:
        router, _ = _router()
        for endpoint in list(router.endpoints()):
            if router.providers.get(endpoint.provider_id).kind is ProviderKind.LOCAL:
                router._endpoints.pop((endpoint.provider_id, endpoint.model), None)
        cloud = _cloud(router, adapter=FakeAdapter(text="cloud answer"))
        outcome = await router.complete(
            [{"role": "user", "content": "private journal"}],
            text="private journal",
            local_only=True,
        )
        self.assertFalse(outcome.answered)
        self.assertIn("no compatible model", outcome.error)
        self.assertEqual(cloud.calls, [])

    async def test_local_only_overrides_a_caller_supplied_task(self) -> None:
        router, _ = _router()
        for endpoint in list(router.endpoints()):
            if router.providers.get(endpoint.provider_id).kind is ProviderKind.LOCAL:
                router._endpoints.pop((endpoint.provider_id, endpoint.model), None)
        cloud = _cloud(router, adapter=FakeAdapter(text="cloud answer"))
        task = router.classifier.classify("say hello")
        outcome = await router.complete(
            [{"role": "user", "content": "say hello"}],
            text="say hello",
            task=task,
            local_only=True,
        )
        self.assertFalse(outcome.answered)
        self.assertEqual(cloud.calls, [])

    async def test_deterministic_completion_is_not_reported_as_answered(self) -> None:
        router, _ = _router()
        outcome = await router.complete(
            [{"role": "user", "content": "what is my RAM usage?"}],
            text="what is my RAM usage?",
        )
        self.assertTrue(outcome.decision.deterministic)
        self.assertFalse(outcome.answered)
        self.assertEqual(outcome.text, "")
        self.assertIn("outside the model router", outcome.error)


class LearningTests(unittest.TestCase):
    def test_policy_requires_support_then_activates_and_rolls_back(self) -> None:
        router, _ = _router()
        tiny = next(e for e in router.endpoints() if e.model == "tiny:2b")
        coder = next(e for e in router.endpoints() if e.model == "coder:7b")
        task = router.classifier.classify("say hello")
        for _ in range(MIN_POLICY_SUPPORT):
            router.record_observation(tiny, task, success=True, latency_ms=100)
            router.record_observation(coder, task, success=False, latency_ms=9000)
        proposal = router.propose_policy()
        self.assertTrue(proposal["eligible"], proposal)
        active = router.activate_policy(expected=proposal)
        self.assertTrue(active["active"])
        self.assertEqual(active["version"], 1)
        rolled = router.rollback_policy()
        self.assertFalse(rolled["policy"]["active"])
        self.assertEqual(router.status()["policy"]["version"], 0)

    def test_policy_without_enough_evidence_cannot_activate(self) -> None:
        router, _ = _router()
        with self.assertRaises(ValueError) as ctx:
            router.activate_policy()
        self.assertIn("not validated", str(ctx.exception))

    def test_observations_are_reported_by_category(self) -> None:
        router, _ = _router()
        tiny = next(e for e in router.endpoints() if e.model == "tiny:2b")
        task = router.classifier.classify("say hello")
        result = router.record_observation(
            tiny, task, success=True, latency_ms=123, usage={"prompt_tokens": 7}
        )
        self.assertTrue(result["success"])
        rows = router.performance()["rows"]
        self.assertEqual(rows[0]["category"], "chat")
        self.assertEqual(rows[0]["runs"], 1)

    def test_policy_keeps_same_named_models_on_different_providers_apart(self) -> None:
        router, _ = _router()
        local = next(e for e in router.endpoints() if e.model == "tiny:2b")
        _cloud(
            router,
            model="tiny:2b",
            profile=ModelProfile(
                name="tiny:2b",
                capabilities=frozenset({ModelCapability.TEXT}),
                provider="cloud:test",
                declared=True,
            ),
        )
        remote = next(
            e
            for e in router.endpoints()
            if e.provider_id == "cloud:test" and e.model == "tiny:2b"
        )
        task = router.classifier.classify("say hello")
        for _ in range(MIN_POLICY_SUPPORT):
            router.record_observation(local, task, success=True, latency_ms=100)
            router.record_observation(remote, task, success=False, latency_ms=9000)
        bonuses = router.propose_policy()["bonuses"]["chat"]
        self.assertIn("ollama/tiny:2b", bonuses)
        self.assertIn("cloud:test/tiny:2b", bonuses)
        self.assertNotIn("tiny:2b", bonuses)
        self.assertGreater(bonuses["ollama/tiny:2b"], 0)
        self.assertLess(bonuses["cloud:test/tiny:2b"], 0)


class Phase27LearningAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_hands_provider_reliability_to_continual_learning(self) -> None:
        captured: list[Any] = []

        class Learning:
            enabled = True

            def capture(self, goal: str, **kwargs: Any) -> Any:
                captured.append((goal, kwargs))
                return {"goal": goal, **kwargs}

            def record(self, evidence: Any) -> dict[str, Any]:
                captured.append(evidence)
                return {"recorded": True}

        router, _ = _router(learning=Learning())
        outcome = await router.complete(
            [{"role": "user", "content": "say hello"}], text="say hello"
        )
        self.assertTrue(outcome.answered)
        self.assertTrue(captured)
        goal, kwargs = captured[0]
        self.assertEqual(goal, "chat")
        self.assertEqual(kwargs["context"]["model"], "tiny:2b")
        self.assertFalse(kwargs["verification"]["verified"])


@unittest.skipIf(TestClient is None, "fastapi.testclient.TestClient requires httpx")
class RoutingApiTests(unittest.TestCase):
    """HTTP checks against the real create_app factory and app instance."""

    def setUp(self) -> None:
        from novacontrol.api.app import create_app
        from novacontrol.application import NovaControlApplication

        self._tmp = TemporaryDirectory()
        self._app = NovaControlApplication(data_dir=self._tmp.name)
        patcher = mock.patch(
            "novacontrol.api.app.NovaControlApplication", return_value=self._app
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self._client = TestClient(create_app())
        self._client.__enter__()
        self.addCleanup(self._client.__exit__, None, None, None)

    def test_routing_status_and_route_validation(self) -> None:
        status = self._client.get("/models/routing")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertIn("providers", body)
        self.assertIn("preferences", body)
        missing = self._client.post("/models/routing/route", json={})
        self.assertEqual(missing.status_code, 422)
        bad_config = self._client.post(
            "/models/routing/config", json={"max_concurrency": 0}
        )
        self.assertEqual(bad_config.status_code, 422)
        bad_provider = self._client.post(
            "/models/routing/providers", json={"provider_id": "missing", "enabled": True}
        )
        self.assertEqual(bad_provider.status_code, 404)

    def test_real_routing_and_completion_through_app_adapters(self) -> None:
        self._app.model_router.register_cloud_model(
            provider_id="cloud:fake",
            model="fake-text",
            adapter=FakeAdapter(text="routed answer"),
        )
        selected = self._client.post(
            "/models/routing/route", json={"text": "say hello"}
        )
        self.assertEqual(selected.status_code, 200)
        self.assertTrue(selected.json()["model"])
        completed = self._client.post(
            "/models/routing/complete", json={"text": "say hello"}
        )
        self.assertEqual(completed.status_code, 200)
        body = completed.json()
        self.assertTrue(body["answered"])
        self.assertEqual(body["text"], "routed answer")
        self.assertTrue(body["decision"]["rationale"])

    def test_agent_roles_receive_evidence_backed_model_assignments(self) -> None:
        from novacontrol.agents.models import AgentRole

        self._app.model_router.register_cloud_model(
            provider_id="cloud:agent",
            model="agent-coder",
            adapter=FakeAdapter(text="agent answer"),
            profile=ModelProfile(
                name="agent-coder",
                capabilities=frozenset(
                    {ModelCapability.TEXT, ModelCapability.CODING}
                ),
                provider="cloud:agent",
                declared=True,
            ),
        )
        assignment = self._app._agent_model_assignment(
            AgentRole.CODING, "fix the failing test", {}
        )
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment["model"], "agent-coder")
        self.assertEqual(assignment["assigned_role"], "coding")
        self.assertTrue(assignment["rationale"])

    def test_policy_activation_without_evidence_is_409(self) -> None:
        response = self._client.post("/models/routing/policy/activate")
        self.assertEqual(response.status_code, 409)
        rollback = self._client.post("/models/routing/policy/rollback")
        self.assertEqual(rollback.status_code, 409)

    def test_local_only_completion_never_calls_the_configured_cloud(self) -> None:
        cloud = FakeAdapter(text="cloud answer")
        self._app.model_router.register_cloud_model(
            provider_id="cloud:private",
            model="cloud-private",
            adapter=cloud,
        )
        response = self._client.post(
            "/models/routing/complete",
            json={"text": "summarise this private journal", "local_only": True},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["local_only"])
        self.assertFalse(body["answered"])
        self.assertEqual(cloud.calls, [])

    def test_provider_toggle_survives_a_status_resync(self) -> None:
        self._app.set_cloud_llm(
            "openai", "sk-test-1234567890abcdefgh", model="gpt-4o-mini"
        )
        self._app.set_model_provider_enabled("cloud:openai", False)
        status = self._app.adaptive_model_status()
        row = next(item for item in status["providers"] if item["provider_id"] == "cloud:openai")
        self.assertFalse(row["enabled"])

    def test_router_attaches_after_a_local_provider_switch(self) -> None:
        class LocalProvider:
            name = "ollama"
            model = "mock:1b"
            usage: dict[str, Any] = {}

        self._app.brain.set_mode("auto")
        with mock.patch.object(
            self._app.model_manager, "provider_name", return_value="ollama"
        ):
            self._app.brain.set_local_provider(LocalProvider())
            self._app._sync_model_router()
        self.assertIs(self._app.brain._model_router, self._app.model_router)

    def test_cloud_mode_does_not_register_the_cloud_model_as_local(self) -> None:
        self._app.set_cloud_llm(
            "openai", "sk-test-1234567890abcdefgh", model="gpt-4o-mini"
        )
        self._app.set_brain_mode("cloud")
        self._app._sync_model_router()
        endpoints = [
            item for item in self._app.model_router.endpoints() if item.model == "gpt-4o-mini"
        ]
        self.assertTrue(endpoints)
        for endpoint in endpoints:
            record = self._app.model_router.providers.get(endpoint.provider_id)
            self.assertIsNotNone(record)
            self.assertIs(record.kind, ProviderKind.CLOUD)

    def test_local_endpoint_keeps_image_modality_and_inferred_provenance(self) -> None:
        class LocalProvider:
            name = "ollama"
            model = "local-vlm:4b"
            usage: dict[str, Any] = {}

        self._app.model_manager.registry.register(
            ModelProfile(
                name="local-vlm:4b",
                capabilities=frozenset(
                    {ModelCapability.TEXT, ModelCapability.VISION}
                ),
                provider="ollama",
                declared=False,
            )
        )
        self._app.brain.set_mode("auto")
        with mock.patch.object(
            self._app.model_manager, "provider_name", return_value="ollama"
        ):
            self._app.brain.set_local_provider(LocalProvider())
            self._app._sync_model_router()
        endpoint = next(
            item for item in self._app.model_router.endpoints() if item.model == "local-vlm:4b"
        )
        self.assertIn("image", endpoint.input_modalities)
        self.assertEqual(endpoint.metadata_source, "inferred")


if __name__ == "__main__":
    unittest.main()
