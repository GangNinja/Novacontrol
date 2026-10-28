# NovaForge NF-20 — Game Planning and Action Selection

**Phase:** NF-20
**Date:** 2026-10-26
**Mode:** implementation, then two independent verification passes against every scope (A–J) in the shared checkout; no commits, no pushes, no live game input.
**Verdict:** **PASS WITH LIMITATIONS** — typed, bounded, evidence-grounded planning is implemented, wired to NF-19, exposed over HTTP and in the panel, and verified with deterministic tests. Live gameplay planning was **NOT_TESTED**, and the only shipped plan rule is the NF-17 labelled fixture.

The two verification passes found and fixed **five defects**. The first four came from re-reading the
phase against its own scope; one of them made a claim in the first version of this report false (plan
decisions never reached the session timeline). Section 17 lists them with the evidence for each fix
and the mutation check that proves the new tests are pins. The fifth (§18) left a cancelled
execution holding the planner permanently "busy". Section 19 lists what remains unverified.

---

## 1. Initial repository state

The checkout was already dirty from the preceding phases (NF-01…NF-19). No unrelated file was reset, stashed, overwritten or committed, and no process, game or drive was touched.

Verified present before this phase:

- `docs/NOVAFORGE_NF15_UNIFIED_GAME_OBSERVATION_REPORT.md`
- `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md`
- `docs/NOVAFORGE_NF17_OBJECTIVE_PROGRESS_REPORT.md`
- `docs/NOVAFORGE_NF18_GAME_KNOWLEDGE_MEMORY_REPORT.md`
- `docs/NOVAFORGE_NF19_GAME_CONTROL_INTERFACE_REPORT.md`

`src/novacontrol/novaforge/` contained the real implementations, not just the reports:
`observer.py` + `state_service.py`, `objectives.py` + `objective_service.py`,
`knowledge.py` + `knowledge_service.py`, and `control.py` (the NF-19
`GameControlService`, `ControlAction` and `ControlPolicy`).

## 2. Dependency verification

| Phase | Capability used | How it was verified | Result |
|---|---|---|---|
| NF-15 | validated observation frames, observer target | read `observer.py`; `tests/test_novaforge_observation.py` in the dependency run | PASS |
| NF-16 | typed `GameState` + `GameStateService` | read `state.py`/`state_service.py`; `tests/test_novaforge_state.py` | PASS |
| NF-17 | `UnderstoodObjective`, `ObjectiveState`, explicit completion | read `objectives.py`/`objective_service.py`; `tests/test_novaforge_objectives.py`; used the real fixture state in planner tests | PASS |
| NF-18 | durable, game-scoped `KnowledgeRecord` retrieval | read `knowledge.py`/`knowledge_service.py`; `tests/test_novaforge_knowledge.py`; planner consumes only VERIFIED records with a verified effect | PASS |
| NF-19 | one typed, bounded input operation with a gate and target identity | read `control.py`/`manager.py`; `tests/test_novaforge_control.py` (49 passed) | PASS |

Two small read seams were added, because the services kept their newest record
private and re-parsing `status()` dictionaries would have made the planner a
second model reader:

- `GameStateService.current` → the newest `GameState`.
- `ObjectiveService.current` → the newest `ObjectiveState`.

No second observation, state, objective, knowledge or control path was added.

## 3. Gap analysis

Before NF-20 the repository could observe (NF-15), read state (NF-16), interpret
objectives (NF-17), remember verified knowledge (NF-18) and send one bounded
input (NF-19) — but nothing chose **which** supported action to attempt. Gaps:

1. No planning context model binding a session, target, state, objective,
   knowledge, gate and action budget into one decision input.
2. No candidate representation, scoring or rejection record.
3. No plan record: objective reference, ordered steps, preconditions, expected
   effects, budgets, approval, execution history, invalidation, replanning.
4. No bounded execution orchestration over NF-19 (approval, per-step
   revalidation, stop conditions, cancellation, budget).
5. No replanning controls or repeat-plan guard.
6. No HTTP/UI surface and no audit events for planning decisions.

The generic `novacontrol/planning/` package was inspected and **not** reused:
it plans NovaControl *tool* workflows over a world model (goals, capabilities,
candidate tools, grounding), while NF-20 must plan typed game inputs over
NF-16/17/18 records and dispatch them through NF-19. Composing it would have
created a translation layer whose semantics (tool capabilities, world state)
do not exist for a game session. The phase's instruction ("start with a small,
testable planning algorithm appropriate to the verified representation") is met
by one module, `novaforge/planner.py`, composed over the existing services.

## 4. Files inspected and modified

**Inspected (relevant):** `novaforge/manager.py`, `control.py`, `state.py`,
`state_service.py`, `objectives.py`, `objective_service.py`, `knowledge.py`,
`knowledge_service.py`, `guardian.py`, `models.py`, `api/app.py`,
`api/models.py`, `api/route_consumers.py`, `web/static/js/novaforge.js`,
`web/static/index.html`, `tests/test_novaforge_*.py`, `planning/*`,
`docs/NOVAFORGE_NF15…NF19*`.

