# NovaForge H02.1 — focus loss, minimized targets and honest capture state

**Date:** 2026-10-22 / 2026-10-23 (UTC in evidence; local timestamps are UTC+05:30)
**Game:** Freedom Fighters (Steam app `1347780`; same game as H02, restarted)
**Target:** PID `28600`, HWND `1509554`
**Related:** [H02 report](NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md), [H01 report](NOVAFORGE_H01_HITMAN_REPORT.md)
**Test:** observation only — no gameplay input, no autonomous loop, no display-mode change

## Verdict

| Area | Verdict |
| --- | --- |
| Phase A — cause of the minimization | **PASS** — the game minimizes itself when it loses the foreground; NovaForge does not cause it |
| NovaForge window manipulation (`SetForegroundWindow`, `ShowWindow`, `SetWindowPos`, …) | **NONE** — no such call exists in `src/novacontrol` |
| NovaForge treating a minimized target as a valid target | **FIXED** — identity and visibility are separate; the tick pauses instead of capturing the desktop |
| Accurate target / minimized / frame-freshness reporting | **FIXED** — record, Guardian, panel and timeline all report `target_minimized` (typed `target.minimized` event) and hide the live frame |
| Resume gating | **FIXED** — resume is refused (HTTP 422 + panel notice) while minimized and re-validates the window after restore |
| Three focus-switch cycles | **PASS** — 3 complete cycles (pause → restore → resume) plus 3 extra pause-only observations |
| Background capture of an unfocused but non-minimized window | **NOT SUPPORTED** by this build — GDI primary-display capture; not attempted or claimed |
| Zero game input | **PASS** — `actions_attempted = 0`, `actions_executed = 0`, `presses = 0`, `held_after = 0` in every session |
| Focus switching triggers unintended window manipulation | **PASS** — the only window calls in the capture worker are reads |

**Status:** the NovaForge defect behind H02 — a minimized fullscreen game was reported as a
valid target and its desktop fallback was recorded and shown as live — is **fixed and
verified live**. The minimization itself is the game's own fullscreen behaviour; it is not
a NovaForge bug and was not suppressed (no display-mode or window-style change was made).
Watching a fullscreen game inside NovaForge on a single display remains **blocked by the
capture backend**, not by this defect.

---

## 1. Repository state and scope

| Fact | Value |
| --- | --- |
| Branch / HEAD | `main` / `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`) |
| Working tree | the pre-existing H01/H02/NF work is intact: 46 tracked modifications and the untracked NovaForge modules, scripts, tests and reports |
| Operations not performed | no reset, clean, stash, revert, checkout, commit or push |
| Existing reports | H01 and H02 reports preserved; this file is new |
| Code scope | minimized/visible target handling only; no new capture architecture, dependency or display-mode change |
| Runtime data | `data/novaforge/` (ignored) holds the frames and session records cited below |

This investigation implements Task A from the H02 report ("narrow safety/UI correction")
and verifies it against the live game. Task B (target-surface capture, a second display, or
a windowed/borderless setup) remains a separate, deliberately unmade decision.

---

## 2. Environment and target window

| Field | Value |
| --- | --- |
| OS | Windows 11 |
| Host | `Narasimha-Laptop`, Intel Core Ultra 5 125H, Intel(R) Arc(TM) Graphics |
| Display | primary display only, `2880x1800` physical at scale `2.0` (`1440x900` logical), 120 Hz |
| Executable | `E:\STEAM\steamapps\common\Freedom_Fighters\Freedom.exe` |
| Process | `Freedom` (PID `28600`), started `2026-10-23 00:10:51` local |
| Window | handle `1509554`, title `Freedom Fighters`, class `ZSystemClass000` |
| Window style | `0xB6000000` = `WS_POPUP \| WS_VISIBLE \| WS_CLIPSIBLINGS \| WS_CLIPCHILDREN`; extended style `0x00000000`; `IsZoomed = false` |
| Foreground geometry | `2880x1800` at `(0, 0)`, client `2880x1800`, `foreground = true`, `IsIconic = false` |
| Minimized geometry | `IsIconic = true`, `foreground = false`; window rectangle reported as `-16000,-16000,157x25` by a DPI-unaware probe and `-32000,-32000,314x50` by NovaForge's DPI-aware worker — both are the Win32 minimized placeholder, and the magnitude differs only because of DPI virtualisation |

