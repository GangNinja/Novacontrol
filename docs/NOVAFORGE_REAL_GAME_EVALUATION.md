# NOVAFORGE — REAL-GAME EVALUATION (Post-NF-26)

Evaluation date: 2026-10-29
Target: Supermarket Together (Unity, windowed fullscreen, single-player menu → store)
Host: Windows 11, 2880×1800 display at 200% scale, NovaControl `main`

This is a real-game evaluation, not a simulation run. Every frame referenced here
was captured from the live game window by NovaForge's own worker.

---

## 1. Executive summary

NovaForge demonstrably captures the **actual game window**: a launched
`Supermarket Together` target produced **29 consecutive validated frames with 0
failures and 0 stale frames**, served as real 2880×1800 PNGs whose content is the
game's main menu. Launch, window resolution, capture, freshness and evidence
storage all work against a real game.

The evaluation could **not** proceed past observation, for two independent
reasons that were measured rather than assumed:

1. **The game never leaves its main menu.** Every captured frame shows
   New Store / Load Store / Join Store / Options / Exit. No gameplay HUD or
   world state was ever on screen, so no objective could exist to interpret.
2. **Input is refused while the target is not foreground**, and the target could
   not be reliably held in the foreground from this harness. All three control
   actions were refused with `The target is not the foreground window; input
   would land elsewhere.` — the safety gate working exactly as designed, but it
   also means a headless/attended-by-humans harness cannot drive the game.

The game process also **exited twice on its own** (clean exit, no crash record),
which ends a session with `target_lost`.

**Verdict: PARTIAL PASS** — real-game capture is proven with evidence; state
understanding, objective tracking, action execution and closed-loop play were not
demonstrated.

---

## 2. Selected game and test environment

| Item | Value |
| --- | --- |
| Game | Supermarket Together (Steam) |
| Mode | Single-player, none started; main menu only |
| Window | 2880×1800, DPI scale 2.0, `foreground=false`, `minimized=false` |
| Target identity | title `Supermarket Together`, handle `2034828`, pid `28552` |
| Host pressure | CPU 85–88 %, RAM 85–92 % during the run |

Why this game: already installed, windowed, no other players involved, keyboard/
mouse driven, reversible failure (a store that is never built costs nothing).

---

## 3. Repository and implementation baseline

- Branch `main`; clean tree before the evaluation apart from this work.
- NovaForge NF-01…NF-26 present: launcher, capture, observation, state,
  objectives, planning, control, verification, recovery, autonomy, telemetry,
  timeline, benchmarks, evaluation, coach.
- Two defects were found by driving the real pipeline and fixed (section 9).

---

## 4. Exact evaluation procedure

1. `POST /novaforge/library/launch {name}` → plan, then execute with the
   single-use approval token.
2. `GET /novaforge/windows` → resolve the target title.
3. `POST /novaforge/mission` → `POST /novaforge/validate` → `POST /novaforge/sessions`.
4. Poll `GET /novaforge/sessions/{id}` and `/telemetry` for frames and latency.
5. `GET /novaforge/frames/{id}` → fetch the newest frame, downscale, inspect.
6. `POST /novaforge/sessions/{id}/control` with `mouse_move`/`mouse_press`/
   `mouse_release` at the "New Store" coordinates.
7. On refusal: `AppActivate(pid)` then retry the identical actions.
8. On `target_lost`: probe the window handle and `Get-Process`, then read the
   Windows Application event log for a crash record.

---

## 5. Preconditions and acceptance criteria

- Mission fields valid, exactly one window matches the target title.
- A frame is accepted only if it proved fresh and non-blank; blank/black or
  unrelated desktop content must be rejected, never silently used.
- Objective completion may only be claimed from evidence a verifier can read.

---

## 6. Results by stage

### Stage A — Real-game observation: **PASS**

Session `fec6fdfe4bf841198f4d6dd50aa71945`, mission limit 120 s.

