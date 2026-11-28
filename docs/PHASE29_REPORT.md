# Phase 29 — Universal Computer & Device Operator

**Date:** 2026-11-10
**Verdict:** **PARTIAL** under the updated universal acceptance policy — core capability complete, phase remains open
**Commit/push:** none

## 1. Dependency and specification gate

Phase 28 was closed as **PASS** in `docs/PHASE28_REPORT.md`; its
collaboration, verification and recovery foundations are present in this
worktree. The dependency gate is therefore satisfied.

There is no canonical Phase 29 document in the repository: `ROADMAP.md` ends at
Phase 20 and `docs/PHASES.md` ends at Phase 15. The supplied Phase 29 brief is
the acceptance contract. No roadmap entry was invented and no Phase 30 work was
started.

## 2. Result in one paragraph

NovaControl now has a real, context-aware Windows and browser operator. A single
`ComputerOperator` owns one active task, builds a plan through the existing
interpreter/planner, observes through window and OCR perception, acts through
the approval-aware desktop and Playwright controllers, observes the result,
verifies the change, enters the existing recovery loop when it diverges, and
persists a redacted evidence record. It is exposed through nine `/computer/*`
routes with a server-minted, single-use token for every run. It reuses the
existing perception, decision, planning, reliability, permission, event, model,
audit, learning and persistence layers; it does not run a parallel agent loop.

The core capability is live-verified, including a native hung-window
observation through production perception and a token-gated Windows Settings
workflow. Strict phase closure is **PARTIAL** because several mandatory
real-world reliability scenarios could not be induced on this machine; they
are listed explicitly in §14 and the acceptance matrix.

## 3. What was built vs. reused

### New package: `src/novacontrol/computer/`

| Module | Responsibility |
|---|---|
| `models.py` | device/operation capability models, operation risk classification, history/report shapes |
| `devices.py` | live Windows display/input/desktop/browser/phone discovery |
| `context.py` | resolution of “the Save button”, “continue where I stopped”, “the file I was working on”, “switch to the browser” and similar references against a real observation/history |
| `history.py` | bounded, redacted, persisted `computer_history` records |
| `operator.py` | one-task lifecycle, emergency stop, plan/run orchestration, observation and history |
| `__init__.py` | public package surface |

### Reused and extended

- `agentcore/orchestrator.py` — the actual observe → plan → act → observe →
  verify → recover loop, cancellation checkpoints and the 12-iteration bound.
- `agentcore/perception.py` / `ui_state.py` — window, dialog, OCR-coordinate,
  loading/error, screenshot-hash and structured-diff observation, plus
  unresponsive-window and live display metadata.
- `agentcore/actions.py` — semantic action execution through the desktop and
  browser controllers.
- `desktop/controller.py` — approval, audit, input guard, window focus,
  minimize/maximize/close, launch verification, dialog probing and desktop
  primitives.
- `browser/controller.py` — approval, audit and Playwright navigation, typing,
  clicking, scrolling, extraction, upload, download and tabs.
- `reliability/` — `VerificationEngine`, `RecoveryEngine`, `TaskStateMachine`
  and `PermissionManager`.
- `intelligence/` — `TaskInterpreter`, decision and capability knowledge.
- `core/audit.py` — the same append-only automation audit trail used by desktop
  and phone.
- `persistence/json_store.py` — durable operation history.
- `application.py` — app-owned operator, device registry, history and shutdown.

No NovaForge or NovaMarket behavior was changed in this phase.

## 4. Closed loop and context

The live probe used the objective **“open calculator”** through the operator:

- plan produced a token-bearing step;
- the run started in the `executing` phase;
- **5 real observations** were captured;
- the action succeeded only after the new window appeared
  (`Window appeared: Calculator`);
- the final verdict was **PASS**;
- the operation history still contained the record after an application
  restart.

The loop is not “plan then fire”. After every action the orchestrator observes
again, checks the expectation, and only then decides whether the step passed.
An unverified step is never rounded up to success.

`resolve_reference()` returns `unresolved` when no element exists and
`ambiguous` when more than one observed element matches. It never resolves a
missing target into a guessed coordinate. “Continue where I stopped” comes from
the unfinished persisted objective, not from a fabricated continuation.

## 5. Perception and grounding

`UiPerceptionEngine` combines:

- the live OS window list, including top-level dialog windows that process
  main-window enumeration misses, and per-window attributes (process, bounds,
  foreground, minimized/maximized, responding);
- unresponsive top-level windows through the Win32 `IsHungAppWindow` probe,
  exposed as `unresponsive_windows` / `has_unresponsive_window`;
- a fresh screenshot captured through the existing approval-gated capture path;
- OCR words with screen coordinates;
- optional genuine vision-model understanding when one is wired;
- real `loading`, `error` and `dialogs` signals;
- live display bounds and work areas from the monitor probe, including
  multi-display geometry when more than one monitor is attached;
- a SHA-256 of the captured frame and a structured UI diff.

