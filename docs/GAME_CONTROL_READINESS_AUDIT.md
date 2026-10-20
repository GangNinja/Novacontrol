# Game Control Readiness Audit

**Question:** is NovaControl technically ready for a real-time game-playing evaluation using
normal human-facing screen and input interfaces?

**Verdict: NOT READY for real-time gameplay.** `GO WITH LIMITATIONS` for a slow or turn-based
game evaluation, and only where every action can be named as a *label on screen*.

> **Remediated 2026-10-17 — read §10 with this verdict.** All six essential blockers are addressed:
> a persistent capture worker (1.7 → **8.2 fps**), coordinate-addressed mouse input (**8.5–21.2 ms**
> per action, live), a bounded key hold with a guaranteed release (`presses == releases`, 4/4), DPI-
> correct geometry validation, per-window geometry, and the agentic observation layer wired to the
> real desktop (`source="none", 0 elements` → `window_probe+browser_dom+screen_text`, 7 windows,
> 265 OCR words). The **real-time** verdict is unchanged — 8.2 fps is 4.6× short and a rich
> observation costs ~1.35 s. The slow/turn-based GO no longer requires label addressing.

The observe → act → observe → verify loop exists and provably works end to end on this host — it
was driven against a disposable window during this audit and every stage produced real evidence.
The blocker is not correctness, it is **rate and addressing**: one observation costs ~571 ms
(1.75 Hz), one label-addressed click costs ~1.0 s, a complete verified vision click costs ~6.1 s,
there is **no coordinate-addressed input action** and **no key-hold primitive**, and the agentic
observe layer is not wired to the desktop at all. A loop that runs at 0.16–0.7 Hz with no way to
hold a key cannot play a real-time game.

---

## 1. Source-tree identity

| | |
|---|---|
| Repository | `NovaControl` (working tree, branch `main`) |
| `HEAD` | `ec03e877092e36a578cda128a37f8511f1a43621` — `feat: add phase 23-26 learning and control systems` (2026-10-16T01:05:14+05:30) |
| Working tree | 20 pre-existing entries (16 modified, 4 untracked) carried in from earlier phases of this session — **all preserved untouched** |
| Interpreter | CPython 3.13.0 (`.venv`, editable install) |
| Host display | 2880×1800 physical, 1440×900 logical → **DPI scale 2.0** (`LOGPIXELSX = 192`), single primary display |
| Model runtime | no Ollama on `127.0.0.1:11434`; live `/vision/status` → `{"vision_model":false,"vision_llm":{"configured":false}}` |

Because the working tree carries earlier-phase edits, every line reference below was taken from
the tree as it stands, and every measurement was taken from that same tree.

### Method

Every capability below was classified from **source + a live measurement**, never from a
docstring, a registry entry or a capability name. Three throwaway probes drove the real code; a
targeted test group and a stubbed-runner end-to-end run supplied the rest.

**Safety contract (enforced in the probe code, not just by intent):**

* Input was sent only while a disposable window **this audit created** provably owned the
  foreground — the probe compared `GetForegroundWindow()` against its own top-level `HWND` and
  aborted the input phases otherwise.
* The click target was the centre of that window's own rect, computed from the same window.
* The previous foreground `HWND` and the previous cursor position were saved and restored in a
  `finally` block, and the restore was verified (`foreground_after == foreground_before`).
* No game was launched, no online multiplayer game was touched, no unrelated process was started,
  closed or typed into, and nothing was sent to any window other than the probe's own.

