# NovaForge NF-22 — First Bounded Autonomous Gameplay: Verification Report

**Phase:** NF-22 — First Autonomous Gameplay
**Date:** 2026-10-26
**Verdict:** **PASS WITH LIMITATIONS** — the bounded autonomous loop works end to end over
the shipped NF-16…NF-21 stack on simulated evidence, with every limit, gate and safety stop
that the phase requires. **Live gameplay is NOT_TESTED**, and this report says so in every
place where that distinction matters.

This report distinguishes five kinds of statement, and never lets one stand in for another:

| Label | Means |
| --- | --- |
| **implemented** | the code exists and is reachable in this build |
| **mocked/simulated** | exercised against a fixture world or a hand-written answer |
| **live (offline)** | exercised against the real application, its real services and this host |
| **observed effect** | a measurement read back after an action (NF-21 verdict) |
| **verified gameplay outcome** | an objective confirmed complete from a fresh reading of a real game — **not achieved in this phase** |

---

## 1. Initial repository state

- Branch `main`, working tree **dirty and uncommitted** (NF-01…NF-22 all sit in one
  uncommitted working tree; this phase neither commits nor pushes, and no file it did not own
  was reset, stashed or discarded).
- Pure-Python FastAPI application; no `package.json`. `.venv` holds an editable install, so
  source edits are live on restart. Tests and mypy need `PYTHONPATH=src`.
- NF-15…NF-21 modules present and importable (`observer`, `state_service`,
  `objective_service`, `knowledge_service`, `control`, `planner`, `verification`), each with
  its phase report in `docs/`.
- One pre-existing closed control loop exists (`novaforge/closed_loop.py`, NF-08) that drives
  a *single* policy action per invocation. NF-22 does **not** replace or fork it; the
  autonomous supervisor composes the same manager methods that loop already uses.
- No game had been validated for autonomous control. The build's own offline fixture
  scenario (`novaforge/simulation.py`) was therefore the only environment in which a closed
  loop could be exercised honestly in this session.

## 2. Dependency matrix

Produced by the acceptance script (`scripts/novaforge_nf22_autonomy_acceptance.py`,
step 2), which imports each phase's module and asserts the surface NF-22 consumes exists:

| Phase | Module | Surface NF-22 consumes | State |
| --- | --- | --- | --- |
| NF-15 unified observation | `novaforge/observer.py` | `GameObserver` | implemented |
| NF-16 state understanding | `novaforge/state_service.py` | `GameStateService` | implemented |
| NF-17 objectives | `novaforge/objective_service.py` | `ObjectiveService` | implemented |
| NF-18 knowledge | `novaforge/knowledge_service.py` | `KnowledgeService` | implemented |
| NF-19 control interface | `novaforge/control.py` | `GameControlService` | implemented |
| NF-20 planning | `novaforge/planner.py` | `GamePlanService` | implemented |
| NF-21 outcome verification | `novaforge/verification.py` | `GameVerificationService` | implemented |

All seven phases are implemented and reachable; the supervisor was reconciled against their
**real** interfaces rather than a guessed API. No upstream phase was bypassed with a
monolithic replacement, and none of them was treated as done merely because its report
exists — each one is driven through its own service object in the closed loop below.

## 3. Selected game and task — simulation, and why

**Live game: NOT_TESTED. Simulated environment: used.**

NF-22 requires a game whose launch/embedding compatibility (NF-13/NF-14), capture
reliability (NF-15), observable state (NF-16), objective and success/failure signal (NF-17),
control surface (NF-19) and outcome measurability (NF-21) have all been verified — and it
explicitly forbids reading *permission to launch* as *permission to control autonomously*.
No installed title carries that evidence in this build, so no session was started and no
input was sent to a real game.

The environment used instead is the build's **own declared fixture scenario**
(`novacontrol/novaforge/simulation.py`): the objective registry entry
`nc-forge-mission@1` ("Collect the audit tokens", completion criteria *reach 3 on progress /
read 'mission complete' / read 'objective complete'*), its shipped plan rule and its shipped
verification rule. Nothing was invented for the simulator's convenience — the simulator
supplies pixels-in-a-scene, a counter and a key handler, and the real NF-16…NF-21 services
do the interpreting, planning, executing and verifying.

Why that is the honest choice, in the phase's own terms: the task's measurable completion
condition exists and is independently observable in the fixture world, and it is
deterministic, reversible and offline. It proves orchestration, limits and verification. It
proves nothing about a real game.

## 4. Session architecture