A click is grounded in the observed window: the controller focuses the owning
window, checks the foreground state and uses the observed/OCR point. A label
that was not observed is sent to the existing OCR/vision locator, which fails
honestly when it cannot locate it. Stale coordinates from an earlier frame are
not reused after an interface change.

The Save As path is safe: the controller first checks that a save dialog is
present in the foreground/top-level window list. With no dialog it refuses to
type a path into the document. In the live probe the dialog was detected as
`Save as`, the dedicated file was written, and the dialog was dismissed.

## 6. Action surfaces

### Desktop

The controller now exposes mouse click/double-click/right-click, scroll, drag,
button press/release, typed text, shortcuts, copy/paste/select-all, key down/up,
focus, minimize, maximize, graceful close, application/folder launch, app deep
links, web search, screenshots, file list/read/write, dedicated-directory
organization, Save As, and vision-guided click.

Input is guarded against the intended target. A launch is successful only when
a **new** window appears; a pre-existing window is not evidence of a launch. A
failed focus types nothing.

### Browser

Playwright supports navigation, extraction, click, type, fill/submit, scroll,
back, key press, screenshot, upload, download, JavaScript evaluation and real tab
operations (`tab_new`, `tab_list`, `tab_switch`, `tab_close`). Upload uses the
page's file input. Download waits for the real browser download event, saves to
the requested path, and verifies a non-empty file before reporting success. Tab
creation and closing go through the same approval/audit layer as other browser
actions.

Browser sessions are normal browser-context sessions: a local disposable login
probe signed in, followed a cookie-protected page, and read the authenticated
marker `Welcome phase29-session`. No authentication control was bypassed; no
third-party credentials were used. The unused `GET_COOKIES`/`SET_COOKIE`
declarations were removed rather than left as dead action types.

### Phone

The existing ADB bridge remains the phone path. It was not paired in this phase;
the device registry reports it unavailable with pairing guidance instead of
pretending it is usable.

## 7. Safety, permissions and audit

- Every `POST /computer/run` requires a server-minted, single-use, expiring
  token tied to the exact objective; `POST /computer/plan` mints it.
- The token is consumed before input. A replay or a mismatched objective is
  refused.
- `read_only`, `reversible`, `sensitive`, `destructive` and `privileged` classes
  are separate. Sensitive, destructive and privileged operations need explicit
  approval; destructive objectives are blocked before input otherwise.
- The operator installs its approved gateway for the run, but every desktop
  action still passes through `DesktopAutomationController` permission and
  audit. The vision capture now takes a per-action approval override instead of
  replacing the controller gateway, fixing a real defect where a capture could
  reset an active run to deny and clobber an approved session
  (`ApprovalIsolationTests`).
- Every browser action in the closed loop now routes through
  `BrowserAutomationController`; the operator swaps and restores both the
  desktop and browser approval gateways. A denied browser action is recorded
  and never reaches the raw Playwright runner (regression test). The raw runner
  remains only a unit-test seam.
- The browser controller records every approved, denied and failed action in
  the shared audit log.
- History redacts passwords, tokens, API keys, cookies, credentials, URL
  user-info and email addresses; it stores no screenshots, page bodies or
  clipboard contents.
- `POST /computer/stop` cancels before the next action, releases held keys and
  buttons, and is safe when no task is active.
- No privilege elevation or unsolicited destructive action is performed.

## 8. Reliability, recovery and lifecycle

- A failed step enters the existing `RecoveryEngine`; destructive actions and
  missing dependencies are never retried.
- A genuinely hung native Win32 window was induced live in
  `tmp/phase29/unresponsive_evidence.json`; production
  `ComputerOperator.observe` reported `responding: false`,
  `unresponsive_windows` and `has_unresponsive_window: true`, then cleared them
  after cleanup. A forced process crash during a workflow remains unverified.
- Optional LLM enrichment is bounded at 8 seconds and falls back to the
  deterministic heuristic interpretation; a hung model cannot block
  `/computer/plan` or loop interpretation.
- If the orchestrator raises before returning a state, the operator persists a
  failed history record and restores both gateways. Plan exhaustion cannot
  complete a task unless final verification passes; the old "all actions
  succeeded" fallback is gone.
- An unexpected dialog is exposed by perception and handled before the next
  step; the live Save As probe exercised dialog detection.
- A missing window, failed focus or absent save dialog is a failure, not a
  completed action.
- Cancellation is cooperative at action boundaries; the stop probe executed
  **0 actions**, opened no Calculator, and released input.
- The task registers active state with the orchestrator and is removed on
  completion, failure or cancellation; the single-task rule is enforced by the
  operator rather than the HTTP process.
- History writes are best-effort and cannot turn the action they record into a
  failure.

## 9. Device and platform support

| Device/platform | Status | Evidence |
|---|---|---|
| Windows 11 desktop | supported, live-tested | Windows 11 10.0.26300, 2880×1800 at 2× scale |
| Keyboard/mouse/window control | supported, live-tested | typing, Ctrl+S, focus, minimize/maximize, input release |
| One attached display | probed live | one monitor; multi-monitor geometry is unit-tested, not physically tested |
| Playwright Chromium | supported, live-tested | local HTTP page, form, upload, download, login/session, tab open/list/switch/close |
| Android ADB phone | delegated, unavailable here | existing bridge reports pairing steps; not paired |
| macOS/Linux desktop | not claimed | no real session tested |

