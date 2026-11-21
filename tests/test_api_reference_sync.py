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
import re
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


ROUTE_DECORATOR = re.compile(r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']')


def _block() -> str:
    text = DOCS.read_text(encoding="utf-8")
    return text[text.find(BEGIN):text.find(END)]


def _generator_module():  # noqa: ANN202 - test helper
    """Load scripts/generate_api_reference.py as a module (it is not a package)."""

    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_api_reference", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_every_route_the_app_serves_is_declared(self) -> None:
        """A handler is not a route until it is declared, and six once were not.

        The app factory registers every endpoint as an ``@app.<verb>("…")``
        decorator on a literal path. Parsing those is enough to prove the
        direction that nothing else pins: a new handler that is never added to
        ``ApiSurface``/``ROUTE_CONSUMERS`` is served, undocumented and invisible
        to the frontend contract — exactly the state ``/build/project``,
        ``/build/artifacts``, ``/build/run`` and the three ``/models/*`` routes
        were in until an audit found them. FastAPI's own ``/docs``, ``/redoc``
        and ``/openapi.json`` are added by the framework, not by a decorator
        here, so no exemption list is needed and none is written.
        """
        source = (REPO / "src" / "novacontrol" / "api" / "app.py").read_text(encoding="utf-8")
        served = {
            (method.upper(), path)
            for method, path in re.findall(
                r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']', source
            )
        }
        declared = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertTrue(served, "the handler parse must find routes, not nothing")
        self.assertEqual(
            served - declared,
            set(),
            "every handler in api/app.py must be declared in ApiSurface and ROUTE_CONSUMERS",
        )
        self.assertEqual(
            declared - served,
            set(),
            "a declared route with no handler in api/app.py is a promise the app cannot keep",
        )

    def test_generated_block_lists_every_route_the_app_serves(self) -> None:
        """A served handler must be documented even if it is not declared.

        The generator used to emit only ``ApiSurface`` entries, so regenerating
        the reference deleted every route registered in ``api/app.py`` without a
        declaration -- 185 market routes at one point.  The block is now built
        from the registrations themselves, so parse the app the same way and
        require a row for each one.
        """

        source = (REPO / "src" / "novacontrol" / "api" / "app.py").read_text(encoding="utf-8")
        block = _block()
        served = ROUTE_DECORATOR.findall(source)
        self.assertTrue(served, "the handler parse must find routes, not nothing")
        for method, path in served:
            with self.subTest(route=f"{method.upper()} {path}"):
                self.assertIn(f"`{method.upper()} {path}`", block)

    def test_the_documented_routes_are_the_served_surface(self) -> None:
        module = _generator_module()
        served = set(module.served_routes())
        declared = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertEqual(set(module.reference_routes()), served | declared)
        rows = re.findall(r"^- `([A-Z]+) ([^`]+)`", _block(), re.MULTILINE)
        self.assertEqual(len(rows), len(module.reference_routes()))

    def test_a_served_route_without_a_declaration_is_still_documented(self) -> None:
        module = _generator_module()
        original = module.served_routes
        try:
            module.served_routes = lambda: [
                *original(),
                ("GET", "/market/undeclared_probe"),
            ]
            block = module.build_section()
        finally:
            module.served_routes = original
        self.assertIn("`GET /market/undeclared_probe`", block)
        self.assertIn("no consumer declared", block)

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
            doctored_path = str(root / "docs" / "API.md")
            Path(doctored_path).write_text(doctored, encoding="utf-8")

            # The generator documents its own checkout, so point it at the copy;
            # the app surface and package still come from this repo.
            result = _run(["--check", "--docs", doctored_path], cwd=root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

            # Regenerating repairs the doctored block.
            result = _run(["--docs", doctored_path], cwd=root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            repaired = (root / "docs" / "API.md").read_text(encoding="utf-8")
            self.assertIn("`GET /health`", repaired)


if __name__ == "__main__":
    unittest.main()
