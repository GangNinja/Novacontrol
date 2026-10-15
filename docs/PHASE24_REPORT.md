# Phase 24 — Planning, Reasoning & Action Policy

**Verdict: PASS**, with the limitations in §15 recorded explicitly. Every mandatory
acceptance criterion in the phase specification is satisfied by code that runs, and
the evidence for each is named below. Two later phases are named as deferred (§18)
and nothing from them is claimed.

The phase's flow, as implemented:

```
USER GOAL → goal representation (goals.py)
          → current world state (grounding.py ← Phase 22)
          → available capabilities (capabilities.py ← Phase 9 registry)
          → goal decomposition (goals.decompose)
          → candidate plans (candidates.py: 2 planners)
          → precondition and safety checks (policy.py, revalidation)
          → outcome estimation (estimation.py)
          → plan selection (ranking.py)
          → bounded execution through the existing executor (goal_manager → executor.py)
          → result verification (Phase 8 VerificationEngine)
          → plan reassessment (monitor.py)
          → completed | blocked | denied | failed | partial | needs_observation
```

---

## 1. Scope and implemented capabilities

NovaControl can now take a goal expressed in words, ground it in the state it can
actually read, decompose it into validated tasks, generate **candidate** plans with two
different planners, compare them, refuse the ones that cannot run, ask a person before
a consequential step, execute the selected plan through the executor the rest of the
build already uses — re-checking every step against fresh state immediately before it
runs — and then decide whether the goal is **completed** from an observation of its own
success conditions rather than from the steps simply ending.

The phase declared twenty capability rows (`planning/capabilities.py`) — as this build
ships it is **nineteen**, because Phase 25 removed the `game_agent` placeholder (see the
correction below). Each row's state is a claim about this build, not about the phase
document:

| state | rows |
|---|---|
| `implemented` (13) | `goal_representation` · `decomposition` · `dependency_ordering` · `plan_validation` · `candidate_generation` · `plan_ranking` · `outcome_estimation` · `action_policy` · `bounded_execution` · `monitoring_revalidation` · `completion_verification` · `replanning` · `persistence` (all prefixed `planning.`) |
| `partially_implemented` (2) | `state_grounding` — "a real reading needs Phase 22's world model; without one every fact is an assertion" · `parallel_execution` — "only READ_ONLY steps carry the flag; ordering still respects every dependency", and waves are capped at 4 |
| `provider_dependent` (3) | `world_state` — unavailable without a wired world model, and nothing is invented in its place · `prediction` — no trained predictive model ships; a transition is rule-based and labelled · `learned_knowledge` — available only once a Phase 23 rule exists for the action **and** scope in question |
| `future` (1) | `generalization` (Phase 26) — the `game_agent` placeholder was removed when Phase 25 shipped the real `embodied.*` rows (see the correction below) |

The `future` row exists so that "can you build an ARC solver?" has an honest answer in the
capability table instead of being absent. It is declared, not implemented, and the report
says so again in §18.

**Correction (Phase 25, verified in the Phases 21–26 audit).** As this build ships, the
table declares **19** rows — 13 `implemented`, 2 `partially_implemented`, 3
`provider_dependent`, 1 `future` (`planning.generalization`). The `planning.game_agent`
placeholder was **removed** when Phase 25 shipped the real `embodied.*` rows on the same
registry: keeping a "future" row beside them would have this build answer "not built" and
"available" about the same thing (its absence is now pinned by a test in
`tests/test_goal_planning.py`, and Phase 25's report records the removal as a defect it
fixed). The table above is what this phase declared; §18 records the deferral.

**What this phase deliberately is NOT.** It is not an agent loop, a second planner, a
second executor, a second permission layer, a world model, or a general-purpose solver.
It is the ORDER in which the components that already exist are called, plus the state
that remembers what happened.

## 2. Existing architecture reused

Nothing was forked. Each collaborator is the one the rest of the build already uses:

| Need | Reused | Where |
|---|---|---|
| Current state | Phase 22 world model, read-only through `StateGroundingService` | `planning/grounding.py` |
| Where a step runs | `WorkflowExecutor` + the application's own step handler | `planning/executor.py`, `application._run_plan_step` |
| Verifying a result | Phase 8 `VerificationEngine` (a `DeterministicVerifier` subclass) | `reliability/verification.py` |
| Risk and approval | Phase 8 `PermissionManager` (`self.risk`), the same layer the tool executor gates on | `reliability/permissions.py` |
| Learned knowledge | Phase 23's rule book, consulted for an outcome estimate only | `interactive/knowledge.py` |
| Capability reporting | Phase 9's ONE `CapabilityRegistry` | `intelligence/capabilities.py` |
| Events | Phase 9's ONE `EventBus`, through an injected observer seam | `core/events.py`, `application._planning_event` |
| Persistence | The shared `JsonStateStore`, one key per goal | `persistence/json_store.py` |
| Ceilings | Phase 14's policy pattern (a config section, not constants) | `core/config.py` → `GoalPlanningSettings` |

