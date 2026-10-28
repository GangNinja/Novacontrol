# NovaForge NF-07 — Integration Gap Audit & Gameplay-Trial Readiness

**Date:** 2026-10-20
**Tree examined:** `main` at `8d10852`, **clean** at audit start. This audit's fixes are uncommitted; nothing was committed, pushed, reset, stashed or cleaned.
**Host:** Windows 11, Python 3.13.0, Node v24.19.0, no model runtime, no listener on `:8000`/`:8001` at start.
**Scope:** the NF-01…NF-06 verification (`docs/NOVAFORGE_VERIFICATION.md`) and the seven phase reports were treated as the prior record. This audit does **not** repeat them; it checks how NF-07's action path and objective verifier actually sit inside NF-01…NF-06, fixes what is demonstrably broken, and decides whether objective-based gameplay evaluation may start.

> **Addendum — 2026-10-21: the NF-05 integration landed (uncommitted).** The gap in §8.1 is closed in the working tree. An evaluation can now run in `mode: "control"`: preflight parses the operator's objective and action plan, judges `objective_definition`, `action_plan` and `action_budget` against the live manager and `platform_support` against the host, and mints a single-use approval bound to the plan digest. `start` re-derives that digest, attaches the objective to the benchmark session, and executes each action through `plan_action` → `execute_action` (Guardian-gated, budget-bounded); the verdict is recorded by `verify_objective` from the session's evidence, never from the action log. The benchmark's `controlled-objective` scenario turns `objective_success`, `completion_time_seconds` and `actions_executed` from unsupported into measured fields, and the registry's capability blocks are overlaid with the live manager's report instead of static declarations. **The verdicts below still describe the tree at audit time.** Current verdict: `GO WITH LIMITATIONS` for a controlled evaluation of the disposable artifact harness **through NF-05**; `NO-GO` for autonomous gameplay evaluation of a real game (no planner; verifier evidence is limited to a window title or a declared game-state artifact). The ending rule the integration introduced is §11.

**Verdict at audit time: `NO-GO` for objective-based gameplay evaluation of a real game.**

* `GO WITH LIMITATIONS` for the disposable-harness trial that already exists (an operator-planned, digest-approved action against a window whose title the verifier can read).
* At audit time the evaluation workflow (NF-05) was **observation-only**: it attached no objective and sent no input, so it could not satisfy the GO requirement ("the existing evaluation workflow can safely execute approved actions and independently verify the chosen objective"). **Closed by the 2026-10-21 addendum** — `mode: "control"` now does both; observation-only remains the default.
* The only objective kind this build verifies is a **window title**, and the only game-state access was what that title said. A `game_state_file` verifier now reads a declared JSON artifact inside a declared root. No real game candidate was found whose win/state is readable through either.

---

## 1. Baseline recorded before any change

