from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from novacontrol.api import ApiSurface
from novacontrol.api.route_consumers import NON_RENDER_ROLES, ROUTE_CONSUMERS as ROUTE_CONSUMER_TABLE
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

    def test_system_panel_has_wired_brain_status_card(self) -> None:
        """The System panel shows the detected LLM (provider + model) without JSON."""
        self.assertIn('id="brainStatus"', self.html, "System panel lacks the brain status container")
        self.assertIn("function renderBrainStatus(", self.js, "renderBrainStatus is missing")
        self.assertIn(
            "renderBrainStatus(status)", self.js,
            "refreshStatus() must feed /status into renderBrainStatus",
        )
        # The card reads the brain block the backend serves (pinned over HTTP in
        # test_web_api.py) and names the provider/model, falling back to scratch.
        self.assertIn("status?.app?.brain", self.js)
        self.assertIn("brain.model", self.js)

    def test_javascript_has_api_routes(self) -> None:
        for route in ("/command/plan", "/command/execute", "/explore",
                       "/improve/preview", "/improve/approve", "/learn", "/train"):
            self.assertIn(route, self.js, f"Missing API route: {route}")

    def test_css_has_layout_system(self) -> None:
        for cls in ("app-shell", "sidebar", "workspace", "panel", "ai-answer-layout",
                     "source-rail", "answer-card", "structured-section"):
            self.assertIn(cls, self.css, f"Missing CSS class: {cls}")

    def test_every_interactive_element_maps_to_a_focus_visible_rule(self) -> None:
        """Freeze the keyboard ring coverage verified live: each interactive
        element authored in index.html must be covered by a :focus-visible rule
        that declares an outline.

        Strictly keyboard-scoped: a plain :focus rule does NOT satisfy this.
        The live walk proved rings are :focus-visible (mouse clicks correctly
        show no ring), so coverage by a mouse-focus-only rule would silently
        break that contract. The one deliberate exception is .skip-link, which
        uses plain :focus so its ring also appears on programmatic focus after
        a skip jump.
        """
        css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        rules = [(s, b) for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                 if ":focus-visible" in s]
        interactive = re.finditer(r"<(a|button|input|select|textarea)\b([^>]*)>", self.html)
        uncovered = []
        for m in interactive:
            attrs = m.group(2)
            if attrs.lstrip().startswith("/"):
                continue
            classes = re.search(r'class="([^"]+)"', attrs)
            if not classes:
                continue  # no class selector to map (none occur in index.html)
            covered = any(
                re.search(rf"(?:^|[^\w-])\.{re.escape(cls)}(?![\w-])", sel)
                and "outline" in body and "outline: none" not in body
                for cls in classes.group(1).split()
                for sel, body in rules
            )
            if covered:
                continue
            if classes.group(1).split() == ["skip-link"]:
                continue  # documented exception: plain :focus ring by design
            name = re.search(r'id="([^"]+)"', attrs)
            uncovered.append(
                f"<{m.group(1)} class=\"{classes.group(1)}\""
                + (f" id={name.group(1)}" if name else "") + ">"
            )
        self.assertEqual(
            uncovered, [],
            "interactive index.html elements have no :focus-visible ring rule: "
            + ", ".join(uncovered),
        )

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

    def test_reduced_motion_gates_js_driven_motion(self) -> None:
        """JS motion must read the same prefers-reduced-motion signal the CSS uses.

        effects.js owns all JS-driven motion (3D background canvas loop + card
        tilt). Each effect must START with the matchMedia guard (count == 2) and
        the guard must precede the motion it gates — background guard before its
        requestAnimationFrame loop, tilt guard before its transform writes — so
        no animation can start before the reduced-motion check runs. The static
        file is the served output (no build step), so this pin is the check that
        reduced-motion users get no canvas redraw or tilt either.
        """
        static = Path("src/novacontrol/web/static")
        effects = (static / "js" / "effects.js").read_text(encoding="utf-8")
        render_utils = (static / "js" / "render-utils.js").read_text(encoding="utf-8")
        guard = (
            "  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {"
            "\n    return;\n  }"
        )
        self.assertEqual(
            effects.count(guard), 2,
            "each JS motion effect (background canvas + card tilt) must start "
            "with the reduced-motion guard",
        )
        # Guard ordering: background guard -> its rAF loop -> tilt guard -> its writes.
        first_guard = effects.find(guard)
        self.assertNotEqual(first_guard, -1)
        tilt_guard = effects.find(guard, first_guard + 1)
        positions = {
            "background canvas guard": first_guard,
            "canvas loop (requestAnimationFrame)": effects.find("requestAnimationFrame("),
            "card tilt guard": tilt_guard,
            "tilt rotate write": effects.find("perspective(800px)"),
        }
        keys = list(positions)
        for i in range(len(keys) - 1):
            prev, nxt = keys[i], keys[i + 1]
            self.assertLess(
                positions[prev], positions[nxt],
                f"{prev} must precede {nxt} so motion never starts before its reduced-motion guard",
            )
        # render-utils.js owns no animation of its own; keep it loop-free so it
        # can never start unguarded motion (no rAF, no transform writes).
        self.assertNotIn("requestAnimationFrame", render_utils)
        self.assertNotIn("style.transform", render_utils)

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

    # Reachability pin for the `content` branch of extractAnswerText: the /ask
    # agent fallback (_handle_agent → AgentResponse.to_dict()) is the one handler
    # that produces a payload with content and NO message. Proven live:
    #   POST /ask {"request": "create tests for this module"}
    #   → data = {task_id, agent_name, role, status, content, metadata, brain_mode}
    # The inner-data scan must hit data.content BEFORE the envelope summary, so
    # the chat bubble shows the agent's real text, not the generic fallback.
    def test_agent_fallback_payload_is_content_only_and_reachable(self) -> None:
        agent_payload = {
            "task_id": "t1", "agent_name": "TestAgent", "role": "testing",
            "status": "completed", "content": "Agent prepared the test plan.",
            "metadata": {}, "brain_mode": "scratch",
        }
        cases = [
            # Exact live agent-fallback shape: content wins over envelope summary.
            ("agent-content-only", json.dumps({"route": "agent", "summary": "Test strategy prepared.", "data": agent_payload}), "Agent prepared the test plan."),
            # Deleting the content branch must fail this: content gone → summary leaks through.
            ("content-beats-summary", json.dumps({"data": {"content": "real answer"}, "summary": "envelope summary"}), "real answer"),
        ]
        cases_js = "\n".join(
            f"  [{json.dumps(name)}, {json.dumps(expr)}, {json.dumps(want)}]," for name, expr, want in cases
        )
        script = (
            "const fs = require('fs');"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8'));"
            "eval(fs.readFileSync('src/novacontrol/web/static/js/render-utils.js','utf8'));"
            "const cases = [\n" + cases_js + "\n];"
            "let failed = 0;"
            "for (const [name, expr, want] of cases) {"
            # Parens around the eval are load-bearing: a bare {…} would parse as
            # a block, not an object. No // comments inside the JS string — the
            # script is one line, so a line comment would eat the whole program.
            "  const got = cleanSummary(eval('(' + expr + ')'));"
            "  if (got !== want) {"
            "    console.error('FAIL ' + name + ': ' + JSON.stringify(got) + ' !== ' + JSON.stringify(want));"
            "    failed++;"
            "  }"
            "}"
            "console.error(failed ? failed + ' case(s) failed' : 'all reachability cases passed');"
            "process.exit(failed ? 1 : 0);"
        )
        proc = self._node_run(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all reachability cases passed", proc.stderr)

    def test_no_envelope_payload_sniffing_remains(self) -> None:
        """/ask envelopes nest under `data`; no renderer may probe `payload` again.

        The old tryRenderResearch probed candidates [data.payload, data, data.report],
        extractAnswerText unwrapped value.payload, and renderBuild kept a dead
        `data.payload || data` fallback (its /plan body is always flat). After the
        flatten, every one of these probes must stay gone.
        """
        render_explore = (self.STATIC / "js" / "render-explore.js").read_text(encoding="utf-8")
        state = (self.STATIC / "js" / "state.js").read_text(encoding="utf-8")
        render_panels = (self.STATIC / "js" / "render-panels.js").read_text(encoding="utf-8")
        self.assertNotIn("data.payload", render_explore)
        self.assertNotIn("data.report", render_explore)
        self.assertNotIn("value.payload", state)
        self.assertNotIn("data.payload", render_panels)


class RenderGeneralAiPageDomTests(unittest.TestCase):
    """DOM-level pins for renderGeneralAiPage (render-panels.js).

    Two historical regressions are frozen here, driving the REAL static JS in
    Node (like ExtractAnswerTextTests) against a minimal DOM shim:

    1. Leaked routing text. renderGeneralAiPage must never read
       payload.message/payload.content as answer text — the lead (summary)
       is the only place the answer renders, and the section below the heading
       is STRUCTURE ONLY. A raw field read there re-leaked scrubbed
       Request:/Intent:/Payload: routing text (the payload.message !== summary
       guard fired on sanitizer deltas) and duplicated content in lead+body.
    2. Answer rendered exactly once. A message-only payload must produce the
       answer text exactly once across the whole layout (the lead), with the
       neutral placeholder in the section — never the same text twice.

    The shim implements only the DOM surface the renderer touches
    (createElement/createTextNode/createDocumentFragment, appendChild,
    textContent, className, classList, querySelectorAll) so the REAL el(),
    renderMd(), richBullet(), and renderGeneralAiPage() run unmodified.
    Skips cleanly on machines without Node (the app itself is pure Python).
    """

    NODE = shutil.which("node")
    STATIC = Path("src/novacontrol/web/static")

    # Same load order as index.html, minus effects.js (canvas) and app.js
    # (init bindings — its top level would explode without a full DOM; the
    # renderer under test only needs dom.js + state.js + render-utils.js
    # helpers plus render-panels.js itself).
    SCRIPTS = ("js/dom.js", "js/state.js", "js/render-utils.js", "js/render-panels.js")

    # ── DOM shim ────────────────────────────────────────────
    # JS source (kept as a Python template; { } are doubled for .format).
    # Every node records its tag/class/text so assertions can walk the tree.
    SHIM = r'''
function makeNode(tag) {{
  const node = {{
    tagName: String(tag).toUpperCase(),
    children: [],
    _text: "",
    className: "",
    style: {{}},
    _classes() {{ return this.className ? this.className.split(/\s+/) : []; }},
    appendChild(child) {{
      if (child && child.__fragment) {{
        for (const c of child.children.slice()) this.appendChild(c);
        return child;
      }}
      child.parentNode = this;
      this.children.push(child);
      return child;
    }},
    addEventListener() {{}},
    get classList() {{
      const self = this;
      return {{
        add(...names) {{ const s = new Set(self._classes()); names.forEach(n => s.add(n)); self.className = [...s].join(" "); }},
        remove(...names) {{ const s = new Set(self._classes()); names.forEach(n => s.delete(n)); self.className = [...s].join(" "); }},
        toggle(name, on) {{ const s = new Set(self._classes()); (on === undefined ? !s.has(name) : on) ? s.add(name) : s.delete(name); self.className = [...s].join(" "); }},
        contains(name) {{ return self._classes().includes(name); }},
      }};
    }},
    get textContent() {{
      if (this.children.length === 0) return this._text;
      return this.children.map(c => c.textContent).join("");
    }},
    set textContent(value) {{ this._text = String(value); this.children = []; }},
  }};
  if (tag === "#fragment") node.__fragment = true;
  return node;
}}
function flatten(node, out = []) {{
  out.push(node);
  for (const c of node.children || []) flatten(c, out);
  return out;
}}
// Walk-like querySelectorAll supporting only the selector shapes the assertions
// use: ".class" and bare tag names ("li") at any depth. Not a CSS engine.
function qsa(root, selector) {{
  const cls = selector.startsWith(".") ? selector.slice(1) : null;
  const tag = cls ? null : selector.toUpperCase();
  return flatten(root).filter(
    n => n !== root && (cls ? n._classes().includes(cls) : n.tagName === tag)
  );
}}
const document = {{
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => {{ const n = makeNode("#text"); n._text = String(text); return n; }},
  createDocumentFragment: () => makeNode("#fragment"),
}};
'''

    def _run_renderer(self, payload: dict, summary_expr: str) -> dict:
        """Eval the real scripts + shim in one sloppy-mode eval, then run
        renderGeneralAiPage and return a serializable inspection of the layout.

        One eval (not separate evals) keeps function declarations (el, renderMd,
        richBullet, renderGeneralAiPage) AND the shim's const bindings in the
        same scope — the DOM shim must exist before the renderer runs.
        """
        if self.NODE is None:
            self.skipTest("node is not installed")
        scripts = "\n".join(
            (self.STATIC / name).read_text(encoding="utf-8") for name in self.SCRIPTS
        )
        driver = f"""
const payload = {json.dumps(payload)};
const summaryExpr = {json.dumps(summary_expr)};
const summary = eval(summaryExpr);  // cleanSummary(payload) — the real call site
const layout = el("article", "ai-answer-layout");
renderGeneralAiPage(layout, {{ query: "What is 2+2", summary, route: "chat", payload }});
console.log(JSON.stringify({{
  lead: qsa(layout, ".answer-lead").map(n => n.textContent),
  placeholder: qsa(layout, ".answer-section").map(n => n.textContent),
  bullets: qsa(layout, "li").map(n => n.textContent),
  rail: qsa(layout, ".rail-card").map(n => n.textContent),
  cards: qsa(layout, ".info-card").map(n => n.textContent),
}}));
"""
        script = (
            "const fs = require('fs');"
            + self.SHIM.format()
            + "eval(fs.readFileSync('src/novacontrol/web/static/js/dom.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/render-utils.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/render-panels.js','utf8') + '\\n'"
            " + " + json.dumps(driver) + ");"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"renderer crashed under the shim: {proc.stderr}")
        return json.loads(proc.stdout)

    def _walk_text(self, result: dict) -> list[str]:
        """Every text the layout emitted (lead, sections, bullets, rail)."""
        return result["lead"] + result["placeholder"] + result["bullets"] + result["rail"]



    # ── 1. Scrubbed routing text never leaks ───────────────

    def test_scrubbed_routing_text_never_leaks_anywhere(self) -> None:
        """The historical leak: a payload whose message/content carry raw
        Request:/Intent:/Payload: text. The lead must show the friendly scrub
        line (via cleanSummary -> sanitizeAnswerText), and NO node anywhere in
        the layout may echo the raw routing text — not the lead, not the
        section, not the rail.
        """
        raw = "Request: hi\nIntent: scratch\nPayload: {'a': 1}"
        result = self._run_renderer(
            {"message": raw, "content": raw, "provider": "scratch"},
            "cleanSummary(payload)",
        )
        texts = self._walk_text(result)
        self.assertTrue(texts, "layout rendered no text at all")
        for needle in ("Request: hi", "Intent: scratch", "Payload:"):
            self.assertFalse(
                any(needle in t for t in texts),
                f"routing text {needle!r} leaked into the layout: {texts!r}",
            )
        self.assertIn(
            "I routed this through NovaControl and prepared a clear result.",
            "\n".join(texts),
            "the friendly scrub line must be what actually renders",
        )

    def test_rail_reports_brain_mode_without_re_reading_message(self) -> None:
        """The Context rail reads brain_mode/provider only — a hostile message
        full of routing text must not surface there (it used to).
        """
        raw = "Request: x\nIntent: chat\nPayload: {...}"
        result = self._run_renderer(
            {"message": raw, "brain_mode": "scratch", "provider": "scratch"},
            "cleanSummary(payload)",
        )
        rail = "\n".join(result["rail"])
        self.assertIn("Brain mode: scratch.", rail)
        self.assertNotIn("Request: x", rail)

    # ── 2. Message-only payloads render exactly once ────────

    def test_message_only_payload_renders_answer_exactly_once(self) -> None:
        """A payload with ONLY a message: the answer appears exactly once (the
        lead), the section carries the neutral placeholder, and the layout has
        no suggestion chips / plan list / reasoning blocks that could duplicate
        the answer text.
        """
        answer = "The capital of France is Paris."
        result = self._run_renderer(
            {"message": answer, "provider": "scratch"},
            "cleanSummary(payload)",
        )
        self.assertEqual(result["lead"], [answer], "the lead must render the answer verbatim, once")
        section = "\n".join(result["placeholder"])
        self.assertIn("I prepared the result in the local runtime.", section)
        self.assertIn("Chat", section, "label(route) drives the section heading")
        self.assertNotIn(answer, section, "the section must not echo the answer text again")

    def test_structured_payload_renders_each_piece_once(self) -> None:
        """Suggestions render as chips (never also as bullets); sections render
        their items once. Structural duplication is the same bug as text
        duplication — both mean a renderer bypass or a second pass.
        """
        result = self._run_renderer(
            {
                "message": "Answer text.",
                "sections": [{"title": "Reasoning", "items": ["step one", "step two"]}],
            },
            "cleanSummary(payload)",
        )
        texts = self._walk_text(result)
        self.assertEqual(texts.count("step one"), 1)
        self.assertEqual(texts.count("step two"), 1)
        self.assertEqual(texts.count("Answer text."), 1, "lead only — the section is structure-only")


    # ── 3. waiting_for_phone_bridge renders blocked, not approvable ──

class RenderCommandBridgeBlockedTests(unittest.TestCase):
    """DOM-level pins for renderCommand's waiting_for_phone_bridge state.

    The phone plan shape must render a distinct blocked card (pairing
    next-steps, NOT an approval banner) and must NOT render an inline Approve
    And Run button — execution is impossible until a device reports available.
    Drives the real render-panels.js in Node against the same DOM shim as
    RenderGeneralAiPageDomTests; skips cleanly without Node.
    """

    NODE = shutil.which("node")
    STATIC = Path("src/novacontrol/web/static")
    SCRIPTS = ("js/dom.js", "js/state.js", "js/render-utils.js", "js/render-panels.js")

    def _run_render_command(self, data: dict) -> dict:
        if self.NODE is None:
            self.skipTest("node is not installed")
        shim = RenderGeneralAiPageDomTests.SHIM.format()
        scripts = "\n".join(
            (self.STATIC / name).read_text(encoding="utf-8") for name in self.SCRIPTS
        )
        driver = f"""
const data = {json.dumps(data)};
const out = makeNode("div");
renderCommand(out, data);
const buttons = flatten(out).filter(n => n.tagName === "BUTTON");
console.log(JSON.stringify({{
  summaries: qsa(out, ".summary").map(n => n.textContent),
  cards: qsa(out, ".info-card").map(n => ({{ classes: n.className, text: n.textContent }})),
  banners: qsa(out, ".approval-banner").map(n => n.textContent),
  buttons: buttons.map(n => ({{ text: n.textContent, disabled: !!n.disabled }})),
  steps: qsa(out, "li").map(n => n.textContent),
}}));
"""
        script = (
            "const fs = require('fs');"
            + shim
            + "eval(fs.readFileSync('src/novacontrol/web/static/js/dom.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/render-utils.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/render-panels.js','utf8') + '\\n'"
            " + " + json.dumps(driver) + ");"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"renderCommand crashed under the shim: {proc.stderr}")
        return json.loads(proc.stdout)

    BRIDGE_PLAN = {
        "route": "phone_control",
        "status": "waiting_for_phone_bridge",
        "command": "text mom on my phone saying hi",
        "target": "mom",
        "summary": "Prepared a text message for your phone.",
        # Carries a token deliberately (stale/replayed plan scenario): even when a
        # token is present, a waiting_for_phone_bridge plan must never offer
        # execution — the state check, not token absence, is what gates the button.
        "approval": {"required": True, "approved": False, "message": "Pair and review the phone action before running it.", "token": "tok-stale"},
        "bridge": {
            "available": False, "state": "not_configured", "adapter": "adb",
            "next_steps": (
                "Install Android platform tools and make adb available on PATH.",
                "Pair the phone explicitly before executing actions.",
            ),
        },
        "workflow": {"id": "wf", "name": "Send text on phone", "actions": [
            {"id": "a1", "type": "send_text", "target": "mom", "description": "Send a text message on the paired phone", "parameters": {"recipient": "mom", "message": "hi"}},
        ]},
    }

    READY_PLAN = {
        **BRIDGE_PLAN,
        "status": "waiting_for_approval",
        "approval": {"required": True, "approved": False, "message": "Review the phone action before running it.", "token": "tok-1"},
        "bridge": {"available": True, "state": "device_connected", "adapter": "adb", "next_steps": ()},
    }

    def test_blocked_plan_shows_pairing_guidance_and_no_run_button(self) -> None:
        out = self._run_render_command(self.BRIDGE_PLAN)
        joined = " ".join(card["text"] for card in out["cards"])
        self.assertTrue(any("Phone bridge needed" in card["text"] for card in out["cards"]), joined)
        self.assertTrue(any("Install Android platform tools" in text for text in out["steps"]), out["steps"])
        self.assertTrue(any("Pair the phone explicitly" in text for text in out["steps"]), out["steps"])
        self.assertEqual(out["banners"], [], "blocked state must not render the approval banner")
        self.assertEqual(
            [b for b in out["buttons"] if "Approve And Run" in b["text"]],
            [],
            "blocked state must not offer execution",
        )

    def test_ready_plan_keeps_approval_banner_and_run_button(self) -> None:
        out = self._run_render_command(self.READY_PLAN)
        self.assertTrue(any("Approval needed" in text for text in out["banners"]), out["banners"])
        self.assertEqual(
            [b["text"] for b in out["buttons"] if "Approve And Run" in b["text"]],
            ["Approve And Run"],
        )
        # The ready path keeps the compact bridge card (device ready), not the blocked card.
        self.assertFalse(any("Phone bridge needed" in card["text"] for card in out["cards"]))

class StatePreviewTokenTests(unittest.TestCase):
    """Pin the improvement-preview token helpers in state.js.

    Approve And Apply must reuse the preview id bound to THIS goal (no server
    re-derive on every click), skip approve responses that already applied,
    and clear after use. Runs the REAL static JS in Node; skips without node.
    """

    NODE = shutil.which("node")

    def test_preview_helpers_bind_goal_and_lifecycle(self) -> None:
        if self.NODE is None:
            self.skipTest("node is not installed")
        checks = "\n".join([
            "let failed = 0;",
            "const check = (name, got, want) => {",
            "  if (got !== want) {",
            "    console.error('FAIL ' + name + ': ' + JSON.stringify(got) + ' !== ' + JSON.stringify(want));",
            "    failed++;",
            "  }",
            "};",
            # A fresh, unapproved preview binds its id to its goal.
            "rememberPreview({goal:'g1',preview:{id:'p1'},status:'temporary_preview_ready',"
            "approval:{required:true,approved:false}});",
            "check('stores-fresh-preview', state.preview && state.preview.previewId, 'p1');",
            "check('reused-for-same-goal', previewFor('g1'), 'p1');",
            "check('other-goal-no-reuse', previewFor('g2'), '');",
            "check('empty-goal-no-reuse', previewFor(''), '');",
            # An approve response carries the preview back already applied: skip it.
            "rememberPreview({goal:'g2',preview:{id:'p2'},status:'approved_and_applied',"
            "approval:{required:true,approved:true}});",
            "check('approve-response-not-remembered', previewFor('g2'), '');",
            # A successful apply clears the stored id (no silent re-apply).
            "rememberPreview({goal:'g3',preview:{id:'p3'},approval:{approved:false}});",
            "clearPreview('g3');",
            "check('cleared-after-apply', previewFor('g3'), '');",
            # A stale preview (past the client TTL) is dropped, not reused.
            "rememberPreview({goal:'g4',preview:{id:'p4'},approval:{approved:false}});",
            "state.preview.mintedAt = Date.now() - 6 * 60 * 1000;",
            "check('expired-preview-not-reused', previewFor('g4'), '');",
            "console.error(failed ? failed + ' case(s) failed' : 'all preview-token cases passed');",
            "process.exit(failed ? 1 : 0);",
        ])
        # The checks must live in the SAME eval as state.js: its `state` binding
        # is scoped to that eval text, not the enclosing node -e module.
        script = (
            "const fs = require('fs');"
            "const src = fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8');"
            "eval(src + '\\n' + " + repr(checks) + ");"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("all preview-token cases passed", proc.stderr)


class ActivityTimelineDomTests(unittest.TestCase):
    """DOM-level pins for the server-fed Recent Activity timeline (activity.js).

    The feed used to be localStorage-written by whichever tab initiated the
    action, so it missed CLI/GUI/second-tab activity. It is now seeded from
    /activity and appended live from <family>.completed SSE events. These
    tests drive the REAL activity.js in Node (like ExtractAnswerTextTests)
    against a minimal DOM shim: completions render newest-first, events from
    any family map to the right pill, unknown families are ignored, and the
    5-second de-dupe window suppresses a seed/stream double-append.
    """

    NODE = shutil.which("node")
    STATIC = Path("src/novacontrol/web/static")

    def _run_activity(self, driver: str) -> dict:
        if self.NODE is None:
            self.skipTest("node is not installed")
        scripts = "\n".join(
            (self.STATIC / name).read_text(encoding="utf-8")
            for name in ("js/dom.js", "js/state.js", "js/render-utils.js", "js/activity.js")
        )
        shim = """
function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], _text: "", className: "", style: {},
    _classes() { return this.className ? this.className.split(/\\s+/) : []; },
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
    get firstChild() { return this.children[0] || null; },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      return child;
    },
    addEventListener() {},
    get classList() {
      const self = this;
      return {
        add(...n) { const s = new Set(self._classes()); n.forEach(x => s.add(x)); self.className = [...s].join(" "); },
        remove(...n) { const s = new Set(self._classes()); n.forEach(x => s.delete(x)); self.className = [...s].join(" "); },
        toggle(name, on) { const s = new Set(self._classes()); (on === undefined ? !s.has(name) : on) ? s.add(name) : s.delete(name); self.className = [...s].join(" "); },
        contains(name) { return self._classes().includes(name); },
      };
    },
    get textContent() {
      if (this.children.length === 0) return this._text;
      return this.children.map(c => c.textContent).join("");
    },
    set textContent(value) { this._text = String(value); this.children = []; },
  };
  return node;
}
function flatten(node, out = []) { out.push(node); for (const c of node.children || []) flatten(c, out); return out; }
function qsa(root, selector) {
  const cls = selector.startsWith(".") ? selector.slice(1) : null;
  const tag = cls ? null : selector.toUpperCase();
  return flatten(root).filter(n => n !== root && (cls ? n._classes().includes(cls) : n.tagName === tag));
}
const registry = new Map();
const byIdEl = makeNode("div"); registry.set("homeActivity", byIdEl);
const document = {
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => { const n = makeNode("#text"); n._text = String(text); return n; },
  getElementById: (id) => registry.get(id) || null,
  querySelector: () => null,
};
"""
        script = (
            "const fs = require('fs');"
            + shim
            + "eval(fs.readFileSync('src/novacontrol/web/static/js/dom.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/state.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/render-utils.js','utf8') + '\\n'"
            " + fs.readFileSync('src/novacontrol/web/static/js/activity.js','utf8') + '\\n'"
            " + " + json.dumps(driver) + ");"
        )
        proc = subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_completions_render_newest_first_with_pills(self) -> None:
        driver = """
recordActivityFromEvent({ type: "command.completed", title: "Command executed", detail: "open notepad", at: Date.now() });
recordActivityFromEvent({ type: "explore.completed", title: "Research complete", detail: "black holes", at: Date.now() });
recordActivityFromEvent({ type: "learn.completed", title: "Learning cycle", detail: "improve memory", at: Date.now() });
console.log(JSON.stringify({
  rows: qsa(byId("homeActivity"), ".activity-item").map(r => r.textContent),
  pills: qsa(byId("homeActivity"), ".activity-type").map(p => p.textContent),
}));
"""
        result = self._run_activity(driver)
        self.assertEqual(len(result["rows"]), 3)
        self.assertIn("improve memory", result["rows"][0])  # newest first
        self.assertIn("open notepad", result["rows"][2])
        self.assertEqual(result["pills"], ["Learn", "Research", "Command"])

    def test_unknown_family_and_empty_title_are_ignored(self) -> None:
        driver = """
recordActivityFromEvent({ type: "vision.completed", title: "Nope", detail: "x", at: Date.now() });
recordActivityFromEvent({ type: "command.completed", title: "", detail: "x", at: Date.now() });
recordActivityFromEvent({ type: "command.completed", title: "Command executed", detail: "open calc", at: Date.now() });
console.log(JSON.stringify({ rows: qsa(byId("homeActivity"), ".activity-item").map(r => r.textContent) }));
"""
        result = self._run_activity(driver)
        self.assertEqual(len(result["rows"]), 1)
        self.assertIn("open calc", result["rows"][0])

    def test_dedupe_window_suppresses_seed_and_stream_double(self) -> None:
        now = 1_700_000_000_000
        driver = f"""
// Simulate the real seed: loadActivityFromServer fills the array AND renders.
activityEntries = [{{ type: "command", title: "Command executed", detail: "open steam", at: {now} }}];
renderActivityFeed();
// The completing tab also receives its own SSE event for the same action —
// the 5s de-dupe window must drop it, leaving exactly one row.
recordActivityFromEvent({{ type: "command.completed", title: "Command executed", detail: "open steam", at: {now + 1000} }});
console.log(JSON.stringify({{ rows: qsa(byId("homeActivity"), ".activity-item").map(r => r.textContent) }}));
"""
        result = self._run_activity(driver)
        self.assertEqual(len(result["rows"]), 1)  # duplicate within 5s window dropped


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

    # Route -> renderer that owns the body, or one of the NON_RENDER_ROLES markers
    # (no-render — infrastructure / channel / CLI-only endpoint with no panel;
    #  load-settings — GET /settings fills the settings form, no render).
    # The table is the SHARED registry in novacontrol.api.route_consumers:
    # scripts/generate_api_reference.py regenerates docs/API.md from the same
    # rows, so the test, the docs, and the wire contract cannot drift apart.
    ROUTE_CONSUMERS = ROUTE_CONSUMER_TABLE
    ALLOWED_NON_RENDER = set(NON_RENDER_ROLES)

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


class BackendIntentRendererContractTests(unittest.TestCase):
    """Every intent route the brain can emit has a JS renderer that consumes the
    flat {route, intent, summary, data} envelope, generated from the dispatch
    table so future routes fail loudly.

    The backend emits two shapes of response:

      * Flat handler payloads for non-/ask routes, rendered through run(type) in
        render-utils.js, where type is the handler's route string.
      * The /ask envelope {route, intent, summary, data}, where route is the
        handler route and intent is the decision.intent value, rendered by
        renderChatResult (the single chat renderer) which branch-guards on the
        report/command/general paths.

    The BEST_VERSION dispatch table below is generated from the backend handlers
    in application.py (_HANDLERS) plus the flat /plan, /improve/*, /learn/*,
    /system/health, /settings endpoints and the generic fallback routes. Any
    BrainIntent value is a possible /ask intent, so the intent map is generated
    from BrainIntent and asserted to cover every value.

    If a new intent is added to BrainIntent, or a new handler route is emitted,
    this contract must be extended before the change lands — adding a route
    without a renderer is a contract violation, not a silent miss.
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

    # Flat route -> (run type, renderer function). Generated from the backend
    # handler table: each handler route == the run type, and each run type is
    # asserted to map to a real renderer in render() below.
    FLAT_DISPATCH: dict[str, str] = {
        "chat": "renderChatResult",
        "explore": "renderExplore",
        "command": "renderCommand",
        "build": "renderBuild",
        "workflow": "renderWorkflow",
        "learn": "renderLearning",
        "health": "renderHealth",
        "settings": "renderSettingsResult",
    }

    # /ask envelope intent -> branch path inside renderChatResult. Generated from
    # the three real branches the chat renderer owns:
    #   report  -> tryRenderResearch(stream, payload)
    #   command -> renderCommand(stream, payload)
    #   general -> renderGeneralAiPage(stream, { query, summary, route, payload })
    # Every BrainIntent.value must map here; if a new intent would not clearly
    # own report or command, it lands on general.
    INTENT_BRANCH: dict[str, str] = {
        "explore": "report",
        "desktop_automation": "command",
        "browser_automation": "command",
        "phone_control": "command",
        "chat": "general",
        "plan": "general",
        "self_improvement": "general",
        "agent": "general",
        "memory": "general",
        "project": "general",
        "clarify": "general",
    }
    ALLOWED_BRANCHES = frozenset({"report", "command", "general"})

    # /ask routes the envelope can carry, transcribed from application.py's
    # handlers (route strings differ from intent values: PLAN -> "planning",
    # CLARIFY -> "brain", AGENT falls through to _handle_agent). Each route
    # must land on a documented renderChatResult branch, so a new handler route
    # without a render path fails here.
    HANDLER_ROUTES: dict[str, str] = {
        "brain": "general",
        "chat": "general",
        "explore": "report",
        "planning": "general",
        "self_improvement": "general",
        "desktop_automation": "command",
        "phone_control": "command",
        "browser_automation": "command",
        "memory": "general",
        "project": "general",
        "agent": "general",
    }

    # Intent -> the route its handler emits (mirrors _HANDLERS + the AGENT
    # fallback). Generated from BrainIntent so a new intent fails until routed.
    INTENT_TO_ROUTE: dict[str, str] = {
        "chat": "chat",
        "explore": "explore",
        "plan": "planning",
        "agent": "agent",
        "self_improvement": "self_improvement",
        "desktop_automation": "desktop_automation",
        "phone_control": "phone_control",
        "browser_automation": "browser_automation",
        "memory": "memory",
        "project": "project",
        "clarify": "brain",
    }

    def test_every_envelope_route_has_a_render_branch(self) -> None:
        """Every handler route the /ask envelope can carry maps to a branch."""
        self.assertEqual(
            set(self.INTENT_TO_ROUTE),
            {intent.value for intent in BrainIntent},
            "every BrainIntent needs an intent->route mapping",
        )
        self.assertEqual(
            set(self.INTENT_TO_ROUTE.values()),
            set(self.HANDLER_ROUTES),
            "intent->route table and route->branch table must cover the same routes",
        )
        self.assertEqual(
            set(self.HANDLER_ROUTES.values()),
            {"general", "report", "command"},
            "every handler route must land on one of the three real branches",
        )
        for route, branch in self.HANDLER_ROUTES.items():
            with self.subTest(route=route):
                if branch == "report":
                    self.assertIn("tryRenderResearch(stream, payload)", self.render_panels)
                elif branch == "command":
                    self.assertIn("renderCommand(stream, payload)", self.render_panels)
                else:
                    self.assertIn("renderGeneralAiPage(stream", self.render_panels)

    def test_every_run_type_is_a_real_branch_in_render(self) -> None:
        """Each run(type) is an explicit branch in render() and maps to its renderer."""
        for run_type, renderer in self.FLAT_DISPATCH.items():
            with self.subTest(run_type=run_type):
                self.assertIn(f'type === "{run_type}"', self.render_utils, f"run type {run_type} has no render branch")
                self.assertIn(f"function {renderer}(", self.js, f"renderer {renderer} missing for run type {run_type}")

    def test_generic_fallback_is_the_final_else_branch(self) -> None:
        """The generic fallback renders through renderGeneric as the else case."""
        self.assertIn("else renderGeneric(", self.render_utils)
        self.assertIn("function renderGeneric(", self.js)

    def test_every_brain_intent_has_an_envelope_branch(self) -> None:
        """Every BrainIntent value the /ask envelope can carry maps to a real branch."""
        intents = {intent.value for intent in BrainIntent}
        self.assertEqual(intents, set(self.INTENT_BRANCH), "add new BrainIntent values to INTENT_BRANCH")
        self.assertTrue(set(self.INTENT_BRANCH.values()) <= self.ALLOWED_BRANCHES)

        # Each intent's route lands on a branch that exists in renderChatResult.
        for intent in BrainIntent:
            route = self.INTENT_TO_ROUTE[intent.value]
            branch = self.HANDLER_ROUTES[route]
            with self.subTest(intent=intent.value, route=route, branch=branch):
                if branch == "report":
                    self.assertIn("tryRenderResearch(stream, payload)", self.render_panels)
                elif branch == "command":
                    self.assertIn("renderCommand(stream, payload)", self.render_panels)
                else:
                    self.assertIn("renderGeneralAiPage(stream", self.render_panels)


if __name__ == "__main__":
    unittest.main()
