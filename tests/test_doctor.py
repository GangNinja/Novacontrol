from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.release import EnvironmentDoctor


class EnvironmentDoctorTests(unittest.TestCase):
    def test_doctor_reports_missing_workspace_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report = EnvironmentDoctor(temp_dir).run()

            self.assertFalse(report.ok)
            files = next(check for check in report.checks if check.name == "workspace-files")
            self.assertFalse(files.ok)

    def test_doctor_passes_required_file_check_when_files_exist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for relative in EnvironmentDoctor.required_files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ok", encoding="utf-8")

            report = EnvironmentDoctor(root).run()
            files = next(check for check in report.checks if check.name == "workspace-files")

            self.assertTrue(files.ok)


if __name__ == "__main__":
    unittest.main()
