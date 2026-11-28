"""Explore research history: the topics a browser actually searched.

Runs the REAL ``static/js/dom.js`` + ``static/js/render-explore.js`` in Node
against a small DOM shim, then drives the history the way the panel does:
record a completed search, re-record an immediate repeat, delete one entry, and
delete all. Storage is asserted each time, because a list that disagrees with
what a reload shows is the bug this feature exists to avoid.

``ExploreWiringTests`` pins the page/report wiring that cannot be observed
without a browser: the History control, the news Refresh control, and the fact
that a completed search (not a started one) is what enters history.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path("src/novacontrol/web/static")

_HARNESS = r"""
const fs = require('fs');
const nodes = new Map();

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    childNodes: [],
    className: '',
    _text: '',
    hidden: false,
    disabled: false,
    type: '',
    title: '',
    value: '',
    clicks: 0,
    attrs: {},
    handlers: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    click() { this.clicks += 1; (this.handlers.click || []).forEach((fn) => fn({})); },
    appendChild(child) { this.childNodes.push(child); return child; },
    removeChild(child) {
      const i = this.childNodes.indexOf(child);
      if (i >= 0) this.childNodes.splice(i, 1);
      return child;
    },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
  };
  Object.defineProperty(node, 'firstChild', { get() { return this.childNodes[0] || null; } });
  Object.defineProperty(node, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); this.childNodes = []; },
  });
  return node;
}

function byId(id) {
  if (!nodes.has(id)) nodes.set(id, makeNode('div'));
  return nodes.get(id);
}

['exploreHistoryList', 'exploreHistoryEmpty', 'exploreHistoryClearButton',
 'exploreHistoryCount', 'exploreInput', 'exploreButton', 'exploreHistory',
 'exploreHistoryButton'].forEach(byId);

const document = {
  getElementById: (id) => (nodes.has(id) ? nodes.get(id) : null),
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  createDocumentFragment: () => makeNode('#fragment'),
  querySelector: () => null,
  body: makeNode('body'),
};

const store = {};
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

const state = { lastExploredTopic: '', lastQuery: '' };
// The real showToast (dom.js) is used; read the latest toast node it appended.
const lastToast = () => {
  const body = document.body.childNodes;
  return body.length ? body[body.length - 1].textContent : '';
};

eval(fs.readFileSync('src/novacontrol/web/static/js/dom.js', 'utf8'));
eval(fs.readFileSync('src/novacontrol/web/static/js/render-explore.js', 'utf8'));

const KEY = 'novacontrol.exploreHistory';
const out = {};
const list = byId('exploreHistoryList');
const badge = byId('exploreHistoryCount');
const clearButton = byId('exploreHistoryClearButton');
const stored = () => JSON.parse(localStorage.getItem(KEY) || '[]');
const rows = () => list.childNodes.map((row) => ({
  topic: row.childNodes[0].childNodes[0].textContent,
  meta: row.childNodes[0].childNodes[1].textContent,
}));

out.initial = { stored: stored().length, rows: rows().length, badgeHidden: badge.hidden, clearDisabled: clearButton.disabled };

recordExploreHistory('first topic');
out.afterFirst = { stored: stored().map((e) => e.topic), rows: rows(), badge: badge.textContent, badgeHidden: badge.hidden };

recordExploreHistory('second topic');
out.afterSecond = { stored: stored().map((e) => e.topic), rows: rows().map((r) => r.topic) };

// An immediate repeat updates the row (and its timestamp) instead of duplicating it.
recordExploreHistory('FIRST TOPIC');
out.afterRepeat = { stored: stored().map((e) => e.topic), count: stored().length, rows: rows().map((r) => r.topic) };

// Blank input is not a search.
recordExploreHistory('   ');
out.afterBlank = stored().length;

// The cap holds: 40 records keep the newest 30.
for (let i = 0; i < 40; i += 1) recordExploreHistory('bulk topic ' + i);
out.cap = { count: stored().length, newest: stored()[0].topic, oldest: stored()[stored().length - 1].topic };

deleteExploreHistoryEntry('bulk topic 39');
out.afterDelete = { count: stored().length, has: stored().some((e) => e.topic === 'bulk topic 39'), toast: lastToast() };

clearExploreHistory();
out.afterClear = { count: stored().length, rows: rows().length, badgeHidden: badge.hidden, clearDisabled: clearButton.disabled, emptyHidden: byId('exploreHistoryEmpty').hidden };