## 10. Real evidence

All paths are relative to the repository root; structured JSON is the
authoritative evidence. Screenshots were saved but were not visually reviewed
during this closure.

### 10.1 Desktop application workflow

`tmp/phase29/desktop_evidence.json`:

- a **new Notepad window** appeared;
- typed text was read back byte-for-byte (45-byte marker);
- a fresh screenshot was captured (**4,631,166 bytes**);
- `Ctrl+S` saved `tmp/phase29/phase29_work.txt` with matching content;
- the save dialog was detected (`dialogs: ["Save as"]`) and dismissed;
- Calculator opened and was controlled;
- Notepad focus, minimize and maximize all succeeded;
- the applications were closed at the end.

### 10.2 Browser UI, tabs, upload/download and session

`tmp/phase29/browser_files_evidence.json`:

- local page `Phase 29 Files` loaded with HTTP 200;
- upload input received `phase29_upload.txt`; the page read back
  `phase29_upload.txt|NovaControl Phase 29 upload payload`;
- the download control wrote
  `tmp/phase29/phase29_download.txt` (**38 bytes**, content
  `NovaControl Phase 29 download payload\n`);
- the disposable login completed, `/private` returned HTTP 200, and the
  cookie-protected page read `Welcome phase29-session`;
- tab operations: a second tab opened, the tab list reported 2, the context
  switched to index 0, index 1 was closed, and the run ended with one active tab;
- the browser closed cleanly.

Earlier browser evidence also covers navigation, a form submission
(`Submitted NovaControl Phase 29`), scrolling, back and re-navigation.

### 10.3 Closed loop and persistence

`tmp/phase29/loop_evidence.json`:

- objective: `open calculator`;
- plan token present;
- run status: `completed`;
- `open_application` output: `Window appeared: Calculator`;
- 5 observations; final verification **PASS**;
- the persisted history survived an application restart.

### 10.4 Cancellation and file workflow

`tmp/phase29/controls_evidence.json`:

- emergency stop returned true; status `cancelled`; **0 actions**;
- input was released; Calculator was absent before and after;
- the dedicated file workflow created 3 files, listed them, moved both `.txt`
  files into a new `archive` folder, preserved SHA-256, left `keep.md` in place,
  and verified the final tree.

### 10.5 Performance

Measured on this machine during the live probes:

| Measurement | Value |
|---|---:|
| capture median | 575.0 ms |
| OCR median | 1,242.5 ms |
| full observation median | 2,287.2 ms |
| verification median | 0.0707 ms |
| Calculator launch | 2,773.9 ms |
| process RSS | 105.4 MB |
| system CPU sample | 42.8% |
| target window identified | true |
| 30-observation soak median | 2,268.7 ms |
| 30-observation soak maximum | 7,164.9 ms |
| soak RSS start → end / peak | 101.4 → 55.6 / 104.1 MB |
| soak failures | 0 of 30 |

These are usable interactive timings, not an instant UI. The CPU sample is
system-wide and high because OCR runs on the CPU; it is reported rather than
hidden.

### 10.6 Bounded soak

`tmp/phase29/soak_evidence.json` records 30 consecutive live observations
through `ComputerOperator.observe(include_semantic=False)` with no actions
executed:

- 30 of 30 observations completed, 0 failures;
- elapsed 83.37 s (median 2,268.7 ms, maximum 7,164.9 ms);
- RSS started at 101.4 MB, peaked at 104.1 MB and ended at 55.6 MB — no growth;
- 30 evidence observations were persisted, the last observation source was
  `window_details+browser_dom+screen_text`, and no task remained active after
  the loop.

This is a bounded stability run, not a multi-hour soak. The multi-hour scenario
is recorded as unverified in the acceptance matrix.

### 10.7 Live unresponsive-window observation

`tmp/phase29/unresponsive_evidence.json` records a disposable native Win32
window with no message loop. Through the real application perception path:

- the window was visible and `window_details` reported `responding: false`;
- `ComputerOperator.observe` reported `has_unresponsive_window: true` and the
  title in `unresponsive_windows`;
- the matching structured element carried `responding: false`;
- after the disposable process was terminated, the window disappeared and the
  next observation reported an empty `unresponsive_windows` list.

This is a live unresponsive-window test, not a forced application crash.

### 10.8 Live system-settings workflow

`tmp/phase29/settings_evidence.json` records the token-gated objective
`open settings`:

- the plan minted a single-use token;
- the run executed `open_application` with target `settings` and verified
  `Window appeared: Settings`;
- 5 observations were captured and final verification passed;
- the Settings window was no longer present in the post-run window list, so the
  cleanup close was a no-op (nothing to close), not a workflow failure.

The probe was run with `NOVACONTROL_DISABLE_OLLAMA=1` so an optional local model
could not distort the desktop-workflow timing. This closes the system-settings
portion of the system-workflow requirement; a live development-environment
workflow is still unverified.

