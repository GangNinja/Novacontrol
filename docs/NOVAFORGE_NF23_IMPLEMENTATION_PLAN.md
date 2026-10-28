# NovaForge NF-23 — Real-Game Compatibility and Observation — Implementation Plan

**Phase:** NF-23. **Tagline:** Observe. Decide. Act. Verify. Evolve.
**Status:** implementation **complete**; every deliverable that can be verified on this host is implemented,
measured and pinned. The live real-game criterion is **BLOCKED** by a measured Steam-side failure of the
selected title (`SteamAPI_Init failed`), with the root cause and the retry procedure in §8 and in the report's
§9. No real-game frame is claimed.
**Companion documents:** the NF-13A…NF-22 reports, `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md`,
`docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md`, `docs/NOVAFORGE_UNIVERSAL_CAPTURE_REPORT.md`
(H03), `docs/NOVAFORGE_H01_HITMAN_REPORT.md`, `docs/NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md`,
`docs/NOVAFORGE_H02_1_FOCUS_LOSS_REPORT.md`.

---

## 1. Repository and working-tree state at phase start

| Fact | Value |
|---|---|
| Branch / HEAD | `main` at `8d10852` |
| Working tree | **dirty by design**: 141 `git status` entries of prior NovaControl + NF-01…NF-22 work (59 tracked files modified, 60+ untracked docs/scripts), all uncommitted. Nothing was reset, stashed, cleaned, reverted or committed. |
| Host | Windows 11, Intel Core Ultra 5 125H, Intel Arc iGPU, 16 GB RAM (15.37 GB visible). Python 3.13 in `.venv` (editable install). |
| Resource gate at start | free **2.74–2.91 GB**, used **81–82 %** — inside the ≥2 GB / ≤92 % rule, with little headroom. Re-checked immediately before any launch. |
| Unrelated tree content | `.kilo/worktrees/rune-board/` (another tool's worktree) exists inside the repo and is **not touched, read for edits, or searched for source changes**. |
| Scratch evidence | `.nc23_tmp/` (untracked), like the earlier `.nc22_tmp/`. |

## 2. Prior findings reviewed (and what each means for NF-23)

Read from source, not from the reports' summaries: `desktop/capture.py` (the persistent worker and its
line protocol), `desktop/capture_backends.py` (backends + `CaptureRouter`), `desktop/capture_validation.py`
(`inspect_frame`/`validate_frame`/`reason_code`), `novaforge/observer.py` (NF-15 `GameObserver`),
`novaforge/manager.py` (wiring, `_acquire_frame`, `start_observation`, `capabilities`),
`novaforge/games.py` (the catalog), `novaforge/launcher.py` (discovery + launch), the UI
(`web/static/js/novaforge.js` `frameLiveState` / `loadFrame`), the API routes, and the NF-23-relevant tests.

* **H03 (universal capture)** shipped `printwindow` + `gdi_display` behind `CaptureRouter` with declared
  capabilities and content validation. Two real-host limits are recorded there and **still true**: a
  legitimately black frame is refused (its §10.4, measured), and validation decodes the whole PNG per frame
  (§9: ~35 ms warm / 333–396 ms cold at 5.2 MP, 25–40 % of frame cost).
* **H01** recorded a geometry defect: a session pinned `2560x1600` while the live window was `1920x1200`
  after a display-mode change, so frames carried black padding. The NF-15 observer re-reads window geometry
  **every tick** (`_sample` → `_resolve(title)`), and the worker re-allocates its surface bitmap when the
  window size changes — so the *observation* path is not that defect. NF-23 verifies it live rather than
  assuming it.
* **H02/H02.1** are the reason the current validation separates `capture_ok` / `target_visible` / `fresh` /
  `content_ok` and why `source="screen"` is never livable. Those guarantees are preserved, not revisited.
* **NF-15**'s observer already: pins handle + pid + start time; rebinds only to the same process; pauses
  (never captures) on minimized/hidden/off-screen/not-foreground; records no frame when no backend can see
  the target; discards a frame that lands after a stop; reports `content="repeated"` as a limitation rather
  than a failure; bounds frames/sessions/history on disk.
