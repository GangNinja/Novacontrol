# NovaForge NF-13A — Embedded Game Host (native window inside NovaControl)

**Status: PARTIALLY VERIFIED — the host is live-verified; embedding an installed GAME is blocked
on this host because every launchable title tested presents a borderless-fullscreen surface that
Windows refuses to reparent (and that the game answers by exiting). The milestone is NOT complete.**

A verification pass on 2026-10-24 re-checked the phase against its requirements, found and fixed
**ten defects** (§3.1) and re-ran the suites and the live probe — twice, cleanly; the results below
are the post-fix ones.

This phase asked one narrow question: *can a game's own top-level window become a child of a
NovaControl widget, and can that be proven from the parent relationship, the window style and the
client geometry?* The answer is yes for an ordinary windowed top-level window, and no for the
borderless-fullscreen windows the installed games open with. Both halves are measured below.

Nothing in this phase claims universal compatibility, gameplay, synthesized input or capture
improvement. Window embedding, frame capture and AI gameplay remain three separate capabilities.

---

## 1. What was built, and where

| File | Role |
| --- | --- |
| `src/novacontrol/novaforge/embedding.py` (new, 1,391 lines) | The host. `WindowApi` protocol, `Win32WindowApi` (the only ctypes in the subsystem), `EmbeddedGameHost`, `GameIdentity`, `WindowInfo`, `fit_rect`, `window_mode`, `parse_start_time`, the seven states. |
| `src/novacontrol/gui/game_host.py` (new, 583 lines) | The PySide6 surface: `GameSurface` (a widget with `WA_NativeWindow`, so `winId()` is a real HWND) and `GameHostPanel` (library, launch, embed, detach, focus, close, status, one 750 ms timer). |
| `src/novacontrol/gui/app.py` | Adds the `NovaForge` dashboard tab (`DashboardTab.NOVAFORGE` already existed), lazy-loads the library when the tab is first opened, and detaches an embedded game in `closeEvent` **before** the dashboard's native windows are destroyed. |
| `src/novacontrol/gui/theme.py` | `_has_animations` now falls back to `SPI_GETCLIENTAREAANIMATION` when the installed PySide6 no longer exposes `QStyleHints.hasAnimations()` (6.12 removed it, and the dashboard crashed on startup with 6.12). |
| `tests/test_novaforge_embedding.py` (new, 60 tests) | The host against a stateful fake `WindowApi`, plus the Win32 adapter's refusal to report an unmeasured rectangle. |
| `tests/test_gui.py` | `GameHostTabTests`: panel construction, library read, capture label, shutdown (offscreen). |
| `scripts/novaforge_embedding_acceptance.py` | Live acceptance against a catalogued game (plan → token → execute → preflight → embed → capture → detach → close). |
| `scripts/novaforge_embedding_host_probe.py` + `scripts/nf13a_embed_probe_window.ps1` | Live probe of the HOST against a disposable real native window (a WinForms window from another process). Explicitly not a game. |

**Reuse, not a second system.** The panel reads the NF-12 catalog through
`NovaControlApplication.game_library()`, plans through `plan_game_launch()` and executes through
`execute_game_launch(approval_token=...)`, i.e. the same single-use token the web UI and chat use.
Closing goes through `close_game_launch()`, which sends WM_CLOSE and confirms the exit; nothing in
this phase calls `TerminateProcess`, `taskkill`, `SendInput` or `ChangeDisplaySettings`. The
`EmbeddedGameHost` never launches anything: it is handed an identity.

The web UI does **not** expose the host. A browser page cannot parent an HWND, so adding an HTTP
route that could only ever say "not available" would be a false surface. The dashboard tab is the
only entry point.

---

## 2. Native embedding design

### 2.1 States

Seven states, deliberately not folded into each other:

`launching` → `running` → `embedding` → `embedded` → (`detached` | `closed`), plus
`embedding_unsupported` when the host looked and refused.

A successful launch is `running`, never `embedded`. Window discovery is `running`, never `embedded`.
`embedded` is only reported after a read-back (§2.3).

### 2.2 Identity

The identity is `(pid, start time, window handle)`, never a PID alone:

* the window must belong to the requested PID (`window_owner_mismatch` otherwise);
* the process creation time read from Win32 must match the launch record's start time within 2 s
  (`parse_start_time` tolerates PowerShell's seven fractional digits and reads naive timestamps as
  UTC). A launch record with no start time, an unreadable one, or an unreadable creation time is
  **refused** (`process_identity_mismatch`), not guessed;
