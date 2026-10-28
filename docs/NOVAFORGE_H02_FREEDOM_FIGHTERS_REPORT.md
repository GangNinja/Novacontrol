# NovaForge H02 — Freedom Fighters live game-view observation

**Date:** 2026-10-22
**Game:** Freedom Fighters (Steam app `1347780`, build `6153020`)
**Session:** `5c20a0f25c8d4514bfd49481aff5c9b4`
**Test:** supervised, observation-only; no gameplay input

## Verdict

| Area | Verdict |
| --- | --- |
| Phase A — game identity and target validation | **PASS** — the running Steam game, process, top-level window and handle were identified |
| Live-view mechanism — endpoint, polling and image updates | **PASS WITH LIMITATIONS** — the panel displays real captured PNGs and updates from new frame sequences; the feed is not a static screenshot |
| Displayed feed bound to the verified game for the whole session | **FAIL** — frames 12-21 are the Freebuff/NovaControl desktop; the panel displayed one of them as `LIVE` at count 15, and frame 15's displayed hash matches the desktop frame exactly |
| Five-frame capture evidence | **PASS for count** — 26 frames, all unique; **FAIL for target binding** — 10 frames are not the game |
| Freshness / stale presentation | **PASS for age-based staleness**; **FAIL for target-visible / fallback state** — the fallback frames were fresh by timestamp but were not the target |
| Zero keyboard/mouse actions | **PASS** — 0 attempted, 0 executed, 0 presses, 0 releases |
| Stop behaviour | **PASS** — the image is hidden, the blob URL removed, and the panel says "Session STOPPED — no live feed" |
| **Overall H02 live-view acceptance** | **NO-GO** — the feed is not reliably the verified game while the operator is looking at it |

The live view is genuinely implemented. The panel polls the session, fetches the frame
PNG for each new `seq`, shows captured time, age, dimensions and source, labels old
frames stale, and hides the feed when the session ends. I verified the displayed image
against the session files by SHA-256: the image at frame count 15 has the same hash as
session frame 15, and the image at count 23 has the same hash as frame 23. The capture
time, frame count and image bytes advanced. This is not a cached or static screenshot.

The acceptance fails for a different reason: **the feed was not always the game**. When
the operator brought the NovaForge/Freebuff window forward to look at the feed on this
single display, the fullscreen Freedom Fighters window was minimised. The session tick
followed the window's off-screen bounds; the crop could not find the target inside the
captured display and fell back to a full-screen frame. Frames 12-21 are the Freebuff
and NovaControl UI itself. At count 15 the panel showed that fallback with the badge
`LIVE`, the target label `Freedom Fighters`, and source `full screen`. Frame 15's OCR
literally contains
`Freebuff`, `NovoForge` and `FORGE VISION LIVE`. That is the failure the acceptance
criteria exist to prevent.

The game itself was captured correctly while it was foreground and visible: frames 1-11
show the base interior, and frames 23-26 show the pause menu/map after the game was
restored. The system can capture the game. It cannot show the game through this UI while
the same single display is showing the UI and the game is fullscreen.

---

## 1. Repository state

| Fact | Value |
| --- | --- |
| Branch | `main` |
| HEAD | `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`) |
| Working tree before H02 | 63 porcelain entries: 44 tracked modifications, 18 pre-existing untracked files/directories, and the H01 report (`docs/NOVAFORGE_H01_HITMAN_REPORT.md`) |
| Operations not performed | no reset, clean, stash, revert, checkout, commit or push |
| Source/test changes for H02 | none |
| Existing reports | H01 report preserved; `docs/NOVAFORGE_H02_FREEDOM_FIGHTERS_REPORT.md` did not exist before this run |
| New files for H02 | this report and transient evidence under `data/novaforge/h02_probe/` (ignored runtime data) |
| NF-10.1 / broad audit | not started |

No source code was modified during the initial runtime experiment. The report is the
only new Git entry attributable to H02; the pre-existing dirty tree is intact.

---

## 2. Game identity and geometry

### 2.1 Exact identity

