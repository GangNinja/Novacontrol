"""Extra 4 — Thinking Orbs, pinned against the real modules and page.

``ActivityAdapterTests`` runs the shipped ``activity-state.js`` in Node and
drives the same events the application publishes: NLU, planning, tool use,
verification, task completion, Explore progress, voice capture and the tracked
task snapshot. It pins the properties that make the orbs informative rather
than decorative:

* every normalized state comes from an event, not a timer or a guess;
* a live microphone is the main activity but the request lifecycle can take
  the main orb back after capture;
* Explore, voice and task views stay isolated;
* duplicate and out-of-order frames cannot overwrite newer state;
* a late frame cannot reopen a finished activity;
* an absent live feed is reported as unavailable, not as work.

``OrbRendererTests`` runs ``thinking-orb.js`` against a canvas shim and renders
every supported state, checking the shared instance lifecycle, DPR cap, sizing
and cleanup. ``OrbAssetTests`` pins the four placements, the module wiring, the
single SSE channel, and the reduced-motion CSS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path("src/novacontrol/web/static")
NODE = shutil.which("node")

_ACTIVITY_HARNESS = r"""
const fs = require('fs');
const assert = require('assert');
global.window = {};
global.document = { hidden: false };
eval(fs.readFileSync('src/novacontrol/web/static/js/activity-state.js', 'utf8'));
const A = window.NovaActivity;
const state = (scope) => A.getState(scope);
const out = {};

out.initial = A.getScopes();
assert.equal(out.initial.main.state, 'idle');
assert.equal(out.initial.explore.state, 'idle');
assert.equal(out.initial.voice.state, 'idle');
assert.equal(out.initial.task.state, 'idle');

A.ingest('task.started', { correlation_id: 'req-1', task_id: 'task-1', kind: 'ask' });
assert.equal(state('main').state, 'thinking');
assert.equal(state('task').taskId, 'task-1');

A.ingest('intent.detected', { correlation_id: 'req-1', intent: 'chat' });
assert.equal(state('main').label, 'Understanding');
assert.equal(state('main').orb, 'weaving');

A.ingest('context.resolved', { correlation_id: 'req-1', strategy: 'conversation' });
assert.equal(state('main').state, 'thinking');

A.ingest('decision.created', { correlation_id: 'req-1', route: 'chat', decision_type: 'chat' });
assert.equal(state('main').label, 'Choosing an approach');

A.ingest('plan.created', { correlation_id: 'req-1', goal: 'answer', steps: ['a', 'b', 'c'] });
assert.equal(state('main').state, 'planning');
assert.equal(state('main').orb, 'shaping');

A.ingest('tool.selected', { correlation_id: 'req-1', task_id: 'task-1', tool: 'web_search' });
assert.equal(state('main').state, 'connecting');
assert.equal(state('task').state, 'connecting');

A.ingest('tool.started', { correlation_id: 'req-1', task_id: 'task-1', tool: 'web_search' });
assert.equal(state('main').state, 'working');
assert.equal(state('task').label, 'Working');

A.ingest('tool.completed', { correlation_id: 'req-1', task_id: 'task-1', tool: 'web_search', status: 'ok' });
assert.equal(state('main').state, 'solving');
assert.equal(state('task').label, 'Checking the result');

A.ingest('verification.started', { correlation_id: 'req-1', step_id: 's1' });
assert.equal(state('main').state, 'solving');
A.ingest('verification.completed', { correlation_id: 'req-1', step_id: 's1', status: 'failed' });
assert.equal(state('main').state, 'error');
A.ingest('recovery.started', { correlation_id: 'req-1', step_id: 's1', attempt: 1, ceiling: 2 });
assert.equal(state('main').state, 'recovering');
A.ingest('recovery.completed', { correlation_id: 'req-1', step_id: 's1', outcome: 'retried' });
assert.equal(state('main').state, 'working');
A.ingest('task.completed', { correlation_id: 'req-1', task_id: 'task-1', route: 'chat' });
assert.equal(state('main').state, 'completed');
assert.equal(state('task').state, 'completed');
A.ingest('intent.detected', { correlation_id: 'req-1', intent: 'late' });
assert.equal(state('main').state, 'completed');
out.lifecycle = { main: state('main').state, task: state('task').state };

A.reset();
A.ingest('intent.detected', { correlation_id: 'dup', intent: 'chat' });
A.ingest('intent.detected', { correlation_id: 'dup', intent: 'chat' });
out.duplicateActivities = A.diagnostics().activities;
assert.equal(out.duplicateActivities, 1);

A.reset();
A.ingest('tool.started', { correlation_id: 'seq', task_id: 't', tool: 'x' }, { seq: 5 });
A.ingest('tool.failed', { correlation_id: 'seq', task_id: 't', tool: 'x', error: 'late' }, { seq: 3 });
out.outOfOrder = state('main').state;
assert.equal(out.outOfOrder, 'working');

