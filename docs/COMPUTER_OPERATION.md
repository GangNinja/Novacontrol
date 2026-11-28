# Computer Operation (Phase 29)

NovaControl's computer operator is a closed-loop desktop and browser capability
built on the existing perception, planning, execution, verification, approval,
recovery, learning and persistence layers. It is not a coordinate macro
recorder and it does not run a second agent loop.

## 1. What the operator does

For one objective at a time it:

1. observes the foreground environment and visible windows;
2. interprets the objective with the existing `TaskInterpreter`;
3. builds a plan with the existing `AdaptivePlanner`;
4. acts only through approval-aware controllers;
5. observes again after each action;
6. verifies the observed change against the step's expectation;
7. replans or recovers when the state diverges;
8. records the task, approvals and evidence in bounded history.

The live path is:

```text
ComputerOperator
  -> AgenticOrchestrator.run_task
       -> UiPerceptionEngine.observe
       -> AdaptivePlanner / TaskInterpreter
       -> ActionEngine
            -> DesktopAutomationController -> LocalDesktopRunner
            -> BrowserAutomationController -> PlaywrightBrowserRunner
       -> Verifier
       -> RecoveryEngine
       -> Phase 27 learning capture
```

`DesktopAutomationController.execute_action` remains the only door for desktop
input. The raw runner is retained for test seams and non-controller callers, not
for an operator task.

## 2. Package layout

| Module | Responsibility |
|---|---|
| `novacontrol/computer/models.py` | device, operation, capability and support models; `classify_operation` |
| `novacontrol/computer/devices.py` | live desktop/display/input/browser/phone discovery and capability registry |
| `novacontrol/computer/context.py` | contextual reference resolution against observation and history |
| `novacontrol/computer/history.py` | bounded, redacted, persisted operation history |
| `novacontrol/computer/operator.py` | single-task lifecycle, cancellation, emergency stop, planning, observation and history |
| `novacontrol/agentcore/perception.py` | multi-layer UI observation, OCR coordinates, dialogs/loading/error signals, frame hash |
| `novacontrol/agentcore/ui_state.py` | observed state and change comparison |
| `novacontrol/agentcore/actions.py` | semantic-first actions over desktop and browser surfaces |
| `novacontrol/agentcore/orchestrator.py` | the observe → plan → act → observe → verify → recover loop |
| `novacontrol/desktop/controller.py` | approval, audit, input guard, windows, dialogs and desktop primitives |
| `novacontrol/browser/controller.py` | approval, audit and Playwright browser actions |

## 3. Perception

`UiPerceptionEngine` fuses:

- the OS window list through `window_details`, including top-level dialog
  surfaces that a process main-window enumeration does not show, per-window
  attributes (process, bounds, foreground, minimized/maximized, responding);
- unresponsive windows through the Win32 `IsHungAppWindow` probe, exposed as
  `unresponsive_windows` / `has_unresponsive_window`;
- live display bounds and work areas from the monitor probe, including
  multi-display geometry when more than one monitor is attached;
- a fresh, approval-gated screenshot (never an old file on disk);
- OCR words with screen coordinates through the existing vision-guided reader;
- an optional genuinely-capable vision model;
- loading, error and dialog signals derived from real text and window titles;
- a screenshot hash and a structured diff between observations.

Coordinates are reported in the physical-pixel space of the capture/input layer.
An element is clicked only after its owning window is focused and its observed
point is checked against that window's geometry. A label that was not observed
is sent to the existing OCR/vision locator, which fails honestly rather than
clicking a guessed point.

## 4. Desktop actions

The desktop controller exposes:

- mouse move, click (left/right/middle; 1–3 clicks), scroll, drag, button
  press/release;
- typing, keyboard shortcuts, key down/up, copy/paste/select-all;
- window focus, minimize, maximize and graceful close;
- open application/folder, app deep links and web search;
- screenshots, file list/read/write and dedicated-directory organization;
- Save As handling with a foreground/top-level dialog check;
- vision-guided click with pixel-difference verification.

Input is validated against the target window before it is sent. A launch is
reported successful only when a new window appears. Multi-word recipients and
messaging compose routes remain in the existing JARVIS/messaging layer; the
computer operator does not bypass their approval flow.

### Input cleanup

`stop_input()` releases held keys and buttons through the runner's input guard.
The operator's `cancel()` and `emergency_stop()` call that seam even if no task
is active, so a stop request cannot leave a key or button held.

## 5. Browser actions

The Playwright runner supports navigation, click, type, form fill/submit,
scroll, back, key press, extract, screenshot, upload, download and real tab
operations (`tab_new`, `tab_list`, `tab_switch`, `tab_close`). Uploads use the
page's file input; downloads use Playwright's download event and save the file
to the requested path, then verify that a non-empty file exists. Tab creation
and closing are approval- and audit-tracked like other browser actions.

