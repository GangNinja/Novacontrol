"""Extra 1 — the animated companion, pinned against the real module.

``CompanionRuntimeTests`` runs the actual ``static/js/companion.js`` in Node
against a minimal DOM shim and drives it the way the application does: through
``run()``'s start/result/error hooks, the live activity frames, and the voice
callbacks. It pins the properties that make the companion honest — every state
comes from an event we feed it, a failure is not silently cleared, pausing
stops the animation loop without freezing the state, and an Avatar Lab
definition is either accepted with its fields applied or refused with a reason.

``CompanionAssetTests`` pins the wiring in the served page: the stage, every id
the module touches, and the module itself are all present, so a rename cannot
leave a companion that renders but never moves.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path("src/novacontrol/web/static")

# The DOM shim + driver, evaluated in ONE scope with companion.js so `const`
# shim bindings and the module's own top-level functions share it. Everything
# the module touches is provided; nothing it does reaches outside this harness.
_HARNESS = r"""
const fs = require('fs');
const els = new Map();
let rafActive = 0;
let rafCancels = 0;
let nextRaf = 1;
const listeners = [];

function makeEl(id) {
  return {
    id,
    tagName: 'DIV',
    attrs: {},
    dataset: {},
    style: {},
    textContent: '',
    title: '',
    value: '',
    files: [],
    focused: false,
    handlers: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(n, v) { this.attrs[n] = String(v); },
    getAttribute(n) { return this.attrs[n]; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    removeEventListener(ev, fn) {
      const list = this.handlers[ev] || [];
      const i = list.indexOf(fn);
      if (i >= 0) list.splice(i, 1);
    },
    clicks: 0,
    click() { this.clicks += 1; (this.handlers.click || []).forEach((fn) => fn({})); },
    focus() { this.focused = true; },
  };
}

function byId(id) {
  if (!els.has(id)) els.set(id, makeEl(id));
  return els.get(id);
}

['companionStage', 'companionFace', 'companionBody', 'companionHead',
 'companionEyeLeft', 'companionEyeRight', 'companionBrowLeft',
 'companionBrowRight', 'companionMouth', 'companionName', 'companionState',
 'companionDetail', 'companionPauseButton', 'companionSpeakButton',
 'companionAvatarButton', 'companionAvatarInput', 'chatInput'].forEach(byId);

const chatNav = makeEl('chatNav');

const document = {
  hidden: false,
  getElementById: (id) => (els.has(id) ? els.get(id) : null),
  querySelector: (sel) => (String(sel).indexOf('chatPanel') >= 0 ? chatNav : null),
  createElement: (tag) => makeEl(tag),
  addEventListener: (ev, fn) => { listeners.push({ event: ev, fn }); },
  removeEventListener: (ev, fn) => {
    const i = listeners.findIndex((l) => l.event === ev && l.fn === fn);
    if (i >= 0) listeners.splice(i, 1);
  },
};

const store = {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

const window = {
  matchMedia: (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }),
};

const requestAnimationFrame = () => { rafActive += 1; return nextRaf++; };
const cancelAnimationFrame = () => { rafActive = Math.max(0, rafActive - 1); rafCancels += 1; };

const toasts = [];
function showToast(message) { toasts.push(String(message)); }
let speakCalls = 0;
function speakLast() { speakCalls += 1; }
const state = { lastText: 'A real answer the companion can read aloud.' };

eval(fs.readFileSync('src/novacontrol/web/static/js/companion.js', 'utf8'));

const C = window.NovaCompanion;
const out = {};
const stageMotion = () => byId('companionStage').dataset.motion;
const label = () => byId('companionState').textContent;
const tone = () => byId('companionState').dataset.tone;
const stageState = () => byId('companionStage').dataset.state;
const eyeRy = () => Number(byId('companionEyeLeft').attrs.ry);
const visibilityHandlers = () => listeners.filter((l) => l.event === 'visibilitychange').length;
const pauseHandlers = () => (byId('companionPauseButton').handlers.click || []).length;

out.modulePresent = Boolean(C);
if (!C) { console.log(JSON.stringify(out)); process.exit(0); }

C.mount();
out.afterMount = {
  state: C.state(),
  label: label(),
  stage: stageState(),
  tone: tone(),
  eyeRy: eyeRy(),
  rafActive,
  visibilityHandlers: visibilityHandlers(),
  pauseHandlers: pauseHandlers(),
  motion: stageMotion(),
  name: byId('companionName').textContent,
};

C.onRunStart('chat', 'Thinking through your question...');
out.thinking = { state: C.state(), label: label(), tone: tone(), stage: stageState(), detail: byId('companionDetail').textContent };

C.onProgress({ detail: 'Searching the web' });
out.progress = { state: C.state(), detail: byId('companionDetail').textContent, label: label() };

C.onRunResult('chat');
out.responding = { state: C.state(), label: label(), tone: tone() };

C.onRunError('the request failed');
out.error = { state: C.state(), label: label(), tone: tone(), eyeRy: eyeRy(), detail: byId('companionDetail').textContent };

// Error must hold: neither an unrelated voice end nor another client's
// completed work may clear this tab's real failure.
C.onListeningEnd();
C.onCompleted('explore');
out.errorHolds = C.state();

// The activity channel is app-wide: another client's completion must not settle
// the face while this tab still has a run in flight.
C.onRunStart('explore', 'Researching');
out.appWideWhileLocalRun = { local: C.state() };
C.onCompleted('explore');
out.appWideWhileLocalRun.afterAppWide = C.state();
C.onRunResult('explore');
out.appWideWhileLocalRun.afterLocalResult = C.state();
// With nothing local in flight, an app-wide completion does settle it.
C.onCompleted('explore');
out.appWideAlone = C.state();

// A plan that came back needing approval is a real stop, not a completion.
C.onRunStart('command', 'Planning the command');
C.onRunResult('command', { waiting: true });
out.waitingForApproval = { state: C.state(), label: label(), tone: tone(), detail: byId('companionDetail').textContent };
// Another client's completed research cannot take down THIS tab's approval gate.
C.onCompleted('explore');
out.waitingAcrossAppWide = C.state();
C.onRunStart('command', 'Running the approved command');
out.waitingClearedByNewWork = C.state();
C.onRunResult('command');

// Pausing stops the animation loop but never the state.
C.setPaused(true);
out.paused = {
  isPaused: C.isPaused(),
  rafActive,
  rafCancels,
  motion: stageMotion(),
  stored: localStorage.getItem('novacontrol.companion.motion'),
  pressed: byId('companionPauseButton').getAttribute('aria-pressed'),
};
const pausedEyeBefore = eyeRy();
C.onListening();
out.listeningWhilePaused = {
  state: C.state(),
  label: label(),
  tone: tone(),
  eyeRy: eyeRy(),
  eyeRyWhileFailed: pausedEyeBefore,
  rafActive,
};
// The app-wide channel belongs to every client; a live local microphone must
// win over another client's work and completion.
C.onProgress({ detail: 'Another client researching' });
C.onCompleted('explore');
out.listeningWhilePaused.appWideProgressHeld = C.state();
out.listeningWhilePaused.detail = byId('companionDetail').textContent;
C.onListeningEnd();
out.listeningEnd = C.state();
// A run that is still in flight keeps the face working when dictation ends.
C.onRunStart('explore', 'Researching');
C.onListening();
C.onListeningEnd();
out.listeningEndDuringRun = C.state();
C.onRunResult('explore');
C.onSpeechStart();
out.speaking = C.state();
C.onProgress({ detail: 'Another client researching' });
C.onCompleted('explore');
out.speakingHolds = C.state();
C.onSpeechEnd();
out.speechEnd = C.state();

C.setPaused(false);
out.resumed = { isPaused: C.isPaused(), rafActive, motion: stageMotion() };

// Import seam: a real Avatar Lab export drives colours and eye geometry.
const definition = {
  schema: 'bible-strong/avatar-definition',
  schemaVersion: 1,
  name: 'Ava',
  colors: { body: '#ff0055', eyes: '#00ff88' },
  expressions: {
    neutral: {
      eyes: {
        left: { width: 5, height: 6, angle: 10 },
        right: { width: 5, height: 6, angle: 10 },
        spacing: 30,
      },
    },
  },
  animations: { wave: { steps: [{ expression: 'neutral', holdMs: 400 }] } },
};
const applied = C.applyAvatarDefinition(definition);
out.import = {
  ok: applied.ok,
  name: applied.name,
  ignored: applied.ignored,
  headFill: byId('companionHead').attrs.fill,
  eyeFill: byId('companionEyeLeft').attrs.fill,
  nameLabel: byId('companionName').textContent,
  source: byId('companionStage').dataset.avatarSource,
  stored: Boolean(localStorage.getItem('novacontrol.companion.avatar')),
  avatar: C.avatar(),
  eyeRy: Number(byId('companionEyeLeft').attrs.ry),
};

out.rejectNotObject = C.avatarFromDefinition([]);
out.rejectWrongSchema = C.avatarFromDefinition({ schema: 'something/else' });
out.rejectBadColour = C.avatarFromDefinition({ schema: 'bible-strong/avatar-definition', colors: { body: 'red' } });
out.rejectVersion = C.avatarFromDefinition({ schema: 'bible-strong/avatar-definition', schemaVersion: 9 });

// Re-mounting must not stack listeners or leave a second loop running.
const beforeRemount = { pause: pauseHandlers(), visibility: visibilityHandlers(), rafActive };
C.mount();
out.remount = {
  before: beforeRemount,
  after: { pause: pauseHandlers(), visibility: visibilityHandlers(), rafActive },
  restoredHead: byId('companionHead').attrs.fill,
  restoredName: byId('companionName').textContent,
};

C.unmount();
out.unmounted = { pause: pauseHandlers(), visibility: visibilityHandlers(), rafActive };

// The face is a real control: clicking it opens Chat and focuses the input.
C.mount();
byId('companionFace').click();
out.faceClick = { chatClicks: chatNav.clicks, focused: byId('chatInput').focused };

// The Speak control calls the real speech path.
byId('companionSpeakButton').click();
out.speak = { calls: speakCalls };

// Pause control toggles through the UI, not just the API.
byId('companionPauseButton').click();
out.pauseToggle = { paused: C.isPaused(), toasts: toasts.length };

console.log(JSON.stringify(out));
"""


class CompanionRuntimeTests(unittest.TestCase):
    """Drive the real companion.js through the events the app really emits."""

    NODE = shutil.which("node")

    @classmethod
    def setUpClass(cls) -> None:
        if cls.NODE is None:
            raise unittest.SkipTest("node is not installed")
        proc = subprocess.run(  # type: ignore[list-item]
            [cls.NODE, "-e", _HARNESS],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(f"companion harness failed:\n{proc.stderr}")
        cls.raw = proc.stdout

    def _out(self) -> dict:
        try:
            return json.loads(self.raw)
        except json.JSONDecodeError as exc:  # a missing console.log reads as this
            raise AssertionError(f"harness produced no JSON: {self.raw!r}") from exc

    def test_module_is_exposed_on_window(self) -> None:
        self.assertTrue(self._out().get("modulePresent"), "companion.js did not publish window.NovaCompanion")

    def test_mount_starts_idle_and_animating(self) -> None:
        after = self._out()["afterMount"]
        self.assertEqual(after["state"], "idle")
        self.assertEqual(after["label"], "Idle")
        self.assertEqual(after["stage"], "idle")
        self.assertEqual(after["tone"], "idle")
        self.assertGreater(after["rafActive"], 0, "the ambient animation loop never started")
        self.assertEqual(after["visibilityHandlers"], 1, "the visibility policy must be bound exactly once")
        self.assertEqual(after["motion"], "on")
        self.assertEqual(after["name"], "Nova")

    def test_a_real_chat_run_moves_the_companion_through_thinking(self) -> None:
        out = self._out()
        self.assertEqual(out["thinking"]["state"], "thinking")
        self.assertEqual(out["thinking"]["label"], "Thinking")
        self.assertEqual(out["thinking"]["tone"], "busy")
        self.assertEqual(out["thinking"]["stage"], "thinking")

    def test_live_progress_becomes_the_working_state_with_its_step(self) -> None:
        progress = self._out()["progress"]
        self.assertEqual(progress["state"], "working")
        self.assertEqual(progress["detail"], "Searching the web")
        self.assertEqual(progress["label"], "Working")

    def test_a_finished_run_responds_then_settles(self) -> None:
        responding = self._out()["responding"]
        self.assertEqual(responding["state"], "responding")
        self.assertEqual(responding["label"], "Responding")
        self.assertEqual(responding["tone"], "live")

    def test_an_app_wide_completion_cannot_settle_a_tab_that_is_still_working(self) -> None:
        out = self._out()["appWideWhileLocalRun"]
        self.assertEqual(out["local"], "working")
        self.assertEqual(out["afterAppWide"], "working", "another client's completion settled this tab")
        self.assertEqual(out["afterLocalResult"], "responding")
        self.assertEqual(self._out()["appWideAlone"], "responding")

    def test_a_plan_awaiting_approval_holds_the_waiting_state(self) -> None:
        waiting = self._out()["waitingForApproval"]
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["label"], "Waiting")
        self.assertEqual(waiting["tone"], "busy")
        self.assertEqual(waiting["detail"], "Waiting for your approval")
        # Another client's completion is not this tab acting, so the gate stays.
        self.assertEqual(self._out()["waitingAcrossAppWide"], "waiting")
        # Starting the approved action is real work again.
        self.assertEqual(self._out()["waitingClearedByNewWork"], "working")

    def test_a_failed_run_is_recorded_and_not_silently_cleared(self) -> None:
        out = self._out()
        self.assertEqual(out["error"]["state"], "error")
        self.assertEqual(out["error"]["label"], "Interrupted")
        self.assertEqual(out["error"]["tone"], "error")
        self.assertEqual(out["error"]["detail"], "the request failed")
        # The face changes too (eyes narrow) — the state is not just a label.
        self.assertLess(out["error"]["eyeRy"], out["afterMount"]["eyeRy"])
        self.assertEqual(out["errorHolds"], "error", "an unrelated event cleared a real failure")

    def test_pausing_stops_the_loop_but_not_the_state(self) -> None:
        out = self._out()
        paused = out["paused"]
        self.assertTrue(paused["isPaused"])
        self.assertEqual(paused["rafActive"], 0, "the animation loop kept running while paused")
        self.assertGreater(paused["rafCancels"], 0)
        self.assertEqual(paused["motion"], "paused")
        self.assertEqual(paused["pressed"], "true")
        self.assertEqual(paused["stored"], "paused")
        # State still tracks real events with motion off.
        self.assertEqual(out["listeningWhilePaused"]["state"], "listening")
        self.assertEqual(out["listeningWhilePaused"]["label"], "Listening")
        self.assertEqual(out["listeningWhilePaused"]["tone"], "live")
        # The face was actually repainted while paused (eyes opened from the
        # narrowed failure pose), and no animation frame was scheduled.
        self.assertGreater(out["listeningWhilePaused"]["eyeRy"], out["listeningWhilePaused"]["eyeRyWhileFailed"])
        self.assertEqual(out["listeningWhilePaused"]["rafActive"], 0)

    def test_voice_listening_and_speaking_are_distinct_states(self) -> None:
        out = self._out()
        self.assertEqual(out["listeningEnd"], "idle")
        self.assertEqual(out["speaking"], "speaking")
        self.assertEqual(out["speechEnd"], "idle")

    def test_app_wide_frames_do_not_stomp_a_live_voice_state(self) -> None:
        out = self._out()
        self.assertEqual(out["listeningWhilePaused"]["appWideProgressHeld"], "listening")
        self.assertEqual(out["listeningWhilePaused"]["detail"], "", "a remote step overwrote the live voice state's detail")
        self.assertEqual(out["speakingHolds"], "speaking")

    def test_ending_dictation_restores_outstanding_work(self) -> None:
        self.assertEqual(self._out()["listeningEndDuringRun"], "working")

    def test_resuming_restarts_the_loop(self) -> None:
        resumed = self._out()["resumed"]
        self.assertFalse(resumed["isPaused"])
        self.assertGreater(resumed["rafActive"], 0, "motion never resumed")
        self.assertEqual(resumed["motion"], "on")

    def test_avatar_lab_definition_drives_the_face(self) -> None:
        imported = self._out()["import"]
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["name"], "Ava")
        self.assertEqual(imported["headFill"], "#ff0055")
        self.assertEqual(imported["eyeFill"], "#00ff88")
        self.assertEqual(imported["nameLabel"], "Ava")
        self.assertEqual(imported["source"], "avatar-lab")
        self.assertTrue(imported["stored"], "an imported avatar must survive a reload")
        self.assertEqual(imported["ignored"], ["animations"])
        self.assertEqual(imported["avatar"]["eyeSpacing"], 1.154, "spacing 30/26 must scale the eye gap")
        self.assertEqual(imported["avatar"]["eyeAngle"], 10)
        # Eye size is normalised by the eye-to-gap ratio, not raw lab units, so
        # the imported avatar keeps its proportions instead of clamping.
        self.assertAlmostEqual(imported["avatar"]["eyeScaleX"], 0.802, places=2)
        self.assertAlmostEqual(imported["avatar"]["eyeScaleY"], 0.788, places=2)
        self.assertAlmostEqual(imported["eyeRy"], 6.6 * 0.788, places=1, msg="the imported eye size never reached the face")

    def test_a_foreign_file_is_refused_with_a_reason(self) -> None:
        out = self._out()
        self.assertFalse(out["rejectNotObject"]["ok"])
        self.assertIn("JSON object", out["rejectNotObject"]["reason"])
        self.assertFalse(out["rejectWrongSchema"]["ok"])
        self.assertIn("bible-strong/avatar-definition", out["rejectWrongSchema"]["reason"])
        self.assertFalse(out["rejectBadColour"]["ok"])
        self.assertIn("colors.body", out["rejectBadColour"]["reason"])
        self.assertFalse(out["rejectVersion"]["ok"])
        self.assertIn("schema version", out["rejectVersion"]["reason"])

    def test_remount_does_not_stack_listeners_or_loops(self) -> None:
        remount = self._out()["remount"]
        self.assertEqual(remount["before"]["pause"], 1)
        self.assertEqual(remount["after"]["pause"], 1, "a second mount stacked click listeners")
        self.assertEqual(remount["after"]["visibility"], 1, "a second mount stacked visibility listeners")
        self.assertEqual(remount["after"]["rafActive"], 1, "a second mount left two loops running")
        self.assertEqual(remount["restoredHead"], "#ff0055", "the stored avatar was not restored")
        self.assertEqual(remount["restoredName"], "Ava")

    def test_unmount_releases_everything(self) -> None:
        unmounted = self._out()["unmounted"]
        self.assertEqual(unmounted["pause"], 0)
        self.assertEqual(unmounted["visibility"], 0)
        self.assertEqual(unmounted["rafActive"], 0)

    def test_the_face_opens_chat_and_speak_uses_the_real_speech_path(self) -> None:
        out = self._out()
        self.assertEqual(out["faceClick"]["chatClicks"], 1, "clicking the companion did not open the Chat panel")
        self.assertTrue(out["faceClick"]["focused"], "clicking the companion did not focus the chat input")
        self.assertEqual(out["speak"]["calls"], 1, "the Speak control did not reach speakLast()")

    def test_the_pause_control_toggles_through_the_ui(self) -> None:
        out = self._out()
        self.assertTrue(out["pauseToggle"]["paused"], "the Motion button did not pause the companion")
        self.assertGreaterEqual(out["pauseToggle"]["toasts"], 1)


class CompanionAssetTests(unittest.TestCase):
    """The served page must carry the stage, its ids, and the module itself."""

    def setUp(self) -> None:
        self.index = (STATIC / "index.html").read_text(encoding="utf-8")
        self.module = (STATIC / "js" / "companion.js").read_text(encoding="utf-8")

    def test_index_ships_the_companion_stage_and_module(self) -> None:
        self.assertIn('id="companionStage"', self.index)
        self.assertIn("/static/js/companion.js?v=__NC_ASSET_VERSION__", self.index)

    def test_every_id_the_module_touches_exists_in_the_page(self) -> None:
        ids = set(re.findall(r'byId\("(companion[A-Za-z]+)"\)', self.module))
        ids |= set(re.findall(r'companionSetAttr\("(companion[A-Za-z]+)"', self.module))
        self.assertGreaterEqual(len(ids), 12, "the module stopped addressing the stage it renders")
        missing = sorted(i for i in ids if f'id="{i}"' not in self.index)
        self.assertEqual(missing, [], f"companion.js addresses ids that index.html does not define: {missing}")

    def test_the_companion_loads_no_external_asset_or_package(self) -> None:
        # No vendored Avatar Lab code and no remote runtime: the AGPL blocker is
        # avoided, not worked around. Only the export schema id is referenced.
        self.assertNotRegex(self.module, r"https?://")
        self.assertNotIn("@bible-strong/", self.module)
        self.assertIn('COMPANION_AVATAR_SCHEMA = "bible-strong/avatar-definition"', self.module)

    def test_the_companion_is_driven_by_real_events_not_a_state_timer(self) -> None:
        # There is exactly one dwell timer, and it only ends a real response.
        self.assertEqual(self.module.count("setTimeout("), 1)
        self.assertIn("companionReturnToIdle(COMPANION_RESPOND_DWELL_MS)", self.module)
        for event_hook in ("onRunStart", "onProgress", "onRunResult", "onRunError"):
            self.assertIn(event_hook, self.module)

    def test_the_application_calls_the_companion_from_the_real_pipelines(self) -> None:
        render_utils = (STATIC / "js" / "render-utils.js").read_text(encoding="utf-8")
        voice = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.NovaCompanion.onRunStart(type", render_utils)
        self.assertIn("window.NovaCompanion.onRunResult(type, { waiting })", render_utils)
        self.assertIn('data.status === "waiting_for_approval"', render_utils)
        self.assertIn("window.NovaCompanion.onRunError(", render_utils)
        self.assertIn("window.NovaCompanion.onProgress(data)", render_utils)
        self.assertIn("window.NovaCompanion.onListening()", voice)
        self.assertIn("window.NovaCompanion.onSpeechStart()", voice)
        self.assertIn("setupCompanion();", app)


if __name__ == "__main__":
    unittest.main()
