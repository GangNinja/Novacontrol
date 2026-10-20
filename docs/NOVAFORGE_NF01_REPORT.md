# NovaForge NF-01 — Gameplay Workspace & Session Foundation

**Date:** 2026-10-17
**Status:** Implemented, tested, and exercised against a live disposable window on this host.
**Verdict:** NF-01 does what it claims — it **observes** one validated window and records the truth about
that observation. It does **not** play, decide, send input, or verify an objective, and the UI says so
wherever a later phase's capability would otherwise be implied.

NovaForge is a subsystem of NovaControl, not a second brain. NF-01 adds no capture stack, no input
path, no permission model and no event bus: it composes the existing desktop capture worker, window
geometry probe, `InputGuard` release, `PermissionManager`, `JsonStateStore` and `EventBus`.

## What shipped

### 1. Session model and state machine (`novaforge/models.py`, 521 lines)

* `MissionConfig` — name, objective, target title, success criteria, optional time limit (1–3600 s)
  and action budget (1–10 000). Every field is validated with a stated reason; a blank mission is
  refused, not defaulted.
* `TargetWindow` — the window **identity** pinned at start: title, handle, pid, physical bounds,
  client rect and DPI scale. Identity is stored so a session can tell "the window closed" apart from
  "another window took the same title".
* `SessionState` — nine states: `READY → STARTING → RUNNING ⇄ PAUSING/PAUSED → STOPPING → STOPPED`,
  plus `COMPLETED` and `FAILED`.
* `SESSION_TRANSITIONS` is the one legal-move table, and `SessionRecord.transition()` **raises**
  `SessionTransitionError` on an illegal move instead of overwriting the state. `COMPLETED` exists
  but NF-01 never reaches it on its own: completion means the objective was verified, and no
  verifier is attached to this phase. The time limit ends as `STOPPED` with `end_reason: time_limit`
  and records the sentence "the objective was NOT verified". An action budget is stored and reported
  as **not enforced** (`limits.max_actions_enforced: false`), because NF-01 sends no actions for it to
  bound.
* `FrameRecord`, `ObservationRecord`, `StageRecord` keep the four pipeline stages separate. The
  panel renders `Observed` from real observations and prints "Not available in NF-01" for
  `Proposed`, `Executed` and `Verified` rather than borrowing a neighbour's status.

### 2. The observation loop (`novaforge/manager.py`, 1 003 lines)

One `asyncio` task per session, pinned to the validated target:

1. resolve window geometry; if the probe returns nothing → pause with `target_lost`;
2. if the resolved handle or pid differs from the pinned target → pause with `target_changed`;
3. if the window moved or resized, follow it and record the new bounds;
4. capture through the persistent PowerShell worker the desktop layer already owns;
5. refuse to record a frame whose capture did not report `fresh` — the observation says "nothing is
   shown as live" and carries `evidence=false`;
6. crop the physical frame to the target window and record width, height, bytes, capture mode and
   latency;
7. append a `FrameRecord` and an `ObservationRecord`, prune the frame directory to 30 PNGs, persist,
   and publish `novaforge.frame.captured`.

`_tick` is also the safety path: a capture that raises is recorded as an observation with
`evidence=false` and does **not** kill the session, while an unexpected loop failure finalises the
session as `FAILED`. `_finalize` runs exactly once for stop, limit, error and cancellation alike and
releases held input through the injected `release_inputs` seam (the same `InputGuard` release a
desktop command uses), recording `presses`, `releases` and `held_after`.

Pause is two-phase on purpose: the API moves `RUNNING → PAUSING` and waits for the **loop** to
acknowledge `PAUSED` (`pause_timeout` 3 s), so "the UI asked" and "the loop stopped" are different
facts. Stop asks the loop to finish and waits up to 8 s; only a loop that misses that deadline is cancelled,
with the timeout recorded as an error. Resume re-validates the target identity (handle and pid)
before returning to `RUNNING`, and follows a window that moved or resized.

### 3. Persistence, restart honesty and permission

* Sessions persist in the `novaforge.session` namespace of the shared `JsonStateStore`; the newest
  40 records are kept and the newest 30 frames per session stay on disk.