Browser actions that navigate, type, fill, upload or download go through the
browser controller's approval layer and audit log. Authentication, CAPTCHAs and
site access controls are never bypassed. Sensitive submissions remain subject
to the existing authorization rules.

## 6. Context and references

`resolve_reference()` resolves phrases such as:

- “click the Save button” — only a single observed element with that label;
- “continue from where I stopped” — the last unfinished persisted objective;
- “the file I was working on” — the recorded operation objective, or an honest
  request to name the file;
- “switch to the browser” — a visible browser window, not an assumed one;
- “go back to the previous page” — browser history;
- “find the error in this window” — only actual error text in the current
  observation.

An absent element is `unresolved`; multiple matches are `ambiguous`. Neither is
silently turned into a guess.

## 7. Safety and permissions

`classify_operation` distinguishes:

| Class | Examples | Default |
|---|---|---|
| `read_only` | observe, inspect, screenshot | observation |
| `reversible` | open an app, focus, type into a draft | runnable under the task token |
| `sensitive` | submit, send, publish, account change | requires explicit approval |
| `destructive` | delete, move/rename, overwrite, shutdown | requires explicit approval |
| `privileged` | install, driver, elevated system change | requires explicit approval |

Every `/computer/run` requires a server-minted, single-use, expiring token tied
to the exact objective. `POST /computer/plan` mints it; the token is consumed
before any input. A replayed or mismatched token is refused. Destructive
objectives are blocked before input unless the caller passed the approval gate.
No operation elevates privileges or writes outside the caller's requested path.

While a run is in flight, the operator installs the approved gateway on both
the desktop and browser controllers, so each action still passes the
controller's permission and audit layers. Every browser action in the loop
travels through `BrowserAutomationController`; a denied action never reaches the
raw Playwright runner. The vision capture uses a per-action approval override
rather than replacing the controller's gateway, so a capture cannot reset an
active run to deny-by-default.

Optional LLM enrichment of the task interpretation is bounded at 8 seconds and
falls back to deterministic heuristics. If the orchestrator raises, the
operator records a failed history entry and restores both gateways. A plan that
ends without final verification is failed, never completed from action success
alone.

History redacts passwords, tokens, API keys, cookies, credentials, URL user-info
and email addresses. It stores observation metadata, not screenshots, clipboard
contents or page bodies.

## 8. History and persistence

`ComputerOperationHistory` uses the shared `JsonStateStore` namespace
`computer_history`, bounded to 50 records. A record carries:

- objective, status, task id, timestamps and duration;
- environment/device report;
- planned steps and executed actions with approval ids;
- observation summaries and safety signals;
- per-step and final verification;
- recoveries, approvals, errors and lessons;
- a summary and blocked reason.

Writes are best-effort: a history failure never changes the result of the action
that produced it. Restart restores the bounded records; it does not replay a
task. The API exposes list and clear operations.

## 9. API

| Method | Path | Purpose |
|---|---|---|
| GET | `/computer/status` | operator, device and history status |
| GET | `/computer/devices` | live device/capability report |
| POST | `/computer/observe` | read-only observation |
| POST | `/computer/plan` | plan and mint the run token |
| POST | `/computer/run` | execute a token-authorized objective |
| POST | `/computer/stop` | cancel the active task and release input |
| GET | `/computer/history` | bounded persisted history |
| POST | `/computer/history/clear` | clear operation history |
| POST | `/computer/reference` | resolve a contextual reference |

Example:

```bash
curl -s -X POST http://127.0.0.1:8000/computer/plan \
  -H 'content-type: application/json' \
  -d '{"objective":"open calculator"}'
# copy approval.token from the response
curl -s -X POST http://127.0.0.1:8000/computer/run \
  -H 'content-type: application/json' \
  -d '{"objective":"open calculator","approval_token":"..."}'
```

The API reference is generated by `python scripts/generate_api_reference.py`;
`--check` must report `in sync`.

## 10. Device support

| Device | Status | Evidence |
|---|---|---|
| Windows desktop | supported, live-tested | Windows 11, one 2880×1800 display at 2× scale; Notepad and Calculator |
| Keyboard/mouse | supported, live-tested | typing, shortcuts, focus, minimize/maximize, cancellation cleanup |
| Display enumeration | implemented | live OS monitor probe; only one display attached here |
| Playwright browser | supported, live-tested | local HTTP page; navigation, extraction, upload, download, tab open/list/switch/close |
| Android phone | delegated to the existing ADB bridge | not paired during this phase; registry reports unavailable with pairing steps |
| macOS/Linux desktop | not claimed | no real session was tested |