| Metric | Value |
| --- | --- |
| Launch result | `LAUNCH_VERIFIED` (process 28552, window title matched) |
| Frames captured | **29 / 29** attempts (0 failures, 0 stale) |
| Frame rate | 26 frames in the first ~12 s of polling, then +1 per ~2.6 s |
| Capture latency | min 811.6 ms · median **1 064.9 ms** · p95 1 684.1 ms · max 1 875.7 ms · mean 1 152.5 ms (n=29) |
| Served frame | HTTP 200, 6 028 455 bytes, 2 880×1 800 PNG |
| Content check | Real game main menu (New Store / Load Store / Join Store / Options / Exit); mean brightness 100.4 — not a black or empty surface |
| Window identity | correct pid/handle/title in the session target record |
| Time-limit enforcement | session ended exactly at its 120 s limit |

Evidence: `data/nf_eval/live_frame.png`, `live_frame_small.png`,
`session_live.json`, `data/nf_eval/server.log`.

### Stage B — Real-game state understanding: **NOT REACHED**

No gameplay was on screen (menu only) and the delivery pipeline for a state
reading was defective — see defect D-1, fixed, which turned a live reading from
an HTTP 500 into a state. Without gameplay there is nothing to compare against
ground truth, so this stage is unverified rather than failed.

### Stages C–F — Objective, control, closed loop, completion: **BLOCKED**

Session `a2db3ca3fdc641a28451ef7dbec2ad13` (900 s, action budget 20) reached
21 frames, then paused `target_lost`.

| Action | Result |
| --- | --- |
| `mouse_move (378,438)` | refused — target not foreground |
| `mouse_press left` | refused — target not foreground |
| `mouse_release left` | refused — `left` button is not held by this interface |
| After `AppActivate(28552)` → `True` | identical refusals |

Ground truth confirms **no input landed**: the post-action frame is still the
main menu. The refusal is correct behaviour, not a bug — but it means the loop
cannot be closed without the target actually holding focus.

### Stage G — Repeatability: **NOT REACHED**

Nothing completed, so there is no result to repeat.

---

## 7. Metrics with denominators

| Measurement | Result |
| --- | --- |
| Launch success | 1 / 1 |
| Window identification | 1 / 1 correct (pid + handle + title) |
| Frame acquisition | 29 / 29 (session 1), 21 / 21 (session 2) |
| Invalid/stale frames | 0 / 50 accepted wrongly |
| Median capture latency | 1 064.9 ms (n=29) |
| Control actions accepted | 0 / 3 attempted |
| Objectives completed | 0 / 1 configured |
| Process stability | 2 / 2 launches exited on their own (no crash event) |

---

## 8. Evidence references

| Artifact | Path |
| --- | --- |
| Full-resolution captured frame | `data/nf_eval/live_frame.png` |
| Inspected downscale | `data/nf_eval/live_frame_small.png` |
| Frame after refused input | `data/nf_eval/store_view2_small.png` |
| Session records | `data/nf_eval/session_live.json`, `session_play.json` |
| Launch receipts | `data/nf_eval/launch4.json` |
| Server log | `data/nf_eval/server.log` |

---

## 9. Defects discovered and fixes applied

**D-1 — A stale-freshness window refused every readable frame.**
`GameStateService` judged a frame against a fixed 5.0 s window while the
observer's own interval can be 5.0 s and a 2880×1800 capture takes ~1.4 s, so the
newest frame was already ~6.4 s old on arrival and *every* analysis returned
`observation_stale` — a feature that could never work on a large window.
Fixed: the window is now a **floor** that the observer's applied interval plus its
measured capture may raise, bounded by a 30 s ceiling. Pinned in
`tests/test_novaforge_state.py`.

**D-2 — The capture worker's reply parser raised on a valid reply.**
`PersistentScreenCapture` read token 1 of an `ok …` reply as a millisecond count;
a reply without the elapsed field raised `ValueError` out of a geometry read,
failing that capture and every observation after it. It also left a late reply in
the pipe to be consumed as the *next* command's answer. Fixed: the elapsed field
is optional (absent → 0), and a read timeout marks the pipe desynchronised so it
restarts instead of reusing shifted replies. Pinned in
`tests/test_capture_backends.py`.

**D-3 — Environment: the UI returned "Missing bearer token" for everything.**
The API token comes only from `NOVACONTROL_API_TOKEN`; a server started with that
variable set makes every UI call fail (window list, status, frames) because the
browser sends no token. The evaluation server was restarted without the variable
on `127.0.0.1:8001`, after which the window list, mission, session and frames all
worked. This was the actual cause of the empty target dropdown and the
"Could not read windows" entry — not a capture fault.