Probe sources (session artifacts, not committed): `%LOCALAPPDATA%\Temp\nc_game_probe\`
`probe_game_control.py`, `probe_vision_loop.py`, `probe_key_hold.py`, plus their JSON result files.

---

## 2. Capability matrix

Classification: **IMPLEMENTED AND VERIFIED** · **IMPLEMENTED BUT UNVERIFIED** · **PARTIAL** ·
**MISSING** · **UNAVAILABLE**.

| # | Capability | Class | Evidence |
|---|---|---|---|
| 1 | Single full-screen capture | **IMPLEMENTED AND VERIFIED** | `LocalDesktopRunner._take_screenshot` (`desktop/controller.py:1403`) — PowerShell `CopyFromScreen` after `SetProcessDPIAware()`. Measured: 6/6 distinct 2880×1800 PNGs, 537.7–601.7 ms, mean **571.0 ms**. App-facing path `VisionController.capture_screen` (`desktop/vision.py:145`), approval-gated. |
| 2 | Repeated frame acquisition | **PARTIAL** | `ScreenFrameSource.read()` (`perception/frames.py:320`, read at `:357-361`) is wired at `application.py:1637` to `_perception_screen_capture` (`application.py:3926-3933`). But each read re-invokes the **whole** PowerShell capture, so the measured ceiling is **1.75 Hz** against the sampler's own configured floor of `max_fps=5.0` / `min_interval_ms=120` (`perception/sampling.py:51-52`) — the design target is 3.3× faster than the hardware path can feed it. No streaming capture, no in-memory frame buffer (a `Frame` is a *path plus metadata* by design, `frames.py:57`), no secondary-monitor capture (`Screen.PrimaryScreen.Bounds`, `controller.py:1419`). |
| 3 | Text recognition + coordinate extraction | **IMPLEMENTED AND VERIFIED** | Real `Windows.Media.Ocr` via PowerShell (`desktop/vision_guide.py:104-146`), phrase/word matching → click centre (`:191`). Measured live: `locate_element(screenshot, "PROBE")` → `(1055, 269)`, `strategy: "ocr"`, **618.4 ms**. `describe_screen()` (`vision.py:184`) → `source: "window_probe"`, 8 windows enumerated, 828.9 ms. |
| 4 | Vision-model screen understanding | **UNAVAILABLE on this host** (code path present, provider absent) | `VisionController.has_vision_model` (`vision.py:130`) gates the real `MultimodalVisionProcessor`; `/vision/status` reports `vision_model: false`, and no Ollama runtime exists. The layer is honest about it: `describe_screen` returned `source: "window_probe"` with the message "…install an Ollama vision model for full screen understanding", and measured how the same code upgrades with zero changes (`application.py:2487+` `set_vision_llm`). |
| 5 | Mouse move + click at a coordinate | **IMPLEMENTED AND VERIFIED — but only reachable through a LABEL** | `_vision_click` (`controller.py:1270`) does `Cursor::Position` + `mouse_event(2)`/`mouse_event(4)`. Measured with the primitive script reused verbatim: cursor landed **exactly** on target `(333, 288)`, and the window received `M_PRESS@180,90` / `M_RELEASE@180,90` (the window's own client centre). Round trip **1027.4 ms**. There is **no** coordinate-addressed action in `DesktopActionType` (`desktop/models.py:12-35`) — the only click path requires `locate_element` to find the label first. |
| 6 | Mouse button hold / drag / second button | **MISSING** | The only mouse code in `src/novacontrol` is the `mouse_event(2) … mouse_event(4)` pair with a fixed 40 ms gap (`controller.py:1302-1305`). No down-without-up, no drag, no right/middle button, no wheel. |
| 7 | Keyboard key press | **IMPLEMENTED AND VERIFIED** | `_keyboard_shortcut` (`controller.py:1565`) → `SendKeys::SendWait`. Measured: the window received `KEY_PRESS:a` **and** `KEY_RELEASE:a`; round trip **272.4 / 274.8 ms**. Note the action is a *shortcut parser* (splits on `+`, maps a small key table at `:1572-1581`), so single letters and modifiers work and arbitrary keys are limited by SendKeys syntax. |
| 8 | Bounded key HOLD + reliable release | **HOLD MISSING · RELEASE GUARANTEED** | No `keybd_event`/`SendInput` down/up exists anywhere in `src/novacontrol`. Measured: the closest available form, `SendKeys('{a 5}')`, produced **5 discrete press+release pairs, not a hold**, with a press→release gap of ~**11 ms**. Across the whole run presses == releases (**6/6**), so input can never be left stuck down — a genuine safety property that a hold primitive would have to preserve. |
| 9 | Focused-window detection & window identification | **IMPLEMENTED AND VERIFIED (no geometry)** | `_window_lines` (`controller.py:795`) → `Get-Process`/MainWindowHandle enumeration; measured **7 windows** as `process\|title` (e.g. `chrome\|NovaMarket UI Decision - Google Chrome`). `_wait_for_window` (`:810`), `_window_matches` (`:322`), `_focus_window` (`:1120`), and `_game_launch`'s dual window-or-process probe (`:1193`) all work on that. The audit additionally confirmed `GetForegroundWindow()` identifies the foreground HWND exactly. **Limitation:** the probe exposes only `process\|title` — no window rect, no client area, no monitor index — so a plan cannot compute an in-window offset or bound input to a window. |
| 10 | Timing, async execution, timeouts | **IMPLEMENTED AND VERIFIED** | Every `_powershell` call carries a timeout (5–15 s; `:970, :994, :1091, :1143, :1307, :1348, :1426, :1584`); `command_timeout_seconds=60` default (`:597`) enforced with `asyncio.wait_for` raising `TimeoutError` (`:853-858`). Captures use 15 s (`:1426, :1433, :1440`). Measured per-action latencies in §4. |
| 11 | Cancellation of a running action sequence | **PARTIAL** | Timeouts and `asyncio` cancellation exist; the reliability layer's `TaskStateMachine` has cooperative checkpoints. But nothing can interrupt an in-flight desktop input sequence mid-action — `STOP_APP` (`controller.py:1328`) closes a *window*, it does not abort input. There is no input-abort primitive. |
| 12 | Feeding observations into planning → action | **PARTIAL — the desktop half is NOT WIRED** | The vision loop closes for one action (§3, path B). The agentic loop's perception does **not**: `UiPerceptionEngine` accepts a `window_probe` (`agentcore/perception.py:46`) that `application.py:1954` never passes, and `orchestrator._observe` calls `perception.observe()` with **no `screenshot_path`** (`agentcore/orchestrator.py:305`), so the vision block guarded by `self._vision is not None and screenshot_path` (`perception.py:138`) can never run. **Measured: `observe()` → `source: "none"`, 0 elements, empty `raw_text`.** See §3. |
| 13 | Recording outcomes & failure recovery | **IMPLEMENTED AND VERIFIED** | `execute_action` appends every result — denied, completed, failed — exactly once to the shared audit log (`core/audit.py`), pinned by the desktop/browser/phone audit tests. Unverifiable outcomes raise `VerificationError` (`controller.py:284`) rather than claiming success; `_game_launch` documents and executes a deep-link → vision-click fallback; vision failures land in `BugLog` (`core/buglog.py`). The stubbed end-to-end run reproduced honest reporting: 1 action executed, 1 recovery attempt, and the run **reported failure** rather than success (below). |

### Registry, tool catalogue and plugins — declarations checked against execution

The brief asks for the capability registry, tools, plugins and configuration to be inspected, and
this codebase's own rule is that a capability *name* proves nothing. Three layers were read, and
**the vocabulary is narrower than the action enum at every one of them.**

**Capability registry** (`intelligence/capabilities.py`, `default_capabilities()`): **58** declared
capabilities, **9** of them relevant to screen or input. *Nothing declares a coordinate mouse
click, a key hold, or any game-control capability* — the primitives missing from
`DesktopActionType` are missing at the registry layer too.

* `press_key` (capability `desktop.press_key`, risk MEDIUM, executor `desktop_controller`) is
  consistent across **four** layers and genuinely wired: the registry row (`capabilities.py:107`,
  id at `:661`), `IntentName.PRESS_KEY` (`intelligence/intent.py:66`), the application's desktop
  routing set `_DESKTOP_INTENTS` (`application.py:9819-9822`), and the agent tool
  `ActionEngine.press_key` (`agentcore/actions.py:112`, catalogued at `:180`).
* `take_screenshot` declares verifier **`file_exists_verifier`** — a screenshot *file existing* is
  not evidence that the capture is fresh, complete or from the right monitor. That is the same
  *"an action ran" ≠ "it worked"* weakness the reliability layer exists to prevent, and it is the
  declared verifier for the one capability a game loop depends on most.
* `volume_control`, `brightness_control` and `media_control` declare ids
  `desktop.volume`/`desktop.brightness`/`desktop.media`, tags and example utterances, with
  executor `desktop_controller` (`capabilities.py:293, 300, 307`) — and are **wired to nothing**:
  no `DesktopActionType` covers them and none appears in `_DESKTOP_INTENTS`. The intent is
  recognised (exemplars, entity extraction) but cannot execute. **Declared but unwired.**

**Tool catalogue** (`ActionEngine.catalog()`, `agentcore/actions.py:174-184`): 7 tools —
`click_element`, `type_text`, `navigate` (all `surface: "browser"`), plus `press_key`,
`open_application`, `screenshot`, `execute_terminal_command` (desktop). The only desktop input
tools are a key press and a screenshot; the *click* tool is browser-only, so the agent surface
cannot click a desktop coordinate either.

**Plugins** (`src/novacontrol/plugins/examples/`): three examples — `browser-tools`
(`classify_url`), `developer-tools` (`filesystem:read`), `system-tools` (`platform_info`). **None
touches screen capture, mouse, keyboard or game control**, so plugins contribute nothing to this
audit in either direction.

**Configuration** (`perception/sampling.py:41-52`): the only game-relevant setting is the sampling
policy — `max_fps=5.0`, `min_interval_ms=120` — which §4 shows acquisition cannot reach.

**A safety-relevant fallback found while checking the above.** Unmatched desktop phrasing does not
become a refusal: `DesktopStep.plan` sends `kind="execute"` to `plan_execute_script`
(`controller.py:93-94`), so `plan_command("turn the volume down")` returns
`actions=["execute_script"]` with the phrase reported in `unmatched` — a phrase the registry
declares as a LOW-risk volume capability is, at the parser level, **aimed at script execution
instead**. It is **gated, not silent** (`EXECUTE_SCRIPT` requires `desktop_control` +
`shell_execute`, `controller.py:2066-2067`, plus a valid approval token), so it cannot run itself.
For a game evaluation this is the wrong fallback target: a mis-parsed game phrase should be
refused, not re-aimed at a shell.

---

## 3. Runtime call path — what is connected, what is isolated

### A. JARVIS / Command desktop path — **CONNECTED** (open-loop, except for vision clicks)

```
POST /desktop/plan            (or /command/plan for the shared token flow)
  → parse_desktop_command            desktop/controller.py:99    phrase → DesktopStep[]
  → DesktopAutomationController.plan_command                      :1976
  → DesktopWorkflow + single-use 5-min approval token