- **One supervisor:** `AutonomousSessionController` (`novaforge/autonomy.py`, 2 418 lines).
  It owns no window, captures no frame and injects no input. It is a supervisor over a
  gateway protocol, and it drives the **manager** — in the application, the real
  `NovaForgeManager`.
- **No second session manager:** the manager's session lifecycle remains the only one. A run
  ends through the session's own `stop`, which releases held input; the pre-existing
  `closed_loop.py` is untouched.
- **The gateway is the whole vocabulary:** `observe_frame_once`, `read_state`,
  `objective_picture`, `plan_actions`, `execute_plan`, `verify_outcomes`, `recover_outcome`,
  `pause`, `resume`, `stop`, `get`. Nothing else is reachable from the controller — there is
  no side door to the control interface.
- **Evidence source is declared, never inferred:** the manager's controller is
  `evidence_source="live"`; the fixture world's is `"simulated"`. The value travels in the
  run record, the status payload and the trace, and the UI badge reads it (§8).
- **Persistence:** runs are written through the shared `JsonStateStore` namespace; the trace
  is the run's own record, so it survives a restart without a second store.

## 5. State transitions

Fourteen phases, adapted from the phase specification to the project's naming:

```
IDLE → INITIALIZING → OBSERVING → UNDERSTANDING_STATE → IDENTIFYING_OBJECTIVE → PLANNING
     → [WAITING_FOR_APPROVAL] → EXECUTING → VERIFYING → (RECOVERING →) OBSERVING → …
     → COMPLETED | CANCELLED | FAILED ;  PAUSED ⇄ back into the loop
```

- `AUTONOMY_TRANSITIONS` is the **only** edge table; an illegal move raises
  `AutonomyTransitionError` and leaves the state unchanged (it never overwrites).
- Active phases: the nine working phases plus `WAITING_FOR_APPROVAL`. Terminal:
  `COMPLETED`, `CANCELLED`, `FAILED` — **no exit edges**, so a finished run cannot be walked
  back into work.
- Every move is recorded as a step with its phase, reason and timestamp; the observer/state/
  objective/plan/action/verification references travel with it.
- The reason for every pause, cancellation and failure is stored on the run
  (`ended_reason` / `pause_reason`), and a cancelled or finished run is never restarted
  (§7).
- Concurrent runs on the same game are refused: `AutonomyConflictError` is raised when
  another **active** run shares the target's handle, pid or title — "two windows of the same
  game is still one game to play".

## 6. Closed-loop orchestration

Each cycle performs, in order, and the trace proves the order:

1. acquire an observation (NF-15) — a refused/unavailable read does not become a state;
2. confirm the observation belongs to this run's pinned target (identity, not title alone);
3. interpret the state (NF-16);
4. read the objective picture (NF-17) — the only authority on completion;
5. plan bounded actions (NF-20) from that evidence;
6. **approve** — the operator, or the separate per-plan policy;
7. execute through NF-19's own interface and guard;
8. obtain fresh post-action evidence;
9. verify the expected outcome (NF-21) and record its evidence;
10. continue, replan, recover, pause or stop on the verified result.

Evidence from the fixture run: the trace's phases were
`INITIALIZING, IDENTIFYING_OBJECTIVE, OBSERVING, OBSERVING, UNDERSTANDING_STATE,
IDENTIFYING_OBJECTIVE, PLANNING, EXECUTING, VERIFYING, OBSERVING, UNDERSTANDING_STATE,
IDENTIFYING_OBJECTIVE, COMPLETED` — the cycle that ends the run is the one that **observes**
completion; the earlier cycle only established the effect of its own input. No stage was
skipped, no selected action was reported as executed, and no dispatched input was reported as
a verified result:

- dispatched actions: 3; verified outcomes: 3; all `verified_success`;
- every verified outcome carries a resolvable evidence reference
  (`state:<state_id>` + `observation_id` + `age_seconds: 0.0` + `accepted: true`);
- completion was read from the objective, **not** from the plan (the completing cycle has no
  plan at all) and never from the game still being open.

## 7. Execution and recovery limits

Declared defaults on `AutonomyLimits` (narrowable per run; **never widened by the system**):

| Limit | Default |
| --- | --- |
| decision cycles (`max_cycles`) | 12 |
| total actions (`max_actions`) | 6 |
| actions per plan (`max_actions_per_plan`) | 2 |
| session wall clock (`session_seconds`) | 180 s |
| observation / planning / action / verification timeouts | 20 / 20 / 60 / 45 s |
| observation attempts | 2 |
| recovery attempts | 2 |
| replans | 2 |

