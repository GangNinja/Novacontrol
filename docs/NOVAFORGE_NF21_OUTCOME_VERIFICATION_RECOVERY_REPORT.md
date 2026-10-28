# NovaForge NF-21 — Outcome Verification and Bounded Recovery

**Phase:** NF-21 (closes the observe → decide → act → **verify** → **recover** loop)
**Status:** `PASS WITH LIMITATIONS`
**Live verification:** `NOT_TESTED` (no game has a verified observable mapping — see §13)

---

## 1. Initial repository state

* Branch `main`, working tree carrying the uncommitted NovaForge NF-08 … NF-21 work
  (untracked: `novaforge/planner.py`, `novaforge/state_service.py`,
  `novaforge/verification.py`, `tests/test_novaforge_verification.py`). Nothing was
  committed, stashed, reset or discarded.
* The preceding phases were **verified by reading their code and running their tests**,
  not by trusting their reports:
  * NF-15 unified observation — `novaforge/observation.py`, `novaforge/observer.py`
    (`GameObserver`, validated frames, freshness/validation verdicts).
  * NF-16 state understanding — `novaforge/state.py`, `novaforge/state_service.py`
    (`GameStateService.analyze()` returns an `AnalysisOutcome` with a state **or** a
    refusal code; HUD rows carry `status`/`value`/`text`; `status()["history"]` keeps a
    bounded history — this is the baseline source NF-21 reads).
  * NF-17 objective understanding — `novaforge/objective.py`, `objective_service.py`
    (`ObjectiveService.observe(state)` → `ObjectiveState` with stable objective ids).
  * NF-18 game knowledge — `novaforge/knowledge.py` (+ `knowledge_service.py`):
    `KnowledgeRecord` with `category`, `status`, `evidence[].verified`,
    `verified_effect`, and free-form `content`.
  * NF-19 control interface — `novaforge/control.py` (`GameControlService`,
    `ControlAction.action_id`, execution records carrying `status/ok/code/reason`).
  * NF-20 planning — `novaforge/planner.py` (`GamePlanService`: `plan/cancel/replan/
    execute/record_effects`, `PlanPolicy`, `ActionPlan.execution`).
* Everything NF-21 needs was present: action ids, expected effects, a state reader that
  refuses stale readings, and a control path that never claims an effect.

## 2. Dependency verification

| Dependency | Verified capability | Result |
|---|---|---|
| NF-15 | Validated frames + freshness verdicts | ✅ present (`observer.py`) |
| NF-16 | `analyze()` → state or coded refusal; bounded history | ✅ present (`state_service.py:277`) |
| NF-17 | Objective id/revision from a state | ✅ present (`objective_service.py`) |
| NF-18 | Verified control records with declared effects | ✅ present (`knowledge.py`) |
| NF-19 | Dispatch results (`status/ok/code`) + `action_id` | ✅ present (`control.py`) |
| NF-20 | Plan + execution record + `record_effects` | ✅ present (`planner.py:1942`) |

No phase was assumed complete because a report existed: each claim above was checked in
source, and the 12-file NovaForge suite (679 tests) was run (§11).

## 3. Gap analysis

| Gap | Resolution |
|---|---|
| No expected-outcome contract per planned step | `EffectContract` built from the plan's own step + a declared observable |
| No post-action evidence acquisition | `GameVerificationService.verify` asks the state reader for a fresh reading through the manager seam |
| No outcome classification | seven distinct `OutcomeStatus` values (never one "failure") |
| Input completion was the only success signal | only a *reading* can satisfy a declared observable; `dispatch_code` is recorded but never upgraded to success |
| No bounded recovery | `RecoveryAction` + `recovery_block` bounds (attempts, session cap, replan cap) |
| Recovery could have re-sent input | the service has **no dispatch seam at all** (asserted by test) |
| A refused precondition looked like a plain miss | `dispatch_code` carried on the contract/outcome → `INVALIDATE_PLAN` |
| Evidence exhaustion looked like a timeout | `attempts_exhausted` vs `deadline_reached` kept apart |
| The verdict was truncated out of the timeline | bounded `_verification_summary` for the durable timeline |
| `WAIT_FOR_TRANSITION` and `PAUSE_SESSION` were declared but **no decision rule selected them** | both are now chosen from evidence (a transition measured in progress; a screen that was only ever stale) and covered by tests at three levels |
| No session-level recovery **time** bound (only counts) | `max_recovery_ms` budgets the total time a session may spend recovering, re-read when a recovery is applied |