**Modified / created:**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/planner.py` | **new** — typed planning context, candidates, plans, policy, rule registry, bounded execution and replanning |
| `src/novacontrol/novaforge/manager.py` | ONE planner per manager; `_planning_context`, `planning_status`, `plan_actions`, `execute_plan`, `cancel_plan`, `replan_actions`, `submit_control_action`, plan events on the bus + session timeline; session stop/abort/shutdown cancel outstanding plans |
| `src/novacontrol/novaforge/timeline.py` | **verification pass** — the nine `novaforge.plan.*` event types and the `planner` source were ADDED to `EVENT_SPECS`/`SOURCES`; without them `TimelineLog.append` raised on every plan event and the manager's recording call swallowed it (see §17.1) |
| `src/novacontrol/novaforge/state_service.py` | `current` read-only accessor |
| `src/novacontrol/novaforge/objective_service.py` | `current` read-only accessor |
| `src/novacontrol/api/app.py` | five planning routes |
| `src/novacontrol/api/models.py` | `ApiSurface` rows for those routes |
| `src/novacontrol/api/route_consumers.py` | consumer rows for the panel |
| `docs/API.md` | regenerated (`scripts/generate_api_reference.py`) |
| `src/novacontrol/web/static/index.html` | Action Plan card in Mission Control |
| `src/novacontrol/web/static/js/novaforge.js` | plan read/build/execute/cancel/replan + `planView`/`renderPlan` |
| `tests/test_novaforge_planning.py` | **new** — 55 tests (41 in the first implementation, 12 added by the §17 pass, 2 by the §18 pass) |
| `tests/test_novaforge_ui.py` | 10 NF-20 behavioural and DOM tests |

## 5. Planning architecture

`GamePlanService` is the only planning entry point:

```
PlanningContext (session, target, GameState, ObjectiveState, verified knowledge,
                 gate, budget, state age, ControlPolicy)
        │
        ├─ PlanRuleRegistry  ── declared rule candidates (declared risk)
        └─ verified knowledge ── control records with a verified effect + binding
        │
   validate parameters through ControlAction.validate(ControlPolicy)
   check declared risk / irreversibility against PlanPolicy
   score (declared weighted sum)
        │
   select highest feasible candidate (bounded candidates + deadline)
        │
   ActionPlan (steps, preconditions, expected effects, rationale,
               rejected alternatives, budgets, approval, evidence, replan state)
        │
   execute(approved) ── one step at a time ── NF-19 submit seam
