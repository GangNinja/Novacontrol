from __future__ import annotations

import unittest

from novacontrol.core.diagnostics import DiagnosticsRegistry, HealthReport, HealthState


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_runs_sync_and_async_checks(self) -> None:
        diagnostics = DiagnosticsRegistry()

        def sync_check() -> HealthReport:
            return HealthReport(name="sync", state=HealthState.OK)

        async def async_check() -> HealthReport:
            return HealthReport(name="async", state=HealthState.DEGRADED)

        diagnostics.register("sync", sync_check)
        diagnostics.register("async", async_check)

        reports = await diagnostics.run()

        self.assertEqual([report.name for report in reports], ["sync", "async"])
        self.assertEqual(reports[1].state, HealthState.DEGRADED)

    async def test_failing_check_becomes_health_report(self) -> None:
        diagnostics = DiagnosticsRegistry()

        def failing_check() -> HealthReport:
            raise RuntimeError("boom")

        diagnostics.register("bad", failing_check)

        reports = await diagnostics.run()

        self.assertEqual(reports[0].name, "bad")
        self.assertEqual(reports[0].state, HealthState.FAILING)
        self.assertEqual(reports[0].details["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
