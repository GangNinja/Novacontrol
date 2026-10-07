"""Phase 14: model benchmarking, execution modes and the central privacy policy.

The tests drive measured behaviour rather than declared behaviour: a fake clock
makes latency arithmetic exact, a fake runner makes success and failure
deterministic, and the privacy tests ask the SAME object the application will
ask, so the refusal reasons are the ones a user would see.
"""

from __future__ import annotations

import time
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from novacontrol.application import NovaControlApplication
from novacontrol.core.config import ResourceSettings
from novacontrol.core.diagnostics import HealthState
from novacontrol.decision.cost import TaskCostEstimator
from novacontrol.decision.providers import PrivacyGatedDecisionProvider
from novacontrol.diagnostics import DiagnosticManager, DiagnosticResult
from novacontrol.explore import ExploreRequest
from novacontrol.explore.trending import TrendingTopicsProvider
from novacontrol.models.hardware import HardwareMonitor
from novacontrol.models.manager import ModelManager
from novacontrol.optimization.benchmark import (
    BenchmarkCompletion,
    InMemoryBenchmarkStore,
    JsonlBenchmarkStore,
    ModelBenchmarking,
    RuntimeReading,
)
from novacontrol.optimization.governor import GovernorThresholds, ResourceGovernor
from novacontrol.optimization.models import (
    BenchmarkRecord,
    CostLevel,
    ExecutionMode,
    PrivacyAction,
    PrivacyControls,
    ResourceLevel,
)
from novacontrol.optimization.privacy import PrivacyDenied, PrivacyPolicy

GiB = 1024 ** 3


def _clock_seq(*values: float):
    """A monotonic clock returning each value once, then holding the last."""
    remaining = list(values)

    def tick() -> float:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0] if remaining else 0.0

    return tick


