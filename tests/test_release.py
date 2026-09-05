from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.release import ReleaseReadinessChecker
from novacontrol.release.checklist import REQUIRED_FILES


# The exact versions resolved in the dev venv AND the Docker build (verified at
# build time). fastapi floats starlette with >=, so an explicit pin is required
# to keep header/middleware semantics stable across installs.
FASTAPI_PIN = "fastapi==0.141.1"
STARLETTE_PIN = "starlette==1.6.0"
NO_CACHE_TRIO = "no-cache, no-store, max-age=0, must-revalidate"


class DependencyPinTests(unittest.TestCase):
    """fastapi/starlette are pinned so the no-cache header trio can never drift."""

    def test_fastapi_and_starlette_pinned_to_resolved_versions(self) -> None:
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(FASTAPI_PIN, pyproject, "fastapi must be pinned to the resolved version")
        self.assertIn(STARLETTE_PIN, pyproject, "starlette must be pinned to the resolved version")
        # A floating spec on either would silently undo the pin.
        for floated in ("fastapi>=", "starlette>="):
            self.assertNotIn(floated, pyproject, f"{floated} would allow version drift")

    def test_deploy_cache_header_check_scripts_exist_and_assert_trio(self) -> None:
        for name in ("check_no_cache.ps1", "check_no_cache.cmd"):
            script = Path("scripts") / name
            self.assertTrue(script.exists(), f"missing deploy-time script {script}")
            text = script.read_text(encoding="utf-8")
            self.assertIn(NO_CACHE_TRIO, text, f"{name} must assert the exact no-cache trio")
            self.assertIn("/static/app.js", text, f"{name} must probe the /static/* surface")
            self.assertIn("/", text, f"{name} must probe the index route")


class ReleaseTests(unittest.TestCase):
    def test_release_checker_reports_missing_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report = ReleaseReadinessChecker(temp_dir).check()

            self.assertFalse(report.ready)
            self.assertIn("README.md", report.missing_files)

    def test_release_checker_reports_ready_when_files_exist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for relative in REQUIRED_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ok", encoding="utf-8")

            report = ReleaseReadinessChecker(root).check()

            self.assertTrue(report.ready)
            self.assertEqual(report.missing_files, ())


if __name__ == "__main__":
    unittest.main()
