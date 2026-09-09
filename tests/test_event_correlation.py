"""Run-scoped correlation ids on progress events.

Two concurrent activities of the same family (two researches, two approved
command executions) used to interleave into ONE progress box in the UI: every
event family had a single slot, and progress events carried no run identity.
The contract now is three-hop:

  1. SOURCE  — explore progress events carry the ExploreRequest id; command
     progress events carry the execution's correlation id (client-supplied or
     server-minted);
  2. RELAY   — every SSE frame carries a correlation_id (run-scoped when the
     source set one, the event's own id otherwise);
  3. UI      — the client registers a slot per run keyed by its id and routes
     each frame to the owning target (driven in Node against the real JS).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from novacontrol.core.events import EventBus, Event
from novacontrol.explore import ExploreRequest
from novacontrol.explore.service import ExploreService
from novacontrol.application import NovaControlApplication

try:  # pragma: no cover - environment guard
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

from conftest import FakeSearchProvider, FakeVideoProvider


def _service(bus: EventBus) -> ExploreService:
    return ExploreService(
        search_provider=FakeSearchProvider(),
        video_provider=FakeVideoProvider(),
        event_bus=bus,
    )


class ExploreCorrelationTests(unittest.IsolatedAsyncioTestCase):
    """Every explore.progress event of one research shares that request's id."""

    async def test_progress_events_carry_the_request_id(self) -> None:
        bus = EventBus()
        service = _service(bus)
        events: list[Event] = []

        async def capture(event: Event) -> None:
            events.append(event)

        await bus.subscribe("explore.progress", capture)
        request = ExploreRequest("correlated research topic", id="run-abc123")
        await service.research(request)

        self.assertTrue(events, "progress events must be published")
        ids = {e.payload.get("correlation_id") for e in events}
        self.assertEqual(ids, {"run-abc123"}, "every frame of ONE research shares its request id")

    async def test_two_researches_do_not_share_ids(self) -> None:
        bus = EventBus()
        service = _service(bus)
        events: list[Event] = []

        async def capture(event: Event) -> None:
            events.append(event)

        await bus.subscribe("explore.progress", capture)
        await service.research(ExploreRequest("topic one", id="run-1"))
        await service.research(ExploreRequest("topic two", id="run-2"))

        first = {e.payload.get("correlation_id") for e in events if e.payload.get("topic") == "topic one"}
        second = {e.payload.get("correlation_id") for e in events if e.payload.get("topic") == "topic two"}
        self.assertEqual(first, {"run-1"})
        self.assertEqual(second, {"run-2"})
        self.assertNotIn(None, first | second)