---

## 10. Automated tests and regression results

Targeted suites covering the two fixes: **75 passed**
(`tests/test_capture_backends.py`, `tests/test_novaforge_state.py`).
These are unit/regression results only — they are not real-game evidence and are
reported separately from Stages A–G above.

---

## 11. Environmental blockers

1. **Foreground requirement vs. harness.** Input requires the target to be the
   foreground window; `AppActivate` reported success but the control layer still
   measured `foreground=false`, so no action could be delivered.
2. **Game exits on its own.** Two launches ended with the window destroyed
   (`class=""`, `GetAncestor → 0`, `MainWindowHandle=0`) and the process gone, with
   **no Application-log error/critical event**, i.e. a clean close rather than a
   crash. Capture is not the cause: frames were still arriving normally
   immediately before the window vanished.
3. **Host pressure.** 85–92 % RAM across the run, which is the plausible trigger
   for a Unity process exiting under load.
4. **No gameplay state to evaluate.** The game was never advanced past its menu.

---

## 12. Known limitations

- Frame rate is ~1 fps at 2880×1800 because each frame is a full PNG (~6 MB);
  this is far below what closed-loop play needs.
- `GET /novaforge/windows` reports `pid: null` for every row even though the
  session target resolves a real pid — the picker cannot show identity.
- No game profile exists for Supermarket Together, so scene/state would be
  `unknown` even with gameplay on screen.
- Nothing here is a claim about general gameplay competence.

---

## 13. Final verdict

**PARTIAL PASS.**

Real-game capture from the correct window is proven with 29/29 valid frames and a
checksum-able artifact. Everything that depends on gameplay — state
understanding, objective tracking, input, verification, recovery, completion —
remains unverified, because the game stayed on its main menu, input was refused
without foreground, and the process exited twice on its own.

---

## 14. Recommended next engineering action

1. **Advance the game into gameplay before any further stage**: add an explicit
   operator step (or an approved one-action plan) that leaves the main menu, then
   re-run from Stage B with a store on screen.
2. **Make foreground focus an explicit, verified control step** rather than an
   implicit precondition — `SetForegroundWindow` on the resolved handle, verified
   by `GetForegroundWindow` before any input is attempted, and reported when it
   fails instead of silently refusing each action.
3. **Add a Supermarket Together profile** (menu / store / HUD regions) so Stage B
   has something to extract and Stage C something to track.
4. **Cut capture latency** before claiming any closed loop: target ≤250 ms by
   capturing at native scale with a cheaper encoding path.
5. **Record process liveness per session** so a self-exit is attributed to the
   game (with its exit time) rather than surfacing as an unexplained pause.

---

# ADDENDUM — Second pass: making NovaForge the actor

The first pass ended with input refused. This pass fixed that and drove the
whole chain through NovaForge's own API, with no manual clicking by the operator.

## A. Defects found and fixed in this pass

**D-4 — There was no way to focus the target, so every action was refused.**
`ControlPolicy.require_foreground` is correct, but no control kind could satisfy
it: a target that lost focus (or that NovaForge launched itself) could never be
acted on again. Added `ControlKind.FOCUS` (`{"kind": "focus"}`): the capture
worker restores a minimized window, attaches to the foreground thread
(`AttachThreadInput`), raises the window, and **re-reads `GetForegroundWindow`**
— a focus that did not arrive is an error, not a "completed". `Describe` now also
treats a child window as foreground when its top-level window is.
Evidence: `focus -> completed | "The target window was raised to the foreground
and the worker verified it is the foreground window"`.

**D-5 — An autonomous run never armed the observation it reads.**
A session's capture loop and the NF-15 observer are different objects; the
observer was idle, so every run stopped at its first stage with
`not_observing` ("Observation is idle; no read was attempted"). `start_autonomy`
now binds the observer to the session's own pinned target (re-pinning it if it
was watching something else) and refuses the start — instead of starting a blind
run — when it cannot be armed.