* On boot, a record persisted mid-run is **reconciled**, not restored as `RUNNING`: it is closed as
  `STOPPED` with `end_reason: application_restarted`, an explanatory error, and an input-cleanup
  record that says no input is held by a process that has exited. A stale "RUNNING" the UI would
  trust is the failure this prevents.
* Starting asks the single `PermissionManager` for `novaforge.session` (`action="start_session"`,
  declared MEDIUM, no confirmation because the session sends no input). The decision, its source and
  its reason are stored in the session record, so "who allowed this" is answerable later. An
  unusable gate is recorded as a refusal, never swallowed into "allowed".
* Frame serving is confined: `frame_path()` resolves the file and requires it to be inside the
  NovaForge frame root, so a tampered record cannot make the server read an arbitrary path.

### 4. API surface (12 routes, all authenticated)

`GET /novaforge/status`, `GET /novaforge/windows`, `GET|POST /novaforge/mission`,
`POST /novaforge/validate`, `GET|POST /novaforge/sessions`, `GET /novaforge/sessions/{id}`,
`POST /novaforge/sessions/{id}/pause|resume|stop`, `GET /novaforge/frames/{id}?seq=`.

Registered in all five required places: the handler in `api/app.py`, the `ApiSurface` row in
`api/models.py`, the consumer row in `api/route_consumers.py` (NovaForge's panel polls these, so
they are `no-render` consumers of `js/novaforge.js`, not mis-routed through a chat renderer),
`docs/API.md` regenerated (`--check` reports "in sync"), and tests that assert the agreement.

`/novaforge/windows` separates "the OS could not be read" (`available: false` plus a reason) from
"nothing is open" — an empty list that looks like a desktop with no windows is not an answer.
`/novaforge/frames/{id}` serves the PNG as `image/png` with the never-cache trio, and a missing
session/frame is a 404, never a different frame.

### 5. Panel — "Forge Mission Control" (`js/novaforge.js`, 841 lines)

One sidebar tab (`novaforgePanel`) with three working views:

* **Mission Control** — mission fields, live target picker from `/novaforge/windows`, Save, Validate
  (validates the mission *and* the live target without starting anything), field-level errors,
  readiness card with the phase's limitations, and the roadmap.
* **Live Session** — Forge Vision (the real frame, fetched with `authHeaders()` and decoded to a
  blob URL; no baked `src=` because an `<img>` cannot send the auth header), freshness age,
  resolution, source label, a `STALE FRAME — not live` overlay, the four pipeline stages, telemetry
  (frames, capture ms, duration, actions attempted/executed) and a session summary including errors,
  cleanup, and the verification limit.
* **Session History** — persisted records, expandable, never re-run.

Polling is 2 s, bounded, paused while `document.hidden`, and stops for terminal sessions. Five
further sidebar entries (Telemetry NF-02, Evidence NF-03, Benchmarks NF-04, Evaluation NF-05, Forge
Coach NF-06) are **disabled** buttons labelled with the phase that will implement them, pinned by a
UI test so a planned destination cannot quietly become clickable.

## Live exercise (this host, 2026-10-17)

A disposable Tk window (`NC FORGE TEST - disposable`, a ticking `TICK n` counter) was created for the
session; no real game was launched and no online service was touched.

| Observation | Result |
|---|---|
| Session | `054950af…`, 14:17:48 → 14:23:19 UTC (331.66 s), `STOPPED`, `end_reason: user` |
| Frames | 79 captured, the newest 30 retained (seq 50–79); every one `fresh`, `source: window_crop` |
| Frame geometry | 1426 × 626, cropped from the captured display to the window |
| Capture latency | across the 30 retained frames: min 126.9 ms, median 135.6 ms, p95 144.3 ms, max 170.8 ms (`persistent` mode) |
| Live feed | Forge Vision showed genuine frames whose `TICK` counter advanced (e.g. `TICK 1109`) |
| Pause | Frame counter froze; state went `PAUSING → PAUSED` and stayed there for 3 min 11 s |
| Resume | Counter advanced again on the next frames; no stale frame was presented as live |
| Target killed mid-session | Auto-paused within one tick with `target_lost`, error "the session paused because its target window closed", and the UI showed a paused session rather than a frozen "live" feed |
| Stop | State `STOPPED`; recorded cleanup `presses 0 / releases 0 / held_after 0`, `ok: true` |
| Permission | `declared`, MEDIUM — the declaration decided, not the request |

