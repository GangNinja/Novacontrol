# NovaForge NF-14 — Game Embedding Compatibility & Recovery

**Verdict: PASS WITH LIMITATIONS.**

The compatibility layer, the recovery paths and the separate capture axis are implemented and
verified, and an unsupported window is now refused with evidence and left untouched. The limitation
is the same one NF-13A recorded: **no installed game was embedded**, because both lightweight titles
on this machine open borderless-fullscreen windows (`UnityWndClass`, style `0x94000000`, 2880×1800,
DPI 192), which the host refuses by policy — on this host a `SetParent` attempt on those windows
previously killed the game process. A supported window was therefore verified with a **real,
disposable cross-process native window**, not with a game. Windowed mode of a real game remains
`NOT_TESTED`, and capture of a hosted window remains unsupported on this machine.

---

## 1. Initial repository state

- Branch `main`, working tree **dirty and shared**: 52 modified tracked files plus the untracked
  NovaForge files from H02/H03/NF-12/NF-13A (`src/novacontrol/novaforge/{embedding,launcher,...}.py`,
  `src/novacontrol/gui/game_host.py`, their tests, probes and reports).
- Nothing was committed, pushed, reset, stashed or discarded in this phase. No unrelated file was
  edited; NF-14 changes live inside the untracked NF-13 files plus the new `compat.py`, the new probe,
  `tests/test_gui.py`, and this report.
- The NF-14 live probe wrote runtime artifacts under `data/novaforge/nf14/` and the compatibility log
  into the application's state store (`data/novaforge_embedding_compat.json`); both are runtime data,
  not source.

## 2. NF-13 dependency status

NF-13A is implemented and its report is present at `docs/NOVAFORGE_NF13A_EMBEDDED_GAME_HOST_REPORT.md`:

- `src/novacontrol/novaforge/embedding.py` — `EmbeddedGameHost`: a `WindowApi` protocol with one
  Win32 implementation, a read-only `preflight()`, and an `embed()` that applies `WS_CHILD` **before**
  `SetParent`, then proves the parent, the style, the rectangle inside the host's physical client
  area, and the content aspect. Seven states, `detach()`, `restore()`, `check()`, `resize()`,
  `focus_game()`, `mark_closed()`.
- `src/novacontrol/gui/game_host.py` — the dashboard panel with the native surface.
- `tests/test_novaforge_embedding.py` — 60 tests at the end of NF-13A; `tests/test_gui.py` — panel tests.

NF-13A's live evidence is reused, not re-derived: reparenting into a real Qt surface, aspect-correct
letterboxing, detach restoring style/parent/rect, the tab-switch (`host_hidden`) behaviour, and the
finding that `PrintWindow` cannot capture a **hosted** window on this host while it can capture the
same window after detach.

## 3. Gap analysis (before implementation)