Deliberate **non**-goals: no second planner, no second input path, no autonomous
gameplay loop (NF-22 owns that), no invented success criteria.

## 4. Files inspected and modified

**Modified in the verification pass (NF-21 §verification):** the two gaps below were found
by auditing every scope against the code, fixed, and pinned by ten new tests (109 total,
up from 97): `verification.py` (verdict motion numbers, the wait/ pause rules, the session
recovery-time bound), `test_novaforge_verification.py`.

**Inspected (read, not modified):** `novaforge/observation.py`, `observer.py`, `state.py`,
`state_service.py`, `objective.py`, `objective_service.py`, `knowledge.py`,
`knowledge_service.py`, `control.py`, `guardian.py`, `models.py`, `timeline.py`;
`api/app.py` (route conventions), `api/models.py`, `api/route_consumers.py`;
`tests/test_novaforge.py`, `test_novaforge_planning.py`, `test_novaforge_gameplay.py`,
`test_novaforge_gameplay_api.py`, `test_novaforge_objectives.py`,
`test_novaforge_control.py`.

**Created / modified for NF-21:**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/verification.py` | **new** — the whole subsystem (contracts, evidence, classification, recovery, service) |
| `src/novacontrol/novaforge/planner.py` | `record_effects` accepts NF-21 outcomes onto the plan it describes |
| `src/novacontrol/novaforge/manager.py` | `GameVerificationService` wiring, evidence seams, `verify_outcomes`/`recover_outcome`/`cancel_verification`/`verification_status`, bounded timeline summary |
| `src/novacontrol/novaforge/timeline.py` | `novaforge.outcome.started/verified/recovery` vocabulary (`outcome` category) |
| `src/novacontrol/novaforge/__init__.py` | NF-21 exports |
| `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py`, `docs/API.md` | four routes registered in all five places |
| `tests/test_novaforge_verification.py` | **new** — 97 tests, 23 subtests |

## 5. Verification-contract design (Scope A)

`EffectContract` (one per executed step) carries: `contract_id`, `session_id`, `plan_id`,
`action_id`, `step_index`, `kind`, `expected_effect`, `requirements`, `pre_state_id`,
`pre_observation_id`, `pre_sequence`, `deadline_ms`, `settle_ms`, `confidence_threshold`,
`failure_conditions`, `recovery_eligible`, `dispatch_code`, `source`, `problems`.

An observable is declared, never invented: `ObservableRequirement(field, comparison,
value, min_change, tolerance, change_kinds)` with eight comparisons
(`changes/increases/decreases/equals/at_least/at_most/present/absent`). Two declared
sources feed a contract:

1. a **shipped rule** — exactly one exists, `fixture-collect-effect@1`, labelled
   `fixture_only`, keyed on the NF-17 fixture game/objective/kind/key;
2. a **VERIFIED NF-18 control record** whose `content` declares `observable` and whose
   `verified_effect` evidence exists, matched on the step's observed kind + key.

Anything else is `problems=(...)`, i.e. `NOT_VERIFIABLE`: *no commercial game gets a
success criterion in this build*. Two rules claiming one step is likewise refused
("refusing to guess") rather than resolved.

## 6. Evidence collection and freshness (Scope B)

`GameVerificationService.observe(session_id)` → the manager's
`_observe_for_verification`, which calls `GameStateService.analyze()` and returns an
`ObservationEvidence` (observation id, state id, session, sequence, timestamp,
`age_seconds`, status, `accepted`, readings, change rows, capture verdict, target).

`_evidence_gate` rejects, with a code, in this order: a non-accepted reading, a reading
from another session, a reading from another **target**, an unreadable timestamp, a
stale reading (`age > policy.max_state_age_seconds`), a reading older than the plan's own
state (`sequence < pre_sequence`, where `0` means "not recorded"), and a reading already
scored in this run (duplicate). **A rejected reading can never score, and a field that was
never read is `indeterminate` — missing evidence is not negative evidence.**
`settle_ms` and the retry interval are awaited through an injected `sleep` (never
`time.sleep`, so the event loop is never blocked).

## 7. Outcome classification (Scope C/D)

| Status | Established by |
|---|---|
| `VERIFIED_SUCCESS` | every declared observable satisfied by a direct reading |
| `VERIFIED_FAILURE` | an observable was read and did not meet the expected effect |
| `PARTIAL_SUCCESS` | some observables satisfied, the rest undecidable |
| `OUTCOME_UNKNOWN` | readings exhausted without deciding anything |
| `VERIFICATION_TIMED_OUT` | the contract/run deadline actually expired |
| `VERIFICATION_ABORTED` | cancelled, or the session/target became invalid |
| `NOT_VERIFIABLE` | no observable is declared for this step |

`merge_verdicts` keeps the **newest** verdict per requirement (stale evidence never
overrides newer) and reports a contradiction instead of picking a side; a contradiction
forces `OUTCOME_UNKNOWN` with code `conflicting_evidence` and the ambiguity text.

A reference requirement's verdict keeps the change it MEASURED as numbers (`motion`, the
signed delta, and `motion_target`, the magnitude the contract declared), not only as the
rendered text. That is what lets the recovery layer tell *the transition is in progress*
from *the effect did not happen* without parsing a string — see §8.

`confidence` is documented everywhere it appears (`confidence_basis`, the policy's
`confidence_note`) as *the share of declared observables decided by a direct reading — a
heuristic, not a calibrated probability*.

## 8. Recovery policies and configured bounds (Scopes E/F)

Responses: `NONE`, `REFRESH_OBSERVATION`, `WAIT_FOR_TRANSITION`, `REEVALUATE_STATE`,
`INVALIDATE_PLAN`, `REPLAN`, `PAUSE_SESSION`, `REQUEST_OPERATOR`, `STOP_OPERATION`. **No
member of that enum can send input**, and the service exposes no dispatch seam (a test
asserts the absence of `submit`/`execute_action`/`dispatch` and that no enum value names a
key/mouse/click). Every member is reachable from the decision model — two of them were
not before this pass (§3), which is what a declared-but-undecidable response looks like.

Decision rules (each evidence-based, never a universal retry): success → none; session
gate unsafe or target-lost → stop; objective changed → invalidate; aborted → stop;
not-verifiable → operator; partial → **wait** when a reference observable was measured
moving toward its target, otherwise re-evaluate; unknown → refresh while a reading is
available, **pause** the session when every reading was stale and none was ever scored,
otherwise operator; timeout → operator; a REFUSED dispatch → invalidate; a measured
transition short of its target → **wait** (once per action); a plain failed effect →
replan (which is itself only a *plan*, needing its own approval).

Bounds (hard ceilings in code, a policy may only lower them):

| Bound | Default | Ceiling |
|---|---|---|
| `max_steps_verified` | 4 | 8 |
| `max_observation_attempts` | 3 | 5 |
| `max_verification_ms` | 30 s | 60 s |
| `max_action_ms` | 8 s | ≤ run deadline |
| `max_recovery_attempts` (per action) | 2 | 3 |
| `max_replans_per_plan` | 1 | 3 |
| `max_session_recoveries` | 4 | 8 |
| `max_transition_waits` (per action) | 1 | 3 |
| `max_recovery_ms` (per session) | 20 s | 60 s |
| `max_state_age_seconds` | 5 s | — |

The bounds are re-read when a recovery is **applied**, not only when it was decided, so a
decision recorded before the first attempt cannot authorise the next one past its limit.
The session's recovery **time** is measured around the applied recovery (with the injected
monotonic clock) and counted whether the seam worked or not: a recovery that failed still
spent the session's budget for recovering.

## 9. NF-19 / NF-20 integration (Scope G)

The eight stages stay distinct and are each observable: plan generated (`plan_actions`) →
plan approved (`approved: true`) → action dispatched and input completed (NF-19 records)
→ post-action observation acquired (`analyze()`) → outcome evaluated (`verify_outcomes`) →
recovery decided (`recovery.decision`) → recovery applied (`recover_outcome`) → plan
completed/invalidated/paused (`record_effects`, `cancel`). Correlation is by
`action_id`/`plan_id`; `verify()` refuses a `plan_id` that does not match the resolved
plan, so outcomes can never be attached to the wrong actions.

Outcomes travel back through the planner's **own** `record_effects`, a failing record seam
is reported as a limitation and never changes the verdict, and every recovery seam
(`invalidate`, `replan`, `pause`, `stop`, `notify`) is the existing NF-20/session
interface — including the pause, which reaches the session's own `pause()` (the same path
a pause request uses, which is what releases held inputs). There is no second planner and
no second input service.

## 10. Failure handling and input cleanup (Scope H)

Handled and tested: missing state, stale readings, wrong session, wrong target, invalid
target, unknown/deleted plan, conflicting evidence, unverifiable action, two rules for one
step, refused dispatch, planner timeout (`deadline_reached`), candidate/reading exhaustion,
approval refusal, recovery-seam failure, cancellation, concurrent verification (`busy`),
plan-less session, shutdown, and resource/gate refusal.

On a critical safety failure the STOP recovery (a) stops further dispatch, (b) invalidates
the outstanding plan, (c) stops or pauses the session **through its own interface** — which
is the existing path that releases held inputs — and (d) records the stop result. If no
session interface is wired it reports `no_session_interface` and does **not** claim a
cleanup, because a cleanup claim without evidence is exactly what this phase forbids. No
process is killed, no window is closed, no game is touched.

## 11. Automated test commands and exact results

```
$ PYTHONPATH=src python -m pytest tests/test_novaforge_verification.py -q
109 passed, 27 subtests passed in 27.10s