The frame counter is the point of the exercise: a still image proves a file exists, while an
advancing counter inside the cropped window proves the frame came from *that* window and is current.
Pause, resume and target loss were each checked against the counter, not against a status label.

## Defect found during the live exercise, and fixed

The crop path clipped a window that extended past the captured display and still reported an
ordinary `window_crop` frame with an empty note — a partial frame that looked complete. `_crop_to_target`
now detects bounds outside the captured display and returns the note "The target window extends
beyond the captured display; this frame is clipped." A window entirely outside the display keeps the
full-screen frame and says so, and a missing Pillow keeps the full frame rather than fabricating a
window-sized one. Pinned by `test_partially_offscreen_window_is_cropped_and_flagged` and
`test_offscreen_window_keeps_the_full_frame_and_says_so`. The frames stored during the live session
above predate the change; no claim is made that they were or were not clipped.

A second gap was closed before the live run: Start re-validated the target by title only, so a
replacement window with the same title would have been accepted. The validated handle and pid are
now pinned through start and checked on every tick and resume.

## Verification pass — 2026-10-17

NF-01 was re-audited against its stated objectives after the live exercise. The audit exercised the
probe seams with raising doubles, drove a paused session past its limit, read the persisted snapshot
back while the application believed otherwise, and checked the panel's decisions in Node. **Eleven
defects were found and fixed** (nine findings; the probe and panel findings each covered more than one
symptom), each with a regression test (the paused-limit test was also
mutation-checked: removing the fix makes it fail, restoring it passes).

### Objective checklist

| Objective | Verdict | Evidence |
|---|---|---|
| One validated target, not a title guess | PASS | Ambiguous titles refused; handle/pid pinned through start, checked every tick and on resume |
| Mission configuration surface | PASS | Fields validated with reasons; draft persisted; save reports whether the store kept it |
| Real start/pause/resume/stop lifecycle | PASS | Pause acknowledged by the loop; stop releases input once; target loss/unreadable pauses; time limit enforced even while paused |
| Persisted history and restart honesty | PASS | Store survives restart; a RUNNING snapshot is reconciled to STOPPED and written back |
| Observation provenance | PASS | Freshness required; capture errors recorded as no-evidence; crop/clipping notes |
| Explicit non-capabilities | PASS | Proposed/Executed/Verified unavailable; completion unreachable; no input path |
| API, consumers and docs | PASS | 12 routes in all five places, `docs/API.md` in sync, contract tests green |
| Panel never shows a non-live frame as live | PASS | Stale overlay, terminal sessions hide the feed, overlapping fetches ordered by request token |

### Findings fixed in this pass

1. **A raising window probe returned HTTP 500** from `/novaforge/windows` and `/novaforge/validate`
   instead of the documented "unavailable, with a reason". Both now report the exception as the
   reason/field error (`test_a_raising_window_probe_is_reported_not_raised`).
2. **A raising geometry probe returned 500** on validate/start and killed a running session as
   `FAILED`. Validation now carries a reason; mid-session the loop pauses with `target_unreadable`
   and `resume` refuses with an explanation until the probe works again
   (`test_a_raising_geometry_probe_is_a_validation_reason`,
   `test_an_unreadable_target_pauses_instead_of_failing_the_session`).
3. **The picker hid a second window with the same title while validation refused the mission as
   ambiguous** (measured: `/windows` listed one entry, validate said "2 windows match"). Different
   processes with the same title are now both listed, so the picker cannot promise a target that
   validation rejects (`test_duplicate_titles_are_all_listed_and_refused`).
4. **The time limit was ignored while paused** (measured: a 1 s mission stayed `PAUSED` after 1.4 s).
   The paused loop now checks the limit and ends `STOPPED/time_limit`
   (`test_time_limit_is_enforced_while_paused`).
