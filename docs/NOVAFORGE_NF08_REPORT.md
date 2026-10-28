# NovaForge NF-08 — observation, screen-text objectives and the closed loop

**Date:** 2026-10-21
**Verdict:** PASS WITH LIMITATIONS.

NF-07 ended with two named blockers: *no decision pipeline* (a person authored every
plan) and *only window-title objectives* (a real game rarely announces success in its
title). NF-08 delivers both in a form that can be checked rather than declared:

1. a **structured observation** that marks a stale, degraded or unreadable screen as
   non-actionable, and keeps measured evidence separate from inference;
2. a second **objective kind**, `ocr_text`, verified by reading text off a fresh frame
   of the target window through the Windows OCR engine the vision layer already uses;
3. a **bounded closed loop** that observes, asks an installed policy for one action,
   and executes it through the session's existing plan/digest/Guardian path.

The loop is a supervisor, not a second brain. It owns stopping, not acting: every
action still travels `plan_action` → `execute_action`, so the Guardian, the digest
binding, the action budget, target identity, foreground and the input guard apply
exactly as they do to a manual proposal.

---

## 1. What shipped

### `novaforge/observation.py`

One `GameObservation` is the only thing a decision may act on. It is a value:
timestamped, normalised, and explicit about what was measured versus what was inferred.

* `evidence` holds facts that were read (window title, frame path, OCR words);
  `interpretations` holds anything a rule or model concluded from them.
* Statuses are `observed | degraded | stale | unavailable`, and only `observed` is
  actionable. A **stale** frame is never used to act; an **unavailable** window is
  never reported as an empty screen.
* A window **title is text that was read**: an observation with a readable title and a
  fresh frame is actionable even when OCR recognises nothing, with the absent sources
  named in `uncertainty`. `degraded` means nothing could be interpreted at all.
* Every probe is awaited under its own timeout, and a timeout/exception becomes a
  stated uncertainty instead of a hang or a raise.
* `ScreenTextReader` adapts the application's OCR callable to the reader protocol;
  `None` from the engine becomes `OcrUnavailable` (engine missing), never an empty
  reading (screen blank).

A freshness bug was found while testing: a capture that claimed `fresh: true` while
reporting a 30 s age was accepted because the builder trusted the boolean. The
builder now requires BOTH the capture's freshness and its own age bound, so the age
limit is not decorative.

### `novaforge/objective.py` — `OcrTextVerifier`

`ocr_text` objectives accept `parameter`, `mode` (`contains | equals | absent`), an
optional `region` (`x,y,width,height` in **cropped-window** pixels) and an optional
`max_age_seconds`. The verifier:

* captures through the manager's existing capture seam and crops to the target window;
* refuses a reading that is not fresh, has no age, or is older than the objective's
  bound — `unknown`, never a failure;
* reports an unavailable engine as `unsupported`, and a raising/silent probe as
  `unknown`;
* applies the region to words before comparing, so "read the scoreboard" cannot be
  satisfied by text elsewhere in the window;
* **never invents a confidence.** Windows' OCR reports words but no per-word
  confidence. That is a missing measurement, not a zero: a reading with recognised
  words can still decide (the words are the measurement), while an EMPTY reading
  cannot prove `absent`, because "nothing came back" and "the read failed" look
  identical without a confidence. The verdict then carries `confidence: null` and
  says so in its evidence note.

`ObjectiveRegistry.availability()` now names the OCR verifier and its readiness, and
`NovaForgeManager.capabilities()` lists it among the ready kinds.

### `novaforge/policy.py` and `novaforge/closed_loop.py`

* `DecisionProposal` (renamed from an initial `ActionProposal` to avoid colliding with
  the digest-bound `actions.ActionProposal`) carries the action, its reason, the
  observation evidence it cites, its expected outcome, and whether it came from a
  model. A proposal without a reason is refused at construction.
* `HarnessReadyPolicy` is the one shipped policy: while the target's title lacks a
  marker it proposes one key tap; once the marker is present it proposes nothing and
  says the verifier should decide. After three identical observations it stops rather
  than pressing again. Its capability reports `model_backed: false` and states that
  it is not evidence of gameplay intelligence. `UnavailablePolicy` is the honest
  refusal for a build with no policy.
