"""Tests for desktop automation: controller, runner, events, models."""

from __future__ import annotations

import asyncio
import ast
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from conftest import AllowGateway
from novacontrol.core.events import Event, EventBus
from novacontrol.desktop import (
    DesktopAction,
    DesktopActionStatus,
    DesktopActionType,
    DesktopAutomationController,
    DesktopAutomationModule,
    InMemoryAutomationAuditLog,
    LocalDesktopRunner,
    NoopDesktopRunner,
)
from novacontrol.desktop.controller import parse_desktop_command


class RecordingRunner:
    def __init__(self):
        self.actions = []

    async def run(self, action):
        self.actions.append(action)
        return {"ran": action.type.value, "target": action.target}


# ---------------------------------------------------------------------------
# Table-driven controller plan methods
# ---------------------------------------------------------------------------

PLAN_METHODS: list[tuple[str, dict, str, DesktopActionType]] = [
    ("screenshot", {"save_path": "/tmp/test.png"}, "Take screenshot", DesktopActionType.TAKE_SCREENSHOT),
    ("system_info", {}, "System information", DesktopActionType.SYSTEM_INFO),
    ("list_files", {"directory": "/tmp", "recursive": True}, "List files", DesktopActionType.LIST_FILES),
    ("keyboard_shortcut", {"shortcut": "ctrl+c"}, "Keyboard shortcut", DesktopActionType.KEYBOARD_SHORTCUT),
    ("open_application", {"application": "notepad"}, "Open notepad", DesktopActionType.OPEN_APPLICATION),
    ("execute_script", {"command": "echo hello"}, "Execute script", DesktopActionType.EXECUTE_SCRIPT),
]


class DesktopControllerPlanTests(unittest.IsolatedAsyncioTestCase):
    """Table-driven: each plan method is one subTest."""

    def test_plan_methods(self) -> None:
        controller = DesktopAutomationController()
        for name, kwargs, expected_name, expected_type in PLAN_METHODS:
            with self.subTest(method=name):
                method = getattr(controller, f"plan_{name}")
                workflow = method(**kwargs)
                self.assertEqual(workflow.name, expected_name)
                self.assertEqual(workflow.actions[0].type, expected_type)


# ---------------------------------------------------------------------------
# Approval and audit
# ---------------------------------------------------------------------------

class DesktopApprovalTests(unittest.IsolatedAsyncioTestCase):

    async def test_default_controller_denies_execution(self) -> None:
        controller = DesktopAutomationController()
        results = await controller.execute_workflow(controller.plan_open_application("Code"))
        self.assertEqual(results[0].status, DesktopActionStatus.DENIED)

    async def test_approved_controller_runs_and_audits(self) -> None:
        audit = InMemoryAutomationAuditLog()
        runner = RecordingRunner()
        controller = DesktopAutomationController(
            approval_gateway=AllowGateway(), runner=runner, audit_log=audit,
        )
        results = await controller.execute_workflow(controller.plan_execute_script("echo hello"))
        audit_entries = await audit.read()

        self.assertEqual(results[0].status, DesktopActionStatus.COMPLETED)
        self.assertEqual(runner.actions[0].target, "echo hello")
        self.assertEqual(audit_entries[0].action_id, results[0].action_id)
        self.assertEqual(audit_entries[0].status, "completed")
        datetime.fromisoformat(audit_entries[0].recorded_at)  # timestamped trace

    async def test_denied_action_is_audited_with_timestamp(self) -> None:
        """Denials are traced too — the default gateway refuses before any run."""
        audit = InMemoryAutomationAuditLog()
        controller = DesktopAutomationController(runner=RecordingRunner(), audit_log=audit)
        results = await controller.execute_workflow(controller.plan_open_application("Code"))
        entries = await audit.read()

        self.assertEqual(results[0].status, DesktopActionStatus.DENIED)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, "denied")
        datetime.fromisoformat(entries[0].recorded_at)

    async def test_local_runner_executes_script(self) -> None:
        controller = DesktopAutomationController(
            approval_gateway=AllowGateway(),
            runner=LocalDesktopRunner(command_timeout_seconds=5),
        )
        results = await controller.execute_workflow(controller.plan_execute_script("echo phase21"))
        self.assertEqual(results[0].status, DesktopActionStatus.COMPLETED)
        self.assertEqual(results[0].output["adapter"], "local-desktop")
        self.assertEqual(results[0].output["exit_code"], 0)
        self.assertIn("phase21", results[0].output["stdout"])