| Field | Value |
| --- | --- |
| Executable | `E:\STEAM\steamapps\common\Freedom_Fighters\Freedom.exe` |
| Process name | `Freedom` |
| PID | `27368` |
| Window handle (HWND) | `2755170` |
| Window title | `Freedom Fighters` |
| Window class | `ZSystemClass000` |
| Steam app / build | `1347780` / `6153020` |
| File/product version | not exposed by the executable (empty version resource) |
| Process start | 22-10-2026 23:43:41 local |
| Process responding | `True` |
| Window style | visible, top-level root window, `IsIconic = false` while foreground |
| Window/client geometry | `2880x1800` at `(0, 0)`, scale `2.0` |
| Display | `2880x1800` at `120 Hz`, Intel(R) Arc(TM) Graphics |
| Foreground at validation | `True`; `GetForegroundWindow()` returned `2755170` |

The handle and PID in the session match the game. `GetWindowThreadProcessId(2755170)`
returned `27368`; the window is a top-level root window, not a launcher or child.

### 2.2 Focus and minimized geometry

At the first Phase A check the game was running but minimized:

- `IsIconic(2755170) = true`
- client area `314x50`
- window positioned around `(-32000, -32000)`
- foreground was Freebuff, not the game

The operator then focused Freedom Fighters. The live check reported `2880x1800`,
`IsIconic = false`, foreground `true`. That is the geometry validated for the session.

The same minimized pattern recurred during the run when the operator brought the
NovaForge UI forward. The capture record preserves the result: frames 12-21 have the
note **"The target window is outside the captured display; showing the full screen."**
The game was restored later; frames 23-26 are game content again.

This is the central usability conflict: on a single display, looking at the fullscreen
game and looking at NovaForge are mutually exclusive. The moment the operator views the
feed, the game is no longer foreground and no longer visible to a primary-display GDI
capture.

---

## 3. How the live view is implemented

### 3.1 Backend

`GET /novaforge/frames/{session_id}?seq=N` (`src/novacontrol/api/app.py`) serves one
captured PNG:

- `seq` selects a recorded frame; without it the newest frame is served;
- the path is confined to the session's own frame directory;
- a missing frame is a 404, never an empty image;
- the response is `image/png`, `Content-Disposition: inline`, and carries the
  application's no-cache trio.

Verified headers for frame 15:

```text
HTTP/1.1 200 OK
cache-control: no-cache, no-store, max-age=0, must-revalidate
pragma: no-cache
expires: 0
content-disposition: inline
content-type: image/png
content-length: 1480569
last-modified: Thu, 22 Oct 2026 18:22:28 GMT
etag: "689e5349de92eba9aba873de87469f99"
```

The bytes from that endpoint hash to
`c96e5e2399cba6dbdbe46ff49c19d93482081b3fbbc0dafd61d2bbad20d5b079`, exactly the file
hash and exactly the hash of the image the panel displayed at count 15.

### 3.2 Frontend

The existing Forge panel is in `src/novacontrol/web/static/index.html` and
`src/novacontrol/web/static/js/novaforge.js`:

- `#forgeFrame` is the live `<img>`; `#forgeFrameStale` is the stale overlay;
- `#forgeFrameTime`, `#forgeFrameAge`, `#forgeFrameRes`, `#forgeFrameSource` and
  `#forgeFrameNote` are the visible frame metadata;
- `#forgeFrameMode` is the `LIVE` / `stale` / `session ended` badge;
- `renderSession` polls `GET /novaforge/sessions/{id}` every `POLL_MS = 2000`;
- `loadFrame` fetches `frame.url` as a blob only when `sessionId + frame.seq` changes,
  creates an object URL and revokes the previous one;
- `frameFreshness` marks a frame stale when the backend says so, the timestamp is
  unreadable, or age exceeds `STALE_MS = 5000`;
- a terminal session (`STOPPED`, `COMPLETED`, `FAILED`) hides the image, removes its
  `src`, revokes the object URL, sets the mode to `session ended`, and shows a
  "no live feed" message with the last frame's sequence and capture time.

This is the existing architecture. No second capture subsystem was added or needed for
the live display.

**The missing state is target visibility.** The panel's live/stale decision is based on
frame age, not on whether the frame actually contains the target. A full-screen fallback
frame with `source: "screen"` is shown as `LIVE`; `sourceLabel("screen")` is
`"full screen"`. The frame note explains the fallback, but the primary badge and target
label still say live game.

---

## 4. Observation run

### 4.1 Configuration