* the same comparison runs again on every `check()` while embedded, so a reused PID ends the
  embedding (`process_identity_changed`).

### 2.3 Preflight gates (read-only, nothing is touched)

`platform_unsupported` (not Windows) · `no_process` · `window_not_found` (visible top-level window
with area, unowned) · `window_owner_mismatch` · `process_identity_mismatch` · `window_minimized` ·
`window_hidden` · `already_child` · `zero_geometry` · `fullscreen_presentation_unsupported`.

### 2.4 The mutation, and the ordering bug it exposed

1. Read the window: style, ex-style, screen rect, parent, DPI, monitor.
2. Save `(parent, style, screen rect)`.
3. Apply `WS_CHILD` and clear `WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU |
   WS_MINIMIZEBOX | WS_MAXIMIZEBOX`, read the style back.
4. `SetParent(game, host)`.
5. Compute the letterboxed target from the **host's physical client rect**, `SetWindowPos` it.
6. Verify: parent == host, `WS_CHILD` set, visible, non-zero owner, screen rect inside the host
   client area within 2 px, size equal to the requested target.

**Step 3 must precede step 4.** The first implementation called `SetParent` first and then changed
the style; with `WS_POPUP` still set, `GetParent` keeps answering `0` after a successful
`SetParent`. The host therefore "proved" the reparent had failed and restored the window from that
state — which is how the disposable probe window was destroyed on the first live attempt
(`data/novaforge/nf13a/20261024T081947Z_host_probe_report.json`:
`SetParent did not take effect (parent is 0, expected 590990)`). Applying `WS_CHILD` first, then
`SetParent`, then reading both back, fixed it; the next run embedded and verified
(`20261024T082306Z_host_probe_report.json`). This is pinned by
`test_child_style_is_applied_before_set_parent`, which asserts the call order.

### 2.5 DPI and geometry

All geometry is in **physical pixels**, read from Win32: the host's client rect, the host's client
origin, the game's screen rect. Qt's logical sizes are never used to decide the game's size — the
widget only triggers a deferred `resize()` on its own resize/show events (`QTimer.singleShot(0)`),
and `resize()` reads the physical client rect itself. `fit_rect` letterboxes the preserved aspect
inside the client area and centres it (a wide panel gets side bars; the window is never stretched).

**The aspect that is preserved is the content's: the CLIENT rect, not the framed window rect.**
Clearing `WS_CAPTION`/`WS_THICKFRAME` turns the former client area into the whole window, so a
1600×1000 window holding 1584×880 of content must be sized 1.8, not 1.6. The first live probe used
the window rect and produced a 1282×712 target (1.80) for 1768×922 of content (1.92) — a 6 %
vertical stretch that only a measurement reveals. It now reads `info.client` (falling back to the
window rect when the client is degenerate), and the probe's corrected target is 1365×712 at x = 439
inside 2244×712. A frameless window has client == rect, so borderless presentation is unaffected.
Pinned by `test_content_aspect_is_taken_from_the_client_not_the_framed_rect`.

### 2.6 Lifecycle

* **Timer:** one `QTimer` (750 ms) in the panel drives `host.check()`; it stops itself in `closed`
  and is restarted by the next launch. There is no thread and no free-running poll loop.
* **Destroyed window:** `check()` → `closed` / `process_exited`.
* **Recreated window:** `detached` / `window_replaced` — the replacement is never attached
  automatically.
* **Minimized / hidden while embedded:** detach and restore the original window.
* **The HOST hidden** (a dashboard tab switch, or the dashboard minimized) is *not* that: a child
  is visible only while every ancestor is, so `check()` asks `_host_on_screen()` first and keeps the
  embedding (`last_check_reason: host_hidden`), while `resize()` reports `deferred: true` and the
  surface's show/resize event applies it. Without that distinction every tab switch detached a
  healthy embedding (§3.1).
* **Destroyed host surface:** `host_destroyed` — the game window is restored and the embedding ends.
* **Left the host by itself** (the game re-parented): `detached` / `window_left_host`; the saved
  state is dropped because restoring from it would be a lie.
* **Resize failure:** the last verified geometry is re-applied if possible; if it is not, the
  window is restored and the host reports `resize_failed`.