class CommandCorrelationTests(unittest.IsolatedAsyncioTestCase):
    """One execution's progress frames share its run-scoped correlation id."""

    async def test_supplied_id_tags_every_progress_frame(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            from novacontrol.desktop import NoopDesktopRunner

            app.desktop.runner = NoopDesktopRunner()
            seen: list[Event] = []

            async def capture(event: Event) -> None:
                seen.append(event)

            await app.event_bus.subscribe("command.progress", capture)
            await app.start()
            try:
                plan = app.plan_desktop_command("open notepad")
                result = await app.execute_desktop_command(
                    "open notepad", approval_token=plan["approval"]["token"], correlation_id="client-run-77"
                )
            finally:
                await app.stop()

        self.assertEqual(result["status"], "executed")
        self.assertTrue(seen)
        ids = {e.payload.get("correlation_id") for e in seen}
        self.assertEqual(ids, {"client-run-77"})
        commands = {e.payload.get("command") for e in seen}
        self.assertEqual(commands, {"open notepad"})

    async def test_blank_id_mints_one_server_side(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = NovaControlApplication(data_dir=temp_dir)
            from novacontrol.desktop import NoopDesktopRunner

            app.desktop.runner = NoopDesktopRunner()
            seen: list[Event] = []

            async def capture(event: Event) -> None:
                seen.append(event)

            await app.event_bus.subscribe("command.progress", capture)
            await app.start()
            try:
                plan = app.plan_desktop_command("open notepad")
                await app.execute_desktop_command("open notepad", approval_token=plan["approval"]["token"])
            finally:
                await app.stop()

        ids = {e.payload.get("correlation_id") for e in seen}
        self.assertEqual(len(ids), 1, "one execution mints exactly one shared id")
        minted = next(iter(ids))
        self.assertTrue(minted and minted != "")


class SseCorrelationTests(unittest.TestCase):
    """The /events/stream frame builder always attaches a correlation_id."""

    def test_run_scoped_payload_id_wins(self) -> None:
        from novacontrol.api.app import sse_frame

        event = Event(
            type="command.progress",
            payload={"step": "executing", "detail": "d", "correlation_id": "run-1"},
            source="command",
            correlation_id="event-own-id",
        )
        frame = sse_frame(event)
        data = json.loads(frame.split("data: ", 1)[1])
        self.assertEqual(data["correlation_id"], "run-1", "the run-scoped payload id wins")
        self.assertEqual(data["event_type"], "command.progress")
        self.assertTrue(frame.startswith("event: command.progress\n"))

    def test_event_own_id_is_the_fallback(self) -> None:
        from novacontrol.api.app import sse_frame

        event = Event(type="learn.completed", payload={"title": "done"}, source="activity")
        frame = sse_frame(event)
        data = json.loads(frame.split("data: ", 1)[1])
        self.assertEqual(data["correlation_id"], event.correlation_id)
        self.assertTrue(event.correlation_id, "Event mints a per-event id by default")


class UiRoutingTests(unittest.TestCase):
    """The REAL render-utils.js routes frames to the owning run's panel."""

    NODE = shutil.which("node")

    def _run_node(self) -> subprocess.CompletedProcess[str]:
        if self.NODE is None:
            self.skipTest("node is not installed")
        script = r'''
const fs = require("fs");
function makeEl(tag, cls, text) {
  const node = {
    tagName: String(tag || "div").toUpperCase(), className: cls || "",
    children: [], textContent: text != null ? String(text) : "",
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    querySelector(sel) {
      const clsName = String(sel).replace(/^\./, "");
      for (const c of this.children) {
        if ((sel.startsWith(".") && c.className === clsName) || (sel === ".loading-indicator" && c.className === "loading-indicator")) return c;
      }
      return null;
    },
    querySelectorAll(sel) {
      const clsName = String(sel).replace(/^\./, "");
      return this.children.filter((c) => c.className === clsName);
    },
    classList: { add() {}, remove() {}, contains() { return false; } },
  };
  return node;
}
global.document = {
  getElementById(id) { return this._byId[id] || null; },
  _byId: {},
  createElement: makeEl, createTextNode: (t) => ({ textContent: t }),
  createDocumentFragment() { return makeEl("#fragment"); },
};
function byId(id) { return document._byId[id] || null; }
global.byId = byId;
const state = {};
global.state = state;
function el(tag, cls, text) { return makeEl(tag, cls, text); }
global.el = el;
function showToast() {}
global.showToast = showToast;
// Real code under test:
eval(fs.readFileSync("src/novacontrol/web/static/js/render-utils.js", "utf8"));

function fakeEvent(type, payload) {
  return { type, data: JSON.stringify(payload) };
}

let failed = 0;
function check(name, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error("FAIL " + name + ": " + JSON.stringify(got) + " !== " + JSON.stringify(want));
    failed++;
  }
}

// Two concurrent command runs in two panels:
registerActivitySlot("run-a", "command", "panelA");
registerActivitySlot("run-b", "command", "panelB");
const evA = fakeEvent("command.progress", { step: "executing", detail: "A-step-1", correlation_id: "run-a" });
const evB = fakeEvent("command.progress", { step: "executing", detail: "B-step-1", correlation_id: "run-b" });
const evA2 = fakeEvent("command.progress", { step: "executing", detail: "A-step-2", correlation_id: "run-a" });
// Panels must exist with a loading indicator for appendLiveStep to queue.
document._byId.panelA = makeEl("div"); document._byId.panelA.appendChild(makeEl("div", "loading-indicator"));
document._byId.panelB = makeEl("div"); document._byId.panelB.appendChild(makeEl("div", "loading-indicator"));

// Drain the paced queue synchronously: each showNextProgressStep() reveals one
// queued row into its panel (progressQueue is eval-scoped `let`, so the DOM is
// the observable surface, not the queue).
function drain() { for (let i = 0; i < 12; i++) showNextProgressStep(); }
function rowsOf(panelId) {
  const box = document._byId[panelId].querySelector(".explore-progress");
  return box ? box.querySelectorAll(".progress-step").map((r) => r.textContent) : [];
}onActivityEvent(evA); onActivityEvent(evB); onActivityEvent(evA2);
drain();
check("routing: A run got only A rows", rowsOf("panelA"), ["A-step-1", "A-step-2"]);
check("routing: B run got only B rows", rowsOf("panelB"), ["B-step-1"]);

// An explore frame with an unknown id is NOT grabbed by command slots.
onActivityEvent(fakeEvent("explore.progress", { detail: "E0", correlation_id: "run-explore-0" }));
drain();
check("cross-family unknown id not adopted by command slots", rowsOf("panelA").length + rowsOf("panelB").length, 3);

// Unknown-id command frame (CLI-initiated run in another client): adopts the
// first open same-family slot so the steps still render somewhere.
const evX = fakeEvent("command.progress", { detail: "X", correlation_id: "run-unknown" });
onActivityEvent(evX);
drain();
check("unknown id adopts same-family slot", rowsOf("panelA"), ["A-step-1", "A-step-2", "X"]);

// After run-a releases, the next unknown frame adopts the surviving slot —
// documented best-effort adoption; exact ids never cross.
releaseActivitySlot("run-a");
const evX2 = fakeEvent("command.progress", { detail: "X2", correlation_id: "run-unknown-2" });
onActivityEvent(evX2);
drain();
check("adoption falls to the surviving same-family slot", rowsOf("panelB"), ["B-step-1", "X2"]);

// With NO open same-family slot, unknown and released-id frames drop.
releaseActivitySlot("run-b");
onActivityEvent(evA); onActivityEvent(evX);
drain();
check("no open slot: unknown and released frames dropped",
  rowsOf("panelA").length + rowsOf("panelB").length, 5);

// Chat (ask) slots accept explore frames — a chat run's research has a
// server-minted id the client never saw, so adoption is how its steps render.
registerActivitySlot("run-chat", "ask", "chatStream");
document._byId.chatStream = makeEl("div"); document._byId.chatStream.appendChild(makeEl("div", "loading-indicator"));
onActivityEvent(fakeEvent("explore.progress", { step: "searching", detail: "E-step", correlation_id: "run-explore" }));
drain();
check("explore frame adopts open ask slot", rowsOf("chatStream"), ["E-step"]);

// newCorrelationId is unique enough for slot keys.
const ids = new Set([newCorrelationId(), newCorrelationId(), newCorrelationId()]);
check("newCorrelationId uniqueness", ids.size, 3);

// Bounded slot registry: 14 registrations with UNIQUE targets; the 12 newest
// survive (FIFO eviction). An exact hit returns its own target; an evicted id
// falls through to adoption and returns a DIFFERENT slot's target.
for (let i = 0; i < 14; i++) registerActivitySlot("run-fill-" + i, "command", "z" + i);
check("newest slot exact-hits", activitySlotForEvent("command", "run-fill-13").targetId, "z13");
check("oldest slot was evicted", activitySlotForEvent("command", "run-fill-0").targetId === "z0", false);
let exactHits = 0;
for (let i = 0; i < 14; i++) {
  const slot = activitySlotForEvent("command", "run-fill-" + i);
  if (slot && slot.targetId === "z" + i) exactHits++;
}
check("exactly the 12 newest survive", exactHits, 12);

console.error(failed ? failed + " routing case(s) failed" : "all correlation routing cases passed");
process.exit(failed ? 1 : 0);
'''
        return subprocess.run([self.NODE, "-e", script], capture_output=True, text=True)  # type: ignore[list-item]

    def test_ui_routes_frames_to_the_owning_run(self) -> None:
        proc = self._run_node()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all correlation routing cases passed", proc.stderr)


if __name__ == "__main__":
    unittest.main()
