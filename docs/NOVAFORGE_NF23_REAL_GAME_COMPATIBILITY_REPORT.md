# NovaForge NF-23 — Real-Game Compatibility and Observation — Verification Report

**Phase:** NF-23. **Tagline:** Observe. Decide. Act. Verify. Evolve.
**Companion documents:** `docs/NOVAFORGE_NF23_IMPLEMENTATION_PLAN.md` (this phase's plan, updated with
completed work), `docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md` (its blocker **B-01** is this
phase's subject), `docs/NOVAFORGE_UNIVERSAL_CAPTURE_REPORT.md` (H03), `docs/NOVAFORGE_H01_HITMAN_REPORT.md`,
`docs/NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md`, `docs/NOVAFORGE_H02_1_FOCUS_LOSS_REPORT.md`,
`docs/NOVAFORGE_NF15_UNIFIED_GAME_OBSERVATION_REPORT.md`, `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md`.
Earlier reports are **not** edited by this one; where a limitation recorded there is now resolved, that is
stated as such here.

---

## 1. Executive summary

NF-23 exists to turn "the observation pipeline works against fixtures" into "the observation pipeline works
against a real window, and says exactly what it can and cannot see". That is what was done, and it is what
was measured.

**Delivered, with evidence in this document**

| Item | Result |
|---|---|
| Observation ladder (10 stages, each from its own evidence) | implemented, served on `/novaforge/capabilities?title=…` and mirrored in the panel; its stage/API/embedding/surface pins are in `tests/test_novaforge_nf23.py` (45 tests in that file) |
| Eight live states on the observation surface | implemented + node-driven pins on the real `novaforge.js` (one label per state, none borrowed) |
| Dark-frame corruption fix (F-01) | implemented in `CaptureRouter` (two independent reads must agree before a blank frame is served as a dark read) + tests |
| Validation cost (F-02) | worker emits a 48×27 paint side car; **measured 71.27 ms → 1.43 ms per frame** at 5.2 MP, channel order verified, verdicts identical |
| Window lifecycle on a real window | measured end to end (unfocused, covered, minimized, restored, resized, destroyed, replaced, process exit) — table in §7 |
| Real pipeline end to end (NF-15 → NF-16) | 8+ consecutive validated frames of a real window, pause with **no** frame, restore with revalidation, `process_exited` on exit |
| **Defect found and fixed (F-06)** | a **paused** or **ended** observation still produced a *state reading*; now refused (`observation_paused` / `observation_inactive`) with 5 regression tests |
| **Defect found and fixed (F-07)** | a path-launched `.exe` was started with the **application's** working directory; now started from its own folder (Explorer's behaviour), with 2 regression tests. Measured cause of a real launch failure (§9). |
| **Defect found and fixed in the verification pass (F-08)** | a refusal that produced **no frame** described one (`capture_ok`, `content="not_target"`), so the panel said "the frame is not the target" about nothing; the refusal envelope now reports what was actually read, and the ladder's validated row cannot borrow a later refusal's content. 4 regression tests (§4, §8.1). |
| **Live real-game observation** | **BLOCKED, root cause measured** — the title's launcher prints `SteamAPI_Init failed` and exits rc=1 in 0.03 s, identically when Steam itself starts it, so **no real-game window can exist on this host**, by the product or by hand. Full evidence in §9. No real-game frame was claimed. |