`planning/agent.py`, `planning/compiler.py`, `planning/engine.py` and
`planning/recovery.py` were left alone. The only shared file modified is
`planning/models.py`, and only where measured behaviour was wrong (§13, defect 1).

## 3. Modules created or modified

Created (5,357 lines across the nine new Phase 24 modules in the current tree):

| Module | Lines | What it owns |
|---|---|---|
| `planning/goals.py` | 782 | `Goal`, `Task`, `SuccessCondition`, `GoalBudget`, `decompose()` and the seven `IssueKind` validations |
| `planning/grounding.py` | 436 | `StateGroundingService`, `GroundedFact`, usable/stale/conflicting classification |
| `planning/candidates.py` | 650 | `PlanningBudget`, `CandidatePlan`, `PlanningOutcome`, `BaselineOrderPlanner`, `CostAwarePlanner` |
| `planning/ranking.py` | 234 | `PlanRanker.compare` / `.select` — the documented selection method |
| `planning/estimation.py` | 268 | `OutcomeEstimator` with its four `EstimateBasis` values |
| `planning/policy.py` | 397 | `ActionPolicy`, `PolicyVerdict`, `PolicyReport` |
| `planning/monitor.py` | 727 | revalidation, observation vs prediction, completion verdicts, replan decision |
| `planning/goal_manager.py` | 1,445 | the orchestrator: ground → decompose → plan → rank → authorize → execute → assess |
| `planning/capabilities.py` | 475 | the 20-row capability table, read live from the manager |

Modified: `planning/models.py` (one property), `application.py` (+761), `api/app.py`
(+337), `api/models.py` (+161), `api/route_consumers.py` (+31), `core/config.py`
(+302), `core/events.py` (+74).

Also added: `tests/test_goal_planning.py` (1,653 lines), `scripts/benchmark_planning.py`
(789 lines), `docs/PHASE24_REPORT.md` (this file). These counts describe the current
working tree and are not acceptance criteria.

## 4. Goal and plan contracts

`Goal` carries the request plus a normalized form, the success conditions, the asserted
initial state, constraints, exclusions, a budget (steps, replans, wall-clock), required
permissions, approval conditions, a deadline, failure and cancellation conditions, and
provenance/correlation ids. `GoalBudget` is a hard ceiling, not a suggestion.

**A field the caller did not state stays empty and is reported as empty.** `create()`
never invents a success condition, a constraint, a dependency or a deadline.
`SuccessCondition` without an `expected` value is *not measurable*, and
`condition.satisfied_by(...)` answers `None` for it rather than `False` — "nobody said
what this means" is a third answer, and rounding it to a boolean is how a planning layer
starts lying.

`Task` mirrors `PlanStep` (that is what it becomes) and adds `estimated_seconds`,
`estimated_cost` and a `verification` as words. `Task.to_step()` refuses to mark a step
`parallel_safe` unless its effect is `READ_ONLY` — the model itself raises, so a caller
cannot declare a write to be safely concurrent.

Uncertainty is preserved end to end. A grounded fact is `OBSERVED`, `INFERRED` or
`UNKNOWN`; a caller's assertion is explicitly `UNKNOWN` with `basis=caller_assertion`
(`source: caller_assertion`), so "the user said so" can never be read as a measurement.
Facts are additionally `usable` / stale / conflicting, and a stale or conflicting fact
is deliberately **not** offered to a precondition.

Decomposition reports seven issue kinds instead of raising: `unknown_dependency`,
`cycle`, `missing_prerequisite`, `duplicate_task_id`, `unmeasurable`,
`impossible_constraint`, `unknown_capability`. Each carries the task ids it concerns.
`TaskDecomposition.ok` is true only with no issues, so a caller cannot build a plan
without stepping over the problems. A goal with no measurable completion condition is
refused with that reason; when a required piece of information is missing the answer is
a refusal naming it, never an invented default.

## 5. Planning algorithms and their limitations

Two planners, both deterministic, both bounded:

1. **`dependency_order`** — dependency-graph planning with a topological ordering,
   cheapest-first among the ready set. Complexity O(T log T + E). One candidate, no
   search. It has no opinion about which of several valid orders is cheaper to *run* —
   that is what the second planner is for.
2. **`cost_aware_beam`** — a bounded beam search over the ready set, ranked by
   `(total cost, total risk, length)`: a lexicographic tuple, documented as a ranking
   and not as a probability. Ceilings (`max_expansions`, `max_candidates`,
   `max_seconds`, `max_depth`) are checked **inside the loop**, and a search that hits
   one reports `truncated=True` — a truncated outcome is useful, and saying so is what
   stops it being read as "these were all the plans there were".

**Limitations, stated plainly.** There is no A\*, no Monte Carlo tree search, no
learned policy, and no planning over a continuous or unbounded state space. The search
is a beam over a task graph that a caller (or the application) supplied; the phase does
not include a model-based planner that predicts multi-step futures, because no learned
transition model ships (§15). `_effects_already_hold` skips a task whose every declared
effect already holds — which means a task whose effects were mis-declared will be
skipped, and the fix for that is a correct declaration, not a cleverer heuristic.

## 6. Action-selection method