* **Dashboard close:** `shutdown()` detaches and restores; it never closes a game.
* **Focus:** only `focus_game()`, called from the explicit *Focus Game* button, moves focus —
  `AttachThreadInput` + `SetFocus` on the game's HWND, so the foreground window never changes.
  `check()` and `resize()` never touch focus (pinned by
  `test_lifecycle_polling_never_steals_focus`). Clicks inside the embedded surface reach the game
  because it is a real child window, not an image.

### 2.7 Fullscreen is a refusal, not a risk

`window_mode` classifies a captionless `WS_POPUP` window whose rect equals its monitor as
`borderless_fullscreen`. Those windows are refused **by preflight**, before any native call, with
`fullscreen_presentation_unsupported`. There is no confirmation flag and no bypass; the measured
cost of trying is described in §4.

---

## 3. Automated tests

```
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/Scripts/python.exe -m pytest \
    tests/test_novaforge_embedding.py tests/test_gui.py -q
→ 68 passed, 6 subtests passed   (60 host tests + 8 GUI tests)
```

Every host test drives a stateful fake `WindowApi` (`tests/test_novaforge_embedding.py`): no test
reparents, resizes or focuses a real window. **These are mocked tests; the live rendering evidence
is §4 and §5.** Coverage, by the phase's list:

* platform unsupported, missing/hidden/minimized/already-child/zero-area windows;
* PID reuse (creation time differs), unreadable identity, no PID;
* successful embed (parent + `WS_CHILD` + geometry read back), style failure, `SetParent` failure,
  position failure — each asserting the original window is restored;
* destroyed window while embedded, recreated window (no automatic re-attach), window that left the
  host, minimized/hidden transitions, a destroyed host surface (`host_destroyed`), and a hidden host
  keeping the embedding (`host_hidden`) with a deferred resize;
* the Win32 adapter refusing to report geometry it could not read (and converting the reads that
  did succeed), and `preflight()` turning a window that vanishes mid-read into `window_not_found`;
* a refused second `embed()` leaving the embedded state intact, and `detach()` with nothing saved
  refusing instead of claiming a restore;
* resize letterboxing, resize verification failure (restore), resize that cannot be applied (last
  verified geometry kept), DPI-independent geometry through the physical client rect;
* focus only on explicit request, no focus calls from polling;
* user-owned instance (`adopt_running`) is never closable; service-owned is;
* graceful detach (unparent before restoring the style), exact parent/style/rect restoration,
  re-embed after detach, detach with nothing embedded refused;
* embedding and capture observation are separate keys
  (`test_embedding_and_capture_observation_stay_separate`, `implied_by_embedding: False`).

**Repository checks run for this phase**

```
PYTHONPATH=src .venv/Scripts/python.exe -m mypy src                        → Success, 438 files
PYTHONPATH=src .venv/Scripts/python.exe -m mypy src --platform win32      → Success, 438 files
```

### 3.1 Verification pass (2026-10-24) — defects found and fixed

Each item is pinned by the named test; the live probe re-verified the tab-switch behaviour that
mocked tests can only approximate.

1. **A dashboard tab switch detached a healthy embedding.** Hiding a Qt tab hides the surface, and
   Win32 then reports the child as *not visible* (a child is visible only while every ancestor is).
   `check()` read that as "the game hid itself" and restored/detached on every tab change. It now
   asks whether the HOST is on screen (`_host_on_screen`) and keeps the embedding when only the host
   is hidden. Pinned by `test_hidden_host_keeps_the_embedding`; live-verified (§5).
2. **Resizing while the surface was hidden reported a failure and could restore the window.** The
   resize is now deferred (`deferred: true`) while the surface is off screen, and the surface's own
   show/resize event applies it. Pinned by `test_resize_is_deferred_while_the_surface_is_hidden`.
3. **A destroyed host window was never noticed.** The old check only compared the game's parent, so
   a host destroyed out from under it could still look embedded. `check()` now detects it, restores
   the game window and reports `host_destroyed`. Pinned by
   `test_host_destroyed_detaches_and_returns_the_window`.
4. **A refused second embed overwrote the embedded state.** `embed()` reported `already_embedded`
   through the generic failure path, which set the state to `embedding_unsupported` while the window
   was still parented — losing the only Detach path. The refusal now leaves the state alone. Pinned
   by the extended `test_embedding_while_embedded_requires_detach`.