$ PYTHONPATH=src python -m pytest tests/test_novaforge_control.py tests/test_novaforge_planning.py \
    tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py \
    tests/test_novaforge_observation.py tests/test_novaforge_state.py \
    tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py \
    tests/test_novaforge_timeline.py tests/test_novaforge_api.py tests/test_novaforge_ui.py \
    tests/test_novaforge_closed_loop.py -q
679 passed, 153 subtests passed in 112.83s

$ PYTHONPATH=src python -m pytest tests/test_novaforge.py tests/test_novaforge_policy.py \
    tests/test_novaforge_evaluation.py -q
173 passed, 43 subtests passed in 53.86s

$ PYTHONPATH=src python -m pytest tests/test_route_shape_contract.py tests/test_web_platform.py \
    tests/test_web_api.py tests/test_novaforge_launcher.py tests/test_application.py -q
224 passed, 582 subtests passed in 216.54s

$ PYTHONPATH=src python -m pytest tests/test_novaforge.py tests/test_novaforge_policy.py \
    tests/test_novaforge_evaluation.py tests/test_novaforge_evaluation_api.py \
    tests/test_novaforge_benchmark.py tests/test_novaforge_benchmark_api.py \
    tests/test_novaforge_coach.py tests/test_novaforge_experiment_api.py \
    tests/test_novaforge_embedding.py tests/test_novaforge_vision.py -q