Observed enforcement (acceptance steps 9–10):

- a world needing 6 presses with a budget of 3: **exactly 3 dispatched, 3 presses, then
  `resource_limit_reached`** — the run stopped with the objective unmet rather than widening
  its own budget;
- a plan larger than the approved per-plan budget: **refused with 0 dispatched**, reason
  "The plan holds 3 step(s) and the approved per-plan budget is 1; the run stops rather than
  widening a limit it was approved with";
- recovery is bounded by `recovery_attempts`; exhaustion ends the run as
  `outcome_unknown` (or `resource_limit_reached` when it is the cycle budget that ran out);
- a repeated action whose outcome is unknown is never re-executed (the recovery path
  re-observes instead);
- a terminal run is never restarted, and `resume` on one answers `run_finished`;
- the resource gate (§ below) is consulted **before** the first cycle.

**Resource gate (fixed during this review).** The manager already accepted an
`autonomy_resources` probe and defaulted `require_resource_gate` to *off*, but **nothing in
the application ever passed the probe**, so a live run reported "No resource gate is wired on
this host" forever while the governor's reading sat one attribute away. Both halves are now
wired:

- `NovaControlApplication` passes `autonomy_resources=self._novaforge_autonomy_resources`,
  which reads the **same** `ResourceGovernor` (and therefore the same `HardwareMonitor`) every
  other load decision in this build asks;
- `AutonomyPolicy.require_resource_gate` now defaults to `True`, so a machine the build
  itself classes **CRITICAL** refuses the run (`resource_gate_refused`, with the reading as
  the reason), while an unreadable or unwired gate is reported as *not measured* and admitted
  — an absent sensor is neither permission nor pressure.

Live reading from the real application on this host (offline, no game): `available: true,
allow: true, level: "ample", reason: "resources are ample", available_ram_bytes ≈ 4.68 GB`,
matching `governor.assess().level` exactly.

## 8. Approval and user control

- Approval is **on by default** (`AutonomyPolicy.require_approval=True`). `POST
  …/autonomy/start` without `approved: true` returns `approval_required` with
  `started: false` and **no input sent** — asserted in the acceptance script against a
  zero-press counter.
- `plan_approval` is `run` by default (one approval covers the pinned run) and may be
  `per_plan`; a parked plan is answered exactly once, and **the next plan parks again**.
- A failure or a replan never inherits an old approval: replanning produces a new plan that
  parks again under the per-plan policy.
- The operators' controls are start, pause, resume, cancel, approve, refuse, refresh — every
  one of them its own route and its own button.- **UI:** the "First Autonomous Run" card in Mission Control (`#forgeAutonomyCard`) shows the
game, session, evidence source, phase, pinned objective, completion criteria, state,
  objective status, plan, dispatched actions against budget, approval mode, last observation,
  last verified outcome, recovery/replan counts, budgets and elapsed time; it carries the
  per-plan approval block, and pause/resume/cancel.
- **The ACTIONS themselves, not only their count** (added in the review pass):
  `#forgeAutonomyActions` lists the current plan's steps — kind, description and NF-19's own
  record for each one (`completed`, `rejected: <code>`, or `not dispatched`) — and the row names
  the cycle it came from. The "current" plan is the newest cycle that BUILT one: the cycle that
  reads completion holds no plan, so reading only the last cycle answered "no plan" for a run
  that had already dispatched three inputs.
- The card's own rules are pinned against the **real** module in a DOM shim
  (`AutonomyCardDomTests`, 8 tests): nothing is pressable without a run; Start is offered
  only when the server's `can_start` says so and no run holds the session; a live run offers
  pause + cancel but never start; a paused run offers resume; only a parked plan shows the
  approval block (and pause is *not* offered there, because nothing is running to pause); an
  ended run enables nothing at all.
- **Never labelled autonomous while simulating:** the badge reads `autonomous` only when the
  run's own `evidence_source` is `live`; a simulated run is badged `simulated` with its own
  CSS state. Mutation-tested: reverting the rule fails exactly
  `test_a_simulated_run_is_never_badged_autonomous`.

## 9. Completion criteria

- The objective and its completion criteria are **pinned before the first cycle** and travel
  in the run record (`objective_id`, `objective`, `completion_criteria`). Fixture run:
  `nc-forge-mission@1:collect:a86de86aec`, criteria `["reach 3 on progress", "read 'mission
  complete'", "read 'objective complete'"]`.
- Completion is taken **only** from a fresh NF-17 reading taken after the input. A finished
  plan, a dispatched action and a game that stayed open are all explicitly *not* completion.