The style is a borderless popup exactly the size of the display: a fullscreen presentation
window with no caption or frame. That is the window shape H02 recorded, and it is why the
window has no usable client area while iconic.

---

## 3. Phase A — established cause

### 3.1 Negative control: the game minimizes with no NovaForge session

With `/novaforge/status` reporting `active_session = null` (no observation, no capture, no
session loop in the process):

| Step | `IsIconic` | `foreground` | Result |
| --- | --- | --- | --- |
| Restore + foreground the game (`ShowWindowAsync(SW_RESTORE)` + `SetForegroundWindow` from an external probe) | false | true | `2880x1800`, `IsZoomed = false` |
| Foreground `Freebuff Desktop` (PID `3800`, HWND `2359988`) | **true** | **false** | placeholder bounds, client `157x25` |

The game minimized with NovaForge doing nothing at all. **The minimization is the game's own
behaviour on losing the foreground**, which is the defining symptom of an exclusive-fullscreen
presentation window: the window does not merely go behind, it becomes iconic.

### 3.2 Code trace: what NovaForge does when the operator switches away

| Question | Finding |
| --- | --- |
| Does NovaForge call `SetForegroundWindow`, `ShowWindow`, `SetWindowPos`, `SetActiveWindow` or `SetFocus`? | No. `grep -rn "SetForegroundWindow\|ShowWindow\|SetWindowPos\|SetActiveWindow\|SetFocus(" src/novacontrol --include=*.py` returns no matches |
| What does the capture worker do with the target? | Reads it only: `GetWindowRect`, `GetClientRect`, `ClientToScreen`, `GetWindowThreadProcessId`, `GetWindowText`, `GetForegroundWindow`, `IsIconic`, `IsWindowVisible`, `GetDpiForWindow` (`src/novacontrol/desktop/capture.py`) |
| What takes the foreground away? | The operator bringing the Freebuff/NovaControl window forward. NovaForge itself never changes focus |
| What does the session loop do each tick? | Reads the window's geometry, compares handle + pid, checks visibility, and (before H02.1) followed the changed rectangle and kept running |
| What did the old capture path do? | GDI `CopyFromScreen` of the primary display, cropped to the target. With the target minimized its rectangle is off-screen, so the crop kept the whole desktop and recorded it as the target frame — the H02 misbinding |

The capture backend is unchanged: a persistent PowerShell worker performing GDI
`CopyFromScreen` of the primary display, then a crop to the target. It has no
`PrintWindow` or Windows Graphics Capture path, so it cannot capture a minimized window,
and it cannot capture an unfocused fullscreen game that the NovaControl window covers.

### 3.3 Root cause

Two distinct facts, which H02 conflated:

1. **The game minimizes on focus loss.** That is the game's fullscreen behaviour; NovaForge
   causes none of it and has no code that manipulates the target window.
2. **NovaForge's defect was its reaction to that state.** A minimized window keeps its
   handle and pid, so identity checks passed while the window had no usable client area.
   The session then treated the desktop fallback as a fresh frame of the target and the
   panel labelled it `LIVE` with the game's name.

H02.1 fixes (2). It does not, and must not, "fix" (1) by forcing the game out of fullscreen.

---

## 4. Phase B — the change

The fix is detection, refusal and honest reporting. No window is moved, restored, restyled
or brought forward by NovaForge, and no display mode is changed.

