# Extra 4 — Intelligent Thinking Orbs & Live AI Activity Visualization

**Date:** 2026-11-04  
**Scope:** the served FastAPI web workspace in `src/novacontrol/web/static`  
**Overall verdict:** **PARTIAL** — implemented and live-verified on the real Chat, Explore and active-task paths; the voice visual lifecycle was observed live but a completed voice capture was blocked by this preview's microphone/speech service, and an equal-window performance comparison was invalidated by the preview tab suspending animation frames. No claim is made for those two unverified halves.

No commit or push was made.

---

## 1. Repository inspection

**Status: PASS**

The repository already has a served browser workspace (`src/novacontrol/web/static`, FastAPI `create_app`) and an optional PySide6 desktop shell (`src/novacontrol/gui`).

- The live Chat, Explore, voice, tracked-task, Extra 1 companion and Extra 2 Atrium surfaces are all in the **web workspace**. The PySide6 shell has Chat/Explore/Tasks but no voice and no app-wide SSE rendering.
- The app already has one app-wide SSE activity channel, `GET /events/stream`, carrying typed lifecycle events (`intent.detected`, `decision.created`, `plan.created`, `task.*`, `tool.*`, `verification.*`, `recovery.*`, `explore.progress`, `command.progress`, …).
- `run()` in `render-utils.js` already owns the local request/loading lifecycle for Chat, Explore and commands.
- `voice.js` already owns Web Speech acquisition and failure states.
- `GET /status.app.tasks` already exposes real tracked tasks.
- Extra 1 (companion) and Extra 2 (Atrium/isometric map) are separate presentation surfaces; neither needed to be rewritten.

Extra 4 was therefore integrated into the **served web workspace**, where the real events, voice and existing companions live. This is not a migration of the PySide6 shell to React, and no second planner, agent loop, event bus or activity channel was added.

## 2. Existing architecture and reuse

**Status: PASS**

The feature consumes existing runtime facts and changes no backend route or event vocabulary.

- **One SSE connection:** `render-utils.js` still creates exactly one `EventSource`; the Extra 4 listeners are attached to that connection.
- **One local lifecycle:** `runStart`, `runResult` and `runError` are called from the existing `run()` lifecycle. No request flow was duplicated.
- **One voice lifecycle:** `voice.js` publishes listening, processing, speaking, error and end states through the adapter.
- **One task feed:** `app.js` continues to read `/status.app.tasks` and passes the same snapshot to `NovaActivity.setTasks`.
- **No fabricated progress:** each displayed activity comes from an SSE frame, a local request lifecycle, a voice event, or a tracked-task snapshot. The adapter has no interval that invents stages.

The browser UI remains vanilla HTML/CSS/ES modules. No npm, React, bundler or new runtime dependency was introduced.

## 3. Library and licence inspection

**Status: PASS (not used as a dependency)**

The requested `thinking-orbs` package was inspected before implementation.

- Licence: **MIT**, © 2026 Jakub Antalik.
- Public API: React 18+ components with nine visual states.
- It ships no vanilla ES-module entry point that can be mounted into this hand-written, no-build host.
- No package code was vendored or copied.

The visual direction (a dotted spherical field with distinct state motion) was implemented as an **original Canvas 2D renderer**. The repository keeps its existing no-npm architecture; the licence obligation for third-party code is not triggered because none is distributed.

## 4. What was built

**Status: PASS**

Four placements share one visual language and one state source:

| Placement | Where | Size | Purpose |
|---|---|---|---|
| Main | Chat workspace, above the voice strip | 216 px (176–218 fluid) | Primary live activity: request, planning, tools, verification, completion |
| Explore | Top of the Explore panel | 48 px | Research-specific progress, isolated from unrelated work |
| Voice | Chat voice strip | 74 px | Listening, processing speech, speaking, blocked/error |
| Active task | System panel, above Tracked Tasks | 44 px | Task/tool/verification lifecycle plus tracked-task fallback |