* **NF-16…NF-22** consume the observer (`GameStateService` → `ObjectiveService` → `KnowledgeService` →
  planner → control → verification). NF-23 must not rewrite them; it verifies the seam.
* **The catalog** (`novaforge/games.py`) lists declared environments only (disposable window, operator
  game window, harness) and declares `launch: not_implemented` for all of them; the real launcher lives in
  `launcher.py` (`GameCatalog` + `GameLaunchService`) and is wired from the desktop runner's Steam manifest
  reader, Start-Menu index, process identity and the memory gate.
* **`docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md`** records NF-23's subject as blocker **B-01**
  ("live game acceptance … an installed, windowed, offline, lightweight title …"). NF-23 exists to clear it
  for observation (not for autonomous gameplay).

## 3. Installed targets on this machine (measured, `.nc23_tmp/probe_inventory.py`)

| Title | Source | Verdict for NF-23 |
|---|---|---|
| **Freedom Fighters** (Steam 1347780, `E:\steam\steamapps\common\Freedom_Fighters\Freedom.Exe`) | Steam manifest, files present | **SELECTED** — single-player only, no online mode, small (2003 title), already characterised in H02/H03. |
| Grand Theft Auto V Enhanced (Steam 3240220) | Steam manifest, files present | **EXCLUDED by phase rule** — never launched. |
| Shadow Fight Arena (Steam 1446890) | Steam manifest, no exe recorded | **EXCLUDED** — online PvP; "do not test online multiplayer automation". |
| Supermarket Together (Steam 2709570, `D:\SteamLibrary\…`) | Steam manifest, files present | **EXCLUDED** — co-op/online title; the phase forbids online multiplayer automation. |
| Steamworks Common Redistributables | Steam manifest | Not a game. |
| Hitman 3 (`G:\games\…`, H01) | Non-Steam install | Not selected — heavy D3D12 title (~100 GB class) with a tight 2.7 GB free-memory gate. |
| Disposable operator window | `scripts/forge_harness_window.ps1` (existing) | **Used for the cases a fullscreen game cannot exercise** (covered/unfocused, resize, destroy/recreate) — reported separately from live-game evidence, per §13. |

No game is running at phase start (process probe: none of the installed titles). No game was launched
during reconnaissance.

## 4. Confirmed defects and gaps, with dispositions

