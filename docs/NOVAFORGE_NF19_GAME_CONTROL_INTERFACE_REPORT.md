# NovaForge NF-19 — Game Control Interface

**Phase:** NF-19 — a typed, bounded, cancellable and observable input interface.
**Verdict:** **PASS WITH LIMITATIONS** (see §14)
**Effect verification:** **NOT IMPLEMENTED — BY DESIGN** (NF-21 owns it)
**Live verification:** **PASS** on a disposable Windows harness window (20/20 checks, run 5; see §11)

NF-19 lets NovaForge issue ONE typed input operation at a time to the window a
session is pinned to, through the session's existing `InputGuard`. It does not
choose actions, chain them, or claim that a game reacted. The phase's five
states are kept separate on every result:

| Requested | Accepted | Dispatched | Operation completed | Effect observed / verified |
|---|---|---|---|---|
| `ControlAction` | live `active.status = waiting` | `dispatched_at` set, `DISPATCHED` | `COMPLETED` | **never claimed**: `effect: null`, `effect_verified: false` |

---

## 1. Initial repository state

- Branch `main`, 14 commits at start; the working tree already carried the
  uncommitted NF-01…NF-18 work (sessions, observation, state, objectives,
  knowledge, panel, reports). Nothing was committed, pushed, reset or stashed.
- NF-18's report, the NF-18 tests and its evaluation script were present and
  passing before NF-19 began.
- One **untracked** pre-existing file, `src/novacontrol/gui/game_host.py`, is not
  part of this phase and is not modified here; it accounts for the only two
  `mypy src` errors (§10).

## 2. Dependency status

| Dependency | State used here |
|---|---|
| NF-15 observation | PASS report present; the observer validates target identity (handle + pid + process start time every tick) and pauses on loss/replacement. |
| NF-16 state | PASS; only relied on indirectly (target identity / session liveness), never for input. |
| NF-17 objectives | PASS; NF-19 deliberately does **not** call the verifier after a control action. |
| NF-18 knowledge | PASS; untouched by the control path. |
| NF-07/08 action path | Digest-bound plan → approve → `ActionRunner` → `InputGuard`; reused, not forked. |
| J.A.R.V.I.S. desktop input | `LocalDesktopRunner.input_worker()` → `PersistentScreenCapture.send_input`; the same injector `application.py` wires into NovaForge. |

No dependency was missing for keyboard/mouse control. Controller input would
need a new injector and was left out (allowed by the phase).

## 3. Gap analysis (before code changed)

1. The NF-07 path could send a `key`/`hold`/`click`/`move` **proposal**, but there
   was no reusable request vocabulary with separate press and release edges, no
   per-request deadline, no cancel handle for one in-flight operation, and no
   status surface describing held inputs or refusals.
2. `ActionRunner.run` is proposal-shaped (digest, budget, verification); nothing
   exposed the runner as a reusable control interface for later phases.
3. `InputGuard` already tracks presses/releases and releases on every exit path,
   so a second injector would have been a duplicate and a safety regression.
4. `guardian.input_safety_decision` already answers "may this session send input
   now" (RUNNING, target validated for this run, input path wired, budget). It
   had no caller besides the NF-07/08 path.
5. Session records had no place for a control action result; the action record
   list and the budget counters are reused rather than duplicated.

## 4. Files inspected and modified

**Inspected (representative):** `novaforge/manager.py`, `actions.py`,
`guardian.py`, `models.py`, `observer.py`, `objective_service.py`,
`desktop/input.py`, `desktop/controller.py`, `desktop/capture.py`,
`application.py`, `api/app.py`, `api/models.py`, `api/route_consumers.py`,
`web/static/index.html`, `web/static/js/novaforge.js`, `scripts/forge_harness_window.ps1`,
`scripts/novaforge_gameplay_acceptance.py`, and the NF-12…NF-18 reports. The
NF-13A embedded host (`gui/game_host.py`, untracked) was inspected and is not
used: control addresses an OS window through the existing injector, so the host
adds no input path and no second embedding requirement.

**Created:**

| File | Purpose |
|---|---|
| `src/novacontrol/novaforge/control.py` | `ControlKind`, `ControlTarget`, `ControlAction`, `ControlPolicy`, `ControlResult`, `ControlStatus`, `GameControlService` — the ONE control implementation. |
| `tests/test_novaforge_control.py` | 42 model/service/manager tests + 7 HTTP tests. |
| `scripts/novaforge_nf19_control_acceptance.py` | Live disposable-window acceptance with the app's resource gate. |
| `docs/NOVAFORGE_NF19_GAME_CONTROL_INTERFACE_REPORT.md` | This report. |

