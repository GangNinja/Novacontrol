# NovaForge H01 — first real-game feasibility test (Hitman: World of Assassination)

**Date:** 2026-10-22
**Game:** HITMAN 3 (HITMAN - World of Assassination), build 3.160.0.0
**Session:** `1fa6db12ad934d4ca87f6eaf9515fa39`
**Test type:** supervised, observation-only; no game input

## Verdict

| Area | Verdict |
| --- | --- |
| Stage 0 — target identity | **PASS** — the running game, process, top-level window and handle were identified exactly |
| Stage 0 — capture viability | **PASS WITH LIMITATIONS** — foreground capture works; a minimized/occluded DirectX window cannot be captured |
| Stage 1 — observation-only | **NO-GO** — five genuine frames and zero input, but the recorded geometry was stale (`2560x1600` while the live window/display was `1920x1200`); the "credible frame dimensions" criterion failed |
| Stage 3 — single interaction | **NOT ATTEMPTED** — blocked by the Stage 1 gate; no key or mouse action was proposed or sent |
| Overall H01 | **NO-GO for input.** The observation content is valid, but the test must not proceed to gameplay input until the geometry defect is removed and a clean observation run passes |

This is not a gameplay-intelligence result. It is an observation/capture feasibility
result with one clear blocker.

The positive evidence is real: the five frames are captures of the actual Hitman main
menu, not a stale cache. The `1920x1200` content area of every frame is pixel-identical
(RMS 0.0) to an independent `1920x1200` capture taken after the session, and Windows OCR
reads the same 31 words from both. The menu is observable. What is wrong is the
geometry bookkeeping: the session pinned `2560x1600` and the persistent capture worker
kept a `2560x1600` buffer, so every frame carries `640x400` pure-black padding.

---

## 1. Initial repository state

| Fact | Value |
| --- | --- |
| Branch | `main` |
| HEAD | `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`) |
| Working tree before H01 | dirty: 62 porcelain entries (44 tracked modifications, 18 untracked files/directories). The existing NF-07 through NF-10 work and user edits were left untouched. |
| Operations not performed | no reset, clean, stash, checkout, revert, commit or push |
| Source/test changes for H01 | none |
| New files for H01 | this report, plus transient evidence under `data/novaforge/h01_probe/` (ignored runtime data) |
| Existing reports | `docs/NOVAFORGE_H01_HITMAN_REPORT.md` did not exist before this run; no report was overwritten |
| NF-10.1 remediation | not modified or started |

The working tree was inspected before any action and again after the report was written.
The only new entry attributable to H01 is this report; the pre-existing changes are
preserved.

---

## 2. Target identity and geometry

### 2.1 Exact identity

| Field | Value |
| --- | --- |
| Executable | `G:\games\HITMAN - World of Assassination\Retail\HITMAN3.exe` |
| Process name | `HITMAN3` |
| PID | `15344` |
| Window handle (HWND) | `88869084` (`0x054C1F5C`) |
| Window title | `HITMAN 3` |
| Window class | `ZSystemClass000` |
| File/product version | `3.160.0.0` |
| Process start | 22-10-2026 22:49:54 local |
| Process responding | `True` |
| Window state at verification | visible, top-level root window, `IsIconic = false` |
| Foreground at verification | `True`; `GetForegroundWindow()` returned `88869084` |
| Window-to-process check | `GetWindowThreadProcessId(88869084)` returned PID `15344` |

The handle and PID in the NovaForge session match the live game. The window is not a
child or a launcher window: it is the top-level root (`GetAncestor(..., GA_ROOT)` equals
the handle), visible, and owned by the game process.

### 2.2 The `314x50` measurement

Stage 0 measured the game while it was **minimized**, not while the main menu was
visible:

- `IsIconic(88869084) = true`
- client area `0x0`
- window position approximately `(-32000, -32000)`
- minimized window rectangle exactly `314x50`
- foreground at that moment was `Peacock Patcher`, not Hitman

NovaForge reported a real minimised-window geometry. It was not the size of the
visible fullscreen game. Once the game was foregrounded, its window/client rectangle
became `1920x1200`. The user's suspicion about the small number was correct; the
number itself was not a capture bug.

### 2.3 The `2560x1600` session geometry

| Source | Geometry |
| --- | --- |
| Session target record (`target`) | bounds/client `[0, 0, 2560, 1600]`, scale `1.5`, foreground `true` |
| Live window geometry after the session | bounds/client `[0, 0, 1920, 1200]`, scale `1.5`, foreground `true` |
| Independent newly started capture worker | screen `1920x1200`; window `1920x1200`; foreground `1920x1200` |
| Display adapter (`Win32_VideoController`) | `1920x1200` at `120 Hz`, Intel(R) Arc(TM) Graphics |

