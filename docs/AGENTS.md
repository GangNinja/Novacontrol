# Multi-Agent System

The Phase 5 agent subsystem provides deterministic routing and agent communication contracts. It is ready for LLM-backed implementations in later phases without changing the public boundary.

## Agents

Default specialized agents:

- Research Agent
- Coding Agent
- Debug Agent
- Planning Agent
- Documentation Agent
- Design Agent
- Testing Agent
- Review Agent
- Browser Agent
- Desktop Automation Agent
- Project Manager Agent

The Coordinator Agent routes tasks by intent and delegates them through the `AgentRegistry`.

## Components

- `AgentTask`: goal, role, metadata, status, and id
- `AgentResponse`: structured task result
- `AgentMessage`: agent-to-agent message
- `AgentMessageBus`: in-process agent message history and delivery
- `AgentRegistry`: stores agents by name and role
- `CoordinatorAgent`: chooses the best role for a task
- `AgentModule`: event-driven runtime module for task delegation

## Events

- `agent.task_requested`: asks the coordinator to route and execute a task
- `agent.task_completed`: emitted when an agent completes a task
- `agent.task_failed`: emitted when delegation fails

## Future Work

Later phases should connect agents to memory, tools, planning graphs, LLM providers, and progress tracking persistence.

## Multi-agent collaboration contracts (Phase 28)

- `CollaborationEngine` is a coordinator over the existing `AgentRegistry` and
  `SpecialistPipeline`; subtasks always traverse the shared eight-stage pipeline.
  There is no second planner, verifier, memory store or executor.
- `AgentRole` adds `DATA_ANALYSIS` and `VERIFICATION`. Phase 28 installs four
  real specialists: research-agent, developer-agent, data-analysis-agent and
  verification-agent. Phase 35 adds a fifth, read-only business-validation-agent
  (`AgentRole.BUSINESS`). Test-only scripted agents must never ship.
- A collaboration result is verified only from the agent run's verification
  stage, plus independent verifier subtasks. `AgentResponse.status == COMPLETED`
  is not proof. A verifier with nothing checked is a failure.
- Independent ready subtasks run in one bounded `asyncio.gather` wave; same-role
  candidates rotate by subtask position so multiple agents actually participate.
  Retries are only for idempotent/read-only units; writes are never retried.
- Coding plans are `requires_approval=True`; `authorize=False` returns DECLINED
  and writes nothing. The coding subtask receives `apply the change: ...`; the
  verifier receives the test clause.
- Conflicts compare claim topics/values and use independent recomputation when
  available; unresolved conflicts block COMPLETED and are surfaced. Majority
  vote is not a resolution mechanism.
- Persistence uses `JsonStateStore` namespace `collaboration`, schema
  `phase28.1`; running records restore INTERRUPTED and are never replayed.

## Product pipeline specialist (Phase 36)

- `product-pipeline-agent` is registered in the existing `AgentRegistry` and
  returns a read-only summary of product projects, states, readiness, gates and
  the no-external-launch boundary. It is advisory and cannot approve, execute,
  write product files, prepare a release or authorize launch.
- Development tasks use the existing roles (`planning`, `coding`, `testing`,
  `review`, `documentation`). `ProductService` accepts an agent result as
  completed only when it carries verified evidence; otherwise the task becomes
  `needs_review`. A real executor can return a `TaskOutcome` with verified
  evidence and files, which are written only into the isolated product workspace.
- The product manager, architect, backend/frontend/database, testing, security
  and documentation responsibilities are logical task roles, not additional
  heavyweight agents. Phase 36 adds no second orchestration framework, scheduler
  or tool permission model.

## Business validation specialist (Phase 35)

- `business-validation-agent` is registered in the existing `AgentRegistry` with
  `AgentRole.BUSINESS` and the regular `handle_task`/pipeline path. It is
  read-only: it reads opportunities and status from
  `BusinessOpportunityService` and returns a summary plus a boundary statement.
- It has no tool that validates an opportunity, approves an experiment, changes a
  lifecycle stage or executes an external action. A `VALIDATED` verdict is only
  produced by the service's evidence rules; an agent recommendation is not a
  verdict.
- The eight Phase 35 responsibilities (discovery, market, customer, competitors,
  feasibility, economics, validation planning, evaluation) are deterministic
  modules in `novacontrol.business`, not separate agents. This avoids eight
  heavyweight agents and keeps the numerical/report path offline-capable.
  Completed work survives; `resume()` is explicit.
- API: eight `/collaboration/*` routes in handler, `ApiSurface`,
  `route_consumers`, generated `docs/API.md` and tests. Config lives in
  `NovaControlConfig.collaboration`; these are coordination ceilings, not
  permission grants.

## Personal knowledge in agent context (Phase 32)

- Agents receive personal knowledge only through `_handle_agent`'s
  `KnowledgeAccess.agent(...)` view: private and sensitive records are excluded,
  personal records require permission, and project-scoped records require an
  explicit project (an agent with none receives only global records).
  The permitted rows are attached to `AgentTask.metadata["personal_knowledge"]`;
  `SpecialistPipeline._stage_context` turns them into the run's context text.
- Do not assemble a second agent-context path or let an agent query the
  `PersonalKnowledgeService` directly. The shared pipeline is the one context
  assembly point; context is advisory and never grants a tool or permission.
- Personal records and continual-learning strategies are distinct: skill
  retrieval may return personal context alongside skill matches, but personal
  facts never become learned strategies. Device/desktop/browser execution still
  requires the existing approval token, permission layer and audit trail.
- Owner/agent visibility is one check (`_visible`) used by retrieval, inspect,
  graph and export. Explicit owner by-id control of a sensitive record is
  allowed; bulk owner search/export still requires an explicit sensitive opt-in.
  If a new surface exposes personal knowledge, route it through that check;
  never rely on the caller having filtered the ids.
