"""System telemetry: real numbers, honest absences, and a cheap request path.

The contract under test is the whole point of the feature: every metric either
carries a value this machine actually produced, or says it is unavailable and
why. No test here accepts a fabricated zero in place of a missing sensor, and one
test exists purely to prove that serving a poll never spawns a process.
"""

from __future__ import annotations

import sys
import time
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.telemetry import HardwareTelemetry, SystemTelemetry
from novacontrol.telemetry.hardware import _metric_unavailable

try:  # httpx is optional; the endpoint tests skip without it
    from fastapi.testclient import TestClient as _Client
except ImportError:  # pragma: no cover - environment specific
    _Client = None  # type: ignore[assignment]

_WINDOWS = sys.platform.startswith("win")
_LINUX = sys.platform.startswith("linux")


def _asserts_available_here() -> bool:
    """Cheap metrics come from Win32 counters or /proc — both must work here."""
    return _WINDOWS or _LINUX


class HardwareMetricTests(unittest.TestCase):
    """Each metric is read from the OS, and each absence explains itself."""

    def setUp(self) -> None:
        self.hw = HardwareTelemetry()

    def test_cpu_is_a_real_measurement(self) -> None:
        first = self.hw.cpu()
        if not first["available"]:
            self.assertTrue(first["reason"], "an unavailable metric must say why")
            self.skipTest(f"platform exposes no CPU counters: {first['reason']}")
        self.assertTrue(first["source"])
        self.assertGreaterEqual(first["percent"], 0.0)
        self.assertLessEqual(first["percent"], 100.0)
        # A second read must still be a percentage, not a placeholder.
        second = self.hw.cpu()
        self.assertTrue(second["available"])
        self.assertLessEqual(second["percent"], 100.0)

    def test_memory_totals_are_self_consistent(self) -> None:
        memory = self.hw.memory()
        self.assertTrue(memory["available"], memory.get("reason"))
        self.assertGreater(memory["total_bytes"], 0)
        self.assertGreaterEqual(memory["used_bytes"], 0)
        self.assertAlmostEqual(
            memory["used_bytes"] + memory["available_bytes"],
            memory["total_bytes"],
            delta=2,  # the counters are sampled a moment apart
        )
        expected = round(memory["used_bytes"] / memory["total_bytes"] * 100, 1)
        self.assertAlmostEqual(memory["percent"], expected, delta=0.2)

    def test_memory_matches_an_independent_source(self) -> None:
        """Cross-check the RAM total against the OS's own value, read separately."""
        memory = self.hw.memory()
        if _WINDOWS:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            self.assertTrue(ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)))
            self.assertEqual(memory["total_bytes"], int(status.ullTotalPhys))
        elif _LINUX:
            total_kb = 0
            for line in open("/proc/meminfo", encoding="utf-8"):
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
            self.assertEqual(memory["total_bytes"], total_kb * 1024)
        else:  # pragma: no cover - not exercised on the supported platforms
            self.skipTest("no independent memory source on this platform")

    def test_storage_reports_real_capacity(self) -> None:
        storage = self.hw.storage()
        self.assertTrue(storage["available"], storage.get("reason"))
        self.assertGreater(storage["total_bytes"], 0)
        self.assertGreater(storage["used_bytes"], 0)
        self.assertLess(storage["used_bytes"], storage["total_bytes"])
        self.assertAlmostEqual(
            storage["used_bytes"] + storage["free_bytes"], storage["total_bytes"], delta=1
        )

    def test_battery_either_measures_or_explains(self) -> None:
        battery = self.hw.battery()
        if battery["available"]:
            self.assertGreaterEqual(battery["percent"], 0.0)
            self.assertLessEqual(battery["percent"], 100.0)
            self.assertIsInstance(battery["on_ac"], bool)
        else:
            self.assertTrue(battery["reason"])
            self.assertNotIn("percent", battery)

    def test_uptime_and_identity_are_real(self) -> None:
        host = self.hw.host()
        self.assertTrue(host["hostname"])
        self.assertTrue(host["cpu_model"])
        self.assertGreaterEqual(host["cpu_count"], 1)
        if _asserts_available_here():
            self.assertGreater(host["uptime_seconds"], 0)

    def test_uptime_is_its_own_metric(self) -> None:
        """Uptime is a card, so it must be a metric — not only a host field.

        A card that reads `hw.host.uptime_seconds` cannot reach its own case in
        the renderer, because the card's availability gate looks at `hw.uptime`:
        it rendered "Unavailable" on a machine that had just reported 3d 9h.
        """
        metric = self.hw.snapshot()["uptime"]
        self.assertIn("available", metric)
        if metric["available"]:
            self.assertGreater(metric["seconds"], 0)
            self.assertTrue(metric["source"])
            self.assertEqual(self.hw.host()["uptime_seconds"], metric["seconds"])
        else:
            self.assertTrue(metric["reason"])
            self.assertNotIn("seconds", metric)

    def test_unsampled_slow_metrics_are_unavailable_not_zero(self) -> None:
        """GPU/network/temperature before the sampler runs: no invented numbers."""
        snapshot = self.hw.snapshot()
        for key in ("gpu", "network", "temperature"):
            metric = snapshot[key]
            if metric["available"]:
                continue
            self.assertTrue(metric["reason"], f"{key} must explain its absence")
            self.assertNotIn("percent", metric)
            self.assertNotIn("download_bps", metric)
            self.assertNotIn("celsius", metric)
        # And the payload says the slow sample has not happened yet.
        self.assertFalse(snapshot["sampler"]["expensive_sampled"])

    def test_every_metric_declares_availability(self) -> None:
        for name, metric in self.hw.snapshot().items():
            if name in {"host", "sampler"}:
                continue
            self.assertIn("available", metric, f"{name} must declare availability")
            self.assertIsInstance(metric["available"], bool)


