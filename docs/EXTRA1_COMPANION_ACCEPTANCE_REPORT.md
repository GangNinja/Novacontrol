# Extra 1 — Animated Companion & Interactive Workspace

**Acceptance report.** Scope is Extra 1 only. NovaForge gameplay was not modified:
this specification reserves that workstream.

- Report date: 2026-10-31
- Re-verified: 2026-11-01 — three state-priority defects found and fixed; fresh runtime and test evidence in §C2
- Application under test: NovaControl web workspace (`novacontrol.api.app:create_app`)
- Runtime: `python -m uvicorn … --port 8001`, real static tree, real `Application` services
- Evidence classes are labelled throughout: **live** (observed in the running app),
  **test** (automated suite), **code** (inspection only).

---

## A. Resource assessment — Bible Strong Avatar Lab

**What it actually is.** <https://avatars.bible-strong.app/> is a client-side
procedural 2D avatar studio (Vite/React bundle). It is a design tool with an
export step, not an embeddable runtime component for a non-React host.

**What it actually provides, verified from the published bundle and registries:**

| Interface | Evidence |
| --- | --- |
| Avatar definition JSON, `schema: "bible-strong/avatar-definition"`, `schemaVersion: 1` | validation schema string in the shipped bundle (`index-CWAgRcMJ.js`), and a definition imported in the live app |
| Export targets: React component, JavaScript module, SVG, PNG | bundle export strings |
| npm packages `@bible-strong/avatar-web`, `@bible-strong/avatar-react`, `@bible-strong/avatar-core` | npm registry, latest `0.1.0` |
| Repository | `github.com/smontlouis/bible-strong-avatar-lab` (TypeScript, 1,048 stars, not archived) |

**Licence, verified.** npm metadata for all three packages reports
`AGPL-3.0-only`; the GitHub licence API reports `AGPL-3.0`
(GNU Affero General Public License v3.0). There is no separate permissive
licence for the packages.

**Integration decision.** NovaControl is pure Python with hand-written ES
modules and no npm toolchain (`AGENTS.md`: never npm). Linking AGPL-3.0-only
JavaScript into the served static tree would put the combined distribution under
AGPL obligations, and the resource exposes no hosting-neutral embed API.
Therefore **no code or assets from the project are copied or linked**.

The integration consumes only the **data format**: the operator exports an
avatar definition from the Avatar Lab, and NovaControl validates and renders it
with its own SVG face. A file the user creates is user data, not a derivative of
the program. The lab's own animation tracks are explicitly **not** used —
`companionAvatarFromDefinition` reports them as ignored, and NovaControl's
procedural motion is the animation system.

**Constraint found:** the schema URL advertised inside the definition
(`https://bible-strong.app/schemas/avatar-definition-v1.json`) does not serve a
JSON schema document (it returns the Bible Strong application HTML). Validation
is therefore implemented from the bundle's published constraints and exercised
against real exported definitions, not against a fetched schema file.

---

## B. Implementation

### Files changed

| File | Change |
| --- | --- |
| `src/novacontrol/web/static/js/companion.js` (new) | the companion: one state table, expression table, procedural animation loop, avatar import/validation, motion policy, global door |
| `src/novacontrol/web/static/index.html` | `#companionStage` in the workspace sidebar; module `<script>` |
| `src/novacontrol/web/static/styles.css` | companion stage, states, controls |
| `src/novacontrol/web/static/js/render-utils.js` | `run()` reports start, result (incl. `waiting_for_approval` / `waiting_for_phone_bridge`) and error; SSE progress and app-wide completion |
| `src/novacontrol/web/static/js/voice.js` | listening, speech start/end and voice failure feed the companion |
| `src/novacontrol/web/static/app.js` | `setupCompanion()` during init |
| `tests/test_extra1_companion.py` (new) | 24 tests: runtime behaviour through the real module, asset/ID contract, wiring guards |

### How the companion connects to real state

- `run()` in `render-utils.js` is the one lifecycle every action already uses.
  It calls `NovaCompanion.onRunStart(type, loadingText)`; chat = **Thinking**,
  every other family = **Working**. On success it passes the real status so a
  plan awaiting approval holds **Waiting**; on rejection it calls
  `onRunError` → **Interrupted**, which is not cleared by a timer.