```

`GamePlanService` has no window, injector, event bus or store of its own:
`context_reader`, `submit`, `cancel_action` and `publish` are injected by the
manager. Every step is a `ControlAction`; nothing bypasses NF-19.

### Statuses

`planned`, `awaiting_approval`, `running`, `executed`, `stopped`, `failed`,
`cancelled`, `rejected`, `insufficient_information`, `no_action`,
`replan_required`, `replan_exhausted`, `invalidated`.

`executed` means **every submitted input operation completed**. It is
deliberately not `completed`/`success`, and every plan and API receipt carries
`effect_verified: false` with a note naming NF-21.

## 6. State and objective inputs

The manager builds `PlanningContext` from the session's own services:

- newest `GameState` and `ObjectiveState`; the objective must anchor to the
  **same observation** as the state (`objective_stale` otherwise);
- state age from the state timestamp; an unreadable age is `unknown_state_age`,
  never treated as fresh; age above `max_state_age_seconds` is `stale_state`;
- a state from another session is refused (`wrong_session_state`);
- `StateStatus.UNAVAILABLE/FAILED` is refused;
- session gate (`input_safety_decision`): a closed/session-less gate,
  unsupported input path or exhausted budget is `REJECTED`; a not-yet state
  (paused, pending validation) is `INSUFFICIENT_INFORMATION`;
- durable knowledge for the state's game only, through `KnowledgeService.records`;
- remaining action budget from the session's mission;
- **one** bounded refresh: when state is missing or stale and the gate allows,
  the state service re-analyses the observer's newest validated frame once. A
  refused analysis stays "no state"; there is no capture or retry loop.

Observed facts and inferred state stay separate: the plan records `state_status`
and `state_quality` in `evidence_refs`, and its reports state that the state is a
reading and the objective an interpretation, neither a verified outcome.

## 7. Candidate generation and selection

Two inspectable sources only:

1. **Declared rules** (`PlanRule`): rule token/game match, objective category or
   tokens, supported `ControlKind`, key/button/duration, required HUD element ids
   that must be `RECOGNIZED`, declared `risk` and `reversible`, alignment,
   repeatability. Matching never reads the screen.
2. **Verified knowledge** (`KnowledgeCategory.CONTROL` or
   `INTERACTION_RULE`): status `VERIFIED`, `verified_effect` true, explicit
   `key`/`button` binding in its content, and an objective match through
   `supports` tokens or an `objective` phrase. A verified effect with no input
   binding is reported as rejected, never guessed into a key press. An
   **undeclared risk is treated as HIGH** and refused by default.

A candidate carries: id, kind, description, expected effect, source, parameters,
preconditions (three-valued), evidence references, declared risk, reversibility,
alignment, expected progress, information gain, cost, repeatability,
`risk_declared`, optional confidence (**always `None`**; no probability is
invented), rejection reason, score and score parts.

Bounds: `max_candidates` (≤200) stops generation, `planning_deadline_ms` (≤5000)
stops it and reports `planning_timeout`, and progress counters cap the step count.
Generation failures are caught and reported as `candidate_generation_failed`.
A generation that stopped at a bound records `budgets.candidates_truncated = true`
and says "PARTIAL candidate set" in its reports, so a selection is never presented
as if every candidate had been seen (added in the verification pass, §17.2).
Scoring is the declared weighted sum in `PlanPolicy`: alignment, progress, cost,
risk, reversibility, information. Every score part is stored on the candidate.

Selection order is `(-score, kind, source, parameters)` — the candidate's own
declared shape, never the random suffix inside its generated id (§17.4).

Safety gate: `allow_high_risk=False` and `allow_irreversible=False` by default, so
a HIGH-risk or irreversible candidate is rejected with its reason unless a
deployment explicitly opts in.

Alternatives are kept on the plan (`candidates`) with their rejection text; the
rationale names up to three feasible candidates that lost on score (with their
scores) and up to four that were rejected (with their reasons), so the comparison
the selection actually made is in the record.

## 8. Preconditions and expected-effect model

Every candidate must satisfy all of: session gate, state present, state fresh,
objective open, objective **explicit** (stated by the screen, not inferred), and
action budget remaining, plus any rule-required HUD fields. The selected
candidate's parameters are validated by the NF-19 `ControlAction.validate` with
the session's `ControlPolicy`; an unsupported key, disallowed button, unbounded
hold or out-of-range coordinate is rejected with the control service's own
wording.

Each step carries `expected_effect` as a **hypothesis** and the plan says the
effect is not observed or verified by this phase. `ActionPlan.validate` rechecks
every step's action, session ownership and evidence references before the plan is
stored.

## 9. Approval and execution orchestration

- Default `PlanPolicy.require_approval=True`; a built plan is
  `awaiting_approval`. `execute()` without `approved=True` returns the plan with
  `code="approval_required"` and sends nothing.
- `execute()` re-reads the context before the first step and after every step
  (`refresh_between_steps`), rechecking gate, state freshness, objective identity
  and revision, the plan's actions and the remaining budget.
- One plan executes at a time (process-wide), so two plans cannot interleave
  inputs. A second execute gets `busy` and nothing is sent.
- Each step is submitted through `submit_control_action`, which re-reads the
  live window identity; a changed handle, pid or process start time is
  `target_changed` **before** dispatch. The action id is preserved so the record
  names exactly one operation.
- A refusal/failure stops the plan at that step; nothing is retried.
- `cancel` sets the loop flag (checked before every step) and asks NF-19 to
  cancel the in-flight action. Session `stop`, `cancel`, `emergency_stop` and
  shutdown cancel outstanding plan work before their own cleanup.
- The execution record stores per step: action id, kind, status, ok, code,
  reason, dispatch timestamp and `effect: None`; the plan stores start/finish
  timestamps and `effect_verified: false`.

Step outcomes map to plan outcomes: a cancelled action → `cancelled`; a timeout
or dispatch failure → `replan_required`; cleanup failure → `failed`; a gate
refusal (`closed`, `held`, `unsupported`, budget) → `stopped`; other refusals →
`replan_required`.

The state-derived preconditions are re-derived from the CURRENT reading on every
check (before the first step and after each refresh), not only when the plan was
built: a rule whose declared HUD field is no longer `RECOGNIZED`, a rule that is
no longer declared, or a knowledge record that is no longer `VERIFIED` stops the
plan with `precondition_false` (§17.3). A record the reader simply did not return
is not treated as withdrawn — that would be a guess, not evidence.

Every announcement is awaited before the planner returns (§17.1): a plan edge is
recorded in the session timeline by the time the caller can read it, and a broken
observer is still swallowed so recording can never fail the work.

## 10. Replanning

`replan(session, plan, reason)` accepts only the declared causes
(`operator`, `state_changed`, `action_failed`, `action_refused`,
`objective_changed`, `precondition_false`, `observation_stale`,
`session_invalid`, `budget_exceeded`); anything else is `unknown_trigger`.

- unknown plan/session → `unknown_plan` (HTTP 404 for an unknown session);
- a running plan → `plan_running`;
- `replan_count >= max_replans` → `replan_exhausted` / `replan_limit`;
- fresh evidence with the **same state id and same candidate sequence** →
  `replan_exhausted` / `repeat_plan_refused` (a failing plan is not replayed);
- fresh evidence that changes the state/plan → a child plan with
  `parent_plan_id`, `revision + 1`, `replan_count + 1`, and the previous pending
  plan is marked `invalidated` (`code="replanned"`) before the child exists, so a
  stale id cannot be executed later.

## 11. Failure handling

| Case | Result |
|---|---|
| No validated state | `insufficient_information` / `no_state` |
| State unreadable age / stale | `unknown_state_age` / `stale_state` |
| State from another session | `rejected` / `wrong_session_state` |
| State unavailable/failed | `insufficient_information` / `state_unavailable` or `state_failed` |
| No objective interpretation / stale interpretation | `no_objective_interpretation` / `objective_stale` |
| Multiple explicit objectives | `no_active_objective` (refuses to guess) |
| Completed counter / no open objective | `no_active_objective` (no step) |
| No rule or verified control | `no_plan_rule` |
| All candidates rejected | `no_action` / `no_feasible_action`, reasons kept |
| Candidate generation raised | `failed` / `candidate_generation_failed` |
| Planning deadline hit | `insufficient_information` / `planning_timeout` |
| Budget exhausted before execution | `rejected` / `budget_exhausted` |
| Approval missing | plan returned with `approval_required`, nothing sent |
| Closed/held/unsupported gate | `rejected`/`stopped` with the gate's own reason |
| Target changed | `target_changed`, nothing dispatched |
| Action refusal/failure/timeout | stop or replan_required, never silent continuation |
| Cancellation | `cancelled`, in-flight action asked to stop |
| Unknown replan cause | `rejected` / `unknown_trigger` |
| Declared HUD field no longer recognized mid-plan | `stopped` / `precondition_false`, no further step sent |
| Plan's rule no longer declared | `rejected` / `plan_invalid`, no further step sent |
| Knowledge behind a step no longer verified | `stopped` / `precondition_false` |
| Candidate generation hit its bound | the plan is `planned`/`awaiting_approval` with `budgets.candidates_truncated = true` and a PARTIAL-set report |

Every refusal is a stored plan record plus a `novaforge.plan.refused` event; no
in-game outcome is claimed.

## 12. Automated tests

Commands run from the repository root with `PYTHONPATH=src` (measured on the second verification
pass, after the fixes in §17 and §18):

```
python -m pytest tests/test_novaforge_planning.py -q
→ 55 passed, 26 subtests passed