5. **Detach after a refused embed claimed a restore that never happened.** With nothing saved,
   `detach()` reported `detached: true, restored: false` ("could not be fully restored"). It now
   refuses with "nothing to restore". Pinned by `test_detach_after_a_refused_embed_claims_nothing`.
6. **Off-Windows embed blamed the host handle.** The handle check ran before the platform check, so
   a valid Qt handle on Linux produced "the panel has no native window handle" instead of
   `platform_unsupported`. Platform is now checked first. Pinned by
   `test_embed_off_windows_names_the_platform_not_the_handle`.
7. **Placement did not request a frame change.** The caption/thick-frame had just been cleared, so
   `SetWindowPos` now includes `SWP_FRAMECHANGED`. Pinned by
   `test_placement_requests_a_frame_change`.
8. **`set_running` defaulted to `owned=True`.** A caller passing a catalog row without an ownership
   flag made a user-owned game closable through the service. Ownership now defaults to False; only
   the launch path says `owned=True`. Pinned by
   `test_a_running_record_without_ownership_is_not_closable`.
9. **The surface used static `QTimer.singleShot` for refits.** That schedules one timer per resize
   event (not the "one per burst" the comment claimed) and can fire into a deleted widget during
   close. It is now a child `QTimer` owned by the surface: coalesced and destroyed with it.
10. **A failed Win32 geometry read was reported as a rectangle.** `GetWindowRect` / `GetClientRect` /
    `ClientToScreen` — in the adapter's quad reads and in `window()` itself — were called without
    checking their return value, so a window that had just died produced a *measurement*: probe run
    `20261024T104209Z` recorded `rect_after_show` at **(-31405, -31210)**, the uninitialised struct.
    They now raise `WindowApiError`, `window()` refuses rather than reporting 0×0 geometry,
    `preflight()` turns a mid-read disappearance into `window_not_found`, `resize()` reports an
    unmeasurable surface instead of sizing from a guessed rect, and the probe records `null` for a
    read that refused. Pinned by `Win32GeometryReadTests` (refusal, the mid-read case, and the
    successful conversion, driven through a fake `user32`) and
    `test_a_window_that_vanishes_during_preflight_is_window_not_found`.

Also: `detach()` now clears `host_handle` instead of reporting a stale one, and the probe fixture
merges its stderr into an artifact and guards a vanished window instead of crashing mid-measurement.

**One "failure" was the fixture, not the host.** Three probe runs (`20261024T102444Z`,
`20261024T102903Z`, `20261024T104209Z`) ended with "Window … does not exist". The fixture's own
loop was `while ($form.Visible) { … }`; WinForms reads `Visible` through the parent chain, so the
moment the window was parented to the dashboard and the dashboard was hidden (the tab-switch step)
the loop ended the **process** — exit code 0, empty stderr, no paint failures. That is now
measured rather than assumed: the fixture loop is time-bounded (`$form.IsDisposed` + a deadline),
it logs paint failures to `-LogPath`, and the probe records process/window state after capture and
after detach (`fixture_after_capture`, `fixture_after_detach`). Both clean runs
(`20261024T104803Z`, `20261024T105352Z`) report `process_alive: true` at each of those points and
complete detach, restore and re-embed.

Full-suite groups (this host, 5,015 tests collected):

| Group | Result |
| --- | --- |
| `tests/test_a*.py` … `test_b*.py` … first 46 files | 1,424 passed, 1 failed, 1 skipped, 1,813 subtests passed |
| next 23 files (through `test_model_pipeline_phase7.py`) | 625 passed + 1 failed on the first pass; the failure (`test_an_undeclared_installed_vlm_is_selected_by_measurement`) was a working-tree regression from the vision-model phase, not NF-13A: `_selected_vision_model` short-circuited on `ollama_disabled()` even for an injected fake runtime. The gate is now scoped to the local `ollama` provider; the file re-ran 21 passed. |
| NovaForge files run per-file (slow suite): `test_novaforge` 77, `test_novaforge_api` 31, `test_novaforge_benchmark` 60 (+111 subtests), `test_novaforge_benchmark_api` 15, `test_novaforge_closed_loop` 47, `test_novaforge_coach` 49 (+19 subtests), `test_novaforge_embedding` 48 (+3 subtests; 60 (+6) after the audit), `test_novaforge_evaluation` 63 (+34 subtests) | **390 passed, 164 subtests passed, no failures** |
| Remaining NovaForge/perception/optimization files and the third file group | **not completed** — the per-file run was stopped after the eight files above; see §9 |

