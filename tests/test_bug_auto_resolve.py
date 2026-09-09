"""Unit tests: bug auto-resolve on verified guided clicks + panel wiring.

The Playwright spec (test_guided_click_e2e.py) proves the browser flow; these
pin the mechanics underneath it so regressions surface without a browser:

  - BugLog.resolve_matching resolves only the matching flow+label, embeds the
    evidence + timestamp, leaves other flows/labels/already-fixed rows alone,
    and matches legacy rows that carry the label only in their what-text.
  - guided_click wires it: a verified click resolves the open verification
    bug and reports resolved_bugs; an unverified one records honestly.
  - The click point the OCR re-check reads comes from the runner (it used to
    be read from the controller, silently disabling the check).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.core.buglog import BugLog
from novacontrol.desktop.controller import DesktopAutomationController
from novacontrol.desktop.vision import VisionController


class ResolveMatchingTests(unittest.TestCase):
    """BugLog.resolve_matching: evidence-backed auto-resolution."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.log = BugLog(Path(self.tmp.name) / "bugs.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record(self, what: str, label: str, status: str = "open") -> str:
        entry = self.log.record(
            what,
            where="vision: guided_click",
            details={"label": label},
        )
        if status != "open":
            self.log.mark_fixed(entry.id)
        return entry.id

    def test_resolves_matching_flow_and_label_with_evidence(self) -> None:
        bug_id = self._record("Vision click 'Library' could not be verified automatically.", "Library")

        resolved = self.log.resolve_matching(
            where="vision: guided_click",
            label="Library",
            evidence={"changed_pct": 2.5, "after_screenshot": "vision_after.png"},
        )

        self.assertEqual(resolved, 1)
        bug = next(b for b in self.log.all() if b.id == bug_id)
        self.assertEqual(bug.status, "fixed")
        proof = bug.details["auto_resolved"]
        self.assertEqual(proof["changed_pct"], 2.5)
        self.assertIn("resolved_at", proof)

    def test_leaves_other_flows_and_labels_alone(self) -> None:
        other_flow = self.log.record("Phone tap failed", where="phone: tap", details={"label": "Library"})
        other_label = self._record("Vision click 'File' failed.", "File")
        already_fixed = self._record("Vision click 'Library' failed.", "Library", status="fixed")

        resolved = self.log.resolve_matching(
            where="vision: guided_click", label="Library", evidence={"changed_pct": 1.0}
        )

        self.assertEqual(resolved, 0)
        statuses = {b.id: b.status for b in self.log.all()}
        self.assertEqual(statuses[other_flow.id], "open")
        self.assertEqual(statuses[other_label], "open")
        self.assertEqual(statuses[already_fixed], "fixed")

    def test_matches_legacy_rows_with_label_in_what_text(self) -> None:
        # Older records carried no details.label — only the what-text.
        entry = self.log.record(
            "Vision click 'Library' failed: could not locate",
            where="vision: guided_click",
            details={},
        )

        resolved = self.log.resolve_matching(
            where="vision: guided_click", label="Library", evidence={"changed_pct": 1.0}
        )

        self.assertEqual(resolved, 1)
        bug = next(b for b in self.log.all() if b.id == entry.id)
        self.assertEqual(bug.status, "fixed")

    def test_persists_resolution_to_disk(self) -> None:
        self._record("Vision click 'Library' failed.", "Library")

        self.log.resolve_matching(
            where="vision: guided_click", label="Library", evidence={"changed_pct": 1.0}
        )

        on_disk = json.loads((Path(self.tmp.name) / "bugs.json").read_text(encoding="utf-8"))
        self.assertTrue(all(b["status"] == "fixed" for b in on_disk["bugs"]))