| Setting | Value |
| --- | --- |
| Mission | `H02 - Freedom Fighters Live Observation` |
| Objective | observe Freedom Fighters through the live view; five fresh frames; no input |
| Target | `Freedom Fighters` (handle `2755170`, PID `27368`) |
| Time limit | `60` seconds, enforced |
| Action budget | not configured (`null`); no policy or closed loop |
| Decision pipeline | none attached (`pipeline.decision.available = false`) |
| Start | `18:21:53.727 UTC` (`STARTING`), `RUNNING` at `18:21:53.780` |
| Stop | operator `STOP` at `18:22:50.274`, `STOPPED` at `18:22:50.906` |
| Duration | `57.18 s` |
| Frames | `26` |
| Actions | `0 attempted / 0 executed` |
| Errors | `0` |
| Target checks | `26`; `0 losses`, `0 changes`, `0 unreadable` |

The session was stopped through the normal UI control before the 60-second limit. The
target checks recorded no losses even though the game was minimized/off-screen during
the desktop frames. That is a finding, not a success: identity (`handle`/`pid`) stayed
the same, but the target was not visible and the feed was not the game. The session did
not pause or stop itself when the desktop fallback appeared; it continued until the
operator stopped it at `57.18 s`.

### 4.2 Capture results

All 26 frame files are `2880x1800`; all 26 SHA-256 hashes are unique. The capture
metrics:

| Measure | Value |
| --- | --- |
| Frames / observations | `26 / 26` |
| Capture latency | median `342.1 ms`; min `220.5 ms`; p95 `840.3 ms`; max `930.8 ms`; mean `444.2 ms` |
| End-to-end rate | `26 / 57.18 s = 0.455 Hz` |
| Frame gaps | `11` above the `2.5 s` threshold; max gap `3963.8 ms` |
| Capture failures | `0` |
| Stale frames | `0` |

The first five frames, plus the critical fallback block and the restored-game frames:

| Seq | Captured UTC | Dimensions | Size (bytes) | Capture (ms) | Source | Content / note |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `18:21:53.808592` | `2880x1800` | 5,758,271 | 668.0 | `window_crop` | game base interior |
| 2 | `18:21:56.990298` | `2880x1800` | 5,752,723 | 499.9 | `window_crop` | game |
| 3 | `18:21:59.669791` | `2880x1800` | 5,729,839 | 533.5 | `window_crop` | game |
| 4 | `18:22:02.547397` | `2880x1800` | 5,731,025 | 502.3 | `window_crop` | game |
| 5 | `18:22:05.220963` | `2880x1800` | 5,755,246 | 509.2 | `window_crop` | game |
| 12 | `18:22:22.644292` | `2880x1800` | 1,604,622 | 288.0 | `screen` | **Freebuff/NovaControl desktop**; target outside captured display |
| 15 | `18:22:28.531919` | `2880x1800` | 1,480,569 | 298.7 | `screen` | **Freebuff/NovaControl desktop**; this exact frame was displayed as LIVE |
| 21 | `18:22:36.494815` | `2880x1800` | 1,474,829 | 287.6 | `screen` | **Freebuff/NovaControl desktop** |
| 23 | `18:22:39.255031` | `2880x1800` | 638,202 | 417.1 | `window_crop` | game pause menu; displayed as LIVE |
| 26 | `18:22:48.998010` | `2880x1800` | 1,331,280 | 295.8 | `window_crop` | game map; last frame before stop |

The source/note sequence is exactly the failure:

- frames 1-11: `window_crop`, game content;
- frames 12-21: `screen`, note **"The target window is outside the captured display;
  showing the full screen."** These frames are the desktop, not the game;
- frame 22: `window_crop`, black transition while the game was restored;
- frames 23-26: `window_crop`, game pause menu/map.

The capture layer is honest in its per-frame note. The live panel is not: it displays
those fallback frames with the `LIVE` badge and the target label `Freedom Fighters`.

### 4.3 What the panel actually displayed

I drove the real Forge panel in a browser and inspected the rendered DOM and the image
bytes, not just the saved files.

**First live state (count 4):**

- active tab `forgeLiveView`, state `RUNNING`, target `Freedom Fighters`;
- `#forgeFrame` visible, natural size `2880x1800`, blob URL;
- captured `11:52:02 PM`, age `2.6 s`, resolution `2880x1800`, source `window crop`;
- count `4`, actions `0 attempted / 0 executed`;
- the frame is a game frame (frames 1-4 are game content).

