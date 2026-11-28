"""Extra 2 — the Atrium, pinned against the real module.

``AtriumRuntimeTests`` runs the actual ``static/js/isometric.js`` in Node
against a minimal DOM shim and drives it the way the page does: building the
scene, clicking every object, and reading the same ``/status`` and
``/novaforge/status`` payload shapes the server really returns. It pins the
properties that make the map a workspace rather than an illustration:

* all eleven objects are real buttons, each routing to its real panel through
  the sidebar's own nav element;
* every badge is derived from a reported field, and an absent field says
  "unavailable" instead of inventing a number;
* a failed status read marks live objects as unavailable and leaves unmetered
  objects saying what they open (never a false "ok");
* figures mount once, clean themselves up, and a failure leaves navigation
  intact;
* the status poll stops when the Atrium is left and pauses while hidden.

``AtriumAssetTests`` pins the served page and the vendored Hairline bundle:
the panel, every object target, the module URL marker, the figures actually
exported, and the MIT licence that makes the reuse lawful.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path("src/novacontrol/web/static")
MODULE = STATIC / "js" / "isometric.js"
VENDOR = STATIC / "vendor" / "hairline"
MID = "\N{MIDDLE DOT}"

# The DOM shim + driver, evaluated in ONE scope with isometric.js so the
# module's top-level functions and the shim share bindings. Everything the
# module touches is provided; nothing it does reaches the network.
_HARNESS = r"""
const fs = require('fs');

const els = new Map();
const docListeners = [];
const navClicks = {};
const toasts = [];
const fetchCalls = [];
const failures = new Set();
const intervals = { created: 0, cleared: 0, last: null };
const routes = {
  '/status': {
    app: {
      runtime_started: true,
      modules: ['core', 'brain'],
      tracked_tasks: 4,
      brain: { mode: 'scratch', effective_mode: 'scratch', model_configured: false, model: '', provider: 'scratch' },
      phone_bridge: { available: false, state: 'no_device', devices: [] },
      vision_pipeline: { provider: { available: false }, ocr: { available: true } },
    },
  },
  '/novaforge/status': { status: { active_session: null, capture_supported: true } },
};

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    attrs: {},
    dataset: {},
    handlers: {},
    classList: {
      _s: new Set(),
      add(...names) { names.forEach((n) => this._s.add(n)); },
      remove(...names) { names.forEach((n) => this._s.delete(n)); },
      contains(name) { return this._s.has(name); },
    },
    style: {
      _p: {},
      setProperty(name, value) { this._p[name] = String(value); },
    },
    textContent: '',
    title: '',
    type: '',
    value: '',
    clicks: 0,
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name]; },
    addEventListener(event, fn) { (this.handlers[event] = this.handlers[event] || []).push(fn); },
    removeEventListener(event, fn) {
      const list = this.handlers[event] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    click() {
      this.clicks += 1;
      (this.handlers.click || []).forEach((fn) => fn({ target: this }));
    },
  };
  return el;
}

function byId(id) {
  if (!els.has(id)) els.set(id, makeEl(id.endsWith('Panel') ? 'section' : 'div'));
  return els.get(id);
}

function fireDocument(event) {
  docListeners.filter((l) => l.event === event.type).forEach((l) => l.fn(event));
}

const PANELS = [
  'homePanel', 'atriumPanel', 'chatPanel', 'explorePanel', 'routingPanel',
  'jarvisPanel', 'visionPanel', 'novaforgePanel', 'buildPanel', 'learnPanel',
  'systemPanel', 'settingsPanel', 'cliPanel',
];
PANELS.forEach((panel) => byId(panel));
byId('homePanel').classList.add('active');

const navs = new Map();
PANELS.forEach((panel) => {
  const nav = makeEl('button');
  nav.dataset.panel = panel;
  nav.closest = (selector) => (selector === '.nav-item' ? nav : null);
  nav.click = () => {
    navClicks[panel] = (navClicks[panel] || 0) + 1;
    PANELS.forEach((id) => byId(id).classList.remove('active'));
    byId(panel).classList.add('active');
    fireDocument({ type: 'click', target: nav });
  };
  navs.set(panel, nav);
});

