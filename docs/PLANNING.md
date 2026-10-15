# Planning System

The Phase 6 planning subsystem decomposes goals into dependency-aware workflow plans, detects ambiguous requests, and executes plans with retry-based recovery.

Phase 24 adds a goal-level layer **above** this one (see
[docs/PHASE24_REPORT.md](PHASE24_REPORT.md) for the full report); this page covers both,
and the boundary between them is deliberate: Phase 6 turns a decided request into ONE
plan and runs it, and Phase 24 turns a stated goal into SEVERAL candidate plans, grounds
them in state, refuses the ones that cannot run, asks before a consequential step, and
decides completion from observed conditions. Phase 24 runs its steps on the SAME
`WorkflowExecutor` — there is no second execution engine.

## Components

- `ClarificationPolicy`: detects goals that need more information
- `PlanningEngine`: converts goals into plans
- `Plan`: workflow plan with steps and status
- `PlanStep`: executable unit with dependencies and optional role metadata
- `WorkflowExecutor`: executes steps in dependency order
- `RecoveryPolicy`: controls step retry attempts
- `PlanningModule`: event-driven runtime module for plan creation and execution

## Events

- `planning.plan_requested`: asks the planning module to create a plan
- `planning.clarification_required`: emitted when the goal is too ambiguous
- `planning.plan_created`: emitted after plan creation
- `planning.workflow_completed`: emitted after successful execution
- `planning.workflow_failed`: emitted after execution failure

## Boundary

Planning does not import agent implementations. Plans may include role metadata, but assigning work to concrete agents remains the responsibility of the agent subsystem.

## Goal-level planning (Phase 24)

`GoalManager` is the orchestrator:
`ground → decompose → candidates → rank → authorize → execute → verify → reassess`.
Every collaborator is one this build already has, and each is optional with an honest
default (no world model means every fact is an assertion; no executor means `execute()`
refuses rather than pretending).

| Module | Owns |
|---|---|
| `planning/goals.py` | `Goal`, `Task`, `SuccessCondition`, `GoalBudget`, `decompose()` and the seven `IssueKind` validations |
| `planning/grounding.py` | `StateGroundingService`: facts read from Phase 22 and classified observed / inferred / stale / conflicting / unknown |
| `planning/candidates.py` | `BaselineOrderPlanner` (dependency order, O(T log T + E)), `CostAwarePlanner` (bounded beam), `CandidatePlan`, `PlanningBudget` |
| `planning/ranking.py` | the documented selection score; an infeasible candidate can never be selected for execution |
| `planning/estimation.py` | `OutcomeEstimator`: `world_model` → scoped `learned_rule` → `rule_based` declaration → `unavailable` |
| `planning/policy.py` | goal exclusions (absolute), approval conditions, one approval set read by both the policy and the executor's confirmation channel |
| `planning/monitor.py` | revalidation against fresh state, prediction-vs-observation, `CompletionVerdict`, the replan decision |
| `planning/goal_manager.py` | the order the above are called in, plus the state that remembers what happened |
| `planning/capabilities.py` | the 20 capability rows, with availability read live from the manager |

**Three rules the code enforces.** A plan is not an execution (`plan()` never touches
the machine; only `execute()` claims steps ran, and it says so). Completion is not
elapsed work (the goal's own conditions must be **observed** holding, so a run whose
condition nobody observed reports `needs_observation` with `verified=false`). And a
refusal is a first-class answer — in the refusal string, the `IssueKind`, the
`PolicyVerdict`, and the run status — so nothing that was refused reports success.

**State the plan leans on.** Before every step, the gate re-checks that step's own
preconditions against fresh grounding and raises `PermissionError` when they no longer
hold; the executor reads that as a refusal (no retry, `DENIED`, downstream blocked). The
facts an earlier step produced are read from the executor's own `StepContext.outputs`
plus that step's DECLARED effects. A step whose state is unknown is not attempted.

**Surfaces.** `GET /planning/status`, `GET /planning/goals`, `POST /planning/goals`,
`GET|POST /planning/goals/{goal_id}` and `/run`, `/reassess`, `/cancel` — all
API/operator-facing (`no-render` in `api/route_consumers.py`; there is no panel and no
CLI command for goal planning). `status()["app"]["goal_planning"]` carries the summary
and the boot restore report. Eight `goal.*` event types travel on the one event bus.
Run the deterministic benchmark with
`PYTHONPATH=src python scripts/benchmark_planning.py`.

## Executing a plan's own commands

`run_tests` and `run_command` steps are carried out by the application's step handler
(`_run_command_step`), which decides nothing by guesswork:

- **The runner is recognised, not assumed.** The located project's `package.json`
  means `npm test`; a pytest marker (`pytest.ini`, `pyproject.toml`, `setup.cfg`, a
  `tests/` directory or the requested scope) means `pytest` through the interpreter
  that owns the process. A project matching neither is reported as unrecognised
  instead of run.
- **The command is executed without a shell.** The step's text is split into literal
  argv tokens and run exec-form (`create_subprocess_exec`), so a metacharacter cannot
  chain a second command past the one that was approved; a hard wall-clock timeout
  stops a hung process, and the output's tail — where a test summary actually is —
  travels with the result beside its exit code.
- **Nothing runs without explicit approval.** The caller names the step ids in
  `run_plan(goal, approved=[...])` (exposed as `POST /plan/run`); an unnamed step is
  refused with a `PermissionError`, which the executor records as DENIED and blocks
  everything downstream, because silence is not consent.

The exit code is handed to the deterministic verifier, whose `exit_code` method
accepts any code for these steps while still recording it — a non-zero exit is the
*result* of "run the tests and tell me what failed", not a failed step.