// A row re-runs its search through the real panel controls.
recordExploreHistory('rerun me');
const row = list.childNodes[0];
row.childNodes[0].handlers.click[0]();
out.rerun = { input: byId('exploreInput').value, clicked: byId('exploreButton').clicks || 0 };

console.log(JSON.stringify(out));
"""


class ExploreHistoryRuntimeTests(unittest.TestCase):
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
            raise AssertionError(f"history harness failed:\n{proc.stderr}")
        cls.raw = proc.stdout

    def _out(self) -> dict:
        try:
            return json.loads(self.raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"harness produced no JSON: {self.raw!r}") from exc

    def test_a_recorded_search_lands_in_storage_and_the_list(self) -> None:
        out = self._out()
        self.assertEqual(out["initial"]["stored"], 0)
        self.assertEqual(out["afterFirst"]["stored"], ["first topic"])
        self.assertEqual(out["afterFirst"]["badge"], "1")
        self.assertFalse(out["afterFirst"]["badgeHidden"])
        self.assertEqual([row["topic"] for row in out["afterFirst"]["rows"]], ["first topic"])

    def test_newest_search_is_first(self) -> None:
        self.assertEqual(self._out()["afterSecond"]["stored"], ["second topic", "first topic"])
        self.assertEqual(self._out()["afterSecond"]["rows"], ["second topic", "first topic"])

    def test_an_immediate_repeat_updates_instead_of_duplicating(self) -> None:
        after = self._out()["afterRepeat"]
        self.assertEqual(after["count"], 2)
        # Case-insensitive match, and the row keeps the spelling just searched.
        self.assertEqual(after["stored"], ["FIRST TOPIC", "second topic"], "repeat must move to the front, not duplicate")

    def test_blank_input_is_not_a_search(self) -> None:
        self.assertEqual(self._out()["afterBlank"], 2)

    def test_history_is_capped_and_drops_the_oldest(self) -> None:
        cap = self._out()["cap"]
        self.assertEqual(cap["count"], 30)
        self.assertEqual(cap["newest"], "bulk topic 39")
        self.assertEqual(cap["oldest"], "bulk topic 10")

    def test_deleting_one_entry_removes_it_from_storage_and_the_list(self) -> None:
        after = self._out()["afterDelete"]
        self.assertEqual(after["count"], 29)
        self.assertFalse(after["has"])
        self.assertIn("removed", after["toast"])

    def test_delete_all_empties_storage_the_list_and_disables_its_control(self) -> None:
        after = self._out()["afterClear"]
        self.assertEqual(after["count"], 0)
        self.assertEqual(after["rows"], 0)
        self.assertTrue(after["badgeHidden"])
        self.assertTrue(after["clearDisabled"])
        self.assertFalse(after["emptyHidden"])

    def test_a_history_row_reruns_its_search(self) -> None:
        rerun = self._out()["rerun"]
        self.assertEqual(rerun["input"], "rerun me")
        self.assertEqual(rerun["clicked"], 1)


class ExploreWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = (STATIC / "index.html").read_text(encoding="utf-8")
        self.app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.render = (STATIC / "js" / "render-explore.js").read_text(encoding="utf-8")

    def test_the_explore_panel_ships_history_and_refresh_controls(self) -> None:
        for element_id in (
            "exploreHistoryButton",
            "exploreHistory",
            "exploreHistoryList",
            "exploreHistoryClearButton",
            "exploreHistoryEmpty",
            "exploreHistoryCount",
            "trendingRefreshButton",
            "trendingStatus",
        ):
            self.assertIn(f'id="{element_id}"', self.index, f"index.html is missing #{element_id}")

    def test_history_is_recorded_on_a_completed_search_not_a_started_one(self) -> None:
        # recordExploreHistory must live in run()'s onSuccess, next to the topic
        # tracking — a failed research never enters history.
        self.assertIn("recordExploreHistory(topic);", self.app)
        self.assertIn("onSuccess: (report) => {", self.app)
        success_block = self.app.split("onSuccess: (report) => {", 1)[1].split("},", 1)[0]
        self.assertIn("recordExploreHistory(topic)", success_block)

    def test_refresh_forces_a_server_refetch_and_hides_topics_already_shown(self) -> None:
        self.assertIn('params.set("refresh", "true")', self.app)
        self.assertIn('params.set("exclude", shown.join(","))', self.app)
        self.assertIn("loadTrendingTopics(true)", self.app)

    def test_the_panel_setup_runs_at_startup(self) -> None:
        self.assertIn("setupExplorePanel();", self.app)
        self.assertIn("setupExploreHistory();", self.app)


if __name__ == "__main__":
    unittest.main()