**Modified:**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/manager.py` | Builds the control service over `action_runner`; four public methods; `game_control` capability row; target-loss refusals pause the session; session finalize cancels and waits for an in-flight action, then adopts the cleanup. |
| `src/novacontrol/api/app.py` | `GET/POST /novaforge/sessions/{id}/control`, `POST …/control/cancel`, `POST …/control/release`. |
| `src/novacontrol/api/models.py` | Four `ApiRoute` rows. |
| `src/novacontrol/api/route_consumers.py` | Four `no-render` consumer declarations. |
| `src/novacontrol/web/static/index.html` | Control card (`Control Interface`) with kind/key/point fields, Send/Cancel/Release, result log. |
| `src/novacontrol/web/static/js/novaforge.js` | Control card rendering, typed request builder, bounded poll, three buttons; pure `inputControlState` / `controlPayloadFor` exported for tests. |
| `docs/API.md` | Regenerated (`350 routes`). |
| `tests/test_novaforge_ui.py` | Five NF-19 panel tests (Node-driven pure logic + static contract). |

No new database table, service, package or dependency was introduced.

## 5. Action model and supported inputs

`ControlAction` is frozen and validated before dispatch:

| Kind | Required | Bounds |
|---|---|---|
| `key_press` | `key` in the policy set | — |
| `key_release` | `key` | the key must be held by THIS interface |
| `key_hold` | `key`, `duration_ms` | 1…`max_key_hold_ms` (default 1500) |
| `mouse_move` | `x`, `y` absolute client points | inside the target's client area |
| `mouse_press` / `mouse_release` / `mouse_hold` | button (default `left`) | policy buttons; hold 1…`max_mouse_hold_ms` (800) |
| `wait` | `duration_ms` | 1…2000 |

Every action also carries `session_id`, a pinned `ControlTarget`
(session/title/handle/pid/process/start_time), optional `deadline_ms` (default
4000, max 30000), `note`, `created_at`, `action_id`. Unknown kinds are refused
(`ValueError` → HTTP 422), not defaulted. Chords, text entry, scrolling,
dragging and controller input are **not** approximated by sequences.

Policy validation refuses a policy whose deadline is shorter than its longest
permitted duration, an unknown key, an unknown button, an empty allowed set, or
out-of-range limits — so a "permitted" action can never be impossible to finish.

Default policy: keys are the NF-07 gameplay set (`DEFAULT_ALLOWED_KEYS`; no Alt,
F4, Win, etc.), left button only, key hold ≤ 1500 ms, mouse hold ≤ 800 ms, wait
≤ 2000 ms, deadline 4000 ms, foreground required, 50 results kept.

## 6. Existing desktop automation reused

- **One injector:** `GameControlService.runner` is the manager's `ActionRunner`,
  which wraps `ScreenInput(send=PersistentScreenCapture.send_input)` — the same
  DPI-aware worker that captures frames. No `SendInput` call exists in
  `control.py`.
- **One guard:** all edges go through `InputGuard` (`_held`, `presses`,
  `releases`, `release_failures`), so a press from either the NF-07 path or the
  control path is tracked and released by the same object.
- **One wait:** `wait_bounded(ms, abort)` (shared with NF-07) slices the sleep so
  cancel is observed at 20 ms checkpoints.
- **One gate:** `guardian.input_safety_decision(record)` — not a second risk
  table.
- **One session/budget:** results are recorded in `record.action_records`, and a
  dispatched control action consumes `record.actions_attempted` /
  `actions_executed`, so the per-session action budget bounds both paths.
- **One lock:** `control_execute` takes the manager's own lock, so a control
  action and a digest-bound proposal cannot interleave injection.

## 7. Target validation (immediately before dispatch)

Order inside `GameControlService.execute`:

1. Service stopped? → `service_stopped`.
2. Parameters valid against the policy? → `policy`.
3. Runner wired? → `unsupported`.
4. Another action in flight? → `busy` (never queued).
5. Guardian gate (`RUNNING`, target validated for this run, input path wired,
   budget room) → its state, e.g. `closed`, `held`, `pending`, `budget_exhausted`.
6. Re-read the pinned window by HANDLE (`objective_probe`): unreadable → `target_lost`.
7. Handle and pid must equal the pinned identity → `target_changed` (a retitled
   window is NOT changed; NF-07's lesson is preserved).
8. Minimized / invisible → `target_minimized` / `target_hidden`.
9. Foreground required and not foreground → `not_foreground`.
10. A point outside the target's client area → `outside_target`.
11. Press of an already-held key/button → `already_held`; release of one not held
    → `not_held`.
12. Only then: `started_at`, `RUNNING`, dispatch under `asyncio.timeout`.

Refusals in steps 1–11 set `dispatched_at = ""` and send nothing. Live checks
6–7 are pinned by tests (`target_lost`, handle mismatch, pid mismatch); the
process **start time** is validated by the observation loop every tick (a reused
pid pauses the session), which is what keeps the gate's `target_validated` true.

## 8. Timeouts, cancellation and cleanup

- The whole operation runs under `asyncio.timeout(deadline_ms)`. A timeout
  becomes `TIMED_OUT`, then every service-held input is released; a release that
  fails turns the result into `CLEANUP_FAILED` with the failure text.
- `cancel()` sets the runner's one-shot `InputStop`; `wait_bounded` and each
  press edge check it. The result is `CANCELLED` (or `CLEANUP_FAILED`), all
  inputs the service holds are released, and the stop is re-armed in `finally`
  so the next action can run.
- `release()` releases exactly what the guard holds and is idempotent; it
  refuses while an action is in flight (cancel first) instead of making the
  in-flight release fail.
- `shutdown()` refuses new actions and releases held input; safe to repeat.
- Closing a session calls `control.cancel("session_closed")`, waits up to 2 s
  for the action to unwind, and only then runs the session's own cleanup, which
  adopts the result. No key is released underneath an action that is still
  finishing.
- Cleanup reports `released`, `held_after`, `failed[]`, and whether the failure
  belongs to THIS action (`release_failures` sliced from the action's start). An
  input held **on purpose** by a `key_press` is named, not reported as a failed
  cleanup; a failed release is `CLEANUP_FAILED`, never `COMPLETED`.
- Exceptions become `FAILED` with `TypeError: …` style text; nothing is retried
  automatically (the phase forbids it).

## 9. Automated tests and exact results

Windows, `.venv` Python 3.13, `PYTHONPATH=src`.

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_control.py -q` | **49 passed, 16 subtests** (model 11, service 23, manager 8, HTTP 7) |
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_control.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py -q` | **350 passed, 33 subtests** |
| `pytest tests/test_novaforge_ui.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | **169 passed, 524 subtests** |
| `pytest tests/test_web_api.py tests/test_web_platform.py tests/test_application.py -q` | **154 passed, 480 subtests** |
| `pytest tests/test_desktop_input.py tests/test_desktop.py tests/test_novaforge_closed_loop.py tests/test_novaforge_observation.py -q` | **225 passed, 46 subtests** |
| `python scripts/generate_api_reference.py --check` | **in sync** (350 routes) |