- Terminal outcomes are the declared nine: `objective_completed`, `objective_failed`,
  `session_cancelled`, `session_timed_out`, `resource_limit_reached`,
  `observation_unavailable`, `outcome_unknown`, `dependency_failure`, `safety_stop`.
- A verified objective failure is reported as `objective_failed`, distinct from an unknown
  outcome (`outcome_unknown`) — the phase's "distinguish verified failure from an unknown
  outcome" requirement.
- After completion the session is stopped through the session's own `stop`, which released
  input (`cleanup.ok: true`, `held: []`), and nothing restarts it.

## 10. Automated test commands and exact results

All commands run from the repository root with `PYTHONPATH=src`.

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_novaforge_autonomy.py -q` | **112 passed**, 21 subtests passed (re-measured after the review pass below; 99/19 before it) |
| `python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py tests/test_novaforge_observation.py tests/test_novaforge_gameplay.py -q` | **438 passed**, 101 subtests passed (measured before this review pass; the 17-file run below covers the same files and was re-measured after it) |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_closed_loop.py tests/test_novaforge_control.py tests/test_novaforge_planning.py tests/test_novaforge_verification.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_timeline.py tests/test_route_shape_contract.py tests/test_novaforge_library.py tests/test_novaforge_library_ui.py tests/test_novaforge_ui.py tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py tests/test_novaforge_observation.py tests/test_novaforge_gameplay.py -q` | **936 passed**, 289 subtests passed — **re-measured after the review pass** |
| `python -m pytest tests/test_web_api.py tests/test_web_platform.py -q` | **120 passed**, 481 subtests passed |
| `python -m pytest tests/test_application.py tests/test_gui.py -q` | **44 passed**, 15 subtests passed |
| `python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_library_ui.py -q` | **169 passed**, 94 subtests passed |
| `python scripts/novaforge_nf22_autonomy_acceptance.py` | **16/16 steps OK**, verdict `PASS WITH LIMITATIONS`, `live_gameplay: NOT_TESTED` |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| `python -m pytest tests/test_api_reference_sync.py tests/test_route_shape_contract.py tests/test_novaforge_autonomy.py tests/test_novaforge_closed_loop.py -q` (milestone verification pass) | **183 passed**, 483 subtests passed in 82.84 s |
| `python -m pytest tests/ -q` (whole suite, one process — milestone verification pass) | **5 663 passed**, 3 847 subtests passed, 1 skipped, **1 failed** in 1 959.81 s |
| 11 `scripts/novaforge_*` acceptance/probe/eval scripts (milestone verification pass) | **11/11 exit 0**; NF-22's own run 16/16 OK |

No test was skipped and no failure was waived by this phase. The phase itself ran focused tests first, as
it asks; the **milestone verification pass** then ran the whole suite in one process (rows 9–10): 5 663
passed, 1 skipped, and **one failure** — `tests/test_browser_token_reuse_e2e.py`, a Playwright spec for the
JARVIS browser token flow, which is outside this phase's code. It is a real, reproduced UI defect (the JARVIS
panel's Approve And Run stops receiving clicks after its first run in that browser), characterised with
measured evidence in `docs/NOVAFORGE_VERIFICATION.md` §A.3.3; the spec's assertions were not weakened.

