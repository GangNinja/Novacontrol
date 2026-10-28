# NovaForge NF-25 — Real-Game Objective Execution and Progress Validation

**Date:** 2026-10-27
**Scope:** one bounded real-game task, executed through the existing NF-19 → NF-22 pipeline, judged from
evidence rather than from dispatch.
**Verdict:** **PARTIAL** — the bounded task layer, its preflight gates and its evidence-based judging are
implemented and verified on fixtures; **no live-game objective was executed**, because no safe installed offline
title can start on this host and no operator approval for a live gameplay test was requested or granted.

> A dispatched input is not a game response. A game response is not progress. Progress is not completion. A
> simulation is not live evidence. Every claim below is separated along exactly those lines.

## 1. Executive summary

NF-25 adds `src/novacontrol/novaforge/tasks.py`: a bounded task definition (`TaskSpec`), a supervisor adapter
(`AutonomyTaskGateway`), a runner (`TaskRunner`) whose only loop is the existing NF-22 controller, and a single
judge (`judge_task`) that decides what a finished run actually established.

The phase's own contribution is the discipline around the existing pipeline:

* a task cannot start without an objective, a **verifiable** success condition, an explicit evidence
  requirement, permitted action kinds and budgets inside the supervisor's ceilings;
* ten preflight gates refuse by name (approval, session, target identity, control policy, initial state,
  initial state age, initial scene, resource gate, duplicate run, supervisor refusal);
* completion is only `verified_completion` when the run ended `objective_completed`, **and** the task's declared
  evidence is present in what the run produced, **and** every action the run dispatched is one the task
  permitted — a dispatch-only run can never complete a task, and neither can one that exceeded its own policy;
* dispatch, observed response and verified progress are three separately counted numbers, and the objective's
  nine stages (identified → initial state → plan → approval → request → dispatch → response → progress evidence
  → verified completion) are reported one by one, never inferred from each other;
* a confirmed cancellation ends the task in the ledger (a session is never left refused `already_running`), and
  a cancel that arrived after the run had already ended does not rewrite that ending;
* every report carries the run's own `evidence_source` and a `simulated` flag, so a fixture can never be read as
  a live result.

No new loop, planner, verifier or input path was created, and no API route was added (the documented API surface
is unchanged).

## 2. Initial findings and dependency status

| Dependency | State at NF-25 start | Consequence for NF-25 |
|---|---|---|
| NF-19 bounded input | verified (control suite green) | reused unchanged; NF-25 never injects input |
| NF-20 evidence-grounded planning | verified | reused through NF-22; NF-25 refuses tasks whose evidence cannot justify a plan |
| NF-21 verification/recovery | verified; `not_verifiable` is reachable | NF-25 reads its outcome rows and accepted evidence refs |
| NF-22 orchestration | verified on fixtures/simulation | the ONE supervisor NF-25 drives |
| NF-24 scene understanding | verified on fixtures and recorded frames | used for the initial-state gate and the final-scene evidence |
| NF-23 live-game observation | **BLOCKED** (`SteamAPI_Init failed`, no other safe offline title) | no live task could be run |
| NF-25 live test approval | **not requested** | per the brief, the live test was not started |

This is a layer built on verified **interfaces**, not on a verified live game. Nothing below claims otherwise.

## 3. Implementation plan and changes made

The plan is `docs/NOVAFORGE_NF25_IMPLEMENTATION_PLAN.md` (written before the code, kept as built).

| File | Change |
|---|---|
| `src/novacontrol/novaforge/tasks.py` | **new** — `TaskSpec`, `TaskVerdict`, `TaskReport`, `judge_task`, `task_progress`, `TaskGateway`, `AutonomyTaskGateway`, `TaskRunner` |
| `src/novacontrol/novaforge/manager.py` | `tasks()` builds the ONE runner from this manager's own readers; `run_task()` is the product entry point |
| `src/novacontrol/novaforge/__init__.py` | NF-25 names exported beside the other phase surfaces |
| `tests/test_novaforge_nf25.py` | **new**, 91 tests (17 spec validation, 23 preflight, 28 judge, 20 runner lifecycle, 2 gateway, 1 manager wiring) |
| `scripts/novaforge_nf25_task_eval.py` | **new** — fixture judge eval + latency measurement (reproducible) |
| `docs/NOVAFORGE_NF25_IMPLEMENTATION_PLAN.md`, this report, `docs/NOVAFORGE_VERIFICATION.md` | phase documentation |