# ---------------------------------------------------------------------------
# Command safety: exec-without-a-shell is the injection boundary
# ---------------------------------------------------------------------------

class DesktopCommandSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Shell metacharacters are inert because commands run via create_subprocess_exec
    (never through a shell) after shlex.split turns them into literal argv tokens."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _execute(self, command: str) -> dict:
        controller = DesktopAutomationController(
            approval_gateway=AllowGateway(),
            runner=LocalDesktopRunner(command_timeout_seconds=10),
        )
        workflow = controller.plan_execute_script(command, working_directory=str(self.tmpdir))
        results = await controller.execute_workflow(workflow)
        self.assertEqual(results[0].status, DesktopActionStatus.COMPLETED)
        return dict(results[0].output)

    async def test_redirection_never_creates_files(self) -> None:
        """> / >> / 2> are literal argv, so no file is ever created."""
        marker = self.tmpdir / "marker.txt"
        for command in ("echo safe > marker.txt", "echo safe >> marker.txt", "echo safe 2> marker.txt"):
            with self.subTest(command=command):
                output = await self._execute(command)
                self.assertEqual(output["exit_code"], 0)
                self.assertFalse(marker.exists(), f"redirection created file: {command}")

    async def test_command_chaining_never_runs_second_program(self) -> None:
        """A second program after ; / && / | must not execute without a shell."""
        marker = self.tmpdir / "PWNED.txt"
        second = (
            "python -c \"import pathlib; pathlib.Path('PWNED.txt').write_text('pwned')\""
        )
        for separator in (" ; ", " && ", " | "):
            with self.subTest(separator=separator.strip()):
                output = await self._execute(f"echo safe{separator}{second}")
                self.assertEqual(output["exit_code"], 0)
                self.assertFalse(marker.exists(), f"second program ran via: {separator}")

    async def test_legitimate_commands_with_metacharacters_still_run(self) -> None:
        """Text that merely contains shell metacharacters is safe and must not be rejected."""
        cases = [
            ("echo \"price is $5 and {fine}\"", "price is $5 and {fine}"),
            ("echo \"a | b & c > d\"", "a | b & c > d"),
            ("python -c \"print('semi;colon ok')\"", "semi;colon ok"),
            ("python -c \"print('brackets () [] {} ok')\"", "brackets () [] {} ok"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                output = await self._execute(command)
                self.assertEqual(output["exit_code"], 0, output.get("stderr"))
                self.assertIn(expected, output["stdout"])


# ---------------------------------------------------------------------------
# No-shell source contract: catches a refactor reintroducing shell=True
# ---------------------------------------------------------------------------

class DesktopNoShellInvariantTests(unittest.TestCase):
    """The behavioral tests above catch hostile payloads; this one catches the plumbing.

    If a future refactor reintroduces a shell — shell=True on any subprocess launch,
    create_subprocess_shell, or os.system/os.popen — the module no longer runs commands
    as literal argv tokens and this source-level contract fails at the offending line.
    """

    def setUp(self) -> None:
        from novacontrol.desktop import controller as desktop_controller

        self.source = Path(desktop_controller.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_shell_launch_is_reintroduced(self) -> None:
        from conftest import shell_launch_offenders

        offenders = shell_launch_offenders(self.source)
        self.assertEqual(offenders, [], "Shell launch reintroduced in desktop/controller.py: " + "; ".join(offenders))

    def test_execute_script_keeps_shlex_and_exec_form(self) -> None:
        """The command boundary still splits into literal argv and launches exec-form.

        AST-based so comments (like the invariant note that mentions shlex.split)
        cannot mask removal of the actual call.
        """
        fn = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_execute_script"
            ),
            None,
        )
        self.assertIsNotNone(fn, "_execute_script must exist")
        calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
        has_shlex_split = any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "split"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "shlex"
            for call in calls
        )
        has_exec_form = any(
            (
                isinstance(call.func, ast.Attribute) and call.func.attr == "create_subprocess_exec"
            )
            or (isinstance(call.func, ast.Name) and call.func.id == "create_subprocess_exec")
            for call in calls
        )
        self.assertTrue(has_shlex_split, "_execute_script no longer calls shlex.split")
        self.assertTrue(has_exec_form, "_execute_script no longer launches via create_subprocess_exec")


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

class DesktopFileOpsTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_organize_files(self) -> None:
        (self.tmpdir / "notes.txt").write_text("notes")
        (self.tmpdir / "image.png").write_text("image")
        action = DesktopAction(
            type=DesktopActionType.ORGANIZE_FILES,
            target=str(self.tmpdir),
            description="Move text files.",
            parameters={"extension": ".txt", "destination": "texts"},
        )
        output = await LocalDesktopRunner().run(action)
        self.assertEqual(output["moved_count"], 1)
        self.assertTrue((self.tmpdir / "texts" / "notes.txt").exists())
        self.assertTrue((self.tmpdir / "image.png").exists())

    async def test_plan_file_organization(self) -> None:
        controller = DesktopAutomationController()
        workflow = controller.plan_file_organization("downloads", {".pdf": "documents"})
        self.assertEqual(workflow.actions[0].parameters["destination"], "documents")

    async def test_list_files(self) -> None:
        (self.tmpdir / "a.txt").write_text("hello")
        (self.tmpdir / "b.txt").write_text("world")
        (self.tmpdir / "sub").mkdir()
        (self.tmpdir / "sub" / "c.txt").write_text("nested")

        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.LIST_FILES, target=str(self.tmpdir), description="List",
        ))
        self.assertEqual(result["action"], "list_files")
        self.assertEqual(result["count"], 3)

    async def test_list_files_not_recursive(self) -> None:
        (self.tmpdir / "a.txt").write_text("hello")
        (self.tmpdir / "sub").mkdir()
        (self.tmpdir / "sub" / "b.txt").write_text("nested")

        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.LIST_FILES, target=str(self.tmpdir), description="List",
            parameters={"recursive": False},
        ))
        self.assertEqual(result["count"], 2)

    async def test_list_files_with_pattern(self) -> None:
        (self.tmpdir / "a.txt").write_text("hello")
        (self.tmpdir / "b.py").write_text("world")

        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.LIST_FILES, target=str(self.tmpdir), description="List",
            parameters={"pattern": "*.txt"},
        ))
        self.assertEqual(result["count"], 1)

    async def test_read_file(self) -> None:
        test_file = self.tmpdir / "test.txt"
        test_file.write_text("Hello, world!")

        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.READ_FILE, target=str(test_file), description="Read",
        ))
        self.assertEqual(result["action"], "read_file")
        self.assertEqual(result["content"], "Hello, world!")
        self.assertFalse(result["truncated"])

    async def test_read_file_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await LocalDesktopRunner().run(DesktopAction(
                type=DesktopActionType.READ_FILE,
                target=str(self.tmpdir / "missing.txt"),
                description="Read",
            ))

    async def test_write_file(self) -> None:
        test_file = self.tmpdir / "output.txt"
        await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.WRITE_FILE, target=str(test_file),
            description="Write", parameters={"content": "Test content"},
        ))
        self.assertEqual(test_file.read_text(), "Test content")

    async def test_write_file_creates_dirs(self) -> None:
        test_file = self.tmpdir / "deep" / "nested" / "file.txt"
        await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.WRITE_FILE, target=str(test_file),
            description="Write", parameters={"content": "nested"},
        ))
        self.assertTrue(test_file.exists())
        self.assertEqual(test_file.read_text(), "nested")


# ---------------------------------------------------------------------------
# System operations
# ---------------------------------------------------------------------------

class DesktopSystemOpsTests(unittest.IsolatedAsyncioTestCase):

    async def test_system_info(self) -> None:
        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.SYSTEM_INFO, target="system", description="Info",
        ))
        self.assertEqual(result["action"], "system_info")
        self.assertIn("platform", result)
        self.assertIn("python", result)

    async def test_list_processes(self) -> None:
        result = await LocalDesktopRunner().run(DesktopAction(
            type=DesktopActionType.LIST_PROCESSES, target="processes", description="List",
        ))
        self.assertEqual(result["action"], "list_processes")
        self.assertGreater(result["count"], 0)

    async def test_noop_runner_does_not_execute(self) -> None:
        result = await NoopDesktopRunner().run(DesktopAction(
            type=DesktopActionType.OPEN_APPLICATION, target="notepad", description="Open",
        ))
        self.assertIn("would_run", result)
        self.assertEqual(result["would_run"]["type"], "open_application")