class BenchmarkingTests(unittest.IsolatedAsyncioTestCase):
    """14.1: every figure is measured or None, and comparisons are measured too."""

    def _benchmark(self, reading: RuntimeReading | None = None) -> ModelBenchmarking:
        return ModelBenchmarking(
            store=InMemoryBenchmarkStore(),
            clock=_clock_seq(0.0, 0.5, 0.5, 1.25),
            stamp=lambda: datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
            reading=reading,
        )

    async def test_a_run_records_every_measured_figure(self) -> None:
        readings = {"ram_bytes": 6 * GiB, "gpu_utilization": 42.0, "gpu_memory_bytes": 2 * GiB,
                    "npu_available": True}

        async def runner(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(
                text="ok",
                first_token_ms=80.0,
                prompt_tokens=12,
                completion_tokens=100,
                task_ok=True,
                structured_ok=True,
                tool_ok=True,
            )

        benchmark = self._benchmark(reading=lambda: readings)
        records = await benchmark.run(
            "qwen3:8b", ["summarise this"], category="summarise", runner=runner, provider="ollama"
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.model, "qwen3:8b")
        self.assertEqual(record.category, "summarise")
        self.assertEqual(record.total_ms, 500.0)
        self.assertEqual(record.first_token_ms, 80.0)
        self.assertEqual(record.tokens_per_second, 200.0)  # 100 tokens / 0.5 s
        self.assertEqual(record.ram_bytes, 6 * GiB)
        self.assertEqual(record.gpu_utilization, 42.0)
        self.assertEqual(record.gpu_memory_bytes, 2 * GiB)
        self.assertIs(record.npu_used, True)
        self.assertIs(record.structured_ok, True)
        self.assertIs(record.tool_ok, True)
        self.assertFalse(record.failed)

    async def test_unmeasured_figures_stay_none(self) -> None:
        async def runner(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(text="ok", task_ok=True)

        benchmark = self._benchmark()  # no runtime reading injected
        records = await benchmark.run("m", ["task"], category="chat", runner=runner)

        record = records[0]
        self.assertIsNone(record.first_token_ms)
        self.assertIsNone(record.tokens_per_second)
        self.assertIsNone(record.ram_bytes)
        self.assertIsNone(record.gpu_utilization)
        self.assertIsNone(record.npu_used)

    async def test_a_runner_that_raises_is_a_recorded_failure(self) -> None:
        async def runner(prompt: str, category: str) -> BenchmarkCompletion:
            raise RuntimeError("model not reachable")

        benchmark = self._benchmark()
        records = await benchmark.run("m", ["task"], category="chat", runner=runner)

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].failed)
        self.assertIn("model not reachable", records[0].failure)
        score = benchmark.compare("chat")[0]
        self.assertEqual(score.runs, 1)
        self.assertEqual(score.failures, 1)
        self.assertEqual(score.success_rate, 0.0)
        self.assertEqual(score.failure_rate, 1.0)

    async def test_comparison_orders_by_measured_success_then_latency(self) -> None:
        async def good(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(text="ok", task_ok=True)

        async def bad(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(text="wrong", task_ok=False)

        benchmark = self._benchmark()
        await benchmark.run("alpha", ["t1"], category="build", runner=good)
        await benchmark.run("beta", ["t1"], category="build", runner=bad)

        scores = benchmark.compare("build")

        self.assertEqual([score.model for score in scores], ["alpha", "beta"])
        self.assertEqual(benchmark.best_for("build").model, "alpha")  # type: ignore[union-attr]
        self.assertIsNone(benchmark.best_for("unknown-category"))

    async def test_rates_are_none_when_nothing_checked_them(self) -> None:
        async def runner(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(text="ok", task_ok=True)

        benchmark = self._benchmark()
        await benchmark.run("m", ["t"], category="chat", runner=runner)

        score = benchmark.compare("chat")[0]
        self.assertIsNone(score.structured_rate)
        self.assertIsNone(score.tool_rate)
        self.assertEqual(score.success_rate, 1.0)

    async def test_the_cap_drops_the_oldest_rows_and_storage_round_trips(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = JsonlBenchmarkStore(Path(temp_dir) / "benchmarks" / "benchmarks.jsonl")
            benchmark = ModelBenchmarking(store=store, cap=2)

            async def runner(prompt: str, category: str) -> BenchmarkCompletion:
                return BenchmarkCompletion(text="ok", task_ok=True)

            await benchmark.run("m", ["one", "two", "three"], category="chat", runner=runner)

            self.assertEqual([row.task for row in benchmark.records()], ["two", "three"])
            reopened = ModelBenchmarking(store=store)
            self.assertEqual([row.task for row in reopened.records()], ["two", "three"])

    async def test_a_damaged_line_is_skipped_not_fatal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "benchmarks.jsonl"
            path.write_text("{not json\n", encoding="utf-8")
            benchmark = ModelBenchmarking(store=JsonlBenchmarkStore(path))

            benchmark.record(
                BenchmarkRecord(model="m", category="chat", total_ms=12.0, task_ok=True)
            )

            self.assertEqual(len(benchmark.records()), 1)
            self.assertEqual(benchmark.records()[0].model, "m")

    async def test_report_names_what_the_history_covers(self) -> None:
        async def runner(prompt: str, category: str) -> BenchmarkCompletion:
            return BenchmarkCompletion(text="ok", task_ok=True)

        benchmark = self._benchmark()
        await benchmark.run("m", ["t"], category="chat", runner=runner)

        report = benchmark.report()

        self.assertEqual(report["records"], 1)
        self.assertEqual(report["models"], ["m"])
        self.assertEqual(report["categories"], ["chat"])
        self.assertEqual(report["sink"], "memory")


class ExecutionModeTests(unittest.TestCase):
    def test_modes_parse_and_an_unknown_spelling_keeps_balanced(self) -> None:
        self.assertIs(ExecutionMode.from_text("local-only"), ExecutionMode.LOCAL_ONLY)
        self.assertIs(ExecutionMode.from_text("PERFORMANCE"), ExecutionMode.PERFORMANCE)
        self.assertIs(ExecutionMode.from_text("banana"), ExecutionMode.BALANCED)
        self.assertIs(ExecutionMode.from_text(None), ExecutionMode.BALANCED)

    def test_only_local_only_closes_the_external_paths(self) -> None:
        self.assertFalse(ExecutionMode.LOCAL_ONLY.external_allowed)
        self.assertTrue(ExecutionMode.BALANCED.external_allowed)
        self.assertTrue(ExecutionMode.PERFORMANCE.external_allowed)

    def test_resource_and_cost_levels_are_ordered(self) -> None:
        self.assertTrue(ResourceLevel.CRITICAL.severe)
        self.assertFalse(ResourceLevel.AMPLE.severe)
        self.assertLess(CostLevel.TRIVIAL.rank, CostLevel.VERY_HIGH.rank)


class PrivacyPolicyTests(unittest.TestCase):
    """14.2/14.3: one object answers every outbound question, with its reason."""

    def test_local_only_refuses_every_external_action_even_with_controls_on(self) -> None:
        policy = PrivacyPolicy(mode=ExecutionMode.LOCAL_ONLY)

        for action in PrivacyAction:
            with self.subTest(action=action):
                decision = policy.evaluate(action)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.control, "execution_mode")
                self.assertIn("local_only", decision.reason)

    def test_balanced_allows_a_permitted_action_and_names_the_control(self) -> None:
        policy = PrivacyPolicy(mode=ExecutionMode.BALANCED)

        decision = policy.evaluate(PrivacyAction.CLOUD_MODEL)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.control, "allow_cloud")
        self.assertTrue(policy.allows("external_search"))

    def test_a_control_that_is_off_refuses_with_its_name(self) -> None:
        policy = PrivacyPolicy(controls=PrivacyControls(allow_external_search=False))

        decision = policy.evaluate(PrivacyAction.EXTERNAL_SEARCH)

        self.assertFalse(decision.allowed)
        self.assertIn("allow_external_search", decision.reason)
        self.assertFalse(policy.allows(PrivacyAction.EXTERNAL_SEARCH))

    def test_require_raises_a_privacy_denied_carrying_the_decision(self) -> None:
        policy = PrivacyPolicy(controls=PrivacyControls(allow_cloud=False))

        with self.assertRaises(PrivacyDenied) as caught:
            policy.require(PrivacyAction.CLOUD_MODEL)

        self.assertEqual(caught.exception.decision.action, PrivacyAction.CLOUD_MODEL)
        self.assertEqual(caught.exception.decision.control, "allow_cloud")

    def test_apply_updates_only_the_named_controls(self) -> None:
        policy = PrivacyPolicy()

        policy.apply(allow_cloud=False, mode=ExecutionMode.PERFORMANCE)

        self.assertFalse(policy.controls.allow_cloud)
        self.assertTrue(policy.controls.allow_external_search)  # untouched
        self.assertIs(policy.mode, ExecutionMode.PERFORMANCE)

    def test_controls_from_mapping_keeps_the_default_on_unusable_input(self) -> None:
        controls = PrivacyControls.from_mapping(
            {"allow_cloud": "maybe", "allow_telemetry": "off", "allow_external_tools": 0}
        )

        self.assertTrue(controls.allow_cloud)
        self.assertFalse(controls.allow_telemetry)
        self.assertFalse(controls.allow_external_tools)

    def test_external_text_redacts_credentials_when_enabled(self) -> None:
        policy = PrivacyPolicy()
        text = "token ghp_abcdefghijklmnopqrstuvwxyz0123 and authorization: Bearer abcdefghijklmnopqrstuvwxyz"

        safe, count, kinds = policy.external_text(text)

        self.assertGreaterEqual(count, 2)
        self.assertTrue(kinds)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123", safe)
        self.assertNotIn("Bearer abcdefghijklmnopqrstuvwxyz", safe)

    def test_external_text_is_the_identity_when_redaction_is_off(self) -> None:
        policy = PrivacyPolicy(
            controls=PrivacyControls(sensitive_data_redaction=False)
        )
        text = "password=hunter2"

        safe, count, kinds = policy.external_text(text)

        self.assertEqual(safe, text)
        self.assertEqual(count, 0)
        self.assertEqual(kinds, ())

    def test_from_settings_reads_a_settings_mapping(self) -> None:
        policy = PrivacyPolicy.from_settings(
            {
                "execution_mode": "local_only",
                "allow_cloud": False,
                "allow_telemetry": False,
            }
        )

        self.assertIs(policy.mode, ExecutionMode.LOCAL_ONLY)
        self.assertFalse(policy.controls.allow_cloud)
        self.assertFalse(policy.controls.allow_telemetry)
        self.assertTrue(policy.controls.allow_external_search)

    def test_from_settings_accepts_an_object_with_to_dict(self) -> None:
        class FakeSettings:
            def to_dict(self) -> Mapping[str, object]:
                return {
                    "execution_mode": "performance",
                    "allow_remote_model": False,
                    "sensitive_data_redaction": False,
                }

        policy = PrivacyPolicy.from_settings(FakeSettings())

        self.assertIs(policy.mode, ExecutionMode.PERFORMANCE)
        self.assertFalse(policy.controls.allow_remote_model)
        self.assertFalse(policy.controls.sensitive_data_redaction)


class _FakeTelemetry:
    """The telemetry surface HardwareMonitor reads, without a real machine."""

    def __init__(
        self,
        *,
        available_gb: float | None = 8.0,
        total_gb: float | None = 16.0,
        cpu: float | None = 10.0,
        gpu: Mapping[str, object] | None = None,
        temperature: float | None = None,
        battery: Mapping[str, object] | None = None,
    ) -> None:
        self.available_gb = available_gb
        self.total_gb = total_gb
        self.cpu_value = cpu
        self.gpu_reading = gpu
        self.temperature_value = temperature
        self.battery_reading = battery

    def memory(self) -> dict[str, object]:
        if self.available_gb is None or self.total_gb is None:
            return {"available": False, "reason": "memory is unreadable"}
        return {
            "available": True,
            "total_bytes": int(self.total_gb * GiB),
            "available_bytes": int(self.available_gb * GiB),
        }

    def cpu(self) -> dict[str, object]:
        if self.cpu_value is None:
            return {"available": False, "reason": "no cpu counter"}
        return {"available": True, "percent": self.cpu_value}

    def gpu(self) -> dict[str, object]:
        return self.gpu_reading or {"available": False, "reason": "no GPU"}

    def temperature(self) -> dict[str, object]:
        if self.temperature_value is None:
            return {"available": False, "reason": "no thermal sensor"}
        return {"available": True, "celsius": self.temperature_value}

    def battery(self) -> dict[str, object]:
        return self.battery_reading or {"available": False, "reason": "desktop machine"}


def _monitor(
    telemetry: _FakeTelemetry | None = None, resident: tuple[str, ...] = ()
) -> HardwareMonitor:
    return HardwareMonitor(
        telemetry=telemetry or _FakeTelemetry(),  # type: ignore[arg-type]
        resident_models=lambda: resident,
        npu_probe=lambda: {"available": False, "reason": "no NPU probe"},
    )


class ResourceGovernorTests(unittest.TestCase):
    """14.4: pressure is read from telemetry, and unknown is never a yes."""

    def _governor(
        self, telemetry: _FakeTelemetry, *, thresholds: GovernorThresholds | None = None
    ) -> ResourceGovernor:
        return ResourceGovernor(_monitor(telemetry), thresholds=thresholds)

    def test_an_ample_machine_is_ample_and_allows_a_load(self) -> None:
        governor = self._governor(_FakeTelemetry(available_gb=8.0))

        assessment = governor.assess()
        advice = governor.advise_load(2 * GiB)

        self.assertIs(assessment.level, ResourceLevel.AMPLE)
        self.assertIs(advice.allow, True)
        self.assertFalse(advice.prefer_lightweight)

    def test_low_ram_is_tight_and_refuses_an_oversized_load(self) -> None:
        governor = self._governor(_FakeTelemetry(available_gb=2.5))

        assessment = governor.assess()
        advice = governor.advise_load(3 * GiB, loaded=("large-a", "large-b"), active=("large-b",))

        self.assertIs(assessment.level, ResourceLevel.TIGHT)
        self.assertIs(advice.allow, False)
        self.assertTrue(advice.prefer_lightweight)
        self.assertEqual(advice.unload, ("large-a",))
        self.assertIn("only", advice.reason)
        self.assertIn("large-a", advice.reason)

    def test_critical_ram_refuses_even_an_unmeasured_load(self) -> None:
        governor = self._governor(_FakeTelemetry(available_gb=1.0))

        assessment = governor.assess()
        advice = governor.advise_load(None)

        self.assertIs(assessment.level, ResourceLevel.CRITICAL)
        self.assertIs(advice.allow, False)
        self.assertIn("critical", advice.reason)

    def test_unmeasured_ram_is_unknown_not_a_yes(self) -> None:
        governor = self._governor(_FakeTelemetry(available_gb=None))

        assessment = governor.assess()
        advice = governor.advise_load(2 * GiB)

        self.assertIs(assessment.level, ResourceLevel.AMPLE)  # no reading, no pressure
        self.assertIn("could not be measured", observation_text(assessment.reasons))
        self.assertIsNone(advice.allow)

    def test_high_cpu_and_gpu_memory_pressure_escalate(self) -> None:
        gpu = {
            "available": True,
            "percent": 97.0,
            "memory": {
                "available": True,
                "kind": "dedicated",
                "used_bytes": int(7.5 * GiB),
                "total_bytes": int(8 * GiB),
            },
        }
        governor = self._governor(_FakeTelemetry(available_gb=8.0, cpu=95.0, gpu=gpu))

        assessment = governor.assess()

        self.assertIs(assessment.level, ResourceLevel.TIGHT)
        joined = observation_text(assessment.reasons)
        self.assertIn("CPU utilization", joined)
        self.assertIn("GPU memory", joined)

    def test_a_low_battery_on_the_road_escalates_but_on_ac_does_not(self) -> None:
        low = {"available": True, "percent": 10.0, "on_ac": False, "charging": False}
        plugged = {"available": True, "percent": 10.0, "on_ac": True, "charging": True}

        on_road = self._governor(_FakeTelemetry(available_gb=8.0, battery=low)).assess()
        at_desk = self._governor(_FakeTelemetry(available_gb=8.0, battery=plugged)).assess()

        self.assertIs(on_road.level, ResourceLevel.TIGHT)
        self.assertIn("battery", observation_text(on_road.reasons))
        self.assertIs(at_desk.level, ResourceLevel.AMPLE)

    def test_temperature_escalates_and_a_very_hot_machine_is_critical(self) -> None:
        warm = self._governor(_FakeTelemetry(available_gb=8.0, temperature=90.0)).assess()
        hot = self._governor(_FakeTelemetry(available_gb=8.0, temperature=96.0)).assess()

        self.assertIs(warm.level, ResourceLevel.TIGHT)
        self.assertIs(hot.level, ResourceLevel.CRITICAL)

    def test_thresholds_clamp_a_critical_line_above_the_tight_one(self) -> None:
        thresholds = GovernorThresholds.from_mapping(
            {"tight_free_ram_mb": 2048, "critical_free_ram_mb": 4096, "max_cpu_percent": "hot"}
        )

        self.assertEqual(thresholds.critical_free_ram_mb, 2048)
        self.assertEqual(thresholds.max_cpu_percent, GovernorThresholds().max_cpu_percent)
        self.assertEqual(thresholds.to_dict()["tight_free_ram_mb"], 2048)

    def test_report_carries_the_assessment_and_the_hardware_block(self) -> None:
        governor = self._governor(_FakeTelemetry(available_gb=8.0))
        governor.assess()

        report = governor.report()

        self.assertEqual(report["assessment"]["level"], "ample")
        self.assertIn("hardware", report)


class _FakeProvider:
    """A model runtime whose resident set is real state, for load-path tests."""

    name = "fake"

    def __init__(self, models: tuple[str, ...], resident: tuple[str, ...] = ()) -> None:
        self._models = list(models)
        self._resident = list(resident)
        self.loads: list[str] = []
        self.unloads: list[str] = []

    def resident_names(self) -> tuple[str, ...]:
        return tuple(self._resident)

    def list_models(self) -> tuple[str, ...]:
        return tuple(self._models)

    def resident_models(self) -> tuple[tuple[str, int], ...]:
        return tuple((name, GiB) for name in self._resident)

    def load(self, model: str) -> bool:
        self.loads.append(model)
        if model not in self._resident:
            self._resident.append(model)
        return True

    def unload(self, model: str) -> bool:
        self.unloads.append(model)
        self._resident = [name for name in self._resident if name != model]
        return True

    def available_memory_bytes(self) -> int:
        return 8 * GiB

    def model_size_bytes(self, model: str) -> int | None:
        """A model's size; the name ``unknown`` reports nothing at all."""
        return None if model == "unknown" else GiB

    def capabilities(self, model: str) -> None:
        return None


class _FakeAdvisor:
    def __init__(self, allow: bool | None, reason: str = "governor says so") -> None:
        self._allow = allow
        self._reason = reason
        self.calls: list[int | None] = []

    def advise_load(
        self,
        needed_bytes: int | None,
        *,
        model: str = "",
        loaded: object = None,
        active: object = (),
    ) -> Mapping[str, object]:
        self.calls.append(needed_bytes)
        return {
            "allow": self._allow,
            "reason": self._reason,
            "level": "tight",
            "needed_bytes": needed_bytes,
            "unload": [],
            "prefer_lightweight": True,
        }

    def report(self) -> Mapping[str, object]:
        return {"assessment": {"level": "tight"}}


class ModelManagerGovernanceTests(unittest.TestCase):
    """14.4/14.5: the governor can refuse, limits evict, priority orders it."""

    def _manager(
        self,
        provider: _FakeProvider,
        *,
        advisor: _FakeAdvisor | None = None,
        max_resident: int = 0,
        priorities: Mapping[str, int] | None = None,
    ) -> ModelManager:
        return ModelManager(
            provider,  # type: ignore[arg-type]
            monitor=_monitor(resident=provider.resident_names()),
            advisor=advisor,  # type: ignore[arg-type]
            max_resident_models=max_resident,
            priorities=priorities,
        )

    def test_a_governor_refusal_blocks_a_load_the_arithmetic_allows(self) -> None:
        provider = _FakeProvider(models=("a",))
        advisor = _FakeAdvisor(False, reason="the machine is under critical pressure")
        manager = self._manager(provider, advisor=advisor)

        outcome = manager.load("a")

        self.assertTrue(outcome.refused)
        self.assertFalse(outcome.loaded)
        self.assertEqual(outcome.reason, "the machine is under critical pressure")
        self.assertEqual(provider.loads, [])
        self.assertEqual(advisor.calls, [GiB])
        self.assertIn("governor", [step.get("step") for step in outcome.steps])

    def test_a_governor_unknown_does_not_block_the_load(self) -> None:
        provider = _FakeProvider(models=("a",))
        manager = self._manager(provider, advisor=_FakeAdvisor(None))

        outcome = manager.load("a")

        self.assertTrue(outcome.loaded)
        self.assertEqual(provider.loads, ["a"])

    def test_the_concurrent_limit_evicts_the_minimum_and_loads(self) -> None:
        provider = _FakeProvider(models=("a", "b"), resident=("a",))
        manager = self._manager(provider, max_resident=1)

        outcome = manager.load("b")

        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.evicted, ("a",))
        self.assertEqual(provider.unloads, ["a"])
        self.assertEqual(provider.resident_names(), ("b",))

    def test_priority_decides_which_resident_model_goes_first(self) -> None:
        provider = _FakeProvider(models=("a", "b", "c", "d"), resident=("a", "b", "c"))
        manager = self._manager(provider, max_resident=2, priorities={"a": 5})

        outcome = manager.load("d")

        self.assertTrue(outcome.loaded)
        self.assertEqual(outcome.evicted, ("b", "c"))  # a is kept: highest priority
        self.assertEqual(provider.unloads, ["b", "c"])

    def test_the_limit_refuses_when_every_candidate_is_protected(self) -> None:
        provider = _FakeProvider(models=("a", "b", "d"), resident=("a", "b"))
        manager = self._manager(provider, max_resident=1)
        manager.begin_activity("b")  # b must not be evicted

        outcome = manager.load("d")

        self.assertTrue(outcome.refused)
        self.assertIn("concurrent model limit", outcome.reason)
        self.assertEqual(provider.loads, [])

    def test_a_models_size_is_the_runtimes_answer_or_none(self) -> None:
        provider = _FakeProvider(models=("a", "unknown"), resident=("a",))
        manager = self._manager(provider)

        self.assertEqual(manager.model_size_bytes("a"), GiB)
        self.assertIsNone(manager.model_size_bytes("unknown"))
        self.assertIsNone(manager.model_size_bytes(""))

    def test_health_carries_the_governor_block_when_one_is_configured(self) -> None:
        provider = _FakeProvider(models=("a",))
        manager = self._manager(provider, advisor=_FakeAdvisor(True))

        health = manager.health()

        self.assertTrue(health["governor"]["configured"])
        self.assertIn("assessment", health["governor"])


def observation_text(reasons: object) -> str:
    return "; ".join(str(reason) for reason in reasons)  # type: ignore[union-attr]


class TaskCostEstimatorTests(unittest.TestCase):
    """14.6: the specification's own examples, plus the routing hints."""

    def setUp(self) -> None:
        self.estimator = TaskCostEstimator()

    def test_the_specifications_three_examples_land_in_their_bands(self) -> None:
        trivial = self.estimator.estimate("What's my RAM usage?")
        medium = self.estimator.estimate("Analyze a large PDF")
        high = self.estimator.estimate("Build and test a full application")

        self.assertIs(trivial.level, CostLevel.TRIVIAL)
        self.assertFalse(trivial.model_required)
        self.assertIsNone(trivial.estimated_ram_mb)
        self.assertEqual(trivial.tool_calls, 0)

        self.assertIs(medium.level, CostLevel.MEDIUM)
        self.assertTrue(medium.model_required)
        self.assertGreater(medium.expected_latency_seconds or 0.0, 5.0)

        self.assertIs(high.level, CostLevel.VERY_HIGH)
        self.assertGreaterEqual(high.tool_calls, 3)
        self.assertTrue(high.model_required)

    def test_an_empty_request_costs_nothing_and_says_why(self) -> None:
        estimate = self.estimator.estimate("   ")

        self.assertIs(estimate.level, CostLevel.TRIVIAL)
        self.assertEqual(estimate.score, 0.0)
        self.assertTrue(estimate.reasons)

    def test_extra_task_kinds_raise_the_cost_but_within_their_ceiling(self) -> None:
        one = self.estimator.estimate("analyze a document")
        two = self.estimator.estimate("research the latest prices and analyze a large PDF")

        self.assertGreater(two.score, one.score)
        self.assertLessEqual(two.score, 1.0)
        self.assertIn("other task kind", " ".join(two.reasons))

    def test_counted_complexity_signals_reach_the_estimate(self) -> None:
        plain = self.estimator.estimate("analyze a document")
        sequential = self.estimator.estimate(
            "analyze a document and then summarise it using that summary"
        )

        self.assertGreater(sequential.score, plain.score)
        self.assertIn("depend", " ".join(sequential.reasons).lower())

    def test_a_carried_image_makes_the_estimate_vision_shaped(self) -> None:
        estimate = self.estimator.estimate(
            "what does this say?", requires_vision=True
        )

        self.assertTrue(estimate.gpu_required)
        self.assertTrue(estimate.model_required)

    def test_routing_hints_never_block_a_deterministic_request(self) -> None:
        status = self.estimator.estimate("What's my RAM usage?")
        build = self.estimator.estimate("Build and test a full application")

        status_hint = self.estimator.route_hint(status)
        build_hint = self.estimator.route_hint(build)

        self.assertTrue(status_hint["deterministic"])
        self.assertTrue(status_hint["prefer_lightweight"])
        self.assertFalse(build_hint["deterministic"])
        self.assertTrue(build_hint["requires_capable_model"])
        # The hint is a preference: nothing in it can refuse the build.
        self.assertNotIn("allowed", build_hint)

    def test_research_is_marked_as_wanting_external_data(self) -> None:
        estimate = self.estimator.estimate("what's the latest news about python")

        self.assertIs(estimate.level, CostLevel.MEDIUM)
        self.assertTrue(self.estimator.route_hint(estimate)["external_data"])


class DiagnosticManagerTests(unittest.IsolatedAsyncioTestCase):
    """14.7: every component gets one structured answer, whatever happens."""

    def _manager(self, timeout: float = 5.0) -> DiagnosticManager:
        manager = DiagnosticManager(timeout_seconds=timeout)
        manager.register(
            "Core", lambda: DiagnosticResult("Core", HealthState.OK, "bus up")
        )
        manager.register(
            "Ollama",
            lambda: DiagnosticResult(
                "Ollama",
                HealthState.DEGRADED,
                "runtime unreachable",
                remediation="start the runtime",
            ),
        )
        manager.register("Redis", lambda: DiagnosticResult("Redis", HealthState.SKIPPED, "off"))
        return manager

    async def test_a_result_carries_the_six_structured_fields(self) -> None:
        manager = self._manager()
        rows = await manager.run(only=["Ollama"])
        payload = rows[0].to_dict()

        self.assertEqual(rows[0].component, "Ollama")
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["severity"], "warning")
        self.assertTrue(payload["message"])
        self.assertTrue(payload["remediation"])
        self.assertIsInstance(payload["metadata"], dict)
        self.assertIn("checked_at", payload)

    async def test_an_async_check_is_accepted_next_to_sync_ones(self) -> None:
        manager = self._manager()

        async def slow_probe() -> DiagnosticResult:
            return DiagnosticResult("Plugins", HealthState.OK, "walked the plugin directory")

        manager.register("Plugins", slow_probe)
        rows = await manager.run()

        self.assertEqual([row.component for row in rows][-1], "Plugins")
        self.assertIs(rows[-1].status, HealthState.OK)

    async def test_registration_order_is_report_order_and_duplicates_raise(self) -> None:
        manager = self._manager()
        self.assertEqual(manager.components, ("Core", "Ollama", "Redis"))
        with self.assertRaises(ValueError):
            manager.register("Core", lambda: DiagnosticResult("Core", HealthState.OK, ""))
        with self.assertRaises(ValueError):
            manager.register("  ", lambda: DiagnosticResult("x", HealthState.OK, ""))

    async def test_an_unknown_component_raises_keyerror_instead_of_guessing(self) -> None:
        manager = self._manager()
        with self.assertRaises(KeyError):
            await manager.check("Nope")
        with self.assertRaises(KeyError):
            await manager.run(only=["Core", "Nope"])

    async def test_a_check_that_raises_becomes_a_failing_row_and_the_roster_continues(self) -> None:
        manager = self._manager()

        def broken() -> DiagnosticResult:
            raise RuntimeError("no driver")

        manager.register("GPU", broken)
        rows = await manager.run()

        self.assertEqual(len(rows), 4)
        self.assertIs(rows[3].status, HealthState.FAILING)
        self.assertIn("RuntimeError", rows[3].message)
        self.assertEqual(rows[3].metadata["error"], "RuntimeError")

    async def test_a_check_that_never_answers_times_out_with_a_remediation(self) -> None:
        manager = DiagnosticManager(timeout_seconds=0.05)

        def stuck() -> DiagnosticResult:
            time.sleep(1.0)
            return DiagnosticResult("Runtime", HealthState.OK, "never reached")

        manager.register("Runtime", stuck)
        rows = await manager.run()

        self.assertIs(rows[0].status, HealthState.FAILING)
        self.assertIn("did not answer", rows[0].message)
        self.assertTrue(rows[0].remediation)

    async def test_a_check_that_returns_the_wrong_type_is_a_failure_not_a_crash(self) -> None:
        manager = DiagnosticManager()
        manager.register("Weird", lambda: "not a result")  # type: ignore[arg-type,return-value]
        rows = await manager.run()

        self.assertIs(rows[0].status, HealthState.FAILING)
        self.assertIn("not a DiagnosticResult", rows[0].message)

    async def test_the_overall_state_is_the_worst_one_that_matters(self) -> None:
        manager = self._manager()
        rows = await manager.run()
        summary = manager.summarize(rows)
        self.assertIs(summary.overall, HealthState.DEGRADED)
        self.assertTrue(summary.degraded)
        self.assertEqual(summary.skipped, ("Redis",))
        self.assertEqual(summary.needs_attention, ("Ollama",))

        manager.register("NPU", lambda: DiagnosticResult("NPU", HealthState.UNKNOWN, "no probe"))
        rows = await manager.run()
        self.assertIs(manager.summarize(rows).overall, HealthState.UNKNOWN)

        manager.register("Storage", lambda: DiagnosticResult("Storage", HealthState.FAILING, "full"))
        rows = await manager.run()
        self.assertIs(manager.summarize(rows).overall, HealthState.FAILING)

    async def test_a_roster_of_only_ok_and_skipped_is_not_degraded(self) -> None:
        manager = DiagnosticManager()
        manager.register("Core", lambda: DiagnosticResult("Core", HealthState.OK, "up"))
        manager.register("Redis", lambda: DiagnosticResult("Redis", HealthState.SKIPPED, "off"))
        summary = manager.summarize(await manager.run())

        self.assertIs(summary.overall, HealthState.OK)
        self.assertFalse(summary.degraded)

    async def test_the_display_form_is_the_specifications_checklist(self) -> None:
        manager = self._manager()
        lines = manager.report_lines(await manager.run())

        self.assertEqual(lines[0], "NovaControl Health")
        self.assertIn("✓", lines[1])
        self.assertIn("⚠", lines[2])
        self.assertIn("–", lines[3])
        self.assertEqual([row.component for row in manager.last_results], list(manager.components))