Coverage brought by `tests/test_novaforge_control.py`:

- **Model:** every kind, unknown/missing kind, key policy, button policy, hold
  and wait bounds, deadline-vs-duration contradiction, coordinate rules, session
  match, target identity vs title, payload cannot redirect the target, policy
  self-consistency, `effect` is always `None`.
- **Target:** closed gate, lost window, changed handle, changed pid, minimized,
  background, point outside the client area — each asserts **zero** injected edges.
- **Execution:** dispatch vs wait (no dispatch), key press holds, bounded hold
  releases, mouse move/hold, busy refusal, slow-injector timeout, cancel,
  external task cancellation, press-failure, duplicate press, phantom release.
- **Cleanup:** failed release → `CLEANUP_FAILED`, repeat-release idempotence,
  release refused while running, shutdown, bounded history/counters, event
  publication, unwired runner.
- **Manager/API:** Guardian gate and budget, closed session, target loss pauses
  the session, session close cancels an in-flight hold with press/release parity,
  unparseable body is a 422, capability row never claims an effect, all four
  routes 404 on an unknown session.
- **Session scoping:** a cancel or release addressed to a stale session neither
  stops nor frees the live session's input, and a stale session's status never
  reports another session's action, result or history.
- **Cancel during an edge:** a stop requested while a press is still in flight
  ends the action `CANCELLED` and releases the key instead of reporting
  `COMPLETED` with the key still down.