# ---------------------------------------------------------------------------
# Module events
# ---------------------------------------------------------------------------

class DesktopModuleTests(unittest.IsolatedAsyncioTestCase):

    async def test_module_emits_planned_and_denied_events(self) -> None:
        bus = EventBus()
        module = DesktopAutomationModule(DesktopAutomationController())
        planned, denied = [], []

        async def cap_planned(e: Event): planned.append(e)
        async def cap_denied(e: Event): denied.append(e)

        await bus.subscribe("desktop.workflow_planned", cap_planned)
        await bus.subscribe("desktop.workflow_denied", cap_denied)
        await module.start(bus)
        await bus.publish(Event(
            type="desktop.workflow_requested",
            payload={"workflow_type": "open_application", "application": "Code", "execute": True},
        ))
        self.assertEqual(planned[0].payload["name"], "Open Code")
        self.assertEqual(denied[0].payload["results"][0]["status"], "denied")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DesktopModelTests(unittest.IsolatedAsyncioTestCase):

    def test_action_types(self) -> None:
        types = {
            "close_application", "take_screenshot", "list_processes",
            "list_files", "read_file", "write_file", "system_info", "keyboard_shortcut",
        }
        for t in types:
            self.assertIsNotNone(getattr(DesktopActionType, t.upper(), None))

    def test_action_to_dict(self) -> None:
        action = DesktopAction(
            type=DesktopActionType.SYSTEM_INFO, target="system",
            description="Info", parameters={"key": "value"},
        )
        d = action.to_dict()
        self.assertEqual(d["type"], "system_info")
        self.assertEqual(d["parameters"]["key"], "value")

    def test_workflow_to_dict(self) -> None:
        from novacontrol.desktop import DesktopWorkflow
        workflow = DesktopWorkflow(name="Test", actions=(
            DesktopAction(type=DesktopActionType.READ_FILE, target="/tmp/test", description="Read"),
        ))
        d = workflow.to_dict()
        self.assertEqual(d["name"], "Test")
        self.assertEqual(len(d["actions"]), 1)


# ---------------------------------------------------------------------------
# Post-execution verification (JARVIS must not report success that never happened)
# ---------------------------------------------------------------------------

class ProbingRunner(LocalDesktopRunner):
    """LocalDesktopRunner with the OS probes stubbed so tests never touch the desktop."""

    def __init__(self, window_lines=None, type_verification=None):
        super().__init__(windows_type_paste=True)
        self._probe_lines = window_lines
        self._type_verification = type_verification
        self.launched = False
        self.pasted = None

    async def _launch_open(self, target: str) -> int:
        self.launched = True
        return 1234

    async def _window_lines(self):
        return self._probe_lines

    async def _stage_and_paste_windows(self, text: str) -> None:
        self.pasted = text

    async def _verify_type_windows(self, text: str):
        return self._type_verification