**Critical state (count 15, `11:52:28 PM`):**

- state `RUNNING`, target `Freedom Fighters`;
- `#forgeFrame` visible, natural size `2880x1800`;
- captured `11:52:28 PM`, age `384 ms`, resolution `2880x1800`, source **`full screen`**;
- mode **`LIVE`**, stale overlay hidden, target still `Freedom Fighters`;
- count `15`, actions `0 attempted / 0 executed`, duration `35.2 s`;
- displayed image SHA-256 `c96e5e...` — exactly session frame 15, which is the
  **Freebuff/NovaControl desktop**, not the game.

This is the acceptance failure in one state: the panel asserted a current, non-stale,
live view of Freedom Fighters while showing the Freebuff UI itself.

**Updated state (count 23, `11:52:39 PM`):**

- state `RUNNING`, target `Freedom Fighters`;
- captured `11:52:39 PM`, age `1.8 s`, resolution `2880x1800`, source `window crop`;
- mode `LIVE`, stale overlay hidden;
- count `23`, actions `0 attempted / 0 executed`, duration `47.3 s`;
- displayed image SHA-256 `23fb2c17...` — exactly session frame 23, the game pause menu.

The transition from `c96e...` to `23fb2c17...`, together with the advancing captured
time and count, proves the displayed image really updates from new capture timestamps.
It does not prove the image is always the game.

**After Stop:**

- state `STOPPED`, target `Freedom Fighters`;
- `#forgeFrame` hidden, `src` removed, natural size `0x0`;
- mode `session ended`, stale overlay hidden;
- empty state visible: **"Session STOPPED — no live feed. Last frame: seq 26 captured
  11:52:48 PM."**
- count `26`, actions `0 attempted / 0 executed`, duration `57.2 s`.

Stopping the session does clearly mark the feed as no longer live.

### 4.4 OCR / visible-state evidence

OCR was run after the session with the same Windows `Windows.Media.Ocr` seam used by the
application. It is a post-hoc read of the saved frames, not a live OCR step inside the
session.

| Frame | OCR result |
| --- | --- |
| 1 | 2 tokens (`oooooo`, `oooooo`) — HUD pips; no meaningful text |
| 5 | 2 tokens (`oooooo`, `oooooo`) — HUD pips; no meaningful text |
| 12 | **216 tokens of the Freebuff/NovaControl UI**, including `Freebuff`, `NovaForge`, `FORGE VISION LIVE`, `START SESSION`, `Live Session` |
| 15 | **200 tokens of the Freebuff/NovaControl UI**, including `Freebuff`, `NovoForge`, `FORGE VISION LIVE`, `LIVE`, `SOURCE full screen` |
| 21 | **199 tokens of the Freebuff/NovaControl UI**, including `SOURCE full screen`, `RESOLUTION 2880 x 1800` |
| 23 | `MENU`, `RESTART LOCATION END GAME` — game pause menu |
| 26 | map labels/visual map; no useful OCR text (encoding failed on the first print attempt; the saved frame is the game map) |

OCR over the remaining fallback frames 13, 14 and 16-20 returned 190-228 UI tokens
each; all ten frames 12-21 are the Freebuff/NovaControl desktop. The OCR is decisive.
The game frames contain almost no readable UI text, so the visible-state evidence for
the game is the image content, not OCR semantics. No objective was configured; no
game-state claim is made from OCR.

Frame 15 is the exact image the live panel displayed at count 15:

![Frame 15 — this is what the live panel displayed as LIVE; it is the Freebuff/NovaControl desktop](../data/novaforge/h02_probe/session_15_960.png)

By contrast, frame 1 is actual Freedom Fighters gameplay captured before the focus
switch:

![Frame 1 — Freedom Fighters gameplay captured while the game was foreground](../data/novaforge/h02_probe/session_01_960.png)

And frame 23 is the game after it was restored, displayed correctly as LIVE:

![Frame 23 — Freedom Fighters pause menu after the game was restored](../data/novaforge/h02_probe/session_23_960.png)

---

## 5. Foreground, fullscreen and the central limitation

The game runs fullscreen at `2880x1800` on a single display. The live view is a JPEG/PNG
frame inside the NovaForge page; to look at it, the operator must bring the browser/
Freebuff window forward. That takes the foreground from a fullscreen game; the game
minimises. This is the same behaviour that made the initial target `314x50` and
off-screen: a minimised fullscreen game has a tiny off-screen rectangle.

