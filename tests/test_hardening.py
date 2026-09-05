from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.release import ReleaseHardeningChecker


class HardeningTests(unittest.TestCase):
    def test_hardening_reports_missing_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report = ReleaseHardeningChecker(temp_dir).run()

            self.assertFalse(report.ready)

    def test_hardening_reports_ready_workspace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_workspace(root)

            report = ReleaseHardeningChecker(root).run()

            self.assertTrue(report.ready)

    def _write_workspace(self, root: Path) -> None:
        (root / "src" / "novacontrol").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "scripts").mkdir()
        (root / "pyproject.toml").write_text("ok", encoding="utf-8")
        (root / "src" / "novacontrol" / "__main__.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "src" / "novacontrol" / "application.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "tests" / "test_placeholder.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
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