**Status (follow-up remediation pass): resolved.** The cause was the decorative 3D card tilt in `effects.js`:
a card that has carried `translateZ`/`perspective` can retarget pointer input to itself, so the console's
button never received the click. The tilt now skips cards containing interactive controls (live: 42 of 86
cards in this build), and this spec passes unmodified along with the reduced-motion tilt spec; the live
two-click trace is `plan, execute, plan, execute`. Detail: `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md`
R-01. This phase's own live-gameplay limitation is unaffected by that fix and still stands (`live_gameplay:
NOT_TESTED`).

**What the phase suite covers** — 112 tests: 86 behaviour over the scripted gateway and the
fixture world (`VocabularyTests` 10, `LifecycleTests` 16, `OrchestrationTests` 31,
`LimitTests` 7, `PersistenceAndObservabilityTests` 6, `SimulatedGameplayTests` 16),
5 API-level over the real manager and its services, 2 that the shipped wiring asks the shared
governor through the gate, 15 DOM pins for the card's own rules (11 over hand-written
payloads, 4 over the payload a real simulated run leaves behind), and 4 source/markup
contracts:

- *lifecycle*: valid and invalid transitions, illegal move raises, cancel, pause/resume,
  timeout, session conflicts (same window and same game), process termination,
  shutdown cleanup;
- *orchestration*: full successful cycle; missing observation; stale observation; unknown
  state; missing objective; invalid plan; approval rejection; action refusal; action timeout;
  verified success; verified failure; unknown outcome; recovery exhaustion;
- *safety and limits*: action budget, cycle budget, session duration, resource-gate refusal,
  no repeat after an unknown outcome, no execution after cancellation, no action against an
  invalid target, no execution beyond the approved plan;
- *integration*: observation→planning, planning→control, control→verification,
  verification→replanning data flow, UI state updates, trace persistence, and the DOM pins
  for the card's rules.

## 11. Type-check results

| Command | Result |
| --- | --- |
| `python -m mypy src` | **Success: no issues found in 451 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 451 source files** |
| `python -m mypy src` with PySide6 hidden from `.venv` (the CI condition — CI installs only `.[dev]`) and a fresh `--cache-dir` | **Success: no issues found in 451 source files** |

Both views were RED when this review pass started, on two `unused "type: ignore" comment`
errors in `gui/game_host.py` (an NF-13A file). **Removing those two comments was right on this
host and wrong in CI, and the milestone verification pass replaced it with a fix that holds in
both.** PySide6 6.12.0 IS installed here, so `QWidget` is a real class and the comments are unused
(an error under strict); in CI PySide6 is absent, `PySide6.*` resolves to `Any` through the
`ignore_missing_imports` override, and each Qt subclass is
`Class cannot subclass "QWidget" (has type "Any") [misc]` — exactly the error those comments were
silencing. No pragma is correct in both environments, so the relaxation is module-scoped in
`pyproject.toml` (`novacontrol.gui.app`, `novacontrol.gui.game_host`,
`disallow_subclassing_any = false`) — the same remedy that file already documents for sqlalchemy —
and the comments stay removed. Measured both ways: with the override and PySide6 hidden the whole
tree is clean (row 3 above), and without it an isolated two-subclass module reports the two `[misc]`
errors, so the override is doing real work rather than being assumed. Type-check results are
reported here, not assumed from the previous pass.

## 12. Simulated integration results

Full receipt (16 steps, 0 failed):

1. every composed phase exposes the surface NF-22 consumes — OK (dependency matrix, §2);
2. a run without the operator's approval starts nothing — OK (`approval_required`, 0 presses);
3. the closed loop completes the objective and says so honestly — OK (`COMPLETED` /
   `objective_completed`, `evidence_source: simulated`, objective + criteria named);
4. every dispatched action carries a verified outcome with evidence — OK (3/3
   `verified_success`, each with a resolvable state/observation reference);
5. the session trace records the whole orchestration, in order — OK (0 missing phases);
6. the session ends and its input is released at the end — OK (`stopped: true`,
   `cleanup.ok: true`);
7. the run is never described as live evidence — OK (`simulated`);
8. the action budget stops the run after exactly its bound — OK (3 dispatched, 3 presses,
   objective unmet);
9. a plan above the approved per-plan budget is refused, not widened — OK (0 dispatched);
10. an unresponsive target stops the run before any input — OK (`safety_stop`, 0 presses);
11. an input backend that refuses is never reported as progress — OK (`dependency_failure`,
    0 presses, 1 refusal recorded);
12. an unreadable counter is not a verified effect — OK (`outcome_unknown`, 0 presses,
    no `verified_success`);
13. a verified objective failure ends the run as failed — OK (`objective_failed`);
14. a paused run stops reading and resumes on the operator's word — OK
    (`paused_by_user` → `observed`);
15. cancellation ends the run and it is never restarted — OK (`session_cancelled`, restart
    answered `run_finished`);
16. the acceptance runs on a supported host — OK.

Plus, live against the real application (offline, no game): the resource gate reads the
shared governor (§7), the served `/` carries the card with the fresh asset token and the
`no-cache, no-store, max-age=0, must-revalidate` header trio, the served JS carries the
session-scoped autonomy route and the exported card logic, the served CSS carries the
`simulated` badge state, and the UI version stamp equals the injected asset version — so the
mid-session edits are served without a manual cache-bust. Re-checked after this pass's last UI
edit (a fresh server on 8001): the served module carried `?v=27f8dd3ffe8a`, the stamp read
`UI v27f8dd3f`, the card rendered with `#forgeAutonomyActions` in the DOM, every NovaForge
request answered 200 and the console was empty.

Browser check of the served panel (NovaForge tab, real Chromium): the card is visible in
Mission Control, badge `untested`, all six controls disabled, approval block hidden, outcome
"No autonomous run for this session.", and **no console messages at all**.

