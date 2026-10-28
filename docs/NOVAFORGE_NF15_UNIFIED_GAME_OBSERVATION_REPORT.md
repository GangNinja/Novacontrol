# NovaForge NF-15 — Unified Game Observation Report

**Date:** 2026-10-25
**Phase:** NF-15 — unified game observation
**Verdict:** **PASS WITH LIMITATIONS** (see §15)

NF-15 adds ONE observation pipeline: it binds the real game window, captures through the existing
capability router, validates every frame with the existing verdict, and publishes a structured record
that says what was measured, what was refused and why nothing could be read. It does not interpret the
game, plan an action or send input.

### Verification pass (2026-10-25, after implementation)

NF-15 was re-audited against every scope after implementation. Seven defects were found and fixed; each
has a pinning test that was mutation-checked (the test fails with the old code and passes with the fix):

| # | Defect | Impact | Fix | Pin |
|---|---|---|---|---|
| 1 | `_target_missing` was annotated `-> GameFrameObservation` while `_pause_record` returns `GameFrameObservation \| None` | `mypy src` failed (1 error), so the earlier "type checks clean" claim was no longer true | Correct union annotation | `python -m mypy src` (both views — a type error has no runtime test) |
| 2 | `CaptureOutcome` dropped the backend's note on an ACCEPTED frame, and `manager.capture_observation` looked for the word "clipped" in the (empty) verdict reason | A window extending past the display produced a partial frame that was reported as the whole target, and NF-09's "never describe a clipped frame" refusal never fired through the router path | `CaptureOutcome.note` carries `FrameCapture.error`; the manager reads it for `clipped`/`note` | `test_a_frame_clipped_by_the_display_says_so_in_its_note`, `test_a_clipped_frame_is_reported_as_clipped_by_the_manager` |
| 3 | A rejected/unavailable record was built BEFORE the failure counter was incremented | The record describing a failure said `consecutive_failures: 0`, and the row that tripped the ceiling lagged the status by one | The record carries the count INCLUDING its own read | `test_each_failure_record_reports_the_count_including_itself` |
| 4 | The loop task had no generation token: a terminal failure finishes while publishes are pending, so a `start` in that window left the OLD loop running and its `finally` cleared the NEW task | Two capture loops on one target, an old loop writing into the new session, and a `stop` that could not cancel the live loop | `_loop_token` / `_spawn_loop`; a loop only runs, and only clears the task, for its own generation | `test_a_restart_after_a_terminal_failure_leaves_one_capture_loop` |
| 5 | The observation card showed process identity but not window state (Scope F's "target process **and window status**") | Window state was visible only as a pause sentence, never as a fact while observing | `observationWindowText` + a `Window` fact row (minimized/hidden/visible · foreground/background · target valid, or the pause code) | `test_the_window_state_is_its_own_fact_not_a_second_identity` (Node, real `novaforge.js`) |
| 6 | `LocalDesktopRunner.process_identity` returned `None` on a non-Windows host, while its own docstring promised that a probe which cannot run raises (the observer reads `None` as "the process exited") | On any host without the Win32 probe a live process would have been reported as EXITED (`process_exited`, terminal) instead of "cannot tell" (pause/unknown). Latent today — no observation can bind on such a host — but it is the exact confusion Scope C forbids | A non-Windows host raises `RuntimeError`, which `_read_identity` records as `unknown`; an unusable pid still returns `None` | `test_a_host_without_the_probe_raises_instead_of_reporting_gone`, `test_an_unusable_pid_is_still_none_not_an_error` |
| 7 | `test_two_observers_are_isolated` asserted `len(history) == 1` per observer — a timing assumption, so it passed in a batch and failed when the file ran alone | A flaky suite: the isolation claim was untested whenever the second loop completed two reads before both stopped | The assertion now pins OWNERSHIP: every row carries that observer's own session id and game, and each session numbers its rows from 1 | `test_two_observers_are_isolated` (five consecutive solo runs of the file) |

No validation threshold was weakened, no test was relaxed, and no sub-assertion was removed to make a
check pass. §10–§12 below are the results of THIS pass; §12.3 is the live evidence recorded after the
fixes.

---

## 1. Initial repository state

Inspected before changing anything:

| Document | What it established |
|---|---|
| `docs/NOVAFORGE_NF13A_EMBEDDED_GAME_HOST_REPORT.md` | A real game window can be hosted in a Qt surface only when it is windowed; Shadow Fight Arena's borderless fullscreen was refused. Capture of a HOSTED window is unavailable on this host. |
| `docs/NOVAFORGE_NF14_GAME_EMBEDDING_COMPATIBILITY_REPORT.md` | The compatibility/recovery layer after NF-13A. There is no separate NF-13B report; NF-14 is the compatibility phase that followed NF-13A, and its findings are reused here. SFA's `PrintWindow` capture was recorded as **blank** on that pass. |
| `docs/NOVAFORGE_NF12_GAME_WORKFLOW_INTEGRATION_REPORT.md` | The launch workflow: plan → token → execute, a verified `LAUNCH_VERIFIED` with pid and **process start time**, and a graceful close that confirms exit. |
| `docs/NOVAFORGE_UNIVERSAL_CAPTURE_REPORT.md` | The capture backends: `WindowSurfaceBackend` (`PrintWindow`, can see a covered window, may answer blank) and `DisplayCropBackend` (display copy, only valid while foreground), selected by declared capability and validated per frame. |
| `docs/NOVAFORGE_H02_1_FOCUS_LOSS_REPORT.md` | The H02.1 defect and fix: a minimized or unfocused fullscreen game must never be labelled LIVE, and a desktop crop of a covered window is not a game frame. |

The capture layer was already split correctly:

* `src/novacontrol/desktop/capture_backends.py` — `CaptureRouter` chooses a backend by DECLARED
  capability plus live state, then validates the pixels; a target no backend can honestly see is
  refused with a stable reason code.
* `src/novacontrol/desktop/capture_validation.py` — `inspect_frame` / `validate_frame` produce
  `capture_ok`, `target_visible`, `fresh`, `content_ok` and the single `livable` conjunction.

Also inspected: `novaforge/manager.py` (NF-01 session loop and NF-08 `_capture_observation_frame`),
`novaforge/observation.py` (NF-08 observation model), `novaforge/launcher.py` + `games.py` (catalog and
launch), `gui/game_host.py` and `gui/app.py` (the embedded host), `application.py` (wiring and events),
`api/app.py` / `api/models.py` / `api/route_consumers.py` (route contract), the Forge panel in
`index.html` / `js/novaforge.js` / `nc-os.css`, and the existing suites
(`test_novaforge*.py`, `test_capture_backends.py`, `test_desktop.py`, `test_web_api.py`).

### Dependency status

* **NF-13A** — complete for windowed embeds; **SFA is refused as fullscreen** and hosted-window capture
  is unavailable. Neither is needed for NF-15: observation captures the game's OWN window, not a
  hosted one.
* **NF-13B** — no separate report exists in this tree; NF-14 supersedes it. NF-15 therefore depends on
  the **supported observation path** (NF-12 launch → real window → NF-13A/14 capture router) and does
  not fabricate embedding or hosted capture support.

## 2. Gap analysis (before implementation)

| Need | Before NF-15 | NF-15 response |
|---|---|---|
| One structured observation record | NF-08's `GameObservation` exists but carries no capture latency, backend/source, frame sequence, session identity or validation verdict; NF-01 session records hold frames, not observations. | `novaforge/observer.py`: `GameFrameObservation` + `CapturedFrame`, one record per read. |
| A capture path outside a session | `_capture_observation_frame` grew as an ad-hoc mapping adapter for NF-08 and OCR; no typed result. | `manager.capture_observation(window, path) -> CapturedFrame` is the ONE capture; the old adapter is a mapping over it. |
| Target identity | A title match; NF-01 pins handle/pid, but the observation path had no start-time pin and no re-check between reads. | Title + pid + start time + handle are pinned; handle/pid/start-time are re-read every tick. |
| "Not tested" as an answer | `last_observation` shapes were absent; zero/unknown were easy to conflate. | Status distinguishes idle/untested, observed, rejected, unavailable, paused and failed with reasons and `None` for unmeasured figures. |
| Bounded scheduling | No scheduler for observation outside a session; NF-01's loop is session-bound. | One bounded async loop: requested interval clamped `[0.25 s, 5 s]`, one capture in flight, timeouts, bounded history/frames/sessions, measured rate and scheduling delay. |
| Pause/resume semantics | A session pauses; an observation had no lifecycle. | Minimized/hidden/missing target pauses; restore resumes; a USER pause never auto-resumes; stop is idempotent. |
| API + panel | No observation routes and no UI; the Forge panel showed sessions only. | Seven routes in all five registration places; a **Unified Observation** card in Mission Control. |
| Live evidence | NF-14 measured capture compatibility, not an observation stream. | `scripts/novaforge_nf15_observation_probe.py` + two live runs against SFA (§10–§11). |

No duplicate capture, no second validation, no new dependency.

## 3. Files inspected and modified

**New**

* `src/novacontrol/novaforge/observer.py` — the observation model, lifecycle and scheduler (~1 850 lines).
* `scripts/novaforge_nf15_observation_probe.py` — the live probe (launch → observe → minimize/restore →
  stop → graceful close), writing measured JSON.
* `tests/test_novaforge_observation.py` — 49 tests.
* `docs/NOVAFORGE_NF15_UNIFIED_GAME_OBSERVATION_REPORT.md` — this report.

**Modified**

* `src/novacontrol/desktop/capture_backends.py` — `CaptureOutcome.note` keeps the backend's note about
  an ACCEPTED frame (added by the verification pass; §5). No selection or validation rule changed.
* `src/novacontrol/novaforge/manager.py` — builds the observer from its OWN probe/capture seams;
  `capture_observation` (router-backed, typed); `start/pause/resume/stop_observation`,
  `observe_frame_once`, `observation_status`, `observation_frame_path`, `_publish_observation`;
  `_capture_observation_frame` became a mapping adapter over `capture_observation`; shutdown stops the
  observer before the session teardown.
* `src/novacontrol/novaforge/__init__.py` — observer exports.
* `src/novacontrol/desktop/controller.py` — `process_identity(pid)` (pid, name, `StartTime`), the same
  identity a launch record pins.
* `src/novacontrol/application.py` — wires `process_identity` into the manager and the observation
  event relay.
* `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` — the seven routes.
* `src/novacontrol/web/static/index.html`, `nc-os.css`, `js/novaforge.js` — the observation card and its
  pure decisions (exported on `NovaForgeLogic`).
* `tests/test_novaforge_ui.py` — eight Node-executed logic pins for the card (`ObservationCardLogicTests`).
* `tests/test_capture_backends.py` — two router/adapter pins for the clipped-frame note.
* `src/novacontrol/gui/game_host.py` — two `# type: ignore[misc]` markers on the Qt subclasses, the
  same convention `gui/app.py` already uses, so the CI type check passes without PySide6 installed.
* `docs/API.md` — regenerated (`--check` reports in sync).
* `docs/STATUS.md` — phase section.

**Deliberately untouched:** `capture_backends.py`, `capture_validation.py` (validation semantics),
`launcher.py`/`games.py`, embedding, actions, policies and the closed loop.

## 4. Observation architecture

```
launch (NF-12) ──> pid + start_time + window title
        │
        ▼
GameObserver.start(title, pid, start_time, interval)
        │  probe (3 s timeout) → WindowState (handle, pid, bounds, client, visible, minimized, foreground)
        │  pin handle/pid/start_time; identity = pid:<pid>:<start_time>
        ▼
bounded loop (one in flight)
        │
        ├── probe stale?        → target_missing / target_replaced / process_exited / pause
        ├── target invalid?     → pause (minimized/hidden/offscreen), NO capture, NO frame
        ├── capture via router  → printwindow → display crop (foreground only) → refusal code
        ├── validate            → inspect_frame + validate_frame (unchanged)
        ├── accepted            → keep PNG (pruned), record `observed`
        ├── rejected            → DELETE PNG, record `rejected`
        └── no frame / timeout  → record `unavailable`
        ▼
status() → state, target, interval, backend, tested, last_observation, last_success,
           failures, counts, measured rate/latency/delay, pause reason, frames, bounded history
```

Model (`GameFrameObservation`, explicit typed fields): `observation_id`, `session_id`, `seq`, `game`,
`captured_at`, `status`, `target{title,game,handle,pid,process,start_time,identity}`, `width`,
`height`, `size_bytes`, `backend`, `source`, `path`, `capture_ms`, `schedule_delay_ms`,
`frame_age_seconds`, `fresh` (`None` when no frame existed), `target_valid`, `visibility`,
`validation` (the router's verdict dict), `reason_code`, `reason`, `consecutive_failures`,
`limitations`.

Vocabulary is the contract:

* state: `idle | observing | paused | stopped | failed`;
* per-read status: `observed` (only a validated frame), `rejected` (a frame existed and the verdict
  refused it), `unavailable` (no frame existed).

Frame ownership: the observer keeps paths, dimensions and measurements in memory; pixels stay on disk
in `data/novaforge/frames/observed/<session>/`, pruned to the newest 12 per session, 3 sessions kept.
A rejected frame is deleted immediately — it is evidence of nothing.

## 5. Capture backend integration

`manager.capture_observation` is the single capture:

1. converts the observer's `WindowState` to the session `TargetWindow`;
2. calls the SAME `CaptureRouter` a session uses with a `CaptureTarget` (handle, title, bounds,
   minimized, visible, foreground);
3. returns a typed `CapturedFrame` (ok, backend, source, dimensions, size, elapsed, age, reason,
   reason_code, verdict, content hash, clipped, note).

Consequences that are pinned by tests and observed live:

* the target window's OWN surface is asked first (`PrintWindow`), so a covered/unfocused window can
  still be observed when the renderer answers;
* the display crop is attempted only when the target is foreground — a desktop crop is never passed off
  as the game;
* a backend that produces a fresh blank frame is recorded as a failed attempt and the next eligible
  backend is tried;
* no eligible backend yields a refusal code (`target_minimized`, `target_hidden`,
  `target_not_foreground`, `target_unavailable`, `target_unreadable`, `stale_frame`, …), never a frame;
* an ACCEPTED frame keeps the backend's note (`CaptureOutcome.note`), so a crop clipped to the display is
  reported as clipped — and NF-09 refuses to describe it — instead of being presented as the whole
  target (fixed in the verification pass);
* backend capabilities are reported per host through `manager.capabilities()["capture"]["backends"]`.

The historical display-crop path remains only for a host with NO router (no window-surface seam). That
path keeps its old rejection rules; NF-15 does not add a third one.

## 6. Validation and lifecycle behaviour

Validation is reused, not re-implemented. `capture_ok`, `target_visible`, `fresh`, `content_ok` and
`livable` keep their exact meanings; the observer stores the whole verdict on each record and never
derives a verdict from a file's existence.

Lifecycle rules:

| Event | Behaviour |
|---|---|
| launch / bind | probe must return a window; a supplied pid must match the window's pid; a supplied start time must match the process identity (pid reuse is refused with `process_replaced`). A failed bind creates no session and leaves `idle`. |
| minimized / hidden | observation pauses (`target_minimized` / `target_hidden`), no capture, no frame, no success. The loop keeps polling cheaply. |
| restored | the next valid tick resumes capture. Live: 1.59 s after `SW_RESTORE`. |
| user pause | never auto-cleared; poll-only until `resume()`. |
| focus loss | not a pause by itself (the surface backend can see an unfocused window); the display-crop backend becomes ineligible and is refused. |
| window destroyed, process alive | `target_missing` → pause; a recreated window with the SAME pid is re-bound and published as `novaforge.observation.rebound`. |
| process exited | terminal `failed` with `process_exited`, which is different from a destroyed window. |
| pid reused by another process | never adopted: `target_replaced` → pause; the record says so. |
| capture timeout | `unavailable/capture_timeout`, counted; `MAX_CONSECUTIVE_FAILURES` ends a failing loop as `failed` instead of spinning. |
| stop | idempotent (`stopped` then `already_stopped`), cancels the loop, keeps the last validated frame only as a RECORD. |
| restart | the persisted snapshot is restored as `previous`, clearly not current; `status.tested` is false for a new process. |
| shutdown | the application's `stop()` shuts the observer down before the manager/store teardown; a second shutdown is safe. |

Capture state is separate from launch and embedding state: an observation's `status()` has no embed
claim, and the launch/embed records keep their own axes (NF-14). Session isolation: two observers are
separate sessions, directories, sequences and histories (test-pinned).

## 7. Scheduling and resource control

* Requested interval clamped `[0.25 s, 5 s]` (`MIN_INTERVAL`/`MAX_INTERVAL`), with a hard floor of
  0.01 s even if a caller configures a smaller minimum — 100 Hz is the ceiling this build will ever
  consider, and the shipped default is 1 s.
* ONE loop and ONE capture in flight; a concurrent `observe_once` answers `busy` and starts nothing.
* Probe timeout 3 s, capture timeout 6 s, identity timeout 6 s, stop timeout 6 s; cancellation via the
  loop task.
* Bounded: 120 history rows, 12 frames/session, 3 sessions, 20 rows in a status response.
* Paused polling is `max(interval, 0.5 s)` — no busy loop.
* Measured, not promised: `rate_hz` from actual capture-start timestamps, latency last/average/max,
  scheduling delay last/average/max. A rate slower than requested is reported as the achieved rate.

## 8. UI integration

A **Unified Observation** card in Mission Control (not a redesign):

* inputs: target window, interval (0.25–5 s), optional game label;
* controls: Start / Pause / Resume / Stop / Observe Once / Refresh, enabled by state (a user pause shows
  Resume, an automatic pause shows Pause disabled but Observe Once allowed);
* state badge, hint with the pause/none reason;
* facts: game, target (title · pid · start · hwnd), **window state** (visible/minimized/hidden ·
  foreground/background · target valid, or the pause code), last read (status · time), backend, frame
  dimensions, measured rate, capture latency, scheduling delay, consecutive failures, counts, session;
* bounded recent-reads list (newest last), including rejected/unavailable rows with their reasons;
* frame panel: the `<img>` starts hidden with no `src`, is fetched only after the backend reports a
  **validated** frame, and is labelled **“validated now”** only while the observer is `observing` and
  the success is ≤ 5 s old. Otherwise it is labelled a record (“observation is paused — the last
  validated frame is a record”), and a stale/unreadable/untested frame is not shown as live;
* the card copy says observation is not understanding or control, and that a minimized/hidden/gone
  target pauses observation.

The card polls `/novaforge/observation` every 1.5 s, stores no parallel state, revokes object URLs, and
uses the existing `requestJson`/auth path (an `<img src>` cannot carry the token, so the frame is
fetched with headers into a blob URL).

## 9. Logging and replayable diagnostics

Published on the application's ONE event bus (payloads carry no pixels and no free text beyond the
reason already shown in the panel):

* `novaforge.observation.started` — session_id, game, state, title, pid, handle, interval;
* `.paused` / `.resumed` / `.rebound` — same identity fields plus the code/detail;
* `.stopped` / `.failed` — plus the end reason;
* `.frame` — seq, status, backend, width, height, capture_ms, reason_code.

Persistence: one snapshot in the shared `JsonStateStore` namespace `novaforge_observation` (session row
+ the last rows), written throttled and on lifecycle moves; read back on boot as `previous`, never as
current. No frame is written to a log, and no replay subsystem was added. Frame retention is the
observer's own directory and bound.

## 10. Automated tests and exact results

Run from the repository root on Windows, Python 3.13 (`.venv`), with `PYTHONPATH=src`. Phase tests
first, then the suites this phase can affect — not the whole repository.

```
python -m pytest tests/test_novaforge_observation.py -q
49 passed, 37 warnings in 8.94s

python -m pytest tests/test_capture_backends.py -q
55 passed in 8.07s
```

Coverage in that file (all with mocked probes/backends; no game, no desktop):

* model vocabulary, `window_state` (zero handle refused), `target_visibility` agreeing with the session
  manager's `_target_visibility_issue` for minimized/hidden/offscreen/foreground;
* an untested observer reports no observation and NO numbers (`rate_hz`, latency, counts, backend);
* a refused bind is `idle` with no session and **no capture call**;
* bind → identity + measurements + validated frame (backend, width, latency, scheduling delay);
* minimized target pauses and resumes with no frame; hidden target is not captured;
* user pause never resumes itself; stop is idempotent; a concurrent read is `busy`;
* capture timeout is an `unavailable` record and ends a failing loop;
* **verification-pass pins:** each failure record counts the failure it reports; a restart after a
  terminal failure leaves exactly one capture loop (and `stop` still ends it);
* a rejected frame is deleted and never becomes a success; an exited process stops rather than pauses;
  a missing window with a live process only pauses; a recreated window is re-bound, a new process is
  not; frames/history are bounded; the schedule is clamped and the measured rate is reported;
  two observers are isolated; the persisted snapshot restores as `previous`; a broken store never
  breaks observation;
* manager: observation uses the capability router (`printwindow`, `window_surface`, no desktop call),
  the historical adapter still returns a frame, lifecycle + frame path + idempotent stop, a non-numeric
  pid is refused;
* HTTP (real application, TestClient): untested status + 404 frame; start → validated frame route
  (PNG, `no-store`) → pause/resume/stop; missing target is a 200 refusal; a second start is refused;
  the seven routes exist in the declared `ApiSurface`;
* panel contract: every control id exists, the frame starts hidden without `src`, and the card states
  observation is not understanding/control.

```
python -m pytest tests/test_novaforge_ui.py -q
133 passed, 75 subtests passed in 1.34s
```

Eight of those are `ObservationCardLogicTests`, executed in Node against the REAL
`js/novaforge.js`: controls by state and user pause; only a running fresh validation is “validated
now”; an unmeasured figure says “not measured yet” instead of zero; pause/failure reasons are shown;
target line names pid, start time and hwnd; and window state is its own fact.

Regression and adjacent suites (this pass):

```
python -m pytest tests/test_novaforge.py tests/test_novaforge_vision.py \
  tests/test_novaforge_closed_loop.py tests/test_route_shape_contract.py -q
194 passed, 90 subtests passed in 61.25s

python -m pytest tests/test_web_api.py tests/test_desktop.py -q
154 passed, 92 subtests passed in 68.69s

python -m pytest tests/test_novaforge_gameplay.py tests/test_novaforge_api.py \
  tests/test_novaforge_embedding.py tests/test_novaforge_launcher.py -q
234 passed, 18 subtests passed in 28.63s

python -m pytest tests/test_novaforge_ui.py tests/test_application.py \
  tests/test_novaforge_library.py tests/test_novaforge_library_ui.py \
  tests/test_novaforge_gameplay_api.py -q
237 passed, 119 subtests passed in 149.40s

python -m pytest tests/test_capture_backends.py tests/test_novaforge_observation.py -q
104 passed in 16.59s
```

The two tests above that had no pin before this pass's last two fixes live in `test_desktop.py`
(`ProcessIdentityProbeTests`, the non-Windows identity probe) and `test_novaforge_observation.py`
(`test_two_observers_are_isolated`, whose ownership assertions replaced a timing-dependent count). The
observation file was run five times on its own (`49 passed` each time) after the flake fix, because the
defect only appeared when the file ran alone.

`node --check src/novacontrol/web/static/js/novaforge.js` — OK.
`python scripts/generate_api_reference.py --check` — `docs/API.md: in sync`.

H02.1/H02/H03 regression is covered by the capture-backend, `test_novaforge.py`, desktop and
`test_web_api.py` suites above (minimized/fullscreen refusal, blank rejection, no desktop fallback).

The full repository suite was not run in one pass; the groups above are the ones this change touches.

## 11. Type checks

```
python -m mypy src                  → Success: no issues found in 440 source files
python -m mypy src --platform win32 → Success: no issues found in 440 source files
```

The verification pass found and fixed ONE regression: `_target_missing` was annotated
`-> GameFrameObservation` while `_pause_record` can return `None`, so `mypy src` reported
`observer.py:1328: Incompatible return value type`. Both views are clean again as shown above. It also
confirmed the earlier `gui/game_host.py` fix: two Qt subclasses whose base is `Any` (PySide6 absent in
CI) carry the same `# type: ignore[misc]` markers `gui/app.py` uses.

## 12. Live verification

**Resource gate, read immediately before each run** (`GameLaunchService.memory_check`, min 2.0 GB free /
max 92 % used, 15.37 GB total):

* run 1: **3.88 GB free, 74.8 % used → pass**;
* run 2: **2.01 GB free, 86.9 % used → pass** (tight; no second game, no duplicate instance);
* verification pass: **4.7 GB free, 69.4 % used → pass** (1440×900 desktop; disposable window, §12.3).

Environment: Windows desktop, per-monitor DPI aware probe, project `.venv` (Python 3.13, PySide6).
Target: **Shadow Fight Arena** (the lightest installed, previously launch-tested title), pid/start time
pinned from the launch record. No input was sent (`input_sent: false`), no process was killed, GTA V was
not launched, the disconnected WD drive was not touched, and each run closed only its own instance
(`CLOSED`, process confirmed absent afterwards).

Artifacts: `data/novaforge/nf15/20261025T114943Z_observation_report.json` and
`.../20261025T115322Z_observation_report.json` (runtime data, git-ignored).

**Read this first.** §12.1 and §12.2 were recorded BEFORE the verification pass, on the game target
(Shadow Fight Arena). The pass changed the capture adapter (a clipped frame now keeps its note), the
failure-record count and the loop generation; it changed no selection or validation rule. §12.3 is the
post-fix live run — a disposable, self-owned window, not a game — and no game was launched during the
verification pass.

### 12.1 Run 2 — the clean measurement (pre-fix game evidence)

| Fact | Measured |
|---|---|
| Launch | `LAUNCH_VERIFIED`, pid **6540**, start `2026-10-25T11:53:44.7995704Z`, window `Shadow Fight Arena` |
| Bind | `started: true`, session `obs-16ca704a1c88`, pinned identity `pid:6540:2026-10-25T11:53:44.7995704Z`, handle `133248` |
| Interval | requested **0.5 s**, applied **0.5 s** (bounds 0.25–5) |
| Frames | **4 / 4** requested observed within the timeout, 0 rejected, 0 unavailable |
| Backends | seq 1 `gdi_display` (display crop, target foreground), seq 2–4 `printwindow` (window surface) |
| Frame | 2880×1800, 6 329 582 B, content hash `a60868af…`, non-blank (`uniform_ratio 0.0231`, `non_black_ratio 1.0`, `mean_luma 49.41`), age at read 0.28 s |
| Rate | **0.838 Hz** achieved at a 0.5 s request — the requested 2 Hz was NOT achieved |
| Latency | last 443 ms, average **433 ms**, max 447 ms |
| Scheduling delay | last 41 ms, average 680 ms, max 2 615 ms |
| Minimize | paused `target_minimized`; **0 frames** captured while minimized (2 reads recorded as `unavailable`); no success |
| Restore | resumed in **1.59 s**, state `observing`, pause cleared, observation continued |
| Stop | `stopped: true`; **0 frames and 0 reads** after the wait; state `stopped` |
| Close | `CLOSED` — “Shadow Fight Arena exited after a graceful close (SFA, pid 6540)”; process probe `None`; observation remained `stopped` |

### 12.2 Run 1 — the refusal path

Pid 4476, 3/3 observed at 0.5 s request, but the first ticks were **rejected blank** (`PrintWindow`
answered a solid fill while the game was not yet foreground, and the display crop was ineligible), so
the observer recorded `rejected` rows with “The frame is essentially blank …”, deleted the files, and
only later accepted a 2880×1800 `printwindow` frame. Measured rate **0.688 Hz**, latency average 235 ms /
max 515 ms. Minimize paused; restore produced one more observed frame but the status read immediately
after showed `paused/target_minimized` again (a fullscreen window that had not finished restoring);
stop then confirmed **0 frames after stop**, and the close was `CLOSED`.

This is the behaviour NF-15 exists for: no frame is accepted because a file exists, no blank frame is
labelled live, and no desktop crop is passed off as the game.

### 12.3 Disposable painted window — the post-fix live run

Run TWICE against a self-owned WinForms window (640×420 client, painted bands animating every 250 ms)
through the REAL `LocalDesktopRunner` seams (geometry, `PrintWindow`, display crop, process identity)
and the real `NovaForgeManager` observer. No game was launched, no input was sent, no user window was
touched; the probe closed only its own window (the window is confirmed absent afterwards).

| Fact | Run A | Run B |
|---|---|---|
| Bind | `started: true`; target title + handle | `started: true`; identity **adopted** as `pid:29504:2026-10-25T13:15:05.2761641Z` |
| Interval | 1.0 s requested, 1.0 s applied (bounds 0.25–5) | same |
| First reads | 2 / 2 `observed`, 0 rejected, 0 unavailable | 2 / 2 `observed` |
| Backend | `printwindow` (`window_surface`) | `printwindow` |
| Frame | 1312×918, capture 25 ms, age 0.015 s | 1312×918, capture 27 ms, age 0.014 s |
| Rate / latency / delay | **0.995 Hz**, avg 52.0 ms (max 79), avg 2.4 ms | **0.996 Hz**, avg 55.5 ms (max 84), avg 2.0 ms |
| Minimize (own window) | paused `target_minimized`, **observed count unchanged (2)** | paused `target_minimized`, observed unchanged (2) |
| Restore | resumed in **0.54 s**, observing, count advanced to 3 | resumed in **0.54 s**, count 3 |
| Stop | `stopped`; counts **unchanged after 2.5 s** | `stopped`; counts unchanged after 2.5 s |

The one `unavailable` row per minimize is the transition itself, recorded once — not one row per poll
(the defect this report previously listed is fixed and pinned by
`test_a_paused_target_does_not_record_a_row_every_poll`).

What this proves: the real window probe, real `PrintWindow` capture, real process identity, the
rejection/acceptance verdict, the interval clamp, the measured rate/latency/delay, minimize→pause (with
no capture), restore→resume, and stop→no further reads. What it does NOT prove: a GAME target on this
pass (§12.1/§12.2 remain the game evidence, recorded before the fixes), window recreation, focus loss,
or any other renderer.

### 12.4 Not live-tested, and marked `NOT_TESTED`

* Focus loss while the game stays visible (would require taking the foreground from a fullscreen game;
  minimization covers the unavailable-target case, the backend rules cover focus).
* Destruction/recreation of a real game window (unit-tested; a live test would require killing the
  game's window, which this phase forbids).
* Live pid reuse and live stale-HWND handling (unit-tested).
* Any second title, any windowed game, any embedded-window capture (NF-13A/14 already report hosted
  capture as unavailable on this host).
* A rate above ~1 Hz: capture alone costs 410–515 ms on this machine at 2880×1800.

## 13. Known renderer and game compatibility limitations

* **One title.** Everything live above is Shadow Fight Arena (Unity, borderless fullscreen, 2880×1800,
  DPI 192). No claim of universal compatibility is made.
* **`PrintWindow` is renderer-dependent.** SFA answered blank on some ticks (run 1) and painted on
  others; a DirectX/anti-cheat-protected renderer may always answer blank. The pipeline refuses such a
  frame instead of guessing, and may then succeed through the display crop only while foreground.
* **Fullscreen targets only work while foreground** for the display crop; the surface backend can see a
  covered window only if the renderer paints for `PrintWindow`.
* **Embedding is not capture** (NF-13A/NF-14): a hosted window's capture is unavailable on this host;
  the observation card reports the capture state on its own and never infers it from embedding.
* **Minimized windows are never captured** by either backend; observation pauses.
* **DPI:** dimensions are physical pixels in the capture worker's space (2880×1800 here at 200 %).
* **Rate:** ~0.84–0.85 Hz at a 0.5 s request on this laptop; the panel says “not measured yet” until
  two capture starts exist, and reports the achieved rate, never the requested one.

## 14. Remaining defects and risks

1. **The GIF-like rate of a 2880×1800 target is low** (capture 410–515 ms on the game runs, ~25–33 ms on
the 1312×918 disposable window). NF-15 reports the achieved rate; a control loop should choose an
interval its capture can actually meet.
2. **Live coverage is one game title and one mode**, plus the disposable-window runs. Windowed games,
   other renderers, focus loss, window recreation and live pid reuse are mocked or `NOT_TESTED`.
3. **Process identity only exists on Windows.** This pass fixed the probe so a non-Windows host raises
   (recorded as `unknown`, which pauses) instead of returning `None` (recorded as `gone`, which stops the
   observation with `process_exited`). No observation can bind on such a host today (`window_geometry` and
   capture are Windows-only), so the path is latent rather than reachable, but the contract is now honest
   before observation is ever ported.
4. **Capture latency is measured by the observer** when a backend reports none (`elapsed_ms` is `None` for
the window-surface seam on some paths), which is the await time, not the backend's own figure.
5. **The memory gate was tight on the game run 2** (2.01 GB free of a 2.0 GB minimum). The probe checks
   the gate before launching and refuses; it does not re-check mid-run.
6. **A rejected frame is deleted**, so a post-mortem of a blank frame relies on the record's inspection
   fields (mean luma, uniform ratio, hash), not on the image. Deliberate retention policy; it limits
   pixel-level debugging.
7. `manager.capture_observation` keeps the legacy display-crop branch for hosts without a router; it is
   exercised only by unit tests, not live.
8. **A pending capture can leave its PNG when the loop is cancelled** (pause/stop mid-capture). It is
   never served (`frame_path()` only returns a validated success) and is pruned by the next successful
   prune pass, so the exposure is one file, not a leak that grows.

## 15. Final verdict

**PASS WITH LIMITATIONS.**

### Scope-by-scope result

| Scope | Result | Evidence |
|---|---|---|
| A — observation model | **PASS** | `GameFrameObservation` + `CapturedFrame` with explicit fields; 49 observation tests; `fresh: None` when no frame existed |
| B — capture integration | **PASS** | ONE `capture_observation` over the existing `CaptureRouter`; capability rows per backend; explicit refusals; clipped note preserved (fixed this pass) |
| C — lifecycle | **PASS** | bind/identity checks, stale handle, minimize/hidden/missing, recreate-rebind, exit vs missing window, user pause vs automatic pause, idempotent stop, one loop per generation (fixed this pass) |
| D — scheduling/resources | **PASS** | bounded interval 0.25–5 s, one capture in flight, timeouts, bounded history/frames/sessions, measured rate/delay, no paused busy loop |
| E — validation/diagnostics | **PASS** | existing verdict reused unchanged; five outcomes kept distinct; failure count now includes the reported read (fixed this pass) |
| F — UI | **PASS** | observation card with state, target identity, **window state** (added this pass), backend, last read/validated, dimensions, rate, latency, delay, failures, counts, pause/failure reason; frame shown only when running + fresh; 8 Node tests |
| G — logging/replay | **PASS** | seven events on the one bus + a throttled store snapshot restored as `previous`; no pixels or reasoning logged; no replay subsystem |
| H — automated tests | **PASS** | 49 + 55 + 8 focused tests, failure injection for exceptions/delays/invalid frames/process termination; relevant suites re-run (§10) |
| I — safe live verification | **PASS WITH LIMITATIONS** | game runs (pre-fix, §12.1–12.2) + post-fix disposable-window runs (§12.3); focus loss, window recreation and a second title remain `NOT_TESTED` |
| J — documentation | **PASS** | this report, updated with the verification pass, exact results and unverified items |

### What is verified

* a unified observation model exists and is validated (49 focused tests + 8 Node UI tests, all passing);
* the existing capture router and validation are integrated without duplication; backend capability and
  refusals are preserved, and a clipped frame now says so;
* target identity (pid + start time + handle) and freshness are enforced; stale, blank, minimized and
  foreign-process situations produce explicit, non-live records;
* lifecycle, cleanup, bounded scheduling and measured reporting are implemented and test-pinned;
  a terminal failure followed by a restart leaves exactly one capture loop;
* the panel reflects the observer's actual state, shows only validated frames, and never labels a
  record “live”;
* both mypy views are clean and `docs/API.md` is in sync;
* live evidence exists for: real window identification, identity adoption from the real process, correct
  target/backend, validated frames with timestamps and dimensions, rejection of blank/unavailable reads,
  minimize → pause (no frames), restore → resume, stop → no further capture, and measured latency/rate.

### Unverified requirements (recorded, not implied)

* Live focus loss with a visible target (the backend rules are unit-tested; taking the foreground from a
  fullscreen game was not performed);
* live window destruction/recreation and live pid reuse (unit-tested only);
* any game other than Shadow Fight Arena, any windowed game, and embedded-window capture (NF-13A/14
  report hosted capture as unavailable on this host);
* capture above ~1 Hz on the game target (the machine's capture cost, not a code limit — the disposable
  window measured ~1 Hz at a 1 s request with 25–33 ms capture);
* a non-Windows host (§14.3).

The limitations above are recorded rather than papered over. NF-15 delivers reliable game observations;
game-state understanding and autonomous gameplay remain subsequent milestones and were not started.