The session was started 0.16 seconds before its first frame. Its target snapshot is
`2560x1600`; it entered RUNNING at `17:46:44.845 UTC`, and the first frame was written
at `17:46:44.902 UTC` with a non-black content area of `1920x1200`. The display mode
changed at the start of the run. The session had pinned the pre-change target geometry,
and the persistent capture worker had allocated its `2560x1600` bitmap at its own
startup. Neither was refreshed after the display settled.

Direct consequences:

- every session frame is `2560x1600`;
- the non-black content occupies exactly `(0, 0, 1920, 1200)`; the remaining
  `640x400` is pure black padding (25% of the image);
- a freshly started worker sees `1920x1200` and writes a `1920x1200` frame whose
  pixels are identical to the session frame's content area.

This is a NovaForge geometry/capture-buffer defect, not a Hitman rendering failure. The
capture itself read the correct screen; it wrote it into a stale-sized buffer, and the
session cropped/recorded that stale size. The app's target guarantee rechecks the
pinned handle and PID (target identity), but the recorded bounds are not part of that
guarantee and were not refreshed. The observation loop's crop therefore kept using
the start-of-run `2560x1600` rectangle.

**Uncertainty:** the exact sub-second moment of the display-mode change was not logged.
The inference is from the validation/first-frame timestamps and from the fact that a
new worker now reports `1920x1200`. The live final state is not uncertain.

### 2.4 Fullscreen, Alt+Tab, and the black client area

The game is running fullscreen at `1920x1200` on a `120 Hz` display. The earlier
"small window with a black client area" after Alt+Tab is consistent with the game
minimising: `IsIconic` becomes true, the client area goes to `0x0`, and the window is
moved off-screen. That is expected game behaviour, not evidence that the game rendered
black.

It is also a capture limitation. NovaForge's capture path uses GDI
`CopyFromScreen` on the primary display and crops to the target window. It captures the
composited desktop, not the game's own backbuffer. A minimised or occluded DirectX
window cannot be captured by this path. No background/minimised capture is claimed from
this experiment; the session ran with Hitman foreground throughout.

---

## 3. Stage 1 — observation-only protocol

### 3.1 Configuration

| Setting | Value |
| --- | --- |
| Mission name | `H01 - Hitman Observation` (session record) |
| Objective | observe the Hitman main menu; capture five fresh frames from the verified window; send no input |
| Success criteria | five fresh frames from the same verified target; credible dimensions; no target mismatch; zero injected actions |
| Target title | `HITMAN 3` |
| Time limit | `60` seconds, enforced |
| `max_actions` | not configured (`null`) |
| Action policy | none; no `HarnessReadyPolicy`, `HarnessStagesPolicy` or `FixedKeyPolicy` was enabled |
| Decision pipeline | none attached (`pipeline.decision.available = false`) |
| Observation loop | built-in session observation loop, interval `1.0 s` |
| Start / stop | `17:46:44.743` / `17:46:58.303 UTC` by the operator |
| Duration | `13.56 s` active; the operator stopped it, not the 60 s limit |

No action budget was configured. Observation-only safety did not rely on a budget:
there was no decision pipeline, no policy, no proposal, and the action ledger is empty.
The action path was available for explicit operator proposals, but none were made.

### 3.2 Five frames

All times are UTC. "Gap" is the time since the previous frame.

| Seq | Captured at | Gap | Dimensions | Size (bytes) | Capture (ms) | Fresh flag | SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `17:46:44.902861` | 0.159 s from start | `2560x1600` | 1,779,322 | 706.9 | `true` | `b85a9db3c0c1d8e2630378f905c155b135b732908c4ed29a7b4f6413c6c89ab8` |
| 2 | `17:46:47.993343` | 3.090 s | `2560x1600` | 1,779,322 | 517.1 | `true` | same |
| 3 | `17:46:50.591297` | 2.598 s | `2560x1600` | 1,779,322 | 520.4 | `true` | same |
| 4 | `17:46:53.143014` | 2.552 s | `2560x1600` | 1,779,322 | 519.7 | `true` | same |
| 5 | `17:46:55.717663` | 2.575 s | `2560x1600` | 1,779,322 | 506.7 | `true` | same |

Observations were recorded at `17:46:46.950`, `49.548`, `52.101`, `54.681` and
`57.264 UTC`, each summarized as a `2560x1600` frame of `'HITMAN 3'` with
`evidence: true`. The target checks recorded zero losses, zero changes and zero
unreadable windows.

### 3.3 Independently captured fresh frames

After the session, a new `PersistentScreenCapture` worker (the existing capture module,
no source changes) was started at the current display mode:

| Capture | Dimensions | Size (bytes) | Worker-reported time | SHA-256 |
| --- | --- | --- | --- | --- |
| `fresh1.png` | `1920x1200` | 2,187,250 | 453 ms | `4a61d06ad3f724dff0e883c159563dab5e444194f488e882033d9672bcf63c7f` |
| `fresh2.png` | `1920x1200` | 2,187,250 | 307 ms | same |

Pixel comparison of the top-left `1920x1200` of every session frame against
`fresh1.png`:

- RMS per channel: `[0.0, 0.0, 0.0]`
- mean absolute difference per channel: `[0.0, 0.0, 0.0]`
- difference bounding box: `None`

So the session frames are genuine captures of the current screen. The `640x400`
padding is dead space, not game content. The session's identical hashes are explained
by a static menu, not by a stale repeated frame. The app's `fresh: true` flag is
capture-time based, not content-change based; the independent comparison is what
supports the liveness claim here.

### 3.4 Frame rate

| Measure | Value |
| --- | --- |
| End-to-end observation rate | 5 frames / 13.56 s = **0.369 Hz** (~2.7 s per frame) |
| Capture latency | median `519.8 ms`; min `506.7 ms`; max `707.2 ms` |
| p95 capture latency | not available: only 5 samples; the telemetry requires 20 |
| Configured interval | `1.0 s` |
| Observed frame gaps | `2.55-3.09 s` between frame timestamps; telemetry max gap `2598.5 ms`, 4 gaps above its 2.5 s threshold |

The previously quoted ~8.2 FPS capture benchmark was not reproduced by the full
observation loop. Pure capture in the fresh probe took 307-453 ms per frame
(~2.2-3.3 FPS), also below 8.2 FPS on this configuration. The loop adds target
checks and file I/O on top of capture; it does not meet a 1 s interval. No timing
claim beyond these measurements is made.

---

## 4. OCR and visible-state evidence

The session's own observation records carried no OCR text. The app's OCR seam
(`novacontrol.desktop.vision_guide.screen_text`, Windows `Windows.Media.Ocr`) was run
after the session on two saved frames:

1. session frame `000005.png` (`2560x1600`, including padding);
2. independent `fresh1.png` (`1920x1200`).

Both returned the **same 31 words** and the same line structure. The engine exposes no
confidence value, so `confidence` is `null`; no confidence is invented. This was a
post-hoc read of saved evidence, not a live OCR step inside the observation session.

Cleaned OCR text (minor recognition noise retained):

```text
PAGE
a CAMPAIGNS
TRAINING
PROLOGUE
[control character] Sei
ENTER
PAGE -
DOWN
GAME MODES
STORY
HITMAN
CAREER
STORY
HITMAN 2
STORE
STORY
HITMAN 3
OPTIONS
STORY
SIDE MISSIONS
Player
ONLINE- LEVEL 26
```

The stable, meaningful labels are `CAMPAIGNS`, `GAME MODES`, `CAREER`, `STORE`,
`OPTIONS`, `TRAINING / PROLOGUE`, `STORY / HITMAN`, `HITMAN 2`, `HITMAN 3`,
`SIDE MISSIONS`, and `ONLINE - LEVEL 26`. The small "1" beside `PAGE` was misread as
`a`, and `Select` was misread as `Sei`; those are minor OCR errors, not a different
screen. Visual inspection of the saved frames confirms the Hitman 3 main menu with the
same labels. The `ONLINE - LEVEL 26` line is visible in the menu; no online,
account-affecting or multiplayer action was taken.

The menu was consistently observable across the five session frames and the later
independent capture: the frames are byte-identical, and the OCR text is identical.
This establishes a stable main menu, not semantic understanding of game state.

---

## 5. Input actions executed

**Zero.** No keyboard, mouse, button or text input was sent by NovaForge or by this
experiment.

Evidence from the session record:

| Field | Value |
| --- | --- |
| `counts.actions_attempted` | `0` |
| `counts.actions_executed` | `0` |
| `counts.recovery_attempts` | `0` |
| `actions.available` | `true` (the path exists) |
| `actions.attempted` / `actions.executed` | `0` / `0` |
| `actions.records` | `[]` |
| `pipeline.decision.available` | `false` (no planner chose an action) |
| `stages.proposed` | unavailable: "No action has been proposed for this session." |
| `stages.executed` | unavailable: "No action has been sent in this session." |
| `input_cleanup.presses` / `releases` / `held_after` | `0` / `0` / `0`; `ok: true` |
| `guardian.input.allow` at stop | `false`; reason: session STOPPED |
| `guardian.cleanup.held_after` | `0` |
| `interventions` | one entry: operator `stop` at `17:46:57.308`, outcome `stopped` |
| `verification.outcome` | `unsupported`; no checkable objective was configured |

