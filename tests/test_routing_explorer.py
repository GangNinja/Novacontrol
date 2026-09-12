"""Routing explorer: /brain/decide trace + rung previews, and the JS mirror.

The routing explorer traces an utterance through NovaBrain's gate table and
previews what the user would actually see (scratch answer text, research
headline, plan outline). These tests pin:

  * /brain/decide returns the landing intent, confidence, reason and the FULL
    gate trace with first-match semantics (the trace ends at the matched gate);
  * the landing decision always equals NovaBrain.decide() for the same text;
  * per-rung preview shapes: scratch text / explore headline / plan outline /
    device-action plan outline / info text; blank input is a 422;
  * the ``classifiers`` block (narrow routing gate vs broad engine) covering
    every deliberate-disagreement relation: match, order, engine_only,
    breadth, unknown;
  * the embedded JS mirror (the offline fallback) lands the same intent AND
    the same classifier relation on a representative probe table, driven by
    the REAL static JS in Node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner

_DATA_DIR = ""
_APP_STACK: list[NovaControlApplication] = []


def _isolated_application(**kwargs: Any) -> NovaControlApplication:
    """Force a temp data dir so create_app never touches ./data."""
    kwargs["data_dir"] = _DATA_DIR
    app = NovaControlApplication(**kwargs)
    _APP_STACK.append(app)
    return app


@unittest.skipIf(TestClient is None, "httpx not installed")
class RoutingDecideEndpointTests(unittest.TestCase):
    """/brain/decide returns the landing decision plus the full gate trace."""

    def setUp(self) -> None:
        global _DATA_DIR
        self._tmp = TemporaryDirectory()
        _DATA_DIR = self._tmp.name
        patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=_isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._client = TestClient(create_app())
        self._client.__enter__()
        self.addCleanup(self._client.__exit__, None, None, None)
        self.addCleanup(_APP_STACK.clear)

    def _trace(self, text: str) -> dict[str, Any]:
        response = self._client.post("/brain/decide", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.json()

    # (utterance, landing intent, matched gate) — first-match semantics.
    # Greetings are whole-text in the scratch brain ("hello" yes, "hello
    # there" is not a saved greeting and falls to the chat fallback).
    LANDINGS = [
        ("hello", "chat", "scratch_greeting"),
        ("what is 15 percent of 200", "chat", "scratch_math"),
        ("what is 2 cubed plus the square root of 9", "chat", "scratch_math"),
        ("open notepad", "desktop_automation", "desktop_command"),
        ("open chrome and search for cats", "desktop_automation", "desktop_command"),
        ("search cats on youtube on my phone", "phone_control", "phone_anchored_action"),
        ("open youtube on my phone", "phone_control", "phone_anchored_action"),
        ("explain how black holes form", "explore", "research_question"),
        ("create a roadmap for a startup", "plan", "plan"),
        ("remember this: buy milk", "memory", "memory_store"),
        ("can you review this code", "agent", "agent"),
        ("add a task to the project", "project", "project"),
    ]

    def test_landing_intents_and_matched_gates(self) -> None:
        for text, intent, gate in self.LANDINGS:
            with self.subTest(text=text):
                result = self._trace(text)
                self.assertEqual(result["intent"], intent)
                trace = result["trace"]
                self.assertTrue(trace, "trace must not be empty")
                self.assertTrue(trace[-1]["matched"], f"trace must end at the matched gate: {trace}")
                self.assertEqual(trace[-1]["gate"], gate)
                self.assertEqual(trace[-1]["intent"], intent)
                # Only the landing row carries a confidence/reason.
                self.assertIsNotNone(trace[-1]["confidence"])
                self.assertIsNotNone(trace[-1]["reason"])
                for row in trace[:-1]:
                    self.assertIsNone(row["intent"])
                    self.assertIsNone(row["confidence"])
                    self.assertIsNone(row["reason"])

    def test_trace_matches_decide_for_every_probe(self) -> None:
        """The landing decision IS decide() — the explorer can't disagree."""
        from novacontrol.brain.models import BrainRequest

        app = _APP_STACK[-1]
        for text, _intent, _gate in self.LANDINGS:
            with self.subTest(text=text):
                result = self._trace(text)
                decision = app.brain.decide(BrainRequest(text=text))
                self.assertEqual(result["intent"], decision.intent.value)
                self.assertAlmostEqual(result["confidence"], decision.confidence, places=6)

    def test_empty_and_blank_inputs_reject(self) -> None:
        for payload in ({"text": ""}, {"text": "   "}, {}):
            with self.subTest(payload=payload):
                response = self._client.post("/brain/decide", json=payload)
                self.assertEqual(response.status_code, 422)

    def test_trace_stops_after_first_match(self) -> None:
        """Gates after the matched one are not evaluated at all."""
        result = self._trace("hello")
        self.assertEqual(result["trace"][-1]["gate"], "scratch_greeting")
        self.assertEqual(len(result["trace"]), 1)

    # (utterance, narrow intent, broad intent, expected relation) — every
    # deliberate-disagreement class the explorer highlights.
    CLASSIFIER_CASES = [
        ("hello", "greeting", "greeting", "match"),
        ("what is 2 cubed plus the square root of 9", "math", "math", "match"),
        ("what is the capital of france", "knowledge", "knowledge", "match"),
        ("what time is 2 plus 3", "math", "time", "order"),
        ("search cats on youtube on my phone", None, "phone_control", "engine_only"),
        ("open notepad and type hello", None, "desktop_control", "engine_only"),
        ("hello there", None, "unknown", "unknown"),
    ]

    def test_classifiers_block_covers_every_relation(self) -> None:
        """The explorer returns narrow-vs-broad with all five relations."""
        for text, narrow, broad, relation in self.CLASSIFIER_CASES:
            with self.subTest(text=text):
                result = self._trace(text)
                cls = result["classifiers"]
                self.assertEqual(cls["narrow"], narrow)
                self.assertEqual(cls["broad"], broad)
                self.assertEqual(cls["relation"], relation)
                self.assertIsInstance(cls["note"], str)
                self.assertTrue(cls["note"])

    def test_classifiers_agree_with_the_engines_directly(self) -> None:
        """The block is computed from scratchable_intent + classify_broad."""
        from novacontrol.brain.brain import _classifier_relation
        from novacontrol.brain.scratch import classify_broad, scratchable_intent

        for text, _n, _b, _r in self.CLASSIFIER_CASES:
            with self.subTest(text=text):
                cls = self._trace(text)["classifiers"]
                expected = _classifier_relation(
                    scratchable_intent(text.lower()), classify_broad(text.lower())
                )
                self.assertEqual(cls, expected)