class ScalarArrayNormalizationTests(unittest.TestCase):
    """PowerShell serializes a one-element array as a bare scalar.

    `@('Wi-Fi') | ConvertTo-Json` is `"Wi-Fi"`, so a one-adapter machine hands
    the UI a string where it expects a list — the wire shape is normalized in
    the collector instead of trusted.
    """

    def test_single_interface_scalar_becomes_a_list(self) -> None:
        from novacontrol.telemetry.hardware import _as_text_list

        self.assertEqual(_as_text_list("Wi-Fi"), ["Wi-Fi"])
        self.assertEqual(_as_text_list(["Wi-Fi"]), ["Wi-Fi"])
        self.assertEqual(_as_text_list(["Wi-Fi", "Ethernet"]), ["Wi-Fi", "Ethernet"])
        self.assertEqual(_as_text_list(None), [])
        self.assertEqual(_as_text_list(""), [])
        self.assertEqual(_as_text_list(["", "  "]), [])

    def test_powershell_payload_is_normalized(self) -> None:
        hw = HardwareTelemetry(clock=lambda: 100.0)
        with mock.patch.object(
            hw,
            "_run_windows_probe",
            return_value={
                "net_interfaces": "Wi-Fi",  # the one-adapter scalar shape
                "net_received_bytes": 500,
                "net_sent_bytes": 100,
                "gpu_name": "Intel(R) Arc(TM) Graphics",
                "gpu_util": 12.0,
            },
        ):
            payload = hw.sample_expensive()
        self.assertEqual(payload["net_interfaces"], ["Wi-Fi"])
        self.assertEqual(hw.network()["interfaces"], ["Wi-Fi"])


class NetworkRateTests(unittest.TestCase):
    """Byte rates must come from two distinct counter reads."""

    def test_one_sample_is_not_a_rate(self) -> None:
        hw = HardwareTelemetry(clock=lambda: 100.0)
        hw._record_network({"net_received_bytes": 1000, "net_sent_bytes": 500})
        metric = hw.network()
        self.assertTrue(metric["available"])
        self.assertEqual(metric["received_bytes"], 1000)
        self.assertFalse(metric["rate_available"])
        self.assertTrue(metric["reason"])
        self.assertNotIn("download_bps", metric)

    def test_two_samples_produce_a_real_rate(self) -> None:
        now = [100.0]
        hw = HardwareTelemetry(clock=lambda: now[0])
        hw._record_network({"net_received_bytes": 1000, "net_sent_bytes": 500})
        now[0] = 102.0
        hw._record_network({"net_received_bytes": 3000, "net_sent_bytes": 1500})
        metric = hw.network()
        self.assertTrue(metric["rate_available"])
        self.assertEqual(metric["download_bps"], 1000.0)
        self.assertEqual(metric["upload_bps"], 500.0)
        self.assertEqual(metric["window_seconds"], 2.0)

    def test_missing_counters_are_reported_as_missing(self) -> None:
        hw = HardwareTelemetry()
        metric = hw.network()
        self.assertFalse(metric["available"])
        self.assertTrue(metric["reason"])