A.reset();
A.setTasks([{ id: 'snap', title: 'A real tracked task', status: 'running', kind: 'agent' }]);
out.taskSnapshot = state('task');
assert.equal(out.taskSnapshot.state, 'working');
assert.equal(out.taskSnapshot.label, 'Task in progress');
assert.equal(out.taskSnapshot.taskId, 'snap');

A.reset();
A.setTransport('reconnecting', 'Reconnecting to the live activity channel…');
out.transport = state('main');
assert.equal(out.transport.state, 'unknown');
assert.match(out.transport.description, /Reconnecting/);
A.setTransport('connected');
assert.equal(state('main').state, 'idle');

A.reset();
A.runStart('explore', { loadingText: 'Researching — live progress appears below...' });
A.ingest('explore.progress', { correlation_id: 'research-1', step: 'searching', detail: 'Searching for cats' });
assert.equal(state('explore').state, 'searching');
assert.equal(state('main').state, 'searching');
A.voiceListening();
assert.equal(state('voice').state, 'listening');
assert.equal(state('main').state, 'listening');
out.voicePriority = { main: state('main').label, voice: state('voice').label };
A.voiceProcessing();
assert.equal(state('voice').state, 'thinking');
assert.equal(state('main').state, 'thinking');
A.voiceError('Microphone blocked');
assert.equal(state('voice').state, 'error');
assert.equal(state('main').state, 'error');
A.voiceListening();
assert.equal(state('voice').state, 'listening');
A.voiceEnd();
assert.equal(state('voice').state, 'idle');
assert.equal(state('voice').status, 'idle');
assert.equal(state('main').state, 'searching');
out.voice = { voice: state('voice').state, main: state('main').state };

A.ingest('explore.progress', { correlation_id: 'research-1', step: 'complete', detail: 'Research complete' });
assert.equal(state('explore').state, 'completed');
A.ingest('explore.progress', { correlation_id: 'research-1', step: 'searching', detail: 'late frame' });
assert.equal(state('explore').state, 'completed');
out.lateAfterComplete = state('explore').state;

A.reset();
A.runStart('chat', { loadingText: 'Thinking through your question...' });
assert.equal(state('main').state, 'thinking');
A.runResult('chat', { waiting: false });
assert.equal(state('main').state, 'completed');
out.localRun = state('main').state;
A.reset();
A.runStart('explore', { loadingText: 'Researching' });
A.runResult('explore');
assert.equal(state('explore').label, 'Research complete');
assert.equal(state('explore').status, 'completed');
assert.match(state('explore').description, /report is ready/);
out.exploreCompletion = state('explore');
A.runStart('command', { loadingText: 'Planning the command' });
A.runResult('command', { waiting: true });
assert.equal(state('main').state, 'waiting');
out.waiting = state('main').state;

A.reset();
A.runStart('chat', { loadingText: 'Thinking' });
A.runError('the request failed');
assert.equal(state('main').state, 'error');
assert.match(state('main').description, /failed/);
out.error = state('main').state;