All four are real DOM placements, not a single orb cloned visually. Each is a canvas managed by the shared renderer; each caption is the adapter's live label and description.

### Files created

- `src/novacontrol/web/static/js/activity-state.js` — central activity adapter and four scoped views.
- `src/novacontrol/web/static/js/thinking-orb.js` — original Canvas 2D spherical renderer, shared loop and lifecycle.
- `src/novacontrol/web/static/js/orbs.js` — binds the four placements to the adapter.
- `tests/test_extra4_orbs.py` — Node adapter/renderer harness and page wiring assertions.
- `docs/EXTRA4_THINKING_ORBS_ACCEPTANCE_REPORT.md` — this report.

### Files modified

- `src/novacontrol/web/static/index.html` — four placement cards, canvas hosts and module tags (using the existing `__NC_ASSET_VERSION__` marker).
- `src/novacontrol/web/static/styles.css` — layout, state ink, responsive rules and reduced-motion rule.
- `src/novacontrol/web/static/js/render-utils.js` — forwards the existing SSE frames and run lifecycle to the adapter; still one EventSource.
- `src/novacontrol/web/static/js/voice.js` — forwards the existing speech lifecycle.
- `src/novacontrol/web/static/app.js` — mounts the orbs and feeds the tracked-task snapshot.
- `README.md`, `docs/STATUS.md` — feature/status notes.
- `docs/screenshots/extra4-*.png` — live captures.

No backend Python module, API route, event schema or scheduler was changed.

## 5. Central activity adapter

**Status: PASS**

`activity-state.js` is the only owner of orb activity. It exposes one store and four scoped views (`main`, `explore`, `voice`, `task`).

Each normalized activity carries:

`id`, `state`, `orb`, `label`, `description`, `source`, `taskId`, `sessionId`, `correlationId`, `status`, `error`, `scopes`, `local`, `priority`, `startedAt`, `updatedAt`, `seq`, `lastEventSeq`, `terminal`, `stale`.

The adapter provides:

- `ingest(type, payload, meta)` for app-wide SSE frames;
- `runStart` / `runResult` / `runError` for this tab's request lifecycle;
- `voiceListening` / `voiceProcessing` / `voiceSpeaking` / `voiceError` / `voiceEnd`;
- `setTasks(snapshot)` for `/status.app.tasks`;
- `setTransport` for live-channel loss/reconnection;
- `subscribe` / `getState` / `getScopes` / `diagnostics`.

Views are resolved independently. An Explore activity feeds `main` and `explore`; it cannot appear as a voice capture. Task/tool activity feeds `main` and `task`. The voice scope is separate from the task scope, and a live microphone temporarily owns `main` only while Listening or in a persistent error.

## 6. State model and event mapping

**Status: PASS**

The normalized activity states are `idle`, `listening`, `thinking`, `searching`, `planning`, `connecting`, `working`, `solving`, `recovering`, `composing`, `waiting`, `completed`, `cancelled`, `error` and `unknown`. They map to 13 renderer modes: breathing, listening, searching, weaving, shaping, connecting, working, solving, composing, waiting, completed, error and unknown.

Representative real mappings:

| Runtime fact | Displayed activity |
|---|---|
| `task.started` (conversational kind) | Thinking — understanding and choosing an approach |
| `intent.detected` | Understanding — classified intent |
| `context.resolved` | Resolving context |
| `decision.created` | Choosing an approach |
| `plan.created` | Planning — step count from the real plan |
| `tool.selected` | Selecting a tool — real tool name |
| `tool.started` | Working — running the named tool |
| `tool.completed` | Checking the result |
| `tool.failed` | Tool failed — error text |
| `verification.started` | Evaluating |
| `verification.completed` | Result verified, or Verification failed |
| `recovery.started` | Recovering — attempt/ceiling |
| `recovery.completed` | Recovered — real outcome |
| `task.completed` / `task.failed` / `task.cancelled` | Completed / Failed / Cancelled |
| `explore.progress` (`searching`, `sources_found`, `searching_videos`, `videos_found`, `synthesizing`, `complete`, `retrying`, …) | Search-specific label and real detail |
| `command.progress` / `command.completed` | Running action / Action complete |
| Local `run()` start/result/error | Request-specific start/completion/failure, with per-family completion copy |
| Voice `onstart` / processing / speaking / error / end | Listening / Processing speech / Speaking / Voice unavailable / Voice idle |
| `/status.app.tasks` non-terminal task | Task in progress fallback, real title and id |