The action path's availability is not evidence of input. No plan, digest, approval or
execution record exists for this session. The only operator intervention was Stop.
No direct OS input API was used.

The session permission was `novaforge.session`, medium risk, scope
`environment:control`; starting the session required confirmation. That is the
session-start permission, not a gameplay action approval.

---

## 6. Stage 3 — single interaction

**Not attempted; blocked.** No key or mouse action was proposed, planned, approved,
digested or executed. The Stage 1 gate failed on the "credible frame dimensions"
requirement, so the precondition for even proposing a reversible interaction was not
met. The OCR labels alone are not sufficient authorization to press a key, and this
report does not treat them as such. No mission was launched.

Because Stage 3 did not run, there is no `plan_action` record, no digest, no Guardian
decision and no post-action verifier verdict to report.

---

## 7. Limitations and blockers

### Blockers

1. **Stale session/capture geometry.** The session pinned `2560x1600` and the
   persistent capture worker kept a `2560x1600` buffer after the display settled at
   `1920x1200`. The frames are valid in content but not credible in dimensions
   (`640x400` black padding, 25% of the image). This is the reason for the NO-GO.
2. **No fresh geometry on display-mode change.** The target guarantee rechecks
   handle/PID identity, not bounds; the capture worker allocates its bitmap at startup.
   A display-mode transition between target validation and the first frame is enough
   to produce this state.

### Limitations

- Capture is primary-display GDI only; it cannot capture a minimised or occluded
  DirectX window. Foreground capture works; background capture is not demonstrated.
- The main menu is static in the captured pixels, so all five frames have the same
  hash. The app's freshness flag is timestamp-based; independent capture comparison
  was needed to show the frames were not a replay.
- The full observation loop ran at `0.369 Hz`, not the earlier ~8.2 FPS capture
  benchmark. The capture alone took 307-453 ms per call in the fresh probe.
- No machine-verifiable objective was configured (`verification.outcome =
  unsupported`). The observation result is human/OCR-evaluated, not a verifier pass.
- OCR confidence is not reported by the Windows engine. No vision model was used;
  NF-09's live model inference remains unavailable on this CPU-only setup.
- The exact sub-second display transition was not logged; the final state is
  established by live geometry and a fresh capture worker.

### What this does not show

This experiment does **not** demonstrate general autonomous gameplay intelligence,
planning, policy learning, reliable change detection, or safe gameplay input. It
observed one static main menu for 13.56 seconds with no action. The installed
`HarnessReadyPolicy` was reported as available in `/novaforge/capabilities`, but it was
never enabled or run; the session had no decision pipeline. The OCR labels are text
evidence, not an understanding of the game's state or objectives.

---

## 8. Smallest recommended next action

1. Confirm Hitman is foreground at the main menu and the display is `1920x1200`.
2. Restart NovaControl (or otherwise reinitialize the persistent capture worker) so its
   screen buffer is allocated at the current display size.
3. Re-run the observation-only H01 protocol, requiring:
   - five frames with `1920x1200` dimensions and no black padding;
   - the same handle/PID/title on every check;
   - OCR labels matching the main menu;
   - zero actions.
4. Only if that clean run passes, consider a single reversible interaction with the
   operator's explicit approval. No input should be sent before then.

If the stale geometry returns after a restart, the defect is in the capture worker's
cached screen bounds and/or the target-bounds refresh on display-mode change. That is a
focused NovaForge fix, not part of this observation run, and it must not be worked
around by weakening the dimension, freshness, target or Guardian checks.

---

## 9. Evidence index

| Evidence | Location |
| --- | --- |
| Session record | `GET /novaforge/sessions/1fa6db12ad934d4ca87f6eaf9515fa39` |
| Telemetry | `GET /novaforge/sessions/1fa6db12ad934d4ca87f6eaf9515fa39/telemetry` |
| Capabilities | `GET /novaforge/capabilities` |
| Five raw frames | `data/novaforge/frames/1fa6db12ad934d4ca87f6eaf9515fa39/000001.png` ... `000005.png` |
| Fresh independent captures | `data/novaforge/h01_probe/fresh1.png`, `fresh2.png` |
| Downscaled comparison images | `data/novaforge/h01_probe/session_crop_960.png`, `fresh_960.png` |

The session frame's content area and the independent fresh capture are the same image:

![Session frame content area, 1920x1200 crop of frame 5](../data/novaforge/h01_probe/session_crop_960.png)

![Independent fresh 1920x1200 capture after the session](../data/novaforge/h01_probe/fresh_960.png)

The two images are pixel-identical in the compared `1920x1200` region. The session
frame's full `2560x1600` canvas is not reproduced here because the extra `640x400` is
pure black padding.

**No source code was changed, no tests were run as part of H01, and no game input was
sent.**