Selection is `PlanRanker.compare` → `.select`, and it is documented rather than
emergent:

- A **score** per candidate from fewer steps, lower estimated cost, higher feasibility,
  shallower critical path, fewer missing prerequisites, and a project/state fit term.
- An **infeasible candidate is never selected for execution**: a candidate with a
  missing prerequisite nobody produces scores last and cannot be picked while any
  feasible candidate exists. A partial plan is still *reported* (the spec asks for one)
  with its `missing` list, because showing a caller why nothing can run is the point.
- Ties break deterministically, so planning the same input twice selects the same plan —
  pinned by `test_planning_the_same_input_twice_selects_the_same_plan` and re-pinned
  through HTTP by `test_the_same_goal_planned_twice_compares_the_same_way`.
- When **every** candidate is infeasible, `plan()` returns a refusal that names what is
  missing instead of selecting the least-bad option.

The score is a **ranking**, not a probability or a confidence. No candidate is presented
as "85% likely to work"; the estimates carry a `basis` and, where nothing measured
confidence, `confidence=None`.

## 7. State and prediction integration

`StateGroundingService` reads the world model (or a plain mapping, which is the shape
its own docstring documents for a deterministic scenario) and classifies every fact:

- **Directly observed** — an attribute with an observed basis; usable while fresh.
- **Inferred** — carried through with its basis, never upgraded to observed.
- **Stale** — an observation older than `stale_after_seconds`; reported, not usable.
- **Conflicting** — two sources disagreeing; reported, not usable as a decision.
- **Unknown** — no fact at all, or no world model wired, with the reason stated.

Every state-dependent decision keeps its evidence: grounding travels with `as_of`,
`world_id`, `world_version` and per-fact source/evidence, and each revalidation report
carries the checks it ran and the verdict it reached. `PlanMonitor` is the only place
prediction and observation meet, and it keeps them apart: `observation` compares what a
step was *expected* to do with what was *seen*, and an expectation with basis
`rule_based` is labelled a DECLARATION.

If a critical precondition is **unknown or stale**, the step does not run: the monitor
answers `observe` and the run is reported `needs_observation` — the planner asks for a
safe reading or a clarification instead of acting on nothing. Nothing here ships a
trained predictive model: `OutcomeEstimator` asks, in order, Phase 22's projection
service (`world_model`), a Phase 23 rule that covers the action **in this scope**
(`learned_rule`), then the task's own declared effect (`rule_based`), and otherwise
answers `unavailable` with an empty estimate. A provider that raises or answers nothing
is UNAVAILABLE, not an error.

## 8. Execution and recovery integration

`GoalManager.execute` is the only method that touches the machine, and it is the only
one whose report claims steps ran.

- It runs the **selected** candidate on the application's own executor. No executor
  wired → the run is `blocked` with that reason and nothing is attempted.
- Before every step, `_gated_executor` re-checks the step's own preconditions against
  **fresh** grounding and raises `PermissionError` when they no longer hold. The
  executor already reads that as a refusal: no retry, no rephrase, `DENIED`, and
  everything downstream of the step is blocked. The set of facts an earlier step
  produced is read from the executor's own `StepContext.outputs` (plus that step's
  DECLARED effects, which is the contract the plan was ordered by) — not from state the
  gate kept beside it (§13, defect 2).
- A plan with **no steps** is the skip path, not a refusal: every effect was already
  grounded true, so nothing needs to run. The completion check decides — complete if
  the conditions already hold, `blocked` with what is still missing otherwise. Calling
  that a policy denial would read as "a step was refused" when no step existed.
- Unexpected outcomes become safe reassessment: `reassess()` asks the monitor whether to
  replan under the goal's **own** ceiling (`min(monitor.max_replans, goal.budget.max_replans)`),
  and a `plan()` call on a goal that already has a selection or a run is counted as the
  replan it is — the ceiling exists to stop the loop, so the place that would start
  another attempt is the place that counts it. An exhausted ceiling returns a refusal
  naming the count (`the replanning ceiling is reached (1/1)`), never a silent re-plan
  of the same thing.
- Recovery primitives are the executor's: `RetryPolicy`, `RecoveryAdvisor`, and
  `EFFECTS_NEVER_RETRIED` (a delete that half-succeeded is not re-run). The goal layer
  adds no retry loop of its own.

## 9. Safety and resource controls

- **Action policy before execution.** `ActionPolicy.evaluate_plan` runs first. A goal's
  `exclusions` are absolute (a goal that says "no writes" is not a preference the
  permission layer may outweigh); a goal's `approval_conditions` force a confirmation
  even where the generic risk rule would not; `DESTRUCTIVE`, `EXTERNAL` and `SYSTEM`
  steps always ask, with or without a permission layer wired. With nothing allowed,
  `execute()` returns `denied` and no step was attempted.
- **One approval set, two readers.** The step ids a caller approved are read by the
  policy ("may this run?") and by the executor's confirmation channel ("is this step
  authorized?"), so an approved step is not refused a second time by the executor.
- **Fail closed.** A step that needs confirmation with no approval channel wired is
  denied, not attempted — in the executor, not just in the policy.
