"""Build tab project mode: multi-file coding agent + runnable saved artifacts.

Build went from one file per goal to actual PROJECTS: the agent drafts a file
map (2-5 files + entry point), runs the entry in the sandbox, feeds the real
error back, and fixes until clean. Saved workspace artifacts can be re-run.
These tests pin:

  * parse_project_files: JSON map, fenced content, traversal rejection;
  * run_project_agent: fail-then-fix across files with a scripted provider,
    honest give-up, non-executable language skip;
  * run_saved_artifact: real subprocess run of a saved .py, 422-style
    rejection of missing/non-runnable files;
  * list_workspace_artifacts: newest-first with runnable flags;
  * scaffold fallback: no model -> deterministic runnable multi-file starter
    (mode code_project, generated_by scaffold, sandbox-verified entry point);
  * HTTP surface: /build/project scaffold without a model, /build/run shape.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from novacontrol.core.code_agent import (
    parse_project_files,
    run_project_agent,
)


def _scripted_provider(replies: list[str]):
    """Completion provider returning queued replies (complete() surface)."""

    class Provider:
        name = "scripted"

        async def complete(self, messages: Any, max_tokens: int = 0) -> str:
            return replies.pop(0) if replies else "{}"

    return Provider()


def _file_map(entry_code: str, helper_code: str) -> str:
    return json.dumps({
        "entry": "main.py",
        "files": [
            {"path": "main.py", "content": entry_code},
            {"path": "helper.py", "content": helper_code},
        ],
    })


class ParseProjectFilesTests(unittest.TestCase):
    def test_plain_json_map_parses(self) -> None:
        files = parse_project_files(_file_map("print(1)", "X = 2"))
        self.assertEqual([f["path"] for f in files], ["main.py", "helper.py"])
        self.assertEqual(files[0]["content"], "print(1)\n")

    def test_fenced_json_and_fenced_content_parse(self) -> None:
        reply = (
            "Here is your project:\n```json\n"
            + _file_map("```python\nprint('hi')\n```", "X = 1")
            + "\n```\nEnjoy!"
        )
        files = parse_project_files(reply)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["content"], "print('hi')\n")

    def test_traversal_paths_are_dropped(self) -> None:
        reply = json.dumps({"files": [
            {"path": "../evil.py", "content": "x = 1"},
            {"path": "ok.py", "content": "y = 2"},
        ]})
        files = parse_project_files(reply)
        self.assertEqual([f["path"] for f in files], ["ok.py"])

    def test_no_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_project_files("no structure here at all")

    def test_empty_files_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_project_files(json.dumps({"files": []}))


class ProjectAgentTests(unittest.TestCase):
    def test_fail_then_fix_across_files(self) -> None:
        broken = _file_map(
            "from helper import add\nprint(add(1))",  # add() needs 2 args
            "def add(a, b):\n    return a + b",
        )
        fixed = _file_map(
            "from helper import add\nprint(add(1, 2))",
            "def add(a, b):\n    return a + b",
        )
        calls: list[tuple] = []

        async def runner(language, files, entry):
            calls.append((tuple(p for p, _ in files), entry))
            joined = dict(files).get("main.py", "")
            if "add(1, 2)" in joined:
                return True, "3"
            return False, "TypeError: add() missing 1 required positional argument"

        result = run_project_agent(
            "add two numbers", "python", _scripted_provider([broken, fixed]),
            runner=runner,
        )
        import asyncio

        out = asyncio.run(result)
        self.assertTrue(out.ran_ok)
        self.assertEqual(out.entry, "main.py")
        self.assertEqual(out.fix_rounds, 1)
        self.assertEqual(out.final_output, "3")
        kinds = [s.kind for s in out.steps]
        self.assertEqual(kinds, ["draft", "run", "fixed", "run"])

    def test_budget_exhaustion_reports_honest_failure(self) -> None:
        always_broken = _file_map("print(1/", "X = 1")

        async def runner(language, files, entry):
            return False, "SyntaxError: unexpected EOF"

        import asyncio

        out = asyncio.run(run_project_agent(
            "doomed goal", "python", _scripted_provider([always_broken]),
            max_fix_rounds=1, runner=runner,
        ))
        self.assertFalse(out.ran_ok)
        self.assertIn("SyntaxError", out.final_output)
        self.assertTrue(any(s.kind == "run" for s in out.steps))

    def test_non_executable_language_is_skipped_honestly(self) -> None:
        sql_map = json.dumps({"files": [
            {"path": "query.sql", "content": "SELECT 1;"},
            {"path": "schema.sql", "content": "CREATE TABLE t(id INT);"},
        ]})

        async def runner(language, files, entry):
            raise AssertionError("SQL must never execute")

        import asyncio

        out = asyncio.run(run_project_agent(
            "make some sql", "sql", _scripted_provider([sql_map]), runner=runner,
        ))
        self.assertFalse(out.ran_ok)
        self.assertTrue(any(s.kind == "skipped" for s in out.steps))


class ProjectScaffoldFallbackTests(unittest.TestCase):
    """No model (or a failing one) still yields a runnable multi-file starter."""

    def setUp(self) -> None:
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self._previous_cwd = Path.cwd()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        import os

        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def _app(self):
        from novacontrol.application import NovaControlApplication

        return NovaControlApplication(data_dir=Path(self._tmp.name) / "data")

    def test_python_scaffold_shape(self) -> None:
        from novacontrol.application import _scaffold_project

        files, entry = _scaffold_project("a calculator", "python")
        paths = [f["path"] for f in files]
        self.assertEqual(entry, "main.py")
        self.assertEqual(len(paths), 3)
        self.assertIn("main.py", paths)
        self.assertTrue(any(p.startswith("test_") for p in paths))
        # The logic module name must be importable from main.py.
        logic = next(p for p in paths if p.endswith("_logic.py"))
        module = logic[:-3]
        main_src = next(f["content"] for f in files if f["path"] == "main.py")
        self.assertIn(f"from {module} import", main_src)

    def test_python_scaffold_entry_actually_runs(self) -> None:
        from novacontrol.application import _scaffold_project
        from novacontrol.core.code_agent import run_sandboxed

        import asyncio

        files, entry = _scaffold_project("a palindrome checker", "python")
        ran_ok, output = asyncio.run(run_sandboxed(
            "python", [(f["path"], f["content"]) for f in files], entry,
        ))
        self.assertTrue(ran_ok)
        self.assertIn("demo ->", output)

    def test_javascript_scaffold_runs_when_node_exists(self) -> None:
        import shutil

        if not shutil.which("node"):
            self.skipTest("no node runtime")
        from novacontrol.application import _scaffold_project
        from novacontrol.core.code_agent import run_sandboxed

        import asyncio

        files, entry = _scaffold_project("a string utils project", "javascript")
        paths = [f["path"] for f in files]
        self.assertIn("package.json", paths)
        ran_ok, _ = asyncio.run(run_sandboxed(
            "javascript", [(f["path"], f["content"]) for f in files], entry,
        ))
        self.assertTrue(ran_ok)

    def test_non_runnable_language_gets_honest_skip(self) -> None:
        import asyncio

        app = self._app()
        result = asyncio.run(app.build_code_project("some schema", language="sql"))
        self.assertEqual(result["generated_by"], "scaffold")
        self.assertFalse(result["ran_ok"])
        self.assertTrue(any(s["kind"] == "skipped" for s in result["steps"]))

    def test_build_code_project_falls_back_without_model(self) -> None:
        import asyncio

        app = self._app()  # default brain: echo provider, model not configured
        result = asyncio.run(app.build_code_project("a vowel counter", language="python"))
        self.assertEqual(result["mode"], "code_project")
        self.assertEqual(result["generated_by"], "scaffold")
        self.assertTrue(result["ran_ok"])
        self.assertEqual(result["model"], "")
        self.assertIn("Scaffolded", result["summary"])
        self.assertIn("connect", result["summary"])
        self.assertTrue(any(s["kind"] == "run" for s in result["steps"]))

    def test_fallback_after_agent_failure_records_reason(self) -> None:
        import asyncio

        app = self._app()

        class DyingProvider:
            name = "dying"

            async def complete(self, messages: Any, max_tokens: int = 0) -> str:
                raise RuntimeError("provider exploded")

        app.brain.completion_provider = DyingProvider()
        result = asyncio.run(app.build_code_project("a sorter", language="python"))
        self.assertEqual(result["generated_by"], "scaffold")
        self.assertTrue(result["ran_ok"])  # scaffold still verified
        gave_up = next(s for s in result["steps"] if s["kind"] == "gave_up")
        self.assertIn("provider exploded", gave_up["detail"])


class RunSavedArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_cwd = Path.cwd()
        import os

        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        import os

        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def _app(self):
        from novacontrol.application import NovaControlApplication

        return NovaControlApplication(data_dir=Path(self._tmp.name) / "data")

    def test_saved_python_artifact_actually_runs(self) -> None:
        app = self._app()
        app.save_build_artifact(
            filename="answer.py", language="python",
            content="VALUE = 21\nprint('answer=', VALUE * 2)",
        )
        import asyncio

        result = asyncio.run(app.run_saved_artifact("answer.py"))
        self.assertTrue(result["ran_ok"])
        self.assertIn("answer= 42", result["output"])

    def test_missing_file_is_a_value_error(self) -> None:
        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(self._app().run_saved_artifact("ghost.py"))

    def test_non_runnable_suffix_is_rejected(self) -> None:
        app = self._app()
        app.save_build_artifact(filename="notes.txt", language="python", content="hello")
        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(app.run_saved_artifact("notes.txt"))

    def test_workspace_listing_flags_runnable(self) -> None:
        app = self._app()
        app.save_build_artifact(filename="a.py", language="python", content="print(1)")
        app.save_build_artifact(filename="b.sql", language="sql", content="SELECT 1;")
        listing = app.list_workspace_artifacts()
        flags = {a["filename"]: a["runnable"] for a in listing["artifacts"]}
        self.assertTrue(flags["a.py"])
        self.assertFalse(flags["b.sql"])


class BuildProjectApiTests(unittest.TestCase):
    """HTTP surface: project drafting requires a model; run/list are honest."""

    def setUp(self) -> None:
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self._previous_cwd = Path.cwd()
        os.chdir(self._tmp.name)
        from fastapi.testclient import TestClient

        from novacontrol.api.app import create_app

        with mock.patch.dict(os.environ, {"NOVACONTROL_API_TOKEN": "t"}):
            self.app = create_app()
        self.client = TestClient(self.app)
        self.client.headers.update({"Authorization": "Bearer t"})

    def tearDown(self) -> None:
        import os

        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def test_project_without_model_returns_scaffold(self) -> None:
        response = self.client.post(
            "/build/project", json={"goal": "a calculator", "language": "python"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "code_project")
        self.assertEqual(body["generated_by"], "scaffold")
        self.assertTrue(body["ran_ok"])  # scaffold entry runs clean in the sandbox
        self.assertEqual(body["entry"], "main.py")
        self.assertGreaterEqual(len(body["files"]), 2)
        self.assertIn("Scaffolded", body["summary"])
        self.assertTrue(any(s["kind"] == "run" and "verified" in s["detail"] for s in body["steps"]))

    def test_run_missing_artifact_is_422(self) -> None:
        response = self.client.post("/build/run", json={"filename": "ghost.py"})
        self.assertEqual(response.status_code, 422)

    def test_artifacts_listing_shape(self) -> None:
        response = self.client.get("/build/artifacts")
        self.assertEqual(response.status_code, 200)
        self.assertIn("artifacts", response.json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