5. **"Action budget" was offered as a limit that could never apply** (measured: `max_actions=1`, 16
   frames, `RUNNING`, 0 actions). The record now publishes `limits.max_actions_enforced: false`; the
   field stays stored for NF-02 and the panel says it cannot end an NF-01 session
   (`test_action_budget_is_reported_as_not_enforced`).
6. **A mission save claimed success when the store dropped the write** (measured: response `valid:
   true` with an empty read-back). `/novaforge/mission` now returns `persisted` measured by reading
   the snapshot back, and the panel says "NOT saved" instead of "saved"
   (`test_a_dropped_mission_snapshot_is_not_reported_as_saved`, UI `saveOutcome` tests).
7. **Restart reconciliation existed only in memory** (measured: API said `STOPPED`, the file still
   said `RUNNING`). The reconciled record is now written back
   (`test_a_record_reconciled_on_restart_is_written_back`).
8. **Panel falsehoods:** a running session with no frame yet said "No session is active"; an older
   overlapping frame response could paint over a newer one; and the object URL was not revoked when
   a session ended. Fixed and pinned by the `visionEmptyText`/`frameStillCurrent` tests.
9. **A docstring claimed a refused transition still writes a history entry**; it does not. Corrected.

### Remaining issues and unverified requirements

* The **action budget is stored, not enforced** — by design in an observe-only phase, now stated in
  the record and the panel rather than implied.
* `status().windows_available` is a **capture-capability claim**, not a live probe result; the probe
  itself is reported by `/novaforge/windows`.
* The UI fixes were verified with the Node logic harness, `node --check`, and the HTTP tests, not by
  replaying a browser race this pass. A live reload check of the panel is still advisable before
  release.
* The frame **stale threshold is a fixed 5 s** in both backend and panel while the loop interval is
  1 s; a future slower cadence would need the threshold derived from it.
* Nothing outside NF-01's scope was implemented: no OCR, decision pipeline, input, or verifier. The
  full suite was not run.

## Checks run

| Check | Result |
|---|---|
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_ui.py -q` | **72 passed**, exit 0, 234.02 s (61 before this pass) |
| `pytest tests/test_web_platform.py -k "NovaForge or Contract"` + `tests/test_route_shape_contract.py` | 32 passed / 320 subtests; 18 passed / 90 subtests; both exit 0 |
| `mypy src` | Success, 417 source files, exit 0 |
| `scripts/generate_api_reference.py --check` | `docs/API.md: in sync`, exit 0 |
| `node --check src/novacontrol/web/static/js/novaforge.js` | exit 0 |
| Red-green mutation | removing the paused-limit check fails the test; restoring it passes |
| Live session (earlier pass) | 79 frames, pause/resume/target-loss/stop as tabled above |

## What NF-01 deliberately does not do

* **No decision pipeline, no gameplay input, no objective verification.** `Proposed`, `Executed` and
  `Verified` are permanently "Not available in NF-01" in the panel. The action counters exist and
  stay at zero because nothing in this phase can move them.
* **No game launch.** The target must already be open; NovaForge never starts a process.
* **Primary display only.** Capture is the existing desktop worker's; a window on another monitor is
  not captured, and the crop now says when it is partial.
* **No OCR, no element model, no scene understanding.** NF-01 records a frame and its provenance;
  understanding the frame is a later phase, and the frame record does not pretend otherwise.
* **No benchmarking, evaluation metrics, replay, or Guardian/telemetry aggregation.** Those are
  NF-02 onward, and their sidebar entries are disabled rather than stubbed.
* **NF-01's status card never reports windows as available when capture is not.** On a platform
  without capture, `/novaforge/windows` returns `available: false` with the reason, and Start cannot
  be reached through a mission that validates.
* A paused session still holds its record and its task; it is not a background recorder, and it does
  not keep capturing while paused. A configured **time limit still ends it**, because the limit is a
  bound on the session's life, not only on its active time.

## Next: NF-02

The honest next step is telemetry and Guardian over the records this phase now writes: per-frame
capture distribution, session-level continuity (gaps, pauses, target losses), and the boundary
between "observed" and "acted" as a first-class fact. A decision/action phase (NF-03+) must attach to
the same session record and the same permission layer; nothing in NF-01 needs to be replaced to make
room for it.