The capture path is GDI `CopyFromScreen` on the primary display, cropped to the target.
It captures the composited desktop, not the game's own surface. It can capture the game
while the game is foreground and visible; it cannot capture a minimised or occluded
DirectX window. When the target is minimised:

1. the tick still reads the handle/PID/title and follows the window's changed bounds;
2. the target is outside the captured display, so `_crop_to_target` keeps the full
   screen and sets `source: "screen"` with the note;
3. the session records the frame as evidence and keeps running;
4. the panel shows the desktop as `LIVE`.

That is the blocker. A live game view that requires the operator to stop looking at the
game in order to look at the view is not a live game view in the acceptance sense.

---

## 6. Input and safety

**No gameplay input was sent.** Evidence:

| Field | Value |
| --- | --- |
| `counts.actions_attempted` | `0` |
| `counts.actions_executed` | `0` |
| `counts.recovery_attempts` | `0` |
| `actions.attempted` / `actions.executed` | `0` / `0` |
| `actions.records` | `[]` |
| `pipeline.decision.available` | `false` (no planner chose an action) |
| `stages.proposed` | unavailable: "No decision pipeline proposes actions..." |
| `stages.executed` | unavailable: "No action has been sent in this session." |
| `input_cleanup.presses` / `releases` / `held_after` | `0` / `0` / `0`; `ok: true` |
| `guardian.input.allow` after stop | `false`; reason: session STOPPED |
| `guardian.cleanup.held_after` | `0` |
| `verification.outcome` | `unsupported`; no checkable objective configured |

No action budget was configured; observation-only safety came from no decision pipeline,
no policy and no proposal, not from a budget. The action path exists in the session
record (`actions.available: true`) but was never used. No plan, digest, approval or
execution record exists. No keyboard or mouse input was injected; no direct OS input API
was used.

The session permission was `novaforge.session`, medium risk, scope
`environment:control`; starting the session required confirmation. That is the
session-start permission, not a gameplay action approval.

---

## 7. Defects and proposed scoped implementation tasks

### Defect A — a minimised/off-screen target is treated as valid

`SessionRecord` follows a changed window rectangle (`_tick`: `elif resolved.bounds !=
target.bounds: record.target = resolved`) as long as the handle/PID/title still match.
It does not treat `IsIconic`, a zero/tiny client area, an off-screen position, or loss
of foreground as `target_not_visible`/`target_lost`. The capture layer then falls back
to the full screen (`_crop_to_target`: `source: "screen"` with the "outside the captured
display" note) and the session records it as an observation. Target checks reported
`0 losses`, `0 changes`, `0 unreadable` for the run even though the game was minimised
for 10 frames.

### Defect B — the live panel labels the fallback as live

`loadFrame`/`renderSession` decide `LIVE` vs `stale` from `frameFreshness` (age and the
backend `stale` flag) only. `sourceLabel("screen")` is `"full screen"`; there is no
`target_visible` / `feed_state` field, no "target not visible" overlay, and no rule that
a full-screen fallback is not a live target frame. The target label remains
`Freedom Fighters`; the badge remains `LIVE`.

### Defect C — single-display fullscreen capture cannot serve the goal

The existing GDI primary-display capture cannot show a fullscreen game while the
NovaForge UI is in front. This is a product/capture capability gap, not a polling or
cache bug. The live-view plumbing works; the capture source does not meet the use case.

### Proposed Task A — narrow safety/UI correction (do this first)

Add one explicit feed state to the existing frame/session contract and use it in the
existing panel:

- a frame/session field such as `feed_state` / `target_visible` with values
  `live_target`, `target_minimized`, `target_not_foreground`, `screen_fallback`;
- when the target is minimised, has a zero/tiny client area, is off-screen, or is not
  foreground, do not record a full-screen fallback as target evidence; pause or record
  a non-evidence observation with the reason;
- make `source: "screen"` / `screen_fallback` render as **not live** in
  `novaforge.js`: hide the image or show a "TARGET NOT VISIBLE — not live" overlay,
  not `LIVE`; keep any previous game frame labelled as a record, never current;
- keep the existing stop/terminal hide behaviour;
- add regression tests for the minimised/off-screen case and for the DOM state: no
  `LIVE` badge with a fallback frame, source/feed state visible, target checks mark the
  transition.

This is a narrow change to existing state and rendering, not a new capture subsystem.

### Proposed Task B — actual live game view while the UI is visible (separate decision)

If the product goal is to watch the game inside NovaForge while operating NovaForge on
one display, the capture source must be changed to the target's own surface (for example
Windows Graphics Capture / DXGI for the window, or a verified `PrintWindow` path where
it works for the game), or the supported setup must be a second display or a game
windowed/borderless mode that remains visible beside the UI. That is a design/capability
task, not a quick fix. It should not be smuggled into the existing GDI capture path
without verifying that the frame is still the target window and not the desktop.