POST /desktop/execute
  → _execute_planned_command         application.py
  → execute_workflow(workflow, progress=…)                        :2001
      → execute_action → audit_log append                         :2025
          → LocalDesktopRunner.run → _open_application / _keyboard_shortcut /
            _type_text / _game_launch / VISION_CLICK …
```
This path is fully wired. Its only *screen* observation is inside a `VISION_CLICK` step; every
other action is verified by window/process probes, not by looking at the screen.

```
VISION_CLICK  →  controller._vision_click                          :1270
  → _take_screenshot            (physical px)                      :1403
  → vision_guide.locate_element                                    :477
       1. LLM region      (_llm_region, needs a vision provider)   :417
       2. Windows OCR     (_ocr_words → _match_ocr_words)          :146, :191
       3. OCR glyph band  (_sync_locate)                           :510-529
       4. UI landmarks    (_heuristic_landmark)                    :228
  → Cursor::Position + mouse_event(2,0,0,0,0) … mouse_event(4,…)   :1298-1305
  → _take_screenshot "after"
  → VisionController._verify_after_click                           vision.py:371
       cv2 absdiff + threshold → changed_pct, change regions,
       change_near_click, OCR re-check near the click point
```
**This loop is real and was demonstrated live** (§5, probe 2).

### B. Vision panel — **CONNECTED**

`POST /vision/describe` → `describe_screen()` → `capture_screen()` → `_understand()` (real vision
model when wired, honest window probe otherwise). `POST /vision/click` → `guided_click()` →
`plan_vision_click` → the `VISION_CLICK` chain above → verify → `BugLog`. `_guard_label`
(`vision.py:273`) refuses command-shaped labels, which is why this surface cannot be used as a
general input channel.

### C. Agentic loop — **REACHABLE BUT BLIND TO THE DESKTOP**

`POST /agent/run` (`api/models.py:647`, authenticated) → `run_agentic_task` →
`AgenticOrchestrator.run_task` (`orchestrator.py:81`). The sequencing is genuinely
observe → act → observe → verify (`:151-157, :173`, and a final observation at `:263`).

Its perception, however, is constructed **without** a window probe
(`application.py:1954` passes `vision_processor`, `browser_runner`, `completion_provider` only),
and `_observe` passes no `screenshot_path`. Measured directly:

```
UiPerceptionEngine(vision_processor=None, browser_runner=None, completion_provider=None)
  .observe()          # exactly what orchestrator._observe calls
  → source="none"   application="unknown"   elements=0   raw_text=''
