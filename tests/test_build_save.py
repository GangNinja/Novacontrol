"""Build tab save flow: drafted code becomes a real file on disk.

The Build tab could draft code (LLM or plan) but nothing wrote it to disk —
"nothing is getting coded". These tests pin the closing half of the loop:

  * safe_artifact_name: traversal-proof, extension-aware sanitizing;
  * save_build_artifact: writes into build_workspace/, rejects empty content,
    records activity;
  * POST /build/save over HTTP: success shape + 422 on empty content.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from novacontrol.core.build_workspace import safe_artifact_name


class SafeArtifactNameTests(unittest.TestCase):
    def test_plain_name_passes_through(self) -> None:
        self.assertEqual(safe_artifact_name("calculator.py"), "calculator.py")

    def test_directory_components_are_stripped(self) -> None:
        # Traversal components vanish; the bare stem keeps the DEFAULT
        # extension ("passwd" had none — .py comes from the language).
        self.assertEqual(safe_artifact_name("../../etc/passwd", "python"), "passwd.py")
        self.assertEqual(safe_artifact_name("..\\..\\evil.py", "python"), "evil.py")
        self.assertEqual(safe_artifact_name("a/b/c/main.go", "go"), "main.go")

    def test_unknown_or_missing_extension_falls_back_to_language(self) -> None:
        self.assertEqual(safe_artifact_name("script", "python"), "script.py")
        # A known-language suffix wins over the requested language default.
        self.assertEqual(safe_artifact_name("main.go", "python"), "main.go")
        # A plausible-looking suffix (tsx) passes through as the extension.
        self.assertEqual(safe_artifact_name("app.tsx", "javascript"), "app.tsx")
        # The LAST suffix segment wins when it looks like an extension
        # (1-8 alphanumerics); earlier dots become underscores.
        self.assertEqual(safe_artifact_name("note.name.with.dots", "rust"), "note_name_with.dots")

    def test_unsafe_characters_become_underscores(self) -> None:
        self.assertEqual(safe_artifact_name("my cool app?.py"), "my_cool_app.py")
        self.assertEqual(safe_artifact_name("!!!", "python"), "artifact.py")

    def test_long_stems_are_capped(self) -> None:
        name = safe_artifact_name("x" * 200 + ".py")
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.endswith(".py"))


class SaveBuildArtifactTests(unittest.TestCase):
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

    def test_save_writes_file_inside_workspace(self) -> None:
        app = self._app()
        result = app.save_build_artifact(
            filename="calculator.py", content="def add(a, b):\n    return a + b\n", language="python", goal="calculator"
        )
        target = Path(result["path"])
        self.assertTrue(target.exists())
        self.assertIn("build_workspace", target.parts)
        self.assertEqual(target.read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")
        self.assertEqual(result["bytes"], len("def add(a, b):\n    return a + b\n".encode("utf-8")))

    def test_save_rejects_empty_content(self) -> None:
        app = self._app()
        with self.assertRaises(ValueError):
            app.save_build_artifact(filename="x.py", content="   ")

    def test_save_never_escapes_the_workspace(self) -> None:
        app = self._app()
        result = app.save_build_artifact(filename="../escape.py", content="evil = True")
        target = Path(result["path"])
        self.assertEqual(target.parent.resolve(), (Path.cwd() / "build_workspace").resolve())


class BuildSaveApiTests(unittest.TestCase):
    """HTTP surface: /build/save succeeds and 422s on empty content."""

    def test_save_over_http(self) -> None:
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from novacontrol.api.app import create_app

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "novacontrol.api.app.NovaControlApplication"
        ) as app_cls:
            instance = app_cls.return_value
            # start() is awaited in the startup hook: it must be awaitable.
            instance.start = AsyncMock()
            instance.stop = AsyncMock()

            def fake_save(*, filename: str, content: str, language: str = "python", goal: str = ""):
                if not content.strip():
                    raise ValueError("Nothing to save — the artifact is empty.")
                return {
                    "mode": "artifact_saved",
                    "path": str(Path(tmp) / "build_workspace" / filename),
                    "filename": filename,
                    "language": language,
                    "bytes": len(content.encode("utf-8")),
                    "goal": goal,
                }

            instance.save_build_artifact.side_effect = fake_save
            with TestClient(create_app()) as client:
                ok = client.post(
                    "/build/save",
                    json={"filename": "demo.py", "content": "x=1", "language": "python", "goal": "demo"},
                )
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(ok.json()["filename"], "demo.py")
                instance.save_build_artifact.assert_called_once()

                empty = client.post("/build/save", json={"filename": "demo.py", "content": "  "})
                self.assertEqual(empty.status_code, 422)


if __name__ == "__main__":
    unittest.main()