## 11. Tests and static checks

Exact focused test command:

```bash
PYTHONPATH=src python -m pytest \
  tests/test_phase29_computer.py tests/test_agentcore.py tests/test_browser.py \
  tests/test_desktop.py tests/test_web_platform.py tests/test_integrations_llm.py -q
```

Result:

```text
298 passed, 473 subtests passed, 19 warnings in 53.11s
```

The Phase 29 suite itself collects **22 tests**. It includes the new
unresponsive-window, display-metadata, browser-tab, browser-approval, failure
history, final-verification, interpreter-timeout and redaction tests. The
unresponsive/display unit rows inject probes; the separate live probe above
uses a real hung window. The tab test is live-dispatch coverage, not a claim
that a physical second monitor was induced.
No full repository suite was run, per the focused-verification policy.

Exact targeted mypy command:

```bash
PYTHONPATH=src python -m mypy \
  src/novacontrol/computer src/novacontrol/agentcore/actions.py \
  src/novacontrol/agentcore/orchestrator.py src/novacontrol/agentcore/interpreter.py \
  src/novacontrol/agentcore/perception.py src/novacontrol/agentcore/ui_state.py \
  src/novacontrol/desktop/controller.py src/novacontrol/desktop/vision.py \
  src/novacontrol/browser/controller.py src/novacontrol/browser/models.py
```

Result:

```text
Success: no issues found in 15 source files
```

Full mypy (both views) still reports **7 pre-existing errors in 3 files**:
`src/novacontrol/novaforge/pilot.py` (4), `novaforge/overlay.py` (2) and
`src/novacontrol/application.py:5568` (1 vision-provider call). The Phase 29
`agentcore/actions.py` error was fixed; those seven files are outside this
phase's changes. They are reported, not suppressed.

API reference:

```text
docs/API.md: in sync
```

`docs/API.md` now contains **405 routes**, including the nine `/computer/*`
routes. `git diff --check` is clean after line-ending normalization; the only
warnings are the repository's existing LF/CRLF notices.

## 12. Acceptance matrix

Legend: **PASS** = implemented and verified with direct evidence in this
session. **PARTIAL** = implemented or unit-verified, but at least one mandatory
real-world scenario has not been exercised. **BLOCKED** = the scenario needs
hardware or external state this session could not induce.

| # | Requirement (spec section) | Verdict | Evidence / remaining gap |
|---|---|---|---|
| 1 | §2 perception: capture, windows, UI elements, OCR, coordinates, diffs, loading/error states, contextual state | **PASS** | live window/OCR/capture evidence; 30-observation soak; live native hung-window observation |
| 2 | §2 multiple resolutions and scaling configurations | **PARTIAL** | display bounds, per-monitor geometry and DPI caveats are read live; only one 2880×1800 @2× display existed and no resolution change was induced |
| 3 | §3 mouse: move, single/double/right click, drag, scroll, button state | **PASS** | controller tests + live clicks/scroll in desktop and browser probes |
| 4 | §3 keyboard: typing, shortcuts, combinations, selection, copy/paste, key release | **PASS** | live typing/Ctrl+S and input-release evidence; regression tests |
| 5 | §3 focus, target verification, no wrong-application input, cancellation | **PASS** | foreground checks, launch-verification tests, live cancellation with 0 actions |
| 6 | §4 application/window management, dialogs, crash/unresponsiveness detection | **PARTIAL** | live launch/focus/minimize/maximize/Save As; live native hung-window detection; no forced live crash |
| 7 | §5 browser navigation, forms, clicks, submission, scroll, inspection, dialogs | **PASS** | live local page navigation, form submission, scroll, back |
| 8 | §5 browser tabs/windows | **PASS** | live tab open/list/switch/close evidence (`browser_files_evidence.json`) |
| 9 | §5 upload/download and session handling without bypassing auth | **PASS** | live upload read-back, 38-byte download, disposable cookie session |
| 10 | §6 closed observe→understand→plan→act→observe→verify→adapt loop | **PASS** | `loop_evidence.json`: 5 observations, new-window verification, final PASS |
| 11 | §7 file/document workflows | **PASS** | live Notepad save + 3-file archive workflow with SHA-256 preserved |
| 12 | §7 development/system workflows | **PARTIAL** | live token-gated Settings workflow PASS; a live development-environment workflow was not performed |
| 13 | §8 contextual references and ambiguity handling | **PASS** | `resolve_reference` unresolved/ambiguous tests; live context-backed plans |
| 14 | §9 integration with existing agent, verification, recovery and learning architecture | **PASS** | one `AgenticOrchestrator` loop; Phase 27 learning/evaluation test; no parallel agent loop |
| 15 | §10 device abstraction and capability registry | **PASS** | desktop/browser/phone registry with honest availability; phone not paired |
| 16 | §10 multiple monitors | **PARTIAL** | two-display registry geometry and display metadata unit-tested; only one physical monitor attached |
| 17 | §11 permissions, risk classes, approvals, audit, emergency stop | **PASS** | token gate tests, risk classification, shared audit, live stop |
| 18 | §11 sensitive/destructive safeguards and credential handling | **PASS** | sensitive/destructive approval gates, redaction tests, no elevation |
| 19 | §12 recovery: dialogs, focus change, delayed response, OCR/ambiguous/input failures, interruption | **PASS** | Save As detection, focus verification, recovery tests, live cancellation |
| 20 | §12 recovery: unresponsive application / forced crash | **PARTIAL** | live native hung-window detection through production observe; no forced process crash was induced |
| 21 | §12 recovery: resolution change, system sleep, restart | **PARTIAL** | restart persistence is live-verified; resolution change and OS sleep were not induced |
| 22 | §13 persistence, history, approvals, verification results, Phase 27 learning | **PASS** | history restored after restart; redacted/bounded; learning test |
| 23 | §14 phase-specific tests | **PASS** | 22 Phase 29 tests; final focused group 298 passed / 473 subtests |
| 24 | §14 regression, static analysis, API sync | **PASS** | 298 passed / 473 subtests; targeted mypy 15 files clean; `docs/API.md` in sync at 405 routes |
| 25 | §15 real application verification (all ten scenarios) | **PARTIAL** | nine scenarios have live evidence; a genuinely unexpected dialog was not induced (the Save As dialog was expected by the workflow) |
| 26 | §16 performance and resource measurements | **PASS** | capture/OCR/observation/verification/launch metrics + bounded soak |
| 27 | §16 long-running stability | **PARTIAL** | 30 observations / 83.37 s with flat RSS; a multi-hour soak was not run |
| 28 | §17 documentation | **PASS** | this report, `COMPUTER_OPERATION.md`, README, STATUS and API reference updated |
| 29 | §18 acceptance criteria 1–15 overall | **PARTIAL** | every core capability is present; rows 2, 6, 12, 16, 20, 21, 25 and 27 are not fully verified |