const scene = byId('isoScene');
byId('isoStatusSource');
byId('isoArtStatus');
byId('isoRefreshButton');
byId('tokenInput');

function figures() {
  const out = [];
  scene.children.forEach((child) => {
    (child.children || []).forEach((node) => {
      if (node.dataset && node.dataset.isoOwner) out.push(node);
    });
  });
  return out;
}

function findButton(key) {
  return scene.children.find((node) => node.dataset && node.dataset.isoKey === key) || null;
}

function findById(node, id) {
  if (node.id === id) return node;
  for (const child of node.children || []) {
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

const document = {
  hidden: false,
  getElementById: (id) => (els.has(id) ? els.get(id) : findById(scene, id)),
  createElement: (tag) => makeEl(tag),
  createElementNS: (_ns, tag) => makeEl(tag),
  addEventListener: (event, fn) => { docListeners.push({ event, fn }); },
  removeEventListener: (event, fn) => {
    const i = docListeners.findIndex((l) => l.event === event && l.fn === fn);
    if (i >= 0) docListeners.splice(i, 1);
  },
  querySelector(selector) {
    const sel = String(selector);
    let match = sel.match(/^\.nav-item\[data-panel="(.+)"\]$/);
    if (match) return navs.get(match[1]) || null;
    match = sel.match(/^\[data-iso-owner="(.+)"\]$/);
    if (match) return figures().find((node) => node.dataset.isoOwner === match[1]) || null;
    return null;
  },
  querySelectorAll(selector) {
    return String(selector) === '[data-iso-owner]' ? figures() : [];
  },
};

const window = {};

function showToast(message) { toasts.push(String(message)); }

const setInterval = (fn) => { intervals.created += 1; intervals.last = fn; return intervals.created; };
const clearInterval = () => { intervals.cleared += 1; intervals.last = null; };

async function fetch(path, options = {}) {
  fetchCalls.push({ path: String(path), headers: options.headers || {} });
  if (failures.has(path)) throw new Error(`${path} blocked`);
  return { ok: true, status: 200, json: async () => routes[path] };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  eval(fs.readFileSync('src/novacontrol/web/static/js/isometric.js', 'utf8'));

  const A = window.NovaAtrium;
  const out = { modulePresent: Boolean(A) };
  if (!A) { console.log(JSON.stringify(out)); return; }

  const byKey = Object.fromEntries(A.objects.map((object) => [object.key, object]));
  const setup = A.setup();
  out.setup = {
    returnedPanel: setup === byId('atriumPanel'),
    active: A.isActive(),
    built: A.debug().built,
    objects: A.objects.length,
    buttons: scene.children.filter((node) => node.dataset && node.dataset.isoKey).length,
    links: (scene.children.find((node) => node.tagName === 'SVG') || { children: [] }).children.length,
  };

  // Every object is a real button whose action is the sidebar nav click.
  out.routes = [];
  A.objects.forEach((object) => {
    const button = findButton(object.key);
    const targetPanel = byId(object.target);
    targetPanel.classList.remove('active');
    button.click();
    out.routes.push({
      key: object.key,
      target: object.target,
      tag: button.tagName,
      ariaLabel: button.attrs['aria-label'],
      buttonChildren: button.children.length,
      panelActive: targetPanel.classList.contains('active'),
      navClicks: navClicks[object.target] || 0,
      activeAfter: A.isActive(),
    });
  });
  out.unknown = { result: A.navigate('notAPanel'), toasts: toasts.slice() };

  // Live badges: only reported fields become text.
  const badge = (key, status, forge, options = {}) => A.badge(byKey[key], status, forge, options);
  const realStatus = routes['/status'];
  out.badges = {
    core: badge('core', realStatus),
    coreStopped: badge('core', { app: { runtime_started: false, modules: [] } }),
    coreMissing: badge('core', { app: {} }),
    chatScratch: badge('chat', realStatus),
    chatLlm: badge('chat', { app: { brain: { effective_mode: 'llm', model_configured: true } } }),
    jarvisNone: badge('jarvis', realStatus),
    jarvisTwo: badge('jarvis', { app: { phone_bridge: { available: true, state: 'paired', devices: [{ id: 'a' }, { id: 'b' }] } } }),
    visionOcr: badge('vision', realStatus),
    visionNone: badge('vision', { app: { vision_pipeline: { provider: { available: false }, ocr: { available: false } } } }),
    forgeSession: badge('novaforge', null, { status: { active_session: 'session-1', capture_supported: true } }),
    forgeCapture: badge('novaforge', null, { status: { active_session: null, capture_supported: true } }),
    forgeNoCapture: badge('novaforge', null, { status: { active_session: null, capture_supported: false } }),
    forgeDown: badge('novaforge', null, null, { novaforgeError: 'blocked' }),
    buildScaffold: badge('build', realStatus),
    buildModel: badge('build', { app: { brain: { model_configured: true, model: 'qwen2.5', provider: 'ollama' } } }),
    system: badge('system', realStatus),
    systemMissing: badge('system', { app: { tracked_tasks: 2 } }),
    explore: badge('explore', realStatus),
    errorLive: badge('core', null, null, { error: 'blocked' }),
    errorIdle: badge('learn', null, null, { error: 'blocked' }),
  };

  // Render those badges into the real scene nodes.
  A.renderStatus(realStatus, routes['/novaforge/status']);
  out.rendered = {};
  A.objects.forEach((object) => {
    const node = document.getElementById(`isoState-${object.key}`);
    out.rendered[object.key] = { text: node.textContent, tone: node.dataset.tone, live: node.dataset.live };
  });

  // A live refresh reads both endpoints and forwards the bearer token.
  byId('tokenInput').value = 'tok-extra2';
  fetchCalls.length = 0;
  await A.refresh();
  out.refresh = {
    calls: fetchCalls.map((call) => call.path),
    auth: fetchCalls.map((call) => call.headers.Authorization || ''),
    source: byId('isoStatusSource').textContent,
    sourceClass: byId('isoStatusSource').className,
    core: document.getElementById('isoState-core').textContent,
    novaforge: document.getElementById('isoState-novaforge').textContent,
  };

  // NovaForge unavailable must not take down the rest of the map.
  failures.add('/novaforge/status');
  await A.refresh();
  out.forgeFailure = {
    core: document.getElementById('isoState-core').textContent,
    novaforge: document.getElementById('isoState-novaforge').textContent,
    tone: document.getElementById('isoState-novaforge').dataset.tone,
    sourceClass: byId('isoStatusSource').className,
  };
  failures.delete('/novaforge/status');

  // A total status failure: live objects say unavailable, unmetered ones still
  // say what they open, and nothing pretends to be healthy.
  failures.add('/status');
  await A.refresh();
  out.statusFailure = {
    core: document.getElementById('isoState-core').textContent,
    tone: document.getElementById('isoState-core').dataset.tone,
    live: document.getElementById('isoState-core').dataset.live,
    learn: document.getElementById('isoState-learn').textContent,
    learnLive: document.getElementById('isoState-learn').dataset.live,
    source: byId('isoStatusSource').textContent,
    sourceClass: byId('isoStatusSource').className,
  };
  failures.delete('/status');

  // Activation starts one poll; the art import has no browser in Node, so the
  // honest fallback must appear without breaking the map.
  out.activation = { before: A.debug() };
  A.activate();
  await sleep(120);
  out.activation.after = A.debug();
  out.activation.polls = { created: intervals.created, cleared: intervals.cleared };
  out.activation.artStatus = byId('isoArtStatus').textContent;

  // A fake module lets the mount/unmount seam run without a browser.
  const mountedCalls = [];
  const destroyed = [];
  const fake = {};
  A.objects.forEach((object) => {
    fake[object.figure] = (container, options) => {
      mountedCalls.push({ owner: container.dataset.isoOwner, options });
      return { destroy: () => destroyed.push(object.figure) };
    };
  });
  const mounted = A.mountArt(fake);
  const mountedAgain = A.mountArt(fake);
  out.art = {
    mounted,
    mountedAgain,
    factoryCalls: mountedCalls.length,
    owners: mountedCalls.map((call) => call.owner).sort(),
    theme: mountedCalls[0] && mountedCalls[0].options.theme,
    status: byId('isoArtStatus').textContent,
    state: A.debug().artState,
    figureMarkers: figures().filter((node) => node.dataset.art === 'mounted').length,
  };

  // Poll pause: hidden tabs do not fetch, becoming visible refreshes again.
  document.hidden = true;
  fetchCalls.length = 0;
  await A.refresh();
  out.hidden = { calls: fetchCalls.length };
  document.hidden = false;
  fetchCalls.length = 0;
  fireDocument({ type: 'visibilitychange' });
  await sleep(60);
  out.visible = { calls: fetchCalls.length };

  out.deactivated = { ok: A.deactivate(), debug: A.debug(), destroyed: destroyed.length, polls: { created: intervals.created, cleared: intervals.cleared } };

  // Leaving removes every mounted figure (the earlier unmount already did, so
  // mount again and take the object's own cleanup path).
  A.mountArt(fake);
  const beforeUnmount = figures().filter((node) => node.dataset.art === 'mounted').length;
  const removed = A.unmountArt();
  out.unmount = {
    removed,
    beforeUnmount,
    afterMarkers: figures().filter((node) => node.dataset.art).length,
    state: A.debug().artState,
  };

  // A partial module is reported as partial; a module that throws is a failed
  // figure, never a crash; no module is stated plainly.
  const partial = { hub: fake.hub };
  out.partial = { mounted: A.mountArt(partial), status: byId('isoArtStatus').textContent };
  A.unmountArt();
  const thrower = {
    hub: fake.hub,
    query: () => { throw new Error('bad figure'); },
  };
  out.thrower = { mounted: A.mountArt(thrower), status: byId('isoArtStatus').textContent };
  A.unmountArt();
  out.none = { mounted: A.mountArt({}), status: byId('isoArtStatus').textContent, state: A.debug().artState };

  // Setup is idempotent: a second call cannot stack listeners.
  const listenerCounts = () => ({
    click: docListeners.filter((l) => l.event === 'click').length,
    visibility: docListeners.filter((l) => l.event === 'visibilitychange').length,
    refresh: (byId('isoRefreshButton').handlers.click || []).length,
  });
  const beforeSetupAgain = listenerCounts();
  A.setup();
  out.listeners = { before: beforeSetupAgain, after: listenerCounts() };

  // Restoring the saved panel opens the Atrium without another click.
  byId('atriumPanel').classList.add('active');
  A.setup();
  out.restore = { active: A.isActive(), built: A.debug().built };
  A.deactivate();

  out.debug = A.debug();
  console.log(JSON.stringify(out));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""


class AtriumRuntimeTests(unittest.TestCase):
    """Drive the real isometric.js through the page's own seams."""

    NODE = shutil.which("node")

    @classmethod
    def setUpClass(cls) -> None:
        if cls.NODE is None:
            raise unittest.SkipTest("node is not installed")
        proc = subprocess.run(
            [cls.NODE, "-e", _HARNESS],
            capture_output=True,
            timeout=120,
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise AssertionError(f"atrium harness failed:\n{stderr}")
        # Node writes UTF-8; decoding with the Windows locale would turn the
        # middle dot into two mojibake characters and fail every badge compare.
        cls.raw = proc.stdout.decode("utf-8", "replace")

    def _out(self) -> dict:
        try:
            return json.loads(self.raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"harness produced no JSON: {self.raw!r}") from exc

    def test_module_is_exposed_and_builds_one_scene(self) -> None:
        out = self._out()
        self.assertTrue(out.get("modulePresent"), "isometric.js did not publish window.NovaAtrium")
        setup = out["setup"]
        self.assertTrue(setup["returnedPanel"])
        self.assertTrue(setup["built"])
        self.assertEqual(setup["objects"], 11)
        self.assertEqual(setup["buttons"], 11)
        self.assertEqual(setup["links"], 10, "the hub must connect to the other ten objects")

    def test_every_object_is_a_button_routing_to_its_real_panel(self) -> None:
        routes = self._out()["routes"]
        self.assertEqual(len(routes), 11)
        for route in routes:
            with self.subTest(object=route["key"]):
                self.assertEqual(route["tag"], "BUTTON")
                self.assertIn("Open ", route["ariaLabel"])
                self.assertEqual(route["buttonChildren"], 3, "figure, label and state must all render")
                self.assertTrue(route["panelActive"], f"{route['key']} did not open {route['target']}")
                self.assertEqual(route["navClicks"], 1, "the object did not use the sidebar nav click")

    def test_an_unknown_target_is_refused_with_feedback(self) -> None:
        unknown = self._out()["unknown"]
        self.assertFalse(unknown["result"])
        self.assertEqual(len(unknown["toasts"]), 1)
        self.assertIn("not available", unknown["toasts"][0])

    def test_every_badge_comes_from_a_reported_field(self) -> None:
        badges = self._out()["badges"]
        self.assertEqual(badges["core"]["text"], f"runtime online {MID} 2 modules")
        self.assertEqual(badges["core"]["tone"], "ok")
        self.assertEqual(badges["coreStopped"]["text"], "runtime stopped")
        self.assertEqual(badges["coreStopped"]["tone"], "warn")
        self.assertEqual(badges["coreMissing"]["text"], "runtime state unavailable")
        self.assertEqual(badges["chatScratch"]["text"], f"scratch brain {MID} no model")
        self.assertEqual(badges["chatScratch"]["tone"], "ok")
        self.assertEqual(badges["chatLlm"]["text"], "llm brain")
        self.assertEqual(badges["jarvisNone"]["text"], "no device paired")
        self.assertEqual(badges["jarvisNone"]["tone"], "warn")
        self.assertEqual(badges["jarvisTwo"]["text"], f"paired {MID} 2 devices")
        self.assertEqual(badges["visionOcr"]["text"], "OCR only")
        self.assertEqual(badges["visionOcr"]["tone"], "warn")
        self.assertEqual(badges["visionNone"]["text"], "vision unavailable")
        self.assertEqual(badges["forgeSession"]["text"], "mission running")
        self.assertEqual(badges["forgeCapture"]["text"], f"ready {MID} capture")
        self.assertEqual(badges["forgeNoCapture"]["text"], "capture unavailable")
        self.assertEqual(badges["forgeDown"]["text"], "status unavailable")
        self.assertEqual(badges["forgeDown"]["tone"], "error")
        self.assertEqual(badges["buildScaffold"]["text"], "scaffold only")
        self.assertEqual(badges["buildScaffold"]["tone"], "warn")
        self.assertEqual(badges["buildModel"]["text"], f"coding model {MID} qwen2.5")
        self.assertEqual(badges["system"]["text"], f"2 modules {MID} 4 tasks")
        self.assertEqual(badges["systemMissing"]["text"], "state unavailable")
        self.assertEqual(badges["explore"]["text"], "research desk")
        self.assertFalse(badges["explore"]["live"], "an unmetered object must not claim a live badge")
        self.assertEqual(badges["errorLive"]["text"], "status unavailable")
        self.assertEqual(badges["errorLive"]["tone"], "error")
        self.assertEqual(badges["errorIdle"]["text"], "knowledge & training")
        self.assertFalse(badges["errorIdle"]["live"])

    def test_render_status_reaches_each_scene_node(self) -> None:
        rendered = self._out()["rendered"]
        self.assertEqual(rendered["core"]["text"], f"runtime online {MID} 2 modules")
        self.assertEqual(rendered["core"]["tone"], "ok")
        self.assertEqual(rendered["system"]["text"], f"2 modules {MID} 4 tasks")
        self.assertEqual(rendered["system"]["live"], "true")
        self.assertEqual(rendered["explore"]["text"], "research desk")
        self.assertEqual(rendered["explore"]["live"], "false")

    def test_refresh_reads_both_endpoints_with_the_page_token(self) -> None:
        refresh = self._out()["refresh"]
        self.assertEqual(refresh["calls"], ["/status", "/novaforge/status"])
        self.assertEqual(refresh["auth"], ["Bearer tok-extra2", "Bearer tok-extra2"])
        self.assertTrue(refresh["source"].startswith("live "), refresh["source"])
        self.assertIn(MID, refresh["source"])
        self.assertEqual(refresh["sourceClass"], "pill ok")
        self.assertEqual(refresh["novaforge"], f"ready {MID} capture")

    def test_a_failed_novaforge_read_does_not_hide_the_rest(self) -> None:
        failure = self._out()["forgeFailure"]
        self.assertEqual(failure["core"], f"runtime online {MID} 2 modules")
        self.assertEqual(failure["novaforge"], "status unavailable")
        self.assertEqual(failure["tone"], "error")
        self.assertEqual(failure["sourceClass"], "pill warn")

    def test_a_total_status_failure_is_stated_and_never_faked(self) -> None:
        failure = self._out()["statusFailure"]
        self.assertEqual(failure["core"], "status unavailable")
        self.assertEqual(failure["tone"], "error")
        self.assertEqual(failure["live"], "true")
        self.assertEqual(failure["learn"], "knowledge & training")
        self.assertEqual(failure["learnLive"], "false")
        self.assertIn("status unavailable", failure["source"])
        self.assertIn("navigation still works", failure["source"])
        self.assertEqual(failure["sourceClass"], "pill error")

    def test_activation_polls_and_a_failed_art_import_leaves_navigation(self) -> None:
        activation = self._out()["activation"]
        self.assertFalse(activation["before"]["active"])
        self.assertTrue(activation["after"]["active"])
        self.assertTrue(activation["after"]["polling"])
        self.assertEqual(activation["polls"]["created"], 1)
        self.assertEqual(activation["after"]["artState"], "failed")
        self.assertIn("map still navigates", activation["artStatus"])

    def test_figures_mount_once_and_clean_up(self) -> None:
        art = self._out()["art"]
        self.assertEqual(art["mounted"], 11)
        self.assertEqual(art["mountedAgain"], 11)
        self.assertEqual(art["factoryCalls"], 11, "a second mount created duplicate figures")
        self.assertEqual(len(art["owners"]), 11)
        self.assertEqual(art["theme"], "dark")
        self.assertEqual(art["status"], "figures live")
        self.assertEqual(art["state"], "ready")
        self.assertEqual(art["figureMarkers"], 11)
        unmount = self._out()["unmount"]
        self.assertEqual(unmount["removed"], 11)
        self.assertEqual(unmount["beforeUnmount"], 11)
        self.assertEqual(unmount["afterMarkers"], 0, "unmount left figure markers behind")
        self.assertEqual(unmount["state"], "idle")

    def test_a_partial_or_broken_module_is_reported_honestly(self) -> None:
        out = self._out()
        self.assertEqual(out["partial"]["mounted"], 1)
        self.assertEqual(out["partial"]["status"], "1 of 11 figures live")
        self.assertEqual(out["thrower"]["mounted"], 1, "a throwing figure took the other figure down")
        self.assertIn("figures unavailable", out["none"]["status"])
        self.assertEqual(out["none"]["state"], "failed")

    def test_hidden_tabs_do_not_poll_and_becoming_visible_refreshes(self) -> None:
        out = self._out()
        self.assertEqual(out["hidden"]["calls"], 0, "a hidden tab kept fetching status")
        self.assertEqual(out["visible"]["calls"], 2, "becoming visible did not refresh")

    def test_leaving_stops_the_poll_and_removes_figures(self) -> None:
        deactivated = self._out()["deactivated"]
        self.assertTrue(deactivated["ok"])
        self.assertFalse(deactivated["debug"]["active"])
        self.assertFalse(deactivated["debug"]["polling"])
        self.assertEqual(deactivated["polls"]["cleared"], 1)
        self.assertEqual(deactivated["destroyed"], 11)

    def test_setup_is_idempotent_and_restores_the_saved_panel(self) -> None:
        out = self._out()
        self.assertEqual(out["listeners"]["before"], {"click": 1, "visibility": 1, "refresh": 1})
        self.assertEqual(out["listeners"]["after"], {"click": 1, "visibility": 1, "refresh": 1})
        self.assertTrue(out["restore"]["active"], "a restored Atrium panel did not activate")
        self.assertTrue(out["restore"]["built"])


class AtriumAssetTests(unittest.TestCase):
    """The page, the module and the vendored figures must agree."""

    def setUp(self) -> None:
        self.index = (STATIC / "index.html").read_text(encoding="utf-8")
        self.module = MODULE.read_text(encoding="utf-8")
        self.vendor = (VENDOR / "index.js").read_text(encoding="utf-8")

    def _rows(self) -> list[tuple[str, str, str, str, bool]]:
        pattern = re.compile(
            r'\{ key: "([a-z]+)", label: "([^"]+)", figure: "([a-z]+)", target: "([A-Za-z]+)", '
            r'.*?live: (true|false) \}',
            re.DOTALL,
        )
        return [(key, label, figure, target, live == "true") for key, label, figure, target, live in pattern.findall(self.module)]

    def test_index_ships_the_atrium_panel_nav_and_module(self) -> None:
        self.assertIn('id="atriumPanel"', self.index)
        self.assertIn('data-panel="atriumPanel"', self.index)
        self.assertIn('id="isoScene"', self.index)
        self.assertIn('id="isoArtStatus"', self.index)
        self.assertIn('id="isoStatusSource"', self.index)
        self.assertIn('id="isoRefreshButton"', self.index)
        self.assertIn("/static/js/isometric.js?v=__NC_ASSET_VERSION__", self.index)

    def test_every_object_target_is_a_real_workspace(self) -> None:
        rows = self._rows()
        self.assertEqual(len(rows), 11, f"the object table changed shape: {rows}")
        targets = [target for _, _, _, target, _ in rows]
        self.assertEqual(len(set(targets)), 11, "two objects open the same workspace")
        for key, label, _figure, target, _live in rows:
            with self.subTest(object=key):
                self.assertIn(f'id="{target}"', self.index, f"{key} opens a panel the page does not define")
                self.assertIn(f'data-panel="{target}"', self.index, f"{key} opens a panel with no nav item")
                self.assertTrue(label, f"{key} has no accessible label")

    def test_every_figure_used_is_exported_by_the_vendored_bundle(self) -> None:
        rows = self._rows()
        block = self.vendor[self.vendor.rindex("export {"):]
        exported = set(re.findall(r"^\s*([a-z]+),?$", block, re.MULTILINE))
        figures = {figure for _, _, figure, _, _ in rows}
        self.assertEqual(len(figures), 11)
        self.assertEqual(sorted(figures - exported), [], "the map names a figure Hairline does not export")

    def test_hairline_is_vendored_under_mit_with_attribution(self) -> None:
        self.assertIn("MIT License", (VENDOR / "LICENSE").read_text(encoding="utf-8"))
        attribution = (VENDOR / "ATTRIBUTION.md").read_text(encoding="utf-8")
        self.assertIn("lucasmarkes/hairline", attribution)
        self.assertIn("MIT", attribution)
        self.assertIn("0.5.1", attribution)
        self.assertIn("Lucas Marques", attribution)
        self.assertNotIn("sourceMappingURL", self.vendor)
        self.assertNotRegex(self.vendor, r"https?://(?!www\.w3\.org)")

    def test_figures_are_lazy_and_the_failure_path_keeps_navigation(self) -> None:
        self.assertIn('const HAIRLINE_MODULE_URL = "/static/vendor/hairline/index.js"', self.module)
        self.assertNotRegex(self.module, r"^\s*import\s", "the 214 KB bundle must not load with the page")
        self.assertIn("await isoLoadHairline()", self.module)
        self.assertIn("map still navigates", self.module)
        self.assertIn("?retry=", self.module, "a cached failed module import would never recover")

    def test_the_application_initializes_the_atrium_before_restoring_the_panel(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("setupAtrium();", app)
        self.assertLess(app.index("setupAtrium();"), app.index("restoreActivePanel();"))

    def test_the_map_is_keyboard_reachable_and_declares_its_group(self) -> None:
        self.assertIn('role="group"', self.index)
        self.assertIn('aria-label="Interactive NovaControl workspace map"', self.index)
        self.assertIn("button.setAttribute(\"aria-label\", `Open ${object.label}`);", self.module)
        self.assertIn("button.addEventListener(\"click\", () => isoNavigate(object.target));", self.module)


if __name__ == "__main__":
    unittest.main()
