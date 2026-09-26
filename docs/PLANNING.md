# Planning System

The Phase 6 planning subsystem decomposes goals into dependency-aware workflow plans, detects ambiguous requests, and executes plans with retry-based recovery.

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