The Explore family therefore says **“Research complete / The research report is ready.”**, not the generic chat completion copy. Chat completion says “Completed / The answer is ready.” and command completion says “Action complete / The approved action finished.”

## 7. Main workspace orb

**Status: PASS (live)**

The main card is a 216 px sphere at the top of Chat, with a live eyebrow, a large state label, a one-line description and a status pill. The sphere is flanked by readable text rather than being a decorative background.

Live evidence:

- Real Chat request **“What is the capital of Japan?”** moved through **Thinking (32 ms) → Selecting a tool (592 ms) → Completed (6,591 ms)**.
- The card rendered the real tool-selection state in `docs/screenshots/extra4-main-chat.png`.
- The final answer was **“The capital of Japan is Tokyo.”**; the main orb then settled to idle.
- The first live attempt exposed a real crash: assigning `timer.extra4Settle` to a numeric `setTimeout` handle threw `Cannot create property 'extra4Settle' on number '7'` and interrupted Chat. The adapter now uses a plain `settleTimer` variable. The asset test pins that the old pattern cannot return.

## 8. Explore orb

**Status: PASS (live)**

The Explore orb is a compact 48 px sphere above the research input. It is a view of the existing research pipeline's own progress frames, not a second research workflow.

Two live runs were performed:

- **“How does photosynthesis work”** — Starting research → Searching → Reading sources → Searching videos → Researching → Checking videos → Composing → Research complete; final report 7,234 characters.
- **“Why do volcanoes erupt”** — completed in 4.4 s on a fresh run (2.96 s cached) with sources read; completion showed **“Research complete / The research report is ready.”**

The second run exposed a status-normalization defect: the visible label was `completed` but the normalized `status` field still carried the old `searching` value. `upsertLocal` and `finishLocal` now set `status` (and clear stale error text), and voice end explicitly restores `status: idle`. The focused suite passes this case.

## 9. Voice orb

**Status: PARTIAL**

The voice card is 74 px and sits beside the real voice status. It is driven only by the existing Web Speech lifecycle:

- `onstart` (microphone acquired) → Listening;
- speech processing → Processing speech;
- speech output → Speaking;
- persistent failure codes → Voice unavailable with the actionable message;
- `onend`/idle → Voice idle.

Live evidence:

- The first attempt genuinely entered **Listening** for about 1.5 seconds, then the browser speech service failed and returned to idle (a transient `network` failure).
- A later attempt failed in **459 ms** with persistent **“Microphone blocked”**. The main orb and voice card both held the error state; the UI did not silently revert to “Voice ready”. Captured in `docs/screenshots/extra4-voice-error.png`.
- No completed voice transcript or voice-initiated answer was obtained in this preview. That is a **BLOCKED runtime capability**, not a fabricated success: the browser did not grant a usable microphone/speech service.

The adapter wiring itself is covered: listening priority, processing, speaking, persistent error, voice end returning to idle, and the main-scope handback are pinned in the Node harness.

## 10. Active-task orb

**Status: PASS (live for Chat task path; no separate agent task was run)**

The 44 px task orb lives in the System panel above Tracked Tasks. It follows `task.*`, `tool.*`, `verification.*` and `recovery.*` frames in the `task` scope, and falls back to a non-terminal `/status.app.tasks` record.