class Phase14ApplicationTests(unittest.IsolatedAsyncioTestCase):
    """The Phase 14 services as the application actually wires them."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.app = NovaControlApplication(data_dir=self.data_dir)
        self.addCleanup(self.app.stop)

    async def test_a_fresh_install_is_balanced_with_every_control_open(self) -> None:
        self.assertIs(self.app.execution_mode, ExecutionMode.BALANCED)
        status = self.app.privacy_status()
        self.assertTrue(status["cloud_brain_allowed"])
        self.assertEqual(status["denied"], [])
        self.assertIn("cloud_model", status["actions"])

    async def test_local_only_closes_every_outbound_path_and_the_cloud_brain(self) -> None:
        await self.app.apply_privacy_settings(execution_mode="local_only")

        status = self.app.privacy_status()
        self.assertFalse(status["cloud_brain_allowed"])
        self.assertEqual(
            set(status["denied"]),
            {"cloud_model", "remote_model", "external_search", "external_tool", "telemetry"},
        )
        # Asking for the cloud brain lands on the local one instead of opening
        # the slot the operator closed.
        self.app.brain.set_mode("cloud")
        self.assertEqual(self.app.brain.mode, "llm")
        self.assertFalse(self.app.brain.cloud_allowed)

    async def test_a_privacy_change_is_persisted_and_survives_a_reload(self) -> None:
        await self.app.apply_privacy_settings(execution_mode="performance", allow_telemetry=False)
        stored = self.app.state_store.read("settings")

        self.assertEqual(stored["execution_mode"], "performance")
        self.assertFalse(stored["privacy_allow_telemetry"])
        self.assertFalse(self.app.privacy.allows(PrivacyAction.TELEMETRY))
        self.assertTrue(self.app.privacy.allows(PrivacyAction.CLOUD_MODEL))

    async def test_the_cost_estimate_rides_on_the_audited_decision(self) -> None:
        await self.app.handle_request("what is my RAM usage?")
        entry = self.app.audit_entries(1)[0]
        estimate = entry["decision"]["metadata"]["cost_estimate"]

        self.assertEqual(estimate["level"], "trivial")
        self.assertFalse(estimate["model_required"])

    def test_the_estimators_own_examples_are_reachable_from_the_application(self) -> None:
        build = self.app.estimate_cost("Build and test a full application")

        self.assertEqual(build["level"], "very_high")
        self.assertTrue(build["route_hint"]["requires_capable_model"])
        self.assertNotIn("allowed", build["route_hint"])

    def test_the_resource_report_names_its_level_and_reasons(self) -> None:
        report = self.app.resource_status()

        self.assertIn(report["level"], {level.value for level in ResourceLevel})
        self.assertTrue(report["reasons"])
        self.assertIn("thresholds", report)

    async def test_the_diagnostic_roster_covers_every_component_the_spec_names(self) -> None:
        report = await self.app.diagnostics_report()
        components = {row["component"] for row in report["components"]}
        for name in (
            "Core", "Fast NLU", "Context", "Decision engine", "Planner", "Tools",
            "Vision", "Ollama", "Models", "Model manager", "GPU", "NPU",
            "Database", "Redis", "Plugin system", "Knowledge system", "Scheduler",
            "Permissions", "Storage", "Network", "Configuration",
            # Phase 16 adds the SFT subsystem: DEGRADED without the training
            # libraries (dry-run still works) and SKIPPED when it is switched off.
            "Training",
            # Phase 17 adds preference optimization: the same treatment, with the
            # DPO/ORPO objectives and the pair store behind it.
            "Preference optimization",
            # Phase 18 adds the RLHF / RLAIF subsystem: same presence reporting,
            # with the mock-only policy optimizer named explicitly.
            "RLHF / RLAIF",
            # Phase 19 adds RLVR + critique learning: verifiers and rewards are
            # reported as present (or switched off) like the rest.
            "RLVR",
        ):
            self.assertIn(name, components)
        self.assertEqual(len(report["components"]), 25)
        for row in report["components"]:
            self.assertIn(row["status"], {state.value for state in HealthState})
            self.assertTrue(row["message"])
        self.assertIn(report["summary"]["overall"], {state.value for state in HealthState})
        self.assertEqual(report["lines"][0], "NovaControl Health")
        # The storage probe cleans up after itself.
        self.assertFalse((self.data_dir / ".diagnostic-probe").exists())

    async def test_the_roster_can_be_asked_for_a_subset_in_roster_order(self) -> None:
        report = await self.app.diagnostics_report(only=["Storage", "Core"])
        self.assertEqual(
            [row["component"] for row in report["components"]], ["Core", "Storage"]
        )
        with self.assertRaises(KeyError):
            await self.app.diagnostics_report(only=["Nope"])

    async def test_a_benchmark_run_is_measured_and_stored_by_category(self) -> None:
        await self.app.apply_privacy_settings(execution_mode="local_only")
        result = await self.app.benchmark_model(
            "echo", ["say hello", "count to three"], category="general"
        )

        self.assertEqual(len(result["records"]), 2)
        self.assertTrue(all(row["category"] == "general" for row in result["records"]))
        stored = self.app.benchmark_status()
        self.assertEqual(stored["categories"], ["general"])
        self.assertEqual(stored["records"], 2)

    async def test_closing_the_cloud_slot_also_resyncs_explore_synthesis(self) -> None:
        class _Cloud:
            name = "cloud:test"

            async def complete(self, messages: object, **kwargs: object) -> str:  # pragma: no cover
                return "a cloud answer"

        cloud = _Cloud()
        self.app.brain.set_cloud_provider(cloud)
        self.app.brain.set_mode("cloud")
        self.assertIs(self.app.brain.completion_provider, cloud)

        await self.app.apply_privacy_settings(execution_mode="local_only")

        # Both voices are local now: the brain AND Explore's synthesizer. The
        # second half is the bug this test exists for — Chat going local while
        # Explore kept synthesizing through the closed cloud slot.
        self.assertIsNot(self.app.brain.completion_provider, cloud)
        self.assertIsNot(self.app.explore.explainer.completion_provider, cloud)
        self.assertFalse(self.app.brain.cloud_allowed)

    async def test_explore_makes_no_external_call_when_search_is_closed(self) -> None:
        class _RecordingSearch:
            def __init__(self) -> None:
                self.calls = 0

            async def search(self, query: str, limit: int = 8) -> tuple[object, ...]:
                self.calls += 1
                return ()

        recorder = _RecordingSearch()
        self.app.explore.search_provider = recorder
        await self.app.apply_privacy_settings(execution_mode="local_only")

        report = await self.app.explore.research(ExploreRequest(topic="quantum widgets"))

        self.assertEqual(recorder.calls, 0)
        self.assertTrue(
            any("closed" in warning.lower() for warning in report.warnings), report.warnings
        )

    async def test_headline_topics_are_empty_and_unfetched_when_search_is_closed(self) -> None:
        calls: list[dict[str, object]] = []

        def fetch(**kwargs: object) -> tuple[str, ...]:
            calls.append(dict(kwargs))
            return ("Quantum widgets leap forward - Example News",)

        closed = TrendingTopicsProvider(external_allowed=lambda: False, fetch=fetch)
        blocked = closed.topics(count=3)
        self.assertEqual(blocked["topics"], [])
        self.assertEqual(blocked["source"], "unavailable")
        self.assertEqual(calls, [])

        opened = TrendingTopicsProvider(external_allowed=lambda: True, fetch=fetch)
        allowed = opened.topics(count=3)
        self.assertTrue(allowed["topics"])
        self.assertEqual(len(calls), 1)

    async def test_an_external_tool_step_is_denied_when_the_control_is_off(self) -> None:
        # The declarations (git write, run tests) reach the SAME risk manager
        # the step runner consults through the pipeline's own declare step,
        # which runs at the start of every specialist run — that is what makes
        # the control live during a run rather than nominal.
        pipeline = self.app._build_specialist_pipeline()
        pipeline._declare(self.app.developer_agent)
        self.assertIsNotNone(self.app.risk.declared_for("developer.git_write"))
        step = SimpleNamespace(id="step-1", action="reason", tool="developer.git_write")

        await self.app.apply_privacy_settings(allow_external_tools=False)
        with self.assertRaises(PrivacyDenied) as refused:
            await self.app._run_plan_step(step, None)
        self.assertEqual(refused.exception.decision.action.value, "external_tool")

        await self.app.apply_privacy_settings(allow_external_tools=True)
        with self.assertRaises(RuntimeError) as raised:
            await self.app._run_plan_step(step, None)
        self.assertNotIsInstance(raised.exception, PrivacyDenied)

    async def test_a_mode_change_is_announced_on_the_one_bus(self) -> None:
        await self.app.apply_privacy_settings(execution_mode="local_only")

        events = self.app.event_bus.recent(type_="privacy.mode_changed", limit=5)
        self.assertTrue(events)
        self.assertEqual(events[-1].payload.get("mode"), "local_only")
        self.assertFalse(events[-1].payload.get("cloud_allowed"))

    async def test_a_closed_search_is_announced_as_a_denied_action(self) -> None:
        await self.app.apply_privacy_settings(allow_external_search=False)

        await self.app.research_task("what is the latest news?", allow_web=True)

        events = self.app.event_bus.recent(type_="privacy.denied", limit=5)
        self.assertTrue(events)
        self.assertEqual(events[-1].payload.get("action"), "external_search")
        self.assertIn("external_search", events[-1].payload.get("reason", ""))

    def test_the_resource_report_carries_resident_model_sizes(self) -> None:
        report = self.app.resource_status()

        self.assertIn("loaded_model_sizes", report)
        self.assertIsInstance(report["loaded_model_sizes"], list)

    def test_the_diagnostic_timeout_is_configuration_not_a_constant(self) -> None:
        self.assertEqual(
            self.app.diagnostics.timeout_seconds,
            self.app.config.resources.diagnostics_timeout_seconds,
        )
        configured = ResourceSettings.from_mapping({"diagnostics_timeout_seconds": 1.5})
        self.assertEqual(configured.diagnostics_timeout_seconds, 1.5)
        clamped = ResourceSettings.from_mapping({"diagnostics_timeout_seconds": 0})
        self.assertEqual(clamped.diagnostics_timeout_seconds, 0.1)

    async def test_a_scheduled_request_still_runs_through_the_ordinary_path(self) -> None:
        # The privacy layer does not shadow the automation layer: a local,
        # deterministic scheduled request is unaffected by LOCAL_ONLY.
        await self.app.apply_privacy_settings(execution_mode="local_only")
        response = await self.app.handle_request("what is my RAM usage?")

        self.assertIn("RAM", response.summary)
        self.assertTrue(response.route)


class PrivacyGatedDecisionProviderTests(unittest.TestCase):
    """14.3: a configured remote decision provider obeys allow_remote_model."""

    class _Inner:
        name = "remote:test"

        def __init__(self) -> None:
            self.calls = 0

        def decide(self, request: object) -> None:
            self.calls += 1
            return None

    def test_a_closed_policy_declines_without_consulting_the_inner_provider(self) -> None:
        inner = self._Inner()
        closed = PrivacyGatedDecisionProvider(inner, enabled=lambda: False)

        self.assertFalse(closed.enabled)
        self.assertIsNone(closed.decide(object()))
        self.assertEqual(inner.calls, 0)
        self.assertEqual(closed.name, "remote:test")

    def test_an_open_policy_forwards_to_the_inner_provider(self) -> None:
        inner = self._Inner()
        opened = PrivacyGatedDecisionProvider(inner, enabled=lambda: True)

        self.assertTrue(opened.enabled)
        self.assertIsNone(opened.decide(object()))
        self.assertEqual(inner.calls, 1)

    def test_a_broken_policy_reads_as_closed(self) -> None:
        def broken() -> bool:
            raise RuntimeError("policy unavailable")

        gated = PrivacyGatedDecisionProvider(self._Inner(), enabled=broken)

        self.assertFalse(gated.enabled)
        self.assertIsNone(gated.decide(object()))



if __name__ == "__main__":
    unittest.main()
