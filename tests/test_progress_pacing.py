"""Progress-step pacing: fast research must still show each stage visibly.

appendLiveStep queues steps and reveals them with a minimum gap, and run()
awaits drainProgressSteps() before rendering the final result — so an instant
(cached) pipeline can never flash every stage AND the report in one frame.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest

STATIC = Path("src/novacontrol/web/static")
RENDER_UTILS = STATIC / "js" / "render-utils.js"

# JS shim: enough DOM for appendLiveStep's reveal path inside Node.
SHIM = r'''
function makeEl(tag, cls, text) {
  return { tag, className: cls || "", textContent: text || "", children: [],
           classList: { contains: (c) => (cls || "").split(/\s+/).includes(c) },
           appendChild(child) { this.children.push(child); return child; },
           querySelector(sel) {
             const cls = sel.replace(".", "");
             if (sel.startsWith(".") && (this.className || "").split(/\s+/).includes(cls)) return this;
             for (const c of this.children) { const hit = c.querySelector(sel); if (hit) return hit; }
             return null; },
           querySelectorAll(sel) {
             const cls = sel.replace(".", "");
             const out = [];
             const walk = (n) => { for (const c of n.children) { if ((c.className||"").split(/\s+/).includes(cls)) out.push(c); walk(c); } };
             walk(this); return out; } };
}
const byIdMap = {};
function byId(id) { return byIdMap[id] || null; }
function el(tag, cls, text) { return makeEl(tag, cls, text); }
function clearNode(n) { n.children.length = 0; }
'''

HARNESS = r'''
const fs = require("fs");
''' + SHIM + r'''
eval(fs.readFileSync("src/novacontrol/web/static/js/render-utils.js", "utf8"));
'''


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )


class ProgressStepPacingTests(unittest.TestCase):

    def _harness(self) -> str:
        return HARNESS

    def test_fast_run_reveals_steps_staged_not_in_one_frame(self) -> None:
        """Six steps arriving instantly reveal staged — first step immediately
        (instant feedback), the rest at a visible gap — and the final render
        waits (drainProgressSteps) until all are shown."""
        script = self._harness() + r'''
const target = makeEl("div", "");
target.classList.remove = () => {};
byIdMap["out"] = target;

// Loading panel present (run() created it), then six steps arrive instantly.
target.appendChild(el("div", "loading-indicator"));
const t0 = Date.now();
["s1","s2","s3","s4","s5","s6"].forEach(t => appendLiveStep("out", t));

// At most the FIRST step may appear in the same frame (instant feedback);
// flashing all six in one frame is the bug this pacing prevents.
const sameFrameCount = target.querySelectorAll(".progress-step").length;

// run() would await this before rendering the report.
drainProgressSteps().then(() => {
  const revealed = target.querySelectorAll(".progress-step").map(n => n.textContent);
  const totalMs = Date.now() - t0;
  let failed = 0;
  const check = (name, ok) => { if (!ok) { console.error("FAIL " + name); failed++; } };
  check("no-single-frame-flash", sameFrameCount <= 1);
  check("all-revealed", JSON.stringify(revealed) === JSON.stringify(["s1","s2","s3","s4","s5","s6"]));
  // First step immediate + 5 paced reveals: >= 5*350ms minus timer slop.
  check("paced-over-time", totalMs >= 5 * 350 - 100);
  console.error(failed ? failed + " failure(s)" : "pacing ok: " + totalMs + "ms for 6 steps");
  process.exit(failed ? 1 : 0);
});
'''
        proc = run_node(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pacing ok", proc.stderr)

    def test_drain_awaits_pending_queue_before_final_render(self) -> None:
        """drainProgressSteps() must not resolve while steps are still queued —
        the guarantee run() relies on before swapping in the report. The first
        step reveals immediately; the second must still be pending at 100ms."""
        script = self._harness() + r'''
const target = makeEl("div", "");
byIdMap["out"] = target;
target.appendChild(el("div", "loading-indicator"));
appendLiveStep("out", "step-one");
appendLiveStep("out", "step-two");

let drained = false;
drainProgressSteps().then(() => { drained = true; });

setTimeout(() => {
  // Well within the 350ms gap: step-one shown, step-two pending, no drain.
  let failed = 0;
  if (drained) { console.error("FAIL: drain resolved before reveal"); failed++; }
  const shown = target.querySelectorAll(".progress-step").map(n => n.textContent);
  if (JSON.stringify(shown) !== JSON.stringify(["step-one"])) {
    console.error("FAIL: expected only step-one at 100ms, got " + JSON.stringify(shown));
    failed++;
  }
  console.error(failed ? failed + " failure(s)" : "drain holds until reveal");
  process.exit(failed ? 1 : 0);
}, 100);
'''
        proc = run_node(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("drain holds until reveal", proc.stderr)

    def test_repeated_step_is_deduplicated(self) -> None:
        script = self._harness() + r'''
const target = makeEl("div", "");
byIdMap["out"] = target;
target.appendChild(el("div", "loading-indicator"));
appendLiveStep("out", "Searching...");
appendLiveStep("out", "Searching...");  // duplicate arrives fast
drainProgressSteps().then(() => {
  const shown = target.querySelectorAll(".progress-step").map(n => n.textContent);
  let failed = 0;
  if (shown.length !== 1) { console.error("FAIL: duplicate shown: " + JSON.stringify(shown)); failed++; }
  console.error(failed ? failed + " failure(s)" : "dedupe ok");
  process.exit(failed ? 1 : 0);
});
'''
        proc = run_node(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dedupe ok", proc.stderr)

    def test_gap_constant_is_humanly_visible(self) -> None:
        """The pacing constants must stay readable — a regression to 0ms would
        reintroduce the single-frame flash."""
        src = RENDER_UTILS.read_text(encoding="utf-8")
        self.assertIn("PROGRESS_STEP_GAP_MS = 350", src)
        self.assertIn("await drainProgressSteps()", src)


if __name__ == "__main__":
    unittest.main()