Live evidence:

- At the end of the real Chat request, the task scope reported **completed** alongside main; both came from the same request lifecycle.
- In the earlier live poll after completion, the task card returned to **“No active task / Idle”**.
- The `task`/`tool` event mapping is unit-tested against the real adapter, including `tool.selected → Selecting a tool` in both scopes.

The fallback branch is unit-tested against a running tracked task (`Task in progress`, real id). The **task orb's intermediate tool-selection label was not separately observed live**; only its completed/end state was. A separate agent task was not started in this pass, so the task orb's agent-specific wording is not claimed as live-verified.

## 11. Stale, duplicate, out-of-order, cancellation and failure handling

**Status: PASS in implementation and focused tests; PARTIAL live**

- **Duplicates:** identical events within 700 ms are ignored by a bounded dedupe key.
- **Out-of-order frames:** an explicit older `meta.seq` cannot overwrite a newer activity. Equal-millisecond events are ordered by the monotonic `seq` tiebreaker, not Map insertion order.
- **Late terminal frames:** a progress frame received after a terminal frame for the same correlation is rejected; only a genuinely new `task.started` can reopen a terminal activity.
- **Staleness:** a running activity with no update for two minutes (five minutes for task scope) becomes **Unknown** with an explanation instead of pretending it is still running. The stale scan runs every 15 seconds.
- **Terminal dwell:** Completed/Failed/Cancelled remain readable for 2.4 seconds, then the scope settles to idle unless newer work takes over.
- **Transport loss:** EventSource reconnection sets the main view to Unknown/Reconnecting rather than leaving a stale Working orb; a successful open clears it.
- **Failure:** tool, verification, task and voice failures become the error state with the real message.
- **Cancellation:** `task.cancelled` maps to Cancelled; a cancelled local run cannot be reopened by a late progress frame.

The duplicate/out-of-order/late-terminal/stale paths are pinned in `tests/test_extra4_orbs.py`. **Live cancellation of a long-running task was not exercised**, so that path is not claimed as runtime-verified.

## 12. Performance, visibility and motion

**Status: PARTIAL**

Implemented performance controls:

- one shared `requestAnimationFrame` loop for all instances;
- 30 fps ceiling for large orbs, 24 fps for compact orbs (<70 px);
- `IntersectionObserver` pauses an offscreen placement;
- `document.hidden` stops scheduling;
- device-pixel-ratio capped at 2 (a 216 px orb uses a 432×432 backing canvas, not 864×864 at DPR 4);
- static/reduced-motion states draw a representative frame and do not loop;
- one shared settle timer and one stale timer, not one per placement;
- no canvas filters, WebGL or per-frame DOM writes.

Measured in this live preview:

- Four instances, **avg draw 0.476–0.488 ms**, **max 6.4 ms** across 1,613 and 3,254 rendered frames.
- The main canvas measured **215×215 CSS px** at the 1440 px viewport (sub-pixel layout rounding of the 216 px target).
- A 5-second `PerformanceObserver` long-task window reported **0 long tasks**, but only **1 animation frame / 2 draws** occurred because the embedded preview tab had suspended `requestAnimationFrame`. That makes the before/after CPU comparison **invalid**, not a pass. No lower CPU claim is made.

The measured per-draw cost is the useful figure. It is not converted into a total CPU percentage because that would require a valid frame-rate window the preview did not provide. A final 2-second probe reported `document.visibilityState === "hidden"` and **0 rAF callbacks**, confirming why the embedded preview cannot supply that window.

## 13. Reduced motion, accessibility and responsive behavior

**Status: PASS in code/tests; PARTIAL visual sweep**