```

A full run of the real orchestrator (real planner, verifier, recovery engine, interpreter,
knowledge graph, evaluation ledger, real `ActionEngine`; **only the two OS runners stubbed**, so
nothing touched the desktop) on the goal *"open the calculator application on the desktop"*:

```
status:              failed
verification:        {"final": {"status": "fail",
                                "reason": "No observed evidence supports goal completion."}}
observations (6):    every one "browser |  | elements=0"
actions executed:    1        recovery attempts: 1
OS side effects:     desktop runner calls: 1   browser runner calls: 6   (both stubs)
```

So actions flow and the loop is honest — it refuses to claim completion — but the observation it
verifies against is **empty**, taken only from the browser DOM layer with no URL and therefore no
elements. The desktop was never observed. This is the single most important structural gap for
autonomous play.

### D. Perception pipeline — **CONNECTED, rate-limited**

`PerceptionEngine(screen_source=ScreenFrameSource(capture=self._perception_screen_capture), …)`
(`application.py:1637-1641`). Frames are *references with metadata* (`frames.py:57`), sampled
adaptively (`sampling.py:152 FrameSampler`, `:216 decide`) with change-triggered interval
adaptation, then compared temporally (`temporal.py`) and tracked spatially
(`tracking.py`). The layer is well-built; it just inherits the ~571 ms capture cost per frame.

### Isolated components

* `agentcore` desktop observation (above) — the biggest one.
* The embodied agent loop (`embodied/`) runs over grid/simulation environments, not the screen.
* The planning package plans over abstract world models; nothing routes a desktop screenshot into
  it outside `_vision_click`.

---

## 4. Measured performance (this host, 2880×1800 @ 200 % DPI)

| Operation | Measured | Implied rate |
|---|---:|---:|
| Single full-screen capture (`_take_screenshot`) | **571.0 ms** mean (537.7–601.7, n=6) | **1.75 Hz** |
| Key press round trip (`_keyboard_shortcut('a')`) | **272.4 / 274.8 ms** | — |
| Mouse move + click round trip (`mouse_event` pair) | **1027.4 ms** | — |
| OCR locate (`locate_element`) | **618.4 ms** | — |
| `describe_screen()` (capture + window probe) | **828.9 ms** | — |
| Full verified vision click (`guided_click`) | **6148.5 ms** | **0.16 Hz** |
| **Minimal control loop**: capture + keypress + capture | **≈ 1417 ms** | **≈ 0.71 Hz** |
| Frame sampler's configured floor (`sampling.py:51-52`) | 120 ms / `max_fps=5.0` | 5.0 Hz *(unreachable: 3.3× faster than acquisition)* |

For context, a 30 fps game needs a decision-or-input loop two orders of magnitude faster than the
0.7 Hz this build can sustain, and it needs to hold a key for however long a turn takes.

**DPI coordinate space is a silent total-failure mode.** The first click probe targeted
`(328, 261)` and the cursor landed at exactly **`(164, 131)`** — half the value, a clean 2.0×
miss — because the probe process was DPI-*unaware* while the app's PowerShell input script calls
`SetProcessDPIAware()`. Nothing errored; the click simply went to the wrong place (outside the
window), and only the window's own event log exposed it. The app is internally consistent
(`_take_screenshot` and `_vision_click` are both physical-pixel), but **any new coordinate path
must be DPI-aware end to end or it fails silently at 200 % scaling.**

---

## 5. Probe evidence (raw)

### Probe 1 — `probe_game_control.py`: capture, window probing, key, click

```
dpi_scale 2.0   logpixelsx 192   physical_aware true
disposable_window  {"hwnd": 12190968, "is_foreground": true, "rect":[153,198,360,180],
                    "center":[333,288]}
capture            6 captures, 6 distinct frames, mean 571.0 ms  → 1.75 Hz implied
window_probe       7 lines, sample: "chrome|…", "explorer|", "SystemSettings|Settings" …
key_press          {"primitive":"…_keyboard_shortcut('a') -> SendKeys::SendWait",
                    "round_trip_ms":274.8,"screen_changed":true,"window_received":true}
mouse_click        {"target":[333,288],"round_trip_ms":1027.4,
                    "cursor_moved_to":[333,288],"cursor_move_worked":true,
                    "window_received":true}
events             KEY_PRESS:a / KEY_RELEASE:a / M_PRESS@180,90 / M_RELEASE@180,90
postconditions     cursor restored to [971,753]; foreground restored & verified
```

### Probe 2 — `probe_vision_loop.py`: the real `VisionController` end to end

```
has_vision_model  false                       # honest: no multimodal provider
describe_screen   828.9 ms, source "window_probe", 8 windows, captured true
                  message names the disposable window: "python|NC VISION PROBE - disposable"
locate_element    618.4 ms, found true, point [1055,269], strategy "ocr"
guided_click      6148.5 ms, status "completed", located_by "ocr",
                  verification {"changed": true, "changed_pct": 8.35,
                                "change_near_click": true, "label_gone_near_click": false,
                                "diff_engine": "opencv"}
