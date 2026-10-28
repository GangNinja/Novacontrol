"""NF-12 — the Game Library tab's words and wiring.

The pure translations are executed in Node (no DOM needed): the four launch
facts must never collapse into one another, and the status vocabulary must not
guess at a status the backend invented. The rest is a static contract: the tab
exists, it talks to the library routes, approval carries the server's token, and
opening the tab reads the catalog rather than launching anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC = Path("src/novacontrol/web/static")
PANEL_JS = STATIC / "js" / "novaforge.js"
INDEX = STATIC / "index.html"

DRIVER = """
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
function stages(value) { return L.gameStageText(value); }
console.log(JSON.stringify({
  noLaunch: stages({}),
  startedNoWindow: stages({
    launch_requested: true, process_started: true,
    window_detected: false, launch_verified: false,
  }),
  verified: stages({
    launch_requested: true, process_started: true,
    window_detected: true, launch_verified: true,
  }),
  labels: {
    launchable: L.gameStatusLabel('INSTALLED_AND_LAUNCHABLE'),
    running: L.gameStatusLabel('ALREADY_RUNNING'),
    runtime: L.gameStatusLabel('REQUIRES_LAUNCHER_OR_RUNTIME'),
    ambiguous: L.gameStatusLabel('AMBIGUOUS_MATCH'),
    failed: L.gameStatusLabel('INSTALLED_BUT_LAUNCH_FAILED'),
    unknown: L.gameStatusLabel('SOME_NEW_STATE'),
  },
  tones: {
    launchable: L.gameStatusTone('INSTALLED_AND_LAUNCHABLE'),
    runtime: L.gameStatusTone('REQUIRES_LAUNCHER_OR_RUNTIME'),
    failed: L.gameStatusTone('INSTALLED_BUT_LAUNCH_FAILED'),
    unknown: L.gameStatusTone('SOME_NEW_STATE'),
  },
  gate: {
    refused: L.memoryText({ ok: false, available: true, reason: 'only 0.6 GB free', snapshot: { free_gb: 0.6 } }),
    unreadable: L.memoryText({ ok: true, available: false, reason: 'Memory could not be read', snapshot: {} }),
    passed: L.memoryText({ ok: true, available: true, snapshot: { free_gb: 8, used_percent: 40 } }),
  },
}));
"""


class _LibraryLogicTests(unittest.TestCase):
    """Executes the REAL exported translations in Node."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not available")
        cls._data = None
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(DRIVER)
            driver = Path(handle.name)
        try:
            completed = subprocess.run(
                ["node", str(driver), str(PANEL_JS)],
                capture_output=True,
                timeout=60,
                check=False,
            )
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            if completed.returncode != 0:
                raise AssertionError(f"node failed: {stderr[:800]}")
            cls._data = json.loads(stdout.strip().splitlines()[-1])
        finally:
            driver.unlink(missing_ok=True)

    @property
    def data(self) -> dict:
        if self._data is None:  # pragma: no cover - harness failure
            self.fail("node produced no JSON")
        return self._data

    def test_no_launch_stage_says_no_launch(self) -> None:
        text = self.data["noLaunch"]
        self.assertEqual(text["requested"], "No launch requested.")
        self.assertEqual(text["process"], "No process started.")
        self.assertEqual(text["window"], "No window was detected.")
        self.assertEqual(text["verified"], "Launch NOT verified.")

    def test_a_started_process_without_a_window_is_not_verified(self) -> None:
        text = self.data["startedNoWindow"]
        self.assertEqual(text["process"], "A process started.")
        self.assertEqual(text["window"], "No window was detected.")
        self.assertEqual(text["verified"], "Launch NOT verified.")

    def test_a_verified_launch_says_exactly_that(self) -> None:
        text = self.data["verified"]
        self.assertEqual(text["window"], "A window appeared.")
        self.assertEqual(text["verified"], "Launch verified against the process and window.")

    def test_status_labels_are_human_and_never_invented(self) -> None:
        labels = self.data["labels"]
        self.assertEqual(labels["launchable"], "installed · launchable")
        self.assertEqual(labels["running"], "already running")
        self.assertEqual(labels["runtime"], "needs a runtime")
        self.assertEqual(labels["ambiguous"], "ambiguous")
        self.assertEqual(labels["failed"], "launch failed")
        self.assertEqual(labels["unknown"], "some new state")

    def test_status_tones_do_not_paint_a_failure_as_good(self) -> None:
        tones = self.data["tones"]
        self.assertEqual(tones["launchable"], "good")
        self.assertEqual(tones["failed"], "bad")
        self.assertEqual(tones["runtime"], "warn")
        self.assertEqual(tones["unknown"], "muted")

    def test_the_resource_gate_is_reported_in_its_own_words(self) -> None:
        gate = self.data["gate"]
        self.assertIn("REFUSED", gate["refused"])
        self.assertIn("0.60 GB", gate["refused"])
        self.assertIn("could not be read", gate["unreadable"])
        self.assertIn("skipped", gate["unreadable"])
        self.assertIn("passed", gate["passed"])
        self.assertIn("8.00 GB", gate["passed"])


class LibraryPanelContractTests(unittest.TestCase):
    """Static contract for the tab the user actually gets."""

    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")
        self.js = PANEL_JS.read_text(encoding="utf-8")

    def test_the_library_tab_and_view_exist(self) -> None:
        for element_id in (
            "forgeTabLibrary",
            "forgeLibraryView",
            "forgeLibraryNotice",
            "forgeLibrarySearch",
            "forgeLibraryRefreshButton",
            "forgeLibraryCloseButton",
            "forgeLibraryList",
            "forgeLibraryCounts",
            "forgeLibraryBadge",
            "forgeLibraryLimits",
            "forgeLibraryResult",
            "forgeLibraryState",
            "forgeLibraryObservation",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
        self.assertIn('data-forge-tab="forgeLibraryView"', self.html)
        self.assertIn('aria-controls="forgeLibraryView"', self.html)

    def test_the_four_launch_facts_have_their_own_rows(self) -> None:
        for element_id in (
            "forgeLaunchStageRequested",
            "forgeLaunchStageProcess",
            "forgeLaunchStageWindow",
            "forgeLaunchStageVerified",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")

    def test_the_tab_talks_to_the_library_routes_only(self) -> None:
        for path in (
            '"/novaforge/library?q="',
            '"/novaforge/library/launch"',
            '"/novaforge/library/close"',
            '"/novaforge/library/attach"',
        ):
            self.assertIn(path, self.js, f"the panel never calls {path}")

    def test_opening_the_tab_reads_the_catalog_and_launches_nothing(self) -> None:
        self.assertIn("function openLibrary() {\n    loadLibrary();\n  }", self.js)
        self.assertIn("if (viewId === \"forgeLibraryView\") openLibrary();", self.js)

    def test_approval_sends_the_server_token_back(self) -> None:
        self.assertIn("command: plan.command,", self.js)
        self.assertIn("approval_token: plan.approval.token,", self.js)

    def test_the_panel_reports_observation_separately_from_launch(self) -> None:
        self.assertIn("renderObservation(plan.observation)", self.js)
        self.assertIn("renderObservation(result.observation)", self.js)
        self.assertIn("never assumed", self.html)

    def test_the_refresh_search_and_close_controls_are_wired(self) -> None:
        self.assertIn('byId("forgeLibraryRefreshButton").addEventListener("click", loadLibrary)', self.js)
        self.assertIn('closeButton.disabled = !payload.owned;', self.js)
        self.assertIn("data-launch-game", self.js)
        self.assertIn("data-attach-game", self.js)
        self.assertIn("data-close-game", self.js)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