471 passed, 181 subtests passed in 345.57s

$ PYTHONPATH=src python scripts/generate_api_reference.py --check
docs\API.md: in sync

$ PYTHONPATH=src python -m mypy src
Success: no issues found in 449 source files
$ PYTHONPATH=src python -m mypy src --platform win32
Success: no issues found in 449 source files
```

No skips, no xfails, no failures. **Tests not run:** the full repository suite (not
required for this change) and `scripts/ui_audit.py` (it imports `playwright`, which is not
installed in this environment; no NF-21 UI exists to audit — see §14).

**Mutation checks** (the harness must bite, not merely pass):

| Mutation | Failing tests |
|---|---|
| `_sleep_before_retry` returns `deadline_reached` where it means `attempts_exhausted` | `test_an_unreadable_counter_is_unknown_and_never_a_failure`, `test_a_repeated_reading_is_a_duplicate_not_a_second_score`, `test_an_observation_seam_that_raises_is_rejected_evidence`, `test_a_fresh_observation_recovery_re_reads_without_repeating_anything`, `test_a_session_with_no_state_reports_no_evidence_rather_than_a_result` |
| the stale-reading gate disabled | `test_a_stale_reading_is_rejected_and_drives_a_refresh`, `test_a_screen_that_is_only_ever_stale_pauses_the_session`, `test_a_session_with_no_state_reports_no_evidence_rather_than_a_result` (+4 as above) |
| `transition_pending` always `False` (the wait rule disabled) | `test_every_declared_recovery_response_is_reachable_from_a_rule`, `test_a_partial_effect_still_moving_is_waited_for_and_a_static_one_is_re_evaluated`, `test_a_transition_still_arriving_is_waited_for_and_never_replanned`, `test_a_transition_wait_waits_reads_again_and_is_allowed_only_once`, `test_a_counter_that_is_still_moving_is_waited_out_then_read_again` |
| the all-stale pause rule disabled | `test_every_declared_recovery_response_is_reachable_from_a_rule`, `test_evidence_that_was_only_ever_stale_pauses_the_session_safely`, `test_a_screen_that_is_only_ever_stale_pauses_the_session`, `test_a_state_reader_that_is_only_ever_stale_pauses_the_session` |
| the session recovery-time budget disabled | `test_the_session_recovery_time_budget_bounds_a_recovery`, `test_time_spent_recovering_is_counted_against_the_session_budget` |

Every mutation was reverted; the suite was green again afterwards, and `# MUTATION` markers
were asserted absent from the tree.