class ServiceTests(unittest.TestCase):
    """The service merges hardware with the app's own status, and owns the thread."""

    class _StubApp:
        class _Vision:
            has_vision_model = False

        vision = _Vision()

        def status(self) -> dict[str, Any]:
            return {
                "runtime_started": True,
                "modules": ["command", "explore"],
                "tracked_tasks": 4,
                "projects": 1,
                "automation_workflows": 2,
                "desktop_runner": "LocalDesktopRunner",
                "browser_runner": "PlaywrightBrowserRunner",
                "browser_adapter_available": True,
                "brain": {
                    "mode": "auto",
                    "effective_mode": "scratch",
                    "provider": "scratch",
                    "model": "built-in",
                    "model_configured": False,
                    "cloud": {"configured": True, "state": "idle"},
                },
                "tasks": [
                    {"id": "1", "title": "index the repo", "status": "running"},
                    {"id": "2", "title": "audit the layout", "status": "pending"},
                    {"id": "3", "title": "old work", "status": "completed"},
                ],
            }

    def test_active_tasks_come_from_the_task_records(self) -> None:
        service = SystemTelemetry(self._StubApp(), background=False)
        nova = service.payload()["novacontrol"]
        self.assertEqual(nova["active_tasks"], 2)
        self.assertEqual(nova["active_task_titles"], ["index the repo", "audit the layout"])
        self.assertEqual(nova["tracked_tasks"], 4)

    def test_engine_and_vision_status_are_reported_honestly(self) -> None:
        service = SystemTelemetry(self._StubApp(), background=False)
        nova = service.payload()["novacontrol"]
        self.assertEqual(nova["ai_engine"]["effective_mode"], "scratch")
        self.assertEqual(nova["ai_engine"]["provider"], "scratch")
        self.assertTrue(nova["ai_engine"]["cloud_configured"])
        self.assertFalse(nova["vision"]["available"])
        self.assertTrue(nova["vision"]["reason"])
        self.assertEqual(nova["automation"]["workflows"], 2)

    def test_payload_shape_is_stable(self) -> None:
        payload = SystemTelemetry(self._StubApp(), background=False).payload()
        self.assertEqual(set(payload), {"generated_at", "hardware", "novacontrol"})
        self.assertIn("sampler", payload["hardware"])
        for key in (
            "cpu", "memory", "storage", "gpu", "network", "battery", "temperature",
            "uptime", "host",
        ):
            self.assertIn(key, payload["hardware"])

    def test_absent_app_is_reported_not_guessed(self) -> None:
        nova = SystemTelemetry(background=False).payload()["novacontrol"]
        self.assertFalse(nova["available"])
        self.assertTrue(nova["reason"])

    class _StubHardware:
        """Stands in for the probe: records calls, never touches the OS."""

        def __init__(self) -> None:
            self.calls = 0

        def sample_expensive(self) -> dict[str, Any]:
            self.calls += 1
            return {}

        def snapshot(self) -> dict[str, Any]:
            return {"sampler": {"expensive_sampled": self.calls > 0}}

    def test_thread_lifecycle(self) -> None:
        service = SystemTelemetry(self._StubApp(), background=False)
        self.assertFalse(service.start(), "background=False must not start a thread")
        service.stop()  # stopping an unstarted service is safe
        self.assertFalse(service.payload()["hardware"]["sampler"]["sampled_once"])

        hardware = self._StubHardware()
        live = SystemTelemetry(self._StubApp(), cadence_seconds=30.0, hardware=hardware)  # type: ignore[arg-type]
        self.assertTrue(live.start())
        self.assertFalse(live.start(), "start() is idempotent")
        for _ in range(100):
            if hardware.calls:
                break
            time.sleep(0.02)
        live.stop()
        self.assertGreaterEqual(hardware.calls, 1, "the sampler should sample on start")


class TelemetryEndpointTests(unittest.TestCase):
    """`/system/telemetry` over real HTTP, with the sampler off (as in tests)."""

    def setUp(self) -> None:
        if _Client is None:
            self.skipTest("httpx not installed")
        self._tmp = TemporaryDirectory()
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
        self._client = _Client(create_app())
        self._client.__enter__()

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _isolated_application(self, **kwargs: object) -> NovaControlApplication:
        kwargs["data_dir"] = self._tmp.name
        return NovaControlApplication(**kwargs)  # type: ignore[arg-type]

    def test_endpoint_serves_real_metrics(self) -> None:
        response = self._client.get("/system/telemetry")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        hardware = payload["hardware"]
        self.assertTrue(hardware["memory"]["available"])
        self.assertGreater(hardware["memory"]["total_bytes"], 0)
        self.assertGreater(hardware["storage"]["total_bytes"], 0)
        self.assertIn("available", hardware["temperature"])
        self.assertTrue(payload["novacontrol"]["available"])
        self.assertIn("active_tasks", payload["novacontrol"])
        self.assertEqual(payload["hardware"]["sampler"]["expensive_sampled"], False)

    def test_serving_a_poll_never_spawns_a_process(self) -> None:
        """The expensive probe belongs to the sampler thread, never a request."""
        with mock.patch.object(
            HardwareTelemetry,
            "_run_windows_probe",
            side_effect=AssertionError("the request path must not spawn the probe"),
        ) as probe:
            for _ in range(3):
                self.assertEqual(self._client.get("/system/telemetry").status_code, 200)
            probe.assert_not_called()

    def test_polling_is_cheap(self) -> None:
        """Ten polls of the live metrics stay well inside an interaction budget."""
        self._client.get("/system/telemetry")  # warm the first CPU delta
        start = time.perf_counter()
        for _ in range(10):
            self.assertEqual(self._client.get("/system/telemetry").status_code, 200)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"10 telemetry polls took {elapsed:.2f}s")


class UnavailableShapeTests(unittest.TestCase):
    def test_unavailable_helper_never_carries_a_value(self) -> None:
        metric = _metric_unavailable("no sensor", "none")
        self.assertFalse(metric["available"])
        self.assertEqual(metric["reason"], "no sensor")
        self.assertNotIn("percent", metric)


if __name__ == "__main__":
    unittest.main()