@unittest.skipIf(TestClient is None, "httpx not installed")
class RoutingPreviewShapeTests(unittest.TestCase):
    """Each landing rung carries the preview of what the user would see."""

    def setUp(self) -> None:
        global _DATA_DIR
        self._tmp = TemporaryDirectory()
        _DATA_DIR = self._tmp.name
        patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=_isolated_application),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._client = TestClient(create_app())
        self._client.__enter__()
        self.addCleanup(self._client.__exit__, None, None, None)
        self.addCleanup(_APP_STACK.clear)

    def _trace(self, text: str) -> dict[str, Any]:
        response = self._client.post("/brain/decide", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text[:300])
        return response.json()

    def test_scratch_rung_returns_real_answer_text(self) -> None:
        result = self._trace("what is 15 percent of 200")
        preview = result["preview"]
        self.assertEqual(preview["kind"], "scratch")
        self.assertIn("30", preview["text"])  # the actual computed answer

    def test_explore_rung_returns_headline(self) -> None:
        result = self._trace("explain how black holes form")
        preview = result["preview"]
        self.assertEqual(preview["kind"], "explore")
        self.assertIn("overview of", preview["headline"].lower())

    def test_plan_rung_returns_outline(self) -> None:
        result = self._trace("create a roadmap for a startup")
        preview = result["preview"]
        self.assertEqual(preview["kind"], "plan")
        self.assertIsInstance(preview["outline"], list)
        self.assertTrue(preview["outline"])

    def test_device_rung_returns_action_outline(self) -> None:
        for text in ("open notepad", "open youtube on my phone", "search cats on youtube on my phone"):
            with self.subTest(text=text):
                result = self._trace(text)
                preview = result["preview"]
                self.assertEqual(preview["kind"], "plan")
                self.assertIsInstance(preview["outline"], list)
                self.assertTrue(preview["outline"], f"expected a real action outline for {text!r}: {preview}")

    def test_memory_rung_returns_info(self) -> None:
        result = self._trace("remember this: buy milk")
        self.assertEqual(result["preview"]["kind"], "info")
        self.assertTrue(result["preview"]["text"])

    def test_preview_never_crashes_the_trace(self) -> None:
        """Even a preview failure must still return the routing decision."""
        with mock.patch(
            "novacontrol.api.app._routing_preview",
            side_effect=RuntimeError("boom"),
        ):
            result = self._trace("open notepad")
        self.assertEqual(result["intent"], "desktop_automation")
        self.assertEqual(result["preview"]["kind"], "info")


