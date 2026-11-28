# Multi-Agent Collaboration (Phase 28)

Status: implemented and exercised; see [PHASE28_REPORT.md](PHASE28_REPORT.md) for
the acceptance verdict and limitations.

## Specification basis

There is no Phase 28 specification in this repository. `ROADMAP.md` stops at
Phase 20, `docs/PHASES.md` stops at Phase 15, and `docs/PHASE27_REPORT.md`
already records that its own brief had to serve as the specification. The
Phase 28 request supplied with the implementation is therefore the acceptance
contract. This document describes what was built; it does not invent roadmap
scope.

## One coordination layer, not a second agent framework

The collaboration layer sits above the existing Phase 12 agent subsystem:

```
AgentRegistry  ──►  CollaborationPlanner  ──►  CollaborationEngine
     │                    │                         │
     │              assess / decompose         schedule DAG waves
     │                                          │
     └── SpecialistAgent ──► SpecialistPipeline (NLU → Context → Decision
                              → Planner → Tool Selection → Execution
                              → Verification → Recovery)
```

There is no second planner, decision engine, verifier, memory store or executor.
Every subtask is handed to a registered agent through its ordinary
`handle_task` / `run_task` path. The collaboration layer adds only coordination:
benefit assessment, decomposition, assignment, bounded concurrency, dependency
context, conflict handling and aggregation.

## Agents and capability contracts

`AgentRole` now includes `DATA_ANALYSIS` and `VERIFICATION` in addition to the
existing research, coding, planning, browser, desktop and phone roles. The
application installs four real pipeline specialists:

| Agent | Role | Real work |
|---|---|---|
| `research-agent` | RESEARCH | Explore research through the existing provider; cited claims |
| `developer-agent` | CODING | Repository inspection, planning, authorized writes and test execution |
| `data-analysis-agent` | DATA_ANALYSIS | Reads a CSV inside its root; SHA-256, row/column statistics, recomputable claims |
| `verification-agent` | VERIFICATION | Recomputes metrics, checks citations and file hashes, runs the project's test suite |

Scripted agents exist only in `tests/test_multi_agent_collaboration.py` as
timing, failure and conflict harnesses. No shipped agent returns a predefined
answer.

`CollaborationEngine.status()["agents"]` publishes each registered agent's
name, role, description, verification requirement, permissions declared by the
agent, and whether it is a pipeline specialist. Availability is probed from the
live agent, not copied into a boot snapshot: an unavailable agent is reported
with its reason and is never selected for a subtask.

## When collaboration is used

`CollaborationPlanner.assess()` is deterministic and model-free. It reads the
objective and the registered roles and returns `beneficial`, a mode, a rationale
and limitations. Modes are `research`, `coding`, `data_analysis` and `single`.

Collaboration is declined when:

- the objective reads as one action (`open calculator`);
- fewer than four words carry no explicit collaboration marker;
- no registered agent fills a required role;
- the plan would be destructive and policy does not allow it;
- a coding plan is run without `authorize=True`.

A simple task keeps the existing single-agent path. In particular, the planner
does not split on the word "and" alone: "open chrome and search" is one action.

## Delegation and execution

- A plan is a validated DAG. Duplicate task ids, unknown dependencies and
  cycles are rejected before any agent runs.
- Before dispatch, a subtask that declares `metadata.provider_requirements` is
  checked against the live completion provider's DECLARED capabilities. A
  missing capability, unknown context ceiling or too-small output ceiling is a
  FAILED subtask with the exact missing requirement — the agent is never called
  on a guess.
- Ready subtasks run in one `asyncio.gather` wave. A semaphore bounds how many
  run at once. Its limit starts at `max_concurrency` and is lowered from the
  live `ResourceGovernor.assess()` level (`critical` → one at a time, `tight` →
  at most two). Without a hardware reading the level is `unmeasured` and the
  policy limit is honored rather than reduced on a fabricated number.
- Only agents whose live availability probe succeeds are candidates. They are
  selected by declared role, then independently verified learning
  history (Phase 27), then registry order. Two independent subtasks with the same
  role rotate across candidates by subtask position, so a second capable agent
  actually participates; a retry moves to the next candidate.
- Dependent subtasks run only after every dependency is COMPLETED. A failed
  dependency records its dependents as SKIPPED with the reason rather than
  silently omitting them.
- Each attempt is recorded. Retries are allowed only for subtasks declared
  idempotent/read-only; writes are never retried. A retryable failure can be
  reassigned to a different capable agent. A rate-limited failure honors
  `Retry-After` first, then uses capped exponential backoff; the delay and its
  source are recorded in `task.backoff_events`.