- **Ceilings from configuration.** `goal_planning:` supplies thirteen settings
  (`enabled`, `persist`, `use_world_model`, `use_learned_rules`, `allow_destructive`,
  `stale_after_seconds`, `max_expansions`, `max_candidates`, `search_seconds`,
  `max_depth`, `max_steps`, `max_replans`, `max_seconds`), each clamped to a hard bound.
  `allow_destructive` defaults to **false**.
- **Bounded work.** Search ceilings are enforced inside the loop; the execution queue is
  the executor's; parallel waves are capped (`MAX_WAVE_SIZE = 4`) and only read-only
  steps may share one.
- **Honest refusals.** A refusal is a first-class result on every surface: the refusal
  string, the `IssueKind`, the `PolicyVerdict.kind`, the run status. Nothing that was
  refused reports success.
- **A stated limit is a clock, read twice.** `deadline_seconds` and the budget's
  `max_seconds` are enforced before **every** step (the gate in front of the step runner
  refuses to start one once the limit has passed, and the run says which limit and which
  step) **and once after the last step**. Without the second reading a plan whose single
  step overran its budget reported `completed` while nothing named the breach; now such a
  run reports `budget_exhausted` / `deadline_exceeded` with the spent-and-allowed figures
  in its reason, while the completion verdict stays attached (`verified` and the goal's
  own status are not demoted by a breach of a limit that the work nevertheless met). A
  plan whose own estimate already exceeds the goal's budget is *reported* at plan time
  (`limitations`, §11) rather than refused, because a declared effect is a declaration,
  not a prediction — the clock, not the estimate, is what enforces.

## 10. API, GUI and CLI changes

Seven routes, added in all five places the repository requires (handler, `ApiSurface`
row, route consumer, regenerated `docs/API.md`, test):

| Route | What it does |
|---|---|
| `GET /planning/status` | goals, planned/verified counts, replan usage, the boot restore report, the capability table read live |
| `GET /planning/goals` | every goal this session holds, and which have a selected plan |
| `POST /planning/goals` | record + decompose + produce candidates and select one — **runs nothing** |
| `GET /planning/goals/{goal_id}` | the record: decomposition issues, candidates, selected plan, last run's revalidations and completion verdict |
| `POST /planning/goals/{goal_id}/run` | execute the selected plan under the per-step gate, with `approved` and `keys` |
| `POST /planning/goals/{goal_id}/reassess` | should this goal be replanned, and is the ceiling exhausted |
| `POST /planning/goals/{goal_id}/cancel` | cancel; cancelling is not deleting — the record stays readable |

**238 routes = 238 consumers**; `docs/API.md` regenerated and `--check` in sync.
`status()["app"]["goal_planning"]` carries the summary beside `world_model` and
`interactive_learning`.

**There is no web panel and no CLI command for goal planning**, and that is declared
rather than left ambiguous: every planning route is registered `no-render` in
`api/route_consumers.py` with the reason in the file, because the surface is
API/operator-facing. Adding a panel would mean a renderer for a plan graph; the phase
did not claim one. `POST /plan/run` (Phase 4/6) is unchanged and still works.

## 11. Automated test results

| Check | Command | Result |
|---|---|---|
| Planning contracts + workflows | `PYTHONPATH=src python -m pytest tests/test_goal_planning.py -q` | **90 passed** (87 + 3 added by the audit below) |
| Planning/reliability/persistence/world integration | `pytest tests/test_planning.py tests/test_reliability.py tests/test_persistence.py tests/test_world_model.py -q` | **372 passed, 50 subtests passed** |
| Application, API and reference sync | `pytest tests/test_application.py tests/test_api.py tests/test_api_reference_sync.py -q` | **40 passed, 250 subtests passed** |
| Full suite, 4 disjoint slices | `PYTHONPATH=src python -m pytest <slice> -q` ×4 | **3,789 passed, 0 failed, 12 skipped, 2,192 subtests passed**; all four exit 0 (708 + 639 + 1,362 + 1,080) |
| Types, Linux view | `python -m mypy src` | **Success: no issues found in 390 source files** |
| Types, Windows view | `python -m mypy src --platform win32` | **Success: no issues found in 390 source files** |
| API reference | `python scripts/generate_api_reference.py --check` | **docs/API.md: in sync** |
| Dead-code hunt | `python scripts/dead_code_hunt.py planning` | one candidate, pre-existing (`planning/verification.py:702 unverified_step_reason`); **none from this phase's modules** |

Re-run on the **final tree** by the audit pass: the phase suite, the three dependency
groups (planning/reliability/persistence/world; application/API/reference-sync;
config/events/route-shape/web), both mypy views and the API reference check — all exit 0. The four-slice full suite and the dead-code hunt are the phase
build's measurements (§19 says exactly what the audit repeated and what it did not).

The 12 skips are all one environmental cause, verified with `-rs`: `playwright not
installed`. Nothing is skipped for a reason related to planning.