## 13. Gate matrix (updated universal acceptance policy)

| Gate | Verdict | Evidence / remaining gap |
|---|---|---|
| Gate 1 — functional completeness | **PARTIAL** | every primary computer-operation capability works with live evidence; development workflow and physically-induced resilience scenarios remain unverified |
| Gate 2 — integration completeness | **PASS** | one orchestrator/reliability/permission/audit/learning stack reused; no duplicate architecture |
| Gate 3 — testing completeness | **PARTIAL** | 298 focused tests / 473 subtests pass; mandatory real-world resilience scenarios were not exercised |
| Gate 4 — safety and reliability | **PARTIAL** | safety, cancellation, recovery, redaction and unresponsive-window detection verified; forced crash, resolution and sleep scenarios lack live evidence |
| Gate 5 — documentation and evidence | **PASS** | requirement-by-requirement matrix, evidence files, limitations and API reference all current |

## 14. Known limitations

1. Only Windows 11 was exercised on real hardware; macOS and Linux desktop
   sessions are not claimed.
2. Only one physical display was attached. Multi-monitor geometry is probed and
   unit-tested, but a two-monitor capture/input run was not performed. A live
   resolution/scaling change was likewise not induced.
3. The natural-language planner covers application/window, typing, saving and
   browser steps. File list/read/write/organize are exposed through the desktop
   controller and were live-verified directly, but a free-form “organize this
   directory” objective and a live development-environment workflow are not
   mapped onto those primitives by the planner. A live Settings workflow was
   verified through the token path.
4. The bounded soak ran 30 live observations over 83.37 s with no RSS growth.
   No multi-hour soak, forced application crash, OS sleep or physical resolution
   change was run. Unresponsive-window detection is now live-verified; multi-
   display behaviour is unit-tested rather than physically induced.
5. The Save As dialog was detected and handled, but a genuinely unexpected
   dialog interrupting an unrelated step was not induced.
6. OCR accuracy depends on the installed Windows OCR engine and display scaling;
   no statistical accuracy benchmark across applications is claimed.
7. Browser auth/session was verified against a disposable local page. External
   providers' credentials, CAPTCHAs and anti-bot controls were not used and are
   never bypassed.
8. The full repository suite was not run; focused groups and relevant
   integration/regression tests were run as reported.
9. Optional LLM enrichment is bounded at 8 s, but a local model that hangs can
   still leave a transport worker blocked until the configured 600 s socket
   timeout if recovery research calls it. The live Settings probe disabled
   Ollama so the optional model could not distort the desktop-workflow result.

## 15. Verdict

**PARTIAL — the phase remains open.** Every core Phase 29 capability is
implemented and has direct live evidence: verified desktop operation,
multi-application control, browser navigation/form/upload/download/session/tab
workflows, a closed observation-and-verification loop, a cancellation that
executed nothing, a file workflow with hash preservation, persisted history,
unresponsive-window and display perception, and measured performance including
a bounded soak.

Under the updated universal acceptance policy, Phase 29 cannot be declared
PASS while mandatory real-world scenarios remain unverified. The remaining
closure work is:

1. a physical second monitor and a live resolution/scaling change;
2. a forced application crash during a workflow (unresponsive-window detection
   is now live-verified);