- Token usage is accounted in ONE `TokenLedger` per collaboration. A call with
  no reported token fields is counted as an unreported call, never as zero
  tokens. `token_budget` (0 = unbounded) blocks only subtasks that have not
  started yet; already-running work is allowed to finish and an overrun is
  reported rather than silently truncated.
- Each subtask has a timeout. A timeout is a reported failure, not an empty
  result.
- Cancellation stops new scheduling and cancels only idempotent in-flight work.
  Completed results remain. `resume()` re-queues cancelled or failed retryable
  units and preserves non-idempotent completed/failed units. A resumed plan that
  writes or executes requires a fresh `authorize=True`; the earlier run's
  approval is never reused.
- An idempotency key returns the existing task, preventing duplicate execution
  on a repeated API call.

## Context, messages and provenance

A subtask receives only:

- its declared `context`;
- public views of the results it declared in `depends_on`;
- immutable task metadata: collaboration id, objective, subtask id and role.

Unrelated subtasks never receive each other's context. A dependent gets a
`verified` flag, agent, content, claims, evidence, conflicts and errors for each
upstream result — enough to reason, not the whole internal run.

The engine records structured messages for assignment, dependency readiness,
completion, failure, cancellation and conflicts. The shared `AgentMessageBus`
carries them, and each message has a kind, sender, recipients, task id,
correlation id, timestamp and structured payload. The engine also publishes
typed `collaboration.*` events through the existing event bus.

Every result carries `verified`, `claims`, `evidence`, `confidence` and its
source agent. Unverified findings are not silently promoted to verified ones.

## Conflicts and aggregation

Claims are compared by topic and value. A conflict is surfaced when two
different agents report incompatible values. Conflicts are never resolved by
majority vote.

When a separately declared verification subtask recomputes the metric, its
`checks` evidence can resolve the conflict in favour of the value that matches
the recomputation. A conflict that cannot be settled remains `resolved=False`;
the collaboration cannot be `completed` and the unresolved topic is published.

Aggregation:

1. includes only results that actually ended COMPLETED;
2. requires every required subtask to be verified before `verified=True`;
3. reports failed, skipped, cancelled and unverified subtask ids;
4. reports unresolved conflicts separately;
5. includes timing, per-agent contributions, learning metadata and summed
   numeric usage when agents report it;
6. never treats an agent's `COMPLETED` response as independent verification.

## Safety and permissions

`authorize=True` builds a per-run authorized pipeline through the injected
factory, exactly as the Phase 12 specialists already do; the build's shared
pipeline is untouched. A coding plan requires approval and is DECLINED without
it; a resumed write/execute plan needs a fresh approval rather than reusing the
earlier run's. The developer agent's write action remains destructive/non-reversible and
keeps its existing approval declaration. The engine never edits permissions,
risk decisions or approval results.

Destructive objectives are refused unless policy explicitly allows them. The
engine's published boundaries state:

- `replaces_specialist_pipeline: false`
- `widens_permissions: false`
- `retries_non_idempotent_subtasks: false`
- `destructive_actions_require_existing_approval: true`
- `agent_output_is_trusted_only_after_verification: true`

## Persistence and recovery

Collaboration tasks persist through the shared `JsonStateStore` under namespace
`collaboration`, schema `phase28.1`. The record includes the objective, mode,
plan, assignments, dependency graph, results, messages, conflicts, aggregate,
timing, learning record, error and lifecycle timestamps.

On restore:

- terminal tasks are restored as-is;
- a task recorded as running becomes `INTERRUPTED`, never replayed;
- completed results are preserved;
- a non-idempotent interrupted unit is not retried and is reported as failed;
- `resume()` is explicit, so a restart cannot silently repeat a side effect;
  plans that write or execute also require fresh authorization.

The application flushes collaboration state on stop and restores it on boot.

## Phase 27 learning

A terminal collaboration captures one Phase 27 `ExecutionEvidence` record with
task kind `collaboration.<mode>`. It records assignments, attempts, retries,
latency, verified counts, conflicts and the aggregate verdict. The learning
engine's evaluator decides the outcome; the collaboration layer does not mark
its own work `success`. Past verified agent participation is used only to order
candidates; unverified history never becomes successful evidence.

## Configuration

`NovaControlConfig.collaboration` (`CollaborationSettings`) provides:

| Setting | Default | Purpose |
|---|---:|---|
| `enabled` | true | coordination switch |
| `persist` | true | write tasks to the shared store |
| `capture_learning` | true | emit Phase 27 evidence |
| `max_concurrency` | 3 | simultaneous agent executions |
| `max_subtasks` | 6 | plan ceiling |
| `max_tasks` | 50 | retained task records |
| `max_retries` | 1 | retry ceiling for idempotent work |
| `default_timeout_seconds` | 120 | per-subtask timeout |
| `rate_limit_backoff_seconds` | 0.5 | base delay before retrying an idempotent rate-limited subtask |
| `rate_limit_backoff_max_seconds` | 5 | maximum delay, including a provider `Retry-After` |
| `token_budget` | 0 | per-collaboration reported-token budget; 0 is unbounded |
| `hardware_aware` | true | lower concurrency from a live resource assessment |
| `sequential_fallback` | true | critical pressure runs one subtask at a time |

These are coordination ceilings, not permissions. No value here widens a tool
grant.

## API

Eight routes are registered in all required places (handler, `ApiSurface`,
route consumer, generated `docs/API.md`, tests):

- `GET /collaboration/status`
- `GET /collaboration/overview`
- `POST /collaboration/plan`
- `POST /collaboration/run`
- `GET /collaboration/tasks`
- `GET /collaboration/tasks/{task_id}`
- `POST /collaboration/tasks/{task_id}/cancel`
- `POST /collaboration/tasks/{task_id}/resume`

`POST /collaboration/run` accepts `objective`, optional `context`, optional
`idempotency_key`, optional `authorize`, and optional `source`. Coding plans
return `DECLINED` unless `authorize=true`. `POST
/collaboration/tasks/{task_id}/resume?authorize=true` requires a fresh
authorization for a plan that writes or executes.

## Verification and measurements

`tests/test_multi_agent_collaboration.py`: 61 tests, including real CSV analysis,
real pytest execution in a disposable project, live HTTP, dependency context,
conflict resolution/surfacing, failure isolation, unavailable-agent filtering,
non-completed responses, centralized token accounting and budget exhaustion,
provider-capability preflight, Retry-After/exponential backoff, hardware-aware
concurrency, retry, timeout, cancellation, authorized resume, persistence and
Phase 27 learning.

Measured on this machine, 20,000-row/3-column CSV:

| Measurement | Result |
|---|---:|
| Single agent, all columns | 135.79 ms, verified |
| Four-way collaboration median wall | 572.43 ms, 4/4 verified |
| Engine wall | 568.50 ms |
| Summed subtask time | 845.32 ms |
| Measured parallelism | 1.49 |
| Serial collaboration median wall | 592.00 ms |
| Parallel vs serial speedup | 1.03× |
| Peak traced memory (one run) | 21,969 KiB |

The collaboration path is slower than one agent for this small dataset because
it intentionally adds independent recomputation and per-column agents. The
result is reported as coordination overhead, not as a speedup claim.

## Known limits

- Token counts are only as complete as the numeric usage agents report; there
  is no billing reconciliation or token estimation from prose. A call with no
  report is visible as `calls > reported_calls`, not charged a fabricated count.
- Provider checks use declarations, not discovery: a provider that never
  declares a context/output ceiling cannot satisfy a requirement for one.
- Hardware scheduling uses the live governor reading and degrades to the policy
  limit with `level="unmeasured"` when no reading exists; it does not infer a
  machine's capacity from its name.
- No web panel for collaboration; this phase exposes the HTTP/API surface.
- The coding scenario is verified in a disposable project with a controlled
  change builder; running the product's real change generator against the live
  repository was not performed.
- The live research scenario depends on network availability and the configured
  research provider.
- The full repository suite was not completed in this session; the focused
  Phase 28 and relevant regression groups were run.

## Skill selection inside a specialist pipeline (Phase 31 integration)

A specialist that has no procedure of its own for a subtask can now run a
validated skill from the one skill library instead of reporting "nothing to do".
The seam is optional (`SpecialistPipeline(skills=...)`) and is wired by the
application to its single `SkillService`.

* The agent's own plan always wins; selection happens only when the plan is empty
  or the task names a skill.
* The skill step is authorized through the same `PermissionManager`, executed by
  `SkillService` and verified from the library's own verdict.
* Unvalidated, incompatible or under-specified skills are not selected, and a
  skill waiting for approval makes the run fail rather than succeed.

See [SKILLS.md](SKILLS.md) for the library contract and
[GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md](GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md)
defect D1 for the audit that required it.