- The single SSE activity channel (`/events/stream`, `appendLiveStep`) pushes the
  real step text into **Working** and `onCompleted` for app-wide completions.
  An app-wide completion cannot settle a tab that still has a local run in
  flight (`activeRuns` counter).
- `voice.js` calls `onListening`/`onListeningEnd` from real recognition events and
  `onSpeechStart`/`onSpeechEnd` from real utterance events.
- Nothing in the companion invents progress: there is no state timer. The only
  timer is the post-success dwell that returns **Responding** to **Idle**
  (`COMPANION_RESPOND_DWELL_MS`), which presents an event that already happened.

### How the workspace integrates

The stage sits in the sidebar next to the live panels, so every tab keeps the
companion visible; the face is a real button that activates the Chat panel and
focuses its input. Existing navigation, panels and actions are untouched
(tab switching was exercised live after the companion mounted).

### Deviations

- The selected resource is integrated by **avatar-definition import**, not by
  embedding its renderer; the licence and the absent npm toolchain are the
  established blockers, documented above.
- Lab animation tracks are replaced by NovaControl's own procedural motion; the
  import reports what it ignored rather than pretending to play them.

---

## C. Runtime evidence

NovaControl was really launched (uvicorn on `127.0.0.1:8001`, preview browser
attached to that origin) and the following were observed live.

| # | Workflow | Observed |
| --- | --- | --- |
| 1 | Idle animation | `#companionBody` transform changed `scale(1.0098)` → `scale(1.0116)` over 1.2 s; eye height `ry` changed on blink. The face is animating, not static. |
| 2 | Pause motion | Clicking **Motion on** → `data-motion="paused"`, `aria-pressed="true"`, `localStorage.novacontrol.companion.motion="paused"`, body frozen at `scale(1.0000)` across 1.5 s. Clicking again resumed (`scale(0.9924`). |
| 3 | Real chat request | Sent “what is the capital of Japan?” through the Chat panel. State sequence captured by a `MutationObserver`: `idle → thinking` (detail “Thinking through your question…”) `→ responding → idle`, with the real answer “The capital of Japan is Tokyo.” rendered and the Brain card reporting `Brain mode: llm`. |
| 4 | Real approval gate | Planned “call mom” on the JARVIS **Phone** device. Sequence: `idle → working → waiting` with detail “Waiting for your approval”, while the output rendered the genuine `waiting_for_phone_bridge` pairing guidance (no device paired). |
| 5 | Real failure | Stopped the server, submitted a chat, observed `thinking → error` (“Interrupted”, detail “Failed to fetch”) with the inline error block. Server restarted afterwards. |
| 6 | Face control | From the System panel, a real click on the face activated `chatPanel` and focused `#chatInput`. |
| 7 | Avatar import | Attached a real `bible-strong/avatar-definition` file to `#companionAvatarInput`: name → “Pilot Nova”, body `#241033`, eyes `#ff8ae2`, eye geometry normalised, definition persisted, input cleared. |
| 8 | Foreign file | Attached `{"hello":"world"}`: refused with “expected the Avatar Lab schema …, found ""”, avatar unchanged. |
| 9 | Speak | Clicking **Speak** invoked the real `speakLast()` path; `speechSynthesis.speaking` was `true` immediately after the click. |
| 10 | Persistence | Reloaded the page: companion restored “Pilot Nova” (`data-avatar-source="avatar-lab"`), colours applied, motion state restored. |
| 11 | Responsiveness | Panels, chat, Explore, JARVIS and vision controls remained operable while the companion animated. |
| 12 | Voice input (live mic) | Clicking **Voice Input** did not start recognition in the preview browser (no audio device/permission), so no live **Listening** state was produced. The state is reached through the real `voice.js` hooks and is covered by tests. |

### Test results