3. OS sleep/resume followed by a recovery observation;
4. a genuinely unexpected dialog that interrupts an unrelated step;
5. a live development-environment workflow (the system-settings workflow is now
   live-verified);
6. a multi-hour stability soak.

The focused test, mypy, API and diff gates pass for the changed surface, and
nothing was committed or pushed.

## 16. Group 1 audit addendum (2026-11-26)

Two of the unverified scenarios in §14 now have live evidence, produced by
`tmp/group1/verify_resilience.py` → `tmp/group1/resilience_evidence.json`
(**14 passed, 0 failed**):

* **A genuinely unexpected dialog** (§14.5, matrix row 25). A real Win32 message
  box (`MessageBoxW`, class `#32770`, title `NovaGroup1UnexpectedDialog`) was
  raised while unrelated applications were open. Production
  `ComputerOperator.observe` listed it in `windows`, reported `has_dialog: true`,
  put the title in `dialogs`, and still carried a screenshot hash.
* **A forced application crash during a workflow** (§14.4, matrix row 20,
  partially). The objective `close the NovaGroup1Probe window` was planned and
  approved; the target process was then killed before execution. The run
  reported `failed` — "1 action(s) executed, 1 recovery attempt(s). No observed
  evidence supports goal completion." — and the history recorded `failed`.
  No success was fabricated against a dead window.

Still open, with reasons: a crash **mid-action** (target dies while input is
being delivered), a physical second monitor, a live resolution/scaling change,
OS sleep/resume, a multi-hour soak, and a live development-environment workflow.
None of them is claimed as passing.

Full record: `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` defects
D3, D4, D6.

### Mid-action crash and a grounding defect (2026-11-26, Group 1 DF-6 pass)

* **Mid-action crash verified on the token-gated input path.** A disposable window
  was brought to the foreground and confirmed there, then a `TYPE_TEXT` action
  typed into it; the window wrote a marker on the paste's first key
  (`keysym=Control_L`) and exited with code 3 without unwinding. The action
  failed with `VerificationError: Typed text was not verified: no window is
  focused — the paste had nowhere to land.`, claimed no verified success, and was
  recorded once on the audit trail. **8/8 checks**, evidence
  `tmp/group1/midaction_controller_evidence_20261126T232731.json`.
* **D12 found — a window-named objective was grounded on a different, live
  application.** Running `plan_computer_task` / `execute_computer_task` with the
  objective `type the phrase hello world into the NovaGroup1MidAction window`
  made perception select the first titled window the OS probe returned
  (`Realtek Audio Console`), plan a `type` step targeting it, deliver the paste
  while it was foreground, and report that step `success: true, verified: true`.
  Only the final goal verification failed. `agentcore/perception.py` sets
  `window_title` to the first titled window (`window_title = window_title or
  title`), and `TYPE_TEXT`'s `expect` is derived from that same window, so the
  per-action verifier confirms the wrong target.
  **Unresolved** — the fix is a grounding-policy decision (goal-named window,
  foreground-only, or refuse-and-clarify). Evidence:
  `tmp/group1/midaction_crash_evidence_20261126T232321.json`.
  Consequence at the time: row 5 ("no wrong-app input") was no longer PASS for
  window-named objectives, and the operator-loop mid-action variant was blocked.
  **D12 was remediated and closed in the fourth pass (see below).**
### D12 remediation and closure (2026-11-26, fourth pass)

* **Policy:** the input target must be explicitly grounded in the objective and
  independently identified in the current OS window observations. `agentcore/targeting.py`
  extracts the named window, binds it to exactly one observed window (title, handle,
  pid), and refuses on absent/ambiguous/similarly-named/title-only matches. The
  first-titled fallback is gone for objective-bound runs.
* **Identity end to end:** the bound `(handle, pid)` is carried on
  `state.interpretation["target_identity"]`, into `TYPE_TEXT`'s
  `expect_handle`/`expect_pid`, into the paste-time foreground check, and into the
  per-step `Expectation`. `_verify_type_windows` now reports the foreground handle
  and pid, and a mismatch is a verification failure, not a success.
* **Verification honesty:** the final verdict no longer overwrites per-step evidence,
  and an input step whose effect was never observed makes the final verdict `fail`.
* **Focused tests:** 28 in `tests/test_d12_targeting.py`, covering every mandated
  negative path, including a fixture reproduction of the Realtek scenario with no real
  input. Phase 29/30/31 plus agent tests: **175 passed**. mypy clean (23 files).
* **Live gate:** `tmp/group1/verify_d12_live.py` — an absent target is refused with
  zero input; a disposable `NovaD12Target` window (pid/handle verified) received the
  text (`the phrase d12 isolated check`), the delivery-time foreground identity matched,
  no other application was targeted, and the run reported `failed` rather than claiming
  an unobserved success. **18/18**, `tmp/group1/d12_live_evidence_20261126T235325.json`.
* **DF-6 operator loop:** the mid-action crash now passes **16/16** — the target died
  during the paste (`keysym=Control_L`, exit 3), the action failed on the identity
  mismatch (expected handle/pid vs the foreground after death), the run and its durable
  record are `failed`, recovery is recorded, the token cannot be replayed, and the
  interrupted objective is recorded exactly once.
  `tmp/group1/midaction_crash_evidence_20261126T235403.json`.
