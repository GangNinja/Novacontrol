"""Frontend contract for the NovaForge panel.

Three kinds of pin, all about the panel never lying:

* **Behavioral** — ``window.NovaForgeLogic`` is evaluated in Node with no DOM.
  It answers the three questions the panel must get right (is this frame stale,
  is this stage really available, may this control be pressed), so those
  answers are tested as code rather than by grepping for substrings.
* **Rendered** — ``renderTelemetry`` and ``renderGuardian`` are evaluated
  against a minimal DOM shim with live payload shapes, so what the tabs build
  for zero, unavailable, unsupported, enforced and NOT-enforced values is
  asserted on the resulting rows.
* **Static** — the panel ships a placeholder with no frame URL, planned
  destinations are disabled, and a frame is fetched through the authenticated
  request path (an ``<img src>`` cannot carry the API token).

The module is skipped cleanly when Node is unavailable, matching the existing
JS-renderer tests in this suite.
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
CSS = STATIC / "nc-os.css"

NEEDLE = """
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const now = Date.now();
function frame(offsetMs, extra) {
  return Object.assign({ captured_at: new Date(now - offsetMs).toISOString(), stale: false }, extra || {});
}
const missing = L.stageText(null);
const proposed = L.stageText({ stage: 'proposed', available: false, reason: 'No decision pipeline is attached.' });
const observed = L.stageText({
  stage: 'observed', available: true, at: nowISO(),
  records: [{ summary: '1000x600 frame of the window', at: nowISO(), source: 'desktop_capture', evidence: true }],
});
const observedNoEvidence = L.stageText({
  stage: 'observed', available: true, at: nowISO(),
  records: [{ summary: 'Capture failed', at: nowISO(), source: 'desktop_capture', evidence: false }],
});
function nowISO() { return new Date().toISOString(); }
const base = { busy: false, activeId: '', captureSupported: true, validated: 'sig', signature: 'sig' };
const empty = {
  running: L.visionEmptyText('RUNNING'),
  paused: L.visionEmptyText('PAUSED'),
  idle: L.visionEmptyText(''),
};
const tokens = { same: L.frameStillCurrent(3, 3), older: L.frameStillCurrent(2, 3) };
const responses = { same: L.responseStillCurrent(4, 4), older: L.responseStillCurrent(3, 4) };
const metrics = [
  L.metricText(0, 'ms'), L.metricText(null, 'ms'), L.metricText(undefined, 'ms'),
  L.metricText(NaN, 'ms'), L.metricText('', 'ms'), L.metricText(12.5, 'ms'), L.metricText(7, '')
];
const limits = {
  enforced: L.guardianLimitState({ enforced: true, state: 'enforced' }),
  configured: L.guardianLimitState({ enforced: false, state: 'configured_not_enforceable' }),
  notConfigured: L.guardianLimitState({ enforced: false, state: 'not_configured' }),
  unsupported: L.guardianLimitState({ enforced: false, state: 'unsupported' }),
  missing: L.guardianLimitState(null)
};
const saves = {
  dropped: L.saveOutcome({ valid: true, persisted: false }),
  kept: L.saveOutcome({ valid: true, persisted: true }),
  invalid: L.saveOutcome({ valid: false, persisted: true }),
};
const out = {
  empty: empty,
  tokens: tokens,
  responses: responses,
  metrics: metrics,
  limits: limits,
  saves: saves,
  missing: missing,
  proposed: proposed,
  observed: observed,
  observedNoEvidence: observedNoEvidence,
  controls: {
    emergency: L.controlAvailability({ can: { emergency_stop: true, cancel: true } },
      Object.assign({}, base, { activeId: 'abc' })),
    terminal: L.controlAvailability({ can: { emergency_stop: false, cancel: false } }, base),
    idle: L.controlAvailability(null, base),
    unvalidated: L.controlAvailability(null, Object.assign({}, base, { validated: '' })),
    staleValidation: L.controlAvailability(null, Object.assign({}, base, { validated: 'old' })),
    noCapture: L.controlAvailability(null, Object.assign({}, base, { captureSupported: false })),
    running: L.controlAvailability({ can: { pause: true, resume: false, stop: true } },
      Object.assign({}, base, { activeId: 'abc' })),
    paused: L.controlAvailability({ can: { pause: false, resume: true, stop: true } },
      Object.assign({}, base, { activeId: 'abc' })),
    stopped: L.controlAvailability({ can: { pause: false, resume: false, stop: false } }, base),
    busy: L.controlAvailability({ can: { pause: true, resume: true, stop: true } },
      Object.assign({}, base, { activeId: 'abc', busy: true })),
  },
  freshness: {
    fresh: L.frameFreshness(frame(1000), now),
    old: L.frameFreshness(frame(6000), now),
    flagged: L.frameFreshness(frame(100), now, { stale: true }),
    flaggedExplicit: L.frameFreshness(Object.assign(frame(100), { stale: true }), now),
    unreadable: L.frameFreshness({ captured_at: 'not-a-date' }, now),
    none: L.frameFreshness(null, now),
  },
  duration: [L.formatDuration(12.44), L.formatDuration(63), L.formatDuration(-1),
    L.formatDuration(null), L.formatDuration(undefined), L.formatDuration(''), L.formatDuration(0)],
};
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeLogicTests(unittest.TestCase):
    """The panel's honesty decisions, executed in Node with no DOM."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        # Decode as UTF-8 explicitly: ``text=True`` uses the Windows locale and
        # turned the em-dash placeholder into U+FFFD, a test failure that says
        # nothing about the panel.
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_a_missing_stage_is_not_available_and_says_no_record(self) -> None:
        self.assertFalse(self.data["missing"]["available"])
        self.assertEqual(self.data["missing"]["text"], "No record.")

    def test_an_unavailable_stage_renders_its_reason(self) -> None:
        proposed = self.data["proposed"]
        self.assertFalse(proposed["available"])
        self.assertIn("No decision pipeline", proposed["text"])

    def test_observed_stage_reports_whether_evidence_exists(self) -> None:
        self.assertTrue(self.data["observed"]["available"])
        self.assertIn("evidence recorded", self.data["observed"]["text"])
        self.assertIn("NO evidence", self.data["observedNoEvidence"]["text"])
        self.assertNotIn("NO evidence", self.data["observed"]["text"])

    def test_start_requires_a_validated_matching_mission_and_no_active_session(self) -> None:
        controls = self.data["controls"]
        self.assertTrue(controls["idle"]["start"])
        self.assertFalse(controls["unvalidated"]["start"])
        self.assertFalse(controls["staleValidation"]["start"], "an edited mission must be re-validated")
        self.assertFalse(controls["noCapture"]["start"])
        self.assertTrue(controls["stopped"]["start"], "with no active session Start returns")

    def test_session_controls_follow_the_backend_can_flags(self) -> None:
        controls = self.data["controls"]
        self.assertTrue(controls["running"]["pause"])
        self.assertFalse(controls["running"]["resume"])
        self.assertTrue(controls["paused"]["resume"])
        self.assertFalse(controls["paused"]["pause"])
        self.assertFalse(controls["stopped"]["stop"], "a terminal session has nothing to stop")
        for key, value in controls["busy"].items():
            self.assertFalse(value, f"{key} must be disabled while a request is in flight")

    def test_a_frame_is_stale_when_old_flagged_or_unreadable(self) -> None:
        freshness = self.data["freshness"]
        self.assertFalse(freshness["fresh"]["stale"])
        self.assertTrue(freshness["old"]["stale"], "a 6s-old frame must not look live")
        self.assertTrue(freshness["flaggedExplicit"]["stale"])
        self.assertTrue(freshness["unreadable"]["stale"])
        self.assertTrue(freshness["none"]["stale"])

    def test_duration_formatting_is_honest_about_negative_and_missing_values(self) -> None:
        self.assertEqual(
            self.data["duration"],
            ["12.4 s", "1 m 03 s", "—", "—", "—", "—", "0.0 s"],
        )

    def test_a_running_session_without_a_frame_does_not_claim_there_is_no_session(self) -> None:
        empty = self.data["empty"]
        self.assertIn("has not been recorded", empty["running"]["detail"])
        self.assertNotIn("No session is active", empty["running"]["title"])
        self.assertIn("paused", empty["paused"]["title"].lower())
        self.assertEqual(empty["idle"]["title"], "No session is active.")

    def test_an_older_frame_response_cannot_replace_a_newer_one(self) -> None:
        self.assertTrue(self.data["tokens"]["same"])
        self.assertFalse(self.data["tokens"]["older"])

    def test_an_older_session_response_cannot_overwrite_newer_state(self) -> None:
        self.assertTrue(self.data["responses"]["same"])
        self.assertFalse(self.data["responses"]["older"])

    def test_zero_is_measured_and_missing_values_are_not_zero(self) -> None:
        metrics = self.data["metrics"]
        self.assertEqual(metrics[0], "0 ms")
        self.assertEqual(metrics[1], "unavailable")
        self.assertEqual(metrics[2], "unavailable")
        self.assertEqual(metrics[3], "unavailable")
        self.assertEqual(metrics[4], "unavailable")
        self.assertEqual(metrics[5], "12.5 ms")
        self.assertEqual(metrics[6], "7")

    def test_limit_state_distinguishes_enforced_from_configured_only(self) -> None:
        limits = self.data["limits"]
        self.assertEqual(limits["enforced"], "enforced")
        self.assertEqual(limits["configured"], "configured — NOT enforced")
        self.assertEqual(limits["notConfigured"], "not configured")
        self.assertEqual(limits["unsupported"], "unsupported")
        self.assertEqual(limits["missing"], "unavailable")

    def test_emergency_controls_follow_the_backend_can_flags(self) -> None:
        controls = self.data["controls"]
        self.assertTrue(controls["emergency"]["emergencyStop"])
        self.assertTrue(controls["emergency"]["cancel"])
        self.assertFalse(controls["terminal"]["emergencyStop"])
        self.assertFalse(controls["terminal"]["cancel"])

    def test_a_saved_draft_is_only_called_saved_when_the_store_kept_it(self) -> None:
        saves = self.data["saves"]
        self.assertIn("NOT saved", saves["dropped"]["message"])
        self.assertEqual(saves["dropped"]["kind"], "error")
        self.assertNotIn("Mission draft saved.", saves["dropped"]["message"])
        self.assertEqual(saves["kept"]["message"], "Mission draft saved.")
        self.assertEqual(saves["kept"]["kind"], "")
        self.assertEqual(saves["invalid"]["kind"], "error")


class NovaForgePanelContractTests(unittest.TestCase):
    """Static contract for the panel markup and its frame path."""

    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")
        self.js = PANEL_JS.read_text(encoding="utf-8")
        self.css = CSS.read_text(encoding="utf-8")

    def test_panel_exposes_the_required_controls(self) -> None:
        for element_id in (
            "novaforgePanel", "forgeState", "forgeTarget", "forgeReady",
            "forgeStartButton", "forgePauseButton", "forgeResumeButton", "forgeStopButton",
            "forgeMissionName", "forgeMissionObjective", "forgeMissionTarget",
            "forgeMissionCriteria", "forgeTimeLimit", "forgeMaxActions",
            "forgeFrame", "forgeVisionEmpty", "forgeFrameStale",
            "forgeStages", "forgeSummary", "forgeHistoryList",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")

    def test_frame_image_has_no_baked_url(self) -> None:
        """A frame URL in the markup would bypass the authenticated fetch."""
        start = self.html.index('id="forgeFrame"')
        end = self.html.index(">", start)
        tag = self.html[start:end]
        self.assertNotIn("src=", tag)
        self.assertIn("fetch(frame.url, { headers: authHeaders() })", self.js)

    def test_the_telemetry_and_guardian_tabs_render_real_value_containers(self) -> None:
        for element_id in (
            "forgeTabTelemetry", "forgeTelemetryView", "forgeTelemetryCapture",
            "forgeTelemetrySession", "forgeTelemetryTarget", "forgeTelemetryResources",
            "forgeTelemetryNotes", "forgeTelemetryUpdated", "forgeTelemetryLatencyNote",
            "forgeTabGuardian", "forgeGuardianView", "forgeGuardianState",
            "forgeGuardianReason", "forgeGuardianLimits", "forgeGuardianTarget",
            "forgeGuardianCleanup", "forgeGuardianInterventions",
            "forgeGuardianGuarantees", "forgeEmergencyStopButton", "forgeCancelButton",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
        self.assertIn("data-forge-tab=\"forgeTelemetryView\"", self.html)
        self.assertIn("data-forge-tab=\"forgeGuardianView\"", self.html)

    def test_the_roadmap_card_does_not_claim_nf03_is_unimplemented(self) -> None:
        """The card listed NF-03 as planned and described it as per-action replay."""
        self.assertNotIn("NF-03 through NF-06 are not implemented", self.html)
        self.assertNotIn("per-action replay", self.html)
        self.assertIn("<strong>NF-03</strong> Evidence Timeline — implemented", self.html)
        self.assertIn("<strong>NF-04</strong> Benchmark Lab — implemented", self.html)
        self.assertIn("<strong>NF-05</strong> Controlled Game Evaluation — implemented", self.html)
        self.assertIn("<strong>NF-06</strong> Forge Coach — implemented", self.html)
        # Every NovaForge phase this build ships is stated as implemented; a
        # stale "not implemented" claim would contradict the panel beneath it.
        self.assertNotIn("NF-06 is not implemented", self.html)
        self.assertNotIn("NF-05 and NF-06 are not implemented", self.html)
        self.assertNotIn("NF-04 through NF-06 are not implemented", self.html)

    def test_planned_destinations_are_disabled_and_labelled(self) -> None:
        for phase in ("NF-02", "NF-03", "NF-04", "NF-05", "NF-06"):
            self.assertIn(phase, self.html)
        planned = self.html.count('class="nav-item planned"')
        self.assertEqual(
            planned, 0,
            "NF-02 through NF-06 are implemented and must not stay planned nav entries",
        )
        self.assertNotIn("disabled", self.html.split('data-panel="novaforgePanel"')[1].split(">")[0])
        self.assertNotIn('title="Planned for NovaForge NF-02', self.html)
        self.assertNotIn('title="Planned for NovaForge NF-03', self.html)
        self.assertNotIn('title="Planned for NovaForge NF-04', self.html)
        self.assertNotIn('title="Planned for NovaForge NF-05', self.html)
        # ONE sidebar destination: NF-02…NF-06 are tabs inside Mission Control,
        # so the four extra nav entries (and their dead data-forge-* hooks) are
        # gone rather than sitting beside the panel they open.
        self.assertEqual(
            self.html.count('data-panel="novaforgePanel"'), 1,
            "NovaForge must not occupy more than one sidebar destination",
        )
        for attribute in ("data-forge-timeline", "data-forge-benchmark",
                          "data-forge-evaluation", "data-forge-coach"):
            with self.subTest(attribute=attribute):
                self.assertNotIn(attribute + "=", self.html)
        for view in ("forgeTimelineView", "forgeBenchmarkView",
                     "forgeEvaluationView", "forgeCoachView"):
            with self.subTest(view=view):
                self.assertIn(f'data-forge-tab="{view}"', self.html)

    def test_every_phase_tab_is_reachable_without_horizontal_scrolling(self) -> None:
        """The phase tabs are the ONLY way into the later phases, so all show.

        Benchmarks, Game Evaluation, Evidence Timeline and Forge Coach lost
        their sidebar entries on purpose. A tab strip that scrolls sideways
        hides its own tail with no scrollbar affordance, which would leave
        three of those four destinations effectively unreachable.
        """
        block = self.css.split(".forge-tabs {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-wrap: wrap", block, "the tab strip must wrap, not scroll")
        self.assertNotIn("overflow-x", block)
        self.assertNotIn("fit-content", block)
        self.assertEqual(
            self.html.count("data-forge-tab="), 9,
            "every NovaForge view needs its own always-visible tab",
        )

    def test_the_panel_has_the_objective_action_and_verification_controls(self) -> None:
        """Remediation R1/R2 controls, and the two claims they must never make."""
        for element_id in (
            "forgeVerificationBadge", "forgeVerificationReason", "forgeObjectiveKind",
            "forgeObjectiveParameter", "forgeObjectiveSaveButton", "forgeVerifyButton",
            "forgeActionKind", "forgeActionKey", "forgeActionPoint",
            "forgeActionPlanButton", "forgeActionRunButton", "forgeActionLog",
        ):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
        # Run is gated on a REAL plan, and what it posts is that plan's own id and
        # digest — not an action rebuilt from the fields on screen.
        self.assertIn("forge.gameplay.proposal && actions.available", self.js)
        self.assertIn("proposal_id: proposal.proposal_id, digest: proposal.digest", self.js)
        self.assertIn("Planning sends nothing", self.html)
        # An executed action is reported with the verifier's verdict, never as a
        # success on its own.
        self.assertIn("verificationBadge(answer.verification)", self.js)
        self.assertIn("objective not met", self.js)

    def test_emergency_stop_reports_only_the_confirmed_backend_result(self) -> None:
        self.assertIn("Emergency stop confirmed by the backend", self.js)
        self.assertIn("Emergency stop was NOT confirmed", self.js)
        self.assertIn("/emergency-stop", self.js)
        request_at = self.js.index('encodeURIComponent(target) + "/emergency-stop"')
        success_at = self.js.index("Emergency stop confirmed by the backend")
        self.assertLess(request_at, success_at, "success is claimed before the backend answers")

    def test_polling_is_bounded_and_stops_for_terminal_sessions(self) -> None:
        self.assertIn("ACTIVE.indexOf(data.state) >= 0", self.js)
        self.assertIn("stopPoll()", self.js)
        self.assertIn("document.hidden", self.js)
        self.assertIn("setTimeout(refreshSession, POLL_MS)", self.js)
        self.assertIn("responseStillCurrent(token, forge.stateToken)", self.js)


RENDERER_NEEDLE = r"""
const fs = require('fs');
function __mk(tag, cls, text) {
  return {
    tagName: tag,
    className: cls || '',
    _text: text == null ? '' : String(text),
    children: [],
    dataset: {},
    hidden: false,
    title: '',
    get textContent() {
      return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    },
    set textContent(value) { this._text = String(value); this.children.length = 0; },
    set innerHTML(value) { this._text = ''; this.children.length = 0; },
    get innerHTML() { return ''; },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}
global.window = {};
var window = global.window;
var __byId = {};
var document = {
  getElementById: function (id) {
    if (!__byId[id]) __byId[id] = __mk(id);
    return __byId[id];
  },
  createElement: function (tag) { return __mk(tag); },
  addEventListener: function () {},
  hidden: false
};
function el(tag, cls, text) { return __mk(tag, cls, text); }
function authHeaders() { return {}; }
function requestJson() { throw new Error('renderers must not touch the network'); }
function rowMap(boxId) {
  var box = document.getElementById(boxId);
  var out = {};
  box.children.forEach(function (metric) {
    var label = metric.children[0] ? metric.children[0].textContent : '';
    out[label] = {
      value: metric.children[1] ? metric.children[1].textContent : '',
      available: metric.dataset.available
    };
  });
  return out;
}
var now = new Date().toISOString();
var telemetry = {
  version: 1,
  generated_at: now,
  session: {
    state: 'RUNNING', duration_seconds: 12.5, active_seconds: 12.0,
    paused_seconds: 0.5, pause_count: 1, state_changes: 3, errors: 0,
    end_reason: '', interrupted: false
  },
  capture: {
    frames: 0, stale_frames: 0, capture_failures: 0, frame_gaps: 0,
    gap_threshold_ms: 1500, max_gap_ms: null, last_frame_age_ms: null,
    latency_ms: {
      samples: 0, window: 0, window_limit: 120, min_ms: null, median_ms: null,
      p95_ms: null, max_ms: null, mean_ms: null, p95_available: false,
      p95_min_samples: 20,
      p95_reason: 'Only 0 latency sample(s) are available; a p95 needs at least 20.',
      invalid_samples: 0, basis: 'all 0 sample(s)'
    }
  },
  loop: {
    configured_interval_ms: 1000, observed_frame_rate_hz: null,
    rate_available: false, rate_reason: 'No frames have been captured.'
  },
  target: { checks: 3, last_checked_at: now, losses: 0, changes: 0, unreadable: 0 },
  actions: {
    available: false, reason: 'NF-02 attaches no action-execution path.',
    proposed: null, attempted: null, executed: null, failed: null, verified: null
  },
  resources: {
    cpu: { available: false, value: null, reason: 'no per-session CPU probe' },
    memory: { available: false, value: null, reason: 'no per-session memory probe' },
    gpu: { available: false, value: null, reason: 'no GPU provider' },
    npu: { available: false, value: null, reason: 'no NPU provider' }
  },
  recovery: {
    available: false, attempts: 0,
    reason: 'No retry or recovery path is attached to a NovaForge session.'
  },
  measurement: { notes: ['Elapsed durations use time.monotonic().'] }
};
var guardian = {
  version: 1,
  safety_state: 'observing',
  session_state: 'RUNNING',
  target: {
    state: 'valid', valid: true, validated: true, title: 'Probe', handle: 7,
    pid: 8, last_checked_at: now, reason: 'The newest geometry check matched.'
  },
  input: { allow: false, state: 'unsupported', reason: 'No input path.' },
  input_path_available: false,
  limits: {
    time_limit: { configured: true, seconds: 60, enforced: true, state: 'enforced' },
    action_budget: {
      configured: true, value: 5, enforced: false,
      state: 'configured_not_enforceable', reason: 'No action-execution path exists in NF-02.'
    },
    observation_loop: {
      interval_seconds: 1, max_frames_on_disk: 30, max_sessions_kept: 40,
      pending_operations: 1, retries: 0, bounded: true
    }
  },
  controls: {
    emergency_stop: { available: true, idempotent: true, bounded_seconds: 8 },
    cancel: { available: true, idempotent: true, bounded_seconds: 8 }
  },
  cleanup: { state: 'ok', ok: true, held_after: 0, presses: 0, releases: 0, note: 'presses matched releases' },
  interventions: [
    { at: now, kind: 'emergency_stop', reason: 'emergency_stop', outcome: 'stopped', detail: '5.8 ms' }
  ],
  guarantees: [
    { name: 'time_limit', enforced: true, state: 'enforced', detail: 'checked while paused too' },
    { name: 'action_budget', enforced: false, state: 'configured_not_enforceable', detail: 'no action path' }
  ]
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
var L = global.window.NovaForgeLogic;
var out = {};
L.renderTelemetry(telemetry);
out.updated = document.getElementById('forgeTelemetryUpdated').textContent;
out.capture = rowMap('forgeTelemetryCapture');
out.session = rowMap('forgeTelemetrySession');
out.resources = rowMap('forgeTelemetryResources');
out.latencyNote = document.getElementById('forgeTelemetryLatencyNote').textContent;
out.notes = document.getElementById('forgeTelemetryNotes').children.map(function (li) { return li.textContent; });
L.renderGuardian(guardian);
out.guardianState = document.getElementById('forgeGuardianState').textContent;
out.guardianReason = document.getElementById('forgeGuardianReason').textContent;
out.limits = rowMap('forgeGuardianLimits');
out.guardianTarget = rowMap('forgeGuardianTarget');
out.cleanup = rowMap('forgeGuardianCleanup');
out.interventions = document.getElementById('forgeGuardianInterventions').children.map(function (li) { return li.textContent; });
out.guarantees = document.getElementById('forgeGuardianGuarantees').children.map(function (li) {
  return { text: li.textContent, enforced: li.dataset.enforced };
});
L.renderGuardian(null);
out.emptyGuardian = document.getElementById('forgeGuardianState').textContent;
L.renderTelemetry(null);
out.emptyTelemetry = document.getElementById('forgeTelemetryUpdated').textContent;
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeRendererDomTests(unittest.TestCase):
    """The REAL renderers, executed with a DOM shim, against live payload shapes.

    The pure-logic suite pins the decisions; this pins what the tabs actually
    render for zero, unavailable, unsupported, enforced and NOT-enforced values.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(RENDERER_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"renderer driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_zero_frames_render_as_a_measurement_and_missing_latency_as_unavailable(self) -> None:
        capture = self.data["capture"]
        self.assertEqual(capture["Frames captured"]["value"], "0")
        self.assertEqual(capture["Frames captured"]["available"], "true")
        self.assertEqual(capture["Latency p95"]["value"], "unavailable")
        self.assertEqual(capture["Latency p95"]["available"], "false")
        self.assertEqual(capture["Latency median"]["value"], "unavailable")
        self.assertIn("p95 needs at least 20", self.data["latencyNote"])
        self.assertEqual(self.data["session"]["Errors recorded"]["value"], "0")

    def test_resources_and_actions_never_render_a_guessed_value(self) -> None:
        resources = self.data["resources"]
        for label in ("CPU", "Memory", "GPU", "NPU"):
            self.assertEqual(resources[label]["value"], "unavailable", label)
            self.assertEqual(resources[label]["available"], "false", label)
        self.assertEqual(resources["Actions"]["value"], "unsupported")
        self.assertEqual(resources["Recovery"]["value"], "unsupported")
        self.assertEqual(self.data["updated"].startswith("updated "), True)
        self.assertTrue(self.data["notes"])

    def test_guardian_marks_what_is_enforced_and_what_is_not(self) -> None:
        self.assertEqual(self.data["guardianState"], "observing")
        self.assertEqual(self.data["limits"]["Time limit"]["value"], "enforced")
        self.assertEqual(
            self.data["limits"]["Action budget"]["value"], "configured — NOT enforced"
        )
        self.assertEqual(self.data["limits"]["Observation loop"]["value"], "bounded")
        guarantees = {entry["text"].split(":")[0]: entry for entry in self.data["guarantees"]}
        self.assertEqual(guarantees["action budget"]["enforced"], "false")
        self.assertIn("NOT enforced", guarantees["action budget"]["text"])
        self.assertEqual(guarantees["time limit"]["enforced"], "true")

    def test_guardian_shows_cleanup_and_intervention_outcomes(self) -> None:
        self.assertEqual(self.data["cleanup"]["Cleanup"]["value"], "ok")
        self.assertEqual(self.data["cleanup"]["Held input after cleanup"]["value"], "0")
        joined = " ".join(self.data["interventions"])
        self.assertIn("emergency stop", joined)
        self.assertIn("stopped", joined)

    def test_empty_state_is_rendered_when_no_session_exists(self) -> None:
        self.assertEqual(self.data["emptyGuardian"], "unarmed")
        self.assertEqual(self.data["emptyTelemetry"], "no session")


TIMELINE_LOGIC_NEEDLE = r"""
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
const recorded = {
  source: 'recorded',
  replay: {
    read_only: true, can_start: false, can_resume: false, can_stop: false,
    can_execute_action: false, can_send_input: false, reason: 'Replay inspects evidence.'
  }
};
const out = {
  guard: L.replayGuard(recorded),
  tampered: L.replayGuard({ replay: { read_only: true, can_execute_action: true } }),
  unconfirmed: L.replayGuard({ replay: { read_only: false } }),
  missingGuard: L.replayGuard(null),
  evidence: {
    ok: L.evidenceStatus({ evidence: [{ kind: 'frame', available: true }], evidence_available: true }),
    missing: L.evidenceStatus({
      evidence: [{ kind: 'frame', available: false, reason: 'no longer retained' }],
      evidence_available: false
    }),
    none: L.evidenceStatus({ evidence: [] })
  },
  persistence: {
    verified: L.persistenceText({ state: 'verified' }),
    failed: L.persistenceText({ state: 'failed', reason: 'the snapshot did not read back' }),
    pending: L.persistenceText({ state: 'pending', reason: 'the session is still running' }),
    unknown: L.persistenceText(null)
  },
  compare: {
    missing: L.compareValue({ left: null, right: 9, available: true, reason: 'p95 needs at least 20' }, 'left'),
    bothMissing: L.compareValue({ left: null, right: null, available: false, reason: 'no verifier' }, 'right'),
    zero: L.compareValue({ left: 0, right: 4 }, 'left'),
    flag: L.compareValue({ left: false, right: true, available: false, reason: 'no verifier' }, 'left')
  },
  elapsed: [L.formatElapsed(null), L.formatElapsed(0), L.formatElapsed(12.4), L.formatElapsed(1500)],
  classification: {
    direct: L.classificationRow({ category: 'capture_failure', direct: true, detail: 'x', evidence: [{ seq: 1 }] }),
    inferred: L.classificationRow({ category: 'unknown', direct: false, detail: 'y', evidence: [{ seq: 2 }, { seq: 3 }] })
  },
  tokens: {
    same: L.timelineResponseStillCurrent(2, 2),
    older: L.timelineResponseStillCurrent(1, 2)
  },
  sourceLabel: L.timelineSourceText(recorded),
  derivedLabel: L.timelineSourceText({ source: 'derived' }),
  pageLabel: L.timelinePageLabel({ count: 40, total: 240 }, 140),
  pageLabelPageOnly: L.timelinePageLabel({ count: 3, total: 3 }),
  pageLabelEmpty: L.timelinePageLabel({ count: 0, total: 0 })
};
console.log(JSON.stringify(out));
"""


TIMELINE_RENDER_NEEDLE = r"""
const fs = require('fs');
function __mk(tag, cls, text) {
  return {
    tagName: tag,
    className: cls || '',
    _text: text == null ? '' : String(text),
    children: [],
    dataset: {},
    hidden: false,
    title: '',
    get textContent() {
      return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    },
    set textContent(value) { this._text = String(value); this.children.length = 0; },
    set innerHTML(value) { this._text = ''; this.children.length = 0; },
    get innerHTML() { return ''; },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}
global.window = {};
var window = global.window;
var __byId = {};
var document = {
  getElementById: function (id) {
    if (!__byId[id]) __byId[id] = __mk(id);
    return __byId[id];
  },
  createElement: function (tag) { return __mk(tag); },
  querySelector: function () { return null; },
  addEventListener: function () {},
  hidden: false
};
function el(tag, cls, text) { return __mk(tag, cls, text); }
function authHeaders() { return {}; }
function requestJson() { throw new Error('replay renderers must not touch the network'); }
eval(fs.readFileSync(process.argv[2], 'utf8'));
var L = global.window.NovaForgeLogic;
function find(node, tag) {
  if (!node) return null;
  if (node.tagName === tag) return node;
  for (var i = 0; i < (node.children || []).length; i += 1) {
    var hit = find(node.children[i], tag);
    if (hit) return hit;
  }
  return null;
}
var expired = {
  seq: 7, type: 'frame.captured', category: 'capture', severity: 'warning',
  source: 'capture', at: '2026-09-18T10:00:00+00:00', elapsed_ms: 1200,
  event_id: 'event-7', derived: false, evidence_available: false,
  summary: 'Frame 1 captured',
  payload: { frame_seq: 1, width: 1000, height: 600 },
  evidence: [
    { kind: 'frame', session_id: 'a', seq: 1, available: false, reason: 'Frame 1 is no longer retained.' },
    { kind: 'observation', session_id: 'a', index: 1, available: true, summary: '1000x600 frame', evidence: true }
  ]
};
L.renderTimelineDetail(expired);
var out = {
  detailTitle: document.getElementById('forgeTimelineDetailTitle').textContent,
  detail: document.getElementById('forgeTimelineDetail').textContent,
  frameHidden: document.getElementById('forgeTimelineFrame').hidden,
  frameNote: document.getElementById('forgeTimelineFrameNote').textContent,
  evidenceAvailability: []
};
var evidenceItems = document.getElementById('forgeTimelineDetail').children;
function collectEvidence(node) {
  if (node.className === 'forge-timeline-evidence') {
    node.children.forEach(function (item) { out.evidenceAvailability.push(item.dataset.available); });
  }
  (node.children || []).forEach(collectEvidence);
}
collectEvidence(document.getElementById('forgeTimelineDetail'));

L.renderTimelineClassification({
  outcome: 'classified',
  detail: '1 classification(s) supported by recorded evidence.',
  end_reason: 'time_limit',
  classifications: [{
    category: 'capture_failure', direct: true, detail: 'The capture seam failed.',
    evidence: [{ kind: 'event', event_id: 'x', seq: 99, type: 'error.recorded' }]
  }],
  completion: { verified: false, claimed: false, reason: 'NF-03 has no objective verifier.' }
});
out.classificationText = document.getElementById('forgeTimelineClassification').textContent;
out.classificationDirect = find(document.getElementById('forgeTimelineClassification'), 'li').dataset.direct;

L.renderTimelineMeta({
  session_id: 'a', state: 'STOPPED', source: 'recorded',
  source_note: 'Recorded as the session happened.',
  persistence: { state: 'verified' },
  retention: { stored: 2, max_events: 240, dropped: 0 },
  page: { count: 1, total: 2 },
  ordering: { primary: 'seq', note: 'Events are ordered by their per-session sequence number.' },
  limitations: ['NF-03 records no action or verification stages, so it emits none.'],
  replay: { read_only: true, can_start: false, can_resume: false, can_stop: false,
    can_execute_action: false, can_send_input: false }
});
out.meta = document.getElementById('forgeTimelineMeta').textContent;

L.renderTimelineCompare({
  left: { session_id: 'a', mission: 'Left run', state: 'STOPPED' },
  right: { session_id: 'b', mission: 'Right run', state: 'STOPPED' },
  rows: [
    { key: 'frames', label: 'Frames captured', left: 0, right: 4, delta: 4, available: true, reason: '' },
    { key: 'latency_p95_ms', label: 'Capture latency p95 (ms)', left: null, right: 9,
      delta: null, available: true, reason: 'p95 needs at least 20 samples.' },
    { key: 'actions_executed', label: 'Actions executed', left: null, right: null,
      delta: null, available: false, reason: 'NF-03 records no action path.' }
  ],
  missing: { left: ['Capture latency p95 (ms)'], right: [], note: 'A missing figure is not zero.' }
});
var table = find(document.getElementById('forgeTimelineCompareOutput'), 'table');
out.compare = table.children[1].children.map(function (row) {
  return row.children.map(function (cell) {
    return { text: cell.textContent, available: cell.dataset.available, title: cell.title };
  });
});
out.compareText = document.getElementById('forgeTimelineCompareOutput').textContent;
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeTimelineUiTests(unittest.TestCase):
    """NF-03's panel answers, executed: replay safety, missing evidence, compare."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = {}
        for key, needle in (("logic", TIMELINE_LOGIC_NEEDLE), ("render", TIMELINE_RENDER_NEEDLE)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
                handle.write(needle)
                driver = Path(handle.name)
            completed = subprocess.run(
                ["node", str(driver), str(PANEL_JS)],
                capture_output=True, timeout=60, check=False,
            )
            driver.unlink(missing_ok=True)
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            if completed.returncode != 0:
                raise AssertionError(f"node failed for {key}: {stderr[:800]}")
            try:
                cls.data[key] = json.loads(stdout.strip().splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
                raise AssertionError(f"{key} driver produced no JSON: {stdout[:400]!r}") from exc

    def test_replay_is_only_called_read_only_when_the_backend_confirms_it(self) -> None:
        logic = self.data["logic"]
        self.assertTrue(logic["guard"]["readOnly"])
        self.assertEqual(logic["guard"]["blocked"], [])
        self.assertFalse(logic["tampered"]["readOnly"])
        self.assertEqual(logic["tampered"]["blocked"], ["can_execute_action"])
        self.assertFalse(logic["unconfirmed"]["readOnly"])
        self.assertFalse(logic["missingGuard"]["readOnly"])

    def test_a_missing_evidence_reference_is_reported_with_its_reason(self) -> None:
        evidence = self.data["logic"]["evidence"]
        self.assertTrue(evidence["ok"]["ok"])
        self.assertFalse(evidence["missing"]["ok"])
        self.assertEqual(evidence["missing"]["text"], "1 evidence unavailable")
        self.assertIsNone(evidence["none"]["ok"])
        self.assertEqual(evidence["none"]["text"], "no evidence reference")

    def test_persistence_is_reported_as_measured_never_assumed(self) -> None:
        persistence = self.data["logic"]["persistence"]
        self.assertTrue(persistence["verified"]["ok"])
        self.assertFalse(persistence["failed"]["ok"])
        self.assertIn("FAILED", persistence["failed"]["text"])
        self.assertIsNone(persistence["pending"]["ok"])
        self.assertIsNone(persistence["unknown"]["ok"])

    def test_an_unmeasured_comparison_figure_is_unavailable_not_zero(self) -> None:
        compare = self.data["logic"]["compare"]
        self.assertEqual(compare["missing"]["text"], "unavailable")
        self.assertFalse(compare["missing"]["available"])
        self.assertIn("p95 needs at least 20", compare["missing"]["note"])
        self.assertEqual(compare["bothMissing"]["text"], "unavailable")
        self.assertEqual(compare["zero"]["text"], "0", "a measured zero is printed")
        self.assertEqual(compare["flag"]["text"], "no")

    def test_elapsed_is_labelled_unavailable_without_a_monotonic_reading(self) -> None:
        self.assertEqual(
            self.data["logic"]["elapsed"],
            ["elapsed unavailable", "0 ms", "12 ms", "1.50 s"],
        )

    def test_direct_and_inferred_classifications_are_distinguished(self) -> None:
        classification = self.data["logic"]["classification"]
        self.assertEqual(classification["direct"]["category"], "Capture Failure")
        self.assertEqual(classification["direct"]["kind"], "directly recorded")
        self.assertEqual(classification["inferred"]["kind"], "inferred")
        self.assertEqual(classification["inferred"]["evidence"], 2)

    def test_an_older_timeline_or_frame_response_is_discarded(self) -> None:
        tokens = self.data["logic"]["tokens"]
        self.assertTrue(tokens["same"])
        self.assertFalse(tokens["older"])

    def test_a_derived_timeline_says_so(self) -> None:
        self.assertEqual(self.data["logic"]["sourceLabel"], "recorded event log")
        self.assertEqual(self.data["logic"]["derivedLabel"], "derived from the session record")

    def test_the_loaded_count_is_reported_after_loading_more(self) -> None:
        logic = self.data["logic"]
        self.assertEqual(logic["pageLabel"], "140 of 240 event(s)")
        self.assertEqual(logic["pageLabelPageOnly"], "3 of 3 event(s)")
        self.assertEqual(logic["pageLabelEmpty"], "no events")

    def test_the_panel_states_which_pipeline_stages_were_unavailable(self) -> None:
        meta = self.data["render"]["meta"]
        self.assertIn("NF-03 records no action or verification stages", meta)
        self.assertIn("verified", meta)
        self.assertIn("recorded event log", meta)

    def test_an_expired_frame_is_explained_and_the_event_stays_inspectable(self) -> None:
        render = self.data["render"]
        self.assertIn("#7", render["detailTitle"])
        self.assertIn("frame.captured", render["detailTitle"])
        self.assertTrue(render["frameHidden"])
        self.assertIn("no longer retained", render["frameNote"])
        self.assertIn("Unavailable", render["detail"])
        self.assertIn("frame_seq", render["detail"])
        self.assertIn("false", render["evidenceAvailability"])
        self.assertIn("true", render["evidenceAvailability"])

    def test_classification_renders_directness_and_never_claims_verification(self) -> None:
        render = self.data["render"]
        self.assertEqual(render["classificationDirect"], "true")
        self.assertIn("Capture Failure", render["classificationText"])
        self.assertIn("directly recorded", render["classificationText"])
        self.assertIn("outside the loaded page", render["classificationText"])
        self.assertIn("NOT verified", render["classificationText"])
        self.assertNotIn("verified successfully", render["classificationText"])

    def test_compare_renders_unavailable_figures_without_a_delta(self) -> None:
        rows = {row[0]["text"]: row for row in self.data["render"]["compare"]}
        frames = rows["Frames captured"]
        self.assertEqual(frames[1]["text"], "0", "a measured zero is shown as zero")
        self.assertEqual(frames[2]["text"], "4")
        self.assertEqual(frames[3]["text"], "+4")
        p95 = rows["Capture latency p95 (ms)"]
        self.assertEqual(p95[1]["text"], "unavailable")
        self.assertEqual(p95[1]["available"], "false")
        self.assertEqual(p95[3]["text"], "—")
        self.assertIn("p95 needs at least 20", p95[1]["title"])
        actions = rows["Actions executed"]
        self.assertEqual(actions[1]["text"], "unavailable")
        self.assertIn("NF-03 records no action path", actions[1]["title"])
        self.assertIn("A missing figure is not zero", self.data["render"]["compareText"])

    def test_the_replay_panel_has_no_mutating_call(self) -> None:
        js = PANEL_JS.read_text(encoding="utf-8")
        start = js.index("NF-03: evidence timeline and read-only replay")
        # The NF-03 block ends where NF-04 begins; the benchmark lab DOES cancel
        # runs, and letting its code into this slice would make the replay
        # boundary assertion test the wrong section.
        end = js.index("NF-04: Forge Benchmark Lab")
        block = js[start:end]
        for forbidden in ("/start", "/pause", "/resume", "/stop", "/cancel",
                          "/emergency-stop", 'method: "POST"'):
            self.assertNotIn(forbidden, block, f"replay must never call {forbidden}")
        self.assertIn("fetch(frame.url, { headers: authHeaders() })", block)

    def test_the_replay_markup_never_bakes_a_frame_url(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        start = html.index('id="forgeTimelineFrame"')
        end = html.index(">", start)
        self.assertNotIn("src=", html[start:end])
        for element_id in (
            "forgeTimelineView", "forgeTimelineSession", "forgeTimelineList",
            "forgeTimelineDetail", "forgeTimelinePrevButton", "forgeTimelineNextButton",
            "forgeTimelineFrame", "forgeTimelineClassification", "forgeTimelineCompareOutput",
            "forgeTimelineReadonly", "forgeTimelineMoreButton", "forgeTimelinePosition",
        ):
            self.assertIn(f'id="{element_id}"', html, f"missing #{element_id}")
        self.assertIn("Read-only replay", html)
        self.assertNotIn("NF-04", html.split('id="forgeTimelineView"')[1].split("</section>")[0])


BENCHMARK_NEEDLE = r"""
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; }, createElement: function () { return {}; } };
eval(fs.readFileSync(process.argv[1], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const out = {
  zero: L.benchmarkMetric({ value: 0, available: true, unit: 'ms' }),
  missing: L.benchmarkMetric({ value: null, available: false, reason: 'p95 needs 20 samples', unit: 'ms' }),
  unsupported: L.benchmarkMetric({ value: null, available: false, kind: 'unsupported', reason: 'no verifier' }),
  canStart: {
    valid: L.benchmarkCanStart({ valid: true, runnable: true }, null),
    invalid: L.benchmarkCanStart({ valid: false }, null),
    running: L.benchmarkCanStart({ valid: true, runnable: true }, { status: 'running' }),
    missing: L.benchmarkCanStart(null, null)
  },
  status: {
    completed: L.benchmarkStatusText({ status: 'completed', valid: true }),
    aborted: L.benchmarkStatusText({ status: 'aborted', valid: false }),
    invalid: L.benchmarkStatusText({ status: 'invalid', valid: false }),
    failed: L.benchmarkStatusText({ status: 'failed', valid: false }),
    none: L.benchmarkStatusText(null)
  },
  verdict: {
    refused: L.benchmarkVerdictText({ established: false, reason: 'NF-04 invokes no model or policy.' }),
    noReason: L.benchmarkVerdictText({ established: false }),
    established: L.benchmarkVerdictText({ established: true, reason: 'two compatible samples' }),
    missing: L.benchmarkVerdictText(null)
  },
  delta: {
    none: L.benchmarkDeltaText({ delta: null, delta_reason: 'not comparable', unit: 'ms' }),
    positive: L.benchmarkDeltaText({ delta: 2.5, unit: 'ms' }),
    negative: L.benchmarkDeltaText({ delta: -1, unit: 'ms' })
  }
};
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeBenchmarkUiTests(unittest.TestCase):
    """NF-04 panel: unavailable stays unavailable, Start needs a live preflight."""

    @classmethod
    def setUpClass(cls) -> None:
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", BENCHMARK_NEEDLE, str(PANEL_JS)],
            capture_output=True,
            text=True,
            # Node prints UTF-8 (the panel uses an em dash for "not measured").
            # Decoding with the Windows locale mangles it into a false mismatch.
            encoding="utf-8",
            timeout=60,
        )
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {completed.stderr[:500]}")
        try:
            cls.data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"unexpected node output: {completed.stdout[:500]}") from exc
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.js = PANEL_JS.read_text(encoding="utf-8")

    def test_a_measured_zero_is_zero_and_a_missing_value_is_unavailable(self) -> None:
        self.assertEqual(self.data["zero"]["text"], "0 ms")
        self.assertTrue(self.data["zero"]["available"])
        self.assertEqual(self.data["missing"]["text"], "unavailable")
        self.assertFalse(self.data["missing"]["available"])
        self.assertIn("p95 needs 20 samples", self.data["missing"]["reason"])
        self.assertEqual(self.data["unsupported"]["text"], "unavailable")
        self.assertIn("no verifier", self.data["unsupported"]["reason"])

    def test_start_requires_a_valid_preflight_and_no_active_run(self) -> None:
        self.assertTrue(self.data["canStart"]["valid"])
        self.assertFalse(self.data["canStart"]["invalid"])
        self.assertFalse(self.data["canStart"]["running"])
        self.assertFalse(self.data["canStart"]["missing"])

    def test_completion_is_data_complete_and_other_endings_are_excluded(self) -> None:
        self.assertIn("data complete", self.data["status"]["completed"])
        self.assertIn("excluded", self.data["status"]["aborted"])
        self.assertIn("excluded", self.data["status"]["invalid"])
        self.assertIn("excluded", self.data["status"]["failed"])
        self.assertEqual(self.data["status"]["none"], "no run")

    def test_no_comparison_is_ever_shown_as_established(self) -> None:
        self.assertTrue(self.data["verdict"]["refused"].startswith("Not established"))
        self.assertIn("NF-04 invokes no model", self.data["verdict"]["refused"])
        self.assertTrue(self.data["verdict"]["noReason"].startswith("Not established"))
        self.assertTrue(self.data["verdict"]["missing"].startswith("Not established"))
        self.assertTrue(self.data["verdict"]["established"].startswith("Established"))

    def test_delta_is_unavailable_when_the_comparison_was_refused(self) -> None:
        self.assertEqual(self.data["delta"]["none"], "—")
        self.assertEqual(self.data["delta"]["positive"], "+2.5 ms")
        self.assertEqual(self.data["delta"]["negative"], "-1 ms")

    def test_the_panel_has_the_benchmark_controls_and_destination(self) -> None:
        for element_id in (
            "forgeTabBenchmark", "forgeBenchmarkView", "forgeBenchmarkScenario",
            "forgeBenchmarkScenarioDetail", "forgeBenchmarkTarget", "forgeBenchmarkDuration",
            "forgeBenchmarkInterval", "forgeBenchmarkPreflightButton",
            "forgeBenchmarkStartButton", "forgeBenchmarkCancelButton",
            "forgeBenchmarkRunState", "forgeBenchmarkRun", "forgeBenchmarkHistoryList",
            "forgeBenchmarkAggregate", "forgeBenchmarkRunDetail",
            "forgeBenchmarkCompareLeft", "forgeBenchmarkCompareRight",
            "forgeBenchmarkCompareButton", "forgeBenchmarkCompareOutput",
            "forgeBenchmarkExportButton",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
        self.assertIn('data-forge-tab="forgeBenchmarkView"', self.html)
        self.assertNotIn(
            'data-forge-benchmark="1"', self.html,
            "the Benchmark Lab is a tab, not a second sidebar destination",
        )

    def test_run_detail_shows_the_basis_beside_the_metrics(self) -> None:
        """The detail surface renders what the run was, not only its numbers.

        The report promises configuration, environment, model/policy reasons and
        limitations on the run detail; without them the export was the only place
        the measurement basis could be read.
        """
        start = self.js.index("async function loadBenchmarkRunDetail")
        end = self.js.index("function benchmarkRunRow", start)
        detail = self.js[start:end]
        for heading in ("Configuration", "Environment", "Model / policy", "Limitations"):
            with self.subTest(heading=heading):
                self.assertIn('el("h4", "", "' + heading + '")', detail)
        for field in ("Scenario", "Budget / interval", "Target", "Platform",
                      "Window geometry", "Resource use", "Model", "Policy"):
            with self.subTest(field=field):
                self.assertIn('detailRow("' + field + '"', detail)
        self.assertIn("run.limitations", detail)

    def test_cancel_notice_follows_the_runs_actual_ending(self) -> None:
        """A cancel that races the time limit must not claim an aborted record."""
        start = self.js.index("async function cancelBenchmark")
        end = self.js.index("function pollBenchmarkRun", start)
        block = self.js[start:end]
        self.assertIn('var finishedAs = String((body.run || {}).status || "unknown");', block)
        self.assertIn('finishedAs === "aborted"', block)
        self.assertIn("no cancellation was recorded", block)
        self.assertNotIn("already_stopped", block)

    def test_the_panel_uses_the_real_benchmark_routes_and_refuses_silent_zeros(self) -> None:
        for endpoint in (
            "/novaforge/benchmarks/scenarios",
            "/novaforge/benchmarks/preflight",
            "/novaforge/benchmarks/runs",
            "/novaforge/benchmarks/runs/",
            "/cancel",
            "/novaforge/benchmarks/aggregate",
            "/novaforge/benchmarks/compare",
        ):
            self.assertIn(endpoint, self.js)
        self.assertIn("Export Result (JSON)", self.html)
        self.assertIn("Open Evidence Timeline", self.js)
        self.assertIn("unavailable — " + "", self.js)
        self.assertIn("CPU, memory, GPU/NPU use and objective success are unavailable", self.js)
        self.assertIn("benchmarkCanStart", self.js)
        self.assertIn("signature !== benchmarkSignature()", self.js)
        # Re-opening the tab refreshes the window list, but must not reset a
        # validated configuration or stop calling the catalog "retained frames".
        self.assertIn("if (!forge.benchmark.scenarios.length) loadBenchmarkCatalog();", self.js)
        self.assertNotIn("Retained frames", self.js)
        # No model/policy picker: the phase invokes neither, and a picker would
        # promise a comparison the backend refuses. The only model-related
        # element is the note explaining that unavailability.
        self.assertIn("forgeBenchmarkModelNote", self.html)
        self.assertNotIn('id="forgeBenchmarkModel"', self.html)
        self.assertNotIn('id="forgeBenchmarkPolicy"', self.html)


EVALUATION_NEEDLE = r"""
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; }, createElement: function () { return {}; } };
eval(fs.readFileSync(process.argv[1], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const ready = { ready: true, checks: [{ id: 'capture', status: 'pass' }] };
const out = {
  canStart: {
    confirmed: L.evaluationCanStart(ready, null, true, true),
    unconfirmed: L.evaluationCanStart(ready, null, false, true),
    stale: L.evaluationCanStart(ready, null, true, false),
    refused: L.evaluationCanStart({ ready: false }, null, true, true),
    running: L.evaluationCanStart(ready, { status: 'running' }, true, true),
    missing: L.evaluationCanStart(null, null, true, true)
  },
  status: {
    completed: L.evaluationStatusText({ status: 'completed', valid: true }),
    completedInvalid: L.evaluationStatusText({ status: 'completed', valid: false }),
    unsupported: L.evaluationStatusText({ status: 'unsupported', valid: false }),
    aborted: L.evaluationStatusText({ status: 'aborted', valid: false }),
    running: L.evaluationStatusText({ status: 'running' }),
    none: L.evaluationStatusText(null)
  },
  checks: {
    pass: L.readinessStateText({ status: 'pass', required: true }),
    fail: L.readinessStateText({ status: 'fail', required: true }),
    optional: L.readinessStateText({ status: 'unavailable', required: false }),
    missing: L.readinessStateText(null)
  },
  baseline: {
    absent: L.evaluationBaselineText({ available: false, reason: 'No completed evaluation exists yet.' }),
    present: L.evaluationBaselineText({ available: true, evaluation_id: 'abc123', samples: 2,
      configuration: { duration_seconds: 5, frame_interval_seconds: 0.5 } }),
    missing: L.evaluationBaselineText(null)
  },
  counts: {
    recorded: L.readinessCountsText({ counts: { pass: 13, warn: 0, fail: 0, unavailable: 1 } }),
    empty: L.readinessCountsText({ counts: {} }),
    missing: L.readinessCountsText(null),
    partial: L.readinessCountsText({ counts: { pass: null } })
  },
  comparison: {
    none: L.evaluationComparisonText(null),
    unavailable: L.evaluationComparisonText({ available: false, reasons: ['not valid'] }),
    incompatible: L.evaluationComparisonText({ available: true, compatible: false,
      reasons: ['different window geometry'] }),
    compatible: L.evaluationComparisonText({ available: true, compatible: true,
      verdict: { established: false, reason: 'a single run per side has no variability' } }),
    established: L.evaluationComparisonText({ available: true, compatible: true,
      verdict: { established: true, reason: 'two compatible samples' } })
  }
};
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeEvaluationUiTests(unittest.TestCase):
    """NF-05 panel: Start needs a passing readiness verdict AND explicit consent."""

    @classmethod
    def setUpClass(cls) -> None:
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", EVALUATION_NEEDLE, str(PANEL_JS)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {completed.stderr[:500]}")
        try:
            cls.data = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"unexpected node output: {completed.stdout[:500]}") from exc
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.js = PANEL_JS.read_text(encoding="utf-8")

    def test_start_requires_readiness_and_an_explicit_confirmation_for_this_exact_config(self) -> None:
        can_start = self.data["canStart"]
        self.assertTrue(can_start["confirmed"])
        self.assertFalse(can_start["unconfirmed"], "readiness is not the operator's consent")
        self.assertFalse(can_start["stale"], "an edited configuration must be re-validated")
        self.assertFalse(can_start["refused"])
        self.assertFalse(can_start["running"])
        self.assertFalse(can_start["missing"])

    def test_completion_never_claims_gameplay_success(self) -> None:
        status = self.data["status"]
        self.assertEqual(status["completed"], "completed · data complete")
        self.assertIn("invalid", status["completedInvalid"])
        self.assertIn("not run", status["unsupported"])
        self.assertIn("excluded", status["aborted"])
        self.assertEqual(status["running"], "running")
        self.assertEqual(status["none"], "no evaluation")

    def test_optional_checks_are_rendered_as_not_required(self) -> None:
        checks = self.data["checks"]
        self.assertEqual(checks["pass"], "pass")
        self.assertEqual(checks["fail"], "fail")
        self.assertIn("not required", checks["optional"])
        self.assertEqual(checks["missing"], "unavailable")

    def test_a_readiness_verdict_that_was_not_recorded_is_not_a_zero(self) -> None:
        """A legacy or partial record must not read as a measured "0 pass"."""
        counts = self.data["counts"]
        self.assertEqual(counts["recorded"], "13 pass · 0 warn · 0 fail · 1 unavailable")
        self.assertIn("not recorded", counts["empty"])
        self.assertIn("not recorded", counts["missing"])
        self.assertIn("? pass", counts["partial"])

    def test_baseline_text_states_its_basis_and_never_invents_one(self) -> None:
        baseline = self.data["baseline"]
        self.assertIn("No completed evaluation exists yet", baseline["absent"])
        self.assertIn("abc123", baseline["present"])
        self.assertIn("5", baseline["present"])
        self.assertIn("2 completed run", baseline["present"])
        self.assertIn("No baseline information", baseline["missing"])

    def test_comparison_refusals_are_shown_as_refusals(self) -> None:
        comparison = self.data["comparison"]
        self.assertIn("baseline", comparison["none"])
        self.assertIn("Not compared", comparison["unavailable"])
        self.assertIn("not valid", comparison["unavailable"])
        self.assertIn("Baseline incompatible", comparison["incompatible"])
        self.assertIn("different window geometry", comparison["incompatible"])
        self.assertIn("Not established", comparison["compatible"])
        self.assertIn("no variability", comparison["compatible"])
        self.assertIn("Established", comparison["established"])

    def test_the_panel_has_the_evaluation_controls_and_destination(self) -> None:
        for element_id in (
            "forgeTabEvaluation", "forgeEvaluationView", "forgeEvaluationGame",
            "forgeEvaluationGameDetail", "forgeEvaluationScenario", "forgeEvaluationTarget",
            "forgeEvaluationDuration", "forgeEvaluationInterval", "forgeEvaluationOffline",
            "forgeEvaluationBaseline", "forgeEvaluationConfirm",
            "forgeEvaluationPreflightButton", "forgeEvaluationStartButton",
            "forgeEvaluationCancelButton", "forgeEvaluationRunState", "forgeEvaluationRun",
            "forgeEvaluationHistoryList", "forgeEvaluationReport",
            "forgeEvaluationReportButton", "forgeEvaluationExportButton",
            "forgeEvaluationPreflight", "forgeEvaluationBaselineCard",
        ):
            self.assertIn(f'id="{element_id}"', self.html, f"missing #{element_id}")
        self.assertIn('data-forge-tab="forgeEvaluationView"', self.html)
        self.assertNotIn(
            'data-forge-evaluation="1"', self.html,
            "Game Evaluation is a tab, not a second sidebar destination",
        )

    def test_a_running_evaluation_links_to_the_sessions_own_surfaces(self) -> None:
        """Live observation reuses the session tabs instead of a second live view."""
        start = self.js.index("function renderEvaluationRun")
        end = self.js.index("function evaluationRunRow", start)
        block = self.js[start:end]
        self.assertIn("Watch Live Session", block)
        self.assertIn("watchEvaluationSession(run.session_id)", block)
        self.assertIn("Open Evidence Timeline", block)
        helper = self.js[self.js.index("function watchEvaluationSession"):
                         self.js.index("function openEvaluationEvidence")]
        self.assertIn('switchTab("forgeLiveView")', helper)
        self.assertIn("forge.sessionId = String(sessionId)", helper)
        self.assertIn("forge.activeId = forge.sessionId", helper)
        self.assertNotIn("forgeEvaluationLive", self.html, "no second live view was added")

    def test_the_panel_uses_the_real_evaluation_routes(self) -> None:
        for endpoint in (
            "/novaforge/games",
            "/novaforge/evaluations/preflight",
            "/novaforge/evaluations",
            "/cancel",
            "/report",
        ):
            self.assertIn(endpoint, self.js)
        self.assertIn("Export Report (JSON)", self.html)
        self.assertIn("Open Evidence Timeline", self.js)

    def test_no_launch_or_input_control_exists_in_the_panel(self) -> None:
        """NF-05 never launches a game or sends input, so it offers no button that would."""
        for forbidden in (
            "Launch Game", "Launch game", "Install Game", "Send Input",
            "Start Game", "Play Game", "forgeEvaluationLaunchButton",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.html)
                self.assertNotIn(forbidden, self.js)
        # Start posts to the evaluation endpoint only; it must never call a session
        # start directly, which would bypass readiness and the baseline record.
        start = self.js.index("async function startEvaluation")
        end = self.js.index("async function cancelEvaluation", start)
        block = self.js[start:end]
        self.assertIn('requestJson("/novaforge/evaluations"', block)
        self.assertNotIn("/novaforge/sessions", block)

    def test_a_refused_readiness_renders_every_reason_and_blocks_start(self) -> None:
        start = self.js.index("function renderEvaluationPreflight")
        end = self.js.index("async function preflightEvaluation", start)
        block = self.js[start:end]
        self.assertIn("readinessStateText", block)
        self.assertIn("check.reason", block)
        self.assertIn("check.evidence", block)
        self.assertIn("body.blocking", block)
        self.assertIn("forge-benchmark-unsupported", block)
        # Start is gated on the readiness verdict, never on the presence of a
        # token or on the form merely being filled in.
        controls = self.js[self.js.index("function updateEvaluationControls"):
                            self.js.index("function invalidateEvaluationPreflight")]
        self.assertIn("evaluationCanStart", controls)
        self.assertIn("confirm.checked === true", controls)
        self.assertIn("forge.evaluation.signature === evaluationSignature()", controls)

    def test_unsupported_games_are_rendered_with_their_reason(self) -> None:
        start = self.js.index("function renderEvaluationGameDetail")
        end = self.js.index("async function selectEvaluationGame", start)
        block = self.js[start:end]
        self.assertIn("status_reason", block)
        self.assertIn("forge-benchmark-unsupported", block)
        self.assertIn("observation only", block)
        self.assertIn("Objective verifier", block)
        # A game with no compatible scenario must not leave a stale scenario
        # selected: the select is rebuilt from the environment's own list.
        selector = self.js[self.js.index("async function selectEvaluationGame"):
                            self.js.index("function evaluationLoadWindows")]
        self.assertIn("renderEvaluationScenarios(detail.scenarios || [], \"\")", selector)

    def test_the_report_shows_objective_and_evidence_gaps_explicitly(self) -> None:
        start = self.js.index("function renderEvaluationReport")
        end = self.js.index("function exportEvaluationReport", start)
        block = self.js[start:end]
        self.assertIn("Objective verified", block)
        self.assertIn("report.objective", block)
        self.assertIn("real_gameplay_control_exercised", block)
        self.assertIn("missing_evidence", block)
        self.assertIn("limitations", block)


EVALUATION_RENDERER_NEEDLE = r"""
const fs = require('fs');
function __mk(tag, cls, text) {
  return {
    tagName: tag,
    className: cls || '',
    _text: text == null ? '' : String(text),
    children: [],
    dataset: {},
    hidden: false,
    title: '',
    type: '',
    disabled: false,
    get textContent() {
      return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    },
    set textContent(value) { this._text = String(value); this.children.length = 0; },
    set innerHTML(value) { this._text = ''; this.children.length = 0; },
    get innerHTML() { return ''; },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    click() {},
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}
global.window = {};
var window = global.window;
var __byId = {};
var document = {
  getElementById: function (id) {
    if (!__byId[id]) __byId[id] = __mk(id);
    return __byId[id];
  },
  createElement: function (tag) { return __mk(tag); },
  addEventListener: function () {},
  body: __mk('body'),
  hidden: false
};
function el(tag, cls, text) { return __mk(tag, cls, text); }
function authHeaders() { return {}; }
function requestJson() { throw new Error('renderers must not touch the network'); }
eval(fs.readFileSync(process.argv[2], 'utf8'));
var L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
function walk(node, depth, out) {
  (node.children || []).forEach(function (child) {
    out.push({ cls: child.className || '', status: child.dataset ? child.dataset.status : undefined,
               text: child.textContent });
    walk(child, depth + 1, out);
  });
  return out;
}
var out = {};

// 1. An UNSUPPORTED environment: the reason must render, with the unsupported styling.
L.renderEvaluationGameDetail({
  game_id: 'gameplay-objective', version: 1,
  display_name: 'Gameplay Objective Evaluation (unsupported)',
  status: 'UNSUPPORTED', runnable: false, control_capable: false,
  status_reason: 'Scoring an in-game objective needs a decision pipeline, a gameplay input path and an objective verifier.',
  supported_systems: ['Windows'], evaluation_modes: [],
  launch: { available: false, reason: 'NF-05 never launches or installs a target.' },
  capture: { available: true },
  input: { available: false, reason: 'No gameplay input path exists in this build.' },
  objective_verification: { available: false, reason: 'No objective verifier is attached.' },
  offline: { classification: 'not_applicable', evidence: 'Unsupported entries cannot run.' },
  known_limitations: ['Catalog record only: this entry states a missing capability.'],
  validation: [], scenarios: []
});
var detail = document.getElementById('forgeEvaluationGameDetail');
out.unsupported = {
  text: detail.textContent,
  classes: walk(detail, 0, []).map(function (n) { return n.cls; }).filter(function (c) { return c; })
};

// 2. A REFUSED readiness verdict: every reason and the blocking ids must render.
L.renderEvaluationPreflight({
  ready: false,
  errors: [],
  blocking: ['target_uniqueness', 'offline_eligibility'],
  counts: { pass: 11, warn: 0, fail: 2, unavailable: 1 },
  checks: [
    { id: 'target_identity', label: 'Target identity', status: 'pass', required: true,
      evidence: 'Probe · handle 4660 · pid 4242', reason: '' },
    { id: 'target_uniqueness', label: 'Target uniqueness', status: 'fail', required: true,
      evidence: 'one title match required',
      reason: 'More than one window matches the target title, so the session could address the wrong window.' },
    { id: 'objective_verification', label: 'Objective verifier', status: 'unavailable', required: false,
      evidence: '', reason: 'No objective verifier is attached to a NovaForge session.' }
  ],
  target: { title: 'Probe', handle: 4660, pid: 4242, width: 1000, height: 600 },
  baseline: { available: false, reason: 'No completed evaluation exists yet.', samples: 0 },
  note: 'Preflight starts nothing.'
});
var preflight = document.getElementById('forgeEvaluationPreflight');
out.refused = {
  cls: preflight.className,
  text: preflight.textContent,
  rows: walk(preflight, 0, []).filter(function (n) { return n.cls === 'forge-readiness-row'; }),
  states: walk(preflight, 0, []).filter(function (n) { return n.cls === 'forge-readiness-state'; })
    .map(function (n) { return n.text; }),
  // The baseline verdict lives in its own card, not inside the checklist.
  baseline: document.getElementById('forgeEvaluationBaselineCard').textContent
};

// 3. An ABORTED report for a legacy record with no readiness counts at all.
L.renderEvaluationReport({
  version: 1, report_id: 'report-abc', evaluation_id: 'abc',
  game: { game_id: 'disposable-window', version: 1, display_name: 'Disposable Test Window', status: 'OBSERVATION_ONLY' },
  compatibility: { status: 'OBSERVATION_ONLY', reason: 'Observable, not controllable.', control_capable: false },
  mode: 'observation_only', scenario: { scenario_id: 'observation-baseline', version: 1 },
  configuration: { duration_seconds: 10, frame_interval_seconds: 0.5 },
  readiness: {},
  status: 'aborted', valid: false,
  reason: 'Benchmark run 5b42 ended aborted: Cancelled by the operator.',
  objective: { available: false, verified: false, achieved: null, reason: 'No objective verifier is attached.' },
  benchmark_run_id: '5b42', session_id: 'sess1', metrics: {},
  evidence: { benchmark_run: '/novaforge/benchmarks/runs/5b42', session: '/novaforge/sessions/sess1',
    timeline: '/novaforge/sessions/sess1/timeline', telemetry: '/novaforge/sessions/sess1/telemetry',
    timeline_source: 'recorded', persistence: { state: 'verified' } },
  baseline: { available: false, reason: 'No completed evaluation exists yet.' },
  comparison: null, guardian: {}, classification: {},
  limitations: ['Observation only: no decision pipeline and no gameplay input.'],
  missing_evidence: ['No captured frame reference was retained for this run.'],
  objective_verified: false, real_gameplay_control_exercised: false,
  note: 'No gameplay input was sent.'
});
out.report = { text: document.getElementById('forgeEvaluationReport').textContent };

// 4. The same report shape WITH counts, so the recorded verdict still renders.
L.renderEvaluationReport({
  evaluation_id: 'abc2', game: { display_name: 'Disposable Test Window' },
  compatibility: { status: 'OBSERVATION_ONLY', reason: 'Observable.' }, mode: 'observation_only',
  readiness: { ready: true, counts: { pass: 13, warn: 0, fail: 0, unavailable: 1 } },
  status: 'completed', valid: true, reason: 'Benchmark run 9 ended completed: data complete.',
  objective: { available: false, verified: false, reason: 'No objective verifier is attached.' },
  benchmark_run_id: '9', session_id: 'sess2', metrics: {}, evidence: {}, baseline: null,
  comparison: null, limitations: [], missing_evidence: [],
  objective_verified: false, real_gameplay_control_exercised: false, note: ''
});
out.reportWithCounts = { text: document.getElementById('forgeEvaluationReport').textContent };
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeEvaluationRendererDomTests(unittest.TestCase):
    """What the NF-05 panel RENDERS for an unsupported environment and a refusal.

    The pure-logic suite pins the decisions; this drives the real renderers, so
    "the UI handles unsupported games and failed readiness" is a behavioural
    assertion instead of a source substring.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(EVALUATION_RENDERER_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"evaluation renderer driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_an_unsupported_environment_renders_its_reason_and_styling(self) -> None:
        unsupported = self.data["unsupported"]
        self.assertIn("needs a decision pipeline", unsupported["text"])
        self.assertIn("UNSUPPORTED", unsupported["text"])
        self.assertIn("forge-benchmark-unsupported", unsupported["classes"])
        self.assertIn("No gameplay input path exists in this build.", unsupported["text"])
        self.assertIn("No objective verifier is attached.", unsupported["text"])
        self.assertIn("Catalog record only", unsupported["text"])

    def test_a_refused_readiness_renders_every_failing_row_and_the_blocking_ids(self) -> None:
        refused = self.data["refused"]
        self.assertIn("forge-validation bad", refused["cls"])
        self.assertIn("More than one window matches the target title", refused["text"])
        self.assertIn("Blocking checks: target_uniqueness, offline_eligibility", refused["text"])
        statuses = sorted(row["status"] for row in refused["rows"])
        self.assertEqual(statuses, ["fail", "pass", "unavailable"])
        self.assertIn("fail", refused["states"])
        self.assertIn("unavailable · not required", refused["states"])
        self.assertIn("No completed evaluation exists yet.", refused["baseline"])

    def test_an_aborted_report_names_its_gaps_and_its_unrecorded_readiness(self) -> None:
        report = self.data["report"]["text"]
        self.assertIn("aborted · excluded", report)
        self.assertIn("Readinessnot recorded for this evaluation", report)
        self.assertIn("Objective verifiedno — No objective verifier is attached.", report)
        self.assertIn("Gameplay control exercisedno", report)
        self.assertIn("No captured frame reference was retained for this run.", report)
        self.assertIn("Observation only: no decision pipeline and no gameplay input.", report)
        self.assertNotIn("0 pass", report)

    def test_a_recorded_readiness_verdict_still_renders_its_counts(self) -> None:
        report = self.data["reportWithCounts"]["text"]
        self.assertIn("13 pass · 0 warn · 0 fail · 1 unavailable", report)
        self.assertIn("completed · data complete", report)


COACH_DECISION_NEEDLE = """
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const approved = { status: 'approved', approval: { spec_digest: 'abc' }, spec: { digest: 'abc' } };
// A readiness refusal at run time keeps the approval: the configuration never
// changed, so the same approval may retry once the environment is ready.
const readyBlocked = { status: 'blocked', errors: [], approval: { spec_digest: 'abc' }, spec: { digest: 'abc' } };
const proposalBlocked = { status: 'blocked', errors: ['A repeat cannot declare a change.'], approval: null, spec: { digest: 'abc' } };
console.log(JSON.stringify({
  approve: {
    blocked: L.experimentCanApprove({ status: 'blocked' }),
    proposed: L.experimentCanApprove({ status: 'proposed' }),
    completed: L.experimentCanApprove({ status: 'completed' }),
    none: L.experimentCanApprove(null)
  },
  run: {
    approvedMatching: L.experimentCanRun(approved),
    // Changing the configuration after approval must revoke the run.
    digestMismatch: L.experimentCanRun({ status: 'approved', approval: { spec_digest: 'abc' }, spec: { digest: 'xyz' } }),
    noApproval: L.experimentCanRun({ status: 'approved', approval: null, spec: { digest: 'abc' } }),
    proposed: L.experimentCanRun({ status: 'proposed' }),
    readinessBlocked: L.experimentCanRun(readyBlocked),
    proposalBlocked: L.experimentCanRun(proposalBlocked),
    readinessBlockedChangedDigest: L.experimentCanRun(
      { status: 'blocked', errors: [], approval: { spec_digest: 'abc' }, spec: { digest: 'xyz' } }
    )
  },
  cancel: {
    blocked: L.experimentCanCancel({ status: 'blocked' }),
    running: L.experimentCanCancel({ status: 'running' }),
    rejected: L.experimentCanCancel({ status: 'rejected' }),
    completed: L.experimentCanCancel({ status: 'completed' })
  },
  status: {
    blocked: L.experimentStatusText({ status: 'blocked' }),
    withVerdict: L.experimentStatusText({ status: 'completed', results: { verdict: { label: 'not_comparable' } } }),
    none: L.experimentStatusText(null)
  },
  guardrail: {
    unverified: L.experimentGuardrailText({ metric: 'latency_p99_ms', direction: 'max', bound: 250, value: null, passed: null }),
    breached: L.experimentGuardrailText({ metric: 'capture_failures', direction: 'max', bound: 0, value: 3, passed: false }),
    within: L.experimentGuardrailText({ metric: 'frames', direction: 'min', bound: 10, value: 14, passed: true })
  },
  kind: {
    observed: L.coachKindLabel('observed'),
    derived: L.coachKindLabel('derived'),
    hypothesis: L.coachKindLabel('hypothesis'),
    unknown: L.coachKindLabel('anything-else')
  },
  tone: {
    improved: L.coachVerdictTone('improved'),
    regressed: L.coachVerdictTone('regressed'),
    notComparable: L.coachVerdictTone('not_comparable'),
    inconclusive: L.coachVerdictTone('inconclusive')
  },
  evidence: {
    metricUnavailable: L.coachEvidenceText({ kind: 'metric', metric: 'latency_median_ms', value: null }),
    capabilityMissing: L.coachEvidenceText({ kind: 'capability', name: 'objective_verifier', available: false }),
    comparison: L.coachEvidenceText({ kind: 'comparison', reasons: ['the observation interval differs'] })
  }
}));
"""


COACH_RENDERER_NEEDLE = r"""
const fs = require('fs');
function __mk(tag, cls, text) {
  return {
    tagName: tag,
    className: cls || '',
    _text: text == null ? '' : String(text),
    children: [],
    dataset: {},
    hidden: false,
    title: '',
    type: '',
    disabled: false,
    get textContent() {
      return this._text + this.children.map(function (c) { return c.textContent; }).join('');
    },
    set textContent(value) { this._text = String(value); this.children.length = 0; },
    set innerHTML(value) { this._text = ''; this.children.length = 0; },
    get innerHTML() { return ''; },
    get childElementCount() { return this.children.length; },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    click() {},
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
}
global.window = {};
var window = global.window;
var __byId = {};
var document = {
  getElementById: function (id) {
    if (!__byId[id]) __byId[id] = __mk(id);
    return __byId[id];
  },
  createElement: function (tag) { return __mk(tag); },
  addEventListener: function () {},
  body: __mk('body'),
  hidden: false
};
function el(tag, cls, text) { return __mk(tag, cls, text); }
function authHeaders() { return {}; }
function requestJson() { throw new Error('renderers must not touch the network'); }
eval(fs.readFileSync(process.argv[2], 'utf8'));
var L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
function walk(node, out, cls) {
  (node.children || []).forEach(function (child) {
    out.push({ cls: child.className || '', text: child.textContent,
               disabled: child.disabled === true, title: child.title || '' });
    walk(child, out, cls);
  });
  return out;
}
function inspect(id) {
  var box = document.getElementById(id);
  return { text: box.textContent, nodes: walk(box, [], '') };
}
var out = {};

// 1. Insufficient evidence, one derived finding, a candidate that is not a cause.
L.renderCoachAnalysis({
  read_only: true,
  subject: { session_id: 'sess1', evaluation_id: '', state: 'completed', end_reason: 'time limit',
    game: { display_name: 'Disposable Test Window' },
    scenario: { scenario_id: 'observation-baseline', version: 1 },
    target: { title: 'NC FORGE TEST' } },
  evidence: { timeline_events: 0, timeline_source: 'derived', frames: 0, frames_available: 0 },
  classification: { outcome: 'unclassified' },
  insufficient_evidence: { state: true,
    reason: 'The record contains no captured frames, no events and no telemetry.',
    what_would_help: ['Record one evaluation before analysing it.'] },
  findings: [
    { category: 'time_limit_reached', title: 'The budget ended the run', severity: 'info',
      severity_basis: 'A time limit is a normal ending.', kind: 'derived', direct: false,
      confidence: 'moderate', confidence_basis: 'One run, one ending.',
      explanation: 'The run ended because its declared duration elapsed.',
      evidence: [{ kind: 'metric', metric: 'frames', value: 20 }],
      alternatives: ['The window was minimised by the operator.'],
      limitations: ['One run only.'] },
    { category: 'objective_verifier_missing', title: 'No objective was verified', severity: 'info',
      severity_basis: 'A missing capability is structural.', kind: 'observed', direct: true,
      confidence: 'strong', confidence_basis: 'The evaluation states it.',
      explanation: 'No objective verifier is attached.',
      evidence: [{ kind: 'capability', name: 'objective_verifier', available: false }] }
  ],
  earliest_recoverable_error: { available: true, causation_claimed: false,
    candidate: { seq: 3, type: 'capture.failed', summary: 'Capture failed', elapsed_ms: 1200, severity: 'error' },
    basis: 'Sequence order in the recorded timeline.',
    uncertainty: 'Later symptoms follow it; that is order, not causation.',
    later_symptoms: [{ kind: 'event', seq: 9, summary: 'Session paused' }],
    counter_evidence: ['Capture recovered on the next frame.'],
    alternatives: ['The window was occluded.'] },
  recommendations: [
    { recommendation_id: 'rec-0123456789ab', finding_id: 'fnd-0123456789ab',
      category: 'persistence_failure', priority: 2, target_issue: 'Timeline events were not stored',
      status: 'proposed', action: 'Check the state store before repeating the run.',
      expected_effect: 'Timeline events are readable after a reload.', metric: 'timeline_events',
      metric_label: 'Timeline events', success_criterion: 'The timeline reports a stored source.',
      risk: 'none: reading only', effort: 'low', requires_approval: true,
      preconditions: ['The state store is writable.'],
      evidence: [{ kind: 'timeline_persistence', state: 'not_stored' }],
      experiment: { kind: '', variable: 'none', supported: false,
        reason: 'This needs an operator change rather than a benchmark run.' } }
  ],
  limitations: ['No objective verifier exists.'],
  note: 'Read-only; nothing was recorded.'
});
out.analysis = {
  subject: inspect('forgeCoachSubject'),
  earliest: inspect('forgeCoachEarliest'),
  findings: inspect('forgeCoachFindings'),
  recommendations: inspect('forgeCoachRecommendations'),
  status: document.getElementById('forgeCoachStatus').textContent,
  findingsCount: document.getElementById('forgeCoachFindingsCount').textContent,
  recommendationCount: document.getElementById('forgeCoachRecommendationCount').textContent
};

// 2. A BLOCKED experiment: reasons, no approval, nothing runnable.
function blockedRecord() {
  return {
    version: 1, id: 'exp1', status: 'blocked',
    reason: 'A repeat cannot declare a change.',
    errors: ['A repeat cannot declare a change.', 'repeat_count must be between 1 and 5.'],
    spec: { experiment_id: 'exp1', kind: 'repeat', game_id: 'disposable-window',
      scenario_id: 'observation-baseline', scenario_version: 1, target_title: 'NC FORGE TEST',
      baseline_configuration: { duration_seconds: 10, frame_interval_seconds: 0.5 },
      candidate_configuration: { duration_seconds: 10, frame_interval_seconds: 0.5 },
      repeat_count: 9, primary_metric: 'frames', primary_direction: 'increase', threshold: 10,
      guardrails: [{ metric: 'latency_p99_ms', direction: 'max', bound: 250 }],
      abort: { max_seconds_per_arm: 40, cancel_on: ['operator cancellation'] },
      subject: { finding_id: 'fnd-0123456789ab', recommendation_id: 'rec-0123456789ab' },
      digest: 'abc' },
    approval: null, arms: { baseline: [], candidate: [] }, results: null
  };
}
L.renderExperiment(blockedRecord());
var runButton = document.getElementById('forgeExperimentRunButton');
var approveButton = document.getElementById('forgeExperimentApproveButton');
var cancelButton = document.getElementById('forgeExperimentCancelButton');
out.blocked = {
  detail: inspect('forgeExperimentDetail'),
  results: inspect('forgeExperimentResults'),
  state: document.getElementById('forgeExperimentState').textContent,
  approveDisabled: approveButton.disabled, runDisabled: runButton.disabled,
  cancelDisabled: cancelButton.disabled, exportDisabled: document.getElementById('forgeExperimentExportButton').disabled
};

// 2b. A READINESS-blocked experiment: approved configuration, refused at run
// time, with the failing checks kept so a reload still explains the refusal.
var refused = blockedRecord();
refused.errors = [];
refused.reason = 'The readiness revalidation refused this run.';
refused.approval = { approved_at: '2026-10-18T10:00:00Z', approved_by: 'operator', spec_digest: 'abc' };
refused.preflight = { ready: false, blocking: ['target_identity'],
  reasons: ['No open window matches "NC FORGE TEST", so there is nothing to observe.'] };
L.renderExperiment(refused);
out.refused = {
  detail: inspect('forgeExperimentDetail'),
  state: document.getElementById('forgeExperimentState').textContent,
  runDisabled: runButton.disabled,
  approveDisabled: approveButton.disabled
};

// 3. A COMPLETED experiment whose comparison NF-04 REFUSED.
var completed = blockedRecord();
completed.status = 'completed';
completed.errors = [];
completed.reason = '';
completed.approval = { approved_at: '2026-10-18T10:00:00Z', approved_by: 'operator', spec_digest: 'abc' };
completed.arms = { baseline: [{ arm: 'baseline', index: 0, status: 'completed', evaluation_id: 'e1' }],
                   candidate: [{ arm: 'candidate', index: 0, status: 'completed', evaluation_id: 'e2' }] };
completed.results = {
  verdict: { label: 'not_comparable', reason: 'The arms were not one comparable basis.',
    improvement_claimed: false, gameplay_claimed: false, objective_verified: false,
    limited_to_single_run: true },
  sample: { baseline: 1, eligible_baseline: 1, candidate: 1, eligible_candidate: 1 },
  primary: { key: 'frames', label: 'Frames captured', unit: 'frames', direction: 'increase',
    threshold: 10, baseline: { value: 20, samples: 1 }, candidate: { value: 22, samples: 1 },
    delta: null, relative_percent: null, source: 'per_arm_description',
    reason: 'No delta: the runs are not compatible. The figures below are each arm\'s own recorded description, not an effect.',
    certified: false },
  secondary: [], guardrails: [{ metric: 'latency_p99_ms', direction: 'max', bound: 250,
    value: null, passed: null }],
  comparison: { compatible: false, reasons: ['The runs recorded a different observation interval.'] },
  failed_runs: [], limitations: ['Observation only: no decision pipeline.'],
  interventions: [], note: ''
};
L.renderExperiment(completed);
out.completed = {
  detail: inspect('forgeExperimentDetail'),
  results: inspect('forgeExperimentResults'),
  state: document.getElementById('forgeExperimentState').textContent,
  runDisabled: runButton.disabled, exportDisabled: document.getElementById('forgeExperimentExportButton').disabled
};

console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeCoachDecisionTests(unittest.TestCase):
    """The Coach panel's decisions, evaluated as code (no source grepping)."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(COACH_DECISION_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"coach decision driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_a_blocked_experiment_can_be_neither_approved_nor_run(self) -> None:
        approve = self.data["approve"]
        run = self.data["run"]
        self.assertFalse(approve["blocked"])
        self.assertFalse(approve["completed"])
        self.assertFalse(approve["none"])
        self.assertTrue(approve["proposed"])
        self.assertFalse(run["proposed"])
        self.assertTrue(run["approvedMatching"])

    def test_an_approval_is_bound_to_the_configuration_digest(self) -> None:
        run = self.data["run"]
        self.assertFalse(run["digestMismatch"])
        self.assertFalse(run["noApproval"])
        self.assertFalse(run["readinessBlockedChangedDigest"])

    def test_a_run_time_refusal_keeps_the_approval_but_a_proposal_block_does_not(self) -> None:
        run = self.data["run"]
        # A readiness refusal does not revoke the approval, so the same
        # experiment can be retried once the window is back.
        self.assertTrue(run["readinessBlocked"])
        # A proposal blocked for its own errors was never approved.
        self.assertFalse(run["proposalBlocked"])

    def test_cancel_stays_available_until_a_terminal_state(self) -> None:
        cancel = self.data["cancel"]
        self.assertTrue(cancel["blocked"])
        self.assertTrue(cancel["running"])
        self.assertFalse(cancel["rejected"])
        self.assertFalse(cancel["completed"])

    def test_the_status_text_carries_the_verdict_and_never_a_score(self) -> None:
        status = self.data["status"]
        self.assertIn("blocked", status["blocked"])
        self.assertIn("refusal reasons are below", status["blocked"])
        self.assertEqual(status["withVerdict"], "completed · not_comparable")
        self.assertEqual(status["none"], "none selected")

    def test_a_guardrail_that_measured_nothing_is_unverified_not_passed(self) -> None:
        guardrail = self.data["guardrail"]
        self.assertIn("not measured", guardrail["unverified"])
        self.assertIn("unverified", guardrail["unverified"])
        self.assertNotIn("within bound", guardrail["unverified"])
        self.assertIn("BREACHED", guardrail["breached"])
        self.assertIn("within bound", guardrail["within"])

    def test_every_finding_kind_is_named_and_unknown_is_explicit(self) -> None:
        kind = self.data["kind"]
        self.assertEqual(kind["observed"], "observed fact")
        self.assertEqual(kind["derived"], "derived metric")
        self.assertEqual(kind["hypothesis"], "hypothesis")
        self.assertEqual(kind["unknown"], "unknown")

    def test_a_refused_comparison_is_a_warning_never_a_success(self) -> None:
        tone = self.data["tone"]
        self.assertEqual(tone["improved"], "good")
        self.assertEqual(tone["regressed"], "bad")
        self.assertEqual(tone["notComparable"], "warn")
        self.assertEqual(tone["inconclusive"], "muted")

    def test_evidence_references_read_as_facts_not_payload_copies(self) -> None:
        evidence = self.data["evidence"]
        self.assertEqual(
            evidence["metricUnavailable"], "metric latency_median_ms = unavailable"
        )
        self.assertEqual(
            evidence["capabilityMissing"],
            "capability objective_verifier available: no",
        )
        self.assertIn("the observation interval differs", evidence["comparison"])


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class NovaForgeCoachRendererDomTests(unittest.TestCase):
    """What the NF-06 panel RENDERS, asserted on the built nodes.

    Three claims matter more than the layout: a thin record says it cannot be
    analysed, a candidate is never called a cause, and a refused comparison is
    shown as a description rather than an effect.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(COACH_RENDERER_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(PANEL_JS)],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"coach renderer driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_a_thin_record_says_insufficient_evidence_and_what_would_help(self) -> None:
        subject = self.data["analysis"]["subject"]
        self.assertIn("INSUFFICIENT EVIDENCE", subject["text"])
        self.assertIn("no captured frames, no events and no telemetry", subject["text"])
        self.assertIn("What would help: Record one evaluation before analysing it.", subject["text"])
        self.assertIn("Timeline0 event(s), source derived", subject["text"])
        self.assertEqual(
            self.data["analysis"]["status"], "analysed · 2 finding(s)"
        )

    def test_a_candidate_is_never_presented_as_the_cause(self) -> None:
        earliest = self.data["analysis"]["earliest"]
        self.assertIn("Earliest supported candidate: Capture failed", earliest["text"])
        self.assertIn("Causation claimedno", earliest["text"])
        self.assertIn("Later symptoms (order, not causation)", earliest["text"])
        self.assertIn("Capture recovered on the next frame.", earliest["text"])
        self.assertIn("The window was occluded.", earliest["text"])
        self.assertNotIn("the cause of", earliest["text"])

    def test_findings_name_their_classification_and_never_hide_it(self) -> None:
        findings = self.data["analysis"]["findings"]
        self.assertEqual(self.data["analysis"]["findingsCount"], "2")
        self.assertIn("derived metric", findings["text"])
        self.assertIn("observed fact", findings["text"])
        self.assertIn("derived metric — not a directly recorded fact", findings["text"])
        self.assertIn("observed fact — the record states it", findings["text"])
        self.assertIn("Alternative explanations", findings["text"])
        self.assertIn("Known limitations", findings["text"])

    def test_a_recommendation_without_a_runnable_experiment_disables_propose(self) -> None:
        recommendations = self.data["analysis"]["recommendations"]
        self.assertEqual(self.data["analysis"]["recommendationCount"], "1")
        self.assertIn("Timeline events were not stored", recommendations["text"])
        self.assertIn("required before anything runs", recommendations["text"])
        self.assertIn("Experiment: not available — This needs an operator change", recommendations["text"])
        propose = [node for node in recommendations["nodes"] if node["text"] == "Propose Experiment"]
        self.assertTrue(propose)
        for node in propose:
            self.assertTrue(node["disabled"])
            self.assertIn("operator change", node["title"])

    def test_a_blocked_experiment_shows_its_reasons_and_disables_the_run(self) -> None:
        blocked = self.data["blocked"]
        self.assertIn("blocked · the refusal reasons are below", blocked["state"])
        self.assertIn("Unsupported: A repeat cannot declare a change.", blocked["detail"]["text"])
        self.assertIn("Not approved.", blocked["detail"]["text"])
        self.assertIn("latency_p99_ms: not measured (at most 250) — unverified", blocked["detail"]["text"])
        self.assertTrue(blocked["approveDisabled"])
        self.assertTrue(blocked["runDisabled"])
        self.assertFalse(blocked["cancelDisabled"])
        self.assertTrue(blocked["exportDisabled"])
        self.assertIn("appears when both arms have finished", blocked["results"]["text"])

    def test_a_run_time_refusal_keeps_its_reasons_visible_after_a_reload(self) -> None:
        refused = self.data["refused"]
        self.assertIn("blocked · the refusal reasons are below", refused["state"])
        self.assertIn("Why the run was refused", refused["detail"]["text"])
        self.assertIn("No open window matches", refused["detail"]["text"])
        # The approval survives the refusal, so the run can be retried once the
        # window is back; approving again is not required (and stays disabled).
        self.assertFalse(refused["runDisabled"])
        self.assertTrue(refused["approveDisabled"])

    def test_a_refused_comparison_is_labelled_a_description_not_an_effect(self) -> None:
        completed = self.data["completed"]
        results = completed["results"]["text"]
        self.assertIn("NOT_COMPARABLE", results)
        self.assertIn("per-arm description (not an effect)", results)
        self.assertIn("Gameplay improvement claimed: no", results)
        self.assertIn("objective verified: no", results)
        self.assertIn("Limited to fewer than two eligible runs per side", results)
        self.assertIn("Why the comparison was refused", results)
        self.assertIn("The runs recorded a different observation interval.", results)
        self.assertIn("not an effect", results)
        # Completed is terminal: nothing re-runs, but the result is exportable.
        self.assertTrue(completed["runDisabled"])
        self.assertFalse(completed["exportDisabled"])


class NovaForgeCoachPanelContractTests(unittest.TestCase):
    """The panel exists, is wired to the NF-06 routes, and claims no score."""

    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")
        self.js = PANEL_JS.read_text(encoding="utf-8")

    def test_the_coach_tab_and_its_controls_are_in_the_panel(self) -> None:
        self.assertIn('id="forgeTabCoach"', self.html)
        self.assertIn('data-forge-tab="forgeCoachView"', self.html)
        for element_id in (
            "forgeCoachAnalyseButton",
            "forgeCoachEvaluation",
            "forgeCoachSession",
            "forgeCoachSubject",
            "forgeCoachEarliest",
            "forgeCoachFindings",
            "forgeCoachRecommendations",
            "forgeExperimentApproveButton",
            "forgeExperimentRejectButton",
            "forgeExperimentRunButton",
            "forgeExperimentCancelButton",
            "forgeExperimentResults",
            "forgeExperimentHistory",
        ):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', self.html)

    def test_the_panel_posts_every_coach_and_experiment_route(self) -> None:
        for path in (
            '"/novaforge/coach"',
            '"/novaforge/coach/analyse"',
            '"/novaforge/coach/recommendations/"',
            '"/novaforge/experiments"',
            '"/novaforge/experiments/"',
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.js)
        for action in ("/approve", "/reject", "/run", "/cancel"):
            with self.subTest(action=action):
                self.assertIn(action, self.js)

    def test_the_panel_states_that_it_is_read_only_and_promises_no_score(self) -> None:
        self.assertIn("Read-only analysis.", self.html)
        self.assertIn("it invokes no", self.html)
        self.assertIn("none of them promises a score improvement", self.html)
        self.assertNotIn("guaranteed improvement", self.html.lower())


if __name__ == "__main__":
    unittest.main()