## 13. Live gameplay evidence

**NOT_TESTED.** No game was launched, no window was focused and no real key was sent by this
phase. Nothing in §12 is live-gameplay evidence: the fixture run is real orchestration over
simulated evidence, and the UI check is a live *interface* observation, not a gameplay one.

What a live run would require, and what is missing today:

1. an installed game with a **validated** objective the system can observe (NF-16/NF-17) —
   none established for any installed title;
2. a fresh, target-validated observation and a resource reading that admits the run (§7 now
   reports both honestly);
3. the operator's explicit approval for that exact run, with its plan, objective and budgets
   visible before execution;
4. a reversible objective or a safe termination path confirmed **before** the first input;
5. pause and cancellation verified on that session ahead of the run.

The script prints this list, its own reading of the fixture scenario, and
`live_gameplay: NOT_TESTED` in its receipt, so the gap cannot be missed by reading the
summary.

## 14. Resource readings

| Reading | Value | Source |
| --- | --- | --- |
| Governor level, live application (offline) | `ample` | `ResourceGovernor.assess()` via the wired gate |
| Free RAM, live application | ≈ 4.68 GB (4 676 210 688 bytes) | same `HardwareMonitor` every other load decision uses |
| Gate admission | `allow: true` | `AutonomyPolicy.require_resource_gate = true` |
| CRITICAL refusal | `allow: false`, `level: "critical"`, reason "Only 0.4 GB free." | scripted governor in the test suite |
| Fixture world | no probe wired | reported `available: false`, admitted, recorded as *not measured* |

No figure in this report was inferred from a missing measurement: an unreadable value is
`None`/absent, never a zero.

## 15. Known limitations and outstanding risks

1. **No live gameplay proof.** Everything gameplay-shaped in this report is simulated. The
   first real game, objective and session remain to be chosen and verified.
2. **The fixture scenario is the build's own.** Its objective and completion criteria come
   from the shipped registry, not from a real game's UI; a real game's success signal will
   need its own profile entry, and that profile will need the same evidence discipline.