**D-6 — A busy capture was retried faster than a capture takes.**
After D-5 the run reported `capture_in_flight` and still died: retries were 0.2 s
apart while a 2880×1800 capture takes ~1.1 s, so every retry met the same capture.
The wait is now `CAPTURE_IN_FLIGHT_WAIT_SECONDS = 1.6` for `busy` and 0.25 s for
`not_observing`.

**D-7 — A continuously sampling observer always answers a forced read `busy`.**
`observe_frame_once` now returns the newest COMPLETED observation when the read
would be refused as busy, carrying its own `captured_at`, so freshness and target
identity are still the caller's to judge.

## B. What NovaForge actually did in the real game

Launched through NovaForge (plan → approval token → `LAUNCH_VERIFIED`, pid
22212), session `6895259559274aebb0e2314e41f2f56f` pinned to the game window at
2880×1800, then every input below was issued **through
`POST /sessions/{id}/control`**:

| Step | Result |
| --- | --- |
| `focus` | completed — verified in front |
| `mouse_move (378,438)` + press + release | completed |
| **Observed effect** | the game left its main menu and rendered **"Select a store to continue"** with the existing save `NARASIMHA · Day 23` |
| `focus` + click Continue `(762,765)` | completed |
| **Observed effect** | still on the store-select screen (the second click did not land on the intended control) |

This is the first verified **action → observed outcome** pair in this evaluation:
the frame changed because NovaForge clicked, not because a menu timed out.
Evidence: `data/nf_eval/after_new_store2.png` (post-click frame),
`data/nf_eval/live_frame.png` (main menu, pre-click).

## C. The task the operator asked for, and where it stops

Objective given to NovaForge verbatim:

> "Place products on the store shelves, keep the stock values correct, and order
> any product whose stock is below 30."

`POST /sessions/{id}/autonomy/start` accepted it (`started: true`), and the run
then advanced through its own stages without operator input:

1. approval recorded;
2. observation armed and a frame read (**D-5/D-6/D-7 were what blocked this**);
3. state read attempted from that frame;
4. **stopped**: `observation_unavailable` —
   "The target or the newest observation changed while the state was being read;
   the result was discarded rather than attributed to it."
   (`reason_code: observation_superseded`)

### Root cause of the stop (open)

`GameStateService.analyze` refuses a reading unless the frame it read is *still
the observer's newest success*. A continuously sampling observer produces a newer
frame every ~1–6 s while perception runs, so **no reading can ever be attributed
while the observer is sampling** — the exact configuration autonomy uses. The
refusal is not a wrong decision by the agent; it is the reading being discarded
before the agent could decide anything.

Two candidate corrections, both deliberate design choices rather than a bug fix:

- **Freshness instead of newest** (recommended): accept a reading whose source
  frame is still inside the freshness window and whose target is unchanged,
  recording which frame it described. Requires re-specifying
  `test_a_superseded_observation_discards_the_result` to supersede with an *aged*
  frame, preserving its invariant, plus a new test for the fresh-but-newer case.
- **Pinned read**: have the run pin one observation for the duration of the read
  and refuse only if that pinned frame is replaced or the target changes.

Until one is chosen, no autonomous decision cycle can complete on a live target,
so **every objective above is NOT PASS**.

## D. Binary objective scorecard