| ID | Finding | Evidence | Disposition |
|---|---|---|---|
| **F-01** | **A legitimately dark frame is refused as `target_unreadable` and pauses the session.** Only one signal (window-surface paint) is consulted; a renderer that answers black and a game painting black are treated as the same event. | H03 §10.4 (measured: `mean_luma 0.0`, `uniform_ratio 1.0` from a foreground game) | **FIX** — consult the *other* eligible backend as an independent signal before refusing a blank surface: if the compositor and the window surface independently agree the region is dark, accept the frame as a dark read (`content="dark"` + limitation); if the compositor shows paint while the surface is black, refuse exactly as today. Never corroborate when the target is not foreground. |
| **F-02** | **Validation decodes the full PNG every frame** (~35 ms warm, 333–396 ms cold at 5.2 MP) — 25–40 % of the per-frame critical path, and it re-reads a file the worker just wrote. | H03 §9; source: `CaptureRouter.capture` → `inspect_frame(path)` | **FIX (measured)** — the worker emits a 48×27 thumbnail beside the PNG it already writes (its bitmap is in memory); `inspect_frame` accepts a thumbnail and computes the *same* statistics/hash from it. Baseline measured first; verdict equivalence pinned on identical pixels; PNG stays for the UI. |
| **F-03** | **The ten-stage ladder is implicit.** No single surface states, per stage (discovered → launchable → process started → window identified → embed-compatible → embedded → backend selected → frame acquired → frame validated → observation accepted), what is true *here* and what the evidence is. Stage facts exist but are scattered across catalog status, launch history, embedding probes, `capabilities()` and observation records. | source reading; `docs/NOVAFORGE_NF13A_NF22_FINAL_VERIFICATION_REPORT.md` §3 (NF-13A "a launched window is not an embedded one") | **ADD** — one derived, evidence-carrying ladder in the manager, served on the existing capabilities route and mirrored in the panel. Every stage names its own evidence and **no stage may be true because a later one is**. |
| **F-04** | **Objective C's eight statuses are not all distinguishable in the UI.** `frameLiveState` renders live/stale/paused/"target not visible — feed not live"/"session ended"; identity mismatch, backend error, capture unavailable and embedding-unsupported have no distinct surface. | `novaforge.js` `frameLiveState`/`loadFrame`, `visionEmptyText` | **FIX** — map the eight states onto the existing vocabulary (no second status system): the refusal's stable `reason_code` selects the label, the backend attempt trail is shown, and embedding status is reported by the existing NF-13A/NF-14 surface. |
| **F-05** | To verify, not assume: frame dimensions vs the live window rect per tick (the H01 defect class); focus-loss → minimize → pause with no frame; restore → revalidation; process exit → `process_exited`; a similar-titled different process → `target_replaced`; downstream (NF-16) not publishing fresh state while observation is paused. | source reading shows the intended behaviour; no live measurement yet | **VERIFIED live** on a real window (report §7; `.nc23_tmp/observer_lifecycle_probe.log`, `consumer_probe.log`) and **pinned** where deterministic. The verification found one real deviation — **F-06**, below — which was fixed and pinned. |
| **F-06** | **A paused or ended observation still produced a state reading.** `GameStateService.analyze()` returned `analyzed: true` from the last frame while the session was paused (`target_minimized`) and after it had stopped (`process_exited`), contradicting its own "stale, paused or missing frame is a refusal with a code" contract. | `.nc23_tmp/observer_lifecycle_probe.log` (first run, `state_after_loss_ok: true`) | **FIXED** — `_pause_refusal` refuses `paused` (`observation_paused`) and `stopped`/`failed` (`observation_inactive`) before perception and again after it; 5 regression tests; re-measured live. |
| **F-07** | **A path-launched executable was started with the application's working directory**, so a title that resolves its assets beside itself was launched from the wrong folder. Measured: `rc=1` in 0.02 s, no process, no window. | `.nc23_tmp/live_game_direct_probe.log`; direct exit-code measurement | **FIXED** — `_launch_open` starts an absolute `.exe` from its own directory (Explorer's behaviour); shell/URI/`.lnk` targets keep the inherited cwd; 2 regression tests in `tests/test_desktop.py`. |

## 5. Implementation steps

1. Write this plan; record the start state (§1–§3). *(done in this file)*
2. **F-01**: corroboration in `CaptureRouter` (+ `FrameVerdict.content="dark"`, `reason_code` unchanged for
   the refusal path), tests in `tests/test_capture_backends.py` (blank+dark-agree accepted; blank+compositor-
   painted refused; not-foreground never corroborated; unavailable second backend ⇒ refuse as today).
3. **F-02**: worker `thumb` support in `desktop/capture.py` (`printwindow <hwnd> <path> [thumbpath]`, plus the
   display verb), `LocalDesktopRunner.capture_window`/`capture` pass-through, `inspect_frame(thumbnail=…)`,
   router wiring, and the **before/after measurement** in `.nc23_tmp/probe_perf.py` (extended) + a
   cross-check test that both paths return the same verdict on the same pixels.
4. **F-03/F-04**: `observation_ladder()` in the manager (live facts + evidence), served through the existing
   `/novaforge/capabilities` payload; UI: the refusal label/attempts and the ladder rows in the panel; DOM
   pins in `tests/test_novaforge_ui.py`.