| File | Change | Why |
| --- | --- | --- |
| `src/novacontrol/desktop/capture.py` | the worker's geometry line now carries `iconic` and `visible` (`hwnd\|bounds\|client\|pid\|scale\|fg\|iconic\|visible\|title`); `_parse_geometry` reads the 9-field shape and still accepts the legacy 7-field shape | the loop must know the window is minimized, not infer it from far-away coordinates; a legacy worker stays usable |
| `src/novacontrol/desktop/input.py` | `WindowGeometry` gains `minimized` and `visible`, serialized in `to_dict()` | identity and visibility are separate facts; input validation and every other consumer see both |
| `src/novacontrol/novaforge/models.py` | `TargetWindow` persists `minimized` and `visible` | the session record keeps the newest measured state instead of only the geometry at validation |
| `src/novacontrol/novaforge/manager.py` | new `_target_visibility_issue()`; the tick pauses with `target_minimized` / `target_hidden` / `target_offscreen`; `validate` refuses a minimized target with restore guidance; `resume` refuses with the same reason; `capture_frame` returns `fresh: false` with `target_issue`; `_pause_for` clears `target_validated` and records `target.minimized` / `.hidden` / `.offscreen` | one predicate, used by validate, every tick, resume and out-of-session capture — a minimized or hidden window is never evidence and never resumable |
| `src/novacontrol/novaforge/guardian.py` | `_target_state` reports `minimized` / `hidden` / `offscreen` with a reason, and the payload carries `target.minimized` / `target.visible` | Guardian and the input boundary must agree with the loop; input stays blocked while held |
| `src/novacontrol/novaforge/timeline.py` | the three new event types are registered in `EVENT_SPECS` (`target.minimized` / `.hidden` / `.offscreen`, category `target`, severity `warning`) and summarised by `summary_text` | `TimelineLog.append` refuses an unknown type and `_event` swallows the refusal, so an unregistered type is recorded nowhere — the pause would look recorded while the timeline showed nothing. This is the same trap `EVENT_SPECS` documents for the action events |
| `src/novacontrol/web/static/js/novaforge.js` | `targetVisibilityLabel()` renders `minimized` / `hidden` / `background`; the Guardian panel gains a "Window state" row; `frameLiveState()` hides the frame whenever the session is not `RUNNING` (`mode: "paused"`) | the panel must not show the last game frame as current while the target is suspended |
| `tests/test_novaforge.py` | `test_minimized_target_pauses_without_recording_a_desktop_frame` (includes the resume refusal, the typed timeline event and resume after restore), `test_offscreen_target_pauses_before_a_desktop_frame_is_recorded`, `test_capture_frame_refuses_a_minimized_target` | the safety behaviour is pinned |
| `tests/test_desktop_input.py` | `test_current_shape_reports_minimized_and_visible`, `test_legacy_shape_defaults_to_visible_and_not_minimized` | the wire shape and its compatibility are pinned |
| `tests/test_novaforge_ui.py` | `test_target_visibility_is_reported_separately_from_identity`, `test_a_paused_or_terminal_session_never_shows_a_live_frame` | the rendering rules are pinned |
| `tests/test_novaforge_gameplay.py`, `tests/test_novaforge_closed_loop.py` | fake desktops report `minimized` / `visible` | the doubles match the real geometry shape |

The legacy 7-field worker shape defaults to `visible = true`, `minimized = false`; the
off-screen placeholder check in `_target_visibility_issue()` still catches a minimized
window reported that way, so an old worker cannot reintroduce the H02 bug.

---

## 5. Phase C — focus-switch verification

### 5.1 Method

The game was already running and was never restarted, moved, resized or restyled. Focus was
changed to the Freebuff window with the same explicit foreground call the operator's own
window switch performs (`ShowWindowAsync(SW_RESTORE)` only when restoring the game, then
`SetForegroundWindow` after `AttachThreadInput`), so no blind click could land in the game.
Sessions were started through the real HTTP API with `frame_interval_seconds: 0.5`, no
action budget, a checkable objective disabled, and **no input path exercised**. Frames were
read back from the session record and inspected as PNGs.

### 5.2 Cycles