## 12. Type-check results

`mypy` is clean in **both** CI views (`src` and `src --platform win32`, 449 files each).
`python scripts/generate_api_reference.py --check` reports `docs/API.md` in sync, so the
four new routes are registered in the handler, the `ApiSurface`, the consumer manifest and
the reference.

## 13. Live verification results

**`NOT_TESTED`.** Reasoning, in the order the phase asks for it:

1. **Dependency readiness:** the pipeline is ready — the manager-level integration test
   drives the *real* manager (plan → approve → NF-19 dispatch → post-action observation →
   classification → `record_effects` → session timeline) with a recording injector.
2. **A verifiable observable:** no. The only declared rule in this build is the NF-17
   *fixture* rule (`fixture_only=True`); the fixture's `progress` counter is a labelled
   scenario value, not a game HUD. No commercial game has a verified effect mapping, so a
   live run against any installed game would be `NOT_VERIFIABLE` **by design** — inventing
   a success criterion to make a live test pass is the exact failure this phase forbids.
3. **A permitted target:** the phase forbids launching GTA V, duplicate instances,
   force-killing processes, the disconnected WD My Passport drive, and online play.
   The disposable-window harness (`scripts/forge_harness_window.ps1`) exists for capture
   work, but it produces no NF-17 profile, so `plan_actions` correctly refuses with
   `no_state` — there would be nothing to execute and therefore nothing to verify.
4. **What *was* executed for real:** NF-19 dispatch through the real control path into a
   recording injector (asserted: `key:w` was pressed exactly once, and recovery replayed
   no input), real NF-16/NF-17/NF-20 objects, the real planner record API, the real
   timeline, and the real HTTP routes through `TestClient`. The two recovery responses
   added in the verification pass were driven through the **real manager** as well: the
   wait path waited out the settle window and read a NEW conclusion from a later state
   (counter 1.5 → 3.0 → `verified_success`, with NF-19's input list unchanged), and the
   all-stale path really paused the session record (`SessionState.PAUSED`) through the
   session's own interface, again sending nothing.

Consequently the phase's live chain is reported as: **plan generated ✅ (mock window),
plan approved ✅, action submitted ✅ (recording injector), input dispatched ✅ (recorded),
in-game effect observed ❌ (no game), in-game effect verified ❌ (not attempted).**

## 14. Known limitations and unresolved risks

1. **No real game is verifiable in this build.** One fixture rule exists by design; every
   live step would report `NOT_VERIFIABLE`/`OUTCOME_UNKNOWN`. NF-22 must not read that as
   "verification works for games".
2. **Confidence is a heuristic share**, not a probability; it is labelled as such in the
   model, the policy and the API.
3. **The integration tests swap the state reader's read surfaces** (a `PropertyMock` for
   `current`, and stubs for `analyze`/`status`) with a real `GameState` fixture, because
   this host has no game to perceive. The perception → state path itself is covered by the
   NF-15/NF-16 suites, not by NF-21's integration tests.