5. **F-05**: live lifecycle verification (below) plus deterministic pins for anything found wrong.
6. **Live verification (Objective H)** — Freedom Fighters, resource gate checked immediately before launch:
   1. confirm target + no duplicate instance; 2. launch it **through the app's own launcher** (which enforces
   the gate); 3. start observation on the pinned window via the API; 4. acquire ≥8 consecutive frames;
   5. validate every frame (identity/freshness/dimensions/content); 6. record counts, latency distribution,
   memory; 7. switch focus away → record the documented behaviour (expected: the fullscreen game minimizes →
   `PAUSED`/`target_minimized`, **no frame**); 8. restore → revalidated frames; 9. stop cleanly;
   10./11. confirm the UI never labelled an invalid frame live and **no input was sent** (injector counters at
   0); 12. close the game gracefully through the app's own close path and confirm the process exit — if it
   cannot be closed gracefully, leave it running and record that instead of force-killing.
7. **Disposable-window cases** (covered/unfocused capture, resize, destroy/recreate, backend unavailability,
   resource-gate refusal) with the existing harness window, reported separately from live-game evidence.
8. Reports: `docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md` (§1–§15 as the phase text requires);
   update this plan with completed work; reference (never rewrite) earlier reports.

## 6. Risks and controls

| Risk | Control |
|---|---|
| Tight memory gate (2.7 GB free) | Re-check immediately before launch; the launcher itself refuses below the bound (that refusal is itself exercised as a test); no second game, no parallel heavy job. |
| A fullscreen-exclusive game minimizes on focus loss | That *is* the measured behaviour to record (`target_minimized`, no frame). The covered/unfocused backend case is measured on the disposable window and **reported separately** — no claim about the game's covered case. |
| Changing verdicts by "optimising" validation | Both paths are pinned to the same verdict on the same pixels; the PNG path stays available; the measured spread is reported. |
| Touching user-owned state | No display-mode forcing, no game settings changed, no input synthesis, no process force-kill; the tree's 141 pre-existing entries stay untouched; nothing committed. |
| Wrong-game adoption under a similar title | Already refused by the observer's pid/start-time binding; re-pinned with a test at the real seam. |
| Scroll of scope into NF-24 | Objective E fixes only defects necessary for observation integration; everything else is recorded in the register for its own phase. |

## 7. Measurable acceptance criteria

**Live (real game).** One real game identified, launched by the app, observed: ≥8 consecutive frames, every
one `livable=true` with `content=valid` (or an explicitly explained `dark` read), each carrying the pinned
handle/pid, dimensions equal to the live window rect, `0` input operations, latency distribution
(min/median/p95/max) and memory recorded; focus-away produces a pause with **no** frame and no input; restore
produces revalidated frames; stop is clean; the game is closed gracefully with a confirmed exit (or the
outcome is reported as not confirmed — never force-killed).

**Backend compatibility.** A per-behaviour table (foreground, unfocused/covered, minimized, restored,
destroyed/recreated, resized, resolution change, focus change, disappearance) for both shipped backends, with
each row marked measured / refused / not-testable-here. No new backend is added unless a measured gap
justifies it: PrintWindow answers both real targets exercised so far, so Windows Graphics Capture stays an
`UNTESTED CANDIDATE` unless the measurement shows a target it cannot serve.

**Validation.** The dark-frame case is decided by two independent signals; a black surface with a painted
display region is still refused; an accepted dark read is never labelled `valid` without its limitation.

**Performance.** Baseline first: capture latency distribution, validation share, validated-frame rate,
time-to-detect target loss, time-to-stale. Then the thumbnail change re-measured on the same code path, with
the verdict-equivalence test green. **No frame-rate claim beyond what is measured.**

**Quality.** Every fixed defect has a regression test; targeted suites + dependency suites green; both mypy
views clean; `scripts/generate_api_reference.py --check` in sync if the API changed; UI and API status
consistent; the resource gate respected; no unrelated change lost.

---

## 8. Completed work and final outcomes

Every step in §5 was carried out; the report (`docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md`) carries
the evidence, and this section is the plan's own closing record.

### 8.1 What shipped