| Command | Result |
| --- | --- |
| `pytest tests/test_extra1_companion.py -q` | 24 passed (1.2 s) |
| `pytest tests/test_web_platform.py tests/test_reduced_motion_e2e.py -q` | 46 passed, 2 skipped, 415 subtests (3.0 s) |
| `pytest tests/test_web_api.py -q` | 75 passed, 73 subtests (119.1 s) |
| `pytest tests/test_route_shape_contract.py -q` | 18 passed, 90 subtests (227.8 s) |
| `pytest tests/test_api_reference_sync.py -q` | 6 passed, 372 subtests (25.1 s) |
| `pytest tests/test_focus_rings.py -q` | 7 passed (58.1 s) |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |

The two skips are browser-driven tests whose Playwright dependency is absent in
this environment; the browser behavior they cover was exercised manually in the
live preview instead (rows 1–12). The route-shape suite is slow by construction
(it builds an application per route contract); it is split from the web-API run
so neither exceeds the harness time cap, not because either stalled.

### Re-verification (2026-11-01)

Fresh run of the real application (`uvicorn`, port 8001, cold start 34 s) in a
Chromium preview at 1440×900. The brain switch was on `scratch`, so the chat
answer came from the local engine (no Ollama call).

**Defects found and fixed.** All three were state-priority defects, not
rendering defects:

1. **App-wide frames could overwrite a local state.** `/events/stream` is
   application-wide, so another client's `explore.progress`/`command.progress`
   or completion could relabel this tab's pending approval gate, a live
   microphone, or an active utterance. `companionLocalStateHolds()`
   (waiting/listening/speaking) now guards both `onProgress` and `onCompleted`;
   a remote completion also no longer clears this tab's `error` state.
2. **Ending dictation or speech during a run claimed the work had stopped.**
   `onListeningEnd`/`onSpeechEnd` always returned to `idle`; with a local run
   still in flight they now return to `working`.
3. **Dangling documentation reference.** The module header pointed at
   `docs/EXTRA1_COMPANION.md`, which does not exist; it now points at this
   report.

Four new assertions across two new tests and two existing tests pin these (24
phase tests total, up from 22): the gate survives a remote completion, voice
states survive remote progress/completion, a remote completion does not clear a
local failure, and dictation ending mid-run restores `working`.

Live observations this run:

| # | Workflow | Observed |
| --- | --- | --- |
| 1 | Idle animation | `#companionBody` transform `scale(0.9975)` → `scale(1.0040)` over 800 ms; face 118×118 in the 268 px sidebar |
| 2 | Pause / resume | `data-motion="paused"`, `aria-pressed="true"`, label “Motion off”, stored `paused`; body frozen at `scale(1.0000)` for 900 ms; resumed and moved again |
| 3 | Real chat run | recorded sequence `idle → thinking` (“Thinking through your question…”) `→ responding → idle`; answer “The capital of Japan is Tokyo.” from **Brain mode: scratch** |
| 4 | Real approval gate | JARVIS Phone “call mom” → `working → waiting`, detail “Waiting for your approval”, with the genuine `waiting_for_phone_bridge` card |
| 5 | App-wide frames vs the gate | second browser tab ran Explore (live step “Searching for 'what is photosynthesis'…”, then completion); the first tab held `waiting` through both |
| 6 | Face control | real click → `chatPanel` active, `#chatInput` focused |
| 7 | Speak | after a real answer, `speechSynthesis.speaking === true` |
| 8 | Avatar import | a real `bible-strong/avatar-definition` file through the real `change` event: name “Verify Nova”, body `#123456`, eyes `#abcdef`, `data-avatar-source="avatar-lab"`, toast reported the ignored `animations`, input cleared, **restored after reload** |
| 9 | Foreign file | `{"hello":"world"}` refused with the schema reason; name and colours unchanged |
| 10 | Workspace usability | Chat, JARVIS (Phone plan) and Explore all operated while the companion animated; layout checked at 1440×900 and in the narrow top-deck form |

The imported test avatar was removed from browser storage afterwards; the page
was reloaded to the built-in **Nova** (`#0b2242`, idle, motion on). The preview
server was stopped after the checks.

### Remaining unverified

- Live microphone acquisition (environment has no audio input for the preview
  browser). The wiring is tested; the live state was not observed.
- Audibility of speech output: the engine accepted the utterance, but a headless
  preview has no audio output to confirm sound.

These are environment limits, not implementation gaps; they are the only
observations the runtime could not produce.

---