| # | Session | Foreground frames | Switch to NovaControl | State after switch | Frames while away | Switch back | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `61d09be1…` (probe) | 3, `19:11:22.6`–`19:11:24.7` | `19:11:25` | `PAUSED`, `target_minimized` at `19:11:25.881` | none; last frame `19:11:24.7` | not resumed (time limit) | pause correct, no desktop frame |
| 2 | `3f84fe42…` | 27, last `19:18:06` | `19:18:07` | `PAUSED`, `target_minimized` at `19:18:10.440` | none; last frame seq 28 `19:18:09.3` | not resumed (40 s limit at `19:18:23`) | pause correct, no desktop frame |
| 3 | `7d33f626…` (first cycle) | 25, last `19:19:32.99` | `19:19:33` | `PAUSED`, `target_minimized` | 1 in-flight frame, `19:19:34.08` | restore + resume `19:20:52.475` | `RUNNING`; frames `28`–`30` at `19:20:53`–`19:20:55` |
| 4 | `7d33f626…` (second cycle) | seq `80`, last `19:21:44.72` | `19:21:44` | `PAUSED`, `target_minimized` | none after `19:21:44.72` | restore + resume `19:21:53.505` | `RUNNING`; frames `82`–`83` at `19:21:54`–`19:21:55` |
| 5 | `f0e859d7…` (third cycle) | 28, last `19:22:51.65` | `19:22:51` | `PAUSED`, `target_minimized` | none after `19:22:51.65` | restore + resume `19:23:00.686` | `RUNNING`; frames `30`–`31` at `19:23:01`–`19:23:02` |
| 6 | `6cad34a8…` (UI check) | 16 | switch away | `PAUSED` in the panel | frozen | restore, panel `RESUME` | `RUNNING`, then `STOPPED` from the panel |

In every pause the frame count stopped increasing; the newest frame at the moment of the
switch is a **game** frame (`window_crop`, `2880x1800`, ~351 KB), and no `screen`-source
frame was recorded. Frames `7d33…/000026.png` (at the switch) and `000030.png` (after
resume) were inspected visually: both are the game's GAME MENU screen, not the desktop.
The API returns at most 30 frames per session (oldest pruned); `seq` keeps increasing, which
is why cycle 4's last frame is `seq 80`.

### 5.3 Resume gating

While the target was minimized, `POST /novaforge/sessions/{id}/resume` returned **HTTP 422**:

```
Resume refused: The target window is minimized. Restore the window to the foreground before resuming.
```

Restoring the window did **not** auto-resume the session: it stayed `PAUSED` until an
explicit resume, which then emitted `target.validated` with reason
`"Resume re-read the window and matched the pinned identity."` and continued capturing at
`2880x1800`. A resume against a stopped session returns HTTP 409.

The pause is also a **typed timeline event**, not only a generic error. Session
`1de43d6677814643ad88452dcd47c0ff` recorded

```
seq 8  target.minimized  category=target  severity=warning
       Target minimized: The target window is minimized.
```

which is queried back with `GET /novaforge/sessions/{id}/timeline?types=target.minimized`.
Before the vocabulary entry was added, this event was refused by `TimelineLog.append` and
silently dropped — the pause still appeared as `guardian.intervention` and
`error.recorded`, but the target-specific event never existed.

### 5.4 Live panel (after a real page reload)

With a session paused on the minimized target, the reloaded NovaForge panel showed:

| Surface | Rendered |
| --- | --- |
| Header | `SESSION PAUSED`, `TARGET Freedom Fighters · minimized`, `CAPTURE ready` |
| Controls | `RESUME` and `STOP` enabled; `START` disabled |
| Live frame | hidden (`mode: "paused"`), not the last image labelled live |
| Guardian | `HELD`; `TARGET VALIDATION minimized`; `WINDOW STATE minimized` with "capture and input are paused until it is restored"; `INPUT BOUNDARY blocked`; intervention row `target — paused / target_minimized` |
| `RESUME` click (minimized) | error notice: "The resume request was refused: Resume refused: The target window is minimized. Restore the window to the foreground before resuming." |
| `RESUME` click (after restore) | "Resumed against the re-validated window."; `SESSION RUNNING`, `TARGET Freedom Fighters · foreground` |
| `STOP` click | "Stopped. Input cleanup: no held input."; `SESSION STOPPED` |

The same panel was reloaded **before** those clicks, so this is the served code, not a
stale DOM.

### 5.5 Safety