- Canvas is `aria-hidden`; the host has a real `role="img"` and a label/description updated from the adapter. The readable caption is always present next to the sphere.
- Status pills use text plus tone attributes; state is never conveyed by color alone.
- `prefers-reduced-motion: reduce` suppresses the live-dot animation and makes the renderer draw a static representative frame.
- The 900/700/480 px breakpoints shrink the main sphere and stack the main card; compact status pills hide at the smallest breakpoint while the label and description remain.
- The 390–1920 px full UI sweep was not re-run for this extra. The existing responsive workspace suite and the page asset tests pass; the extra's live captures are at 1440 px.

## 14. Integration with Extra 1 and Extra 2

**Status: PASS**

- Extra 1's companion still receives the real run lifecycle and the same app-wide SSE progress/completion frames. During the live Chat it showed **Responding** while the main orb was working.
- Extra 2's Atrium isometric map remains a separate, read-only surface. No Atrium code or planner was modified.
- There is no second EventSource, no second activity bus and no second planner. The new adapter is presentation-only.
- Regression run before the final status-field fix: `tests/test_extra1_companion.py`, `tests/test_extra2_isometric.py`, `tests/test_web_platform.py` — **91 passed, 437 subtests**.

## 15. Automated tests

**Status: PASS**

`tests/test_extra4_orbs.py` has three tests:

1. **Activity adapter** (real `activity-state.js` in Node): initial scopes, the full task lifecycle, intent/context/decision/plan/tool/verification/recovery mapping, duplicates, out-of-order sequence, task snapshot fallback, transport loss/recovery, voice priority and handback, late frames, local completion/error/waiting, Explore completion copy and status, diagnostics.
2. **Renderer contract** (real `thinking-orb.js`): all 13 renderer states, shared instance count, DPR cap (432 px backing for a 216 px orb at DPR 4), canvas label, destroy cleanup, reduced-motion branch.
3. **Page wiring**: four hosts/labels/statuses, all module tags, single EventSource, real event types, voice hooks, reduced-motion CSS, no forbidden imports/fetch/XHR, no old `extra4Settle` pattern.

Focused result after the status fix:

```text
tests/test_extra4_orbs.py ... 3 passed in 0.52s
```

Final combined regression after the last asset edit:

```text
tests/test_extra4_orbs.py tests/test_extra1_companion.py tests/test_extra2_isometric.py
tests/test_web_platform.py tests/test_web_api.py
... 169 passed, 512 subtests passed, 457 warnings in 76.28s
PYTEST_EXIT=0
```

`mypy` was not run because no Python source changed; only the JS assets, their Python test harness and documentation changed. The API reference was not regenerated because no route or `ApiSurface` row changed.

## 16. Live application verification

**Status: PASS for Chat, Explore and task; PARTIAL for voice; PARTIAL for performance**

The app was run from source at `http://127.0.0.1:8001/` and driven through the real UI. The real `/events/stream` frames, `/ask`, `/explore`, `/status.app.tasks` and Web Speech paths were used.

| Scenario | Result | Evidence |
|---|---|---|
| Page loads four placements | PASS | 4 canvases; main 215×215; asset modules load without console errors |
| Real Chat request | PASS | “What is the capital of Japan?” → Tokyo; main moved through real tool selection and completed; task scope ended completed |
| Explore research | PASS | Two runs; 4.4 s fresh and 2.96 s cached; “Research complete / The research report is ready.” |
| Explore distinct view | PASS | Explore card follows Explore frames; no unrelated task overwrote it |
| Voice Listening | PARTIAL | 1.5 s real Listening once; later persistent microphone blocked |
| Voice completed request | NOT PASS / BLOCKED | No usable microphone/speech service in this preview; no transcript obtained |
| Task orb end state | PASS | task scope reported completed with main after the same request; tool mapping is unit-tested |
| All scopes settle after completion | PASS | Main, Explore, task and voice returned to idle after completion/end |
| Tool failure / recovery / cancellation live | NOT RUN | Unit paths only; no live destructive/failing command was launched |
| Performance before/after | PARTIAL | Draw cost measured; long-task window invalid due suspended rAF |