The current evidence does not justify claiming either capability. It does justify a
clear "not live" state for the desktop fallback immediately.

---

## 8. Checks run and checks not run

**Run:**

- OS process/window identity, class, PID, HWND, foreground, `IsIconic`, DPI-aware
  `2880x1800` geometry;
- real Forge UI start/validate/start/stop through the browser panel;
- live DOM inspection: image natural size, blob URL, mode, stale overlay, captured
  time, age, resolution, source, count, actions, state;
- image SHA-256 over the displayed blob and the session files; exact matches for frames
  15 and 23;
- session and telemetry APIs for frames, observations, target checks, actions, input
  cleanup, Guardian state, verification;
- frame endpoint headers and content hash;
- post-hoc OCR over selected frames;
- frame hash uniqueness across all 26 frames.

**Not run:**

- `pytest` / `mypy` (no source changes were made);
- a second display or a windowed/borderless game configuration;
- an induced target-loss/capture-failure test (the session recorded 0 capture failures);
- objective verification (no checkable objective was configured; verdict `unsupported`);
- vision-model interpretation (not needed; NF-09 remains unavailable on this CPU-only
  setup);
- any gameplay input or autonomous loop.

---

## 9. What this does not show

This experiment does **not** demonstrate autonomous gameplay intelligence, planning,
policy learning, or gameplay competence. It captured one running single-player game for
57 seconds with no input. It does not show that the live view can display the game while
the operator is using NovaForge on one display: the opposite happened. The game frames
are real; the feed's binding to the game is not reliable across the session.

---

## 10. Smallest recommended next action

1. **Do not accept the current live view as a live game feed.** A full-screen fallback
   must never render as `LIVE` with the target's name.
2. Implement Task A (narrow state/UI fix) and its regression tests: minimised/off-screen/
   not-foreground target → `target_not_visible`/`screen_fallback`, not live evidence;
   hide or clearly mark the image; keep the stop behaviour.
3. Then choose Task B deliberately: target-surface capture, a second display, or a
   windowed/borderless setup that keeps the game visible beside the UI. Verify with five
   consecutive frames that are both fresh and visibly the game, while the operator is
   actually looking at the feed.
4. Re-run H02 only after those changes; the current session's 26 frames are evidence of
   the mechanism and the misbinding, not an acceptance pass.

No source code was changed, no commit or push was made, and no game input was sent.

---

## 11. Evidence index

| Evidence | Location |
| --- | --- |
| Session record | `GET /novaforge/sessions/5c20a0f25c8d4514bfd49481aff5c9b4` |
| Telemetry | `GET /novaforge/sessions/5c20a0f25c8d4514bfd49481aff5c9b4/telemetry` |
| Session snapshot | `data/novaforge/h02_probe/session_final.json` |
| All raw frames | `data/novaforge/frames/5c20a0f25c8d4514bfd49481aff5c9b4/000001.png` ... `000026.png` |
| Downscaled evidence | `data/novaforge/h02_probe/session_01_960.png`, `session_15_960.png`, `session_23_960.png`, `session_26_960.png` |
| Live-view code | `src/novacontrol/api/app.py` (`/novaforge/frames/{session_id}`), `src/novacontrol/web/static/js/novaforge.js` (`loadFrame`, `renderSession`, `frameFreshness`), `src/novacontrol/web/static/index.html` (`#forgeFrame`, `#forgeFrameStale`) |
| Capture/fallback code | `src/novacontrol/novaforge/manager.py` (`_tick`, `_crop_to_target`) |

The decisive comparison is frame 15: the displayed blob hash and the saved frame hash
are identical, the panel said `LIVE`/`Freedom Fighters`, and the image and OCR are the
Freebuff/NovaControl UI. That is the H02 blocker.