`tests/test_goal_planning.py` has two halves. The first pins the **contracts**:
goal representation and unmeasurability, the seven decomposition issues, candidate
generation and deterministic selection, the beam's truncation report, the four estimate
bases (including "a retired rule is never a basis" and "a rule from another scope is not
borrowed"), grounding and revalidation, completion verdicts, the action policy,
replanning ceilings, persistence, the capability table and the config bounds. The second
drives the **real application over HTTP** (`create_app()` against a temporary data
directory) through workflows A–L: representation, ordering, a cycle, a missing
prerequisite, candidate comparison, grounding, revalidation (including a contradiction
that stops the step before any attempt and an earlier step's declared fact that lets the
next one run), completion (a condition nothing observed never reports completion),
approval, replanning, persistence across a restart, estimation honesty and the
event lifecycle.

## 12. Real user-level workflow results

A real `uvicorn` server was started from an **isolated working directory** (its own
`data/`) on `127.0.0.1:8001`, and the planning surface was driven **over HTTP only** —
no internal calls, no test client. **30 legs, all PASS**, covering: the wired report;
a real observation through `POST /world/observe`; create+plan with a two-step dependent
chain (dependency order `['read-system', 'read-cpu']` in the plan, two planners
compared, `cost_aware_beam` selected, policy verdicts attached, and **nothing ran**);
reading the goal back; a run that **refuses to claim completion** while nothing observed
the condition (`needs_observation`, `verified=false`, both steps genuinely executed via
the real hardware readers); observing the condition and running again
(**`completed`, `verified=true`**, no unverified step, 2 revalidations, step outputs
recorded); reassessment after a verified run (`should_replan: false`); an unmeasurable
goal refused with the reason; and a prerequisite nothing holds refused **by name**.

Then the server was **stopped and restarted** on the same data directory: the boot
restore came back `{"restored": 3, "skipped": 0, "ids": [...]}`, the goal under test
kept its `completed` status, its request and its conditions, its decomposition, and its
run record — and, correctly, **no plan** (a plan is a decision this process never made).
`GET /` still served with the exact `no-cache, no-store, max-age=0, must-revalidate` /
`Pragma: no-cache` / `Expires: 0` trio, `GET /system/health` answered 200, and chat was
unaffected (`"what is 12 times 12" → "12 times 12 is 144."`).

The full session detail is reproducible with the two artifacts named in §13 and §16; the
probe itself lived outside the repository (a temporary directory) because it is
verification scaffolding, not a shipped tool.

## 13. Defects found and fixed

Six defects were found by building, testing, driving the phase for real, and then
re-reading it against its own objectives. Each was fixed at its cause and pinned by a
test.

1. **A refused step counted as an executed one — in shared Phase 4 code.**
   `StepOutcome.executed` was `status not in (SKIPPED, BLOCKED)`, so a `DENIED` step —
   one the approval layer refused and which therefore never ran — read as *executed*. A
   plan whose only step was refused reported `executed=True` while its own summary said
   the plan did not run: exactly the confusion between "a plan exists" and "work
   happened" this field exists to prevent. Fixed in `planning/models.py` (`DENIED` is
   not an execution). Pinned by the approval workflow (`executed is False` after a
   denial) and by benchmark scenario H.
2. **The fresh-state gate crashed on its first step.** `_gated_executor`'s first version
   kept the map of facts produced so far in the closure and assigned to it from inside
   the nested handler without `nonlocal` — a real `UnboundLocalError` on the first
   step, which would have made every gated run fail. Replaced by reading the executor's
   own `StepContext.outputs` (plus each finished step's declared effects) as the single
   source of truth. Pinned by the revalidation workflows, including
   `test_a_fact_an_earlier_step_declared_helps_the_next_step_run`.
3. **A gate refusal was reported as "go and look" instead of "blocked".** With the step
   refused by fresh state, `_status_for` fell through to the completion check and
   answered `needs_observation`, which reads as "the state could not be read" rather
   than "the state contradicted the plan". The gate's own refusal now outranks the
   completion check: `blocked`, with the gate's reason leading. Pinned by
   `test_the_gate_refuses_a_contradicted_precondition_before_any_attempt` and by
   benchmark scenario G.
4. **The boot restore report shipped whole stored payloads on the status surface.**
   `restore()` recorded the entire persisted record per goal — including the user's
   request text and asserted initial state — into the map that `GoalManager.status()`
   publishes, while that method's own docstring promises a *"bounded, content-light
   summary"*. A request is the user's words, not an operational figure. It is now a
   bounded row (`goal_id`, status, whether a decomposition came back, how many
   conditions, and `previously_selected`), which also makes the previously chosen
   candidate id *readable* rather than written and ignored. Pinned by
   `test_the_restore_report_is_bounded_and_carries_no_request_text`.

A final verification pass re-read the phase against its own objectives and probed each
stated field through the real code path (`GoalManager` over a real `WorkflowExecutor`,
the real gate, the real monitor). It found two more defects, both in that pass's own
budget work, and confirmed three behaviours that are deliberate rather than broken.

5. **A run that finished past its stated seconds budget reported `completed`.**
   `max_seconds` and `deadline_seconds` were consulted before each step, which is not the
   same as consulting them after the run: a plan with a **single** step checked the clock
   at ~0 ms, ran its one step for 0.06 s of a 0.001 s budget, and then reported
   `completed` because there was no "next step" to refuse. The limit was therefore
   unenforced for exactly the plans where it is easiest to break. `execute()` now asks the
   clock once more after the last step; a run that finished past a limit is labelled
   `budget_exhausted` / `deadline_exceeded` with the spent-and-allowed figure leading its
   reason, while `verified` and the goal's status are left to the completion check (the
   conditions really did hold). Pinned by
   `RunLimitTests::test_a_run_that_finishes_past_its_seconds_budget_reports_the_breach`
   and `::test_a_step_that_would_start_past_the_deadline_is_never_started`, and by
   benchmark scenario M (§14), which plays the overrun end-to-end.