* **Correction (2026-11-27, §18 below).** The DF-6 operator-loop PASS recorded in this
  section is retracted: the run's own record shows the paste's tail observed in an
  unrelated window and a recovery strategy of `retry`. The current-tree run keeps
  row 20's operator-loop half **PARTIAL**.
* **Row 5 is PASS:** "no wrong-app input" holds for window-named objectives, proven by
  the fixture regression and the isolated live run. Row 20's operator-loop half was
  recorded PASS here; **retracted 2026-11-27 — PARTIAL, see §18**.

## 17. LIM-2 remediation addendum (2026-11-26)

LIM-2 (recovery mis-diagnosis and potential duplicate input) is remediated on the Phase 29
operator path.

* **Root cause.** `RecoveryEngine._find_relocation` scored interactive elements by word
  overlap with the failed target over `"{text} {selector}"`, so an element whose text was
  the target scored full overlap and was offered as its "similar replacement". The engine
  therefore reported a present window as gone ("Target 'NovaD12Target' is gone but a similar
  element 'NovaD12Target' exists") and `_recover` replanned a click on that same element —
  turning a retried `type` step into a click.
* **Reachability.** The duplicate was theoretical in the shipped orchestrator: `_next_step`
  retires plan entries whose id already has an action record and `replan` reuses the failed
  id, so the replanned RETRY/RELOCATE never re-dispatched (recovery was inert). The
  diagnosis was false, the recovery object named the target as its own replacement, and any
  dispatcher of the replanned plan — or one id change — would have produced duplicate or
  unintended input. Both are now closed.
* **Behaviour.** New `RecoveryStrategy.RECHECK`; `input_delivery_state()` classifies
  delivered / not-delivered / uncertain against an explicit non-idempotent action set;
  `_ground_target()` distinguishes present-unchanged, present-changed, absent, ambiguous and
  unknown using the observation's binding plus the objective's bound identity. Present but
  unchanged with an unobserved effect is reported as exactly that; changed identity and
  ambiguity abort; possibly-delivered input is only re-observed and re-verified, with no
  dispatch and no second `ActionRecord`. A not-delivered input failure may retry the same
  step through the existing target and permission gates, as a new attempt id, keeping the
  original action's id and record. D12 identity checks and DF-6 behaviour are untouched.
* **Verification.** `tests/test_lim2_recovery.py` (new, 21 tests) plus D12, AgentCore,
  Phase 29, 30 and 31: **196 passed**, exit 0. `mypy src/novacontrol/agentcore
  src/novacontrol/desktop` clean (23 files). New test file ruff-clean; `git diff --check`
  clean. D12 live evidence (18/18) and DF-6 operator-loop evidence (16/16) were not
  modified.
* **Limitations.** Delivery classification depends on the recorded outcome/detail, so an
  action kind must be classified explicitly; `RECHECK` cannot pass without an observable
  expectation (recovery never converts an unverified action into success); LIM-1 is
  unchanged; no live desktop run was performed in this pass. Phase 29 remains PARTIAL for
  its environmental rows; Group 1 remains NOT CLOSED.

## 18. DF-6 operator-loop current-tree addendum (2026-11-27)

The authorized attended live run was executed once on the current tree (`main` / `afa5ff6`,
uncommitted worktree preserved). Command:
`PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_midaction_crash.py`.
Evidence `tmp/group1/midaction_crash_evidence_20261127T010222.json` (sha256
`2dfb0a4e…e31cf34`), 19/19 harness checks as written, exit 0, fresh data directory.

* **Passes on the current tree.** The disposable `NovaGroup1MidAction` window received the
  paste's first key (`event_type=2`, `keysym=Control_L`, pid 33624) and exited with code 3
  mid-delivery; the run status and the durable record are `failed`; no executed action
  claims success and the final verdict is `fail`; exactly one typing attempt was recorded;
  recovery classified the delivery as `uncertain` and chose `recheck` (never `retry` or
  `relocate`), re-observing and re-verifying instead of replaying; the dead window is gone
  from the next observation; a fresh instance restores the failed record; the objective is
  recorded once; the approval token cannot be replayed. This is also the **live confirmation
  of LIM-2** on the operator path.
* **Fails two mandated assertions.** The `type` path pastes with
  `SendKeys::SendWait('^v')` — Ctrl-down, V-down, V-up, Ctrl-up. The target died on the
  first event, so the rest of the chord went to the window that took the foreground: pid
  4192, recorded `expected [7670084, 33624]` vs `observed [15729620, 4192]`,
  `identity_match = false`. The controller then reported `VerificationError` and its final
  verification says the target is `absent` and "The input cannot be attributed to the
  intended application". Fail-closed is correct; containment is not. Recorded as
  **DF-6-L1** (structural limitation of chord injection into a foreground window that
  vanishes mid-chord; options in the Group 1 report §N.3, none implemented).
* **Correction.** The operator-loop PASS recorded in §16 of this report is **retracted**.
  The 2026-11-26 evidence has the same unrelated foreground and, additionally, a recovery
  strategy of `retry` — which the LIM-2 acceptance forbids for possibly-delivered input.
* **Row 20, operator-loop half: PARTIAL.** The controller-only half (8/8, §16) is unchanged.
* **Harness defect.** M2.8 read `output["verification"]`, which a failed typing record does
  not have, so it passed by finding nothing. It now also parses the identity pair from the
  failure message and **fails** when an input action carries no identity evidence. It was
  not re-run (no rerun authorized); a corrected M2.8 reports FAIL for this scenario.
* **Containment follow-up (offline, no input).** The Group 1 report §O traces the tail to
  per-event dispatch by the input system, finds no Windows cancellation for in-flight
  events, and rules out hidden desktops (`SendInput` fails off the input desktop) and
  keyboard `PostMessage` as replacements. Guaranteed containment is scoped to UI
  Automation patterns or a dedicated session/VM; the interactive keyboard path is
  best-effort. Row 20 stays **PARTIAL**.
* **Checks.** LIM-2, D12, AgentCore and Phase 29/30/31 suites: **200 passed**, exit 0;
  `mypy src/novacontrol/agentcore src/novacontrol/desktop` clean (23 files);
  `git diff --check` clean. No production source changed.
* **Containment implemented (2026-11-27, offline — no input, no harness run).** The paste
  chord is no longer a single `SendKeys('^v')` call: `TYPE_TEXT` dispatches the four edges
  individually, each after its own foreground identity check, withholding the remainder on
  any mismatch; a refused or aborted paste records `bound_identity`,
  `destination_identity`, `events_after_last_identity_check`, `stuck_keys` and the clipboard
  result, and the orchestrator carries that record into `ActionOutcome.output` (the evidence
  M2.8b needed). The clipboard is restored unless another application has taken it, UIPI is
  preflighted, and two guaranteed profiles exist — UI Automation (element-scoped patterns,
  zero keyboard events, unsupported controls refused) and an isolated session/VM boundary
  that reports `no_session_transport` on this host. 43 dispatch-ledger tests (the twelve
  scenarios plus UIA, session, clipboard and integrity suites) and **399 passed** across the
  DF-6/desktop/DF-6-input/D12/LIM-2/AgentCore/Phase 29/30/31 suites; mypy and ruff clean on
  the new modules. The
  interactive keyboard profile stays **BEST_EFFORT** and row 20 stays **PARTIAL**; the UIA
  profile was live-verified on 2026-11-28 in a separately authorised run (23/23 against a
  disposable probe, zero key/mouse events), while the isolated session remains
  `no_session_transport`. Reports:
  [DF6_CONTAINMENT_IMPLEMENTATION_REPORT.md](DF6_CONTAINMENT_IMPLEMENTATION_REPORT.md),
  [DF6_DISPATCH_LEDGER_TEST_REPORT.md](DF6_DISPATCH_LEDGER_TEST_REPORT.md),
  [DF6_WINDOWS_SESSION_ARCHITECTURE.md](DF6_WINDOWS_SESSION_ARCHITECTURE.md).
* **Live-UIA preparation and test-suite recovery (2026-11-27, second pass — no live input).**
  The UIA live run is now prepared rather than merely planned: a disposable WinForms probe
  (`tmp/group1/uia_probe_app.ps1`) with a writable control, an invokable control, a
  pattern-less panel, a disabled control and two controls sharing one AutomationId, plus a
  harness (`tmp/group1/verify_uia_live.py`) implementing the 14 checks behind an explicit
  `--authorize-live` gate; its default mode prints the plan and creates nothing. Measured
  before asking for authorization: `CoCreateInstance(CLSID_CUIAutomation)` returns `S_OK` and
  the production `ComtypesUiaBackend` reports available with `verified_live: false` — so the
  transport is reachable, and **no element was acted on**. The suite's collection failure was
  a public-facade regression (the Phase 31 rewrite dropped `SkillInvocation`, `SkillResult`,
  `SkillSchema`, `SkillStatus` from `skills/__init__.py` while `skills/models.py` still
  advertises them); the re-exports were restored and 6,712 tests now collect. Running the
  suite one file at a time then exposed a genuine capture-worker defect — a cancellation
  **during startup** left the host's `ready WxH` banner unread and the next exchange read it
  as its frame reply — fixed at the cause plus a fail-closed reply parser, with two new
  regression tests and a mutation check. Row 20 stays **PARTIAL**; the open gate is the
  operator authorization for the disposable-probe run, and a session/VM transport for
  arbitrary applications. Reports: [DF6_LIVE_UIA_VERIFICATION_REPORT.md](DF6_LIVE_UIA_VERIFICATION_REPORT.md),
  [DF6_TEST_SUITE_RECOVERY_REPORT.md](DF6_TEST_SUITE_RECOVERY_REPORT.md),
  [DF6_SESSION_TRANSPORT_READINESS.md](DF6_SESSION_TRANSPORT_READINESS.md).