The one remaining full-suite failure is **unrelated to this phase and reproduces standalone**:
`tests/test_browser_token_reuse_e2e.py::…::test_approve_and_run_reuses_token_without_replanning`
times out (60 s Playwright `wait_for_function`) while the live Bing search execution runs; the
window-surface/browser code is untouched by NF-13A. It is reported as failing, not as passing.

---

## 4. Live verification — the games

### 4.1 Shadow Fight Arena (lightest installed target), final tree

Artifact: `data/novaforge/nf13a/20261024T084954Z_report.json`.

* Catalog: 12 titles, 4 `INSTALLED_AND_LAUNCHABLE`; memory gate read **2.9 GB free / 15.37 GB**
  (minimum 2.0 GB, 81 % used) → pass.
* Nothing was running beforehand; the plan carried a token; execute → `LAUNCH_VERIFIED`
  (`launch_requested / process_started / window_detected / launch_verified` all true),
  pid **21848**, start `2026-10-24T08:50:20.0094702Z`, process `SFA`, window `Shadow Fight Arena`.
* Window facts before embedding: class `UnityWndClass`, style `0x94000000`
  (`WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS`), rect **(0, 0) 2880×1800**, client 2880×1800,
  DPI **192** — a captionless popup covering the whole 2880×1800 monitor.
* `preflight` → `supported: false`, `reason_code: fullscreen_presentation_unsupported`,
  `window_mode: borderless_fullscreen`.
* `embed` → `embedded: false`, state `embedding_unsupported`, **no native call was made**: the
  window's style, parent and rect were untouched.
* Graceful close: `CLOSED`, "Shadow Fight Arena exited after a graceful close (SFA, pid 21848)".
  No orphan process, no broken window.

Capture of the same game **before** any embedding attempt: `printwindow` returned a 2880×1800 frame
in 1,370 ms whose content inspection says `blank: true, mean_luma: 0.0, uniform_ratio: 1.0`
("essentially blank … how a renderer that refuses window capture answers"). So even unembedded,
this title's window-surface capture produces no content — see §6.

### 4.2 What happened when fullscreen windows were still attempted (earlier same-day runs)

These runs predate the hard refusal and are the reason it exists. They are kept because they are
the measured failure, not because the current build would repeat them.

| Run | Game | Window | Result |
| --- | --- | --- | --- |
| `20261024T081246Z_report.json` | Shadow Fight Arena | `0x94000000`, 2880×1800 | `SetParent` refused (`parent is 0`); `embedding_unsupported`. |
| `20261024T081458Z_report.json` | Supermarket Together | `0x94000000`, 2880×1800 | `SetParent` refused; the follow-up close then reported "No window matching 'supermarket together' is running — nothing was stopped", i.e. the game's window was gone. |

The host cannot prove that Windows' refusal *caused* the process to exit, and does not claim it.
What the runs establish is the observation: on this host, an attempted reparent of these
borderless-fullscreen Unity windows was followed by the game's window disappearing. A refusal that
costs a game is not a useful feature, which is why fullscreen is now refused outright and the game
survives (§4.1).

No further titles were attempted after that. GTA V was never launched, and the disconnected WD My
Passport drive was never read (the catalog refuses non-fixed drives; only internal storage is
scanned).

### 4.3 Games tested and their exact presentation

| Title | Process | Window class | Style | Rect | Mode | Embedding |
| --- | --- | --- | --- | --- | --- | --- |
| Shadow Fight Arena | `SFA` | `UnityWndClass` | `0x94000000` | (0,0) 2880×1800 | borderless fullscreen, DPI 192 | refused (`fullscreen_presentation_unsupported`) |
| Supermarket Together | `Supermarket Together` | `UnityWndClass` | `0x94000000` | (0,0) 2880×1800 | borderless fullscreen, DPI 192 | refused by today's preflight; earlier attempt failed at `SetParent` |

**No installed game was successfully embedded. That is the blocking result of this phase.**