No test was removed, skipped, weakened or deselected; every count below is a full run of the named files.

## 4. Architecture and data flow

```
TaskSpec ──► TaskRunner.start ──► [preflight gates] ──► NF-22 AutonomousSessionController.start
                                   (refuse by code)             │
                                                                ├─ observe     (NF-15/24)
                                                                ├─ state       (NF-16/24 scene)
                                                                ├─ objective   (NF-17)
                                                                ├─ plan        (NF-20)
                                                                ├─ act         (NF-19 → Guardian)
                                                                └─ verify      (NF-21)
                                      TaskRunner.finish ◄── NF-22 wait (terminal run)
                                            │
                                      judge_task(spec, run) ──► TaskReport
```

* `TaskRunner` owns no loop and no dispatch: its only mutable state is a ledger (which task is active on which
  session, the last 20 reports, three counters).
* `judge_task` is a pure function over `AutonomyRun.to_dict()`; it never re-verifies and never re-plans.
* `AutonomyTaskGateway` translates `TaskSpec` into `AutonomyLimits` (refusing wider bounds at spec validation,
  never clamping) and forwards the operator's approval verbatim.

## 5. Task specification and safety boundaries

`TaskSpec` requires: id, `MissionConfig` (objective, target title, action budget), **success conditions**
(vague goals refused), permitted action kinds, cycle/action/time budgets, an observation freshness bound, a
recovery limit, allowed initial scenes, and **required evidence**. A task that names no evidence is refused,
because nothing could then distinguish success from a plausible story.

Safety is inherited, not re-implemented: the task's actions still travel NF-22 → NF-20 → NF-19 → the Guardian,
so the existing action policy, digest binding, budget and input-release rules apply exactly as they do for a
manual action. NF-25 adds only the refusals and one claim gate: it will not start without the operator's
approval, will not start on a window that is not the task's target, will not start on a screen the state layer
cannot name, and will not call a run complete when that run dispatched an action kind the task did not permit
(or a dispatched step whose kind cannot be read). The task's action policy is therefore a boundary on the
claim, not a label: this layer can report such a breach, but it cannot pre-filter a plan the existing
supervisor already built (recorded as a limitation on every report where it applies).

The brief's standing constraints were respected: GTA V was never launched, the external drive was never touched,
no process was force-killed, no duplicate instance was started, no injection/memory/rendering hook was used, no
display mode was changed, no input was sent, and unrelated working-tree changes were left alone.

## 6. Automated tests and exact results