## D. Binary acceptance scorecard

| # | Required behavior | Verification | Result | Status |
| --- | --- | --- | --- | --- |
| 1 | Companion renders in the running application | live: stage visible in the sidebar on the real app | rendered, visible, non-zero box | **PASS** |
| 2 | Companion genuinely animates | live: body scale and eye height sampled over 1.2 s | continuous motion; frozen only when paused | **PASS** |
| 3 | Idle state | live: initial state and after every run settles | `idle` with “Idle” label | **PASS** |
| 4 | Listening state driven by real voice events | code: `voice.js` `onstart` → `onListening`; test: state table + wiring guard. Live mic unavailable | state exists and is wired to the real recognition lifecycle; live mic not exercised | **PASS** (live limitation stated) |
| 5 | Thinking/processing from a real assistant request | live: real chat request | `thinking` with the run's loading text, then real answer | **PASS** |
| 6 | Working state with live step detail | live: real phone plan run; code: SSE `onProgress` writes the real step | `working` observed on run start; progress text path tested | **PASS** |
| 7 | Waiting state from a real approval gate | live: `waiting_for_phone_bridge` plan | `waiting`, detail “Waiting for your approval” | **PASS** |
| 8 | Responding on real completion | live: chat completion sequence | `responding` then dwell back to `idle` | **PASS** |
| 9 | Error/interrupted on real failure, held | live: request with the server down | `error`, detail “Failed to fetch”; not timer-cleared | **PASS** |
| 10 | No fabricated progress or simulated activity | code: only `run()`, SSE and voice events call the door; test forbids a state timer | no independent state timer exists | **PASS** |
| 11 | Integrated into the real workspace; other functions accessible | live: sidebar stage + panel switching + chat/Explore/JARVIS use with the stage mounted | existing UI fully usable | **PASS** |
| 12 | Pause/resume motion control | live: click both directions, storage, frozen frames | paused freezes; resumed animates; persists | **PASS** |
| 13 | Face opens Chat and focuses the composer | live: click from System panel | `chatPanel` active, `chatInput` focused | **PASS** |
| 14 | Speak reads the last real answer | live: click with a real answer present | real `speakLast()` path invoked, utterance accepted | **PASS** (audibility unverifiable here) |
| 15 | Avatar Lab definition import | live: real file through the real input | applied, persisted, foreign file refused with a reason | **PASS** |
| 16 | Resource assessed; licence verified; nothing unlicensed copied | registries + GitHub API + bundle inspection | AGPL-3.0-only confirmed; data-only integration | **PASS** |
| 17 | Responsive with animations/background work | live: interactions during animation and while runs were in flight | no blocking or lost controls | **PASS** (not benchmarked) |
| 18 | No regression in the surrounding suites | tests above | all green | **PASS** |

---

## E. Overall verdict

- In-scope objectives: **18**
- PASS: **18**
- NOT PASS: **0**
- **Overall: PASS**

Qualifications, stated rather than hidden:

1. **Listening was not observed live** because the preview browser has no
   microphone. The state and its wiring are covered by the module tests; the
   runtime row could not be produced.
2. **Speech audibility was not confirmed**; the real speech path was invoked and
   the engine accepted the utterance.
3. The Avatar Lab is integrated through its **exported definition format**, not
   its AGPL-3.0-only code; that is the documented, licence-safe deviation.

The report's claims distinguish live observation (section C, rows 1–12) from
automated tests (section C, test table) and code inspection. No criterion above
rests on a test alone except where explicitly marked.

Re-verification on 2026-11-01 kept all 18 objectives PASS. It also found and
fixed three state-priority defects the original scorecard could not distinguish
(a remote frame could relabel a local gate or voice state; ending dictation
during a run claimed the work stopped). Remaining, non-blocking issues, stated
rather than hidden:

1. The manual **Motion** control still reads “Motion on” when the OS
   reduced-motion preference is what stopped ambient animation. The button
   reflects only the manual toggle; the OS preference itself is honored, and
   state changes still repaint the face.
2. An imported avatar can be replaced but not reset from the UI. Clearing the
   stored definition (or importing another) is the current way back to built-in
   Nova.

Neither issue affects any of the 18 objectives.