restored          cursor + foreground restored and verified
```
The window painted a large rectangle on click, and the after-frame diff detected it
(`changed: true`, 8.35 %, `change_near_click: true`). **Observe → locate → act → verify is proven
working on real OS interfaces.**

### Probe 3 — `probe_key_hold.py`: hold vs repeat

```
single_tap      [PRESS:a, RELEASE:a]                     272.4 ms round trip
repeat_count_5  [PRESS:a, RELEASE:a] × 5                 394.3 ms total
timeline        presses and releases alternate ~11 ms apart — never a hold
totals          presses 6, releases 6                    → no stuck key, ever
```

### The agentic run (stubbed OS runners)

See §3.C — 6 observations, all `browser |  | elements=0`; outcome `failed`, reason
*"No observed evidence supports goal completion."*

---

## 6. Tests run and outcomes

Run with `PYTHONPATH=src`, `.venv` interpreter, filtered through a background log whose final line
is the captured exit status (`PYTEST_EXIT=`), so a passing filter cannot be mistaken for a passing
run.

| Group | Tests | Result |
|---|---|---|
| `test_desktop.py test_vision.py test_vision_manager.py test_vision_pipeline.py test_vision_model_config.py test_buglog_vision.py test_jarvis_desktop_vision_fixes.py test_perception.py` | **312 passed, 51 subtests passed**, 31 warnings | **exit 0**, 538.94 s |
| `test_agentcore.py test_embodied.py` | **151 passed, 84 subtests passed**, 211 warnings | **exit 0**, 468.21 s |

An earlier invocation of this group named a file that does not exist (`tests/test_vision_guide.py`)
and exited 4 with *"no tests ran"*; that run is recorded here rather than quietly dropped, and the
corrected file list above is what produced the 312 passes.

**Stated limitation of that coverage:** the desktop/vision tests pass with **stubbed OS probes**
(`ProbingRunner` in `test_desktop.py`), and `test_agentcore.py` exercises `UiPerceptionEngine` with
`browser_runner=DomRunner()` and a `url=` argument — the browser DOM layer. That is exactly why
they are green while the wired desktop perception returns nothing: the tests exercise the engine's
capabilities, not the application's wiring. No amount of reading those tests would have revealed
the missing `window_probe`; the measurement did.

The full ~2,254-test suite was **not** run (deliberately, per scope) and the completed
Integration & Performance Audit was **not** repeated.

---

## 7. Minimum blockers

### Essential — without these a real-time game cannot be driven

| # | Blocker | Why it is essential | Where it lives |
|---|---|---|---|
| E1 | **Observation rate: 571 ms per frame (1.75 Hz)** | 3.3× below the sampler's own 5 fps floor and two orders below gameplay. Spawning PowerShell + encoding a 5.18 MP PNG per frame is the cost. Needs one persistent capture path (DXGI Desktop Duplication or a long-lived helper process) rather than a process launch per frame. | `controller.py:1403`, `frames.py:357-361` |
| E2 | **No coordinate-addressed input action** | `DesktopActionType` has no mouse move / click-at-point / key down / key up. Every click must first satisfy `locate_element`, and a game has no labels to locate. The gap spans all three layers — the enum, the agent tool catalogue (`click_element` is browser-only) and the capability registry. Add explicit `MOUSE_MOVE`/`MOUSE_CLICK`/`MOUSE_DOWN`/`MOUSE_UP`/`KEY_DOWN`/`KEY_UP` actions to the existing enum and runner. | `desktop/models.py:12-35`, `controller.py:1270`, `agentcore/actions.py:174-184` |
| E3 | **No bounded key hold** | Cannot steer, sprint, charge or hold a direction. Press→release is ~11 ms. A hold primitive must preserve the current guarantee that release always happens (presses == releases, measured 6/6). | absent from `src/novacontrol` — action enum, tool catalogue and capability registry alike |
| E4 | **Coordinate space must be DPI-aware end to end** | Measured a silent 2.0× miss on this 200 % host. A new input path that mixes a DPI-unaware reader with the DPI-aware input script sends every click to the wrong place with no error. | `controller.py:1298-1305` vs any new reader |
| E5 | **Per-window geometry** | The app can name a window but cannot get its rect, client size or monitor, so it cannot compute an in-window offset, bound input to a target window, or survive the window moving. | `controller.py:795`, `_wait_for_window:810` |
| E6 | **Wire the agentic observation layer** | Until `window_probe` is passed and a `screenshot_path` reaches `observe()`, the only closed loop is `_vision_click` for a single action; no autonomous observe→act→observe→verify exists for the desktop. | `application.py:1954`, `orchestrator.py:305`, `perception.py:138` |

### Optional — quality, safety and fidelity

* **O1 · Install a vision model** (llava/moondream) to replace window-probe screen understanding.
  OCR and label location already work without it; `set_vision_llm` hot-swaps it with no restart.
* **O2 · Secondary-monitor capture** — `Screen.PrimaryScreen.Bounds` captures only the primary
  display (`controller.py:1419`), which a game on a second monitor cannot use.
* **O3 · Anti-cheat reality check** — injected `SendKeys`/`mouse_event` input is exactly what
  anti-cheat systems detect. Any game evaluation should use an offline, single-player, windowed
  target at a fixed resolution; this audit deliberately touched no online game.
* **O4 · Input pacing and scheduling** — nothing maps a decision to frame timing; add coalescing
  and a scheduler once acquisition is fast enough to have a frame budget.
* **O5 · An input abort** — `STOP_APP` closes a window; it cannot interrupt an in-flight input
  sequence. A game loop needs a real stop.

---

## 8. What this audit did **not** establish

* **No gameplay claim.** The complete observe → act → observe → verify loop was demonstrated for
  **one** action on a **label-addressed** target with a **cooperative** window. That is not
  gameplay, and nothing here should be read as gameplay readiness.
* **No autonomous desktop loop** was demonstrated — the agentic observation path returns nothing
  (§3.C).
* **No hold, no drag, no coordinate click, no secondary monitor** — all verified absent, not merely
  untested.
* **The full suite was not run**, and real game/GPU performance was not measured. `ruff` is not
  installed on this host (re-verified during this audit: `.venv` reports *No module named ruff*
  and it is not on `PATH`), so no lint pass is claimed.
* **The vision-model path is unverified here** because no multimodal provider exists on this host;
  its code path is present and gated honestly, but this audit did not execute it.
* Two `uvicorn` servers from an earlier phase of this session are still listening on **port 8001**
  (PIDs 28280, 3284). They were deliberately **left running**: they are preview servers rather than
  the user's port 8000 target, and `netstat` shows live `ESTABLISHED` connections to 8001, so
  something (most likely the Preview panel) is actively using them. They are the one piece of
  session state this audit did not clean up, on purpose.

---

## 9. Recommended next steps

1. **Measure a persistent capture path before building anything on top.** A DXGI Desktop
   Duplication or single-helper-process capture is the difference between 1.75 Hz and something a
   game can be evaluated against. Re-run probe 1's capture loop as the acceptance measurement.
2. **Add coordinate and hold primitives to the existing action vocabulary** — `MOUSE_MOVE`,
   `MOUSE_CLICK`, `KEY_DOWN`/`KEY_UP` — and re-run the disposable-window probes as their
   acceptance test, including the presses == releases invariant.
3. **Pass `window_probe` and a `screenshot_path` into `UiPerceptionEngine`** so the agentic
   observe layer actually observes; the stubbed-runner probe in §5 is a ready-made regression
   harness for it.
4. **Expose window geometry** on the window probe so input can be bounded to a target window.
5. **Then** consider a slow, turn-based, offline game evaluation as the first honest test of
   play — not a real-time one.

---

## 10. Remediation status (2026-10-17) — what changed, measured before and after

**Verdict after remediation: NOT READY for real-time gameplay — unchanged.** `GO WITH LIMITATIONS`
for a slow or turn-based game is now a **broader** GO: mouse input no longer needs a label on
screen, a key can genuinely be held, and the agentic loop observes the desktop instead of nothing.
The rate is still the ceiling: **8.2 frames/s** warm, ~**15–21 ms** per coordinate action, and
~**1.35 s** for a full text-bearing observation. Sections 1–9 are kept as the as-audited record;
this section is the remediation.

### 10.1 Blocker status

| # | Blocker | Status | What exists now |
|---|---|---|---|
| E1 | Observation rate | **IMPROVED, still short** | `desktop/capture.py` `PersistentScreenCapture` — ONE long-lived PowerShell host serves every frame, started lazily on the first capture, restarted once if it dies, with the historical one-process-per-frame path kept as the fallback. `_take_screenshot` reports `capture_mode`, `capture_ms`, and a three-part `freshness` (`file_written`/`recent`/`valid_png`). |
| E2 | Coordinate-addressed input | **RESOLVED** | `DesktopActionType.MOUSE_MOVE`, `MOUSE_CLICK`, `MOUSE_BUTTON_DOWN`, `MOUSE_BUTTON_UP`, `KEY_DOWN`, `KEY_UP`, `WINDOW_GEOMETRY`; runner methods, planner methods (`plan_mouse_move`/`plan_mouse_click`/`plan_mouse_button`/`plan_key`/`plan_window_geometry`), tool-catalogue declaration and permission scopes. Input is injected by the SAME DPI-aware worker that captures. |
| E3 | Bounded key hold | **RESOLVED** | `KEY_DOWN`/`KEY_UP` via `keybd_event` in the worker, through `input.InputGuard`, which records every held input and releases it on normal completion, exception, cancellation, timeout, or `stop_input()`. Measured: presses == releases (4/4) across a whole run including a stop with a key held. |
| E4 | DPI-correct coordinate space | **RESOLVED (and a bug found by measuring)** | Capture and input run in one per-monitor-DPI-aware process; geometry carries `scale`; `scale_point`/`client_to_screen` are pure and tested. The live probe caught a real defect: the worker reports the client rect as **(offset_x, offset_y, width, height)**, and `contains()` read width/height as absolute edges — so every point past `x=840` on a window whose left edge was at 440 was refused. Fixed; `client_bounds()` now computes the edges from the origin, and five tests pin a window that is **not** at (0, 0) (the fixture at the origin could never catch it). |
| E5 | Per-window geometry | **RESOLVED** | Worker commands `find <title>`, `foreground`, `at x y`, `describe <hwnd>` return `hwnd|bounds|client|pid|scale|foreground|title`, parsed into `WindowGeometry`; `validate_input_target` checks identity (handle/pid/title), client containment, and (optionally) foreground **before** any input is sent. |
| E6 | Wire the agentic observation layer | **RESOLVED** | `application.py` passes `window_probe`, `screenshot_provider` and `screen_text_reader`; `orchestrator._observe` asks for desktop layers for every task that is not a browser task; observations state `source`, the capture's provenance, an `evidence` flag and an `evidence_gap` reason. |

Two further changes came out of the same evidence: `_needles` in the orchestrator no longer counts generic words ("desktop", "application", "window") as proof a goal was reached — with real observations, `"application"` matched `ApplicationFrameHost` and a stubbed action was reported COMPLETED; and unmatched desktop phrasing is now **refused** (`status: "not_an_action"`, no token, no workflow) after the GIL fallback runs, instead of being aimed at `EXECUTE_SCRIPT` (§2's safety finding).

### 10.2 Measured — capture (same program, two process models)

Benchmark: `scripts/benchmark_screen_capture.py`, run twice in one session on this host
(2880×1800, 200 % DPI). Both arms are in the same run, so the comparison is not across sessions:

| Arm | Frames | Median latency | p95 | Sustained FPS | Fresh frames |
|---|---:|---:|---:|---:|---:|
| One process per frame (baseline) | 3 | 587.6 ms | 607.5 ms | **1.70** | 3/3 |
| Persistent worker, steady state | 11 | 121.3 ms | 129.0 ms | **8.23** | 11/11 |
| One process per frame (repeat) | 3 | 566.7 ms | 686.0 ms | **1.65** | 3/3 |
| Persistent worker, steady state (repeat) | 11 | 120.7 ms | 125.7 ms | **8.27** | 11/11 |

The first persistent frame costs one worker start (~606 ms, i.e. no better than the baseline); every
frame after it is ~120 ms. Twelve distinct SHA-256s per run — the frames are real, not a cached file.
Worker resource use (reported by the worker itself): **1 spawn**, **~150 MB working set**,
**1,562 ms CPU** for the run, and cleanup verified (`closed: true`, `pid_gone: true`).

**No target FPS is claimed beyond these measurements.** 8.2 fps is a 4.8× improvement and still
4.6× short of 30 fps; the remaining cost is GDI+ `CopyFromScreen` + PNG encode of a 5.18 MP image
(~115–120 ms), not process startup.

### 10.3 Measured — coordinate input (live, disposable window)

Probe: temp `nc_input_probe.py`, a Win32 window this session created and owned; input only sent
while `GetForegroundWindow()` was provably that window; cursor and foreground restored and verified.

| Operation | Measured | Verified by |
|---|---:|---|
| Window geometry (`find <title>`) | **841.8 / 864.2 ms** | returned `hwnd` equals the window's own handle |
| `MOUSE_MOVE` to (852, 672) | **8.5–14.1 ms** | worker's own cursor read landed exactly on target |
| `MOUSE_CLICK` left at (852, 672) | **15.0–21.2 ms** | window received `M_PRESS:1` then `M_RELEASE:1` |
| `KEY_DOWN` a → 0.30 s → `KEY_UP` a | hold **0.46 s** wall | exactly one `K_PRESS:a` during, `K_RELEASE:a` after |
| `MOUSE_BUTTON_DOWN` → 0.30 s → `UP` | hold verified | `M_PRESS:1` during, `M_RELEASE:1` after |
| `stop_input()` with `w` held | presses **4**, releases **4** | `stop_released_events` ends `K_PRESS:w`, `K_RELEASE:w` |
| Click outside the client area | **refused** | `ValueError: … outside the target window's client area (456, 422, 1264, 984)` |
| Click on the title bar | **refused** | same check, point (445, 365) vs client origin (456, 422) |
| `KEY_UP` of something not held | **refused** | `Cannot release key 'w': it is not held` (unit test) |
| Unknown key / unknown button | **refused, never guessed** | `test_pressing_an_unknown_key_refuses`, `test_an_unknown_button_refuses` |

