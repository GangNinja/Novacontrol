"""docs/API.md is generated: the REST reference cannot drift from the wire contract.

``scripts/generate_api_reference.py`` splices the route list built from
``novacontrol.api.route_consumers.ROUTE_CONSUMERS`` + ``ApiSurface`` into the
marked block of ``docs/API.md``. These tests pin the invariants that keep the
reference honest: the block exists exactly once, every declared route is
listed, the shared registry and the wire contract agree, and ``--check``
fails when the block is doctored (and regenerating repairs it).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from novacontrol.api import ApiSurface
from novacontrol.api.route_consumers import ROUTE_CONSUMERS

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs" / "API.md"
GENERATOR = REPO / "scripts" / "generate_api_reference.py"
BEGIN = "<!-- BEGIN GENERATED: api-reference"
END = "<!-- END GENERATED: api-reference -->"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class ApiReferenceSyncTests(unittest.TestCase):
    def test_generated_block_present_exactly_once(self) -> None:
        text = DOCS.read_text(encoding="utf-8")
        self.assertEqual(text.count(BEGIN), 1, "docs/API.md must contain exactly one generated block")
        self.assertEqual(text.count(END), 1, "docs/API.md must contain exactly one generated block")

    def test_every_declared_route_appears_in_the_reference(self) -> None:
        text = DOCS.read_text(encoding="utf-8")
        for route in ApiSurface.default().routes:
            with self.subTest(route=f"{route.method} {route.path}"):
                self.assertIn(f"`{route.method} {route.path}`", text)

    def test_registry_and_surface_agree(self) -> None:
        """The shared table and the wire contract must name the same routes."""
        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertEqual(routes, set(ROUTE_CONSUMERS))

    def test_check_mode_passes_on_the_current_tree(self) -> None:
        result = _run(["--check"], cwd=REPO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_mode_fails_when_the_block_is_doctored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            text = DOCS.read_text(encoding="utf-8")
            doctored = text.replace("`GET /health`", "`GET /healthz`")
            self.assertNotEqual(doctored, text, "doctoring must change the doc")
            (root / "docs" / "API.md").write_text(doctored, encoding="utf-8")

            result = _run(["--check"], cwd=root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

            # Regenerating repairs the doctored block.
            result = _run([], cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            repaired = (root / "docs" / "API.md").read_text(encoding="utf-8")
            self.assertIn("`GET /health`", repaired)


if __name__ == "__main__":
    unittest.main()