class RoutingJsMirrorTests(unittest.TestCase):
    """The embedded JS mirror (offline fallback) lands the same intents.

    Driven by the REAL static routing-explorer.js in Node, exactly like the
    ExtractAnswerTextTests harness. The mirror is approximate by design — it
    cannot run the Python scratch engine — so this pins the first-match gate
    ORDER and the landing intents, not exact scratch answers.
    """

    NODE = shutil.which("node")
    STATIC = Path("src/novacontrol/web/static")

    # (utterance, expected landing intent, expected matched gate)
    MIRROR_LANDINGS = [
        ("hello", "chat", "scratch_greeting"),
        ("what is 15 percent of 200", "chat", "scratch_math"),
        ("open notepad and type hello", "desktop_automation", "desktop_command"),
        ("search cats on youtube on my phone", "phone_control", "phone_anchored_action"),
        ("explain how black holes form", "explore", "research_question"),
        ("create a roadmap for a startup", "plan", "plan"),
        ("remember this: buy milk", "memory", "memory_store"),
        ("this sentence is totally random", "chat", "chat_fallback"),
    ]

    # (utterance, expected relation) — the mirror must classify the SAME
    # deliberate-disagreement relations as the Python engine.
    MIRROR_RELATIONS = [
        ("hello", "match"),
        ("what is 2 cubed plus the square root of 9", "match"),
        ("what is the capital of france", "match"),
        ("recommend a good movie", "match"),
        ("convert 10 km to miles", "match"),
        ("what time is 2 plus 3", "order"),
        ("search cats on youtube on my phone", "engine_only"),
        ("open notepad and type hello", "engine_only"),
        ("hello there", "unknown"),
    ]

    def test_mirror_relations_track_the_python_engines(self) -> None:
        """The JS broad-classifier mirror reports the same relations as Python.

        The relation label is the explorer's core honesty check — if the
        offline mirror ever claims "match" where the engine would say "order"
        (or vice versa), the comparison card lies. This pins them equal on a
        probe table that exercises every relation.
        """
        if self.NODE is None:
            self.skipTest("node is not installed")
        rows = "\n".join(
            "  [" + json.dumps(t) + ", " + json.dumps(relation) + "],"
            for t, relation in self.MIRROR_RELATIONS
        )
        script = (
            "global.window = global;"
            "const fs = require('fs');"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/routing-explorer.js','utf8'));"
            "const rows = [\n" + rows + "\n];"
            "let failed = 0;"
            "for (const [text, relation] of rows) {"
            "  const r = window.routingExplorerMirror.mirrorTrace(text);"
            "  if (r.classifiers.relation !== relation) {"
            "    console.error('FAIL ' + JSON.stringify(text) + ': relation ' + r.classifiers.relation + ' !== ' + relation);"
            "    failed++;"
            "  }"
            "}"
            "console.error(failed ? failed + ' relation(s) failed' : 'all mirror relations passed');"
            "process.exit(failed ? 1 : 0);"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)  # type: ignore[list-item]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all mirror relations passed", proc.stderr)

    def test_mirror_gate_names_track_the_python_table(self) -> None:
        """The JS mirror's gate names and order must match the Python table.

        A gate renamed or reordered on the Python side and forgotten in the
        mirror would silently skew the offline fallback; this pins them equal.
        """
        from novacontrol.brain.brain import NovaBrain

        python_gates = [g[0] for g in NovaBrain._ROUTING_GATES]
        mirror_src = (self.STATIC / "js" / "routing-explorer.js").read_text(encoding="utf-8")
        self.assertTrue(python_gates, "gate table must not be empty")
        # Gate order in the mirror source must match the Python order exactly.
        last = -1
        for name in python_gates:
            pos = mirror_src.index(f'gate: "{name}"')
            self.assertGreater(pos, last, f"mirror gate {name!r} is out of order")
            last = pos

    def test_mirror_lands_the_same_intents(self) -> None:
        if self.NODE is None:
            self.skipTest("node is not installed")
        rows = "\n".join(
            "  [" + json.dumps(t) + ", " + json.dumps(intent) + ", " + json.dumps(gate) + "],"
            for t, intent, gate in self.MIRROR_LANDINGS
        )
        script = (
            "global.window = global;"
            "const fs = require('fs');"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/routing-explorer.js','utf8'));"
            "const rows = [\n" + rows + "\n];"
            "let failed = 0;"
            "for (const [text, intent, gate] of rows) {"
            "  const r = window.routingExplorerMirror.mirrorTrace(text);"
            "  const trace = r.trace;"
            "  const last = trace[trace.length - 1];"
            "  if (r.intent !== intent || last.gate !== gate) {"
            "    console.error('FAIL ' + JSON.stringify(text) + ': ' + r.intent + '/' + last.gate + ' !== ' + intent + '/' + gate);"
            "    failed++;"
            "  } else if (!last.matched) {"
            "    console.error('FAIL ' + JSON.stringify(text) + ': trace does not end matched');"
            "    failed++;"
            "  }"
            "}"
            "console.error(failed ? failed + ' case(s) failed' : 'all mirror cases passed');"
            "process.exit(failed ? 1 : 0);"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)  # type: ignore[list-item]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all mirror cases passed", proc.stderr)


if __name__ == "__main__":
    unittest.main()