| Item | Where | Pinned by |
|---|---|---|
| F-01 dark-frame corroboration (two independent reads must agree before a blank frame is served as a dark read) | `desktop/capture_backends.py` (`CaptureRouter._corroborated_dark`) | `tests/test_capture_backends.py` |
| F-02 validation side car (48×27 BGR paint read, `<frame>.thumb`) | `desktop/capture.py`, `desktop/capture_validation.py`, `desktop/capture_backends.py`, `desktop/controller.py` | side-car plumbing + verdict-equivalence + orphan-sweep tests |
| F-03 ten-stage observation ladder with per-stage evidence | `novaforge/manager.py` (`OBSERVATION_LADDER_STAGES`, `observation_ladder`), `/novaforge/capabilities?title=…` | `tests/test_novaforge_nf23.py` (`ObservationLadderTests`, `LadderEmbeddingTests`, `LadderApiTests`) |
| F-04 eight live states on the observation surface | `web/static/js/novaforge.js` (`observationLiveState`), `index.html` (`#forgeObservationLadder`), `nc-os.css` | node-driven pins + `LadderSurfaceTests` |
| F-06 paused/ended observation refuses a state reading | `novaforge/state_service.py` (`_pause_refusal`) | 5 tests in `tests/test_novaforge_nf23.py` |
| F-07 path launch uses the executable's own directory | `desktop/controller.py` (`_launch_open`) | 2 tests in `tests/test_desktop.py` |
| **F-08** refusal envelope tells the truth about what was read (`capture_ok` = a backend produced pixels; `content` = what the read was; structured code kept when nothing was produced), the ladder's validated row uses only the record it describes, and a no-frame pause maps to the target/identity state it is instead of "invalid frame" | `desktop/capture_backends.py` (`CaptureRouter.capture`/`_refuse`), `novaforge/manager.py` (`observation_ladder`), `web/static/js/novaforge.js` (`observationLiveState`) | 2 tests in `tests/test_capture_backends.py`, 1 in `tests/test_novaforge_observation.py`, 1 in `tests/test_novaforge_nf23.py`, plus `noFrame`/`windowMissing`/`identityUnavailable`/`replaced`/`exitedNoFrame` node cases |

No new capture backend was added: PrintWindow served every real-window case measured (foreground, unfocused,
covered, resized, restored), so Windows Graphics Capture stays an `UNTESTED CANDIDATE` per the phase's rule
against building a subsystem a targeted correction does not require.

### 8.2 Measurements that replaced assumptions

| Assumption at phase start | Measured |
|---|---|
| "Validation decodes the whole PNG every frame (~35 ms)" | 2880×1800: full decode **71.27 ms** median → side car **1.43 ms** median (same verdict, per-channel deltas ≤ 0.17) |
| "The H01 geometry defect class may still apply" | frame dims equal the live window rect on every frame, including after a resize (1920×1360 frame vs bounds 320,320→2240,1680) |
| "A fullscreen game's focus loss pauses observation" | verified on a real window: `paused` / `target_minimized`, **no frame**, counts unchanged |
| "A paused observation cannot leak a state reading" | **false at phase start** — F-06, fixed |
| "Path launching a game executable works" | **false** — F-07 (`rc=1` in 0.02 s, no window), fixed |
| "Stale frames are never shown as current" | 3.65 s-old frame → `livable=false`, "The frame is stale." |
| "The worker is cheap enough for a session" | 20 frames: 37.4 ms median capture, 3.7 ms validation, +7.6 MB working set, 38.2 ms CPU/frame, one worker process |