| Requirement | NF-13A state | Gap closed in NF-14 |
| --- | --- | --- |
| Classification vocabulary (`SUPPORTED` … `NOT_TESTED`) | none — only `supported: bool` + reason code | `novaforge/compat.py`: `CompatibilityClass` plus one explicit code→class mapping, evidence list, limitations, timestamp |
| Recorded identity + configuration | host kept a `GameIdentity`, but no record | record carries game, pid, **process start time**, hwnd, window title/class, app id, launch id, ownership, window mode, DPI and the check time |
| Validate HWND/process **immediately** before native writes | preflight was the last read before mutation | `_revalidate_before_mutation`: re-reads the window, its pid, its parent/style and the process creation time after the approval gap; refuses with `window_changed_before_embed` **without writing anything** |
| Partial-failure evidence | restore was attempted, not described | `applied` step list, `partial`, `recovery {attempted, applied, restored, verified}` |
| A refusal before any write must not claim a restore | detach already refused, embed path did not report it | pre-mutation refusals report `applied: []`, `partial: false`, `recovery.attempted: false` |
| Window/process dies during the mutation | treated as a generic `embedding_failed` → **UNSUPPORTED** | `window_destroyed` → `UNKNOWN`, `restored: null`, no false "not restored" limitation (found and fixed in this phase's own pass, §6.4) |
| Idempotent cleanup | present | `cleanup()` reports `changed`/`restored: null` when it did nothing; pinned for the destroyed-window case |
| Delayed window creation | `set_running`/`adopt_running` only | `refresh_window()` (bounded, same PID + start time) called by the existing panel timer while `running`/`launching` |
| Explicit fallback | none | `fallback()` report: stops the attempt, offers the existing NF-12 launch workflow, never acts |
| Embedding vs capture independence | `status_payload(observation=…)` already separated them | the compatibility record has its own `capture` axis, written ONLY by `note_capture()` from a measured frame result; the panel renders a compatibility line and a capture line that never read each other |
| Persistence of the evidence | none | `CompatibilityLog` through the app state store, 200-row cap, newest first |

Nothing in "alternatives" (borderless switch, Windows Graphics Capture streaming) was implemented:
the gap analysis showed they are not required to meet the phase's safety goal, and neither can be
implemented honestly without a windowed test target on this machine.

## 4. Files inspected and changed

**Inspected:** `novaforge/embedding.py`, `gui/game_host.py`, `gui/app.py`, `novaforge/launcher.py`,
`novaforge/manager.py`, `desktop/capture.py`, `desktop/capture_backends.py`,
`desktop/capture_validation.py`, `desktop/input.py`, `application.py`, `novaforge/__init__.py`,
`tests/test_novaforge_embedding.py`, `tests/test_novaforge_ui.py`, `tests/test_gui.py`,
`tests/test_novaforge_launcher.py`, `tests/test_novaforge_library*.py`,
`docs/NOVAFORGE_NF13A_EMBEDDED_GAME_HOST_REPORT.md`, `docs/NOVAFORGE_NF12_…`,
`docs/NOVAFORGE_UNIVERSAL_CAPTURE_REPORT.md`, `docs/NOVAFORGE_H02_1_FOCUS_LOSS_REPORT.md`.

**Changed:**

- `src/novacontrol/novaforge/compat.py` — **new** (525 lines): classes, evidence, `classify_preflight`,
  `apply_attempt`, `apply_restore`, `note_window_ended_embedding`, `CompatibilityLog`.
- `src/novacontrol/novaforge/embedding.py` — compatibility record on the host
  (`probe_compatibility`, `note_compatibility`, `note_capture`, `compatibility`), pre-mutation
  revalidation, recovery facts, `window_destroyed` handling, `refresh_window`, `fallback`,
  `cleanup` reporting, record on every `embed`/`detach`.
- `src/novacontrol/gui/game_host.py` — compatibility line (class, reason, fact count, check time,
  separate capture axis, limitations), fallback line, delayed-window retry in the existing timer,
  compatibility recording from the panel (kept on the host, persisted by the log).
- `tests/test_novaforge_embedding.py` — four new NF-14 test classes (**40 new tests**, 100 total).
- `tests/test_gui.py` — `GameHostTabTests` (4 tests, offscreen), including
  `test_compatibility_is_rendered_separately_from_capture`.
- `scripts/novaforge_nf14_compat_probe.py` — **new** live probe (real app, real Win32 adapter, every
  write recorded).
- This report; a `docs/STATUS.md` entry.

## 5. Compatibility classifications and evidence

| Class | Meaning in this build | Example observed |
| --- | --- | --- |
| `SUPPORTED` | preflight accepted a windowed window and (for an attempt) the embed verified | disposable WinForms window (live); windowed fake (tests) |
| `SUPPORTED_WITH_LIMITATIONS` | borderless but not covering the monitor: no frame to restore, aspect preserved by resizing the whole window | borderless fixture (tests) |
| `UNSUPPORTED` | measured fullscreen/borderless-fullscreen presentation, or already a child window | **Shadow Fight Arena, live** |
| `UNKNOWN` | a condition, not a verdict: no window yet, minimized, hidden, wrong pid, pid reuse, window changed owner before the write, window destroyed mid-attempt, host platform | tests + live destroyed-window case |
| `NOT_TESTED` | no record for this game/window; also the capture axis until a frame is verified | default |

- **No title-based inference.** The class comes from `window_mode(style, rect, monitor)`, visibility,
  minimization, parent/owner, the identity check, and the attempt's own measured outcome. There is no
  per-game table anywhere in the code or the log.
- A property that cannot be read is `measured: false` in the evidence and cannot produce
  `SUPPORTED`: a non-Windows host, an unreadable window, and a missing window are all `UNKNOWN`.
- An **unrecognised** refusal code is `UNKNOWN` with an `unclassified_preflight` evidence row, never a
  pass and never a proven failure.

### 5.1 Live classification, Shadow Fight Arena

- launch through the NF-12 plan/token path; `launch_requested`, `process_started`,
  `window_detected`, `launch_verified` all true; pid **11152**, start
  `2026-10-24T12:15:37.1616074Z`, hwnd **12716568**, `UnityWndClass`, style `0x94000000`,
  rect/client 2880×1800 at (0,0), DPI 192.
- classification **`UNSUPPORTED`**, reason code `fullscreen_presentation_unsupported`, window mode
  `borderless_fullscreen`, three measured evidence rows (`preflight`, `window`, `geometry`).
- `embed()` was then called anyway (the probe deliberately exceeds the refusal) and returned
  `embedded: false`, `applied: []`, `partial: false`,
  `recovery {attempted: false, applied: [], restored: null, verified: null}`, and the recorder saw
  **zero native writes** (`native_writes: []`).
- closed through the service: `status: CLOSED`, `closed: true`, "exited after a graceful close".

## 6. Recovery implementation

1. **Original state recorded before modification** — `_SavedWindow(parent, style, rect)`, written
   before the first Win32 call.
2. **Identity validated immediately before the mutation** — window exists, same pid, same
   parent/style (not already a child), and process creation time within tolerance of the recorded
   start time. A refusal touches nothing and is reported as such.
3. **Partial failures detected** — `applied` grows *before* each call (`style`, `parent`,
   `placement`), so an API that applies and then fails to read back is still described truthfully.
4. **Restore on failure** — style, parent and rectangle are written back with `SWP_FRAMECHANGED |
   SWP_SHOWWINDOW`, then read back; a read-back mismatch is `restored: false`, and the record gets an
   explicit limitation instead of a success claim.
5. **Destruction and termination** — `check()` reads the window first; a dead handle becomes
   `closed`/`process_exited` (verified live), a window that left the host `window_left_host`, a
   recreated window is reported detached and never re-attached automatically.
6. **Idempotent cleanup** — `cleanup()` twice changes nothing the second time; after destruction it
   reports `changed: false, restored: null` (live: exactly that).
7. **Recovery failures reported** — `restore_failed` reason code, limitation text, and the recovery
   block on the attempt.
8. **Never touch an unrelated window** — every mutation is preceded by the identity/hierarchy check;
   a pid mismatch is `UNKNOWN` and is not adopted (`test_a_foreign_window_is_never_adopted`,
   `test_a_reused_pid_is_not_adopted_as_the_window`).
9. **No success from an API return value alone** — `SetWindowPos` returning true is not success: the
   host reads back the parent, `WS_CHILD`, visibility, screen rect inside the host's client area, and
   the exact target size.
10. **No display-mode change, no config edit** — the host writes three window properties to a window
    it already proved it owns, and nothing else.

### 6.4 A defect found by this phase's own verification

A window destroyed *during* the mutation was folded into `embedding_failed`, which the classifier
mapped to `UNSUPPORTED` with a "could not be confirmed restored" limitation. That is two false
statements: a crash is not evidence about hosting, and there was no window to restore. It is now
`window_destroyed`, class `UNKNOWN`, `restored: null`, no limitation, and cleanup claims nothing.
Pinned by `test_a_window_destroyed_during_embedding_is_unknown_not_unsupported`.

## 7. Lifecycle and focus handling

| Scenario | Handling | Verification |
| --- | --- | --- |
| process starts, no window yet | `running` + bounded `refresh_window()` from the existing timer | tests (`test_a_window_that_appears_after_the_launch_is_adopted`); live NOT_TESTED |
| window appears after a delay | adopted only if pid **and** start time match; a foreign window is never adopted | tests; live NOT_TESTED |
| game exits during embedding | `window_destroyed` → `UNKNOWN`, no restore claim, cleanup no-op | test (new); live equivalent: destruction after embed |
| window destroyed while embedded | `closed`/`process_exited`, window dropped | **live** (process exit 1 → `closed`) + tests |
| window destroyed and recreated | reported detached, never silently re-parented | test |
| NovaControl loses foreground | nothing is read from the foreground; state stays `embedded`, parent intact | **live**: another probe window took the foreground, `state_after: embedded`, parent unchanged |
| host minimized / tab switch | `host_hidden` keeps the embedding; resize defers | NF-13A live evidence reused; unit tests re-run |
| embedded window hidden/minimized | detached and restored, unless the host itself is off screen | tests |
| panel resized | letterboxed re-fit, failure restores the last valid geometry | **live**: 1035×540 at (562,1004) inside 1884×540, parent intact |
| user detaches | original parent/style/rect restored and read back | NF-13A live; unit tests re-run |
| host closes while the game runs | `cleanup()` restores if it can, reports if it could not | unit tests |
| same game relaunched, new process identity | previous record dropped with the identity; a reused pid is refused | tests |

No focus-recovery loop was added: the host never steals focus, `focus_game()` exists only behind the
explicit button, and `check()` reads no foreground window — so there is no "regain" action that could
fire on its own. That is deliberate and pinned (`test_lifecycle_polling_never_steals_focus`).

## 8. Fallback behavior

`EmbeddedGameHost.fallback()` is a report, never an action. For an unsupported or failed attempt it
returns `active: true`, `native_embedding: false`, `workflow: "desktop"`, the measured reason, and
the existing NF-12 workflow steps (use the desktop window; launch/reopen through the Game Library;
switch to Windowed in the game's own settings). The panel renders it in its own line under the
compatibility line and only while nothing is embedded. The refusal itself happens before any native
call, so "stop the failed attempt" means "never start it" for `UNSUPPORTED`, and "restore, then fall
back" for `embedding_failed`.

## 9. Separate embedding and capture status

Three separate surfaces, all in the same payload but never derived from each other:

- `embedding.verified` — the NF-13 hierarchy/geometry proof.
- `observation` — the existing H03 capture capability read, copied with
  `implied_by_embedding: false`.
- `compatibility.embedding` (`embedded|refused|failed|not_attempted`) and
  `compatibility.capture` (`not_tested|unavailable|available`).

A verified embed **never** writes `capture: available`. The record's capture axis has exactly one
writer, `EmbeddedGameHost.note_capture(ok, error=…)`, and it takes a **result**: the caller must have
captured and validated THIS window's frame. A capability read cannot be passed in as a frame, which
matters here because this host reports `observation.available: true` while a hosted window refuses
`PrintWindow` — and the live record now says `capture: unavailable` next to `embedding: embedded`.
The panel prints "Capture: …" and "Compatibility: … · capture compatibility: …" as different lines,
and `test_compatibility_is_rendered_separately_from_capture` asserts a SUPPORTED record with an
unmeasured capture still renders `not_tested`.

## 10. Automated tests

Commands (offscreen Qt, `PYTHONPATH=src`), exact results:

```
pytest tests/test_novaforge_embedding.py tests/test_gui.py \
       tests/test_capture_backends.py tests/test_desktop_input.py \
       tests/test_novaforge.py -q
   → 289 passed, 15 warnings, 34 subtests passed   (21.65 s)

pytest tests/test_novaforge_library.py tests/test_novaforge_library_ui.py \
       tests/test_novaforge_launcher.py -q
   → 105 passed, 205 warnings, 22 subtests passed  (30.08 s)
```

`test_novaforge_embedding.py` alone: **100 passed, 8 subtests** (60 at the end of NF-13A; **40 new
NF-14 tests** in `CompatibilityClassificationTests` 8, `CompatibilityAttemptTests` 12,
`RecoveryLifecycleTests` 12, `CompatibilityLogTests` 6, plus one detach case in `DetachTests` and
the launch guard in `ReportingTests`).

Coverage required by Scope F, and where it lives:

| Requirement | Test |
| --- | --- |
| classification | `CompatibilityClassificationTests` (8) |
| invalid HWND / stale handles | `test_window_destroyed_while_embedded_reports_closed`, `test_a_destroyed_window_ends_the_embedding_without_lying`, preflight read-refusal tests |
| PID reuse / identity | `test_wrong_owner_and_a_reused_pid_are_unknown`, `test_a_reused_pid_is_not_adopted_as_the_window`, `test_a_window_that_changes_owner_before_the_mutation_is_not_touched` |
| failed embedding | `test_a_failed_embed_reports_the_partial_mutation_and_restores`, `test_set_parent_failure_restores_the_original_window` |
| partial embedding | same, plus `applied == ["style", "parent"]` assertions |
| successful restoration | `test_detach_restores_parent_style_and_screen_rect` |
| failed restoration | `test_a_failed_restore_is_reported_not_claimed` |
| repeated cleanup | `test_cleanup_is_idempotent`, `test_cleanup_before_any_attempt_changes_nothing`, `test_a_failed_check_restore_stays_retryable`, destroyed-window cleanup |
| window destruction / process termination | `test_a_window_destroyed_during_embedding_is_unknown_not_unsupported`, `test_window_destroyed_while_embedded_reports_closed` |
| focus changes | `test_focus_is_only_requested_explicitly`, `test_lifecycle_polling_never_steals_focus` |
| minimize / restore | `test_minimized_embedded_window_is_detached_and_restored`, `test_hidden_host_keeps_the_embedding` |
| a game that reverses the embedding | `test_a_game_that_minimizes_itself_is_recorded_as_a_limitation`, `test_a_recreated_window_is_recorded_as_a_limitation`, `test_a_window_that_reverses_the_child_style_is_detached_and_restored`, `test_window_leaving_the_host_is_reported_detached` |
| a launch attempted while embedded | `test_a_second_launch_while_embedded_is_refused` |
| corrupted history | `test_a_corrupt_row_does_not_lose_the_readable_history`, `test_a_recorded_evidence_row_is_a_snapshot` |
| resize | `test_resize_letterboxes_within_the_new_client_area`, `test_resize_verification_failure_restores_the_window`, `test_resize_that_cannot_be_applied_keeps_the_last_valid_geometry`, `test_resize_is_deferred_while_the_surface_is_hidden` |
| unsupported configuration | `test_fullscreen_is_unsupported_and_reads_only`, `test_an_owned_child_is_unsupported` |
| fallback reporting | `test_the_fallback_offers_the_desktop_workflow` |
| independent embedding/capture | `test_embedding_and_capture_observation_stay_separate`, `test_a_verified_embed_is_supported_and_leaves_capture_untested`, `test_a_measured_capture_result_never_changes_embedding`, `test_capture_can_be_available_while_embedding_is_unsupported`, GUI `test_compatibility_is_rendered_separately_from_capture` |
| regression H02.1 / H03 / NF-12 / NF-13 | `test_novaforge.py` (77), `test_capture_backends.py` (52), `test_desktop_input.py` (50), library + launcher (105), host (100), GUI (10) |

All native operations stay behind the `WindowApi` protocol; failure-path tests use `FakeWindowApi`
subclasses (`refuse_parent`, `refuse_style`, `fail_next_position`, `misplace`, `StickyChild`,
`Vanishing`, `ChangingOwner`). No test weakens an existing assertion; the NF-13A tests are unchanged
and still pass.

## 11. Type checks

```
mypy src                     → Success: no issues found in 439 source files
mypy src --platform win32    → Success: no issues found in 439 source files
```

## 12. Live verification

Artifacts: `data/novaforge/nf14/20261024T121513Z_nf14_report.json` (cited below), plus four earlier
runs (`…113014Z`, `…113409Z`, `…113746Z`, `…115439Z`) in the same directory. Run with
`QT_QPA_PLATFORM=windows PYTHONPATH=src .venv/Scripts/python.exe scripts/novaforge_nf14_compat_probe.py`.

**Resource gate, read immediately before testing** (`LaunchService.memory_check`, minimum 2.0 GB free
/ maximum 92 % used): **4.81 GB free, 68.7 % used, 15.37 GB total → pass**. The probe re-checked the
gate itself and refused nothing.

Environment: Windows desktop, 15.37 GB RAM, project `.venv` (Python 3.13, PySide6), real Win32
adapter, real `NovaControlApplication`, real Qt surface (`QT_QPA_PLATFORM=windows`; the offscreen
platform is used only by the automated suite).

Safety: no GTA V, no duplicate instance, no user-owned game (the game was launched by the probe
through the token path and closed through the service), no force-kill, no input synthesis, no
injection/hooks, no external drive access.

| Step | Result |
| --- | --- |
| SFA launch | pid 11152, all four launch facts true |
| SFA classification | `UNSUPPORTED` / `fullscreen_presentation_unsupported` / `borderless_fullscreen` |
| SFA attempt | `embedded: false`, `applied: []`, `partial: false`, recovery `attempted: false`, **native_writes `[]`** |
| SFA fallback | active, workflow `desktop`, three next steps |
| SFA close | `CLOSED`, graceful, exit confirmed |
| fixture classification | `SUPPORTED` / `ok` / `windowed` (pid 34364, hwnd 38799756, style `0x16CA0000`, 1800×1000, client 1768×922, DPI 96) |
| fixture embed | `embedded: true`, parent verified `377161304`, applied `[style, parent, placement]`, 1135×592 inside 2244×592, `partial: false` |
| foreground change | another probe window took the foreground; state stayed `embedded`, parent unchanged |
| resize | 1035×540 at (562,1004) inside 1884×540, parent intact |
| capture of the embedded window | `ok: false`, `printwindow`, "the capture worker stopped responding"; recorded on the record as `capture: unavailable` while `observation.available` stayed `true` and `implied_by_embedding: false` |
| process/window destruction | `exit code 1` → `state: closed`, `reason_code: process_exited`, window dropped |
| cleanup after destruction | `cleaned: true`, `changed: false`, `restored: null` |
| compatibility log | 2 records from this run (`UNSUPPORTED` SFA, `SUPPORTED` fixture with `capture: unavailable`), 9 in the namespace overall (earlier probe runs), newest first |

The fixture half reproduced identically in all five runs; the SFA half in the four runs that launched
it. `foreground_is_other` in the artifact means the probe's second window successfully took the
foreground (the field is the probe's own focus request, not the game's).

**Not live-tested, and marked `NOT_TESTED`:** a real game in windowed/borderless mode (would require
changing the game's own settings), delayed window adoption, pid reuse against a live process, host
minimize/restore in this phase (NF-13A's live evidence reused), detach-after-failure on a real game.

## 13. Capture compatibility, reported independently

Same conclusion as NF-13A, re-measured in this phase: capturing the **hosted** fixture through the
existing window-surface backend (`PrintWindow`/`PW_RENDERFULLCONTENT` via
`LocalDesktopRunner.capture_window`) **fails** — `ok: false`, `backend: printwindow`,
error "the capture worker stopped responding", 0×0. The measurement is passed to
`note_capture(False, error=…)` and the record then says `capture: unavailable` next to
`embedding: embedded` — the two axes disagree in public, which is the point. `observation.available`
stayed a separate, pre-existing capability read with `implied_by_embedding: false`; it was never used
to set the record's capture axis (a capability is not a frame).

So on this host: **embedding is supported, capture of a hosted window is not**, and nothing in NF-14
claims otherwise. No streamed/Windows-Graphics-Capture alternative was added or claimed, and
minimized-window capture remains unsupported and unclaimed.

## 14. Known limitations, remaining defects and risks

1. **No real game was embedded.** The two lightweight installed titles are borderless-fullscreen; the
   policy refusal is correct, but "NovaForge can embed a real game" is still unproven. A windowed
   game (or the user switching these games to windowed) is the next honest test.
2. **Capture of a hosted window fails** on this host (see §13). Embedding and capture are separate
   capabilities and only the first is proven here.
3. **Delayed-window, pid-reuse and detach-after-failure paths are automated only.** They are
   deterministic fake-API tests, not live evidence.
4. **A game that reverses window changes** (re-parents itself back, recreates its window, re-applies
   a non-child style) is detected and reported (`window_left_host`, `window_destroyed`,
   `window_style_reverted`, `process_identity_changed`) and classified `SUPPORTED_WITH_LIMITATIONS`,
   but recovery is "tell the truth and detach", not "fight the game". That is deliberate.
5. **Exclusive fullscreen is not distinguished from borderless-fullscreen.** The host reads window
   style and monitor geometry, not DXGI exclusive-mode state; the refusal does not need the
   distinction, and this report does not claim it was measured.
6. **Compatibility history is a diagnostic**, capped at 200 rows in the app state store; it is not an
   audit trail and is not durable against a state-store wipe.
7. **No second native stack.** Everything remains one `SetParent`-based host behind `WindowApi`; any
   future streamed alternative is a new capability, not a toggle here.

## 15. Verification pass (2026-10-24)

The phase was re-audited against every NF-14 requirement after implementation. The phase tests were
run first, then the dependency/integration groups; **twelve defects** were found and fixed, every
behavioural one pinned by a test that fails on the previous behaviour.

1. **A preflight refusal was recorded as `UNSUPPORTED` after any embed attempt.** Missing window,
   minimized/hidden window, wrong owner, wrong platform and unreadable geometry all reached
   `apply_attempt`, whose else-branch called them proven incompatibility and `embedding: failed`.
   They are now `UNKNOWN` + `refused`; only an attempt that actually mutated the window and failed is
   `UNSUPPORTED`/`failed`. Pinned by `test_a_missing_window_is_unknown_not_unsupported` and
   `test_a_platform_refusal_after_an_attempt_stays_unknown`.
2. **Detach after window destruction claimed a failed restore.** `restored: false` plus "could not
   confirm the original window state" for a window that no longer existed. It now returns
   `restored: null` and adds no limitation. Pinned by
   `test_detach_after_the_window_was_destroyed_claims_no_restore`.
3. **The compatibility log stored live objects.** The panel records the preflight, then the attempt
   refines the same object, so the logged "preflight" row silently became the attempt row and two
   rows shared one object. `record()` stores a snapshot through `to_dict`/`from_dict`. Pinned by
   `test_a_recorded_evidence_row_is_a_snapshot`.
4. **One corrupt persisted row could crash the panel.** `CompatibilityLog.__init__` parsed every row
   with no guard and `from_dict` called `int()` on stored values, so a non-numeric pid raised out of
   the constructor. Identity numbers are now tolerant (`0` when unreadable) and a bad row is
   skipped, not fatal. Pinned by `test_a_corrupt_row_does_not_lose_the_readable_history`.
5. **`refresh_window` trusted a cached HWND.** A handle reused by another process was reported as
   the game's window. The cache is re-read and must carry the recorded pid; otherwise a fresh
   lookup runs. Pinned by `test_a_reused_handle_is_not_reported_as_the_game_window`.
6. **A game that re-applied its own window style stayed "embedded".** The parent still pointed at
   the host, so only a later resize noticed. `check()` now detects the missing `WS_CHILD`, restores
   the window and reports `window_style_reverted`. Pinned by
   `test_a_window_that_reverses_the_child_style_is_detached_and_restored`.
7. **A game that undid the embedding left the record saying `SUPPORTED`.** Self-minimizing, hiding,
   re-parenting or recreating the window was reported in the state but not in the classification.
   Those paths now record a limitation and `SUPPORTED_WITH_LIMITATIONS`. Pinned by three tests.
8. **`set_launching` while embedded dropped the only restoration state.** It reset `_saved` and
   `_window` without detaching, which would leave the game parented to a panel that no longer tracks
   it. It now raises, like `set_running`. Pinned by `test_a_second_launch_while_embedded_is_refused`.
9. **A failed restore was unrecoverable.** After a window event whose restore failed, the host went
   `DETACHED` and `cleanup()` refused to retry, losing the saved geometry. A failed restore now keeps
   the handle and saved state, `detach()` accepts that state, and `cleanup()`/dashboard shutdown
   retry it. Pinned by `test_a_failed_check_restore_stays_retryable`.
10. **Resize and check restores were not written to the record.** `restored` stayed `null` after a
    recovery the host actually performed; both paths now fold the result in.
11. **The panel showed a fallback claim with no game.** At rest `fallback()` returned
    `active: true` with "Native embedding is not available for this window" although no window was
    associated and nothing had been attempted. It is inactive until an identity is attached, with an
    empty step list. Pinned in `test_state_labels_and_initial_state`.
12. **Smaller corrections.** The panel counted unmeasured evidence rows as "measured fact(s)" (now
    "evidence row(s)"); repeated failed restores no longer duplicate their limitation; an unused
    `for_pid`/`status_payload`/`record_fields` API was deleted and the one test that used it reads
    `latest()` instead.

**Checks after the fixes:** phase suite **110 passed** (`test_novaforge_embedding.py` 100, GUI 10);
host + GUI + capture + desktop-input + `test_novaforge.py` **289 passed / 34 subtests**;
library/launcher **105 passed / 22 subtests**; both mypy views clean over **439 files**. No test was
weakened; every behavioural defect above is pinned by a test (new or extended).

**Live re-run after the main fixes** (`data/novaforge/nf14/20261024T121513Z_nf14_report.json`):
memory gate **4.81 GB free / 68.7 % used**, SFA (pid 11152, `UnityWndClass`, 2880×1800, DPI 192)
**UNSUPPORTED** with `applied: []` and **zero native writes**, closed gracefully; fixture (pid 34364)
**SUPPORTED**, embedded (parent `377161304`, 1135×592 in 2244×592), foreground change survived,
resized to 1035×540, `closed`/`process_exited`, cleanup `changed: false`; hosted-window capture failed
and the record says `capture: unavailable`. The later fixes touch paths this probe does not exercise
(launch, re-parent/self-minimize, retry-on-failure, at-rest fallback); they are covered by the suite
only.

**Still unverified (unchanged):** a real game in windowed mode, live delayed-window adoption, live
pid reuse, live detach-after-failure, and capture of a hosted window. These remain `NOT_TESTED`
where the report says so.

## 16. Verdict

**PASS WITH LIMITATIONS** — compatibility is classified from evidence, an incompatible window is
refused with zero native writes and a fallback, a supported window is embedded, focused, resized,
destroyed and cleaned up with truthful reporting, embedding and capture stay independent, and the
automated suites plus both mypy views are green. The limitation is scope-honest: on this machine the
only installed lightweight games expose windows this host refuses by design, so game embedding itself
remains unproven while its refusal, recovery and reporting are verified.
