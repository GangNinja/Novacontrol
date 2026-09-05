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