### 8.3 Verification runs

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge_nf23.py -q` | 45 passed (after the verification pass) |
| `python -m pytest tests/test_desktop.py -q` | 81 passed, 20 subtests passed |
| `python -m pytest tests/test_capture_backends.py tests/test_novaforge_observation.py tests/test_novaforge_state.py tests/test_novaforge_launcher.py tests/test_novaforge_api.py tests/test_novaforge_nf23.py -q` | 302 passed, 3 subtests passed |
| `python -m mypy src` | Success: no issues found in 451 source files |
| `python -m mypy src --platform win32` | Success: no issues found in 451 source files |
| `python scripts/generate_api_reference.py --check` | `docs\API.md: in sync` |
| `python -m pytest tests/ -q` | **5733 passed, 1 skipped, 3847 subtests passed** in 1828.25 s (30:28), exit 0 (`.nc23_tmp/full_suite_final.log`) |

### 8.4 Independent verification pass (2026-10-27, after the first close-out)

The phase was then re-verified against its own objectives rather than its own summary: the refusal envelope was
read against the panel's mapper, the ladder's evidence against its records, the panel mapping against the
observed states, the API against the route consumers, and every phase/dependency suite re-run. Findings and
outcomes:

| Check | Result |
|---|---|
| Phase group (`nf23` + `capture_backends` + `observation` + `state` + `ui`) | **381 passed, 94 subtests passed** (25.7 s) |
| Dependency group (`novaforge`, `_api`, `_gameplay_api`, `_launcher`, `desktop`, route-shape, API-sync) | **282 passed, 495 subtests passed** (2 min 08 s) |
| `mypy src` and `mypy src --platform win32` | Success, 451 files, both views |
| `scripts/generate_api_reference.py --check` | `docs\API.md: in sync` |
| Refusal envelope vs panel mapping | **defect F-08 found** (no-frame refusal claimed a frame; no-frame pause mapped to INVALID FRAME), fixed + regression tests and node cases |
| Ladder evidence vs records | **defect F-08 found** (a later refusal lent its content to the validated row), fixed + 1 regression test |
| Live acceptance re-check (attempt 5, gate 3.63 GB / 76.4 %) | `INSTALLED_BUT_LAUNCH_FAILED`; Steam's log: `Launcher.exe` added then removed 2 s later; no process left behind — blocker unchanged |
| Attached volumes / alternative targets | `C:`, `D:`, `E:` only — the `G:` volume with the non-Steam Hitman 3 install is not attached, so no substitute target exists |

The full suite was **not** re-run after F-08 (it takes ~30 min and touches only `CaptureRouter` and
`observation_ladder`, whose consumers are all in the groups re-run); the earlier 5733-pass run is reported as
pre-F-08 in the report §12.1 rather than implied to cover it.

### 8.5 The one criterion that could not be earned here

The live real-game acceptance is **BLOCKED**, and the blocker is measured rather than inferred: this host's
Steam API initialisation fails for the selected title.

```
$ .\Launcher.exe            # E:\STEAM\steamapps\common\Freedom_Fighters, cwd = the game folder
rc 1 after 0.03 s
output: SteamAPI_Init failed
```

* `Freedom.Exe` exits `rc=1` in 0.02 s and asks Steam to relaunch the app (Steam console log shows the relaunch
  and its 2–3 s death); Steam's own launch of the title dies identically, so this is a host/Steam fact, not a
  NovaForge behaviour.
* The surrounding environment is healthy: Steam logged on today, title fully installed (`StateFlags 4`,
  `buildid 6153020`), `steam_appid.txt` = `1347780`, no crash dump, no WER event, no window ever created.
* The product behaved correctly under the failure: `INSTALLED_BUT_LAUNCH_FAILED`, no verified-window claim,
  `NO_WINDOW` graceful close, no kill, no duplicate instance, no input.
* Retry procedure (once the Steam-side failure is cleared): `.nc23_tmp/live_game_direct_probe.py` — it runs
  the full acceptance sequence on the already-verified observation path (bind identity, ≥10 frames, latency,
  focus-away, restore, stop, graceful close, confirmed exit).
* Re-measured in the verification pass (attempt 5): same failure, same 2-second launcher death in Steam's own
  log, no process left behind, gate open throughout. The blocker is external to NovaForge and unchanged.

No real-game frame is claimed anywhere in this phase's documents, and no simulation or disposable-window result
is presented as one.