class GuidedClickAutoResolveTests(unittest.IsolatedAsyncioTestCase):
    """guided_click wiring: verified clicks resolve; unverifiable ones record."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.log = BugLog(Path(self.tmp.name) / "bugs.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _vision(self, runner: object) -> VisionController:
        return VisionController(DesktopAutomationController(runner=runner), bug_log=self.log)

    async def test_verified_click_resolves_open_bug_and_reports_count(self) -> None:
        self.log.record(
            "Vision click 'Library' could not be verified automatically.",
            where="vision: guided_click",
            details={"label": "Library"},
        )

        class ScriptedRunner:
            """Executes the click and produces a real before/after diff."""

            def __init__(self) -> None:
                self.captured: object | None = None

            async def run(self, action: object) -> dict:
                from PIL import Image

                base = Image.new("RGB", (40, 30), (250, 250, 250))
                base.save("vision_locate.png")
                changed = base.copy()
                for x in range(10, 30):
                    for y in range(10, 20):
                        changed.putpixel((x, y), (10, 10, 10))
                changed.save("vision_after.png")
                return {
                    "adapter": "local-desktop",
                    "action": "vision_click",
                    "label": action.target,
                    "coordinates": [10, 10],
                    "located_by": "ocr",
                    "before_path": "vision_locate.png",
                    "after_path": "vision_after.png",
                }

        runner = ScriptedRunner()
        output = await self._vision(runner).guided_click("Library")

        self.assertEqual(output["status"], "completed")
        self.assertIs(output["verification"]["changed"], True)
        self.assertEqual(output["resolved_bugs"], 1)
        bug = next(b for b in self.log.all() if "could not be verified" in b.what)
        self.assertEqual(bug.status, "fixed")
        self.assertEqual(bug.details["auto_resolved"]["changed_pct"], output["verification"]["changed_pct"])

        Path("vision_locate.png").unlink(missing_ok=True)
        Path("vision_after.png").unlink(missing_ok=True)

    async def test_unverified_click_records_bug_and_resolves_nothing(self) -> None:
        self.log.record(
            "Vision click 'Library' could not be verified automatically.",
            where="vision: guided_click",
            details={"label": "Library"},
        )

        class FlatRunner:
            async def run(self, action: object) -> dict:
                from PIL import Image

                base = Image.new("RGB", (40, 30), (250, 250, 250))
                base.save("vision_locate.png")
                base.save("vision_after.png")
                return {
                    "adapter": "local-desktop",
                    "action": "vision_click",
                    "label": action.target,
                    "coordinates": [10, 10],
                    "located_by": "ocr",
                    "before_path": "vision_locate.png",
                    "after_path": "vision_after.png",
                }

        output = await self._vision(FlatRunner()).guided_click("Library")

        self.assertNotIn("resolved_bugs", output)
        statuses = [b.status for b in self.log.all()]
        self.assertEqual(statuses.count("open"), 2, "seeded bug stays open + new no-change bug")
        self.assertEqual(statuses.count("fixed"), 0)

        Path("vision_locate.png").unlink(missing_ok=True)
        Path("vision_after.png").unlink(missing_ok=True)

    async def test_verification_reads_click_point_from_the_runner(self) -> None:
        # Regression: _verify_after_click read _last_click_point from the
        # controller — the runner writes it, so the OCR re-check never fired.
        import novacontrol.desktop.vision as vision_module

        source = Path(vision_module.__file__).read_text(encoding="utf-8")
        self.assertIn('getattr(getattr(self.desktop, "runner", None)', source)


class FrameDiffEngineTests(unittest.TestCase):
    """OpenCV fast path vs Pillow fallback: agreement + region localization."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.before = Path(self.tmp.name) / "before.png"
        self.after = Path(self.tmp.name) / "after.png"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _frames(self, after_differs: bool) -> None:
        from PIL import Image

        base = Image.new("RGB", (200, 120), (250, 250, 250))
        base.save(self.before)
        if after_differs:
            changed = base.copy()
            for x in range(120, 180):
                for y in range(40, 80):
                    changed.putpixel((x, y), (10, 10, 10))
            changed.save(self.after)
        else:
            base.save(self.after)

    def test_both_engines_agree_on_changed_pct(self) -> None:
        from novacontrol.desktop.vision import _cv2_frame_diff, _pillow_frame_diff

        self._frames(after_differs=True)
        cv2_result = _cv2_frame_diff(self.before, self.after)
        pillow_result = _pillow_frame_diff(self.before, self.after)
        self.assertEqual(cv2_result["engine"], "opencv")
        self.assertEqual(pillow_result["engine"], "pillow")
        self.assertAlmostEqual(cv2_result["changed_pct"], pillow_result["changed_pct"])

    def test_identical_frames_report_zero_and_no_regions(self) -> None:
        from novacontrol.desktop.vision import _cv2_frame_diff

        self._frames(after_differs=False)
        result = _cv2_frame_diff(self.before, self.after)
        self.assertEqual(result["changed_pct"], 0.0)
        self.assertEqual(result["regions"], [])

    def test_cv2_localizes_the_change_region(self) -> None:
        from novacontrol.desktop.vision import _cv2_frame_diff

        self._frames(after_differs=True)
        result = _cv2_frame_diff(self.before, self.after)
        regions = result["regions"]
        self.assertTrue(regions, "a changed frame must produce change regions")
        top = regions[0]
        # The synthetic change block is (120,40)-(180,80); the detected region
        # must cover it (allowing the component to be slightly larger).
        self.assertLessEqual(top["x"], 120)
        self.assertLessEqual(top["y"], 40)
        self.assertGreaterEqual(top["x"] + top["w"], 180)
        self.assertGreaterEqual(top["y"] + top["h"], 80)

    def test_verify_reports_whether_the_click_site_reacted(self) -> None:
        from novacontrol.desktop.vision import VisionController

        vision = VisionController(
            DesktopAutomationController(), bug_log=BugLog(Path(self.tmp.name) / "bugs.json")
        )
        self._frames(after_differs=True)
        # No click point recorded -> change_near_click stays None (no claim).
        result = await_noop(vision._verify_after_click("File", str(self.before), str(self.after)))
        self.assertIsNone(result["change_near_click"])
        self.assertEqual(result["diff_engine"], "opencv" if _cv2_available() else "pillow")

        # A click point inside the change region -> the site itself reacted.
        vision.desktop.runner._last_click_point = (150, 60)  # type: ignore[union-attr]
        result = await_noop(vision._verify_after_click("File", str(self.before), str(self.after)))
        self.assertIs(result["change_near_click"], True)

        # A click point far from every change region -> honest False.
        vision.desktop.runner._last_click_point = (10, 10)  # type: ignore[union-attr]
        result = await_noop(vision._verify_after_click("File", str(self.before), str(self.after)))
        self.assertIs(result["change_near_click"], False)


def _cv2_available() -> bool:
    try:
        import cv2  # noqa: F401

        return True
    except ImportError:
        return False


def await_noop(coro: object) -> object:
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