| Check | Result |
| --- | --- |
| Keyboard/mouse input sent to the game | none — every session: `actions_attempted = 0`, `actions_executed = 0`, `input_cleanup.presses = 0`, `held_after = 0` |
| Autonomous loop / policy | not started |
| Target window moved, restored or restyled **by NovaForge** | no — all restores/foreground switches came from the external probe used to simulate the operator's own window switch |
| Display mode / resolution changed | no |
| Game process restarted by this investigation | no (the process at `00:10:51` was the one under test) |

---

## 6. Tests and type checks

```
PYTHONPATH=src python -m pytest tests/test_novaforge.py tests/test_novaforge_api.py \
  tests/test_novaforge_ui.py tests/test_novaforge_gameplay.py \
  tests/test_novaforge_gameplay_api.py tests/test_novaforge_closed_loop.py \
  tests/test_novaforge_timeline.py tests/test_desktop.py tests/test_desktop_input.py -q
→ 515 passed, 138 subtests passed in 99.42s

PYTHONPATH=src python -m mypy src                     → Success: no issues found in 433 source files
PYTHONPATH=src python -m mypy src --platform win32    → Success: no issues found in 433 source files
```

---

## 7. What is fixed, what is mitigated, what is still blocked

| Item | Status |
| --- | --- |
| NovaForge reporting a minimized target as valid | **FIXED** — identity and visibility are separate; the tick pauses and records `target_minimized` |
| Desktop fallback recorded/shown as the game | **FIXED** — no frame is captured while the target is minimized/hidden/off-screen; a paused session shows no live image |
| Resume while minimized | **FIXED** — refused with a restore instruction (API and panel) |
| Guardian / panel / timeline honesty | **FIXED** — all report the measured state and reason |
| The game minimizing when it loses focus | **NOT A DEFECT / NOT CHANGED** — the game's own fullscreen behaviour. The explicit, reversible recommendation is a windowed or borderless game setting (or a second display) if the operator wants the game visible beside NovaForge; NovaForge makes no such change itself |
| Capturing the game while NovaControl is in front | **BLOCKED** — GDI primary-display capture cannot see a minimized or covered fullscreen surface. Target-surface capture (Windows Graphics Capture / DXGI, or a verified `PrintWindow` path) is a separate design task and was not added |

This report does not claim a live game feed while the operator is using NovaForge on one
display. It claims the opposite of H02: such a feed is now honestly reported as paused,
with the target's minimized state named, instead of showing the desktop as the game.

---

## 8. Evidence index

| Evidence | Location |
| --- | --- |
| Sessions cited above | `61d09be11c414d3bbbcee83260fa988d`, `3f84fe424db9479c9133cf93328dd365`, `7d33f6269e024ba997d4d62b408752aa`, `f0e859d7bc204eb195fe35fd741ba81d`, `6cad34a8bf014e0ba9dfcb1d2d2352b0`, `ef0efabc41f14d57a32840621611c2a8`, `1de43d6677814643ad88452dcd47c0ff` (`GET /novaforge/sessions/{id}`) |
| Pause/resume events | `GET /novaforge/sessions/7d33f6269e024ba997d4d62b408752aa/timeline` (resumes at `19:20:52.475` and `19:21:53.505`; `guardian.intervention` `reason=target_minimized, outcome=paused` at `19:19:35.149` and `19:21:46.133`), `…/f0e859d7bc204eb195fe35fd741ba81d/timeline` (resume `19:23:00.686`), `…/1de43d6677814643ad88452dcd47c0ff/timeline?types=target.minimized` (typed event) |
| Frames inspected | `data/novaforge/frames/7d33f6269e024ba997d4d62b408752aa/000026.png`, `…/000030.png` |
| Pause reason and visibility in the record | `pause_reason: "target_minimized"`; `target.minimized: true`, `target.foreground: false`, bounds `-32000,-32000` |
| Live panel checks | DOM inspection and clicks against `http://127.0.0.1:8001/` after reload (header, Guardian, `RESUME`, `STOP`) |
| Tests | the nine files in §6 |
| H02 baseline | [NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md](NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md) |

No commit, push, reset, clean or stash was performed. No game input was sent, and no
autonomous gameplay was started. This investigation stops here; the next game-observation
test was not started.