3. **The gate's thresholds are the governor's, not NF-22's.** A TIGHT machine is admitted by
   design (the run's budgets bound it); if a live run later shows TIGHT is not enough for
   gameplay, that must be a declared policy change, not an ad-hoc check.
4. **Recovery is bounded to two attempts.** A real game may need a recovery shape the phase's
   vocabulary does not have; that is a deliberate stop rather than a silent retry.
5. **Approval ergonomics are per-run by default.** A long session with `plan_approval: run`
   is one approval for many actions — visible in the card, but worth revisiting before any
   unattended use.
6. **The phase report's own numbers are one host's.** Timing-sensitive figures (for example
   observation age on a loaded machine) may differ; the assertions are on state, not on
   timing.
7. **The phase's own tree is uncommitted.** This review added no commits; the reviewer that
   follows should expect one dirty working tree carrying NF-01…NF-22.

### Found and fixed during the verification pass

Every row below was found by re-reading the phase against its requirements, is covered by a
test added in the same pass, and was re-verified after the change.

| # | Defect | Why it mattered | Fix + pin |
| --- | --- | --- | --- |
| 1 | `AutonomousSessionController.shutdown()` cancelled the runs and returned while the session stop still sat unrun on the event loop. | Scope B requires a shutdown that leaves no service-owned input held. The stop is what releases it, so the method's own claim ("release the sessions they were driving") was not true when it returned, and a queued stop the process exits underneath never runs. | `shutdown()` awaits the queued stops, bounded, and reports `released` (`_await_cleanup`). Pinned by `test_shutdown_stops_every_run_it_owns` (the session's own stop has already run when shutdown returns) and `test_shutdown_reports_a_stop_that_never_answered` (a dependency that never answers is reported `released: false`, never a hang). |
| 2 | A plan that answered "planned" with **no steps** ended the run as `objective_failed`. | Nothing was proposed and nothing sent, so no failure was ever observed. Reporting an objective failure for a non-event is the mirror of claiming an unverified success. | `outcome_unknown`, with a reason that says nothing was established. Pinned by `test_a_plan_that_claims_to_be_runnable_and_has_no_steps_is_only_unknown`. |
| 3 | NF-19 answering `unknown_plan` / `no_steps` during execution was reported as `objective_failed`. | Both mean nothing was sent (the layer does not hold that plan, or the plan had nothing to run): a stage mismatch and an unknown, never a verdict about the game. | `dependency_failure` for `unknown_plan`, `outcome_unknown` for `no_steps`. Pinned by `test_a_plan_the_control_layer_does_not_know_is_never_an_objective_failure`. |
| 4 | `_plan_outcome` tested a gate-code set whose two branches returned the same outcome. | Dead branch: it implied a distinction the mapping does not make, which is how a later reader "restores" behaviour that never existed. | One `return SAFETY_STOP`; the gate's own name travels in the reason. |
| 5 | Two `# type: ignore[misc]` comments in `gui/game_host.py` were unused under strict mypy **on this host**. | `python -m mypy src` is a CI check and this phase requires the type checks to pass: both views were **red** locally before this pass and are clean after it — but the same comments are *required* in CI, where PySide6 is absent. A fix that trades one red environment for the other is not a fix. | Comments removed **and** a module-scoped `disallow_subclassing_any = false` override added in `pyproject.toml` for `novacontrol.gui.app` / `novacontrol.gui.game_host`, so both environments are clean (§11). |
| 6 | The card showed the plan's **count** rather than its actions, and read only the LAST cycle's plan. | Scope H requires the planned and executed actions to be visible; the cycle that reads completion holds no plan, so a completed run that had dispatched three inputs displayed no plan at all. | `#forgeAutonomyActions` lists each step with NF-19's own record and the cycle it came from; `autonomyPlanCycle` selects the newest plan-bearing cycle. Pinned by 3 DOM tests over hand-written payloads and 4 over the real payload. |
| 7 | The DOM shim's `textContent` did not clear children, unlike the real DOM. | A re-render meant to drop a row still read as present, so a "this must NOT be shown" assertion could pass on a stale node. | `textContent` is now an accessor that replaces the children, as the browser does. |
| 8 | Six routes were served with no declaration anywhere: `POST /build/project`, `GET /build/artifacts`, `POST /build/run`, `GET /models/status`, `POST /models/load`, `POST /models/unload`. | A route is done in five places; these had the handler only (the `docs/API.md` block is regenerated from the declaration, so it looked complete). They answered correctly while being invisible to `ApiSurface`, to the frontend consumer contract and to every drift check the suite had — those compare the declarations with each other and never with the handlers, which is how the gap stayed green for phases. | Rows added to `api/models.py` and `api/route_consumers.py`; pinned by `test_every_route_the_app_serves_is_declared` in `tests/test_api_reference_sync.py`, which parses every `@app.<verb>("…")` decorator out of `api/app.py` and refuses a served-but-undeclared route (FastAPI's own `/docs`, `/redoc` and `/openapi.json` are not decorator-registered here, so no exemption list is needed). Mutation-tested: one inserted probe route fails it by name. |

### Checked and held correct (no change)

- **The card's `approval_required` read.** The card reads a key the *controller's* `status()` does
  not send; `NovaForgeManager.autonomy_status` adds it, and that is the route the page calls —
  checked at the source of the route, not assumed.
- **The badge rule.** `autonomous` requires `evidence_source == "live"`; a simulated run is
  `simulated`, and an ended run is `ended` with its evidence source still named in the facts.
- **A finished run still offers Start when the server's `can_start` allows it** — that is a NEW
  run with its own approval (Scope F), not a restart; the real-payload test pins the button row.
- **All eleven declared limits are enforced, not merely declared** (`session_seconds`,
  `max_cycles`, `max_actions`, `max_actions_per_plan`, the four stage timeouts,
  `observation_attempts`, `recovery_attempts`, `replans`), and all nine terminal outcomes have a
  reachable path.

### Re-verified live after the fixes (2026-10-26)

The milestone verification pass re-ran this phase against the running application, not only the unit
suites:

- `python scripts/novaforge_nf22_autonomy_acceptance.py` — **16/16 steps OK**, `failed: 0`, verdict
  `PASS WITH LIMITATIONS`, `live_gameplay: NOT_TESTED`, and the trace still shows the complete cycle
  (`INITIALIZING → IDENTIFYING_OBJECTIVE → OBSERVING → UNDERSTANDING_STATE → PLANNING → EXECUTING →
  VERIFYING → COMPLETED`) with 3/3 dispatched actions carrying verified evidence.
- A fresh `uvicorn` on `127.0.0.1:8001` served `/` with the never-cache trio and injected asset
  versions; `/novaforge/status` reported its honest limitations; `POST
  /novaforge/sessions/does-not-exist/autonomy/start` was refused with **422**
  (`"No NovaForge session is named 'does-not-exist'."`, nothing started); `GET
  /novaforge/sessions/{unknown}/autonomy` returns the service's bounds with `runs: []`, `sessions: []`
  and zeroed counts.
- The served page was loaded in a real browser: **zero console messages**, the sidebar footer stamp
  matched `/health`'s `ui_version`, the NovaForge panel opened, clicking *Forge Coach* activated exactly
  that view, and the autonomy card rendered its empty state with `Start`/`Pause`/`Resume`/`Cancel`/
  `Approve` **disabled** and only `Refresh Run` enabled — no session, no way to start a run by accident.
- Both mypy views and the CI-parity view (PySide6 deliberately absent) are clean, and
  `docs/API.md` is in sync.

Commands, cross-phase verdicts and the remaining unverified items: `docs/NOVAFORGE_VERIFICATION.md`
Appendix A.

### Unverified or partially met requirements

1. **Live gameplay: NOT_TESTED.** Unchanged by this pass — no game, no window and no real key.
2. **"Planned actions visible before execution" (Scope E) holds under `plan_approval: per_plan`,
   not under the default `run`.** With `run`, the operator approves one objective and one budget
   before the first cycle and the plan's steps become visible as that cycle runs; the card shows
   the objective, criteria and budget up front. This report does not claim the default previews
   the first plan before it is dispatched.
3. **"Pause on invalid or stale observations" (Scope D) is implemented as a terminal stop**
   (`observation_unavailable` / `safety_stop`) rather than a resumable pause: an unreadable
   pipeline ends the run with its reason recorded instead of being re-read until the budget is
   spent. A resumable pause exists (`PAUSED` plus the operator's resume); a stale reading is not
   what triggers it.
4. **The card's populated state was verified against the payload a real run leaves behind
   in-process, not against a live game session** — a live session cannot be started without a
   game. The live browser check covers the card's markup, controls, badge and empty state on the
   served page, with no console output at all.
5. **`_act`'s mid-plan statuses** (`stopped`, `cancelled`, `replan_required`, `invalidated`)
   proceed to verification by design, because the dispatched effects are real; their
   post-verification behaviour is exercised through the recovery paths, not by an execution that
   invalidates itself mid-step against a real game.

## 16. Final verdict

**PASS WITH LIMITATIONS.**

The bounded autonomous loop is implemented, reachable and verified end to end over the
shipped NF-16…NF-21 services: it observes, understands, identifies the objective, plans,
waits for approval, acts only through NF-19, verifies against fresh evidence, recovers within
bounds, and terminates with a declared reason — with budgets that stop it exactly at their
bound, safety stops that end a run with zero input when the world cannot be read or driven, a
cancelled or finished run that can never be restarted, a resource gate that now really reads
the build's shared governor, and a session trace that records the entire orchestration.

It is **not** evidence of general gameplay capability. The first live game, its objective and
its verified completion are outstanding, and this report does not describe them as done.

A verification pass over the phase then found and fixed seven defects: two honesty defects in
how a run reported its ending (an empty plan and a plan the control layer did not hold were
both reported as a failed objective), a shutdown that returned before the session's input was
released, a dead branch that implied a distinction its own mapping never made, a type check left
red by two stale ignores, and two defects in the operator's card (the actions were only
counted, and its DOM harness accumulated children). Each fix carries its own pin, and the
numbers above were measured **after** them, not before.

The later milestone verification pass over NF-13A…NF-22 added two more, both integration-level:
the type-check fix above was replaced by one that is correct where PySide6 is installed **and**
where it is absent (the CI condition), and six routes that had shipped served-but-undeclared now
have their `ApiSurface`/consumer rows plus a regression test that refuses the next one. That pass
also re-verified this phase live — the acceptance script, the HTTP surface and the served page —
and found no new phase defect. See `docs/NOVAFORGE_VERIFICATION.md` Appendix A.

The NF-22…NF-26 cross-phase audit (2026-10-28) found one more, in this phase's own provenance
default: `AutonomousSessionController` reported an **undeclared** `evidence_source` as `"live"`, and
restored a stored run without one the same way, so a run nobody had labelled could travel downward to
NF-26 as real-game evidence. Both defaults are now `"simulated"` (the manager's controller and the
acceptance harness already declared their class explicitly, so no real run changed), pinned by
`test_undeclared_provenance_is_never_reported_as_live` in `tests/test_novaforge_autonomy.py`. After the
fix that suite is **114 passed, 21 subtests**, and the acceptance script still reports 16 steps, 0
failed, `live_gameplay: NOT_TESTED`. See `docs/NOVAFORGE_VERIFICATION.md` Appendix E.
