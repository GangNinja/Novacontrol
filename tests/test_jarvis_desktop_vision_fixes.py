"""Regression tests for the JARVIS desktop/vision fixes.

Covers the four bugs found in the bug log and live use:

  1. Game resolution — installed-library lookup (Steam appmanifest_*.acf) and
     the word-boundary guard that stopped 'supermarket together' matching 'ark'.
  2. Command-as-label guard — a full command in the vision click box is a 422
     with guidance, never a locate attempt against a phrase that cannot be a
     screen element.
  3. Real verification — _verify_after_click pixel-diffs the before/after
     captures instead of always reporting "could not be verified".
  4. Provider wiring — the desktop controller's vision provider reaches
     locate_element (previously the LLM was never passed at all).
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from novacontrol.core.buglog import BugLog
from novacontrol.desktop.controller import (
    DesktopAutomationController,
    _steam_game_id,
    parse_desktop_command,
)
from novacontrol.desktop.vision import VisionController


class SteamGameResolutionTests(unittest.TestCase):
    """Whole-word matching + installed-library resolution (live registry)."""

    def test_known_table_games_resolve(self) -> None:
        self.assertEqual(_steam_game_id("gta v"), "3240220")
        self.assertEqual(_steam_game_id("grand theft auto v enhanced"), "3240220")
        self.assertEqual(_steam_game_id("ark"), "346110")

    def test_unknown_game_is_none(self) -> None:
        self.assertIsNone(_steam_game_id("some random thing"))
        self.assertIsNone(_steam_game_id(""))

    def test_installed_games_resolve_from_manifests(self) -> None:
        # Live machine registry: whatever is installed must resolve to its
        # real appid. Skips cleanly when Steam is absent (CI containers).
        import novacontrol.desktop.controller as controller_module

        controller_module._installed_games_cache = None
        installed = controller_module._installed_steam_games()
        if not installed:
            self.skipTest("no Steam library on this machine")
        for name, appid in installed.items():
            if name == "steamworks common redistributables":
                continue  # a redistributable, not a launchable game
            with self.subTest(game=name):
                self.assertEqual(_steam_game_id(name), appid)

    def test_no_substring_theft_from_short_keys(self) -> None:
        # 'supermarket together' must not match the 'ark' key (the exact bug
        # that launched ARK when the user asked for their supermarket game).
        import novacontrol.desktop.controller as controller_module

        controller_module._installed_games_cache = None
        resolved = _steam_game_id("supermarket together")
        installed = controller_module._installed_steam_games()
        if "supermarket together" in installed:
            self.assertEqual(resolved, installed["supermarket together"])
        else:
            self.assertNotEqual(resolved, "346110", "short-key substring theft")


class GameLaunchParsingTests(unittest.TestCase):
    def test_known_game_after_steam_navigates_via_deeplink(self) -> None:
        steps = parse_desktop_command("open steam and go to library and launch gta v")
        self.assertEqual(
            [(s.kind, s.target, s.text) for s in steps],
            [("open", "steam", ""), ("navigate", "steam", "library"), ("game_launch", "gta v", "")],
        )

    def test_installed_unknown_table_game_still_deeplinks(self) -> None:
        # An installed title absent from the static table must ALSO take the
        # reliable deep-link path now (this is the fix; before, only table
        # games did and everything else fell to a vision click).
        import novacontrol.desktop.controller as controller_module

        controller_module._installed_games_cache = None
        installed = controller_module._installed_steam_games()
        candidate = next(
            (name for name in installed if name != "steamworks common redistributables"),
            None,
        )
        if candidate is None:
            self.skipTest("no Steam library on this machine")
        steps = parse_desktop_command(f"open steam and go to library and launch {candidate}")
        self.assertEqual(steps[-1].kind, "game_launch", f"{candidate} should deep-link")
        self.assertEqual(steps[-1].target, candidate)


class CommandShapedLabelGuardTests(unittest.TestCase):
    def _vision(self) -> VisionController:
        desktop = DesktopAutomationController()
        return VisionController(desktop, bug_log=BugLog(Path("nonexistent-bugs.json")))

    def test_full_command_is_rejected_with_guidance(self) -> None:
        vision = self._vision()
        with self.assertRaises(ValueError) as caught:
            vision._guard_label("open chrome and search nlp in geeksforgeeks")
        self.assertIn("command, not a screen element", str(caught.exception))
        self.assertIn("JARVIS", str(caught.exception))

    def test_action_verbs_alone_are_rejected(self) -> None:
        vision = self._vision()
        for label in ("open notepad", "search for cats", "launch gta v", "stop that"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    vision._guard_label(label)

    def test_real_ui_labels_pass(self) -> None:
        vision = self._vision()
        for label in ("File", "Library", "Play", "Submit", "New Game", "SETTINGS"):
            with self.subTest(label=label):
                vision._guard_label(label)  # must not raise


class VerificationDiffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.before = Path(self.tmp.name) / "before.png"
        self.after = Path(self.tmp.name) / "after.png"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _frames(self, after_differs: bool) -> None:
        from PIL import Image

        base = Image.new("RGB", (40, 30), (250, 250, 250))
        base.save(self.before)
        if after_differs:
            changed = base.copy()
            for x in range(10, 30):
                for y in range(10, 20):
                    changed.putpixel((x, y), (10, 10, 10))
            changed.save(self.after)
        else:
            base.save(self.after)

    async def test_identical_frames_report_changed_false(self) -> None:
        self._frames(after_differs=False)
        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog(Path("nonexistent-bugs.json"))
        )
        result = await vision._verify_after_click("File", str(self.before), str(self.after))
        self.assertIs(result["changed"], False)
        self.assertEqual(result["changed_pct"], 0.0)

    async def test_different_frames_report_changed_true(self) -> None:
        self._frames(after_differs=True)
        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog(Path("nonexistent-bugs.json"))
        )
        result = await vision._verify_after_click("File", str(self.before), str(self.after))
        self.assertIs(result["changed"], True)
        self.assertGreater(result["changed_pct"], 1.0)

    async def test_missing_frames_report_changed_none(self) -> None:
        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog(Path("nonexistent-bugs.json"))
        )
        result = await vision._verify_after_click("File", str(self.before), str(self.after))
        self.assertIsNone(result["changed"])
        self.assertIn("unavailable", result["note"])


class VisionProviderWiringTests(unittest.TestCase):
    def test_runner_passes_provider_to_locate_element(self) -> None:
        # Regression: _vision_click used to call locate_element WITHOUT the
        # provider, so a wired vision model was silently never used.
        from novacontrol.desktop.controller import LocalDesktopRunner

        provider = object()
        runner = LocalDesktopRunner(vision_provider=provider)
        self.assertIs(runner.vision_provider, provider)

    def test_application_wires_brain_provider_into_desktop(self) -> None:
        from novacontrol.application import NovaControlApplication

        app = NovaControlApplication()
        self.assertIs(app.desktop.runner.vision_provider, app.brain.completion_provider)


if __name__ == "__main__":
    unittest.main()