* `ClosedLoopRunner` records every decision and stop: cancellation, deadline, budget,
  a target-session mismatch, a stale/degraded/unavailable observation, a refused
  plan, an action that did not execute, a verifier timeout, and a policy with nothing
  left to say. `completed` requires `verified_success`; a `verified_failure` is final
  only AFTER an action, because before one it is the starting state that made the loop
  run at all. A precheck therefore only saves work; it never fails a run that has not
  tried.

### Application and API

* The application builds the observation seams per run from the **current** manager
  (`manager.window_probe`, `manager.capture_frame`, `manager.read_frame_text`), so a
  builder bound at boot cannot keep reading a replaced manager. It wires the OCR
  reader only where the manager has one, so no host starts an engine that cannot read.
* `POST /novaforge/sessions/{id}/loop` runs one bounded loop (default 8 actions / 60 s,
  ceiling 64 / 3600 s) and returns the run record; `POST
  /novaforge/sessions/{id}/loop/cancel` asks a running loop to stop before its next
  action and answers honestly when none is running. A stop/pause/cancel/emergency stop
  of the session also cancels a running loop.
* `/novaforge/capabilities` reports `decision_pipeline` from the application (the loop
  lives around the manager), including the policy identity, `model_backed`, the limits,
  the guarded-action statement and the stop conditions. A host with no injector reports
  the pipeline unavailable with that reason.
* Both routes are registered in `api/app.py`, `api/models.py` (`ApiSurface`),
  `api/route_consumers.py` and `docs/API.md`.

### Panel

The Guardian tab's **Gameplay Objective & Actions** card now offers the OCR objective
(kind, parameter, comparison, region), and a new **Closed Loop** card shows the
installed policy and its honest description, bounded inputs, Run/Stop controls, and
the run's own record — status, verifier badge, actions/refusals/decisions, elapsed
time and one row per step. The card is disabled when no session is active, when the
pipeline is unavailable, and while a run is in flight; Stop stays enabled so the
operator can cancel. The Phase Roadmap carries an NF-08 row.

---

## 2. Checks

| Check | Result |
| --- | --- |
| `pytest tests/test_novaforge_closed_loop.py` | **47 passed** |
| `pytest tests/test_novaforge_ui.py tests/test_novaforge_closed_loop.py` | **157 passed**, 68 subtests |
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_gameplay.py tests/test_novaforge_closed_loop.py tests/test_novaforge_ui.py` | **313 passed**, 75 subtests |
| `pytest tests/test_novaforge_gameplay_api.py` | **11 passed**, 10 subtests |
| `pytest tests/test_route_shape_contract.py` | **18 passed**, 90 subtests |
| `pytest tests/test_novaforge_benchmark.py tests/test_novaforge_benchmark_api.py` | **75 passed**, 111 subtests |
| `pytest tests/test_novaforge_evaluation.py tests/test_novaforge_evaluation_api.py` | **76 passed**, 34 subtests |
| `pytest tests/test_web_api.py -k "static or index or asset or cache"` | **3 passed**, 12 subtests |
| `python -m mypy src` | clean, 431 files |
| `python -m mypy src --platform win32` | clean, 431 files |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| `node --check static/js/novaforge.js` | pass |

**Checks that did not complete:** the full `tests/test_web_api.py` run exceeded the
10-minute tool window twice and was not completed; the static/index/cache subset that
covers this change's HTML/CSS/JS surface passes. The DOM panel harness was
mutation-checked: replacing the completed-loop label made the expected copy disappear
from the captured nodes, so the tests drive the shipped renderer rather than a copy.

New tests the change's honesty rests on: the stale-claiming capture, a title-only
observation still being actionable, an unavailable engine never being asked to read,
`absent` being undecided when nothing was recognised and no confidence exists, a
precheck failure not ending a run, a failure after acting being final, the action
budget and policy stall both stopping without an extra press, and the panel offering
Stop while a run is in flight.

---

## 3. Live acceptance on real hardware

Server: `uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001`
from a temporary working directory, so the user's `data/` was untouched. Target:
`scripts/forge_harness_window.ps1` — a disposable WinForms window that retitles and
redraws its own label only when it receives a key.

Capabilities reported `decision_pipeline.available: true`, `observation.ocr: true`,
`vision_model: false`, policy `HarnessReadyPolicy`, `model_backed: false`.

**API run** (`POST /novaforge/sessions` → `objective` → `verify` → `loop`):

| Fact | Value |
| --- | --- |
| Objective | `ocr_text · contains "RECEIVED 1 KEY"` |
| Before any action | `verified_failure` — the OCR evidence read 16 words and did not contain the marker |
| Loop | `completed` |
| Reason | `Read window 9964850: satisfied because the screen showed 'RECEIVED 1 KEY'.` |
| Actions / decisions | 1 / 1 |
| Step 0 | `proposed` → `executed`, expectation met |
| Verdict | `verified_success`, method `ocr_text`, `confidence: null` |
| Evidence (OCR) | `NC FORGE HARNESS - READY x Title is RECEIVED 1 KEY(S) now: NC FORGE HARNESS READY` |
| Session | `COMPLETED` with `objective_verified` |

The verdict is the OCR engine's reading of a frame captured after the key was
injected. The action record cannot produce it; the screen did.

**UI run** through the served page, from the form: validate → start → Guardian tab →
set the OCR objective → **Run Closed Loop**. The card rendered
`Closed loop: completed — objective verified`, the `verified success` badge, the OCR
reason, `Actions executed: 1 · refused: 0 · decisions: 1 · 5.962 s`, a `#1 key space`
row with `expectation met` and the follow-up observation row; the session header read
`COMPLETED`. The objective notice now says the verifier will read text recognised on a
fresh frame, rather than claiming it reads the title.

