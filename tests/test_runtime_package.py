from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.release import RuntimePackageBuilder


class RuntimePackageTests(unittest.TestCase):
    def test_runtime_package_reports_missing_scripts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            package = RuntimePackageBuilder(temp_dir).build()

            self.assertFalse(package.ready)
            self.assertIn("scripts/run_dashboard.cmd", package.missing_scripts)

    def test_runtime_package_is_ready_when_scripts_exist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in (
                "run_dashboard.cmd",
                "run_api.cmd",
                "run_tests.cmd",
                "run_doctor.cmd",
                "run_package.cmd",
            ):
                (scripts / name).write_text("@echo off", encoding="utf-8")

            package = RuntimePackageBuilder(root).build()

            self.assertTrue(package.ready)
            self.assertEqual(package.missing_scripts, ())
            self.assertIn("dashboard", {command.name for command in package.commands})


if __name__ == "__main__":
    unittest.main()
