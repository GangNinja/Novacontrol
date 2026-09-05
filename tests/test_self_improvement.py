"""Tests for self-improvement engine: plan, apply, path safety."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.security import ApprovalRequest
from novacontrol.self_improvement import CodeChange, CodeChangeStatus, SelfImprovementEngine
from conftest import AllowGateway


class SelfImprovementTests(unittest.IsolatedAsyncioTestCase):

    async def test_engine_builds_improvement_plan(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "src" / "novacontrol").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "src" / "novacontrol" / "module.py").write_text("VALUE = 1\n")
            (root / "tests" / "test_module.py").write_text("def test_value():\n    assert True\n")

            plan = SelfImprovementEngine(root).plan("make it intelligent and improve itself")

            self.assertEqual(plan.profile.source_files, 1)
            self.assertGreaterEqual(len(plan.actions), 2)
            self.assertIn("python -m unittest discover -s tests", plan.test_commands)

    async def test_apply_requires_approval_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            engine = SelfImprovementEngine(temp_dir)
            change = CodeChange("src/novacontrol/generated.py", "VALUE = 1\n", "demo")
            results = await engine.apply_changes((change,))
            self.assertEqual(results[0].status, CodeChangeStatus.DENIED)

    async def test_apply_writes_after_approval(self) -> None:
        with TemporaryDirectory() as temp_dir:
            engine = SelfImprovementEngine(temp_dir)
            change = CodeChange("src/novacontrol/generated.py", "VALUE = 1\n", "demo")
            results = await engine.apply_changes((change,), approval_gateway=AllowGateway())
            self.assertEqual(results[0].status, CodeChangeStatus.APPLIED)
            self.assertTrue((Path(temp_dir) / "src/novacontrol/generated.py").exists())

    async def test_apply_blocks_path_escape(self) -> None:
        with TemporaryDirectory() as temp_dir:
            engine = SelfImprovementEngine(temp_dir)
            change = CodeChange("../outside.py", "VALUE = 1\n", "escape")
            with self.assertRaises(ValueError):
                await engine.apply_changes((change,), approval_gateway=AllowGateway())


if __name__ == "__main__":
    unittest.main()
