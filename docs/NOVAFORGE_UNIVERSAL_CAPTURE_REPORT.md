# NovaForge — Universal Capture & Live Observation (H03)

**Scope:** replace the single, focus-dependent capture path with interchangeable,
capability-declared backends behind a frame-validation layer, so a captured
frame is only shown as `LIVE` when it is demonstrably a current frame **of the
target**.

**Status:** implemented and verified on this host against the one game that was
already running. Not universal: see [Limitations](#9-limitations-and-next-engineering-task).

Observe. Decide. Act. Verify. Evolve.

---

## 1. Baseline repository state

- Branch `main`, working tree **already dirty** before this task (the H02 work:
  63 entries documented in `docs/NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md`).
  Nothing was reset, cleaned, stashed, reverted or committed. `git status
  --porcelain` now reports **76** entries (29 untracked): the H02 entries, the
  files this phase changed, its new files (§5) and this report.
- No game process was launched. `Freedom Fighters` (PID 28600) was already
  running when the phase started; it was the only game used.
- **No gameplay input was sent at any point.** No key, button, cursor move or
  action proposal was made by any probe. The only window operations performed
  were focusing a window (`SetForegroundWindow` via `.nc_win_ctl.ps1`) so the
  foreground/covered cases could be measured, minimizing and restoring a
  *disposable* probe window (never the game), and restoring the game window that
  the probe found minimized.
- Phase reports H01, H02 and H02.1 were not modified.

## 2. Architecture and capture contract

Three thin pieces, no framework:

```
PersistentScreenCapture (existing PowerShell worker, 1 process, N frames)
        │  capture(path)          → full-display PNG
        │  capture_window(h,path) → the window's own surface PNG
        ▼
CaptureRouter ── declares, selects, validates ──► CaptureOutcome
   ├── WindowSurfaceBackend(name="printwindow", source="window_surface")
   └── DisplayCropBackend   (name="gdi_display", source="window_crop" | "screen")
        ▼
validate_frame(...) → FrameVerdict{capture_ok, target_visible, fresh, content_ok, livable}
        ▼
NovaForgeManager session loop → FrameRecord{backend, source, content, validation}
        ▼
/describe payload → live panel (Backend, Source, Validation rows; LIVE gate)
```

`src/novacontrol/desktop/capture_backends.py`

- `CaptureBackend` (Protocol): `name`, `capabilities()`, `available()`,
  `async capture_frame(path, target) -> FrameCapture`.
- `BackendCapabilities`: `backend, captures, unfocused, occluded, minimized,
  requires_foreground, notes`. Declared data, printable and testable — never a
  guess made at the call site.
- `FrameCapture`: one backend's raw answer (`ok, backend, source, path, width,
  height, size_bytes, elapsed_ms, error`). A backend reports failure; it never
  raises out of `capture_frame`.
- `CaptureTarget`: the facts a backend may use — `handle, title, bounds,
  minimized, visible, foreground`. **Identity is not visibility**: a valid
  handle+PID says nothing about whether the pixels contain the window.
- `CaptureOutcome`: the router's answer — frame metadata, the `verdict`,
  the pixel `inspection`, and every `attempt` (which backend was tried, and why
  it was rejected). A refusal names the condition instead of falling back.
- Cleanup: no backend owns a process. The single worker is owned by the desktop
  layer and closed with `aclose()`; the router holds no OS resource, so start /
  pause / resume / stop cycles leak nothing (§7 tests).

`src/novacontrol/desktop/capture_validation.py`

- `inspect_frame(path, previous_hash)` — measurable facts only: dimensions,
  mean luma, non-black ratio, single-colour ratio, 48×27 content hash, `blank`,
  `repeated`. Unreadable → `readable=False` with a reason, never "valid".
- `validate_frame(...)` — the four facts stay separate, and `livable` is their
  conjunction:

  | condition | `capture_ok` | `target_visible` | `fresh` | `content_ok` | `livable` | `content` |
  |---|---|---|---|---|---|---|
  | window surface, covered target, painted | ✔ | ✔ | ✔ | ✔ | **✔** | `valid` |
  | window surface, blank (renderer refused) | ✔ | ✔ | ✘ | ✘ | ✘ | `blank` |
  | desktop crop, target foreground | ✔ | ✔ | ✔ | ✔ | **✔** | `valid` |
  | desktop crop, target background | ✔ | ✔ | ✘ | ✘ | ✘ | `not_target` |
  | full-screen fallback (`source="screen"`) | ✔ | ✔ | ✘ | ✘ | ✘ | `not_target` |
  | target minimized / hidden | ✔ | ✘ | ✘ | ✘ | ✘ | `not_target` |
  | capture failed | ✘ | (unknown) | ✘ | ✘ | ✘ | `unverified` |
  | identical to the previous frame | ✔ | ✔ | ✔ | ✔ | ✔ + limitation | `repeated` |
  | older than `max_age_seconds` | ✔ | ✔ | ✘ | ✔ | ✘ | `valid` |

- `reason_code(text)` — stable codes (`target_minimized`, `target_hidden`,
  `target_offscreen`, `target_not_foreground`, `target_unreadable`,
  `stale_frame`, `capture_rejected`) so a pause reason and the UI never depend on
  prose. A note may be reworded; a code is a contract value.

## 3. Backends investigated

| Candidate | Captures | Unfocused | Occluded | Minimized | Verdict |
|---|---|---|---|---|---|
| **PrintWindow `PW_RENDERFULLCONTENT`** (implemented) | the window's own surface | **yes, verified** | **yes, verified** | no — refused | shipped as `printwindow` |
| **GDI `CopyFromScreen` + crop** (existing, now declared honestly) | composited desktop | no | no | no | kept as `gdi_display`, `requires_foreground=True` |
| **Windows Graphics Capture** | window or display | claimed yes | claimed yes | claimed for some apps | **not implemented, not tested** |
| Desktop Duplication API (DXGI) | whole display | n/a | n/a | n/a | not pursued: it is a display capture, so it cannot answer "which window?" |

Why WGC was not adopted in this stage: it needs WinRT + D3D11 interop. This
machine's capture path is a PowerShell worker with GDI/`System.Drawing`, so WGC
would mean a new native/C# helper or a new Python dependency
(`windows-capture`, which pulls numpy/opencv) — a dependency and a build change
the phase explicitly forbids deciding on assumption. Its documented benefit
(device-side frame pool, works for some minimized apps) remains a **candidate,
untested here**. The printed matrix in §8 therefore does not claim WGC.

PrintWindow is honest about its own weakness: it asks the window to paint into a
bitmap, and a DirectX renderer that refuses leaves a black bitmap — which is why
**every frame is blank-checked** (`_MIN_PAINTED`, `_MAX_UNIFORM`) before use.

The worker already had the command (`printwindow <hwnd> <path>`), including the
honest failures `target_minimized`, `target_offscreen`, `target_unreadable`,
`printwindow_refused`. This phase added the seam that exposes it
(`LocalDesktopRunner.capture_window`) and wired it into the application.

## 4. The H02 failure and its correction

**H02:** after a fullscreen game lost focus it was minimized to placeholder
bounds (`-32000,-32000`). The capture fell back to a full-screen frame, which was
recorded and labelled `LIVE` under the game's name — the NovaControl UI was
being presented as gameplay.

Corrections, in the order they execute:

1. `validate_frame` refuses `source="screen"` outright (`content="not_target"`,
   `livable=False`) — a full-desktop frame can never be evidence of the target.
2. `DisplayCropBackend` refuses a minimized/hidden target and never returns a
   crop of the desktop *as* a window frame.
3. `CaptureRouter` refuses **before** touching any backend when the target is
   minimized, hidden or has no usable area, and returns the stable code.
4. `NovaForgeManager._acquire_frame` propagates `fresh=False` + `reason_code`;
   the session loop now **pauses** on a target-condition refusal
   (`_TARGET_PAUSE_REASONS`) instead of polling forever with no frame and no
   explanation. A transient capture failure (a worker hiccup) is still recorded
   and retried — pausing on it would end a run for a hiccup.
5. The frame is only recorded when the verdict is `livable`; otherwise the PNG
   bytes stay as rejected evidence, an observation with `evidence=False` is
   recorded, and `target.minimized` / `target.offscreen` / `target.background` /
   `target.unreadable` is written to the timeline.
6. The live panel gates `LIVE` on the same verdict (`frameLiveState`): a frame
   whose `validation.livable === false`, or whose `source === "screen"`, renders
   **`TARGET NOT VISIBLE — FEED NOT LIVE`** with the server's reason and, when a
   previous frame exists, its sequence and capture time labelled as historical.
7. Focus loss is *not* treated as "stop capturing": an unfocused, still-visible
   target is captured through the window surface (verified in §8).

## 5. Files changed

New:

| File | Purpose |
|---|---|
| `src/novacontrol/desktop/capture_backends.py` | backend protocol, two backends, capability-declared `CaptureRouter`, cropping |
| `src/novacontrol/desktop/capture_validation.py` | `inspect_frame`, `validate_frame`, `reason_code` |
| `tests/test_capture_backends.py` | 52 deterministic tests: validation table, inspection, backend contract suite ×2, router selection/refusal, manager integration, persistence |

Modified:

| File | Change |
|---|---|
| `src/novacontrol/novaforge/manager.py` | `capture_window` / `capture_backends` seams, router construction, `_acquire_frame` through the router, target-condition pause, `_capture_capabilities` in `capabilities()["capture"]["backends"]`, `reason_code` in `_rejection_reason` |
| `src/novacontrol/novaforge/models.py` | `FrameRecord.backend` / `.content` / `.validation` persisted and restored |
| `src/novacontrol/desktop/controller.py` | `LocalDesktopRunner.capture_window(handle, path)` — the window-surface seam |
| `src/novacontrol/application.py` | wires `capture_window` from the desktop runner, so the production app builds the router |
| `src/novacontrol/web/static/js/novaforge.js` | `LIVE` gated on validation; `backendLabel`, `validationLabel`, `window_surface` source label; `LIVE` gate exported for tests |
| `src/novacontrol/web/static/index.html` | `forgeFrameBackend` and `forgeFrameValidation` rows in the frame metadata |
| `tests/test_novaforge_ui.py` | validation-gated live states and the new labels driven in Node |

Scratch evidence (untracked, not part of the product): `.nc_cap_evidence/`
(`probe_router.py`, `probe_occlusion.py`, `probe_app_session.py`,
`probe_perf.py`, `occlusion_target.ps1`, `occlusion_ctl.ps1`, PNG/JSON output).

## 6. Test commands and exact results

All run from the repository root on Windows, Python 3.13.

| Command | Result |
|---|---|
| `PYTHONPATH=src python -m pytest tests/test_capture_backends.py -q` | **52 passed** (6.8 s) |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_ui.py -q` | **125 passed, 75 subtests** |
| `PYTHONPATH=src python -m pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py -q` | **176 passed, 17 subtests** (51 s) |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_evaluation.py tests/test_novaforge_evaluation_api.py tests/test_novaforge_benchmark.py tests/test_novaforge_benchmark_api.py tests/test_novaforge_coach.py -q` | **200 passed, 165 subtests** (5 min 14 s) |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_closed_loop.py tests/test_novaforge_policy.py tests/test_novaforge_vision.py tests/test_novaforge_experiment_api.py -q` | **141 passed, 9 subtests** (47 s) |
| `PYTHONPATH=src python -m pytest tests/test_application.py tests/test_desktop.py tests/test_desktop_input.py -q` | **161 passed, 61 subtests** (2 min 59 s) |
| `PYTHONPATH=src python -m pytest tests/test_web_api.py tests/test_web_platform.py tests/test_route_shape_contract.py -q` | **138 passed, 528 subtests** (2 min 6 s) |
| `PYTHONPATH=src python -m mypy src` | **Success: no issues found in 435 source files** |
| `PYTHONPATH=src python -m mypy src --platform win32` | **Success: no issues found in 435 source files** |

The full suite (~2,254 tests) was **not** run as one command; every suite that
touches the capture path, the manager, the panel, the API surface, the
application wiring and the desktop controller was run, plus both mypy views.
Untested: the suites unrelated to this change.

Deterministic coverage in `tests/test_capture_backends.py`:

- `FrameValidationTests` (10) — the §2 table, including "a fresh frame can still
  be the wrong content" and "a failed capture says nothing about visibility".
- `FrameInspectionTests` (7) — painted/black/white frames, repeat detection,
  unreadable and missing files, stable reason codes.
- `DisplayCropBackendContract` / `WindowSurfaceBackendContract` — one shared
  suite run against **both** backends: declared capabilities are internally
  consistent, availability reflects the wiring, one frame carries correct
  metadata, a raising seam is reported (not raised), a minimized target is
  refused.
- `CropTests` (3), `RouterTests` (10) — selection by capability, ground-truth
  falls through only when eligible, blank surface refused for a background
  target, off-screen refused, minimized/hidden capture nothing **and call no
  backend**, repeated content reported as a limitation, no backends is an
  explicit refusal, unavailable backends are skipped.
- `ManagerIntegrationTests` (6) — a background target records
  `backend=printwindow / source=window_surface / livable=true`; an off-screen
  target pauses `target_offscreen` with **no** frame; a blank surface pauses
  `target_unreadable` with no frame; capability rows differ with and without the
  window seam; two start/stop cycles leak nothing.
- `FrameRecordPersistenceTests` — backend/verdict survive a reload.

## 7. Real-host verification

Artifacts: `.nc_cap_evidence/universal/*.json` and the PNGs beside them.
Nothing below was inferred: each row says what was run.

### 7.1 Production wiring (the real application)

`.nc_cap_evidence/probe_app_session.py` builds `NovaControlApplication`, which
constructs `LocalDesktopRunner` and hands NovaForge its seams — i.e. the exact
production path, not a test double.

- `router_wired: true`; capabilities list `printwindow` (unfocused/occluded)
  and `gdi_display` (requires foreground).
- Session on `Freedom Fighters` (2880×1800, DPI scale 2.0), run **twice**:
  **8 consecutive frames** (`capture_ms` 123–208) and **6 consecutive frames**
  (`capture_ms` 117–176) after the final edits. Every frame in both runs was
  `backend=printwindow`, `source=window_surface`, `content=valid`,
  `livable=true`, `stale=false`, frame age 1–20 ms, state `RUNNING`,
  **0 errors**.
- Same session, with another window focused 3 s in: 3 validated frames, then the
  game minimized itself (fullscreen exclusive) and the session went **`PAUSED`
  / `pause_reason=target_minimized`** with the message *"capture is suspended
  rather than showing the desktop"* — **no further frame was recorded**.

### 7.2 Occlusion and minimize, on a disposable window

The installed game cannot test occlusion: on this host it is fullscreen
exclusive and **minimizes** when focus leaves it. A disposable animated WinForms
window (`occlusion_target.ps1`, 1240×840, never a game, never activates itself)
was therefore used to measure the covered case honestly.

| Phase | Backend | Frames | Result |
|---|---|---|---|
| foreground | printwindow | 5/5 | `valid`, unique hashes, 59–429 ms (first includes warm-up) |
| **covered** (Freebuff focused, target `foreground=false`, `minimized=false`) | printwindow | **5/5** | `valid`, **unique hashes on every frame**, 73–87 ms |
| same window, desktop crop | gdi_display | 1 | crop produced (`ok=true`, `source=window_crop`) and **validation refused it**: `livable=false`, `content=not_target`, *"The target is not foreground, so a desktop crop of its rectangle may contain another window."* |
| minimized | (router) | 0 | refused **before any backend ran**: `code=target_minimized`, no file written |
| restored | printwindow | 2/2 | `valid` — recovery works, nothing is wedged |

Worker cost for the whole run: **1 spawn, 13 frames**.

### 7.3 The game's own case, as found

At phase start the running game was minimized. The router refused five
consecutive captures with `target_minimized` and wrote **no frame** — the exact
behaviour H02 lacked.

## 8. Compatibility matrix

Legend: **PASS** = measured on this host; **FAIL** = measured and refused;
**N/T** = not tested; **UNSUPPORTED** = the wired backends declare it.

| Target | Rendering | Mode | Foreground | Unfocused (covered) | Minimized |
|---|---|---|---|---|---|
| Freedom Fighters (2003, D3D8-era) | Direct3D 8 | fullscreen 2880×1800 | **PASS** (8/8 valid, app path) | **UNSUPPORTED on this host** — the game minimizes on focus loss (measured), so it becomes the row below | **FAIL → honest pause** (`target_minimized`, 5/5 refusals, no frame) |
| Disposable WinForms window (GDI) | GDI+ | windowed 1240×840 | **PASS** (5/5) | **PASS** (5/5 valid, unique hashes) | **FAIL → honest refusal** (`target_minimized`, no file) |
| Any other installed game | — | — | **N/T** | **N/T** | **N/T** |
| Modern D3D11/12/Vulkan title | — | — | **N/T** | **N/T** | **N/T** |

Why the other rows are `N/T`: a game was already running, and launching a second
one is forbidden by this phase's constraints. No claim is made about renderers
this host never exercised — PrintWindow answering black for a particular
DirectX title is precisely the failure the blank-check exists to catch, and it
has not been measured here for any title other than the two above.

## 9. Performance measurements

`.nc_cap_evidence/probe_perf.py`, 6 runs per backend, `Freedom Fighters`
foreground at 2880×1800 (5.2 MP), warm worker, two independent runs (a
colourful frame and a mostly-black frame), plus the in-session measurements
from §7.1.

| Measurement | Median (run A / run B) | Range |
|---|---|---|
| PrintWindow surface frame (incl. PNG encode/write) | **135.4 / 123.1 ms** | 112–230 ms |
| Display frame (2880×1800 PNG) | **132.9 / 115.5 ms** | 107–172 ms |
| `inspect_frame` on a 2880×1800 PNG, warm | **34.7 / 47.6 ms** | 39–55 ms |
| `inspect_frame` on a 2880×1800 PNG, first read after write | **333–396 ms** (disk-cache miss) | — |
| `inspect_frame` on a 1240×840 PNG | **8.4 ms** | — |

Interpretation, measured rather than assumed: at 5.2 MP the two backends cost
about the same because **PNG encoding dominates**, not the capture API.
Validation adds ~25–40 % (warm) of a frame at that size, ~2–3× that on a cold
file read, and is negligible at 1 MP. The session's default interval is 1 s; the achieved capture
loop in the app probe was ~1 frame/s within a 1 s interval with 123–208 ms of
capture time, leaving the interval as the limit. **No frame-rate target is
claimed**: a configurable target of ≥1 validated frame/s at 1440p is what was
observed, and a higher rate would need the PNG encode moved off the critical path
(see the next task).

The first measurement of `inspect_frame` at 5.2 MP was **74 ms**; a box
`reduce()` before the thumbnail resize (instead of a full-size bicubic resize)
brought the warm cost to ~35 ms. Both numbers are real: the 54.9 ms figure is
the in-loop measurement including a cold file read.

## 10. Limitations and next engineering task

1. **No minimized capture.** No wired backend can render a minimized window, so
   a minimized target is a refusal plus an actionable pause, never a frame. WGC
   may support some apps; it is not implemented (§3).
2. **PrintWindow is renderer-dependent.** Two targets answered correctly; a
   D3D/Vulkan title that refuses will produce a blank bitmap and be refused.
   Until that is measured per title, the matrix stays `N/T`, not "supported".
3. **Fullscreen exclusive games minimize on focus loss** on this host (measured
   both on the game and in the earlier H02.1 work). Unfocused capture of such a
   title is therefore not a capture problem that can be solved here; it needs
   borderless/windowed mode, which this phase must not force on a player.
4. **The blank check has a false-negative mode, observed for real.** During the
   second performance run the game painted a **pure black frame** (`mean_luma
   0.0`, `non_black_ratio 0.0`, `uniform_ratio 1.0`): `inspect_frame` called it
   blank, so that frame would be refused and the session pauses as
   `target_unreadable`. That is the designed trade — a black bitmap is exactly
   how a renderer that refuses PrintWindow answers, and the two are
   indistinguishable — but it also means a legitimate fade-to-black or a
   black loading screen pauses the session until paint returns (resume
   re-reads the window and is cheap). A future backend that can prove the frame
   came from the window (WGC's frame pool can, at the API level) would let this
   case be accepted rather than refused.
5. **Repeated content is a limitation, not a failure.** A static game screen is
   indistinguishable from a frozen capture by content alone; the panel and the
   record say so (`content="repeated"`) instead of claiming either.
6. **The cursor is not in a PrintWindow frame.** The hardware cursor is
   composited by DWM, not painted by the window. Frames are the window's own
   rendering.
7. **Validation decodes the whole PNG** (~35 ms at 5.2 MP). Moving the paint
   statistics into the worker (where the bitmap is already in memory) would
   remove it from the Python path.
8. **One game, one renderer, one OS build.** The matrix is what was measured,
   not a compatibility promise.

**Next engineering task:** remove the PNG encode from the per-frame critical
path — have the worker hand back raw BGRA plus the paint statistics over the
existing stdin/stdout protocol (or a memory-mapped buffer), then re-measure the
achievable frame rate at 1440p and 4K; and, if a modern D3D11/12 title becomes
available, evaluate Windows Graphics Capture as a third declared backend rather
than assuming PrintWindow generalises to it.