class DesktopVerificationTests(unittest.IsolatedAsyncioTestCase):
    """Open and type actions are verified before success; unverifiable probes skip."""

    def test_window_stem_and_matching(self) -> None:
        from novacontrol.desktop.controller import _window_matches, _window_stem
        self.assertEqual(_window_stem("notepad"), "notepad")
        self.assertEqual(_window_stem(r"C:\Windows\notepad.exe"), "notepad")
        self.assertEqual(_window_stem("Visual Studio Code"), "visual studio code")
        self.assertTrue(_window_matches("Notepad", "Untitled - Notepad", "notepad"))
        self.assertTrue(_window_matches("explorer", "C:\\Users\\me\\Documents", "documents"))
        self.assertFalse(_window_matches("explorer", "This PC", "notepad"))

    def test_window_stems_include_default_browsers_for_http_launches(self) -> None:
        from novacontrol.desktop.controller import _window_matches, _window_stems

        # "Open the browser" launches a neutral http URL through the protocol
        # handler, so the window belongs to the user's default browser.
        stems = _window_stems("the browser", "http://localhost")
        self.assertIn("chrome", stems)
        self.assertIn("msedge", stems)
        self.assertTrue(any(_window_matches("chrome", "New Tab - Google Chrome", s) for s in stems))
        self.assertTrue(any(_window_matches("msedge", "Bing - Microsoft Edge", s) for s in stems))
        # https URLs passed directly get the same treatment.
        url_stems = _window_stems("example.com", "https://example.com")
        self.assertIn("firefox", url_stems)
        # A steam:// launch must NOT verify against a browser window.
        steam_stems = _window_stems("steam", "steam://open/main")
        self.assertNotIn("chrome", steam_stems)
        self.assertFalse(any(_window_matches("chrome", "New Tab - Google Chrome", s) for s in steam_stems))

    def test_resolve_strips_articles_and_knows_the_browser(self) -> None:
        from novacontrol.desktop.controller import LocalDesktopRunner

        resolve = LocalDesktopRunner._resolve_app_target
        # Articles are stripped before lookup: "the notepad" == "notepad".
        # (Only true articles — "my computer" is itself an alias key.)
        self.assertEqual(resolve("the notepad"), resolve("notepad"))
        self.assertEqual(resolve("my computer"), "explorer.exe")
        # The browser is the user's default: launch a neutral http URL through
        # the OS protocol handler instead of failing to find an app named that.
        self.assertTrue(resolve("the browser").startswith("http://"))
        self.assertTrue(resolve("web browser").startswith("http://"))

    async def test_open_reports_verified_when_window_appears(self) -> None:
        runner = ProbingRunner(window_lines=["Notepad|Untitled - Notepad"])
        output = await runner._open_application("notepad")
        self.assertTrue(runner.launched)
        self.assertTrue(output["verified"])
        self.assertEqual(output["window"], "Untitled - Notepad")
        self.assertIn("Window appeared", output["verification"])

    async def test_open_fails_when_no_window_appears(self) -> None:
        runner = ProbingRunner(window_lines=[])
        with self.assertRaisesRegex(Exception, "no window appeared"):
            await runner._open_application("notepad")

    async def test_open_skips_when_probe_unavailable(self) -> None:
        runner = ProbingRunner(window_lines=None)
        output = await runner._open_application("notepad")
        self.assertIsNone(output["verified"])

    async def test_type_reports_verified_when_paste_landed(self) -> None:
        runner = ProbingRunner(type_verification={
            "clipboard_match": True, "focused_window": True, "window_title": "Untitled - Notepad",
        })
        output = await runner._type_text("hello world")
        self.assertEqual(runner.pasted, "hello world")
        self.assertTrue(output["verified"])
        self.assertTrue(output["verification"]["clipboard_staged"])
        self.assertEqual(output["verification"]["focused_window"], "Untitled - Notepad")

    async def test_type_fails_when_clipboard_staging_mismatches(self) -> None:
        runner = ProbingRunner(type_verification={
            "clipboard_match": False, "focused_window": True, "window_title": "Untitled - Notepad",
        })
        with self.assertRaisesRegex(Exception, "clipboard"):
            await runner._type_text("hello")

    async def test_type_fails_when_no_window_is_focused(self) -> None:
        runner = ProbingRunner(type_verification={
            "clipboard_match": True, "focused_window": False, "window_title": "",
        })
        with self.assertRaisesRegex(Exception, "no window is focused"):
            await runner._type_text("hello")

    async def test_type_skips_when_probe_unavailable(self) -> None:
        runner = ProbingRunner(type_verification=None)
        output = await runner._type_text("hello")
        self.assertIsNone(output["verified"])

    async def test_controller_reports_failed_when_verification_fails(self) -> None:
        """A launch that never shows a window must not surface as success."""
        runner = ProbingRunner(window_lines=[])
        controller = DesktopAutomationController(approval_gateway=AllowGateway(), runner=runner)
        results = await controller.execute_workflow(controller.plan_open_application("notepad"))
        self.assertEqual(results[0].status, DesktopActionStatus.FAILED)
        self.assertIn("no window appeared", results[0].error)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# J.A.R.V.I.S desktop fixes: in-tab web search + app alias registry
# ---------------------------------------------------------------------------