python -m pytest tests/test_novaforge_ui.py -q
→ 156 passed, 94 subtests passed

python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_knowledge.py \
  tests/test_novaforge_objectives.py tests/test_novaforge_state.py \
  tests/test_novaforge_observation.py tests/test_novaforge_gameplay.py -q
→ 438 passed, 101 subtests passed

python -m pytest tests/test_novaforge_planning.py tests/test_novaforge_control.py \
  tests/test_novaforge_api.py tests/test_novaforge_gameplay_api.py \
  tests/test_api_reference_sync.py tests/test_route_shape_contract.py -q
→ 175 passed, 497 subtests passed

python -m pytest tests/test_novaforge_timeline.py tests/test_novaforge.py \
  tests/test_novaforge_closed_loop.py tests/test_novaforge_launcher.py \
  tests/test_desktop.py tests/test_desktop_input.py -q
→ 347 passed, 49 subtests passed
```

No failures and no skips in any of the five runs.

Coverage highlights (`tests/test_novaforge_planning.py`):

- **Models:** policy bounds, plans without evidence/objective, steps from another
  session or with an unsupported action, duplicate rule rejection, no rule for a
  commercial game, plan `action` validation.
- **Selection:** the NF-17 fixture objective yields two bounded steps; policy and
  budget caps; no state, stale/age-unknown state, closed gate, no exact objective,
  stale objective, complete counter, no rule, unsupported/missing-field rules,
  exhausted budget, verified knowledge, unbound knowledge, undeclared risk,
  HIGH-risk/irreversible policy gate, candidate-generation failure, planning
  timeout.
- **Execution:** approval required, submitted steps once each with distinct ids,
  refusal stops without retry, submit exception → `interface_error`, cancel before
  execution, concurrent-plan `busy`, mid-plan closed gate, mid-plan stale state,
  objective change → `invalidated`, budget exhausted, cancel during execution.
- **Replanning:** unknown trigger/plan, replan limit, identical-evidence refusal,
  fresh evidence child plan, invalidating a pending parent.
- **Events and lifecycle (verification pass):** the published edge sequence
  (`created → started → step → step → finished`) with `effect_verified: false`
  on every payload; a refusal publishes `novaforge.plan.refused`; a publish seam
  that raises never fails a plan or an execution; every plan event type is in
  `EVENT_SPECS` and `TimelineLog.append` really accepts one with source
  `planner`; shutdown refuses new plans, cancels a running one at its next
  checkpoint and leaves no step un-sent twice.
- **Selection (verification pass):** a generated id is not part of the selection
  key; a candidate set cut by `max_candidates` is reported as PARTIAL; the
  session's prior action count is recorded beside the plan's budget.
- **Execution (verification pass):** a step whose rule's declared field is no
  longer recognized stops with `precondition_false` after exactly one action;
  a knowledge plan's step stops when its record is no longer verified, and does
  NOT stop when the reader simply returned nothing.
- **Cancellation state (§18):** a cancelled `execute` — both during its first
  announcement and inside a step submission — restores the service (`running`
  false, the next attempt answered on its merits instead of `busy`) and records
  the plan `cancelled` / `caller_cancelled`, never `interface_error`.
- **Manager/API:** planning status, no-state plan sends nothing, target changed
  refused before dispatch, live target dispatches through NF-19 with the same
  action id, missing plan id 422, 404 for unknown sessions on every route,
  approval/no-state/refusal receipts, and the refusal read back from the stored
  session timeline (`novaforge.plan.refused`, source `planner`, category `plan`).
- **UI (Node):** `planView` gating for missing/ready/pending/running/executed/
  exhausted/blocked states; the real `renderPlan` builds objective, steps,
  approval, rejected candidates and history, and shows `not observed (NF-21)`;
  the unread card is honest and disables everything.

No test sends real input: the only injector is the project's recording double.

## 13. Type checks

```
PYTHONPATH=src python -m mypy src
→ Success: no issues found in 448 source files