6. **One stop kind for two different gate findings.** The gate stamped every non-proceed
   revalidation with `kind: "state"`, and `_status_for` maps that to `blocked` — so a
   precondition the state could not settle (`verdict == "observe"`) was reported as a
   state **refusal**, losing the `needs_observation` label that tells an operator to go
   and look instead of to replan. The stop now carries the gate's own verdict
   (`"state"` for `refuse`, `"needs_observation"` for `observe`). Pinned by
   `RunLimitTests::test_the_stop_kind_tells_a_contradicted_state_from_an_unreadable_one`,
   which asserts both sides — and which failed on `'blocked' != 'needs_observation'` with
   the old kind, so the distinction is really exercised rather than merely present.

Probed and found **not** defects (recorded so a future pass does not "fix" them):

- **Parallel waves really overlap.** An earlier measurement said three 0.2 s steps took
  0.60 s, which looks sequential — but the probe's handler was synchronous and its
  `time.sleep` blocked the event loop, so the number was an artifact of the probe. Re-run
  with an async handler that records each step's interval: two read-only steps overlap
  (`overlap=True`) and the three-step plan finishes in **0.407 s**, not 0.60 s. `asyncio.gather`
  over the wave is genuinely concurrent.
- **An unknown capability is reported, not fatal.** A task naming a capability this build
  does not declare produces `IssueKind.UNKNOWN_CAPABILITY` in the decomposition (visible
  in the plan report) and the plan is still built; `_structurally_blocked` excludes it on
  purpose, the same way it excludes a missing prerequisite (§15).
- **An unmeasurable goal is refused before anything is planned.** A goal with no success
  conditions yields `IssueKind.UNMEASURABLE` and `planned=false`; executing it answers
  `blocked` with "no candidate plan has been selected", and no step runs.

## 14. Benchmark methodology and metrics

`scripts/benchmark_planning.py` — thirteen deterministic scenarios (A–M, fourteen
measured legs because the approval scenario is played both ways), each checking **its
own** expectation and the process exiting non-zero if any leg fails. Scenario M was
added by the audit to pin defect 5 end-to-end: a run that outlasts its stated seconds
budget must report the breach. Nothing is canned:
the script builds a real `GoalManager` over a real `WorkflowExecutor`, the real
`VerificationEngine`, the real two planners, ranker, estimator, monitor, action policy
and JSON repository, and drives the whole pipeline.

Two substitutions, both named in the script's docstring: the world model is a plain
mapping handed to `StateGroundingService` (the shape that module documents for exactly
this purpose), and the step handler is a deterministic in-process function that records
the call and makes the step's declared effect true. That second one is the only place
real work is replaced, and it must be: a benchmark may not open a browser, a phone or a
file.

```
scenario                                       plan  planner        cand stp  run            plan ms  result
A  a single-step goal completes and is verified T   cost_aware_beam   2   1   completed        0.51  PASS
B  dependency order respected at plan and run   T   cost_aware_beam   2   2   completed        0.33  PASS
C  only read-only steps share a wave            T   cost_aware_beam   5   3                    0.62  PASS
D  a missing prerequisite is refused, named     F                     2   0                    0.16  PASS
E  a dependency cycle is refused and named      F                     0   0                    0.06  PASS
F  work whose result is already known is skipped T  dependency_order  3   0   completed        0.23  PASS
G  a contradicted precondition blocks the run   T   cost_aware_beam   2   1   blocked          0.18  PASS
H  a destructive step without an approval       T   cost_aware_beam   2   1   denied           0.17  PASS
H  the same step with the caller's approval     T   cost_aware_beam   2   1   completed        0.31  PASS
I  an unmeasurable goal is refused              F                     0   0                    0.05  PASS
J  a narrow goal is one short, declared plan    T   cost_aware_beam   2   1                    0.16  PASS
K  the candidate search respects its ceilings   T   cost_aware_beam   2   8                    0.39  PASS
L  replanning stops at the goal's own ceiling   T   cost_aware_beam   2   1   completed        0.16  PASS
M  a run past its budget reports the breach     T   cost_aware_beam   2   1   budget_exhausted 0.17  PASS

14/14 legs passed; 28 candidates compared, 64 search expansions, 7 step revalidations;
planning mean 0.25 ms / max 0.625 ms; estimate bases {'rule_based': 20}
```