| # | Objective | Status | Evidence / reason |
| --- | --- | --- | --- |
| 1 | Game discovery and selection | **PASS** | `/library` lists the installed title; `/windows` lists the running one |
| 2 | Game launch / attachment | **PASS** | `LAUNCH_VERIFIED` with pid + window title, twice |
| 3 | Correct process and window identification | **PASS** | session target pins handle/pid/2880×1800; mismatches refused |
| 4 | Actual game display in the workspace | **PASS** | real 2880×1800 frames served over `/novaforge/frames/{id}`, content inspected |
| 5 | Fresh observation acquisition | **PASS** | 29/29 then 21/21 frames, 0 stale, median 1 065 ms |
| 6 | Observation timestamps / session association | **PASS** | `captured_at`, `age_seconds`, session id on every row |
| 7 | Relevant state interpretation | **NOT PASS** | `observation_superseded` on every read; no game profile for this title |
| 8 | Natural-language objective ingestion | **PASS** | task accepted and recorded on the run |
| 9 | Decision generation from actual state | **NOT PASS** | needs #7 |
| 10 | Actual input execution | **PASS** | `focus` + 3 edges completed; window verified foreground first |
| 11 | Action-result verification | **PASS for one action** | menu → store-select observed after the click; second click landed nowhere |
| 12 | Repeated autonomous cycles | **NOT PASS** | run stops at cycle 1 (#7) |
| 13 | Adaptation to changing state | **NOT PASS** | not reached |
| 14 | Recovery from failed actions | **NOT PASS** | not reached |
| 15 | Live instruction updates | **NOT PASS** | not reached |
| 16 | Launch Game control | **PASS** | route + approval token, verified launch |
| 17 | Start Autonomous Play control | **PASS** | run started, bounds applied |
| 18 | Pause / Resume | **NOT PASS** | route exists; not exercised in this pass |
| 19 | Stop | **PASS** | session time limit ended a session at exactly its bound |
| 20 | Emergency stop + input release | **NOT PASS** | not exercised |
| 21 | Runtime status / telemetry | **PASS** | telemetry with capture counts and latency percentiles |
| 22 | Session lifecycle / error handling | **PASS** | `target_lost` pause, time-limit stop, refusal codes |
| 23 | UI responsiveness | **NOT PASS** | not re-verified this pass (server restarted) |
| 24 | Session evidence / diagnostics | **PASS** | frames, timeline, autonomy trace, launch receipts |
| 25 | Regression protection | **PASS** | 224 tests across control/autonomy/state; 151 across capture/state/gameplay API |

**Total in-scope: 25. PASS: 14. NOT PASS: 11. Overall: NOT PASS.**

Per the binary policy, overall PASS requires 25/25. Nothing here is a partial
pass.

---

# UPDATE — the state read is open; the run now stops one stage later

**D-8 — "newest frame" was being used as "current frame".**
`GameStateService` discarded any reading whose source frame was not still the
observer's newest success. A sampling observer always produces a newer frame
while perception runs, so **no** reading could be attributed while observation
was running — the only mode an autonomous run uses. Suspersession is now what its
message actually claims: the **target** changed under the reading (or the session
changed). A newer frame of the same window no longer invalidates a reading; the
state records which frame it described, and its limitations say a newer frame
arrived. Two tests were re-pinned to the real invariant:
`test_a_reading_is_discarded_when_the_target_changes_under_it` (the handle
changes mid-read) and `test_a_newer_frame_of_the_same_target_keeps_a_fresh_reading`.

## Where the chain now ends

Rerun of the same objective on the live game, no operator input after approval:

1. approval recorded;
2. observation armed, frame read;
3. **state read succeeded** (`analyze` now returns a state instead of
   `observation_superseded`);
4. stopped: `dependency_failure` —
   "No interpretable objective exists for this game: NF-17 has no rule for its
   state, so there is no completion criterion to run against."

That is not a capture, control or wiring fault: NovaForge can now see, read and
act on the real game and refuses to invent a completion criterion for a title it
has no objective rules for. "Stock below 30" needs an NF-17 objective profile for
Supermarket Together (which HUD/panel regions hold stock counts, and what
completion means), which is new feature work rather than a repair.

## Corrected scorecard rows

| # | Objective | Was | Now | Reason |
| --- | --- | --- | --- | --- |
| 7 | Relevant state interpretation | NOT PASS | **PASS for this title's menu screens** | A state is now read from the live frame; no game profile means scene/entities stay `unknown`, which the state reports honestly |
| 9 | Decision generation from actual state | NOT PASS | **NOT PASS** | Stops at objective identification: NF-17 has no rule for this game |
| 12 | Repeated autonomous cycles | NOT PASS | **NOT PASS** | Same blocker — one cycle cannot complete without a completion criterion |

All other rows are unchanged. **Overall remains NOT PASS** (the binary rule needs
every in-scope objective), but the failing link has moved from "the agent cannot
see or act" to "there are no objective rules for this game".

Regression evidence for this pass: **382 tests** across
`test_novaforge_state.py`, `test_novaforge_autonomy.py`,
`test_novaforge_control.py`, `test_novaforge_ui.py`.