out.diagnostics = A.diagnostics();
console.log(JSON.stringify(out));
"""

_ORB_HARNESS = r"""
const fs = require('fs');
const assert = require('assert');
function makeCtx() {
  return new Proxy({}, {
    get(target, prop) {
      if (prop === 'createRadialGradient') return () => ({ addColorStop() {} });
      if (prop === 'canvas') return null;
      return target[prop] || (() => {});
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}
const ctx = makeCtx();
function makeCanvas() {
  return {
    className: '', style: {}, width: 0, height: 0, parentNode: null,
    getContext() { return ctx; }, setAttribute() {},
  };
}
const document = {
  hidden: false,
  createElement() { return makeCanvas(); },
  addEventListener() {},
};
function makeHost(size) {
  return {
    children: [], dataset: {}, style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    appendChild(node) { this.children.push(node); node.parentNode = this; },
    removeChild(node) { this.children = this.children.filter((item) => item !== node); },
    getBoundingClientRect() { return { width: size, height: size }; },
    ownerDocument: document, setAttribute() {}, getAttribute() { return null; },
  };
}
global.document = document;
global.window = {
  devicePixelRatio: 4,
  getComputedStyle() { return { getPropertyValue() { return ''; } }; },
  matchMedia() { return { matches: false }; },
};
eval(fs.readFileSync('src/novacontrol/web/static/js/thinking-orb.js', 'utf8'));
const host = makeHost(216);
const orb = window.NovaOrb.create(host, { size: 216, dots: 80 });
const states = ['breathing', 'listening', 'searching', 'weaving', 'shaping', 'connecting', 'working', 'solving', 'composing', 'waiting', 'completed', 'error', 'unknown'];
for (const state of states) orb.setState(state);
const out = {
  states: states.length,
  canvas: { width: host.children[0].width, height: host.children[0].height, styleWidth: host.children[0].style.width },
  ariaHidden: host.children[0].className,
};
assert.equal(window.NovaOrb.stats().instances, 1);
assert.equal(out.canvas.width, 432); // 216 CSS × DPR 2 cap, even though devicePixelRatio is 4
assert.equal(out.canvas.height, 432);
assert.equal(out.canvas.styleWidth, '216px');
assert.equal(out.ariaHidden, 'thinking-orb-canvas');
assert.equal(window.NovaOrb.stats().instances, 1);
orb.destroy();
assert.equal(window.NovaOrb.stats().instances, 0);
out.statsAfterDestroy = window.NovaOrb.stats();

global.window.matchMedia = () => ({ matches: true });
const reducedHost = makeHost(64);
const reduced = window.NovaOrb.create(reducedHost, { size: 64, dots: 40 });
reduced.setState('searching');
assert.equal(window.NovaOrb.reducedMotion(), true);
reduced.destroy();
out.reducedMotion = true;
console.log(JSON.stringify(out));
"""


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE or "node", "-e", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Node harness failed (exit %s)\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (completed.returncode, completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


@unittest.skipUnless(NODE, "node is required for the Extra 4 module harness")
class ActivityAdapterTests(unittest.TestCase):
    def test_activity_mapping_transitions_and_isolation(self) -> None:
        result = _run_node(_ACTIVITY_HARNESS)
        self.assertEqual(result["lifecycle"], {"main": "completed", "task": "completed"})
        self.assertEqual(result["duplicateActivities"], 1)
        self.assertEqual(result["outOfOrder"], "working")
        self.assertEqual(result["taskSnapshot"]["state"], "working")
        self.assertEqual(result["taskSnapshot"]["label"], "Task in progress")
        self.assertEqual(result["transport"]["state"], "unknown")
        self.assertIn("Reconnecting", result["transport"]["description"])
        self.assertEqual(result["voicePriority"], {"main": "Listening", "voice": "Listening"})
        self.assertEqual(result["voice"], {"voice": "idle", "main": "searching"})
        self.assertEqual(result["lateAfterComplete"], "completed")
        self.assertEqual(result["localRun"], "completed")
        self.assertEqual(result["exploreCompletion"]["label"], "Research complete")
        self.assertIn("report is ready", result["exploreCompletion"]["description"])
        self.assertEqual(result["waiting"], "waiting")
        self.assertEqual(result["error"], "error")
        self.assertEqual(result["diagnostics"]["subscribers"], 0)


class OrbAssetTests(unittest.TestCase):
    def test_four_placements_and_module_wiring(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for host in ("novaOrbMain", "novaOrbExplore", "novaOrbVoice", "novaOrbTask"):
            self.assertIn(f'id="{host}"', html)
        for scope in ("Main", "Explore", "Voice", "Task"):
            self.assertIn(f'id="novaOrb{scope}Label"', html)
            self.assertIn(f'id="novaOrb{scope}Status"', html)
        self.assertIn('class="thinking-orb orb-size-main"', html)
        self.assertIn('class="thinking-orb orb-size-voice"', html)
        self.assertIn('class="thinking-orb orb-size-compact"', html)
        self.assertIn('class="thinking-orb orb-size-task"', html)
        for module in ("activity-state.js", "thinking-orb.js", "orbs.js"):
            self.assertIn(f'/static/js/{module}?v=__NC_ASSET_VERSION__', html)

        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("window.NovaOrbs.mount()", app)
        self.assertIn("NovaActivity.setTasks", app)

        render_utils = (STATIC / "js" / "render-utils.js").read_text(encoding="utf-8")
        self.assertEqual(render_utils.count("new EventSource"), 1)
        for event_type in (
            "intent.detected",
            "decision.created",
            "plan.created",
            "task.completed",
            "tool.started",
            "verification.completed",
            "recovery.started",
            "explore.progress",
            "command.progress",
        ):
            self.assertIn(event_type, render_utils)
        self.assertIn("NovaActivity.ingest", render_utils)
        self.assertIn("NovaActivity.setTransport", render_utils)

        voice = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
        for method in ("voiceListening", "voiceProcessing", "voiceError", "voiceEnd", "voiceSpeaking"):
            self.assertIn(f"NovaActivity.{method}", voice)

        styles = (STATIC / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".thinking-orb", styles)
        self.assertIn("prefers-reduced-motion: reduce", styles)

        for module in ("activity-state.js", "thinking-orb.js", "orbs.js"):
            source = (STATIC / "js" / module).read_text(encoding="utf-8")
            self.assertNotIn("extra4Settle", source)
            self.assertNotIn('from "thinking-orbs"', source)
            self.assertNotIn("from 'thinking-orbs'", source)
            self.assertNotIn("/thinking-orbs", source)
            self.assertNotIn("fetch(", source)
            self.assertNotIn("XMLHttpRequest", source)

    def test_orb_renderer_contract(self) -> None:
        result = _run_node(_ORB_HARNESS)
        self.assertEqual(result["states"], 13)
        self.assertEqual(result["statsAfterDestroy"]["instances"], 0)
        self.assertEqual(result["canvas"], {"width": 432, "height": 432, "styleWidth": "216px"})
        self.assertEqual(result["ariaHidden"], "thinking-orb-canvas")


if __name__ == "__main__":
    unittest.main()
