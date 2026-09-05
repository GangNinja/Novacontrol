from __future__ import annotations

import unittest

from novacontrol.performance import LoadTester, MetricsRegistry, Profiler, TtlCache


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_registry_tracks_values(self) -> None:
        metrics = MetricsRegistry()

        metrics.increment("requests")
        metrics.increment("requests", 2)
        metrics.gauge("queue_depth", 4)

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["requests"], 3)
        self.assertEqual(snapshot["gauges"]["queue_depth"], 4)

    async def test_ttl_cache_expires_values(self) -> None:
        cache: TtlCache[str] = TtlCache()

        cache.set("key", "value", ttl_seconds=-1)

        self.assertIsNone(cache.get("key"))
        self.assertEqual(cache.size(), 0)

    async def test_profiler_measures_async_operation(self) -> None:
        async def operation() -> str:
            return "ok"

        result = await Profiler().measure_async("demo", operation)

        self.assertEqual(result.result, "ok")
        self.assertGreaterEqual(result.duration_seconds, 0)

    async def test_load_tester_counts_successes(self) -> None:
        async def operation() -> None:
            return None

        result = await LoadTester().run(operation, requests=5, concurrency=2)

        self.assertEqual(result.total_requests, 5)
        self.assertEqual(result.successes, 5)
        self.assertEqual(result.failures, 0)


if __name__ == "__main__":
    unittest.main()