The only honest way to give these games a host is to set **Windowed** mode in the game's own
settings (or its own launcher option) and then use the panel's *Embed Game*. NovaControl does not
change the game's display mode, configuration or launch arguments to arrange that.

---

## 5. Live verification — the host mechanism

Because no game could be reparented, the host itself was exercised against a **disposable real
native window**: a framed WinForms window in a separate PowerShell process — 1800×1000 of window
around **1768×922 of client area** (DPI 96) — repainting a frame counter every 120 ms. It is explicitly **not a game**, and the
artifact says so (`gameplay_claimed: false`). This is the evidence that the mechanism renders,
resizes, focuses, detaches and restores; it is not evidence that a game does.

Artifact: `data/novaforge/nf13a/20261024T105352Z_host_probe_report.json` (errors: none) — the second
post-audit run, which also contains the tab-switch step (`hidden_surface`) and the fixture's own
liveness; `20261024T104803Z` is the first, identical in every step. Screenshots:
`20261024T105352Z_probe_panel.png`, `…_probe_embedded.png`, `…_probe_fresh_a/b.png`,
`…_probe_980x700.png`, `…_probe_1240x760.png`, `…_probe_restored.png`. The pre-audit run
`20261024T101006Z` remains the same evidence without the tab-switch step. The panel screenshot is
committed as [`screenshots/novaforge-nf13a-embedded-host.png`](../screenshots/novaforge-nf13a-embedded-host.png)
and shows the shipped panel around the hosted window: the real 12-row NF-12 library, and
`Embedding: Embedded — Parent verified as 3542006; WS_CHILD applied; window 1365x712 inside the
2244x712 client area`, with the identity line (pid, hwnd, window title, DPI, owned) and the capture
line kept separate.

**Embedding.** preflight `supported`, `windowed`. `embed` → `embedded: true`, parent verified as
the game surface's HWND (`3542006` in the cited run; every run a different handle, as expected),
`WS_CHILD` read back (`style 0x56000000`), window placed at
**1365×712 inside the 2244×712 client area at x = 439** — the fixture's 1768×922 **content** aspect
ratio (1.918) letterboxed and centred, not stretched. (Sizing by the framed 1800×1000 window rect
would have produced 1282×712 — a 6 % vertical stretch. See §2.5.)

**It renders inside NovaControl.** The screenshot of the surface is the probe's own pixels
(centred text, frame counter, its two-tone background), with the letterbox bars around them. It is
a screenshot *of the hosted area*, taken so a human can see it; the claim that the window is the
probe's comes from the parent/style/geometry read-back, not from the image. The panel screenshot
(§11) shows the same pixels inside the shipped panel, next to the status lines.

**Freshness.** Two grabs of the same surface 1.1 s apart produced different content hashes
(`622d7fff…` → `f4d7faaa…`), both non-blank. A frozen or stale surface would repeat the first.

**Resize.** Dashboard 980×700 → surface client 1844×552 → game 1058×552 at x = 393 inside the client
(screen x = 531); 1240×760 → 2364×672 → 1289×672 (x = 537, screen 675); parent unchanged, window
responsive (`SendMessageTimeout(WM_NULL)`) at every step.

**Focus.** After the explicit *Focus Game*, the probe's own thread reports the probe as its focus
window (`GetGUIThreadInfo().hwndFocus == probe`), while `GetForegroundWindow()` remains
NovaControl's top-level window — focus moved *inside* the dashboard, it did not steal from another
application. No input was synthesized at any point.

**A tab switch keeps the embedding.** Hiding the dashboard (`window.hide()`), then five `check()`
polls: the state stays `embedded`, `last_check_reason` is `host_hidden`, the game is still parented
(`parent 3542006`), and a `resize()` in that state reports **`deferred: true`** instead of failing.
After `show()`, the deferred resize lands on its own: state `embedded`, window **1289×672 at
(675, 928)** inside the 2364×672 client area. Before the fix this same step detached the window
(and the defunct fixture then ended its process — see §3.1).

**Detach / restore.** `detached: true`, `restored: true`; parent returned to 0, style back to
`0x16CA0000`, screen rect back to (540, 352) 1800×1000 — byte-identical to the pre-embed values —
and the window still responsive. The unembedded capture after detach then works: `printwindow`,
1800×1000, 29,065 bytes, non-blank.

**Re-embed.** A second `embed()` after detach succeeded (parent set again), and the final detach
left the window at parent 0, responsive. The probe process is then closed with WM_CLOSE by the
script that started it.