| Item | Value |
|---|---|
| Branch / HEAD | `main` / `8d10852` |
| Working tree | clean (`git status --porcelain` empty) |
| Environment | Windows 11; Python 3.13.0; Node v24.19.0; Ollama disabled for every run (`NOVACONTROL_DISABLE_OLLAMA=1`) |
| Running processes | none bound to 8000/8001; no stale suite runners |
| Real game launched | **no** (the only window driven was the repository's disposable WinForms harness) |

## 2. Integration matrix

`PASS` = implementation + evidence both verified in this audit. `PARTIAL` = works with a named limitation. `FAIL` = verified defect (fixed below, with the column stating the post-fix verdict). `UNAVAILABLE` = capability does not exist.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | All NovaForge input passes the Guardian gate (`input_safety_decision`) | PASS | `manager.execute_action` asks it before every injection; refusals recorded `denied` and never counted |
| 2 | Approval digest required and revalidated against the live target immediately before injection | PASS | mismatched digest → `denied` (`acceptance` step 4, live workflow step 7); `action_digest` covers session + action + handle/pid/title |
| 3 | Action budget enforced at execution; a refusal does not consume it | PASS | `actions_executed` increments only on `EXECUTED`; `GuardianSafetyTests`, live `remaining_actions` |
| 4 | Cancel / emergency stop interrupt in-flight input and release held keys | PASS | live: hold aborted in **253.3 ms** (1,500 ms hold budget), `held_after 0`, `presses 1 / releases 1` |
| 5 | Target loss pauses and refuses the next action | PASS | live: window closed → `pause_reason target_lost`, next action refused `PAUSED` |
| 6 | A **retitled** target is the same target (handle + pid identity) | **FAIL → PASS** | defect D1; was reported as `target_lost` the moment the objective's own title change occurred |
| 7 | Rejected actions remain distinguishable from executed ones | PASS | distinct `ActionStatus` values; `action.denied` / `action.executed` / `action.aborted` timeline events |
| 8 | Proposed / approved / executed / verified visible in record and timeline | PASS | live session timeline: `action.proposed` → `action.executed` → `objective.verified` → `session.stopped` |
| 9 | No alternate input route, retry or concurrency path bypasses the gate | PASS | one input door (`/novaforge/sessions/{id}/actions/execute` → manager); digest is single-proposal-bound and re-derived; abort flag set **before** the manager lock |
| 10 | Observation-only phases stay observation-only | PASS | NF-04/NF-05 start sessions with no objective; `input` capability reported unavailable-for-the-run |
| 11 | Unsupported objective kind → explicit `unsupported` with reason | PASS | `ObjectiveRegistry.unavailable`, `kinds[]` from `availability()` |
| 12 | Ambiguous observation → `unknown`, never success | PASS | probe `None`/handle or pid mismatch → `UNKNOWN` |
| 13 | Verifier error cannot become success | PASS | probe raise → `VERIFICATION_ERROR`, `verified: None` |
| 14 | Completion and achievement are separate concepts | **FAIL → PASS** | defect D2: `COMPLETED` was rewritten to `end_reason time_limit` + "objective NOT verified" after the verdict |
| 15 | A verified objective completes the session | **FAIL → PASS** | defect D3: verifying while `PAUSED` left `achieved: true` on an open session |
| 16 | Benchmark/evaluation records retain the actual verification outcome | PARTIAL → **PASS (post-integration)** | observation runs still record `objective.available: false` (honest); a controlled run records the session's real verdict, and the registry's capability blocks are overlaid from the live manager — see §8 and §11 |
| 17 | Coach never infers gameplay success from a completed session or injected input | PASS | verdicts stay `inconclusive`/`not_comparable`; `improvement_claimed: false` |
| 18 | Coach reports the session's actual objective verdict | **FAIL → PASS** | defect D4: unconditional "No objective was verified by this session" + capability `available: false` even with a `verified_success` verdict |
| 19 | Missing/expired evidence represented honestly | PASS | `insufficient_evidence`, persistence `unverified`/`failed` states |
| 20 | Persistence / restart recovery preserves terminal status | PASS | restore reconciles an active record to `STOPPED/application_restarted`; `COMPLETED` records restore as `COMPLETED` |
| 21 | NF-04 declares the gameplay-objective scenario unsupported | PASS | `scenario.supported = False`, preflight refuses it |
| 22 | NF-05 baseline-first, compatibility, refusal preserved | PASS | `evaluation.baseline`, NF-04 `compare_runs`; live preflight `ready` |
| 23 | NF-05 can drive the NF-07 action path | UNAVAILABLE → **PASS (post-integration)** | `mode: "control"` attaches one approved objective and one digest-approved plan to the session it starts; observation-only still sends nothing — see §11 |
| 24 | NF-06 approval bound to the exact spec digest and revalidated at run | PASS | `ExperimentSpec.digest`, run refuses before approval (422), approval digest echoed |
| 25 | Insufficient evidence → `inconclusive` / `not_comparable` | PASS | verdict table; single-run arms stay `inconclusive` |

## 3. Files inspected or changed

**Inspected (no change):** `novaforge/actions.py`, `novaforge/objective.py`, `novaforge/guardian.py`, `novaforge/models.py` (`SESSION_TRANSITIONS`), `novaforge/telemetry.py`, `novaforge/timeline.py`, `novaforge/benchmark_lab.py`, `desktop/input.py`, `desktop/capture.py`, `api/app.py` (NovaForge routes), `api/models.py` / `api/route_consumers.py`, `application.py` (manager wiring: `ScreenInput`, `window_by_handle`), `web/static/js/novaforge.js`, `scripts/novaforge_gameplay_acceptance.py`, `scripts/novaforge_live_workflow.py`, `scripts/forge_harness_window.ps1`.

**Changed (this audit):**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/manager.py` | `_tick` resolves the target by pinned **handle** first; a retitle updates the target instead of pausing. `resume` uses the same handle-first read. `_finalize` signals the loop to stop. `_limit_reached` refuses to act on a terminal record. `_tick` returns on a terminal record. `verify_objective` also completes from `PAUSED`. |
| `src/novacontrol/novaforge/coach.py` | The structural finding is now one of three states — decided verdict (quoted as a result), configured-but-undecided, or no objective — instead of an unconditional "no verifier exists". Stale build-wide capability sentences corrected. `_earliest_recoverable_error` quotes the verdict when one exists. |
| `src/novacontrol/novaforge/games.py` | `_NO_INPUT` / `_NO_VERIFIER` no longer claim the capability is absent from the whole build; they state that an **evaluation run** configures neither. `gameplay-objective` status reason updated to the real missing pieces (planner + game-state verifier). |
| `src/novacontrol/novaforge/benchmark.py` | `objective_note` no longer says "No objective verifier exists in NF-04". |
| `src/novacontrol/novaforge/evaluation.py` | Objective-block reasons and the module's honesty rules now say *an evaluation configures no objective*, not that no verifier exists. |
| `tests/test_novaforge_gameplay.py` | 3 regression tests (D1, D3, D2) |
| `tests/test_novaforge_coach.py` | 2 regression tests (D4) |
| `tests/test_novaforge_experiment_api.py` | one assertion re-pinned from the false "no objective verifier" string to the honest rule ("decided verdict") |

**Changed (2026-10-21 integration pass):**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/evaluation.py` | `mode: "control"` — plan parsing, digest approval, Guardian-gated execution, verdict recording; `_await_target_validated` waits for the session's own target confirmation before the first action (D6). |
| `src/novacontrol/novaforge/benchmark.py`, `benchmark_lab.py`, `games.py`, `manager.py`, `objective.py`, `guardian.py`, `coach.py`, `__init__.py` | `controlled-objective` scenario and measured metrics; the ending rule in §11; live capability overlay; verdict-aware coach copy. |
| `src/novacontrol/desktop/capture.py`, `desktop/controller.py` | Helper processes are spawned with `CREATE_NO_WINDOW` (D6): a console window created for a probe took the foreground from the guarded target. |
| `scripts/novaforge_controlled_evaluation_acceptance.py` | **new** — the HTTP acceptance this section records; starts an isolated server and the disposable harness, proves the approval gate and a real verified success. |
| `src/novacontrol/api/models.py`, `docs/API.md` | `POST /novaforge/evaluations` no longer describes itself as observation-only. |
| `src/novacontrol/web/static/js/novaforge.js` | A decided-but-unmet objective renders as "not achieved", not "not evaluated". |
| `tests/test_novaforge_evaluation.py` | 4 target-validation tests; the Windows-only control tests cover approved execution, a decided failure, missing/mismatched approval and a refused plan. |
| `tests/test_desktop.py` | 2 tests pinning the no-console spawn flag for the capture worker and the probe helper. |

No route was added or removed: `ApiSurface` already carried the five NF-07 routes, and `generate_api_reference.py --check` stays in sync.

## 4. Confirmed defects, root causes, fixes, regression tests

### D1 — A retitled window was reported as a lost target (high impact)

**Symptom (observed live, not hypothesised):** after the approved key tap, the timeline read

```
25 action.executed   status executed, presses 1, releases 1
26 target.lost       "The session paused because its target window closed."
29 objective.verified outcome verified_success
```

The window had not closed — it had retitled itself from `NC FORGE HARNESS - idle` to `… - READY`. **Root cause:** `_tick` resolved its target with `self.geometry(record.target.title)`, i.e. by the *title pinned at start*. A title change made the lookup return `None` → `target_lost` → pause. This is fatal for the one objective kind the build supports, because **satisfying a `window_title` objective is exactly a title change**. The NF-07 acceptance script passed only because its verify landed before the next tick (a race, not a guarantee).

**Fix:** the tick asks the handle probe (`objective_probe(handle)`, the same read the verifier uses) first and falls back to the title lookup only when no probe is wired. A different title with the same handle + pid is followed and recorded as `target.validated` with reason *"The window was retitled; its handle and process still match."* `resume` uses the same order.

**Regression test:** `test_a_retitled_window_is_the_same_target_not_a_lost_one` (real manager + title-resolving seam, so the pre-fix code returns `None`). **Mutation-checked:** disabling the handle-first read makes it fail (`pause_reason` set, state `PAUSED`); with the fix it passes.

### D2 — A completed session was rewritten by its own time limit (high impact)

**Symptom:** live record read `state COMPLETED` **with** `end_reason time_limit`, plus `error.recorded` "The 120s session time limit was reached. The objective was NOT verified" and a `guardian.intervention/time_limit` event — all on a session whose objective had been verified.

**Root causes, three compounding:** (a) `_finalize` — reachable from `verify_objective`, outside the loop — did not stop the loop, so capture continued past completion; (b) `_finalize` clears `_started_monotonic`, so `_limit_reached` fell back to wall-clock elapsed from `started_at` and "reached" the limit on the finished session; (c) `_limit_reached`/`_tick` did not check terminal state, so they happily wrote to a closed record.

**Fix:** `_finalize` sets the loop's stop event; `_limit_reached` returns `False` for a terminal record; `_tick` returns immediately for a terminal record.

**Regression test:** `test_a_completed_session_is_not_rewritten_by_the_time_limit` (1 s limit, verify, wait 1.6 s). **Mutation-checked:** removing the terminal guard **and** the stop signal reproduces the failure; with the fix the state, `end_reason` and error list stay correct.

### D3 — A verified objective did not complete a paused session

**Symptom:** `verify_objective` reached `COMPLETED` only when `record.state is RUNNING`. A session the loop had paused (operator pause, or D1 before its fix) could carry `achieved: true` while staying `PAUSED` forever — deferred outcome, never recorded.

**Fix:** the guard accepts `{RUNNING, PAUSED}`; `PAUSED → STOPPING → COMPLETED` is already legal in `SESSION_TRANSITIONS`, so the recorded close stays inside the state-machine contract.

**Regression test:** `test_a_verified_objective_completes_a_paused_session`.

### D4 — Coach denied objectives that had been verified

**Symptom:** `analyse` appended `objective_verifier_missing` unconditionally, with title "No objective was verified by this session", `severity_basis` "This build has no objective verifier" and evidence `{"kind": "capability", "available": False}`. After NF-07 that is false about the build **and** about a session carrying `verified_success`. NF-06's own limits list repeated it.

**Fix:** three explicit states — decided verdict (`objective_verified` / `objective_failed`, quoting the verifier's own reason and citing `objective.verified` events), configured-but-undecided (`objective_unverified`), or no objective (`objective_verifier_missing`, now scoped to the session). `_earliest_recoverable_error` says "the objective outcome is recorded separately (…)" when a verdict exists, and keeps "that is not the same as success" only when none does. Build-wide "no verifier exists" copy in `capabilities()["limits"]` and `_analysis_limitations` replaced with the actual rule (a success claim requires a decided verdict).

**Regression tests:** `test_a_verified_success_is_a_result_not_a_missing_verifier`, `test_an_undecided_objective_is_not_reported_as_verified`. **Mutation-checked:** forcing `if False and decided:` makes the first fail (`KeyError: 'objective_verified'`).

### D5 — Stale capability copy in NF-04/NF-05 (documentation-level lie)

`games.py` said *"No gameplay input path exists in this build"* and *"No objective verifier is attached to a NovaForge session"* as build constants; `benchmark.py` said *"No objective verifier exists in NF-04"*; `evaluation.py`'s rules said the same. All false since NF-07. Corrected to state what is actually true: **an evaluation run configures neither an action path nor an objective**, so its completion is data completeness. The `gameplay-objective` registry entry now names the genuinely missing pieces (a decision planner and a game-state verifier) rather than two capabilities that exist.

**Test impact:** `test_the_capability_card_names_its_vocabularies_and_limits` pinned the false string; it now pins the honest rule ("decided verdict" + "verifier"). This is a strictly more specific assertion, not a weakened one.

## 5. Commands, exit codes, counts, durations

All from the repository root, `PYTHONPATH=src:.`, Python 3.13.0, one process at a time.

| Command | Result | Duration | Exit |
|---|---|---|---|
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py -q` | **105 passed** | 408.77 s | 0 |
| `pytest tests/test_novaforge_coach.py -q` | **49 passed, 19 subtests** | 224.01 s | 0 |
| `pytest tests/test_novaforge_evaluation.py tests/test_novaforge_gameplay.py -q` (pre-fix) | **91 passed, 33 subtests** | 39.86 s | 0 |
| `pytest tests/test_novaforge_gameplay.py -q` (final, 3 new tests) | **42 passed, 7 subtests** | 6.21 s | 0 |
| `pytest tests/test_novaforge_benchmark.py tests/test_novaforge_gameplay_api.py -q` | **63 passed, 102 subtests** | 106.82 s | 0 |
| `pytest tests/test_novaforge_ui.py tests/test_desktop.py tests/test_desktop_input.py -q` | **210 passed, 81 subtests** | 23.53 s | 0 |
| `pytest tests/test_novaforge_timeline.py -q` | **42 passed** | 4.82 s | 0 |
| `pytest tests/test_novaforge_experiment_api.py -q` | **9 passed** (first attempt: 1 failed on the stale-copy assertion — fixed and re-run) | 150.73 s | 0 |
| `pytest tests/test_novaforge_benchmark_api.py tests/test_novaforge_evaluation_api.py -q` | **28 passed** | 390.38 s | 0 |
| `pytest tests/test_route_shape_contract.py -q` | **18 passed, 90 subtests** | 279.75 s | 0 |
| `python -m mypy src` | no issues, **428 files** | — | 0 |
| `python -m mypy src --platform win32` | no issues, **428 files** | — | 0 |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` | — | 0 |
| `node --check` on `app.js` + every `js/*.js` | all pass | — | 0 |
| `python scripts/novaforge_gameplay_acceptance.py` | **15/15 steps**, `failed []` | ~30 s | 0 |
| `python scripts/novaforge_live_workflow.py --base http://127.0.0.1:8001` | **10/10 steps**, `failed []` | ~15 s | 0 |
| `pytest tests/test_novaforge_evaluation.py -q` (2026-10-21, incl. 4 new wait tests) | **63 passed, 34 subtests** | 38.28 s | 0 |
| `pytest tests/test_desktop.py -q` (incl. 2 no-console pin) | **77 passed, 20 subtests** | 21.05 s | 0 |
| `pytest tests/test_novaforge.py test_novaforge_benchmark.py test_novaforge_evaluation.py test_novaforge_gameplay.py test_novaforge_benchmark_api.py test_novaforge_evaluation_api.py -q` | **267 passed, 152 subtests** | 460.30 s | 0 |
| `python scripts/novaforge_controlled_evaluation_acceptance.py` (four consecutive runs) | **15/15 steps**, `failed []`, each | ~35 s | 0 |

**Not re-run, and why:** `tests/test_application.py` and `tests/test_web_api.py` exercise application wiring and HTTP surfaces this audit did not modify (no route, no wiring, no permission code changed); `test_novaforge_api.py` and `test_route_shape_contract.py` cover the same NovaForge HTTP surface. The **full** suite was not re-run: the earlier 10-slice run in the previous pass is the last whole-suite figure, and the changes here are confined to `novaforge/` plus two test files. A first attempt to run five files in one call hit the 590 s tool ceiling (recorded, not reported as a pass); the files were then run in the batches above.

**Skips:** none added; none of these files skip on this host.

## 6. Live workflow evidence (three distinct tiers)

1. **Disposable OS window (real input, real read-back):** `scripts/novaforge_gameplay_acceptance.py` — harness launched by the script, verified un-satisfied (`NC FORGE HARNESS - idle`), digest mismatch denied, approved tap executed (`presses 1 / releases 1 / held 0`), verifier read `NC FORGE HARNESS - READY`, session `COMPLETED`, hold aborted by Guardian cancel in 253.3 ms, vanished target paused and refused, emergency stop released input. **15/15, exit 0.** No game involved.
2. **Live HTTP against a fresh server** (`127.0.0.1:8001`, `NOVACONTROL_DISABLE_OLLAMA=1`): validate → start → objective → plan → unapproved digest refused → approved action executed → window read back → `COMPLETED`. **10/10, exit 0.**
3. **Real browser page** (the app served on 8001, driven through the panel): Mission Control → select the harness → *VALIDATE MISSION* → *START SESSION* → Guardian → *Set objective* (`window_title contains READY`) → *Plan Action* (key `enter`) → *Run Planned Action*. Result: badge `VERIFIED SUCCESS`, reason *"Read 'NC FORGE HARNESS - READY' from window 1969992 (pid 33784) … contains 'READY'"*, header `COMPLETED`. Screenshot: `screenshots/novaforge-nf07-audit-guardian.png`.

**The browser pass is what found D1 and D2.** With the pre-fix build the same flow ended `PAUSED` (D1), and after D1's fix the record completed but was then rewritten to `end_reason: time_limit` with a "NOT verified" error (D2). After both fixes the same flow ends `state COMPLETED`, `end_reason objective_verified`, `achieved true`, **zero errors**, and a timeline of exactly one `action.executed`, one `objective.verified`, one `session.stopped`, with no `target.lost` and no `time_limit`:

```
session.created → target.validated → session.started → objective.configured
→ action.proposed → action.executed → objective.verified → session.stopped
→ input.cleanup → telemetry.snapshot          (+ frame.captured, target.validated on retitle)
```

The UI's Guardian guarantees read `gameplay input: enforced` and `objective verification: enforced` for a live session, and Mission Control's readiness list states the real limits.

**What is not browser-tested:** restart recovery and the coach/benchmark/evaluation tabs in this pass (they are covered by the suites; the earlier verification pass browser-tested the coach tab).

## 7. Current action-path and objective-verification capability

| Question | Answer |
|---|---|
| Can NovaForge act on a validated window? | Yes — planned, digest-approved, policy-bounded, Guardian-gated, guard-released |
| Does a verified objective mean success? | Yes — the verifier reads the window, never the action record |
| Does `COMPLETED` mean the objective was achieved? | Yes, and only that (`end_reason objective_verified`) — now also from `PAUSED`, and no longer overwritable by the limit |
| Can an operator stop mid-action? | Yes — 253 ms abort of a 1,500 ms hold, with confirmed cleanup |
| Which objectives can be verified? | **`window_title` only** (`contains`/`equals`/`absent`); everything else reports `unsupported` |
| Is there a decision pipeline? | **No** — `decision_pipeline.available: false`; a caller proposes every action |
| Can NF-05 evaluations act? | At audit time **no** — observation-only. Post-integration: **yes** in `mode: "control"` (one operator-authored plan, digest approval, Guardian-gated), while observation-only is unchanged and remains the default. |

## 8. Remaining gap between NF-07 and NF-04/NF-05/NF-06

1. **NF-05 cannot attach an objective or an action plan to the session it starts.** Its run is an NF-04 observation benchmark; wiring an approved, operator-supplied action plan into an evaluation run is the integration that would let an evaluation produce a verified outcome. Not implemented here (it is a design decision about who approves each action inside an automated run). **Resolved 2026-10-21:** see §11.
2. **The compatibility registry's capability blocks are module constants.** `games.py` declares `input`/`objective_verification` availability statically, so even on a host where both work the registry reports them unavailable. Correct for today's observation-only entries, but a control-capable entry would have to read the live manager's `capabilities()`. **Resolved 2026-10-21:** `to_dict(runtime=...)` overlays the live report and keeps the declaration under `declared`.
3. **`objective_success` and `completion_time_seconds` metrics remain unavailable for every NF-04 run** because no benchmark scenario configures an objective. That is honest, and it means NF-04 can never certify gameplay from its own runs today. **Resolved 2026-10-21:** the `controlled-objective` scenario measures them from the session's verdict; an observation-only run still reports them as unavailable, never zero.
4. **Coach has no verdict-driven recommendation.** A verified success is now reported as a result, but no rule proposes the next step from it (deliberate: no rule may claim an improvement NF-04 cannot measure).

## 9. First-game candidate assessment

No game was launched or controlled. Candidate considered: **Freeciv** (offline, single-player, turn-based, windowed, no anti-cheat, saves to a local file).

| Criterion | Assessment |
|---|---|
| Window reliably identified and captured | Likely — a single titled window, captured by the existing pipeline |
| Supported action set sufficient for a minimal test | Plausible (clicks + gameplay keys on a turn-based game) but **unproven**; the policy allows no text entry and no chords |
| Input restricted to the intended target | Yes — pinned handle/pid, foreground requirement, client-area coordinates |
| Guardian limits and emergency stop enforceable | Yes — same path proven against the harness |
| Trustworthy objective verification | **No.** The title stays "Freeciv"; a win/score is not readable through the only verifier. A save file or an OCR criterion would be a *new* verifier kind. |
| Baseline recordable before tuning | Partly — NF-04 baselines are observation-only (no actions), so they cannot be a gameplay baseline |
| Repeated runs comparable | Yes at the NF-04 level (eligible-only, same-basis), not at the gameplay level |
| Anti-cheat / privilege / platform restriction | None known for offline single-player; the client also starts a local server process, which is allowed but must be declared |

**This is why the answer is NO-GO rather than "try it and see":** capture compatibility is not objective verifiability, and this build can only prove an objective by reading a window title.

**Smallest additional capability that would unblock a real trial:** one new objective-verifier kind that reads a bounded, independently produced game artifact — a game-written state/log file with a declared exact-match criterion, or a deterministic screen-region OCR criterion — plus NF-05 passing an operator-approved action plan and an objective into the session it starts. A decision planner is *not* required for a planned-trial design; it is required for autonomous play.

## 10. Decision

**`NO-GO` for objective-based gameplay evaluation.**

- Mandatory requirement missing: the evaluation workflow cannot execute approved actions or verify an objective, and no real game candidate has a verifiable objective through the single supported verifier.
- The safety and control prerequisites that *are* required are met and were re-verified live (Guardian gate, digest binding, budget, cancellation, cleanup, refusal ≠ failure, execution ≠ success).

**`GO WITH LIMITATIONS` for the disposable-harness trial** — the only controlled gameplay trial this build supports today, with these documented restrictions: an operator plans every action; the target is an operator-opened, non-protected window; the only verifiable objective is its window title; no decision pipeline, no game launch, no automation of the evaluation pipeline.

Nothing in this audit authorises launching or controlling a real game.

---

## 11. Post-audit integration — how a controlled evaluation ends

A controlled evaluation has more than one legitimate ending, and the record must tell them apart. This is the rule the integration implements, pinned by `tests/test_novaforge_evaluation.py::ControlEvaluationTests`, `tests/test_novaforge_benchmark.py::BenchmarkRunnerTests` (the fast-verdict regressions) and the manager tests in `test_novaforge_gameplay.py`.

| Ending | Session record | Benchmark run | Evaluation | Why |
|---|---|---|---|---|
| The last approved action executed and the verifier read **success** | `COMPLETED`, `end_reason objective_verified` | `completed`, valid | `completed`, valid, `objective.verified: true`, `verified_success` | The verdict is the measurement this run was asked for. |
| The last approved action executed and the verifier read **failure** | `STOPPED`, `end_reason action_budget` (the plan's own bound) | `completed`, valid | `completed`, valid, `verified_failure`, `achieved: false`, metric `objective_success: 0` | A decided "no" is a result, not an aborted run. |
| The plan's budget was spent and the verifier could not decide | `STOPPED`, `action_budget` | `completed` if the data floor was met, with the undecided reason | `completed`, `objective.verified: false`, never success | Data completeness only; the run is **inconclusive**. |
| An action was refused (policy, Guardian, budget, stale approval) | `STOPPED` via the evaluation's refusal path | `aborted` | `aborted`, invalid | The plan did not finish; no verdict may be inferred. |
| Operator cancel / emergency stop / external stop | `STOPPED` with the operator's reason | `aborted` | `aborted`, invalid | A stop outside the runner is not a completed plan. |

**The frame floor is an observation floor, not a verdict gate.** The manager closes a session the moment a verified success is recorded, so a legitimate fast run can record fewer frames than the scenario's `min_frames` (4). `_terminal_status` therefore waives the frame floor only when the session's objective block is **decided** *and* at least one fresh frame was recorded; an undecided run still faces the full frame and latency floors. A verdict with nothing observed is not a measurement, and frames are never a substitute for a verdict.

**What the rule deliberately does not do:**

* it does not turn `action_budget` into success — the verdict, never the ending reason, decides `achieved`;
* it does not let a completed-but-undecided controlled run enter a comparison as a verified result; its `objective_success` row is unavailable, not `0`;
* it does not treat an external stop as a completed plan, however far the plan got.

## 12. The HTTP acceptance run (2026-10-21) and the two defects it found

`scripts/novaforge_controlled_evaluation_acceptance.py` drives the controlled path the way an operator
reaches it — over HTTP, against a server the script starts on a free loopback port in a temporary working
directory (so `data/` is disposable), with its own disposable `NC FORGE HARNESS` window. It proves: a plan
with no objective is refused with no token; a complete plan passes preflight and receives one digest-bound
approval; a start with no digest, and a start carrying another plan's digest, are both `422` and start **no**
benchmark run; the approved start executes exactly one key; the harness retitles; the evaluation ends
`completed`, `valid`, `objective.decided`, `verified_success`; the report separates "executed" from
"verified" and its evidence links resolve to the benchmark run and a recorded timeline. Five runs after the
fixes below: **15/15 steps, exit 0 each**.

### D6 — A helper's console window stole the foreground from the guarded target

**Symptom (intermittent, observed live):** the approved action was DENIED with *"The target is not the
foreground window; input would land elsewhere"* even though the harness had just been confirmed
foreground — roughly one run in three.

**Root cause, two independent paths:** **(a)** `PersistentScreenCapture` spawned PowerShell via
`asyncio.create_subprocess_exec` with no `creationflags`; a console-less parent therefore gave the worker a
**new console window**, which Windows brings to the foreground — at the first frame of the session, i.e.
exactly when the first action runs. The probe helper (`LocalDesktopRunner._powershell`) that enumerates
windows and reads geometry had the same defect, so target validation and typing could steal focus too.
**(b)** `_run_control` executed the plan the instant the benchmark reported its session id, while the
session's own observation loop confirms the target identity on its first tick; until it does, `plan_action`
correctly refuses input. That race was a second, independent way for a correct plan to be refused.

**Fix:** both helpers pass `creationflags=CREATE_NO_WINDOW` (0 on non-Windows). `_run_control` awaits
`_await_target_validated`, bounded at 15 s, which returns the real reason when the session ended, paused or
never confirmed. Nothing about the Guardian's foreground rule was relaxed — the point is that the build
stopped breaking it.

**Regression tests:**
`test_the_capture_worker_is_spawned_without_a_console` and `test_window_probes_run_without_a_console`
(`tests/test_desktop.py`); `ControlTargetValidationTests` (`tests/test_novaforge_evaluation.py`) pins that
the wait returns "" after the confirmation, reports a paused/vanished session instead of acting, and is
bounded. **Mutation-checked:** removing the wait makes the live acceptance run refuse the first action
with the observation-loop reason; removing `CREATE_NO_WINDOW` makes it refuse intermittently with the
foreground reason. Both were observed before the fix, not inferred.

**Limits of this evidence:** one machine, one disposable window, an operator-authored single-key plan. It
proves the controlled workflow and the approval gate; it says nothing about a real game, and no planner
chose any action.