PYTHONPATH=src python -m mypy src --platform win32
→ Success: no issues found in 448 source files
```

API reference is in sync:

```
PYTHONPATH=src python scripts/generate_api_reference.py --check
→ docs/API.md: in sync
```

## 14. Live verification

**NOT_TESTED.**

- The application's own resource gate was re-read on BOTH verification passes
  (`ResourceGovernor.assess()`): **TIGHT** — **2.6 GB available of 15.37 GB**
  (2,788,368,384 of 16,503,619,584 bytes; 2.76 GB at the first pass and 3.42 GB
  before it), battery 41%, CPU idle, GPU/NPU unreadable (reported `None`/false,
  never filled in). The reason is `free RAM is 2.6 GB, below the 4.0 GB comfort
  line`; `advise_load(256 MiB | 512 MiB | 2 GiB)` answers `allow=True` with
  `prefer_lightweight=True`. It is not CRITICAL, so the gate alone did not
  forbid a run at any of those readings.
- No live run was attempted because no verified game-state profile exists for a
  permitted offline target in this build. NF-20's only rule maps the NF-17
  **labelled fixture** ("collect" / counter) to a bounded `w` hold; the NF-19
  disposable harness exposes no such HUD/objective, so a live plan against it
  would either return `no_plan_rule` or require inventing a mechanic — which the
  phase forbids.
- No game was launched, no input was sent, no window/process was closed, nothing
  was force-killed, and the disconnected WD My Passport 2TB drive was not touched.

The live sequence, when a verified offline target and mapping exist, is:
detect plan → approve → submit → dispatch → input operation completes →
observe effect → verify effect. Only the first five can be produced by this
phase; the last two belong to NF-21.

## 15. Capability statement (what is actually verified)

| Claim | Status |
|---|---|
| Typed planning context from real NF-16/17/18 models | Verified by tests |
| Typed candidate + plan model with validation | Verified by tests |
| Rule and verified-knowledge candidate generation | Verified by tests |
| Bounded candidate count, planning deadline, action count, budget | Verified by tests |
| Declared risk/irreversibility refusal by default | Verified by tests |
| Preconditions from state/objective/gate, explicit objective only | Verified by tests |
| Approval required before any submission | Verified by tests |
| Steps submitted through the NF-19 path with the manager's budget/events | Verified by tests |
| Stop on refusal/failure/staleness/objective change, no retry | Verified by tests |
| Cancellation and session invalidation cancel plan work | Verified by tests |
| Target identity rechecked before each planned dispatch | Verified by tests |
| Bounded replanning, repeat-plan refusal, parent invalidation | Verified by tests |
| Plan audit events on the bus and session timeline | Verified by tests (the first version of this report had this WRONG for the timeline — see §17.1; the type is now registered and the event is read back from the stored timeline) |
| Panel card + HTTP routes | Verified by pytest/TestClient/Node DOM |
| **Real game reaction / in-game effect** | **Not observed. Not verified.** |
| **Live game control planning** | **NOT_TESTED** |

## 16. Known limitations, defects and risks

1. **One rule, fixture only.** No commercial game has a verified mapping in this
   build, so a real game returns `no_plan_rule`. This is intentional, not a
   missing prompt.
2. **No live proof.** The report's execution evidence comes from a recording
   submit seam; real dispatch behavior is NF-19's tested path, but this phase did
   not exercise it against a game.
3. **No effect verification.** `executed` means the input operations completed.
   NF-21 must observe and verify effects; nothing here infers them.
4. **Plan records are process-lifetime.** The session timeline persists plan
   status/code/reason events and NF-19's control results, but candidate details,
   rationale and plan history are not stored across a restart.
5. ~~**Approval is a request flag, not a second token.**~~ **RESOLVED (follow-up
   remediation pass).** As recorded: `/plan/execute` required `approved: true` and
   the UI's labelled button was the operator's approval, but an authenticated
   caller could set the flag directly. Plans now carry the same server-minted,
   single-use, time-limited token the desktop/browser/phone command plans carry:
   every route that serves a plan receipt mints it (`forge_plan_receipt`, for both
   the `plan` and `current` payload shapes, and only for a plan whose approval is
   `required` and still `pending`), and `POST /plan/execute` consumes it before
   anything is dispatched — a missing, unknown, expired, replayed or
   differently-bound token is **403** with nothing sent. The plan service's
   in-process `execute(..., approved=True)` contract is deliberately unchanged, so
   the caller inside the process is unaffected and the boundary is what tightened.
   The panel sends `approval_token` from the preview and repaints from a fresh
   preview when a refusal arrives, so an expired token is one press away from
   recovery, not a dead end. Pinned by six route tests plus a panel contract test
   in `tests/test_novaforge_planning.py` / `tests/test_novaforge_ui.py`, each
   mutation-checked. Detail: `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md`
   R-02.
6. **One plan at a time globally**, not per session. This is safe but coarser
   than necessary when multiple sessions exist.
7. **Refresh is one bounded attempt**, only when the gate allows. There is no
   observation loop in planning; a stale session needs a new state read before a
   new plan.
8. **No controller input.** Only the NF-19 keyboard/mouse/wait kinds are planned.
9. **Risk declarations are required for knowledge controls.** An undeclared risk
   is HIGH, so such a record is refused by default rather than guessed at MEDIUM.
10. **PlanPolicy is construction-time configuration.** There is no HTTP/UI control
    for the action cap, deadlines or risk policy; changing them requires wiring a
    different policy at construction.
11. **UI reads are poll-based** with the session card; there is no push channel
    for plan state.
12. **Planning and execution are separate presses, but approval and execution are
    one press in the panel.** Planning never sends input; the Execute button is
    the explicit approval action that submits the plan. `execute()` without
    `approved=True` refuses.
13. **No autonomous loop.** Nothing in this phase retries a failed plan, chains
    plans, or runs without an operator action.
14. **The state-dependent precondition recheck covers declared rules and
    verified knowledge only.** It re-derives what candidate GENERATION checked
    (a rule's declared HUD fields, a record's verification status). A future
    candidate source with its own state predicates must add them there; a
    precondition nobody re-derives is a precondition that expires.
15. **Candidate truncation is reported, not repaired.** A plan built from a
    partial candidate set says so (`budgets.candidates_truncated`, a PARTIAL-set
    report line); it still selects from what it has, because a bound that stops
    planning entirely would make a busy game unplannable.
16. **Timeline volume.** Every plan step appends an event, so a long plan adds
    one row per action to the session's bounded timeline. The bound itself is
    NF-03's; if plan rows crowd out capture evidence, a separate category filter
    is the follow-up, not a second log.
17. **A cancelled execution can lose its final `finished` announcement.** The
    cancelling await cannot complete while the task is being cancelled, so the
    `novaforge.plan.finished` edge is written only if that last await is not the
    one cancelled. The plan's status, its `cancelled` / `caller_cancelled`
    record and the restored service state are written regardless (§18.1).

## 17. Verification-pass findings (implemented here, with evidence)

Each item below was found by reading the phase against its own scope, fixed, and
pinned with a test that was mutation-checked (the forbidden behaviour was
re-introduced, the new test failed, the behaviour was restored).

### 17.1 Plan decisions never reached the session timeline (scope G)

**Defect.** `GamePlanService` published through `publish`, and the manager's
`_publish_plan` writes the session timeline with `_event`. `TimelineLog.append`
REFUSES an event type that is not in `EVENT_SPECS`, and `_event` swallows that
refusal — and no `novaforge.plan.*` type (nor the `planner` source) was ever
registered. Every plan decision was therefore dropped from the audit trail while
the code looked like it recorded one. Two smaller defects came with it:

- `_publish_now` scheduled the (async) publish with `asyncio.ensure_future` and
  returned, so the event was written whenever the loop next ran — after the
  caller had read its receipt, and never at all if the loop stopped first.

**Fix.** The nine `novaforge.plan.*` types and the `planner` source are registered
in `timeline.py`; the announcement helper is `async def _announce` and every
call site awaits it (a broken observer is still swallowed, so recording can never
fail a plan). `_record_refusal` became `async` for the same reason.

**Evidence.** `PlanningManagerTests.test_a_plan_decision_lands_on_the_session_timeline`
asserts the refusal is READ BACK from `manager.timeline_payload` with type
`novaforge.plan.refused`, source `planner`, category `plan`, code `no_state` and
`effect_verified: false`. Mutation: restoring `ensure_future` makes that test the
only failure (1 failed, 1 passed).
`PlannerEventSeamTests.test_every_plan_event_type_is_registered_on_the_timeline`
pins the vocabulary and appends one event to a real `TimelineLog`.

### 17.2 A truncated candidate set looked exhaustive (scope C)

**Defect.** Reaching `max_candidates` (or the planning deadline after some
candidates existed) silently planned from the part that was generated; only the
case with NO candidate at all was reported (`planning_timeout`). A reader could
not tell that alternatives had never been considered.

**Fix.** `_candidates` returns `truncated`; a plan built from a partial set records
`budgets.candidates_truncated = true` and appends a "PARTIAL candidate set"
report.

**Evidence.** `test_a_candidate_set_cut_by_its_bound_is_reported_as_partial`
(true when cut, false when not). Mutation: deleting the report line fails it.

### 17.3 A plan outlived the state its preconditions were checked against (scopes D/E)

**Defect.** Execution revalidated the gate, freshness, objective identity and the
actions' own parameters between steps — but never the STATE-derived
preconditions. The fixture rule declares the `progress` HUD field; if that field
stopped being recognized after step 1, step 2 was still sent. The intent was
visible as dead code (`if after_step and ... same state_id: if not
refresh_between_steps: return None` — unreachable, since the refresh that
produced the reading is exactly what `refresh_between_steps` enables) and as an
unused `after_step` parameter.

**Fix.** `_state_preconditions(plan, context)` re-derives them from the current
reading: the plan's rule must still be declared and its required fields still
`RECOGNIZED` (`precondition_false` / `plan_invalid`), and a knowledge record still
present in the reading must still be VERIFIED. A record the reader did not return
is not treated as withdrawn. The dead branch and the unused parameter are gone.

**Evidence.**
`test_a_step_after_a_state_change_rechecks_the_rules_declared_field` (STOPPED,
`precondition_false`, exactly one action submitted) and
`test_execution_rechecks_the_verified_knowledge_behind_a_step`. Mutation: removing
the recheck fails the first.

### 17.4 The selection key contained a generated name (scope C)

**Defect.** Feasible candidates were ordered by `(-score, candidate_id)`, and
`candidate_id` ends in `uuid4().hex[:6]`. The shipped ids all carry a
distinguishing prefix, so the effect was latent rather than live — but a generated
name is not a selection criterion, cannot be inspected, and would make the choice
between two candidates whose ids differ only in that suffix unreproducible.

**Fix.** `_selection_key` orders by `(-score, kind, source, parameters)`; equal
keys keep generation order (rules by id, then knowledge as read).

**Evidence.** `test_the_selection_key_never_contains_a_generated_name` and
`test_the_same_evidence_selects_the_same_rule_every_time` (which also asserts the
rationale names the alternative it beat and its score).

### 17.5 Checked and found correct (no change)

- Approval is required by default and an unapproved plan sends nothing; the five
  routes, their `ApiSurface` rows, their consumer rows and `docs/API.md` agree
  (`--check` in sync).
- Refusal codes cover every case in scope H; the bus path accepts `novaforge.*`
  because `validate_payload` only checks types the build declares.
- `PlanningContext.actions_executed` was written by the manager but read by
  nothing; it is now recorded in the plan's own budgets (scope B: the session's
  action history at build time) instead of being dead weight.
- No test in the phase suite sends real input; the only injector is the project's
  recording double (`RecordingInput`).

## 18. Second verification pass (this pass)

The scope was re-read end to end against the shipped code, the phase suite was re-run first, and
one further defect was found and fixed. Three claims carried in §4/§12 were also stale after the
§17 additions, and are corrected there (test count 41 → 55, planning run 53 → 55, integration run
173 → 175, and a fresh resource reading in §14).

### 18.1 A cancelled execution left the planner permanently "busy" (scope E)

**Defect.** `execute()` set its in-flight state (`_running`, `_running_plan`, `_running_session`,
`_cancel`) and then awaited the `novaforge.plan.started` announcement BEFORE entering the `try`.
Awaiting that seam is a suspension point, and the only protection around it was `_announce`'s
`except Exception` — which by design does not catch `asyncio.CancelledError`. A cancelled caller (a
disconnected request, an aborted task, a shutdown that cancels the task) therefore skipped the
`finally` entirely: `_running` stayed `True` and `_running_plan` kept naming a plan that was no
longer executing, so every later `execute` was answered `busy` and every later plan was refused for
the life of the process. The plan itself was left at `running`.

A second, smaller defect sat directly behind it: when a cancellation arrived inside a step
submission, the `finally`'s defensive branch recorded the plan as `failed` / `interface_error`
("The plan ended without a terminal status") — a claim that an interface fault occurred when in
fact the caller went away.

**Fix** (`planner.py`, `GamePlanService.execute`):

- the `started` announcement moved INSIDE the `try`, so the `finally` that restores the in-flight
  state runs on every exit path;
- an `except asyncio.CancelledError` records `cancelled` / `caller_cancelled` ("The executing task
  was cancelled; no further step was sent.") and re-raises, so the caller's task stays cancelled
  and the plan never claims a fault nobody observed.

Both branches are state restoration, not new capability: no step is sent, no effect is claimed.

**Evidence.**
`PlannerEventSeamTests.test_a_cancelled_execute_restores_the_service_and_records_itself` blocks a
real publish seam on the `started` event, cancels the executing task, and asserts
`service.running is False`, `plan.status == cancelled`, `plan.code == "caller_cancelled"`, no action
submitted, `counts["cancelled"] == 1`, and that the next `execute` is answered on its merits rather
than `busy`. `test_a_cancelled_step_submission_is_not_recorded_as_an_interface_error` cancels
inside a step submission and asserts the same state plus `plan.code != "interface_error"`.

**Mutation.** Putting the announcement back outside the `try` fails exactly
`test_a_cancelled_execute_restores_the_service_and_records_itself`
(`AssertionError: True is not false` on `service.running`) and nothing else; the fix was restored
and the whole file re-run green (55 passed).

### 18.2 Re-checked on this pass and left unchanged

- The five routes, their `ApiSurface` rows, their consumer rows and `docs/API.md` agree
  (`generate_api_reference.py --check` → in sync). The plan routes carry the NovaForge panel's own
  `no-render` marker, exactly like the other forty-odd NovaForge routes whose card polls in place;
  the generator's prose for that marker ("no web panel") is inherited, pre-existing wording for
  every panel-internal route, so it was left alone rather than rewritten for ~40 routes.
- `_failure_status`'s code sets still match NF-19's real refusal codes (`ControlStatus` values,
  `invalid_request`, `service_stopped`, `policy`, `unsupported`, `busy`, the gate codes,
  `target_lost`): a session/gate refusal stops the plan, anything else is `replan_required`, which
  is the scope-F trigger.
- `PlanningContext.active_objective()`'s ambiguity rule, `ActionPlan.validate`, the
  risk/irreversibility gate, the candidate/deadline bounds, the deadline/truncation report and the
  repeat-plan guard behave exactly as §7–§10 describe; `UnderstoodObjective.confidence` and
  `CandidateAction.confidence` are `None` upstream by design, so no plan invents one.

## 19. Unverified requirements

| Requirement (scope) | Status | Why |
|---|---|---|
| Live plan against a real game (scope J) | **NOT_TESTED** | no verified state profile or rule exists for a permitted offline target in this build; the only rule maps the NF-17 labelled fixture, whose HUD/objective the NF-19 harness does not expose |
| Approval-token gate on `/plan/execute` | **Resolved in the follow-up pass** (was limitation 5) | the route now requires and consumes a server-minted single-use token; six route tests + one panel contract test, mutation-checked |
| In-game effect observed / verified (scope J) | **Not observed, not verified** | effects belong to NF-21 by design; every plan and receipt carries `effect_verified: false` |
| Commercial-game planning (scopes A–F) | **Unverified** | no rule and no verified knowledge binding ships for any commercial game; a real game returns `no_plan_rule` instead of a guessed mechanic |
| Cancellation from a really-disconnected HTTP request | **Unit-verified only** | the restored state is pinned with real asyncio tasks, but no request was aborted mid-execute through uvicorn |
| Real dispatch behaviour inside a plan | **Unit-verified** | NF-20's execution evidence comes from the recording submit seam; NF-19's own suite covers the real dispatch path |
| Coarse concurrency | **Behaviour verified, scope wider than needed** | one plan executes process-wide, so two sessions cannot run plans at once (limitation 6) |
| Plan records across a process restart | **Not persisted** | plan history is process-lifetime; only the plan edge events persist on the session timeline (limitation 4) |
| "What is needed next" as a structured field (scope H.5) | **Prose + code only** | each refusal states its requirement (a fresh observation, the operator's decision, a replan) in `reason` and names the case in `code`; there is no separate field |
| Controller / gamepad input (scopes A, E) | **Not implemented** | only NF-19's keyboard/mouse/wait kinds are planned |

## Final verdict

**PASS WITH LIMITATIONS.**

The phase's definition of done is met for the deterministic scope: a typed,
validated plan representation; candidates limited to verified state/objective/
knowledge and NF-19-supported actions; inspectable selection criteria with
rejection reasons; explicit preconditions and hypothesis-only effects; approval,
budgets, deadlines, concurrency and cancellation enforced; safe stops on stale
evidence and changed objectives; bounded replanning; recorded rationale and
execution history; passing focused, dependency and UI tests plus both mypy views.

It is not a PASS on live game planning: no verified game has a plan rule, and no
live test was run. The missing live evidence is stated here rather than replaced
by a mocked claim.

### Unverified requirements

Stated once, in §19 — the single authoritative table.

### Verification-pass outcome

All five defects found across the two passes (§17, §18) are fixed and pinned; the
phase suite went from 41 to 55 tests, all five measured runs are green with no
skips, both mypy views are clean, `docs/API.md` is in sync, and the resource gate
was re-read on both passes. The one claim that was wrong before the first pass —
plan decisions reaching the session timeline — is now true and is read back from
the stored timeline by a test.

Per the phase's repository-protection rules: nothing was committed or pushed, no
unrelated change was reset, stashed or overwritten, no game was launched, no input
was sent, and the disconnected WD My Passport 2TB drive was not touched.