### Screenshots

- `docs/screenshots/extra4-main-chat.png` — real Chat run, main orb on “Selecting a tool”.
- `docs/screenshots/extra4-explore-live.png` — real Explore run reading three source pages.

The Explore **completion label was verified in the live DOM** (`state: completed`, `status: completed`, “Research complete / The research report is ready.”). A saved completion frame was not published: the preview compositor lagged behind the DOM and captured an earlier “Starting research” frame, so that misleading image was removed.
- `docs/screenshots/extra4-voice-error.png` — persistent microphone-blocked state on both main and voice cards.

Screenshots are 1440 px viewport captures; the right edge of the Chat card can be clipped by the preview layout. They are evidence of the rendered UI, not of a full viewport sweep.

## 17. Acceptance verdict and limitations

### Acceptance criteria

| # | Requirement | Verdict |
|---|---|---|
| 1 | Distinct, high-quality spherical orb visual language | **PASS** |
| 2 | Main workspace orb with real runtime activity | **PASS** |
| 3 | Explore orb with real research lifecycle | **PASS** |
| 4 | Voice orb with real voice lifecycle | **PARTIAL** — Listening/error live; no completed capture |
| 5 | Active-task orb with real task activity | **PASS** — task scope completed with the live Chat request; intermediate mapping unit-tested; no separate agent task |
| 6 | One central activity adapter, four scoped views | **PASS** |
| 7 | Real events only; no fabricated progress | **PASS** |
| 8 | Stale, duplicate and out-of-order handling | **PASS** (focused tests) |
| 9 | Cancellation and failure handling | **PARTIAL** — failure live for voice; cancellation tested only |
| 10 | No duplicate planner, agent or event bus | **PASS** |
| 11 | Extra 1/2 remain intact | **PASS** — 91 tests + live companion state |
| 12 | Performance controls and measurement | **PARTIAL** — 0.49 ms average draw, no valid before/after CPU window |
| 13 | Reduced motion, visibility and responsive fallbacks | **PASS in code/tests; PARTIAL live sweep** |
| 14 | Automated tests | **PASS** — 3 focused tests |
| 15 | Actual app integration | **PASS** for Chat/Explore/task |
| 16 | Documentation and screenshots | **PASS** |
| 17 | No commit or push | **PASS** |

### Why the overall verdict is PARTIAL

The feature is complete and works on the real application paths that could be exercised. Two acceptance claims are deliberately withheld:

1. **Voice end-to-end** — this preview granted Listening once but then blocked the microphone/speech service. The voice orb's visual transitions are implemented and unit-tested; a spoken request producing an answer was not observed.
2. **Performance before/after** — the embedded preview suspended `requestAnimationFrame` during the comparison window (1 frame in 5 s), so those numbers cannot compare orb-on vs orb-off. The renderer's own draw cost is measured, and its visibility/rate caps are implemented, but no total-CPU saving is claimed.

Cancellation and a live tool-failure/recovery sequence were unit-tested, not run live; they are therefore marked partial/not run above rather than being presented as live evidence.

### Defects found and fixed during verification

- **Live Chat interruption:** `timer.extra4Settle = setTimeout(...)` wrote a property onto a numeric browser handle and threw in strict mode. Replaced with a `settleTimer` variable; pinned against regression.
- **Ordering:** same-millisecond events could resolve by Map insertion order. The monotonic `seq` is now the tiebreaker.
- **Completion copy:** Explore originally showed a generic “Completed / The request finished.” It now has research-specific completion copy.
- **Status normalization:** completion frames showed the right label but left `status: searching`; voice end left `status: error`. Both local update paths now keep `status` aligned and clear stale error text.

### Deliberate exclusions

- No backend event, route, planner, agent or task scheduler was added.
- No React/npm toolchain or third-party package was vendored.
- No demo/random animation was added to stand in for absent work.
- No commit or push was performed.