---

## 6. Capture — reported separately, and not improved by embedding

Embedding and capture are independent facts in the payload: `status_payload(observation=…)` copies
the caller's observation under its own key with `implied_by_embedding: False`, and the panel shows
them on separate lines. Nothing in this phase treats a reparented window as capturable.

**Measured against the existing NovaForge capture seam**
(`LocalDesktopRunner.capture_window`, i.e. `PrintWindow(PW_RENDERFULLCONTENT)` in the capture
worker):

| State | Target | Result |
| --- | --- | --- |
| before embedding | Shadow Fight Arena (2880×1800) | `ok`, `printwindow`, 1,370 ms — but content inspection says **blank** (`mean_luma 0.0`, uniform) |
| embedded | probe window | **fails**: "the capture worker stopped responding" (two attempts, worker restarted between them) |
| after detach | the same probe window | `ok`, `printwindow`, 1800×1000, non-blank |
| embedded | display crop of the hosted surface | works, fresh (two distinct hashes), non-blank |

The embedded-window capture failure reproduced in every probe run
(`20261024T083017Z`, `20261024T083914Z`, `20261024T084632Z`, `20261024T100642Z`,
`20261024T101006Z`, `20261024T102709Z`, `20261024T104803Z`, `20261024T105352Z`), reporting either
"the capture worker stopped responding" (two worker attempts, restarted between them) or
`printwindow_refused`, and the
same seam captured the same window successfully after detach — so it is the *hosted* window that the
window-surface backend cannot render on this host, not a broken capture worker.