4. **No NF-21 web panel.** The four routes are declared `no-render` in
   `api/route_consumers.py` and documented in `docs/API.md`; the verdict is visible through
   the API, the plan card (`effect_verified`/`effect_note`) and the session timeline. A
   panel card is a UI change that was not requested in this pass.
5. **No browser/UI verification was performed** (`playwright` unavailable, and there is no
   NF-21 UI to drive).
6. **Adjacent risk, not changed:** the manager also hands the whole `plan.to_dict()` to the
   timeline; it currently fits the timeline's payload bound (20 keys / 300 chars) and its
   tests pass, but it is one field away from the truncation that NF-21's payload had. A
   follow-up should give plan events the same bounded-summary treatment.
7. **`replan` is bounded by the planner's own policy** (a plan that is not stopped may
   refuse a replan); NF-21 reports whatever the planner returns and never fabricates a new
   plan.
8. **The stale/refresh boundary is a deliberate split, and it changed in this pass.** A
   stale reading *among* usable ones still asks for a fresh observation; a run in which
   EVERY frame was too old (nothing was ever scored) now pauses the session instead of
   spending the recovery budget on another read of the same frozen screen. The old
   behaviour refreshed in both cases and ignored the reading budget, so
   `test_a_fresh_observation_recovery_re_reads_without_repeating_anything` and
   `test_a_stale_reading_is_rejected_and_drives_a_refresh` now use the mixed shape (a
   stale frame plus a readable-but-undecidable one), which is what the refresh path is
   for. A paused session still needs the operator: the recovery is side-effecting, so it
   is only carried out with approval.
9. **Verification reads the state reader's fields only.** An effect with no declared HUD
   field cannot be established, so it is `NOT_VERIFIABLE` rather than guessed.

## 15. Final verdict

**`PASS WITH LIMITATIONS`** — every NF-21 scope (A–K) is implemented and covered by tests;
109 focused tests, 679 NovaForge dependency tests, 224 API/UI/platform tests, 173
core/policy/evaluation tests and both mypy views pass, and five mutations of the
load-bearing rules were each caught by the suite. Every response in Scope E's list is now
reachable from the decision model, and every bound in Scope F's list — including the
session's recovery TIME — is enforced in code and re-read when a recovery is applied. The
limitations are declared, not hidden: **no game is verifiable in this build** (only the
labelled NF-17 fixture declares an observable), confidence is a heuristic, the
verification evidence path in tests is fixture-driven rather than perceived, no UI panel
exists, and live verification is `NOT_TESTED`.

Eleven defects found in this pass were fixed: evidence exhaustion reported as a deadline;
a requested `plan_id` ignored; non-dispatched steps dropped from the report; a stored
recovery decision bypassing the attempt bounds; a refused dispatch classified as a plain
miss; a wrong-target reading retried instead of aborting; a bogus field-vs-comparison
check; the timeline truncating the verdict out of its payload; step rows reported out of
plan order; `WAIT_FOR_TRANSITION` and `PAUSE_SESSION` declared but never selected by any
rule (with the stale-frame path refreshing past its reading budget); and no session-level
bound on the TIME spent recovering.

**NF-21 decides what actually happened and what to do when it is uncertain. NF-22 may now
integrate it — with the caveat above that no game has a verified observable yet.**
