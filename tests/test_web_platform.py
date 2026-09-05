from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from novacontrol.api import ApiSurface
from novacontrol.brain import BrainIntent


class WebPlatformTests(unittest.TestCase):
    """Test the web UI has the required structural contracts."""

    def setUp(self) -> None:
        static = Path("src/novacontrol/web/static")
        self.html = (static / "index.html").read_text(encoding="utf-8")
        self.css = (static / "styles.css").read_text(encoding="utf-8")
        js_files = [static / "app.js"]
        js_dir = static / "js"
        if js_dir.is_dir():
            js_files.extend(sorted(js_dir.glob("*.js")))
        self.js = "\n".join(f.read_text(encoding="utf-8") for f in js_files)

    def test_static_assets_exist(self) -> None:
        static = Path("src/novacontrol/web/static")
        self.assertTrue((static / "index.html").exists())
        self.assertTrue((static / "styles.css").exists())
        self.assertTrue((static / "app.js").exists())
        self.assertTrue((static / "js" / "dom.js").exists())
        self.assertTrue((static / "js" / "state.js").exists())

    def test_all_panels_have_ids(self) -> None:
        for panel in ("homePanel", "chatPanel", "explorePanel", "buildPanel",
                       "learnPanel", "systemPanel", "settingsPanel", "cliPanel"):
            self.assertIn(f'id="{panel}"', self.html, f"Missing panel: {panel}")

    def test_input_elements_have_ids(self) -> None:
        for input_id in ("commandInput", "chatInput", "exploreInput", "planInput",
                          "improveInput", "learnGoal", "learnFeedback"):
            self.assertIn(f'id="{input_id}"', self.html, f"Missing input: {input_id}")

    def test_action_buttons_have_ids(self) -> None:
        for btn_id in ("askButton", "exploreButton", "planButton", "improveButton",
                        "previewButton", "approveButton", "learnButton", "trainButton",
                        "commandPlanButton", "commandRunButton"):
            self.assertIn(f'id="{btn_id}"', self.html, f"Missing button: {btn_id}")

    def test_voice_controls_present(self) -> None:
        self.assertIn("voiceButton", self.html)
        self.assertIn("voiceChatButton", self.html)
        self.assertIn("stopVoiceButton", self.html)

    def test_javascript_implements_core_renderers(self) -> None:
        for fn in ("submitChat", "renderCommand", "renderWorkflow",
                    "renderAiAnswerPage", "renderAnswerSections"):
            self.assertIn(fn, self.js, f"Missing JS function: {fn}")

    def test_javascript_has_api_routes(self) -> None:
        for route in ("/command/plan", "/command/execute", "/explore",
                       "/improve/preview", "/improve/approve", "/learn", "/train"):
            self.assertIn(route, self.js, f"Missing API route: {route}")

    def test_css_has_layout_system(self) -> None:
        for cls in ("app-shell", "sidebar", "workspace", "panel", "ai-answer-layout",
                     "source-rail", "answer-card", "structured-section"):
            self.assertIn(cls, self.css, f"Missing CSS class: {cls}")

    def _reduced_motion_block(self) -> str:
        """Extract the body of the prefers-reduced-motion media block."""
        marker = "@media (prefers-reduced-motion: reduce) {"
        start = self.css.find(marker)
        self.assertNotEqual(start, -1, "Missing prefers-reduced-motion media query")
        depth = 0
        for i in range(start, len(self.css)):
            if self.css[i] == "{":
                depth += 1
            elif self.css[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.css[start : i + 1]
        self.fail("Unterminated prefers-reduced-motion media block")

    def test_reduced_motion_emulation_disables_animations(self) -> None:
        """Emulating prefers-reduced-motion must collapse all CSS motion app-wide.

        The static file is the served output (no build step), so asserting the
        block contents here is the check that reduced-motion users get no
        animations, transitions, smooth scroll, or will-change compositing.
        """
        block = self._reduced_motion_block()
        self.assertIn("*, *::before, *::after", block, "Reduced-motion block must cover pseudo-elements")
        for decl in (
            "animation-duration: 0.01ms !important",
            "animation-iteration-count: 1 !important",
            "animation-delay: 0s !important",
            "transition-duration: 0.01ms !important",
            "transition-delay: 0s !important",
            "scroll-behavior: auto !important",
        ):
            self.assertIn(decl, block, f"Reduced-motion block missing disable rule: {decl}")

    def test_asset_versions_use_server_injected_marker(self) -> None:
        """Cache-bust tokens are injected at serve time, never hardcoded by hand."""
        self.assertIn("?v=__NC_ASSET_VERSION__", self.html)
        self.assertNotIn("?v=2026", self.html, "hardcoded date token found — use the server-injected marker")

    def test_ui_version_stamp_uses_injected_marker(self) -> None:
        """The visible sidebar stamp must show the served build id, not a hand-written date."""
        self.assertIn('id="uiVersion"', self.html, "Missing sidebar UI version stamp")
        self.assertIn("UI v__NC_UI_VERSION__", self.html, "UI stamp must use the server-injected marker")
        self.assertNotIn("UI v2026", self.html, "hardcoded date in UI stamp — use __NC_UI_VERSION__")

    def test_raw_output_only_in_cli_panel(self) -> None:
        self.assertIn('id="cliOutput"', self.html)
        self.assertNotIn('id="buildOutput" class="cli-output"', self.html)
        self.assertNotIn('id="exploreOutput" class="cli-output"', self.html)


class ExtractAnswerTextTests(unittest.TestCase):
    """Pin the shared chat-answer text extractor (state.js extractAnswerText).

    Runs the REAL static JS in Node against a table of response shapes so a
    future reorder of the documented field precedence, a weakened leak scrub,
    or a lost fallback fails CI instead of silently changing chat answers.
    Skips cleanly on machines without Node (the app itself is pure Python).
    """

    NODE = shutil.which("node")
    STATIC = Path("src/novacontrol/web/static")

    # (name, JS expression, expected result) — precedence is documented in
    # extractAnswerText's header comment; this table is the executable copy.
    CASES = [
        # /ask envelope: data.message wins over the top-level summary.
        ("ask-envelope", "cleanSummary({data:{message:'**bold** answer'},summary:'short'})", "**bold** answer"),
        # Payload answer beats a top-level summary.
        ("data-answer", "extractAnswerText({data:{answer:'full answer'},summary:'short'})", "full answer"),
        # Payload content is the free-text fallback when message/answer absent.
        ("data-content", "extractAnswerText({data:{content:'free text'}})", "free text"),
        # Flat body (no data key): top-level fields are scanned in the same order.
        ("flat-message", "extractAnswerText({message:'top message'})", "top message"),
        ("flat-summary", "cleanSummary({command:'x',summary:'Prepared a phone action.'})", "Prepared a phone action."),
        # Empty result falls back to the caller-provided default.
        ("empty-default", "cleanSummary({})", "I completed the request."),
        ("string-empty-fallback", "extractAnswerText('', 'fb')", "fb"),
        # Leaked internal routing context is scrubbed to a friendly line.
        ("leak-scrub", "extractAnswerText({data:{content:'Request: hi\\nIntent: scratch\\nPayload: {...}'}})", "I routed this through NovaControl and prepared a clear result."),
        # Bare strings pass through (trimmed).
        ("string", "cleanSummary('plain')", "plain"),
        # textForSpeech: shared precedence first, then speech-only fallbacks.
        ("speech-message", "textForSpeech({data:{message:'m'}})", "m"),
        ("speech-training", "textForSpeech({training_scope:'scope text'})", "scope text"),
        ("speech-ready", "textForSpeech({ready:true})", "Ready"),
        ("speech-default", "textForSpeech({})", "Result ready."),
        ("speech-empty", "textForSpeech('')", ""),
    ]

    def _node_run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)  # type: ignore[list-item]

    def test_extractor_precedence_is_pinned(self) -> None:
        if self.NODE is None:
            self.skipTest("node is not installed")
        # expr is emitted as a JSON string so node stores the SOURCE, then evals
        # it in the loop below — emitting it raw would evaluate it at array-build
        # time and turn expr into the result instead of the expression.
        cases = "\n".join(
            f"  [{json.dumps(name)}, {json.dumps(expr)}, {json.dumps(want)}]," for name, expr, want in self.CASES
        )
        script = (
            "const fs = require('fs');"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8'));"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/render-utils.js','utf8'));"
            "const cases = [\n" + cases + "\n];"
            "let failed = 0;"
            "for (const [name, expr, want] of cases) {"
            "  let got;"
            "  try { got = eval(expr); } catch (e) { got = 'THREW: ' + e.message; }"
            "  if (got !== want) {"
            "    console.error('FAIL ' + name + ': ' + JSON.stringify(got) + ' !== ' + JSON.stringify(want));"
            "    failed++;"
            "  }"
            "}"
            "console.error(failed ? failed + ' case(s) failed' : 'all extractor cases passed');"
            "process.exit(failed ? 1 : 0);"
        )
        proc = self._node_run(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all extractor cases passed", proc.stderr)

    def test_no_call_site_reimplements_field_precedence(self) -> None:
        """The inline chains this pass removed must not come back."""
        render_panels = (self.STATIC / "js" / "render-panels.js").read_text(encoding="utf-8")
        app = (self.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("payload.message || payload.content || data.summary", render_panels)
        self.assertNotIn("payload.answer || payload.message || payload.content", app)

    def test_no_envelope_payload_sniffing_remains(self) -> None:
        """/ask envelopes nest under `data`; chat renderers must not probe `payload` again.

        The old tryRenderResearch probed candidates [data.payload, data, data.report]
        and extractAnswerText unwrapped value.payload. After the flatten these must
        stay gone — the only tolerated `data.payload || data` fallback is
        renderBuild's, which never receives an /ask envelope.
        """
        render_explore = (self.STATIC / "js" / "render-explore.js").read_text(encoding="utf-8")
        state = (self.STATIC / "js" / "state.js").read_text(encoding="utf-8")
        render_panels = (self.STATIC / "js" / "render-panels.js").read_text(encoding="utf-8")
        self.assertNotIn("data.payload", render_explore)
        self.assertNotIn("data.report", render_explore)
        self.assertNotIn("value.payload", state)
        self.assertEqual(
            render_panels.count("data.payload || data"),
            1,
            "only renderBuild's flat-body fallback may keep `data.payload || data`",
        )


class BackendFrontendContractTests(unittest.TestCase):
    """Every backend surface must map to an intended frontend render branch.

    The contract registry below is the single place that answers "what renders
    this response?": each (method, path) either names the renderer that owns its
    body, names renderGeneric (the explicit fallback), or is enumerated as a
    non-render consumer (infrastructure probe, EventSource channel, form fill,
    CLI-only API). Drift in either direction fails: a new backend route without
    a declared consumer, or a declared consumer whose endpoint disappears.
    """

    def setUp(self) -> None:
        static = Path("src/novacontrol/web/static")
        js_files = [static / "app.js"]
        js_dir = static / "js"
        if js_dir.is_dir():
            js_files.extend(sorted(js_dir.glob("*.js")))
        self.js = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
        self.render_utils = (static / "js" / "render-utils.js").read_text(encoding="utf-8")
        self.render_panels = (static / "js" / "render-panels.js").read_text(encoding="utf-8")

    # Route -> renderer that owns the body, or one of ALLOWED_NON_RENDER markers:
    #   no-render    — infrastructure / channel / CLI-only endpoint with no panel
    #   load-settings — GET /settings fills the settings form (loadSettings), no render
    ROUTE_CONSUMERS: dict[tuple[str, str], str] = {
        # Rendered panels (run(type) -> renderer dispatch, render-utils.js).
        ("POST", "/ask"): "renderChatResult",  # envelope branches by route/intent
        ("POST", "/explore"): "renderExplore",
        ("POST", "/command/plan"): "renderCommand",
        ("POST", "/command/execute"): "renderCommand",
        ("POST", "/desktop/plan"): "renderCommand",
        ("POST", "/desktop/execute"): "renderCommand",
        ("POST", "/phone/plan"): "renderCommand",
        ("POST", "/phone/execute"): "renderCommand",
        ("POST", "/plan"): "renderBuild",
        ("POST", "/improve/workflow"): "renderWorkflow",
        ("POST", "/improve/preview"): "renderWorkflow",
        ("POST", "/improve/approve"): "renderWorkflow",
        ("POST", "/learn"): "renderLearning",
        ("POST", "/train"): "renderLearning",
        ("GET", "/system/health"): "renderHealth",
        ("GET", "/system/harden"): "renderHealth",
        ("GET", "/system/package"): "renderGeneric",  # explicit generic fallback
        ("GET", "/status"): "renderGeneric",  # home metrics + generic fallback card
        ("POST", "/settings"): "renderSettingsResult",
        # Enumerated non-render consumers.
        ("GET", "/settings"): "load-settings",
        ("GET", "/events/stream"): "no-render",  # EventSource activity channel
        ("GET", "/"): "no-render",  # served HTML (index.html)
        ("GET", "/health"): "no-render",  # infrastructure probe
        ("POST", "/improve"): "no-render",  # raw plan API (CLI/demos only)
        ("GET", "/phone/status"): "no-render",  # bridge status (CLI/demos only)
        ("GET", "/plugins"): "no-render",  # plugin marketplace API (CLI only)
    }
    ALLOWED_NON_RENDER = {"no-render", "load-settings"}

    # run(type) -> renderer dispatch, mirroring render() in render-utils.js.
    RENDER_DISPATCH = [
        ("chat", "renderChatResult"),
        ("explore", "renderExplore"),
        ("command", "renderCommand"),
        ("build", "renderBuild"),
        ("workflow", "renderWorkflow"),
        ("learn", "renderLearning"),
        ("health", "renderHealth"),
        ("settings", "renderSettingsResult"),
        ("generic", "renderGeneric"),
    ]

    # /ask envelope routes (decision.intent values) -> branch inside renderChatResult.
    INTENT_BRANCH: dict[str, str] = {
        "explore": "report",  # payload is a research report -> tryRenderResearch
        "desktop_automation": "command",  # approval plan -> renderCommand
        "browser_automation": "command",
        "phone_control": "command",
        "chat": "general",  # scratch/LLM chat answer -> renderGeneralAiPage
        "plan": "general",
        "self_improvement": "general",
        "agent": "general",
        "memory": "general",
        "project": "general",
        "clarify": "general",
    }
    ALLOWED_BRANCHES = {"report", "command", "general"}

    def test_every_api_route_has_a_declared_frontend_consumer(self) -> None:
        """ApiSurface and the registry must agree exactly (drift fails both ways)."""
        routes = {(route.method, route.path) for route in ApiSurface.default().routes}
        self.assertEqual(routes, set(self.ROUTE_CONSUMERS))

    def test_consumer_is_render_branch_or_explicit_fallback(self) -> None:
        """Each consumer names a real renderer or an enumerated non-render role."""
        for route, consumer in self.ROUTE_CONSUMERS.items():
            with self.subTest(route=route):
                if consumer in self.ALLOWED_NON_RENDER:
                    continue
                self.assertTrue(
                    consumer.startswith("render"),
                    f"{route} consumer must name a renderer or be an allowed marker: {consumer}",
                )
                self.assertIn(f"function {consumer}(", self.js, f"missing renderer {consumer}")

    def test_render_dispatches_each_type_to_its_renderer(self) -> None:
        """render() maps every run(type) to the renderer the registry names.

        generic has no explicit `type === "generic"` branch — it IS the final
        `else renderGeneric(...)` fallback, asserted separately.
        """
        for render_type, renderer in self.RENDER_DISPATCH:
            with self.subTest(type=render_type):
                if render_type == "generic":
                    self.assertIn("else renderGeneric(", self.render_utils)
                else:
                    self.assertIn(f'type === "{render_type}"', self.render_utils)
                self.assertIn(f"function {renderer}(", self.js)

    def test_every_ask_intent_has_a_render_branch(self) -> None:
        """Every brain intent value maps to a documented renderChatResult branch."""
        intents = {intent.value for intent in BrainIntent}
        self.assertEqual(intents, set(self.INTENT_BRANCH), "add new intents to INTENT_BRANCH")
        self.assertTrue(set(self.INTENT_BRANCH.values()) <= self.ALLOWED_BRANCHES)
        # Each allowed branch must actually exist in renderChatResult's code.
        self.assertIn("tryRenderResearch", self.render_panels)  # report branch
        self.assertIn("renderCommand(stream, payload)", self.render_panels)  # command branch
        # General branch: renderGeneralAiPage with its neutral explicit fallback.
        self.assertIn("function renderGeneralAiPage(", self.render_panels)
        self.assertIn("I prepared the result in the local runtime.", self.render_panels)


if __name__ == "__main__":
    unittest.main()
