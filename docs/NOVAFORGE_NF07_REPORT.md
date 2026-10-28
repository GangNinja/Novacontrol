# NovaForge NF-07 — Guarded action path & objective verification

**Tagline:** *Observe. Decide. Act. Verify. Evolve.* — this phase adds the **Act** and
**Verify** halves to a panel that could previously only observe.

**Status:** implemented, tested, and accepted on real hardware (a disposable WinForms
window on this machine, real injected keystrokes, a verdict read back from the OS).

---

## 1. What was missing before this phase

`docs/GAME_CONTROL_READINESS_AUDIT.md` recorded `INPUT_PATH_AVAILABLE = False`. NovaForge
could validate a window, capture it and report telemetry, but:

* the "action budget" was a number nobody could enforce, because nothing could act;
* the `Proposed` / `Executed` / `Verified` stages were placeholders that no code could
  ever populate;
* `COMPLETED` was unreachable — no verifier was attached.

NF-07 supplies exactly those three things and nothing else: a bounded, approval-bound
action path, an independent verifier, and the wiring that makes `COMPLETED` mean
_verified_, not _elapsed_.

## 2. The three distinctions the implementation refuses to blur

| Distinction | Where it is enforced |
|---|---|
| A **proposal** is not an action | `NovaForgeManager.plan_action` returns a digest-bound `ActionProposal` and injects nothing. `execute_action` requires that exact digest **and** re-validates it against the live window. |
| A **refusal** is not a failure | A denied action records `denied`, emits `action.denied`, and never increments `actions_executed` (so it never consumes the budget). |
| An **execution** is not a success | `executed` means the input left the process with a matching release. Only `ObjectiveRegistry` — reading the window, never the action record — can produce `verified_success`. |

## 3. Pieces

* `src/novacontrol/novaforge/actions.py` — `GameplayAction` (key tap, hold, click, move)
  with a `ActionPolicy` that bounds keys, buttons, hold/duration and step count;
  `action_digest` (session + action + **target identity**); `ActionRunner`, which injects
  only through the desktop package's `InputGuard`; `ScreenInput`, which speaks the same
  DPI-aware capture worker the frames come from.
* `src/novacontrol/novaforge/objective.py` — `ObjectiveDefinition` (window title),
  `WindowTitleVerifier` (reads the OS; `unknown` when it cannot), `ObjectiveRegistry`
  (states which kinds this build supports instead of guessing).
* `src/novacontrol/novaforge/manager.py` — plan/execute/verify, the budget enforced **at
  execution**, target re-validation immediately before injection (handle + pid +
  foreground), pause on target loss/change, and `COMPLETED` reachable only from a
  verified objective. Cancel/emergency stop now also interrupt in-flight input.
* API (`src/novacontrol/api/app.py`, registered in `ApiSurface`): `POST
  /novaforge/sessions/{id}/objective`, `.../actions/plan`, `.../actions/execute`, `POST
  .../verify`, `GET .../verification`, plus `capabilities` reporting each gap by name.
* Panel (`web/static/index.html`, `web/static/js/novaforge.js`): the Guardian tab's
  **Gameplay Objective & Actions** card — objective kind/parameter, Plan Action, Run
  Planned Action, Verify Now, the action log and the verdict badge.

## 4. Defects found by the live acceptance run (and fixed)

Running the acceptance script on real hardware found three defects that no unit test had:

1. **A cancelled capture exchange corrupted the worker protocol.** Cancelling the frame
   loop mid-request left an unread reply on the worker's pipe; the next `find` read that
   reply as its own answer, so a session that had just been cancelled could not
   re-validate its window ("The matching window disappeared before it could be
   measured"). Fixed in `desktop/capture.py` (`_desynced` + `_request_guarded`), pinned by
   `test_a_cancelled_exchange_cannot_answer_the_next_command`.
2. **A stop request was permanent.** `ActionRunner`'s guard captured its `InputStop` at
   construction, so one abort made every later action abort instantly — including the
   next session's. Fixed with `reset_abort()` / `begin_session()`, pinned by
   `test_a_stop_is_one_shot_and_a_reset_rearms_the_runner` and
   `test_an_earlier_stop_does_not_block_the_next_session`.
3. **Emergency stop could not interrupt a held key, and input cleanup reported lifetime
   totals as per-session numbers.** `_abort` now sets the runner's stop flag *before*
   taking the manager lock (a running action holds that lock for the whole injection), and
   `begin_session()` zeroes the guard's press/release totals, so a session that tapped
   once reports `1 / 1` rather than the process's lifetime count. Pinned by
   `test_a_guardian_cancel_interrupts_an_in_flight_hold` and
   `test_input_cleanup_counts_only_this_sessions_presses`.

Copy that had become untrue was corrected at the same time: the status `limitations`
list, the Mission Control limit hint, the phase roadmap (which now includes NF-07), the
"observe-only" stage placeholders, and the session-start notice.

## 5. Verification evidence

### 5.1 Disposable-window acceptance (real OS window, real keys)

`python scripts/novaforge_gameplay_acceptance.py` — launches its own harness window
(`scripts/forge_harness_window.ps1`), which retitles itself to `NC FORGE HARNESS - READY`
only when it receives a key press. All 15 steps passed:

```
harness window opened ........................ ok   (1 window, restored, 1008×362 client)
harness holds the foreground ................. ok
session pinned / target identity confirmed ... ok
harness starts un-satisfied (no READY) ....... ok   "NC FORGE HARNESS - idle"
checkable objective configured ............... ok   window_title contains READY
plan is a proposal, not an action ............ ok
mismatched digest denied ..................... ok
guarded key executed ......................... ok   steps 2, presses 1, releases 1, held 0
verifier read READY from the window .......... ok   verified_success, evidence observed
verified objective completed the session ...... ok   COMPLETED
Guardian cancel interrupted the hold ......... ok   253.7 ms (hold budget 1500 ms), held 0
vanished target pauses and refuses next ...... ok   pause_reason target_lost
emergency stop closed it, idempotent ......... ok   cleanup 3/3, held 0
summary ...................................... ok   steps 15, failed []
```

The verdict is causal, not asserted: the window did **not** say READY before the run, and
the evidence row quotes the window's title and handle/pid read after the injection.

### 5.2 Live HTTP workflow against a running server

`python scripts/novaforge_live_workflow.py --base http://127.0.0.1:8001` — 10/10 steps:
status readiness, window listing, validate, start, objective, plan, unapproved digest
refused, approved action executed (`presses 1 / releases 1 / held 0`), verifier read
`'NC FORGE HARNESS - READY' from window 24447322`, session `COMPLETED` with
`end_reason = objective_verified`.

### 5.3 Live panel workflow in the browser

Driven through the real UI (Mission Control → validate → start → Guardian → Set Objective
→ Plan Action → Run Planned Action):

* badge `VERIFIED SUCCESS`, reason `Objective MET: Read 'NC FORGE HARNESS - READY' from
  window 5047084 (pid 32316): satisfied because the title contains 'READY'`;
* action log `executed — The action was sent and every press has a release.`;
* Input cleanup `ok`, held after cleanup `0`, releases / presses `1 / 1`;
* Target & Input: validation closed, input boundary blocked for the closed session, input
  path `available`.

![Guardian panel after a verified action](screenshots/novaforge-action-verified-live.png)

### 5.4 Checks run

| Check | Result |
|---|---|
| `pytest tests/test_novaforge.py` | 74 passed |
| `pytest tests/test_novaforge_gameplay.py` | 39 passed, 7 subtests |
| `pytest tests/test_novaforge_api.py tests/test_novaforge_gameplay_api.py` | 38 passed, 5 subtests |
| `pytest tests/test_novaforge_ui.py` | 88 passed, 61 subtests |
| `pytest tests/test_desktop.py tests/test_desktop_input.py` | passed (incl. worker protocol) |
| `pytest tests/test_web_api.py` (3 class groups) | 75 passed |
| `pytest tests/test_web_platform.py tests/test_route_shape_contract.py` | 63 passed, 452 subtests |
| `pytest tests/test_application.py` | 34 passed, 15 subtests |
| `pytest tests/test_novaforge_{timeline,benchmark,coach,evaluation}.py` | 197 passed, 142 subtests |
| `python -m mypy src` / `--platform win32` | clean, 428 files |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |

Every new test was mutation-checked: reverting its fix makes it fail (`_desynced` removal
→ worker desync test fails; `begin_session` counter reset removal → cleanup test fails;
`reset_abort` removal → next-session test fails).

### 5.5 Controlled evaluation over HTTP (2026-10-21)

`scripts/novaforge_controlled_evaluation_acceptance.py` closes the gap between "the manager can do it" and
"an operator can do it over the API". It starts its own server on a free loopback port in a disposable
working directory, launches the disposable harness, and drives the whole chain: preflight → digest-bound
approval → refusal of a start with no digest and of another plan's digest (both `422`, zero benchmark runs)
→ approved start → one Guardian-gated key → the harness retitles → `completed`, `valid`,
`verified_success` → report and timeline evidence resolved.

Observed: `objective_success 1.0`, `actions_executed 1`, one `executed` step with `remaining_actions 0`,
control states `execution: completed` / `verification: verified_success` / `cleanup: completed`, and the
report's `objective_verified: true` with `real_gameplay_control_exercised: true`. **15/15 steps, exit 0,
five consecutive runs.** The run found and now pins two defects (a console window stealing the foreground
from the target, and a race against the session's own target confirmation); both are written up as D6 in
`docs/NOVAFORGE_NF07_INTEGRATION_GAP_AUDIT.md` §12.

## 6. What this build still cannot do — stated, not hidden

* Only a **window-title** objective is checkable. A session with no objective never
  reports success; a session whose objective cannot be decided reports `unknown`.
* Input is limited to the declared gameplay keys; system keys, chords and text entry are
  refused by policy rather than approximated.
* Nothing launches a game, and no capture or control of a protected/anti-cheat process is
  attempted.
* NF-04's observation scenarios still send no input and configure no objective. NF-05 can
  now run a **controlled evaluation** (an operator-authored, digest-approved plan against
  the disposable artifact harness) and record the session's real verdict; it still has no
  planner, so it cannot choose an action or score autonomous play. NF-06 remains read-only.

## 7. Verdicts

| Question | Verdict |
|---|---|
| Can NovaForge act on a validated window? | **Yes** — planned, digest-approved, policy-bounded, guard-released. |
| Can it prove the action worked? | **Yes** — by reading the window, independently of its own action record. |
| Does `COMPLETED` mean the objective was achieved? | **Yes**, and only that: `end_reason = objective_verified`. |
| Does a refusal or a stopped session ever look like success? | **No** — denied/aborted/stopped are distinct records, and the UI labels them as such. |
| Can an operator stop it mid-action? | **Yes** — Guardian cancel/emergency stop interrupts in-flight input (measured 254 ms) and reports the confirmed cleanup. |
| Is this safe for an arbitrary game? | **No claim made.** The action path is proven against a disposable window; anti-cheat, kernel-level input and per-game policies are out of scope and unproven here. |