All commands from the repository root with `PYTHONPATH=src` and the project's `.venv`.

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge_nf25.py -q` | **91 passed** |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_verification.py tests/test_novaforge_planning.py tests/test_novaforge_nf24.py -q` | **417 passed, 74 subtests passed** |
| `python -m pytest tests/test_novaforge_api.py tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_control.py -q` | **198 passed, 16 subtests passed** |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_api.py tests/test_novaforge_control.py -q` (after the verification fixes) | **269 passed, 37 subtests passed** |
| `python -m pytest tests/test_web_api.py -q` | **75 passed, 72 subtests passed** |
| `python scripts/novaforge_nf25_task_eval.py` | fixtures **7/7**, false completions **0** |

What the 91 tests pin, by objective:

* **A — bounded task:** 17 spec-validation tests (vague goal, no evidence, unknown/duplicate action kind, no
  action budget, budget above the ceiling refused not clamped, cycle ceiling, unknown scene, impossible
  freshness, limits translation, payload).
* **B — evidence-grounded selection:** the initial-state gate refuses an unreadable, refused, unnamed, stale or
  wrong-scene reading; the control-policy gate refuses an action this build cannot perform.
* **C — safe execution:** approval is required by default; a target that is unpinned or not the task's target is
  refused; two tasks cannot run on one session; a supervisor refusal is passed through with its own code.
* **D/E — closed loop and progress:** completion needs the ending **and** the evidence **and** the action
  policy; missing evidence, missing everything, dispatch-only, progress-only, a final-cycle scene mismatch, an
  out-of-policy dispatch and an unattributable dispatch all stay unverified; a verified failure maps to
  `objective_failed`, never to completion. Six more tests pin the nine-stage ladder individually and the
  ledger behaviour of a cancel (confirmed, unconfirmed, undeliverable, and after another ending).
* **F — recovery/endings:** cancellation, safety stop, dependency failure, observation loss, spent clock and
  resource limit each map to their own verdict; a resource limit without progress is not "progress".
* **G — live-test design:** the plan's procedure and approval gate are documented; nothing ran.
* **H — tests:** the suite above, including simulation-vs-live labelling and the manager's own wiring.
* **I — performance:** the eval script measures the layer (see §11).

## 7. Type-check and documentation validation

| Command | Result |
|---|---|
| `python -m mypy src` | **Success: no issues found in 453 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 453 source files** |
| `python scripts/generate_api_reference.py --check` | **docs/API.md: in sync** (no route added) |

## 8. Live-game test procedure and approval status

The procedure is in the plan (§7): a main-menu → options → main-menu task, `key_press`/`key_release` only,
4 actions, 60 s, 3 cycles, 5 s observation freshness, 1 recovery, required evidence `scene:menu` and the
verifier's own evidence, and completion only from a fresh observation after the last action.

**Approval status: not requested, not granted, not run.** The brief requires the operator's explicit approval
before a live gameplay test; this pass did not present one, because the prerequisite is also absent: NF-23/NF-24
measured that the only safe installed offline title cannot start on this host (`Launcher.exe` prints
`SteamAPI_Init failed` and exits rc=1 in 0.5 s with Steam running; the other candidate folders are empty). A
live test cannot be approved into existence.

## 9. Live-game results

**None.** No game was launched, no window was observed for NF-25, and no input was sent. The last real-game
observation attempt remains NF-23's recorded blocker; the last real (non-game) window reading is NF-24 §9.2 and
is explicitly not game evidence. Nothing in this report is a live-game result.

## 10. Objective-progress and completion evidence

The fixture eval (`scripts/novaforge_nf25_task_eval.py`) demonstrates the required judgement behaviour:

| Fixture | Verdict | Dispatched / responded |
|---|---|---|
| verified completion with its evidence | `verified_completion` | 2 / 1 |
| completion missing a required tag | `outcome_unknown` (`evidence_missing`) | 2 / 1 |
| dispatch-only run | `outcome_unknown` | 3 / **0** |
| progress without completion | `progress_only` | 2 / 1 |
| stale screen evidence from an earlier cycle | `outcome_unknown` (`evidence_missing`) | 2 / 1 |
| objective failed | `objective_failed` | 2 / 1 |
| an action outside the task's permitted set | `safety_stop` (`action_policy_exceeded`) | 2 / 1 |

False completions: **0**. The dispatch-only row is the phase's central pin: three input operations completed and
no outcome was readable, and the task reports `outcome_unknown` with `actions_responded = 0`.

## 11. Performance and resource measurements

| Measurement | Value | Basis |
|---|---|---|
| `TaskSpec.errors()` validation | **0.0013–0.0019 ms** per call | 500 iterations, `novaforge_nf25_task_eval.py`, across runs |
| `judge_task` over a 2-cycle run | **0.030–0.032 ms** per call | 500 iterations (includes the stage ladder and the policy audit) |
| `TaskRunner.run` (preflight + start + wait + judge), scripted supervisor | **0.060–0.075 ms** per task | 50 iterations |
| Preflight state read | **not measured live**; it is one NF-16/24 analysis, measured by NF-24 at **735–980 ms** when a real window was read | NF-24 §9.2/§10 |
| Cycle cost | not measured end-to-end; the stages are NF-19/20/21's own, measured in their reports | NF-19–NF-21 reports |

The plan's targets (≤ 50 ms preflight, ≤ 20 ms judging) are met for the layer's own work. The end-to-end cycle
cannot be timed without a live game; that remains unmeasured rather than estimated.

## 12. Defects discovered and fixes applied

Found during implementation and fixed before the suite was declared green (each is now pinned):

1. **The "no evidence" guard was unreachable.** `_evidence_found` synthesised `objective:<run.objective_id>`
   even when the run recorded no cycle, so a completion with literally no reading reference looked evidenced.
   The synthetic objective reference is now taken only from the final cycle's objective row.
   Pinned by `test_completion_without_any_evidence_is_not_claimed`.
2. **An earlier cycle's screen could satisfy a final claim.** Scene and objective evidence were collected across
   every cycle, so a `scene:menu` seen at the start satisfied a completion read from a death screen. Both are
   now taken from the **last** cycle only. Pinned by
   `test_only_the_final_cycle_supplies_the_screen_evidence` and the eval's stale-scene case.
3. **`finish()` would judge any run on the session.** Called without a matching start, it judged whatever run the
   session held, including another objective's. It now refuses `wrong_task` when the run names a different
   objective. Pinned by `test_a_run_for_another_objective_is_not_judged_as_this_task`.
4. **An adapter type mismatch** (`read_state` as an awaitable attribute instead of a coroutine method) was caught
   by `mypy` and fixed; the adapter now `await`s its injected reader inside a real coroutine.
5. **A task with no action budget** (`MissionConfig.max_actions = None`) initially slipped past the spec; it is
   now refused by name, because an unbounded task is not a bounded task.

Found in the verification pass (same day, after the first draft of this report) and fixed with pins:

6. **`permitted_actions` was a declaration, not a gate.** A run that dispatched an action kind outside the
   task's permitted set could still be judged `verified_completion` — the task's own boundary did not bind the
   claim. `dispatched_kinds` now reads the dispatched step kinds off the run (only `ok: true` steps count), the
   judge records them under `verification.action_policy`, and a completion carrying a violation (or a dispatched
   step whose kind cannot be read) is `safety_stop` with code `action_policy_exceeded`. Pinned by
   `test_an_action_outside_the_tasks_policy_voids_the_completion`,
   `test_a_refused_step_is_not_an_action_the_task_performed` and
   `test_a_dispatched_step_without_a_kind_keeps_completion_unproven`; the eval's seventh fixture is the same case.
7. **A confirmed cancellation left the task active forever.** `TaskRunner.cancel` reported the stop but never
   removed the session's ledger entry, so every later task on that session was refused `already_running`; a
   cancel that arrived after the run had already ended was also reported as `cancelled`, overwriting the run's
   real ending. A confirmed cancel now finishes and archives the report (`cancelled`), an unconfirmed one
   deactivates without claiming a stop (`outcome_unknown` with the supervisor's code), an undeliverable one
   leaves the task active, and a cancel after another ending reports `cancelled_after_completion` /
   `cancelled_after_ending` instead of rewriting it. Pinned by the five cancel-ledger tests in
   `TaskRunnerLifecycleTests` and the two release-measurement tests beside them.
8. **Objective E's stage ladder was not reported.** The report counted dispatched/responded/verified but did not
   answer the brief's nine stages separately; `task_stages` now does, and `completion_verified` may only be set
   by the judge after its gates. Pinned by `test_every_objective_stage_is_answered_separately`,
   `test_a_dispatch_only_run_reports_its_ladder_without_a_response` and
   `test_verified_completion_marks_the_final_stage_then`.
9. **The eval script's fake supervisor predated the held-input reader**, so `python
   scripts/novaforge_nf25_task_eval.py` died with `AttributeError: '_Gateway' object has no attribute
   'held_inputs'` and its measurements were stale. The fake now answers the reader (and the fixture set grew from
   six cases to seven); the script runs end to end again.

No defect was found in NF-19/20/21/22 itself, so no cross-phase change was made; the action-policy gate is
implemented where the claim is made (the judge) rather than by changing the planner's own policy.

## 13. Remaining limitations and blockers

1. **Live-game objective execution is unverified.** No safe installed offline title can start on this host; the
   NF-23 measurement stands and was not re-attempted.
2. **No operator approval for a live test** was requested or granted, so the live half could not have run even
   with a launchable title.
3. **End-to-end cycle latency is unmeasured.** The stage latencies exist in their own reports; the combined
   observe→verify cycle needs a live target.
4. **The task layer's scene requirement depends on NF-24's pipeline.** No commercial-game profile exists, so a
   task requiring a named scene can only be proven on a layout the state layer can name.
5. **`TaskRunner.finish` waits for `max_seconds + 10`.** A run whose verification overruns that bound reports
   `still_running` (honest, non-terminal) rather than blocking; the caller must read it again.
6. **No API route.** The task layer is reachable from the product through `NovaForgeManager.run_task`; exposing it
   over HTTP would be a new documented route and was deliberately out of this phase's scope.
7. **The action policy is enforced on the claim, not before the plan.** A plan the existing supervisor built is
   executed through NF-20/NF-19 as before; if it contained a kind the task did not permit, the task reports the
   breach and refuses completion. Pre-filtering would require a per-run action policy inside NF-20's planner,
   which is a cross-phase change this layer deliberately did not make.

## 14. Reproduction commands

```bash
export PYTHONPATH=src