class JarvisDesktopCommandTests(unittest.TestCase):
    """\"open chrome and search for cats\" must search IN the browser tab, and
    spoken app names (steam, mail, settings, ...) must resolve to launchable
    targets instead of falling into `cmd /c start <name>`."""

    def test_open_then_search_plans_a_web_search_in_that_browser(self) -> None:
        from novacontrol.desktop.controller import DesktopAutomationController, parse_desktop_command
        from novacontrol.desktop.models import DesktopActionType

        controller = DesktopAutomationController(runner=RecordingRunner())
        workflow, descriptions, _ = controller.plan_command("open chrome and search for cats")
        kinds = [action.type for action in workflow.actions]
        self.assertEqual(kinds, [DesktopActionType.OPEN_APPLICATION, DesktopActionType.WEB_SEARCH])
        search_action = workflow.actions[1]
        self.assertEqual(search_action.target, "cats")
        self.assertEqual(search_action.parameters.get("browser"), "chrome")

    def test_search_query_keeps_words_joined_by_and(self) -> None:
        from novacontrol.desktop.controller import parse_desktop_command

        steps = parse_desktop_command("open chrome and search for cats and dogs")
        self.assertEqual(steps[-1].kind, "search")
        self.assertEqual(steps[-1].text, "cats and dogs")

    def test_multi_step_chain_splits_type_and_press(self) -> None:
        from novacontrol.desktop.controller import parse_desktop_command

        steps = parse_desktop_command("open notepad and type hello world and press enter")
        self.assertEqual(
            [(step.kind, step.text) for step in steps],
            [("open", "hello world" and ""), ("type", "hello world"), ("press", "enter")],
        )
        # First step opens notepad, not types it.
        self.assertEqual(steps[0].kind, "open")
        self.assertEqual(steps[0].target, "notepad")

    def test_edge_browser_search_targets_that_browser(self) -> None:
        from novacontrol.desktop.controller import DesktopAutomationController
        from novacontrol.desktop.models import DesktopActionType

        controller = DesktopAutomationController(runner=RecordingRunner())
        workflow, _, _ = controller.plan_command("open edge and search for trains")
        self.assertEqual(workflow.actions[1].type, DesktopActionType.WEB_SEARCH)
        self.assertEqual(workflow.actions[1].parameters.get("browser"), "msedge")

    def test_standalone_search_is_a_web_search_not_execute_script(self) -> None:
        from novacontrol.desktop.controller import DesktopAutomationController
        from novacontrol.desktop.models import DesktopActionType

        controller = DesktopAutomationController(runner=RecordingRunner())
        workflow, _, _ = controller.plan_command("search for latest news on mars")
        self.assertEqual(workflow.actions[0].type, DesktopActionType.WEB_SEARCH)
        self.assertEqual(workflow.actions[0].target, "latest news on mars")

    def test_app_aliases_resolve_to_launchable_targets(self) -> None:
        from novacontrol.desktop.controller import LocalDesktopRunner

        resolve = LocalDesktopRunner._resolve_app_target
        self.assertEqual(resolve("steam"), "steam://open/main")
        self.assertEqual(resolve("mail"), "mailto:")
        self.assertEqual(resolve("settings"), "ms-settings:")
        self.assertEqual(resolve("notepad"), "notepad.exe")
        self.assertEqual(resolve("calculator"), "calc.exe")
        # Known apps resolve to a launchable target (alias, Start Menu .lnk,
        # App Paths exe, or UWP shell: target) — anything beats a raw name.
        resolved_vscode = resolve("visual studio code")
        self.assertTrue(
            resolved_vscode == "visual studio code"
            or "visual studio code" in resolved_vscode.lower(),
            f"VS Code should resolve or pass through, got {resolved_vscode!r}",
        )
        # Paths and explicit file names always pass through untouched.
        self.assertEqual(resolve("C:/Games/SomeGame.exe"), "C:/Games/SomeGame.exe")

    def test_index_lookup_matches_exact_whole_word_and_shortest(self) -> None:
        from novacontrol.desktop.controller import _index_lookup

        index = {
            "visual studio code": "VSCode.lnk",
            "google chrome": "Chrome.lnk",
            "chrome canary": "ChromeCanary.lnk",
        }
        # Whole-word: 'code' finds VS Code; a raw substring search would have
        # no way to prefer it over longer names deterministically.
        self.assertEqual(_index_lookup(index, "code"), "VSCode.lnk")
        # 'chrome' matches two entries; the shortest name wins.
        self.assertEqual(_index_lookup(index, "chrome"), "Chrome.lnk")
        self.assertIsNone(_index_lookup(index, "totally unknown app"))
        self.assertIsNone(_index_lookup(index, ""))

    def test_loose_spoken_folder_names_are_not_swallowed_as_apps(self) -> None:
        from novacontrol.desktop.controller import parse_desktop_command

        # 'open the games folder' must plan an OPEN_FOLDER (searched on disk at
        # execution), not an app launch of the literal string 'the games folder'.
        steps = parse_desktop_command("open the games folder")
        self.assertEqual(steps[0].kind, "open_folder")
        self.assertEqual(steps[0].target, "games")
        steps = parse_desktop_command("open my projects folder")
        self.assertEqual(steps[0].kind, "open_folder")
        self.assertEqual(steps[0].target, "projects")
        # In-app sections stay navigation, not folder searches.
        steps = parse_desktop_command("open steam and go to library")
        self.assertEqual(steps[0].kind, "open")

    def test_a_named_project_opens_as_a_folder_not_an_app(self) -> None:
        """"open my NovaControl project" is a folder, under another name.

        A project is named in prose, never by extension, so without its own
        pattern the app branch reads the whole phrase as a program called
        "my novacontrol project" and tries to launch it.
        """
        from novacontrol.desktop.controller import parse_desktop_command

        steps = parse_desktop_command("open my NovaControl project")
        self.assertEqual(steps[0].kind, "open_folder")
        # The parser folds case before matching, and the on-disk folder search
        # compares whole words, so the lowercased name still finds the folder.
        self.assertEqual(steps[0].target, "novacontrol")
        steps = parse_desktop_command("open the report project")
        self.assertEqual(steps[0].kind, "open_folder")
        self.assertEqual(steps[0].target, "report")
        # A bare "open my project" names nothing, so it must not become a
        # folder called "my" — there is no target to act on.
        bare = parse_desktop_command("open my project")
        self.assertFalse(
            any(step.kind == "open_folder" and step.target.strip().lower() == "my" for step in bare)
        )

    def test_resolve_folder_searches_temp_roots(self) -> None:
        """Spoken folder names resolve across the search roots by whole-word match."""
        import unittest.mock

        from novacontrol.desktop.controller import _resolve_folder

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "D-Games").mkdir()
            (base / "Projects").mkdir()
            with unittest.mock.patch(
                "novacontrol.desktop.controller._folder_search_roots", return_value=[base]
            ):
                self.assertEqual(Path(_resolve_folder("games")).name, "D-Games")
                self.assertEqual(Path(_resolve_folder("projects")).name, "Projects")
                # Unknown names resolve to the CWD-relative literal path (no
                # wrong folder opens; the file manager reports it as missing).
                self.assertEqual(
                    _resolve_folder("no such folder xyz"),
                    str(Path("no such folder xyz").resolve()),
                )

    def test_window_launch_command_routes_uwp_and_uri_targets(self) -> None:
        """Pure routing contract, platform-independent: shell:AppsFolder targets
        launch through explorer (cmd start opens them as folders); URI schemes
        and plain exes go through `cmd /c start` so the OS registries resolve
        protocol handlers and file associations."""
        from novacontrol.desktop.controller import _window_launch_command

        self.assertEqual(
            _window_launch_command("shell:AppsFolder\\App_abc!App"),
            ["explorer", "shell:AppsFolder\\App_abc!App"],
        )
        self.assertEqual(_window_launch_command("steam://open/main"), ["cmd", "/c", "start", "", "steam://open/main"])
        self.assertEqual(_window_launch_command("notepad.exe"), ["cmd", "/c", "start", "", "notepad.exe"])

    def test_launch_open_uses_explorer_for_uwp_shell_targets(self) -> None:
        """shell:AppsFolder targets go through explorer, not cmd start."""
        import subprocess as sp
        import unittest.mock

        runner = LocalDesktopRunner()
        captured = {}
        real_popen = sp.Popen  # captured BEFORE the module attribute is patched

        def fake_popen(args, **kwargs):
            captured["args"] = list(args)
            return real_popen([sys.executable, "-c", "pass"], stdout=sp.DEVNULL)

        async def run():
            with unittest.mock.patch.object(
                LocalDesktopRunner, "_resolve_app_target",
                staticmethod(lambda t: "shell:AppsFolder\\App_abc!App"),
            ), unittest.mock.patch(
                "sys.platform", "win32"
            ), unittest.mock.patch(
                "novacontrol.desktop.controller.subprocess.Popen", side_effect=fake_popen
            ):
                return await runner._launch_open("microsoft store")

        asyncio.run(run())
        self.assertEqual(captured["args"][0], "explorer")
        self.assertEqual(captured["args"][1], "shell:AppsFolder\\App_abc!App")

    def test_web_search_plan_carries_query(self) -> None:
        from novacontrol.desktop.models import DesktopAction, DesktopActionType
        from novacontrol.desktop.controller import DesktopAutomationController

        # Planner-level contract: one WEB_SEARCH action carrying the query.
        # (The runner's URL construction shells out and is exercised live.)
        workflow = DesktopAutomationController(runner=RecordingRunner()).plan_web_search("cats")
        action: DesktopAction = workflow.actions[0]
        self.assertEqual(action.type, DesktopActionType.WEB_SEARCH)
        self.assertEqual(action.target, "cats")
        # With no browser named, the parameter is omitted (default browser).
        self.assertNotIn("browser", action.parameters)