Figures are per-scenario wall clock on this machine (Windows, CPython 3.13), not a
throughput claim. Planning is sub-millisecond at this scale because the scenarios are
small on purpose; the interesting number is that the search stayed inside its ceilings
(K: `expansions ≤ 2`, `truncated=true`) and that every estimate is labelled
`rule_based` — with no predictor and no learned rule wired, **twenty of twenty
estimates are declarations**, which is what the estimator is supposed to say.

**The benchmark was mutation-tested before it was trusted, and again on the audit's own
fix.** Changing the refusal branch of `_status_for` back to `return "completed"` made
scenario G fail (`a contradicted precondition must block the run, got completed; a
refused step must never read as completed`) and the script exit **1**; the change was
reverted and every leg passed again with exit 0, verified byte-identical to the
pre-mutation file. The audit repeated the method on defect 5: disabling the post-run
clock check in `execute()` made scenario M fail (`a run past its stated budget must say
so, got completed; the reason must name the limit`) and the script exit **1**, and the
restore was again verified byte-identical (sha256).

## 15. Known limitations

Recorded rather than hidden:

- **No learned predictive model ships.** `planning.prediction` and
  `planning.learned_knowledge` are `provider_dependent`: with no Phase 22 predictor and
  no Phase 23 rule that covers the action in scope, every estimate is the task's own
  declaration. Installed providers are used through the existing adapter; a provider
  that raises is UNAVAILABLE.
- **The search is a bounded beam, not a model-based planner.** No multi-step lookahead
  beyond the declared graph, no continuous state spaces, no learned policy. Complexity
  is documented per planner.
- **Parallelism is declared, not discovered.** Only `READ_ONLY` steps may share a wave,
  waves are capped at 4, and a caller who mis-declares an effect misleads the ordering
  and the skip rule. The model refuses the impossible case (a write claiming to be
  parallel-safe) but cannot detect a wrong declaration.
- **A plan is not restored.** Goals, conditions, budgets and decompositions survive a
  restart; a selection does not, deliberately. The record reports what was chosen
  (`previously_selected`) without adopting it.
- **`world_state` is partial.** Facts are grounded, never inferred beyond what the world
  model already computed; a fact nobody observed stays UNKNOWN, which can make a plan
  refuse to start rather than guess.
- **A capability gap warns; it does not block.** A task naming a capability this build
  does not declare is reported as `UNKNOWN_CAPABILITY` and the plan is still produced, so
  a partial plan exists to read. The refusal happens where the capability would actually
  be needed — tool selection and the permission layer — not at planning time. A caller
  that wants the stricter reading must refuse on the issue kind itself.
- **A limit is enforced on the clock, not on the estimate.** A plan whose declared
  estimate exceeds the goal's budget is reported at plan time and then *run*: the budget
  stops it when the clock passes, not when the arithmetic says it might.
- **No web panel, no CLI command.** The 7 routes are the interface; they are registered
  `no-render`.
- **Benchmark scale.** 14 legs and ≤8 tasks per scenario is enough to exercise every
  documented rule and is nowhere near a stress test. The live-server legs used the real
  hardware readers, which are machine-specific in what they report (not in whether they
  report).

## 16. Mock and provider-dependent capabilities

- **Step execution in the benchmark** is a deterministic in-process handler (§14). The
  live-server workflow does **not** use it: it ran the application's real step handler,
  which read this machine's real hardware through `_read_metric_step`.
- **World state in the benchmark** is an injected mapping. The live-server workflow used
  the real world model through `POST /world/observe`.
- **Phase 22's projection service** is a provider: absent → `unavailable`.
- **Phase 23's rule book** is a provider: absent, retired, out-of-scope or contradicted
  rules are not used, and the estimate says which of those happened.
- **No model is downloaded, loaded or trained by this phase**, and no weights are
  touched.

## 17. Compatibility with Phases 15–23

- Phases 15–20 (evaluation, training, preference, RLHF, RLVR, agentic RL): untouched;
  their suites pass. The planning layer is not wired into any training path.
- Phase 21 (perception) and Phase 22 (world model): read-only. The world model is read
  through `StateGroundingService`, and `world.observe` is how a live workflow makes a
  condition observed. Nothing writes to the world model from planning.
- Phase 23 (interactive learning): its rule book is consulted for an estimate with its
  scope and status, and a rule that is out of scope or retired is not borrowed. Planning
  does not create, promote or retire rules.
- Phase 4/6 planning (`planning/engine.py`, `compiler.py`, `plan/run`): unchanged and
  still passing; the goal layer calls the SAME executor and the SAME verifier.
- Phase 8 reliability: the permission layer, the verification engine and the recovery
  advisor are composed, not duplicated.
- Backward compatibility: one shared property changed behaviour (`StepOutcome.executed`
  no longer counts a denial) — a correction, not a break, and the full suite passes.

## 18. Explicitly deferred Phases 25–26