**Final verdict:** **PARTIAL — PASS on the observation path, BLOCKED on the live real-game criterion.** Every
deliverable that could be verified on this machine was verified, the three defects found during verification
(F-06, F-07, and F-08 in the independent re-verification) were fixed and regression-pinned, and the one
criterion that needs an actual game window is reported as blocked with a
*measured root cause* (the title's Steam API initialisation fails on this host — see §9), not with a guess.
Per the phase's §15 this is not a PASS: `One real game is reliably identified` holds, `A supported observation
path acquires multiple consecutive real-game frames` does **not** (no game window could be produced by any
supported path, including Steam's own launcher).

---

## 2. Initial repository and working-tree state

| Fact | Value |
|---|---|
| Branch / HEAD | `main` at `8d10852` |
| Working tree | dirty by design: 59 tracked files modified + 60+ untracked docs/scripts from NovaControl and NF-01…NF-22, all uncommitted. Nothing was reset, stashed, discarded, reverted, committed or pushed. |
| Host | Windows 11, Intel Core Ultra 5 125H, Intel Arc iGPU, 16 GB RAM (15.37 GB visible), Python 3.13 in `.venv`. |
| Resource gate at phase start | free **2.74–2.91 GB**, used **81–82 %** — inside the ≥2 GB / ≤92 % rule. |
| Resource gate during this work | oscillated **1.50–2.50 GB free** (85–90 % used) with the operator's own applications (browsers, editors) resident. The gate was re-read immediately before every launch attempt; the launcher itself refuses below its bound (measured once, §9). |
| Untouched | `.kilo/worktrees/rune-board/` (another tool's worktree), every process this phase did not start, the disconnected WD My Passport 2TB. |

Scratch evidence lives in `.nc23_tmp/` (untracked), as in earlier phases (`.nc22_tmp/`, `.nc_cap_evidence/`).

---

## 3. Prior findings reviewed (and what each meant for NF-23)

Read from source, not from summaries: `desktop/capture.py`, `desktop/capture_backends.py`,
`desktop/capture_validation.py`, `desktop/controller.py` (runner seams), `novaforge/observer.py` (NF-15),
`novaforge/manager.py` (wiring), `novaforge/state_service.py` (NF-16), `novaforge/launcher.py`,
`novaforge/games.py`, the panel (`web/static/js/novaforge.js`), the API routes and the NF-23-relevant tests.

* **H03** shipped `printwindow` + `gdi_display` behind `CaptureRouter` with declared capabilities, and recorded
  two real limits that were **still true at NF-23 start**: a legitimately black frame is refused
  (its §10.4, measured), and validation decodes the whole PNG on every frame (§9: ~35 ms warm / 333–396 ms cold
  at 5.2 MP).
* **H01** recorded a geometry defect class (a session sized for a display mode the window no longer had).
  The NF-15 observer re-reads geometry every tick and the worker re-allocates its surface on size change, so
  NF-23 **measured** dimensions against the live window rect instead of assuming the class was gone (§7).
* **H02/H02.1** are why validation separates `capture_ok` / `target_visible` / `fresh` / `content_ok`, and why
  `source="screen"` is never live. Preserved, not revisited.
* **NF-15's observer** already pinned handle + pid + start time, rebinds only to the same process, pauses
  instead of capturing a minimized/hidden/offscreen/not-foreground target, discards a frame that lands after a
  stop, and bounds frames/sessions/history. NF-23 verified that live rather than rewriting it.
* **NF-16 … NF-22** consume the observer through one direction (`GameStateService` → `ObjectiveService` →
  `KnowledgeService` → planner → control → verification). NF-23 fixed only the one seam defect it measured (F-06).
* **The catalog** (`novaforge/games.py`) declares *environments*; the real launcher is `launcher.py`
  (`GameCatalog` + `GameLaunchService`), wired from the desktop runner's Steam manifests, Start-Menu index,
  process identity and the memory gate. NF-23 used that composition unchanged (§9).
* **`docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md`** records live game acceptance as blocker **B-01**.
  This phase narrowed B-01: the *observation* half is now verified to the limit the machine allows; the blocker
  that remains is a launch-side environmental fact (§9, §14).

---

## 4. Confirmed issues and their dispositions

| ID | Finding | Evidence (source of the claim) | Disposition |
|---|---|---|---|
| **F-01** | A legitimately dark frame was refused as `target_unreadable` (single signal: the window-surface read). | H03 §10.4 (`mean_luma 0.0`, `uniform_ratio 1.0` from a live game); NF-23 code review | **FIXED** — `CaptureRouter._corroborated_dark` serves a blank read as `content="dark"` **only** when every eligible backend independently read black; a painted second read keeps the refusal; fewer than two eligible backends ⇒ refuse as before; never corroborated when the target is not foreground. Pinned in `tests/test_capture_backends.py`. |
| **F-02** | Validation decoded the full PNG every frame (~35 ms warm, 333–396 ms cold at 5.2 MP). | H03 §9; NF-23 baseline measurement | **FIXED + MEASURED** — the worker writes a 48×27 BGR paint side car (`<frame>.thumb`, 3888 bytes) beside the PNG it already writes; `inspect_frame(thumbnail=…)` computes the *same* statistics from it; the router consumes and deletes it. Measured: **71.27 → 1.43 ms** at 2880×1800 (§11). |
| **F-03** | The ten-stage ladder existed only implicitly, scattered across catalog status, launch history, embedding probes and observation records. | source reading | **FIXED** — one derived `NovaForgeManager.observation_ladder(title)` with a state + evidence per stage, served on the existing capabilities route and rendered in the panel. No stage may be true because a later one is. |
| **F-04** | Objective C's eight statuses were not all distinguishable in the UI. | `novaforge.js` `frameLiveState`/`loadFrame` | **FIXED** — `observationLiveState()` maps the eight states onto the existing vocabulary (the refusal's stable `reason_code` picks the label) and the backend attempt trail is shown; node-driven pins run against the real `novaforge.js`. |
| **F-05** | Unverified: frame dimensions vs the live window rect, focus loss, minimize/restore, process exit, replacement, downstream state while unavailable. | source reading; no live measurement at phase start | **VERIFIED live** on a real window (§7) and, for the downstream half, **found a defect** — see F-06. |
| **F-06** | **A paused or ended observation still produced a state reading.** With the pipeline paused (`target_minimized`) and again after the process exited (`process_exited`), `GameStateService.analyze()` returned `analyzed: true` from the last frame, which was younger than the service's own 5 s window — while the service's declared contract says "a stale, paused or missing frame is a refusal with a code". | measured in `.nc23_tmp/observer_lifecycle_probe.log` (first run: `state_after_loss_ok: true`) | **FIXED** — `_pause_refusal` refuses `paused` with `observation_paused` and `stopped`/`failed` with `observation_inactive`, before perception is asked anything and again after it (a pause that lands mid-analysis discards the reading). 5 regression tests; re-measured live (§7). |
| **F-07** | **A path-launched executable was started with the application's working directory.** `launch_game_target(kind="path")` reached `cmd /c start` with the inherited cwd, so a title that resolves its assets relative to its own folder was started from `NovaControl\` instead. Measured: `Freedom.Exe` exited `rc=1` **in 0.02 s with no process and no window** when started that way; started from its own folder the same executable behaves as the title's own launcher does (the launcher prints its Steam failure, see §9). | `.nc23_tmp/live_game_direct_probe.log` (`window_found: false`, `processes_after_start: []`), direct measurement `EXITED rc=1 (0x00000001) after 0.02s` | **FIXED** — `LocalDesktopRunner._launch_open` starts an absolute `.exe` target with its own directory as the working directory (what Explorer does); shell/URI/`.lnk` targets keep the inherited cwd. 2 regression tests in `tests/test_desktop.py` (`test_a_path_launch_starts_an_exe_from_its_own_directory`, `test_a_uri_launch_keeps_the_inherited_directory`). |
| **F-08** | **A refusal that produced NO frame claimed one.** When every backend failed to return an image the router's refusal still described a frame: `content="not_target"` (and, before that was noticed, `capture_ok: true`), so the panel mapped a vanished/unreadable window to **TARGET IDENTITY MISMATCH** — "the frame is not the target" — for a capture that returned nothing, and the ladder's `frame_validated` row carried that content as its evidence. Four adjacent halves of the same envelope: a produced-but-uninspected frame (`content_inspection=False`) was reported as "no frame at all"; a blank refusal reported `not_target` instead of `blank`; after a success, a later refusal lent its content to the `frame_validated` row; and **a pause whose record read no frame** (`window_missing`, `target_replaced`, `target_identity_unavailable`, `process_exited`) fell through the panel's mapper to **INVALID OR UNVERIFIED FRAME** — the same false claim one layer up. | found in the NF-23 verification pass by reading the refusal envelope against the panel's own mapping; reproduced deterministically (a `refuse_window` seam and a `content_inspection=False` router) | **FIXED** — `capture_ok` now answers exactly "did a backend produce pixels?" (`produced`, independent of inspection); the refusal's `content` is derived from what was actually read (`blank`/`repeated`/`dark`/`unverified`, `not_target` only for a real wrong-target read); its `code` keeps the backend's own structured token when no frame existed; the ladder reports `frame_validated` evidence only from the record that row describes; and the panel maps a no-frame pause to the target/identity state it is (a missing window is TARGET HIDDEN OR MINIMIZED with its own detail, a replaced target is TARGET IDENTITY MISMATCH, an exit record is NOT OBSERVED) instead of inventing an invalid frame. Regression tests: 2 in `tests/test_capture_backends.py`, 1 in `tests/test_novaforge_observation.py`, 1 in `tests/test_novaforge_nf23.py`, plus four new node cases in `ObservationLiveStateTests` (`windowMissing`, `identityUnavailable`, `replaced`, `exitedNoFrame`). |

---

## 5. Architecture and code changes

No parallel subsystem was introduced: every change is inside the existing capture/validation/observation
stack.

| File | Change |
|---|---|
| `src/novacontrol/desktop/capture.py` | worker `display`/`printwindow` verbs accept an optional `path⇥thumb` second field; `Save-NCThumb` writes the 48×27 BGR read; `CaptureResult.thumbnail`; `capture()`/`capture_window(thumbnail=True)`; `THUMB_SUFFIX` re-export; `sweep_orphan_thumbnails()`; `stats()` unchanged |
| `src/novacontrol/desktop/capture_validation.py` | `THUMB_WIDTH/HEIGHT/BYTES/SUFFIX`; `inspect_frame(thumbnail=…)` with `_thumbnail_pixels()` (BGR→RGB, unusable side car ⇒ full decode, never a guess); the same statistics/hash code for both pixel sources |
| `src/novacontrol/desktop/capture_backends.py` | `FrameCapture.thumbnail`; `WindowSurfaceBackend` forwards the side car (PrintWindow's read covers exactly the window); the display-crop backend never uses one (a screen-wide read is a different region); `CaptureRouter` consumes + deletes the side car after judging, and `_corroborated_dark` implements F-01; **F-08**: the refusal envelope's `capture_ok` is "a backend produced pixels" (`produced`, not "a verdict exists"), its `content` describes the read (`blank`/`repeated`/`dark`/`unverified`, `not_target` only for a wrong-target read) and its `code` keeps the backend's structured token when nothing was produced |
| `src/novacontrol/desktop/controller.py` | `capture_screen`/`capture_window` pass `thumbnail` through to the worker; `_take_screenshot` reports the side car path; the one-shot fallback leaves it empty (truthful); **F-07**: `_launch_open` starts an absolute `.exe` from its own directory (shell/URI/`.lnk` targets keep the inherited cwd) — `_window_launch_command` itself is unchanged, so the pure routing contract and its tests are untouched |
| `src/novacontrol/novaforge/manager.py` | `OBSERVATION_LADDER_STAGES`/`EMBEDDING_LADDER_STAGES`, `observation_ladder(title)` (catalog, live observation, embedding history, each with its own evidence), `_capture_capabilities()` on the capabilities payload; frame pruning also sweeps orphan side cars; **F-08**: the `frame_validated` row takes its evidence only from the record that stage describes (a later refusal cannot lend it a `blank`/`not_target` content) |
| `src/novacontrol/novaforge/state_service.py` | F-06: `_pause_refusal` (pause/inactive) applied before the frame checks and after perception; the declared limitation text now matches the behaviour |
| `src/novacontrol/web/static/js/novaforge.js` | `observationLiveState()` (8 labels), `observationLadderRows()`, `observationLadderHeadline()`, refusal code → label mapping, the backend attempt trail, the ladder card renderer; the poll still reads only the status (the ladder is read on demand) |
| `src/novacontrol/web/static/index.html`, `nc-os.css` | the observation card's ladder block (`#forgeObservationLadder`) above the history, styled with the existing observation card's type scale |
| `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py`, `docs/API.md` | the ladder rides the existing `/novaforge/capabilities` payload with an optional `title` query; the API reference is regenerated (`--check` below) |
| `tests/test_novaforge_nf23.py` (**new**, 45 tests) | the ten-stage separation (no stage borrowing another's evidence), embedding rows measured but never gating, the capabilities payload, the eight live states through the real `novaforge.js` under node, the ladder DOM block, and the F-06 refuse-on-pause/refuse-on-ended pins |
| `tests/test_capture_backends.py`, `tests/test_novaforge_observation.py`, `tests/test_desktop.py`, `tests/test_novaforge_ui.py` (extended) | F-01 corroboration matrix, the side-car plumbing and orphan sweep, the thumbnail pass-through, the ladder/refusal rendering, and the F-07 launch-cwd contract (an exe from its own folder; a URI keeps the inherited one); **F-08** pins: a refusal with no frame carries no identity claim (router + manager record), a produced-but-uninspected frame still says a frame existed, a blank refusal says `blank`, and the ladder's validated row cannot borrow a later refusal's content |

Public API shape: unchanged except the additive ladder inside the capabilities payload and its optional
`title` query. `python scripts/generate_api_reference.py --check` → `docs\API.md: in sync`.

---

## 6. Capture-backend comparison (Objective B)

Both shipped backends, as declared and as measured. Every "measured" row comes from a real window on this
host (the probe's own disposable window — reported as such; no game row exists, see §9).

| Behaviour | `printwindow` (window surface) | `gdi_display` (display crop) | How it was measured |
|---|---|---|---|
| Declared capability | `unfocused=true, occluded=true, minimized=false, requires_foreground=false` | `unfocused=false, occluded=false, minimized=false, requires_foreground=true` | `CaptureRouter.capabilities()` in `.nc23_tmp/lifecycle_probe.log` |
| Foreground target | **PASS** 840×600, content `valid` | eligible (foreground) | `c_unfocused` row (the probe window was foreground) |
| Unfocused target (another window has focus) | **PASS** — 35.7 ms, content `valid` | **not eligible** (`target_not_foreground` refusal, by declaration) | `d_*` rows |
| Covered target (probe's own TopMost cover, same rect) | **PASS — the target's own pixels**: frame means `[35.86, 15.03, 27.06]` = the target's colours `[35.82, 14.87, 26.94]` | would serve the *cover*: display crop mean `[100.39, 131.83, 112.42]` ≠ target | `d_covered_*` / `d_display_crop_rgb_means` |
| Minimized target | refused `target_minimized`, **no frame written** | refused (not eligible) | `e_minimized_*` (`IsIconic true`, refusal 0.1 ms, `e_frame_written false`) |
| Restored target | **PASS** on the first tick after restore (48 ms), dims = live rect | eligible once foreground | `f_restored_*` |
| Resized target | dims **follow the live rect** (frame 1920×1360, window bounds 320,320→2240,1680) | crop follows the same bounds | `g_resized_*` |
| Window destroyed | refused `target_unreadable`, no frame (303 ms from WM_CLOSE to the window being gone) | n/a | `h_*` |
| Title re-used by a different process | old handle **refused** (`target_unreadable`), never adopted | n/a | `i_*` (new handle 592628 vs old 1379108) |
| Backend unavailable | `capture_unavailable` refusal naming the reason; never a substituted target | same | router refusal paths + `tests/test_capture_backends.py` |
| Side car | written and used (it describes exactly the window) | never used (a screen-wide read is a different region) | §11 measurements |
| Windows Graphics Capture | **`UNTESTED CANDIDATE`** — not implemented, not claimed | — | no measured gap justified it: PrintWindow served every real target exercised |

**No new backend was added.** The measured rows above are exactly why: the shipped window-surface backend
answered every case a real game can present on this machine, so the phase's rule ("do not build a new capture
subsystem if a targeted correction to an existing backend resolves the issue") applied verbatim.

---

## 7. Window lifecycle and integration results (Objective D + E)

Measured on the probe's own disposable WinForms window (`.nc23_tmp/lifecycle_window.ps1`) through the **real**
pipeline: `LocalDesktopRunner`-shaped seams → `CaptureRouter` → `GameObserver` → `GameStateService`.
Command: `python .nc23_tmp/observer_lifecycle_probe.py` (logs: `.nc23_tmp/observer_lifecycle_probe.log`).

| Condition | Measured result |
|---|---|
| Bound target | `handle:4982184:pid:25396:start_time` pinned; interval requested 0.5 s, applied 0.5 s |
| Live frames | **8 consecutive** `observed`, backend `printwindow`, source `window_surface`, dims 840×600 = the live window rect, content `valid` |
| Focus away | the disposable window was **never** foreground; frames kept arriving (the window-surface backend does not require focus) |
| Minimized | state `paused`, code `target_minimized`, **no frame**; counts frozen at 10 observed / 1 `unavailable` row with `consecutive_failures: 0` (a target state is not a capture failure) |
| Restored | state `observing` again, new frames (seq 20/21), each revalidated (fresh, dims = live rect) |
| Process exited | state `stopped`, code `process_exited`, reason "Process 33688 is no longer running", no further frames |
| Window destroyed, process alive | state `paused`, code `window_missing`, reason "…The process is still running, so this is a missing or recreated window, not an exited game." (identity probe answer — the confirm-once rule applies) |
| Downstream while paused (NF-16) | `GameStateService.analyze()` → `observation_paused` ("Observation is paused (target_minimized)…"), `counts.analyzed` unchanged, `last_refusal` recorded |
| Downstream after exit | `analyze()` → `observation_inactive` ("Observation has stopped (process_exited)…") |
| Downstream while live | at a 2.5 s observation interval: `analyzed: true`, `state-c56270575df2`, observation reference `obs-8acbbef5239b:0004` (same observation id, backend `printwindow`, 840×600), processing 775.8 ms (`python .nc23_tmp/consumer_probe.py`) |
| Observation failure ⇒ input? | impossible by construction: the observation pipeline holds **no** injector (`manager.action_input is None`), sends no input and creates no session; the gameplay gate (`input_safety_decision`) additionally refuses a paused/unvalidated session. **Zero input operations were performed in every probe** |
| UI never labels an invalid frame LIVE | the eight-state mapping is pinned against the real panel module (§8); an unavailable/rejected read maps to a non-live label by its refusal code |

Every one of these facts also has a deterministic pin in the suites (§8), so the behaviour is not only
"observed once on this machine".

---

## 8. Test commands and outcomes

| Command | Result | Duration |
|---|---|---|
| `python -m pytest tests/test_novaforge_nf23.py -q` | **45 passed** (44 at first report + the F-08 ladder-evidence pin) | 4.3 s |
| `python -m pytest tests/test_desktop.py -q` (after the F-07 fix) | **81 passed, 20 subtests passed** | 20.8 s |
| `python -m pytest tests/test_capture_backends.py tests/test_novaforge_observation.py tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py tests/test_novaforge_nf23.py -q` | **344 passed** | 58.2 s |
| `python -m pytest tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_autonomy.py tests/test_novaforge_closed_loop.py -q` | **277 passed** (21 subtests) | 43.6 s |
| `python -m pytest tests/test_novaforge_state.py tests/test_novaforge_observation.py -q` | **106 passed** | 20.0 s |
| `python scripts/generate_api_reference.py --check` | `docs\API.md: in sync` | 1 s |
| `python -m pytest tests/test_api_reference_sync.py -q` | **6 passed, 372 subtests** | 5.3 s |
| full suite + both mypy views | see §12 (run last, after every code change) | — |

### 8.1 Independent verification pass (2026-10-27, after the first report was written)

The phase was then re-verified end to end against its own objectives: the refusal envelope was read against
the panel's mapper, the ladder's evidence checked against its records, and every phase suite re-run. This pass
found **F-08** (§4), fixed it with four regression tests plus four new node cases for the no-frame pause mapping, and re-ran the affected surfaces:

| Command | Result | Duration |
|---|---|---|
| `python -m pytest tests/test_novaforge_nf23.py -q` | **45 passed** | 4.3 s |
| `python -m pytest tests/test_capture_backends.py -q` | **72 passed** | 16.0 s |
| `python -m pytest tests/test_novaforge_nf23.py tests/test_capture_backends.py tests/test_novaforge_observation.py tests/test_novaforge_state.py tests/test_novaforge_ui.py -q` | **381 passed, 94 subtests passed** | 25.7 s |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_launcher.py tests/test_desktop.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | **282 passed, 495 subtests passed** | 2 min 08 s |
| `python -m mypy src` | `Success: no issues found in 451 source files` | — |
| `python -m mypy src --platform win32` | `Success: no issues found in 451 source files` | — |
| `python scripts/generate_api_reference.py --check` | `docs\API.md: in sync` | — |

The same pass re-ran the live acceptance probe (attempt 5, §9) to re-check the external blocker; the failure
was unchanged, and no process was left behind (`Get-Process` for `Freedom`/`Launcher`: none).

#### NF-23 test groups

The NF-23 file's own tests are grouped as: `ObservationLadderTests` (stage separation, evidence per stage,
ambiguity, unreadable catalog, unbound question), `LadderEmbeddingTests` (measured-only embedding rows that
never gate capture), `LadderApiTests` (the payload, the `title` query, the read-only poll),
`StatePauseRefusalTests` (F-06), `ObservationLiveStateTests` (node, real `novaforge.js`: 8 states, one label
each, static and dark-but-valid frames still LIVE, stale/blank/mismatch/backend-error distinct),
`LadderSurfaceTests` (DOM block + single mapping + the poll never reads the ladder).

Nothing was deleted, skipped or weakened. No assertion was relaxed to obtain a green result.

---

## 9. Live-game evidence (Objective H) — and the exact blocker

**Target selected during reconnaissance:** *Freedom Fighters* (Steam `1347780`,
`E:\steam\steamapps\common\Freedom_Fighters\Freedom.Exe`) — single-player, offline, small, and the title the
earlier H02/H03 characterisation used. Excluded by the phase's own rules or by safety: GTA V (never launched,
by instruction), Shadow Fight Arena and Supermarket Together (online multiplayer), Hitman 3 (heavy D3D12 title
against a ~2 GB memory headroom), Steamworks redistributables (not a game).

**What the operator's own machine shows:** Steam is running (pid 4740) and the title is fully installed.
The H03 report records that its run used a game the operator had **already started manually** ("No game process
was launched. Freedom Fighters (PID 28600) was already running"), so the *supported launch path* for this title
had never been exercised by NovaForge before.

**Attempt 1** (`python .nc23_tmp/live_game_probe.py`, gate open: free 2.14 GB / 86.1 % used):

* Discovery: `INSTALLED_AND_LAUNCHABLE` — artifact `steam:1347780`, executable present.
* Launch: the application's own composition (`GameCatalog` + `GameLaunchService`, the same
  `_build_game_service` wiring) issued Steam's deep link and verified it. Result:
  `INSTALLED_BUT_LAUNCH_FAILED` — *"A process appeared (Launcher, pid 18076) but no window did within 30 s;
  the launch is not verified. The instance stays tracked as this service's own."*
* Steam's own console log (the authoritative account of what Steam started):

  ```
  [2026-10-27 16:20:03] GameAction [AppID 1347780, ActionID 11] : LaunchApp changed task to CreatingProcess
  [2026-10-27 16:20:04] Game process added : AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 18076
  [2026-10-27 16:20:04] GameAction [AppID 1347780, ActionID 11] : LaunchApp changed task to WaitingGameWindow
  [2026-10-27 16:20:06] Game process removed: AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 18076
  ```

  i.e. the title's **own launcher exited 2 s after starting, with no window**, so no game window existed to
  observe. No process was left behind (verified: no `Freedom`/`Launcher` process afterwards), nothing was
  killed, and the graceful close reported `NO_WINDOW` — *"The process has no window to ask to close. A graceful
  close is not possible through J.A.R.V.I.S.; no process was killed."*
* No duplicate instance was started, no input was sent, no display mode was changed, and no other application
  was touched. The launch was bounded to one attempt.

**Attempt 2** (same command, minutes later): the **memory gate refused** before anything was launched —
`{"memory_before": {… "free_gb": 1.91, "used_percent": 87.6}, "blocked": "the resource gate is closed; no game
was launched"}`. This is the gate working as designed (≥2 GB free **and** ≤92 % used) on a machine whose
operator has several heavy applications resident; it is recorded as such rather than bypassed.

**Attempt 3 — the executable itself, through the product's own launch seam.** With the gate open again
(free 3.4 GB / 77.9 % used) the same probe used
`LocalDesktopRunner.launch_game_target(kind="path", target=<the catalog's own verified executable>)` — the seam
the catalog launcher itself drives — and then waited **91 s** for the window:

```json
{"window_found": false, "window": null, "processes_after_start": [], "window_wait_seconds": 91.12}
```

No process at all appeared. Running the executable directly, with the working directory captured explicitly,
gave the mechanism:

```
EXITED rc=1 (0x00000001) after 0.02s
```

The Steam console log for the same minute shows the executable asking Steam to relaunch the app — i.e. its
own `SteamAPI_RestartAppIfNecessary` ran — and Steam's relaunch dying exactly like the first attempt:

```
[2026-10-27 17:04:22] Game process added : AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 28428
[2026-10-27 17:04:23] GameAction [AppID 1347780, ActionID 12] : LaunchApp changed task to WaitingGameWindow
[2026-10-27 17:04:25] Game process removed: AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 28428
```

**Attempt 4 — the launcher's own output (the decisive measurement).** Running
`Launcher.exe` with its stdout captured, from the game's own directory, prints exactly one line before exiting:

```
rc 1 after 0.03 s
output: SteamAPI_Init failed
```

The binary's own strings say what it is: `SteamAPI_Init`, `SteamAPI_Shutdown`,
`SteamAPI_RestartAppIfNecessary`, `SteamAPI_Init failed`, `.\Freedom.exe`, `-SKIP_LAUNCHER`, `freedom.ini`,
`\IO Interactive\Freedom Fighters\` (product “Freedom Fighters Launcher”, file version 1.0.0.0).
`Freedom.Exe` imports `steam_api` too. So the title is a thin launcher plus the 2003 game, and **both fail at
Steam API initialisation** — before any renderer, window or game code runs.

**The environment around that failure is healthy, measured at the same time:**

| Fact | Measurement |
|---|---|
| Steam client | running (pid 4740); `connection_log.txt` ends in `Logged On … 'OK'` **today 15:51:04** |
| Offline mode | `WantsOfflineMode 0`, `OfflineMode` unset — not offline |
| Title installation | `appmanifest_1347780.acf`: `StateFlags 4` (fully installed), `buildid 6153020`, `SizeOnDisk` 666 MB |
| App id the game will use | `steam_appid.txt` = `1347780` (verified byte-wise) |
| Crash evidence | **none** — no `CrashDumps` entry, no Windows Error Reporting event, no `error.log` |
| Window evidence | **none** — no window ever existed, so no identity could be bound |

**Attempt 5 — the independent verification pass** (`python .nc23_tmp/live_game_probe.py`, 2026-10-27 18:21
local; gate open: free **3.63 GB / 76.4 % used**). The same probe was re-run end to end through the product's
own composition, with no code change to the launch path since the fix of F-07:

* Discovery: `INSTALLED_AND_LAUNCHABLE` — `steam:1347780`, executable present, no running instance.
* Launch through `GameCatalog` + `GameLaunchService`: `INSTALLED_BUT_LAUNCH_FAILED` — *"Started 'Freedom
  Fighters' but no new matching process appeared within 120s."*; no owned record, so no window identity to bind.
* Steam's own log for that minute shows the identical 2-second death:

  ```
  [2026-10-27 18:21:36] Game process added : AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 30424
  [2026-10-27 18:21:37] GameAction [AppID 1347780, ActionID 13] : LaunchApp changed task to WaitingGameWindow
  [2026-10-27 18:21:39] Game process removed: AppID 1347780 "E:\STEAM\steamapps\common\Freedom_Fighters\Launcher.exe", ProcID 30424
  ```

* Nothing was left behind: a `Get-Process` sweep for `Freedom`/`Launcher` afterwards lists none; memory
  **3.25 GB free / 78.8 % used**. Steam itself was running (pid 4740) and had launched other titles.
* No alternative installed offline title exists on this host to substitute: the same pass listed the attached
  volumes (`Get-Volume`) as **C:, D:, E: only** — the `G:` volume that holds the non-Steam Hitman 3 install
  from H01 is not attached, and the phase forbids using the disconnected external drive as a game source.

So the blocker is **re-measured, unchanged, and not a NovaForge behaviour**: the title's own launcher exits
before any window exists, and it does so identically when Steam starts it.

**Disposition.** The live-game acceptance is **BLOCKED**, not failed and not passed. The precise blocker is:

> On this machine, at this time, **Steam API initialisation fails for this title**
> (`SteamAPI_Init failed`, rc=1, 0.03 s), so neither the title's launcher nor the game itself ever reaches a
> window — and Steam's *own* launcher produces the same failure, which is why this is a host/Steam-side fact
> rather than a NovaForge behaviour. No window ⇒ no identity to bind ⇒ no frame can be acquired by any
> supported path. The phase's rules forbid bypassing it (no forced display mode, no process killing, no
> second instance, no code injection), so the attempts were stopped and recorded.

Independent support for the same conclusion: the *product* never claimed a launch either. The catalog launcher
reported `INSTALLED_BUT_LAUNCH_FAILED` with *"A process appeared (Launcher, pid 18076) but no window did
within 30 s; the launch is not verified"*, kept the instance tracked as its own, refused to verify a window
that did not exist, and the graceful close reported `NO_WINDOW` — *"The process has no window to ask to
close. A graceful close is not possible through J.A.R.V.I.S.; no process was killed."* That is the ladder's
honesty rule working on a real failure.

**F-07 was found in this investigation and fixed** (§4): the product's path launch started an executable with
the *application's* working directory, so a title that resolves assets beside itself was started from the
wrong place (`rc=1` in 0.02 s, no window). The fix starts an absolute `.exe` from its own folder, pinned by
two tests; the observation path is therefore not blocked by a launch-side defect of ours.

**How to clear the blocker (for whoever can interact with Steam on this desktop):**

```bash
# 1. clear the Steam-side failure FIRST — it is not a NovaForge concern and the retry is pointless without it.
#    Steam -> right-click Freedom Fighters -> Properties -> Installed Files -> Verify integrity of game files,
#    then launch it once from the Steam client itself. If the launcher still prints "SteamAPI_Init failed",
#    the client's handshake with this title is what has to be repaired (re-login, client restart, or a
#    security product blocking the game<->Steam pipe). Evidence: the same failure happens when Steam starts it.
# 2. gate (>= 2 GB free and <= 92 % used):
powershell -NoProfile -Command '\$os=Get-CimInstance Win32_OperatingSystem; \$os.FreePhysicalMemory/1MB'
# 3. once a game window exists, bind it through the product path — the probe below is exactly that flow
#    (its launch step is the only part that needs replacing with start_observation on the live window):
python .nc23_tmp/live_game_direct_probe.py     # or .nc23_tmp/live_game_probe.py for the Steam deep-link path
```

No claim is made anywhere in this report that a real game frame was acquired.

---

## 10. Simulation / disposable-window results, separated from live evidence

Per the phase's §13, these are **not** real-game compatibility results. They are reported separately and
labelled as what they are: a real **window** (WinForms, the probe's own), the real capture stack, the real
observer, real validation.

| Evidence | Kind | Where |
|---|---|---|
| Window capture, side car, channel order, verdict equivalence | real window, disposable | `.nc23_tmp/probe_thumb_and_perf.log`, `.nc23_tmp/lifecycle_probe.log` |
| Backend rows: unfocused / covered / minimized / restored / resized / destroyed / replaced | real window, disposable | §6/§7 |
| Observation pipeline: 8+ frames, pause, restore, process exit, downstream refusals | real window, disposable | §7 |
| Eight-state UI mapping, ladder payload, ladder DOM | deterministic (node + DOM shim + HTTP) | §8 |
| Dark-frame corroboration (F-01) | deterministic (blank+dark-agree accepted; blank+painted refused; single backend ⇒ refuse; not-foreground ⇒ no corroboration) | `tests/test_capture_backends.py` |
| Resource-gate refusal | real (measured twice: launch gate at 1.91 GB; the service's own `memory_check`) | §9 |

A successful fixture run cannot substitute for a real-game compatibility result, and it is not used as one here.

---

## 11. Performance and resource measurements (Objective F)

Baseline first, on the same code path, repeated samples (not a single good frame).

**Validation (the F-02 change), 2880×1800 display frame of the real desktop** — `.nc23_tmp/probe_thumb_and_perf.log`:

| Path | Runs | min | median | max |
|---|---|---|---|---|
| `inspect_frame(path)` (full PNG decode) | 5 | 69.11 ms | **71.27 ms** | 91.02 ms |
| `inspect_frame(path, thumbnail=…)` | 5 | 1.29 ms | **1.43 ms** | 2.02 ms |

Same pixels judged: reference RGB means `[62.74, 54.98, 45.90]` vs side car `[62.91, 55.10, 46.01]`
(per-channel deltas 0.11–0.17, so the BGR→RGB order is right), `blank` verdict identical, `mean_luma`
55.94 vs 56.10. A synthetic 5.2 MP PNG measured 32.71 ms by the same code path.

**Window-surface capture, 20 consecutive frames at a 0.5 s cadence** — `.nc23_tmp/perf_probe.log`:

| Figure | Value |
|---|---|
| Frames requested / accepted | 20 / 20 (validated rate **1.0**) |
| Capture latency | min 30.6 ms, **median 37.4 ms**, p95 53.7 ms, max 170.7 ms (first, cold) |
| Schedule delay | median 6.8 ms, p95 14.3 ms, max 15.0 ms — the 0.5 s cadence was met (2.0 Hz achieved) |
| Validation (side car) | min 2.4 ms, **median 3.7 ms**, max 5.7 ms |
| Dimensions | 840×600 = the live window rect on every frame |
| Content | `valid` on every frame |
| Worker resource | idle working set **104.8 MB**, growth **+7.6 MB** over 20 frames, CPU **38.2 ms/frame**, one worker process for the whole run |
| Display-crop path for comparison (5.2 MP) | 329–355 ms per frame — ~10× the window path; the reason a game observation uses the window surface |

**Time-to-stale / target loss** (measured, same run and §7):

| Event | Measured |
|---|---|
| A frame 3.65 s old, read after the 3.0 s window | `livable=false`, `content="valid"`, reason "The frame is stale.", limitation names the window — **an old frame is never presented as current** |
| Minimized target noticed | 0.1 ms (geometry read; no capture attempted, no frame written) |
| Destroyed window noticed | window gone 303 ms after `WM_CLOSE`; capture refused `target_unreadable` |
| Process exit reported | `process_exited` on the observer's next poll (0.5 s cadence with the identity cache) |
| Validation share of one frame, before/after F-02 | 71 ms of a ~330 ms display frame (≈22 %); 1.4 ms (≈4 %) after |

No frame-rate claim beyond these measurements is made. The observer's own bounds (0.25–5 s interval,
default 1 s) and the stated limitation that capture is scheduled no faster than the interval remain the
operating envelope.

**One measured caveat, recorded rather than hidden:** at a 0.5 s observation interval an NF-16 state read is
discarded as `observation_superseded` (a newer capture landed while perception ran; perception measured
775.8 ms). At a 2.5 s interval the same read completes and carries the exact observation reference. This is
the NF-16 freshness guard working as designed; the practical guidance is *read state at an interval longer than
perception*, and it is a documented behaviour, not a defect introduced here.

---

## 12. Regression analysis

**Changed behaviour, and what could observe it:** the side car (new worker output + validation input), the dark
corroboration path, the manager's ladder + capabilities payload, the panel's state mapping, the state
service's pause/inactive refusals, the launch working directory for `.exe` targets (F-07), and the refusal
envelope's `capture_ok`/`content`/`code` plus the ladder's validated-row evidence (F-08). Suites re-run
after those changes: NF-23 (44), capture/observation/state/objectives/knowledge (344),
state/objectives/autonomy/closed-loop (277), desktop (81 + 20 subtests, including the two new F-07 pins),
API sync (6 + 372 subtests) — all green. The full suite and both mypy views are run last, after the final
edit, and their result is recorded here:

* `python -m pytest tests/ -q` — result recorded in §12.1 below
* `python -m mypy src` — result recorded in §12.1 below
* `python -m mypy src --platform win32` — result recorded in §12.1 below

### 12.1 Final full-suite and type-check results

Run last, after the final edit (including F-07 and its tests), on this host:

| Command | Result |
|---|---|
| `python -m pytest tests/ -q` | **5733 passed, 1 skipped, 3847 subtests passed** in **1828.25 s (30:28)**, exit code **0** (log: `.nc23_tmp/full_suite_final.log`) |
| `python -m mypy src` | `Success: no issues found in 451 source files` |
| `python -m mypy src --platform win32` | `Success: no issues found in 451 source files` |
| `python scripts/generate_api_reference.py --check` | `docs\API.md: in sync` (exit 0) |

The single skip is an environment-gated test in the tree's existing suites (`skipTest` guards on browser /
Playwright availability); it is not an NF-23 test and no assertion was weakened to obtain the green result —
NF-23's own tests all ran and passed.

**After the independent verification pass's F-08 fix** (refusal envelope in `desktop/capture_backends.py`, the
ladder's evidence in `novaforge/manager.py`, and the panel's pause mapping in `web/static/js/novaforge.js`) the
affected surfaces were re-run
rather than the 30-minute full suite: the phase group **381 passed + 94 subtests** (§8.1), the dependency
group **282 passed + 495 subtests** (§8.1), both mypy views clean, API reference in sync, and NF-23's own
suite 45/45. The full-suite line above therefore describes the tree *before* F-08; no other module imports the
changed code paths (`CaptureRouter` and `NovaForgeManager.observation_ladder` are reachable only from the
surfaces re-run), so the omission is stated rather than silently implied to be covered.

**Unrelated work preserved:** the pre-existing working-tree entries remain in place (no reset, no stash, no
revert, no commit), and every probe ran in `.nc23_tmp/` or a temp directory. The independent verification pass
touched only NF-23-owned code, its tests and its two documents, plus two untracked probe scripts under
`.nc23_tmp/`, taking `git status` from 141 entries at phase start to 145 (59 modified / 86 untracked); no
unrelated tracked file was edited or reverted. `.gitignore` already excludes the runtime `data/` tree and
scratch directories.

---

## 13. Acceptance matrix

### Functional acceptance (phase §13)

| Criterion | Verdict | Evidence |
|---|---|---|
| One real game is reliably identified | **PASS** | `GameCatalog.resolve("Freedom Fighters")` → `INSTALLED_AND_LAUNCHABLE`, `steam:1347780`, executable present (§9) |
| A supported observation path acquires multiple consecutive real-game frames | **BLOCKED** | no game window could be produced by any supported path — the title's Steam API initialisation fails on this host (measured, §9), so the game itself exits in 0.02 s and Steam's own launch of it dies in 2–3 s. The same observation path acquired 8–20 consecutive frames of a real window (§7, §11) |
| Each accepted frame correctly associated with the target and passing freshness/validity checks | **PASS (real window)** | dims = live rect on every frame, content `valid`, `fresh` from the file, identity pinned (handle+pid+start time), stale refused (§7, §11) |
| Invalid or unavailable frames cannot be represented as live observations | **PASS** | `livable` conjunction; the eight-state mapping pinned against the real panel module; a refusal with no frame carries `capture_ok: false` + `content: unverified` (F-08), so it maps to BACKEND ERROR rather than an identity claim; the ladder never marks a later stage true from an earlier one (§8) |
| Focus and window lifecycle behaviour handled correctly | **PASS (real window)** | §7 rows |
| Capture failure propagates safely through the existing pipeline | **PASS** | pause with no frame, `unavailable` rows, downstream `observation_paused` / `observation_inactive` (F-06 fix), no injector in the observation path |
| Existing architecture and public contracts remain consistent | **PASS** | additive capabilities field + optional query; `docs/API.md` in sync; `tests/test_route_shape_contract.py` and the API-sync suite green |
| No duplicate automation or capture subsystem introduced | **PASS** | all changes in `desktop/` + `novaforge/`; one ladder derived from existing facts |

### Quality acceptance

| Criterion | Verdict | Evidence |
|---|---|---|
| Every confirmed NF-23 defect has a regression test | **PASS** | F-01 (`test_capture_backends.py`), F-02 (side-car plumbing/equivalence/orphan sweep), F-03/F-04 (ladder + eight states), F-06 (5 tests in `test_novaforge_nf23.py`), F-07 (2 tests in `test_desktop.py`), **F-08** (2 in `test_capture_backends.py`, 1 in `test_novaforge_observation.py`, 1 in `test_novaforge_nf23.py`, plus the node case in `ObservationLiveStateTests`) |
| Required targeted and integration tests pass | **PASS** | §8 and the independent verification pass in §8.1 (381 passed + 94 subtests in the phase group; 282 passed + 495 subtests in the dependency group) |
| Relevant type checks pass | see §12.1 | mypy views |
| API documentation synchronized | **PASS** | `--check` → in sync |
| UI and API status reporting consistent | **PASS** | the panel maps the same `reason_code`/state vocabulary the pipeline emits; the capabilities payload carries the same ladder the panel renders |
| Resource limits respected | **PASS** | gate re-read before each attempt; one refusal recorded and honoured; 20-frame session grew the worker's working set by 7.6 MB (§11) |
| No unrelated user changes lost | **PASS** | §12 |
| Verification report contains reproducible evidence | **PASS** | this document + the probe logs named throughout |

### Evidence requirements

| Requirement | Verdict | Notes |
|---|---|---|
| Live-game claims carry game/process/window identity, backend, duration, frame counts, validation, latency, resources | **N/A — no live-game frame claim exists** | §9 states the blocker instead |
| Simulation/disposable results reported separately from live results | **PASS** | §10 |
| A successful fixture is not used as a real-game compatibility result | **PASS** | §1, §10, §13 |

---

## 14. Remaining known issues and external blockers

1. **B-01 (root cause measured, still open): `SteamAPI_Init failed` for the selected title on this host.**
   The launcher prints exactly that and exits rc=1 in 0.03 s; Steam's own launch of the same title fails
   identically (console log) and the game executable exits rc=1 in 0.02 s after asking Steam to relaunch it.
   No window, no crash dump, no WER event; the client is logged on and the title is fully installed (§9).
   **Re-measured in the independent verification pass (attempt 5, 2026-10-27 18:21):** same result — Steam's
   log shows `Launcher.exe` added and removed 2 s later, no process left behind, gate open the whole time. This
   is the **only** reason the live-game criterion is BLOCKED, and it is a Steam/host fact, not a NovaForge
   behaviour: the product reported the launch as unverified rather than claiming success. The host also has
   no alternative installed offline title to substitute (only C:/D:/E: are attached; the G: volume holding the
   non-Steam Hitman 3 install from H01 is not).
2. **A real-game frame still has to be acquired after that is cleared.** The remaining step is environmental:
   repair the title's Steam handshake (verify the install, launch it once from the Steam client, restart or
   re-login the client, check for a security product blocking the game↔Steam pipe), then run
   `.nc23_tmp/live_game_direct_probe.py` — it performs the whole acceptance sequence (bind identity, ≥10 frames,
   latency, focus-away, restore, stop, graceful close, exit confirmation) on the already-verified path (§7).
3. **Validation hash is path-scoped.** The side car's reduction (GDI+ HighQualityBicubic of the surface) and the
   full-decode path (box-reduce then resize) agree on verdicts and statistics within measurement noise
   (mean_luma 55.94 vs 56.10, deltas ≤ 0.21 per channel) but are not bit-identical, so their content hashes
   differ. Consequence: the `repeated` limitation is judged per source; a mid-session backend switch can miss
   (or add) one `repeated` note. It never changes `livable`. Recorded, not hidden.
4. **Windows Graphics Capture remains an `UNTESTED CANDIDATE`.** No measured gap justified implementing it —
   PrintWindow answered every real target exercised (foreground, unfocused, covered, resized, restored).
5. **Dark-scene corroboration was not measured against a real dark game frame** (no dark real target was
   available: the selected title never produced a window). F-01 is pinned deterministically on both branches
   (dark+dark ⇒ accepted as `dark` with its limitation; dark+painted ⇒ refused).
6. **Addressing (for the next phase, not fixed here):** the state-service/observer interval interaction in §11
   is a UX guidance item for NF-20's planner, recorded here so it is not lost; it is not a defect in NF-23's
   scope.
7. **Losses to watch:** the observation card's ladder is a new UI element; the user-facing checks were the DOM
   pins and the live probes. A visual pass after a reload is the operator's remaining step (the phase's UI rule).
8. **F-08 was found by the independent verification pass and fixed** (§4): the router's refusal envelope
   described a frame that no backend produced, which made the panel say "the frame is not the target" about
   nothing, let a later refusal lend its content to the ladder's validated row, and let a no-frame pause read
   as an invalid frame. Fixed with four regression tests plus four node cases; no API shape changed (the
   envelope keys are the same; only their values are now true of the read).

---

## 15. Final verdict

**PARTIAL.**

* **PASS — the observation path (Objectives A, B, C, D, E's integration behaviour):** on a real window, through
  the real stack, with measured backend rows for every lifecycle condition the phase names, an eight-state UI
  vocabulary pinned against the shipped module, a ten-stage ladder that cannot borrow evidence, and two
  defects found here (F-06, F-07) fixed and regression-pinned. The two H03-recorded quality limits are
  resolved with measurements, and the resolution is pinned on identical pixels.
* **BLOCKED — the live real-game criterion (Objective H):** the title selected for the live test cannot reach a
  window on this machine because **its Steam API initialisation fails** (`SteamAPI_Init failed`, rc=1, 0.03 s),
  and Steam's own launch of the title fails the same way. No real-game frame is claimed, and no substitute
  (simulation, disposable window, fixture) is presented as one.

An independent verification pass re-checked every objective, re-ran the phase and dependency suites, both mypy
views and the API-sync check, confirmed the working tree was not disturbed, and re-ran the live probe: it found
and fixed **F-08** (§4, §8.1) and re-measured the same external blocker (§9, attempt 5). Nothing in the verdict
changed except that the observation path's diagnostics are now true for a refusal that produced no frame.

**NF-24 must not start until the live-game criterion is cleared** (phase §15): the work needed is clearing that
Steam-side failure and running the already-written probe, after which this report's §9 and §13 rows move from
BLOCKED to PASS or to a measured failure with a real frame count.

Nothing in this report is asserted from a successful exit code, a screenshot of a fixture, or a plan.

## 16. NF-23…NF-26 consolidated acceptance (2026-10-28)

A cross-phase remediation and acceptance pass re-audited this phase together with NF-24…NF-26. Full
evidence: `docs/NOVAFORGE_NF23_NF26_CONSOLIDATED_ACCEPTANCE_REPORT.md`; findings:
`docs/NOVAFORGE_NF23_NF26_REMEDIATION_REGISTER.md`.

**Verdict unchanged: PARTIAL** — PASS on the observation path, BLOCKED on the live real-game criterion.

**What this pass added as evidence (no game, no input):**

* the **shipped** application with the real `LocalDesktopRunner` reports `capture_supported: true`,
enumerates 5 windows, and advertises `printwindow` and `gdi_display` as available — the observation path
is reachable in the product, not only through test seams;
* a session over this project's own disposable harness window acquired **3 frames** (printwindow, 1040×440,
`fresh`, content `valid`/`repeated`, all `livable`) and produced 3 observation records, the last at 34.4 ms
with `evidence: true`; `target_validated` became true.

**One defect found here and fixed:** a session-start payload field this build does not read (for example
`max_seconds`, or an objective expressed as `objective_mode`) was **silently discarded**, so a session
could start with no time limit while its operator believed one was set. `NovaForgeManager.validate` now
refuses every field outside the mission plus `expected_handle`/`expected_pid`, naming it and listing the
accepted fields (R-01 in the register). The change is pinned by
`test_an_unknown_start_field_is_refused_by_name` in `tests/test_novaforge.py`.

No claim in this report was strengthened: no live-game frame exists, no input was sent, and the blocker in
§14 remains the title's Steam API initialisation.