# ----------- In-app chains, vision clicks, stop, folders (JARVIS batch) --------

class DesktopChainParsingTests(unittest.TestCase):
    def test_steam_library_launch_chain(self):
        steps = parse_desktop_command("open steam and go to library and launch gta v")
        # A known game launches via steam://rungameid deep link (reliable);
        # a vision click is only the FALLBACK for unknown titles.
        self.assertEqual(
            [(s.kind, s.target, s.text) for s in steps],
            [("open", "steam", ""), ("navigate", "steam", "library"), ("game_launch", "gta v", "")],
        )

    def test_unknown_game_falls_back_to_vision_click(self):
        steps = parse_desktop_command("open steam and go to library and launch totally unknown game")
        self.assertEqual(steps[-1].kind, "click")
        self.assertIn("play totally unknown game", steps[-1].text)

    def test_stop_that_is_stop_step(self):
        for phrase in ("stop that", "stop", "stop everything"):
            steps = parse_desktop_command(phrase)
            self.assertEqual(len(steps), 1, phrase)
            self.assertEqual(steps[0].kind, "stop", phrase)

    def test_chain_ending_in_stop_carries_last_app(self):
        steps = parse_desktop_command("open notepad and type meeting notes and stop")
        self.assertEqual(steps[-1].kind, "stop")
        self.assertEqual(steps[-1].target, "notepad")

    def test_spoken_user_folder(self):
        steps = parse_desktop_command("open downloads")
        self.assertEqual(steps[0].kind, "open_folder")
        self.assertIn("Downloads", steps[0].target)

    def test_standalone_click_is_vision_click(self):
        steps = parse_desktop_command("click file")
        self.assertEqual(steps[0].kind, "click")
        self.assertEqual(steps[0].text, "file")

    def test_search_payload_with_and_stays_one_step(self):
        steps = parse_desktop_command("search for cats and dogs")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].kind, "search")
        self.assertEqual(steps[0].text, "cats and dogs")


