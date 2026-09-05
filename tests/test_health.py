from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.release import HealthLevel, SystemHealthMonitor


class HealthMonitorTests(unittest.TestCase):
    def test_health_monitor_warns_when_app_status_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report = SystemHealthMonitor(temp_dir).run()

            self.assertFalse(report.ok)
            self.assertEqual(report.level, HealthLevel.ERROR)

    def test_health_monitor_reports_ok_with_ready_workspace_and_status(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_required_workspace(root)
            status = {
                "modules": ("memory", "planning"),
                "desktop_runner": "LocalDesktopRunner",
                "browser_runner": "PlaywrightBrowserRunner",
                "browser_adapter_available": True,
            }

            report = SystemHealthMonitor(root).run(status)

            self.assertTrue(report.ok)
            self.assertEqual(report.level, HealthLevel.OK)

    def _write_required_workspace(self, root: Path) -> None:
        (root / "src" / "novacontrol").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "pyproject.toml").write_text("ok", encoding="utf-8")
        (root / "src" / "novacontrol" / "__main__.py").write_text("ok", encoding="utf-8")
        (root / "src" / "novacontrol" / "application.py").write_text("ok", encoding="utf-8")
        for name in (
            "run_dashboard.cmd",
            "run_api.cmd",
            "run_tests.cmd",
            "run_doctor.cmd",
            "run_package.cmd",
        ):
            (root / "scripts" / name).write_text("@echo off", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