**Limitation of the evidence:** the preview screenshot tool could not capture the
page — it reported that the preview webview was not being composited, before and after
a reload and a resize. The UI verification above is therefore textual (DOM reads of the
served page), not an image. No game was launched, and no input was sent outside the
disposable harness.

---

## 4. What is still not true

1. **The policy is not a game brain.** `HarnessReadyPolicy` is a deterministic rule for
   the disposable harness. NF-08 proves the *loop* and its gates, not gameplay
   intelligence, and the capability and the panel both say so.
2. **No vision model is wired into observations.** `vision_model: false` is reported;
   OCR text and window geometry are the evidence. Adding a describer is an injection,
   not a redesign.
3. **OCR confidence is absent by engine.** Empty readings cannot prove `absent`; they
   are `unknown`. Numeric comparison is deliberately unsupported (ordering is not in
   `OCR_MODES`) because OCR cannot support a precision claim.
4. **The frame is a full-screen capture cropped to the window**, so the target must be
   visible on the primary display; a window extending past it produces a clipped frame
   that the manager already reports as clipped.
5. **One loop at a time per session**, and the HTTP run is a blocking request bounded
   by its deadline. A second start answers 409.
6. **The harness is not a game.** NF-08 moves the objective kind and the decision loop
   forward; it does not establish a game-playing evaluation, anti-cheat policy or
   per-game control.

---

## 5. Files

* `src/novacontrol/novaforge/observation.py` (new)
* `src/novacontrol/novaforge/policy.py` (new)
* `src/novacontrol/novaforge/closed_loop.py` (new)
* `src/novacontrol/novaforge/objective.py` — `OcrTextVerifier`, availability row
* `src/novacontrol/novaforge/manager.py` — OCR reader seam, `read_screen_text`,
  `read_frame_text`, `window_probe`, `capture_frame`, bounded observation frames
* `src/novacontrol/novaforge/__init__.py` — exports
* `src/novacontrol/application.py` — per-run observation builder, loop runner,
  capability, shutdown cancel
* `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py`, `docs/API.md`
* `src/novacontrol/web/static/index.html`, `static/js/novaforge.js`, `static/nc-os.css`
* `tests/test_novaforge_closed_loop.py` (new), `tests/test_novaforge_gameplay.py`,
  `tests/test_novaforge_gameplay_api.py`, `tests/test_novaforge_ui.py`,
  `tests/test_novaforge_api.py`
