# Extra 1 — Animated Companion (NovaControl): re-audit and blocked implementation

- Date: 2026-11-28
- Status: **AUDITED, IMPLEMENTATION BLOCKED** — the Coastal/NovaControl working
  tree is being modified by another active task (DF-6 containment). Per the
  concurrency rules for this mission, no NovaControl source file was modified.
- Evidence classes: **code** (inspection), **test** (previous reports only —
  this run did not execute the NovaControl suites; see §E).

---

## A. What already exists (verified in the tree)

| Artefact | State |
| --- | --- |
| `src/novacontrol/web/static/js/companion.js` (727 lines) | untracked, present |
| `src/novacontrol/web/static/index.html` — `#companionStage` + controls | present (lines 105–132) |
| `src/novacontrol/web/static/js/render-utils.js` — `run()` lifecycle hooks | present (lines 80, 90, 97, 235, 245) |
| `src/novacontrol/web/static/js/voice.js` — listening / speech hooks | present (lines 25, 60, 141, 153, 184, 189) |
| `tests/test_extra1_companion.py` (525 lines, previously 24 tests) | untracked, present |
| `docs/EXTRA1_COMPANION_ACCEPTANCE_REPORT.md` | untracked; claims 18/18 PASS (2026-10-31, re-verified 2026-11-01) |
| `.nc_extra1/` session logs | present (Oct 31 – Nov 1) |

So Extra 1 in NovaControl is **implemented in the served FastAPI web workspace**,
not in the PySide6 shell. The previous report documents that deviation
explicitly: the repository's actively served UI is the web workspace, and the
PySide6 GUI (`novacontrol/gui`) is a separate, lazy, phase-11 dashboard.

## B. Gap analysis against this mission's requirements

| Requirement | Existing | Gap |
| --- | --- | --- |
| Eight states named IDLE / LISTENING / THINKING / ANALYZING / EXECUTING / SUCCESS / UNCERTAINTY / ERROR | `idle`, `listening`, `thinking`, `working`, `waiting`, `responding`, `speaking`, `error` | **Vocabulary mismatch.** `working` ≈ EXECUTING, `responding` ≈ SUCCESS; **ANALYZING** and **UNCERTAINTY** do not exist at all |
| Research begins → ANALYZING | every `explore.progress` step sets `working` | research and execution are indistinguishable |
| Confidence insufficient / abstention → UNCERTAINTY | no source; a low-confidence research answer still ends in `responding` | **missing state and source** |
| Verified completion → SUCCESS | `responding` is entered on any `run()` success, including a plan that changed nothing | success is not gated on verification |
| Actual failure → ERROR | `onRunError`, held (good) | unchanged |
| Task inactive → IDLE | dwell timer after success | unchanged |
| Workspace: current task, progress, agent, stage, tool activity, recent events, history, error detail | partial: `#homeActivity`, activity-state.js (729 lines), the operations board | no single companion workspace; no per-task pause/resume/cancel surface tied to the companion |
| Pause/resume/cancel via authorized APIs | operations panel already posts to `/operations/*` | not surfaced from the companion |
| Animation must not block the UI | rAF loop, paused when hidden, reduced-motion honored | unchanged (good) |

Two of the previous report's own "remaining" notes are still true and now
matter: the motion button does not reflect OS reduced-motion, and an imported
avatar cannot be reset from the UI.

## C. Why implementation is blocked

Observed during this session (21:17–21:25 IST):

- a `python -m pytest …` run started at 21:17:21 (web/API suite),
- `uvicorn` preview on port 6003 started at 21:21:29,
- a Playwright driver at 21:21:42,
- `tests/test_novaforge_policy.py` written 21:23, `tests/test_novaforge_gameplay_api.py` 21:24,
- `docs/DF6_TEST_SUITE_RECOVERY_REPORT.md` written 21:17,
- a fresh `python.exe` at 21:24:53.

DF-6 is documented in the tree as **PARTIAL** and requiring a live run
(`docs/DF6_CONTAINMENT_IMPLEMENTATION_REPORT.md`,
`docs/DF6_LIVE_UIA_VERIFICATION_REPORT.md`,
`docs/DF6_SESSION_TRANSPORT_READINESS.md`). Editing
`web/static/js/companion.js`, `render-utils.js`, `voice.js`, `index.html` or the
companion tests while that session is writing and running the suite would risk
both its evidence and this work. The mission's Part A forbids it.