One more measurement worth recording because it is E4's exact failure: the first version of this
probe read the cursor without `SetProcessDPIAware()` and "restored" a cursor to **half** its
pre-probe position — the same 2.0× mismatch §4 describes, reproduced from a second code path.
Declaring awareness first made the restore verify. A new coordinate path is not DPI-safe until it
has been measured on a scaled display.

### 10.4 Measured — agentic observation (same engine, wired vs not)

```
engine as the application built it before:  source="none"  application="unknown"  elements=0  evidence=false
engine wired to the application's seams:   source="window_probe+browser_dom+screen_text"
                                           7 window elements, 265 OCR words, 1846 chars
                                           evidence=true  (screenshot: "captured now: …")
```

Steady-state cost of one wired observation, measured three times in one process: **1.34 s, 1.38 s**
(the first call is **3.12 s** — worker start plus the OCR engine's first load). The halves measured
separately: capture **250.7 ms**, OCR **690.9 ms**, window probe **349.5 ms**. A first run in a cold
process measured **9.53 s**, recorded here rather than dropped: the observation is correct but its
cold-start cost is real and is what a game loop would pay on the first frame.

A browser observation is unchanged by the wiring: `include_desktop=False` pays for neither a
capture nor a window probe (`screenshot`/`window_probe` = "not requested for this observation"), and
`test_browser_observation_is_untouched_by_desktop_wiring` pins it. `AgenticObservationWiringTests`
reproduces the audit's six empty observations and asserts the desktop run now carries evidence, the
unwired engine reports an evidence gap instead of guessing, a failed probe/capture is reported
(never fabricated), and a generic word cannot complete a goal.

### 10.5 Tests and checks run for the remediation

| Group | Result |
|---|---|
| `tests/test_desktop.py` (whole file) | **74 passed, 20 subtests**, exit 0, 22.13 s |
| `tests/test_desktop.py -k "CaptureWorkerProtocol or ScreenshotEvidence"` | **12 passed** (included above; listed for the groups) |
| `tests/test_desktop_input.py` (guard, refusals, geometry, client addressing, runner dispatch) | **47 passed** |
| `tests/test_desktop_input.py` + `tests/test_agentcore.py` together | **84 passed**, exit 0 |
| `tests/test_agentcore.py` | **37 passed** |
| `tests/test_application.py -k "desktop or non_action or unified_command"` | **8 passed, 2 subtests**, exit 0, 120.57 s (includes the new refusal + real-chain tests) |
| `tests/test_application.py -k "unmatched or refusal_gate"` | **2 passed, 3 subtests**, exit 0 |
| `tests/test_route_shape_contract.py` (whole file, real HTTP + TestClient) | **18 passed, 90 subtests**, exit 0, 285.54 s |
| `POST /desktop/plan` refusal over HTTP | **passed** — 200, `not_an_action`, empty workflow, no token |
| `python -m mypy src` | **Success: no issues found in 414 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 414 source files** |
| `python scripts/generate_api_reference.py --check` | **`docs/API.md: in sync`** |

Not run here: the whole ~2,254-test suite (a single run exceeds this session's tool timeout; the
earlier audits' full-suite result stands as the broad run) and any real game. No ruff pass is
claimed — it is not installed on this host.

`tests/test_desktop_input.py` and the capture-protocol tests use a **fake worker**: they pin the
safety contract and the protocol, and they inject nothing. The live probe above is what proves the
real worker injects, and the two are complementary — the fake worker is why the client-bounds bug
needed a live window at a non-zero origin to surface.

### 10.6 What is still missing (real-time verdict)

* **Rate.** 8.2 fps warm with a 120 ms frame; a rich observation is ~1.35 s. There is no frame
timing, no scheduler, no prediction — a real-time game needs two orders of magnitude more.
* **No DXGI Desktop Duplication.** The remaining 120 ms is GDI+ capture and PNG encode; a GPU
  duplication path (or a raw framebuffer without PNG) is the next real step, not a tweak.
* **Primary display only.** `Screen.PrimaryScreen.Bounds`; a game on a second monitor still cannot
  be captured.
* **Keys are not window-validated.** Mouse actions validate identity/foreground; `KEY_DOWN` goes to
  the current foreground window. A hold survives focus changes only in the sense that it is always
  released — not that it lands in the intended app.
* **No drag action and no chords.** Down/move/up can be composed by a caller, and multiple holds
  are supported by the guard, but there is no `MOUSE_DRAG` action and no atomic multi-key chord.
* **Anti-cheat.** Injected `mouse_event`/`keybd_event` is exactly what anti-cheat systems detect.
  The evaluation target must stay offline, single-player and windowed.

**Verdict:** slow / turn-based offline evaluation is now supportable without label-addressing and
with genuine holds; real-time gameplay is **not** supportable, and the measurement says the next
limit is frame acquisition and observation cost, not the input vocabulary.

---

## 11. NF-07 remediation (2026-10-20) — the input path and the verifier exist

This section is the second remediation pass, and it closes the two blockers that the first pass left at
"no action path" and "no verifier". Sections 1–10 are kept as the as-audited record.

### 11.1 What changed

| Blocker | Status now | What exists |
|---|---|---|
| No gameplay action path (`INPUT_PATH_AVAILABLE = False`) | **RESOLVED (bounded)** | `novaforge/actions.py`: four typed action kinds (key tap, hold, click, move) under one `ActionPolicy`; a plan is a digest-bound **proposal** that injects nothing; execution requires that exact digest and revalidates the pinned handle/pid, the client geometry and the foreground immediately before injecting; every edge goes through `desktop.input.InputGuard`, so a press always has a release. |
| No objective verifier | **RESOLVED (window title only)** | `novaforge/objective.py`: the verdict is read from the OS — `verified_success` / `verified_failure` / `unknown` — never from the action record, and only the first completes a session (`end_reason = objective_verified`). |
| Action budget unenforceable | **RESOLVED** | Enforced at execution: a `denied` action never increments `actions_executed`, so a refusal cannot consume a budget it never used. |
| Guardian cannot interrupt an action | **RESOLVED** | cancel/emergency stop sets the runner's stop flag **before** taking the manager lock (a running action holds that lock for the whole injection) and the held key is released by the guard: measured **253.7 ms** into a 1500 ms hold, with nothing left held. |

### 11.2 Evidence (real OS window, real injected keys)

`scripts/novaforge_gameplay_acceptance.py` launches its own disposable window
(`scripts/forge_harness_window.ps1`) that retitles itself to `NC FORGE HARNESS - READY` **only** when it
receives a key press, so a verified success cannot be faked by the action record. 15/15 steps passed:
plan injects nothing; a wrong digest is denied; the approved digest executes one press/release
(`presses 1 / releases 1 / held_after 0`); the verifier reads `NC FORGE HARNESS - READY` from handle + pid;
the session completes; a Guardian cancel interrupts the hold and leaves nothing held; `Esc` through the same
guarded path closes the window → session `PAUSED/target_lost` and the next action refused; emergency stop is
idempotent with cleanup `3 / 3`. `scripts/novaforge_live_workflow.py` repeats the chain over HTTP (10/10),
and the panel was driven in a browser to the same `VERIFIED SUCCESS` badge with `1 / 1` releases.

### 11.3 What the hardware run found that the tests could not

1. A cancelled capture exchange left an unread reply on the worker's pipe; the next `find` read it as its own
   answer, so a just-cancelled session could not re-validate its window (`_desynced` + restart in
   `desktop/capture.py`).
2. A stop request was permanent: the guard captured its `InputStop` at construction, so one abort made every
   later action abort — including the next session's (`reset_abort` / `begin_session`).
3. Input cleanup reported lifetime totals as per-session numbers (`2 / 2` for a one-tap session), because one
   runner serves every session and its counters were never cleared between them.

Each is pinned by a test that fails when the fix is reverted.

### 11.4 Verdict — unchanged in the way that matters

The essential blockers for *injecting and verifying* a bounded action are gone, which makes a **slow or
turn-based offline environment genuinely drillable** for the first time: an action can be planned,
approved, sent, and judged by the window rather than by the log.

**Real-time gameplay is still NOT READY**, and this phase does not move it. The ceiling recorded in §10 is
unchanged — ~8.2 fps warm, ~15–21 ms per coordinate action, ~1.35 s for a rich observation — and the new
limits are explicit:

* The verifier understands **one** objective kind: a window title. A game that does not announce success in
  its title cannot be verified, and such a session reports `unknown` rather than inferring a win.
* Keys are limited to the declared gameplay set; there is still no drag, no atomic chord, and no
  window-validated key target beyond the foreground check.
* Injected `keybd_event`/`mouse_event` is exactly what anti-cheat detects, and the acceptance target was a
  disposable window — not a commercial game. No per-game policy exists.