class DesktopInAppPlanningTests(unittest.TestCase):
    def setUp(self):
        self.controller = DesktopAutomationController(runner=NoopDesktopRunner())

    def test_plan_app_navigate_uses_deep_link_parameters(self):
        workflow = self.controller.plan_app_navigate("steam", "library")
        action = workflow.actions[0]
        self.assertEqual(action.type.value, "app_navigate")
        self.assertEqual(action.parameters["app"], "steam")
        self.assertEqual(action.target, "library")

    def test_plan_vision_click_carries_label(self):
        workflow = self.controller.plan_vision_click("Library button")
        action = workflow.actions[0]
        self.assertEqual(action.type.value, "vision_click")
        self.assertEqual(action.target, "Library button")

    def test_plan_desktop_command_emits_navigate_and_game_launch_actions(self):
        workflow, descriptions, _first = self.controller.plan_command(
            "open steam and go to library and launch gta v"
        )
        kinds = [action.type.value for action in workflow.actions]
        self.assertIn("app_navigate", kinds)
        self.assertIn("game_launch", kinds)
        self.assertTrue(any("Launch" in description for description in descriptions))

    def test_plan_stop_emits_stop_app(self):
        workflow, _descriptions, _first = self.controller.plan_command("stop that")
        self.assertEqual(workflow.actions[0].type.value, "stop_app")