No isolated worktree was created: `git worktree list` shows the shared tree plus
the NovaMarket branch worktree, and adding a third checkout on the same branch
would not isolate a running suite's expectations from a changed static tree.

## D. Implementation plan (ready to apply once the tree is quiet)

**D1. Extend the state table, do not replace it** (`companion.js`):

```js
const COMPANION_STATES = {
  idle:        { label: "Idle",        expression: "neutral",   tone: "idle" },
  listening:   { label: "Listening",   expression: "listening", tone: "live" },
  thinking:    { label: "Thinking",    expression: "thinking",  tone: "busy" },
  analyzing:   { label: "Analyzing",   expression: "working",   tone: "busy" },  // NEW
  executing:   { label: "Executing",   expression: "working",   tone: "busy" },  // was "working"
  success:     { label: "Success",     expression: "speaking",  tone: "ok" },    // was "responding"
  uncertain:   { label: "Uncertainty", expression: "waiting",   tone: "warn" },  // NEW
  error:       { label: "Error",       expression: "error",     tone: "error" },
};
```

Keep `working`/`responding`/`waiting`/`speaking` as **aliases** mapping onto the
new rows, so existing calls, CSS and tests keep working; add a test that every
legacy alias resolves.

**D2. Real sources for the two new states** (no timers):

- `render-utils.js` `appendLiveStep`/`onProgress`: if the event family is
  `explore` (research) → `analyzing`; if `command` → `executing`.
- New `NovaCompanion.onUncertain(detail)`: called from
  `render-explore.js` when the report carries no verified answer (e.g. an empty
  `sources` list with a refusal/`insufficient` marker) and from
  `renderCommand` when a plan returns `not_an_action` or
  `waiting_for_phone_bridge` **without** a token. Both are existing payload
  conditions already rendered by `render-panels.js`.
- `SUCCESS` only from `run()`'s success path when the payload is not a refusal
  and not a blocked plan. A refusal must not pass through `onRunResult`.

**D3. Transform guard**: a table-driven `COMPANION_TRANSITIONS` exactly like
NovaMarket's (success not reachable from `idle`/`listening`), and a
`companionRuntime.rejected` counter surfaced in the workspace.

**D4. Companion workspace panel** (`index.html` + a new
`js/companion-workspace.js`, or an extension of `activity-state.js`): current
task/stage from the activity adapter, recent steps, last error, and
pause/resume/cancel buttons that post the **existing** `/operations/*` task
controls — no new route.

**D5. Tests** (`tests/test_extra1_companion.py`): the existing Node-eval harness
pattern already used for `state.js`; add the eight-state table, the transition
matrix, alias resolution, the research→analyzing mapping, the refusal→uncertain
mapping, and "success requires a non-refusal payload". Keep the existing 24.

## E. Verification not performed (and why)

- The NovaControl suites were **not executed** in this session: doing so while
  another task is running the same suite in the same tree produces interleaved,
  unattributable results. The previous acceptance report's numbers are cited as
  history, not re-asserted.
- No live NovaControl UI was started by this session.

## F. Acceptance

| # | Requirement | Status |
| --- | --- | --- |
| 1 | Companion exists and reflects real events | **PASS (pre-existing, code-verified)** |
| 2 | All eight required states | **NOT MET** — ANALYZING and UNCERTAINTY absent; names differ |
| 3 | Deterministic transitions with refusal of illegal ones | **NOT MET** — no transition table |
| 4 | Success only after real success | **NOT MET** — any `run()` success shows success |
| 5 | Uncertainty represented honestly | **NOT MET** |
| 6 | Interactive workspace with authorized controls | **PARTIAL** — pieces exist, no unified workspace |
| 7 | Non-blocking animation, reduced motion, cleanup | **PASS (pre-existing)** |
| 8 | No DF-6 regression | **N/A — nothing changed** |

**Verdict: Extra 1 in NovaControl is INCOMPLETE against this mission's eight-state
requirement.** The work is planned in §D and blocked by Part A, not by any
technical unknown.