The earlier "the fixture died under capture" note was wrong, and the audit corrected it. The
fixture process was ending itself: its loop keyed on `$form.Visible`, which WinForms resolves
through the parent chain, so parenting it to NovaControl (and hiding the dashboard) ended the
**process** — exit code 0, empty stderr, no paint failures (§3.1). It now lives for its requested
duration and logs paint failures; the run above reports `process_alive: true` immediately after the
capture attempt. Capture of an embedded window is still **refused** on this host, and that refusal
is now attributed to the capture seam rather than to a fixture crash. The display-crop path
(`DisplayCropBackend`'s source) does see the hosted pixels, but it is a desktop capture: it requires
the dashboard to be foreground and unoccluded, and it is labelled as display-source, never as a
window frame.

Therefore: **reparenting does not improve capture compatibility here; for the window-surface backend
it regresses it.** Two capture results for games remain unmeasured rather than claimed: a
successfully embedded game's capture (no game could be embedded), and a real game frame at all for
Shadow Fight Arena (its unembedded capture is blank).

No Windows Graphics Capture, DWM-thumbnail or streaming implementation was added. They remain a
separately labelled fallback (§8).

---

## 7. What the panel shows, and what it refuses to say

`GameHostPanel` keeps three status lines distinct: **Embedding** (state label + reason/reason
code), **Identity** (pid, start time, hwnd, window title, DPI, owned-by-this-process) and
**Capture** (available/unavailable + reason + "embedding and capture are separate facts"). A
launch result is rendered as the four independent facts, followed by "Embedding has not been
attempted". Buttons reflect the state: *Launch* is disabled while a game is embedded, *Embed* is
only enabled in `running`/`detached`/`embedding_unsupported`, *Detach* and *Focus* only when
embedded, *Close Test-Launched Game* only for a service-owned instance that is not `closed`.

A user-owned instance (started by the operator, adopted from the library row) can be embedded but
the close button is disabled with an explanation: the host detaches, it does not close what it did
not start.

---

## 8. Fallback strategy

* When embedding is refused, the window is untouched; the game keeps its normal launch behaviour
  (proven: SFA was launched, refused and then closed gracefully by the service).
* When embedding fails *after* a mutation, `restore()` puts the parent, style and screen rect back
  and the host reports `embedding_unsupported` with the exact native error (`embedding_failed`).
* The documented alternative for these titles is **windowed mode chosen by the user in the game**;
  NovaControl does not change it.
* Windows Graphics Capture and streaming remain possible fallbacks and are **not implemented**
  here. If one is added, it must be labelled as a stream/capture view and never reported as native
  embedding.

---

## 9. Known limitations and remaining blockers

1. **Blocker:** the installed launchable titles open borderless-fullscreen windows; Windows refuses
   the reparent and the game's window disappears. Nothing in this build bypasses that, and it is
   not treated as a success.
2. Windows only. A non-Windows host reports `platform_unsupported` from `Win32WindowApi` without
   importing ctypes.
3. Only top-level, visible, non-minimized windows can be hosted; a child window is refused
   (`already_child`).
4. A game that re-parents its own window or recreates it after an in-game display change is reported
   `detached` and is never re-attached automatically.
5. The window-surface capture backend cannot render the hosted window on this host (§6), and
   Shadow Fight Arena's unembedded capture is blank. Capture must not be aimed at an embedded
   window here; the display-crop path sees the pixels but is a desktop capture, labelled as such.
6. The disposable probe fixture is a *test instrument*, not part of the product: it now lives for
   its requested duration and no longer ends its own process when the dashboard is hidden (§3.1),
   but it is still a WinForms window and says nothing about a game's own behaviour.
7. No injection, no anti-cheat bypass, no rendering hook, no display-mode change, no input
   synthesis, no process termination. Closing is `close_game_launch` only.
8. The embedded host is desktop-only and has no HTTP route.
9. The full repository suite was not completed on this machine. What ran is exactly what §3 lists:
   the two groups (1,424 + 625 passed), the targeted 157, the model-pipeline re-run (21) and the
   eight NovaForge files above (390 passed / 164 subtests). The remaining files were still being run
   one by one when the run was stopped, so they are **unverified in this pass**, not passing. The
   browser-token E2E failure in §3 is unrelated and reproduces standalone.

---

## 10. Milestone decision

| Criterion | Result |
| --- | --- |
| Dedicated game surface in the NovaForge workspace | done (dashboard tab + native `GameSurface`) |
| NF-12 catalog and `GameLaunchService` reused | done (same library/plan/execute/close methods) |
| `(pid, start_time, hwnd)` identity tracked | done, and re-checked while embedded |
| Seven states distinguished | done |
| Launch/window discovery never reported as embedding | done and tested |
| Embedding, capture and gameplay kept separate | done (separate payload keys, separate UI lines) |
| Real native window embedded and rendered inside the panel | **yes for the disposable window; NO for any installed game** |
| Resize, focus, detach, restore verified live | yes for the disposable window; not reachable for a game |
| Capture compatibility with an embedded game | **unmeasured** (no game could be embedded) |
| Automated regression tests for the host | done (60 mocked tests + 8 GUI tests + mypy clean) |
| Tab switch / hidden dashboard keeps the embedding | done, live-verified (`…_host_probe_report` `hidden_surface`) |

**NF-13A is partially verified and blocked on the game half.** The host is real, exercised and
tested; the games the phase asked for cannot be hosted in the presentation mode they open in. The
next step is not more host code — it is a windowed game instance (the user's own choice in the
game's settings), after which the same panel's *Embed Game* is the remaining measurement, followed
by capture against a genuinely embedded game. Autonomous gameplay stays out of scope until then.

## 11. Artifacts

Live reports and screenshots are runtime data (git-ignored) under `data/novaforge/nf13a/`:

* `20261024T084954Z_report.json` — Shadow Fight Arena: launch, refusal, graceful close.
* `20261024T105352Z_host_probe_report.json` — **the post-audit host probe** (errors: none): embed,
  render, freshness, focus, resize, hidden-surface/tab switch, capture-during-embed,
  fixture liveness, detach, restore, re-embed. Its `_probe_panel.png` is committed as
  `screenshots/novaforge-nf13a-embedded-host.png`.
* `20261024T104803Z_host_probe_report.json` — the first post-audit run, same steps and result.
* `20261024T101006Z_host_probe_report.json` — the pre-audit host probe, same evidence without the
  tab-switch step.
* `20261024T102444Z`, `20261024T102903Z`, `20261024T104209Z` — the three runs whose fixture ended
  its own process; kept as the measurement that corrected the earlier explanation (§3.1).
* `20261024T081246Z_report.json`, `20261024T081458Z_report.json` — the earlier fullscreen attempts
  that motivated the refusal.
* `20261024T104803Z_probe_embedded.png`, `…_probe_fresh_a/b.png`, `…_probe_980x700.png`,
  `…_probe_1240x760.png`, `…_probe_restored.png` (plus the same set from `…T101006Z`).