The device registry reports availability, connection state, required
permissions, supported operations and known limitations. It never calls an
installed Playwright package “connected” without a live browser.

## 11. Recovery and reliability

- Every action is surrounded by an observation before and after it. An
  unverified action is not treated as completed.
- A failed step enters the existing `RecoveryEngine`; destructive actions and
  missing dependencies are not retried.
- A new window must appear after an application launch; reusing a pre-existing
  window is not proof of a launch.
- A Save As path is typed only after the OS reports a save dialog. If the dialog
  never appears, the action refuses rather than typing a path into the document.
- A cancelled task stops before its next action; held input is released. The
  live cancellation probe executed zero actions and opened no application.
- Window geometry, visible dialogs and screenshot hashes are re-read for each
  observation; stale coordinates are not reused across an interface change.
- An unexpected dialog is visible in the observation’s `dialogs` signal and can
  be dismissed or handled before the next step.

## 12. Configuration and operation

There is no separate computer-operation service or port. The feature is built by
`NovaControlApplication` and served by the existing FastAPI app:

```bash
PYTHONPATH=src python -m uvicorn novacontrol.api.app:create_app \
  --factory --host 127.0.0.1 --port 8000
```

The one-active-task rule is enforced by the operator, not by the API process.
`/computer/stop` is the emergency stop. `status()` reports `closed_loop: true`
and the operator's history summary.

## 13. Verification commands

```bash
PYTHONPATH=src python -m pytest tests/test_phase29_computer.py tests/test_browser.py -q
PYTHONPATH=src python -m pytest \
  tests/test_phase29_computer.py tests/test_agentcore.py tests/test_desktop.py \
  tests/test_browser.py tests/test_reliability.py tests/test_bug_auto_resolve.py \
  tests/test_jarvis_desktop_vision_fixes.py \
  tests/test_multi_agent_collaboration.py tests/test_web_platform.py -q
PYTHONPATH=src python -m mypy src/novacontrol/computer \
  src/novacontrol/agentcore/actions.py src/novacontrol/agentcore/orchestrator.py \
  src/novacontrol/agentcore/perception.py src/novacontrol/agentcore/ui_state.py \
  src/novacontrol/desktop/controller.py src/novacontrol/desktop/vision.py \
  src/novacontrol/browser/controller.py src/novacontrol/browser/models.py
PYTHONPATH=src python scripts/generate_api_reference.py --check
```

Live probes and recorded evidence are under `tmp/phase29/`:

- `verify_desktop.py` / `desktop_evidence.json`;
- `verify_browser.py` / `browser_evidence.json`;
- `verify_browser_files.py` / `browser_files_evidence.json`;
- `verify_loop.py` / `loop_evidence.json`;
- `verify_controls.py` / `controls_evidence.json`;
- `verify_soak.py` / `soak_evidence.json` (30 live observations, 0 failures,
  flat RSS).

## 14. Known limitations

1. Only Windows 11 was exercised on real hardware; macOS and Linux desktop
   sessions are not claimed.
2. Only one physical monitor was attached. Multi-monitor geometry is probed and
   unit-tested, but a two-monitor capture/input run was not performed. A live
   resolution/scaling change was likewise not induced.
3. The agent's natural-language planner is strongest on application/window,
   typing, saving and browser steps. File list/read/write/organize are exposed
   through the existing desktop controller and were live-verified directly;
   a natural-language “organize this directory” objective is not mapped to those
   file primitives by the planner.
4. A bounded soak (30 live observations, 83.37 s, 0 failures, no RSS growth) was
   run. No multi-hour soak, OS sleep/resume or physical resolution change was
   performed. Restart persistence was verified at the application level.
5. Application unresponsiveness is detected live through `IsHungAppWindow`,
   real error/dialog text and window state; a forced live process crash during a
   workflow was not run. The Save As dialog was expected by its workflow, so a
   genuinely unexpected interrupting dialog is also unverified.
6. A live token-gated Settings workflow completed with five observations and a
   final verification PASS. A live development-environment workflow was not run.
7. Optional LLM enrichment is bounded, but a hung local model can still leave a
   recovery-research transport worker until its 600-second socket timeout; the
   live Settings probe disabled Ollama to remove that optional variable.
8. OCR accuracy depends on the installed Windows OCR engine and display
   scaling. No statistical accuracy benchmark across applications is claimed.
9. Browser upload/download was verified against a local HTTP page; third-party
   site authentication and anti-bot surfaces remain outside the operator's
   control and are never bypassed.
10. The full repository suite was not run, per the Phase 29 focused-verification
    policy. Phase-specific, modified-module, integration and relevant regression
    groups were run and are reported in `PHASE29_REPORT.md`.