# the phase suite
python -m pytest tests/test_novaforge_nf25.py -q

# the dependencies it stands on
python -m pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py \
    tests/test_novaforge_verification.py tests/test_novaforge_planning.py \
    tests/test_novaforge_nf24.py -q
python -m pytest tests/test_novaforge_api.py tests/test_novaforge_state.py \
    tests/test_novaforge_objectives.py tests/test_novaforge_control.py -q
python -m pytest tests/test_web_api.py -q

# the fixture judge + latency report
python scripts/novaforge_nf25_task_eval.py

# types and the API surface
python -m mypy src
python -m mypy src --platform win32
python scripts/generate_api_reference.py --check
```

## 15. Final acceptance checklist and verdict

| Criterion | Result | Evidence |
|---|---|---|
| A bounded, evidence-grounded task model | **PASS** | `TaskSpec` + 12 validation tests; vague goals and missing evidence refused |
| Reuse of existing NF-19 and NF-20 interfaces | **PASS** | `TaskRunner` delegates to NF-22, which drives NF-20 → NF-19; no new planner or input path |
| Safe, bounded closed-loop execution | **PASS on fixtures; not exercised live** | approval/target/policy/state/resource gates; budgets translated into `AutonomyLimits` |
| Fresh observation after actions | **PASS (inherited, pinned)** | NF-22 re-observes before verifying; NF-25's judge requires that run's evidence |
| Dispatch vs observed game response | **PASS** | three separate counters; dispatch-only fixture stays `outcome_unknown` with 0 responses |
| Reliable objective-progress tracking | **PASS** | per-cycle outcomes, `progress_only` vs `outcome_unknown`, no completion from progress |
| Evidence-backed completion verification | **PASS** | completion needs the terminal ending **and** every required evidence tag; 0 false completions |
| Bounded interruption and recovery behaviour | **PASS (inherited, mapped)** | recovery limit in the spec → NF-22 bounds; every ending maps to its own verdict |
| Regression tests and integration tests passing | **PASS** | 91 new + 417 + 198 + 269 existing tests green |
| Type checks and documentation validation | **PASS** | both mypy views clean (453 files); API reference in sync |
| No known unresolved, fixable, in-scope defect | **PASS** | nine defects found and fixed, each pinned (§12) |
| Approved live-game test demonstrating an objective | **NOT PERFORMED** | no launchable safe title; no approval requested (§8, §9) |

**Final verdict: PARTIAL.**

* **PASS** — the bounded task model, the preflight gates, the delegation to the existing supervisor, and the
  evidence-based judging, verified on deterministic fixtures and the full dependency suites. No known fixable
  in-scope defect remains.
* **NOT PERFORMED / BLOCKED** — live-game objective execution and its end-to-end measurements. The blocker is
  external (no launchable safe title, per NF-23/NF-24) plus procedural (no operator approval for a live input
  test), not a code defect.

Per the brief's stop condition, NF-25 ends here: NF-26 was not begun. The next step is an operator-approved live
task on a machine where a safe offline title actually starts, using the procedure in the plan §7.

## 16. Verification pass (same day, after the first draft)

The phase was then re-verified against the brief's objectives A–I. Summary of that pass:

| Objective | Re-check | Result |
|---|---|---|
| A — bounded task | every field and every refusal re-read against the code | **PASS** — 17 spec-validation tests; no field is decorative |
| B — evidence-grounded selection | is the task's permitted-action policy actually binding? | **DEFECT 6 found and fixed** (now a claim gate) |
| C — safe execution | who verifies the target, and what proves the input was released? | **PASS** — target/approval/policy/state/resource gates; held input is measured, never assumed |
| D — closed loop | is there a second loop anywhere in the layer? | **PASS** — none; `TaskRunner` only starts, waits, judges (NF-22 owns the loop) |
| E — progress and completion | are the nine stages reported separately? | **DEFECT 8 found and fixed** (`task_stages`) |
| F — recovery and endings | can a cancel leave a stale task or rewrite an ending? | **DEFECT 7 found and fixed** (ledger + ending semantics) |
| G — live-test design | procedure and approval gate present, nothing run | **PASS as a design; NOT RUN** |
| H — tests | suite green, nothing weakened | **PASS** — 91 phase tests; 417 / 198 / 269 dependency and integration tests |
| I — performance | layer overhead measured after the fixes | **PASS for the layer** — 0.0013–0.0019 / 0.030–0.032 / 0.060–0.075 ms per call; end-to-end cycle still needs a live target |

Commands re-run after the fixes (all from the repository root, `PYTHONPATH=src`):

| Command | Result |
|---|---|
| `pytest tests/test_novaforge_nf25.py -q` | **91 passed** |
| `pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_api.py tests/test_novaforge_control.py -q` | **269 passed, 37 subtests passed** |
| `pytest tests/test_novaforge.py tests/test_novaforge_autonomy.py tests/test_novaforge_verification.py tests/test_novaforge_planning.py tests/test_novaforge_nf24.py tests/test_web_api.py -q` | **492 passed, 146 subtests passed** |
| `mypy src` / `mypy src --platform win32` | both **clean** (453 files) |
| `python scripts/novaforge_nf25_task_eval.py` | fixtures **7/7**, false completions **0** |

Remaining issues and unverified requirements after the pass are unchanged and explicit: live-game objective
execution (no launchable safe title, no operator approval — §8/§9), end-to-end cycle latency and
invalid-observation rate (both need a live target), a commercial game's screens as evidence (no profile), and
the plan-time enforcement of the task's action policy (§13.7). No test was skipped, weakened or removed; the
eval gained a fixture rather than relaxing one.

## 17. NF-23…NF-26 consolidated acceptance (2026-10-28)

Re-audited with NF-23, NF-24 and NF-26. Full evidence:
`docs/NOVAFORGE_NF23_NF26_CONSOLIDATED_ACCEPTANCE_REPORT.md`; findings:
`docs/NOVAFORGE_NF23_NF26_REMEDIATION_REGISTER.md`.

**Verdict unchanged: PARTIAL** — PASS on the task model, preflight gates, delegation and evidence-based
judging; BLOCKED on live objective execution (no launchable title, no operator approval).

**What this pass added as evidence.** Through the real manager against a live session, an **unapproved**
task was refused before anything ran:

```
status: refused   verdict: ""   code: approval_required
reason: A task may send bounded input to the game, so it needs the operator's explicit approval.
        Nothing was started.
```

Two earlier attempts in the same probe were refused still earlier with `code: invalid_task` (a missing
action budget, then missing required evidence), which shows the preflight validates the spec before the
approval gate and that neither path starts anything.

**One defect found in this phase's boundary and fixed:** the session-start preflight silently ignored
unknown fields, so an operator who wrote the time limit in this layer's spelling (`max_seconds` instead of
`time_limit_seconds`) got a session with **no bound at all**. `NovaForgeManager.validate` now refuses every
unknown field by name and lists the accepted ones (R-01 in the register, pinned by
`test_an_unknown_start_field_is_refused_by_name` in `tests/test_novaforge.py`).

**One defect in the learning hand-off, fixed:** the record used the game's NAME as the run's profile
identity; it now takes the state layer's profile from the run. A broken recorder is also now pinned as
unable to fail a task (R-04/R-05).

No test was weakened and no live run was performed.