**Phase 25 (embodied/game agent) and Phase 26 (generalization, ARC, intelligence
evaluation) are not implemented by this phase.** One capability row
(`planning.generalization`) is declared `future` so that the table answers honestly; the
`planning.game_agent` placeholder was removed when Phase 25 shipped the real `embodied.*`
rows on the same registry (see the correction in the capability table above). There is no
game-playing agent, no complete autonomous game
controller, no ARC solver, and no unrestricted self-directed loop. The phase provides
stable interfaces a later phase could build on — a goal contract with measurable
success conditions, candidates with declared preconditions and effects, and an action
policy that asks before it acts — and nothing more.

## 19. Remaining failures and unverified requirements

- **Nothing failed.** The full suite passed in four disjoint slices during the phase
  build (**3,789 passed, 0 failed, 12 skipped, 2,192 subtests, all four exit 0**). The
  audit pass re-ran every surface its edits touch on the **final tree**, all exit 0:
  the phase suite (90 passed), planning/reliability/persistence/world (372 + 50
  subtests), application/API/reference-sync (40 + 250 subtests),
  config/events/route-shape/web (133 + 124 subtests), both mypy views clean over 390
  files, `docs/API.md` in sync, and the benchmark at **14/14 with exit 0** — with
  scenario M mutation-checked (disabling the post-run clock check made M fail and the
  process exit 1; the revert is byte-identical by sha256). The live workflow was 30/30
  with a verified restart leg (§12, measured during the phase build).
- **Deliberately not repeated by the audit:** the four-slice full suite and the 30-leg
  live-server workflow (§12). The audit's source edits were confined to
  `planning/goal_manager.py` (defects 5–6) and the benchmark script; every consumer of
  those — the phase suite, the application/API/config/events groups, the benchmark — was
  re-run green afterwards, so the ~3,150 tests outside those groups exercise code the
  audit never touched. The live workflow predates defects 5–6, but what those fixes
  changed (a breach label after a run overruns a stated limit; a distinct stop kind for
  a precondition the state could not settle) is exercised through the same HTTP surface
  by the phase suite's workflows and by benchmark scenario M. Re-running both is the
  cheap follow-up if belt-and-braces is wanted.
- **CI was not run for this phase.** The verification above is local (Windows, CPython
  3.13). The repository's CI runs on Linux, and the Phase 22 audit showed that a
  host-dependent defect can hide from a Windows-only run — so "the suite is green here"
  is not the same claim as "CI is green". This is the one acceptance item that is
  reported as unverified rather than passed.
- **Not exercised:** a machine with a live Ollama/Qwen model wired into the estimator (no
  model is available here), a Phase 22 prediction provider answering (`model_unavailable`
  is what this build reports), and playwright-backed end-to-end legs (playwright is not
  installed — the 12 skips).
- **Not measured:** planning throughput at scale (hundreds of tasks). The benchmark's
  largest scenario is 8 tasks.

## 20. Final acceptance checklist and verdict

| Acceptance criterion | Evidence | Status |
|---|---|---|
| Goals can be represented and decomposed into verifiable tasks | §4; `GoalRepresentationTests`, `DecompositionValidationTests`; workflows A/B | ✅ |
| Plans respect dependencies, constraints and preconditions | §5, §8; workflow B; benchmark B/D/E; gate revalidation | ✅ |
| Candidate plans can be compared by a documented selection method | §6; `test_ranking_names_one_feasible_candidate_and_never_an_infeasible_one`; workflow D | ✅ |
| Action selection uses verified state and available capabilities | §7; grounding/revalidation tests; capability table read live | ✅ |
| Planning is integrated with the existing execution engine | §2; `_gated_executor` wraps the application's `WorkflowExecutor` | ✅ |
| Unexpected outcomes cause safe reassessment or recovery | §8; replanning tests; workflow I; benchmark L | ✅ |
| Completion requires verified success conditions | §8; completion tests; live leg 4 → leg 5 | ✅ |
| Safety permissions and resource budgets are enforced | §9; approval tests; policy tests; config bounds tests; `RunLimitTests` (an overrun is named, and a step past a limit is never started); benchmark M | ✅ |
| Prediction uncertainty and provider unavailability are represented honestly | §7; estimation honesty tests; `confidence=None`, `basis=unavailable` | ✅ |
| Phase 23 learned knowledge is used with evidence and scope | §7, §17; scoped-rule, out-of-scope and retired-rule tests | ✅ |
| Replanning and stopping conditions are bounded | §8; ceiling tests; benchmark L | ✅ |
| Relevant user-facing workflows executed successfully | §12; 30/30 legs over HTTP on a real server, plus a restart | ✅ |
| Genuine defects fixed or documented | §13: six defects, each fixed and pinned | ✅ |
| Regression, type and API checks completed | §11: suite, both mypy views, API sync, dead-code hunt | ✅ |
| Documentation matches the final code | this file; `STATUS.md`, `DEVELOPMENT_LOG.md`, `README.md`, `PLANNING.md`, `API.md` | ✅ |
| No Phase 25/26 functionality claimed | §18; two `future` capability rows | ✅ |

**Verdict: PASS.** The one criterion that is *not* fully closed is CI (§19): the
verification is local, and this repository has previously been broken by a defect that
only Linux CI could see. Everything claimed above was measured on this machine, and the
commands to reproduce each row are in §11–§14.