The tests use a recording injector and a fake `WindowGeometry`; no test sends
real input.

## 10. Type checks

```
python -m mypy src                → 2 errors, both in src/novacontrol/gui/game_host.py
                                     (UNTRACKED, pre-existing, not part of NF-19)
python -m mypy src --platform win32 → the same 2 errors
python -m mypy src/novacontrol/novaforge/control.py src/novacontrol/novaforge/manager.py
                                   → Success: no issues found in 2 source files
```

The two errors are `Unused "type: ignore" comment` in an untracked file that
predates this phase. They are reported as a repository-level limitation, not
suppressed or edited here.

## 11. Live verification

Script: `scripts/novaforge_nf19_control_acceptance.py` (expands under §12).
Environment: Windows, disposable WinForms harness `scripts/forge_harness_window.ps1`
(`-Task ready`, 420 s), no game involved, no network.

**Resource gate (the app's own `ResourceGovernor`)**

| Reading | Value |
|---|---|
| Level | `ample` (above the 4.0 GB comfort line) |
| Available RAM | **4,505.1 MB** (4.4 GB) |
| Reasons | "resources are ample" |

No model, browser or game was loaded; the run allocates one small window and one
capture worker. A `critical` reading would have been `NOT_TESTED` and started
nothing.

**Result: 20/20 checks passed, verdict `PASS`** (2026-10-25, run 5):

Run 5 is the current build, re-run after the §15 rows 9–10 fixes. Run 4 also
passed (18/18) on the pre-fix build; its numbers differed only in latency.

| Check | Observed |
|---|---|
| Harness opened, restored, client area | handle `1179904`, client `[16, 62, 1008, 362]` |
| A READY harness left by the previous run was replaced once | new window `NC FORGE HARNESS - idle`; the delivery check stayed falsifiable |
| Harness held the foreground | yes |
| Session pinned + identity confirmed | handle `1574388`, pid `7612` |
| Gate allowed + interface available | `state=allowed`, budget 6 |
| Harness started undispatched | title `NC FORGE HARNESS - idle` |
| Bounded hold (120 ms `w`) | `completed`, dispatched, **200.7 ms**, cleanup `ok`, `held_after 0`, `effect null` |
| Independent delivery read | title `NC FORGE HARNESS - idle` → **`- READY`** |
| Press held until explicit release | held `key:w`, then release cleanup `ok`, `held_after 0` |
| Cancel a 1200 ms hold | `cancelled`, ended at **264.5 ms** (before natural completion), release `ok`, `held_after 0` |
| No input held after the sequence | `held: []`, counters `received 3 / completed 2 / cancelled 1 / cleanup_failed 0` |
| Esc closes the harness; target then refuses | session `PAUSED`, `pause_reason=target_lost`, next action `rejected: held`, no dispatch |
| Session close cleanup | `4 / 4` presses/releases, `held_after 0`, `ok` |
| Closed session refuses the next action | `state=closed`, `allow=false` |

**Delivery vs effect.** The title change is an INDEPENDENT observation that the
key reached the window the request named. It is not gameplay-effect verification:
the harness retitles itself on any key press, no objective was configured, and
the result's `effect` is `null`.

**Earlier attempts (reported, not counted).** Run 3 found the fresh harness
already `READY` (a window left by the previous attempt) and reported the
delivery check inconclusive; run 2 could not claim the foreground within 15 s
(Windows foreground lock under an agent-driven session). The script now
replaces a contaminated harness once and reports a focus-blocked run as
`NOT_TESTED` rather than sending input to whatever happened to be focused. Run 5
took the replacement path and still passed 20/20. A stuck-key probe
(`GetAsyncKeyState` for W/A/S/D, Esc, Enter, Space, Shift/Ctrl/Alt) reported no
key down after the runs.

**Not live-tested:** timeouts (mocked only), mouse edges (would click real UI;
mocked + API-tested), controller input (not implemented), text/chords/drags,
and online multiplayer games (prohibited by the phase).

## 12. Verified input-interface capabilities

| Capability | Verified how | Status |
|---|---|---|
| Key press / release (edge-explicit) | live (`w`, `esc`) + tests | VERIFIED |
| Bounded key hold + auto-release | live (120 ms, cancel at 1200 ms) + tests | VERIFIED |
| Bounded wait (no input) | tests + HTTP route | VERIFIED (mock) |
| Mouse move / press / release / hold | tests + HTTP | NOT LIVE-TESTED |
| Refusal when not foreground / minimized / invisible | tests | VERIFIED (mock) |
| Refusal on stale handle / changed pid / lost window | tests + live (window closed) | VERIFIED |
| Refusal on closed/paused/unvalidated/over-budget session | tests + live | VERIFIED |
| One action at a time, busy refusal | tests | VERIFIED |
| Cancel in flight, input released, one-shot stop | live + tests | VERIFIED |
| Deadline + timeout + cleanup | tests only | VERIFIED (mock) |
| Cleanup failure reported, not hidden | tests | VERIFIED |
| Result distinguishes dispatch/completion/effect | live + tests | VERIFIED |
| Effect (gameplay) observed or verified | — | NOT IMPLEMENTED (NF-21) |
| Controller input | — | NOT IMPLEMENTED |

## 13. Known limitations

1. **Injected input is not attributable at the OS level.** The guard tracks only
   what NovaForge pressed; a physically held key is invisible to it and will not
   be released. Documented in `control.status().limitations` and in the card.
2. **Foreground race.** Between the last-moment foreground check and `SendInput`
   the user can change focus; Windows delivers to the new foreground window. The
   check makes this a small window, not zero. A target-specific injection backend
   would be required to remove it.
3. **Coordinates are absolute physical pixels** in the target's client space
   (the same space NF-07 and the capture worker use). They are not
   window-relative surface coordinates.
4. **No per-action process start-time probe.** The observation loop validates it
   every tick and pauses on a mismatch; the control gate relies on that
   `target_validated` flag plus a fresh handle/pid read. A pid reused between
   ticks could in principle pass one dispatch.
5. **Keyboard only for live evidence.** Mouse operations are implemented and
   unit/API tested but were not sent to a live window (a click could hit real UI).
6. **Cancel latency under load.** Live cancel ended the 1200 ms hold at 915 ms;
   the stop is checked every 20 ms, but release IPC shares the capture worker and
   can queue behind a capture. Mocked tests show tens of milliseconds.
7. **The control policy is configured on the manager** (`control_policy`), not
   from the config file or environment; a deployment changes code or constructs
   the manager with a different policy.
8. **No retry.** A failed dispatch is reported, never repeated.
9. **UI verified by DOM inspection, not by clicking Send.** The card was
   checked in the running app (127.0.0.1:8001) after a reload: it renders in
   Mission Control at 473 px, starts `untested` with the honest "has not been
   read" hint and all three buttons disabled, and logs no console errors. The
   Send/Cancel/Release buttons were not driven from the browser against a live
   session; those routes were exercised through the HTTP tests and the live
   acceptance script instead. No screenshot-based visual review was performed.
10. **`mypy src` is not repo-clean** because of two errors in the untracked,
    pre-existing `gui/game_host.py`.

## 14. Remaining defects and risks

- No known defect in the implemented path after the fixes in §15.
- **Risk:** a caller that ignores `status` and sends repeatedly can consume the
  session budget quickly; each call is bounded and budget-gated, and `busy`
  prevents overlap, so the blast radius is the configured per-session budget.
- **Risk:** a session with no budget (`max_actions=None`) has no action-count
  ceiling; the hold/wait/deadline bounds still apply per action. This is the
  existing NF-07/08 semantics, unchanged.
- **Risk:** events are fire-and-forget on the result path (the action does not
  wait for the bus). Recording happens in the same call (timeline + persistence),
  so observability does not depend on the event.
- **Planned follow-up (NF-21):** observe and verify the in-game effect of a
  dispatched action; nothing in this phase claims it.

### Final verdict

**PASS WITH LIMITATIONS.** The typed model, bounded execution, target
verification before dispatch, cancellation/cleanup, honest result states,
session/UI integration, tests, type checks on the changed modules and live
verification all hold. The limitations above (no controller input, mouse not
live-tested, foreground race, repo-level mypy errors, no effect verification by
design) are stated rather than hidden.

## 15. Defects found and fixed during this pass

| # | Defect | Fix |
|---|---|---|
| 1 | `GameControlService.execute` called a nonexistent `self._read_target`; every action ended `interface_error` (`AttributeError`). | Call the injected `read_target`; the 40-test service suite covers every refusal path, so this cannot silently return. |
| 2 | A policy could allow a deadline shorter than its longest permitted duration, making a "valid" action impossible to finish. | `ControlPolicy.errors()` now requires `action_deadline_ms ≥` the longest hold/wait; refused at construction. |
| 3 | Cleanup marked a later success as `CLEANUP_FAILED` if an EARLIER action's release had failed. | Failures are sliced from the action's own start; a key held on purpose is named, not treated as a cleanup failure. |
| 4 | A cancel left the runner's stop token stopped, so the next action in the session could never run. | A stop is one-shot; `finally` re-arms the runner (pinned by the post-cancel request test). |
| 5 | `clock=utc_now` was a `Callable[[], str]` where a datetime was expected (mypy error). | Use the service's default UTC clock. |
| 6 | Fire-and-forget result announcements could be garbage-collected before they ran. | The service keeps strong references until the task completes. |
| 7 | A control refusal after target loss did not pause the session (the NF-07 path did). | `control_execute` pauses through `_pause_for` on `target_lost` / `target_changed`. |
| 8 | The live script credited a delivery check to a harness that arrived already `READY`. | It replaces the window once; if a fresh window is still `READY`, the check is reported inconclusive and the verdict is `PASS WITH LIMITATIONS`, never `PASS`. |
| 9 | The control card shipped inside the Guardian tab — a `display:none` view — so every id/JS assertion passed while Mission Control never showed it and its renderer never ran. | Moved into the Mission Control view, `renderControl(null)` now runs at boot so the card never stays on “Reading…”, and `test_the_control_card_is_rendered_in_mission_control_not_guardian` pins the placement. |
| 10 | A cancel or release addressed to a STALE session id could stop or free the live session's input (one service and one guard serve every session), and a cancel arriving during a slow press edge was ignored — the result said `COMPLETED` with the key still held. | `cancel()` takes the session id and refuses a mismatch; `release_control_inputs` refuses a non-current session; `control_status` masks another session's active action, last result and history; `_dispatch` checks the stop AFTER the edge so a cancelled press ends `CANCELLED` and is released. Pinned by three new tests. |

## 16. Verification pass (2026-10-25, after rows 9–10)

Every fix above was re-verified against the final files; no earlier result is
reused for a later edit.

| Command | Exact result |
|---|---|
| `pytest tests/test_novaforge_control.py -q` | **49 passed, 16 subtests** (0 failed) |
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_control.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py -q` | **350 passed, 33 subtests** (0 failed) |
| `pytest tests/test_novaforge_ui.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | **169 passed, 524 subtests** (0 failed) |
| `pytest tests/test_web_api.py tests/test_web_platform.py tests/test_application.py -q` | **154 passed, 480 subtests** (0 failed) |
| `pytest tests/test_desktop_input.py tests/test_novaforge_closed_loop.py -q` | **97 passed, 26 subtests** (0 failed) |
| `python -m mypy src/novacontrol/novaforge/control.py src/novacontrol/novaforge/manager.py` | **Success: no issues found in 2 source files** |
| `python -m mypy src` | 2 errors, both `gui/game_host.py` (untracked, pre-existing) |
| `python scripts/generate_api_reference.py --check` | **`docs/API.md: in sync`** |
| `python scripts/novaforge_nf19_control_acceptance.py` | **PASS — 20/20 steps, 0 failed** (§11) |
| Live browser (127.0.0.1:8001, reload) | Card renders in Mission Control; badge `untested`; hint "The control interface has not been read for this session."; Send/Cancel/Release disabled; no console errors |

**Remaining unverified requirements (not defects):**

- In-game effect for any control action — **NF-21** by design; `effect: null`
  everywhere.
- Live mouse edges, live timeout expiry, and controller input (the latter is not
  implemented). Mouse and timeout behavior are covered by mocked tests and the
  HTTP surface.
- Clicking the card's own Send/Cancel/Release in a browser against a live
  session. The renderer and its pure gating logic are tested in Node, the routes
  are tested over HTTP, and the service was driven live by the acceptance script;
  only that one composition was not exercised through the browser.
- A repository-wide mypy-clean result, blocked by the untracked pre-existing
  `gui/game_host.py` errors.

**Remaining issues:** none known in the implemented path. The risks listed in
§14 are unchanged.
