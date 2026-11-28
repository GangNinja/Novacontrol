# Persistent Operations

Phase 30 turns NovaControl's request/response work into **durable objectives**:
a goal keeps its tasks, evidence, deadlines, approvals and history across
restarts, and a background tick advances only the work that is actually
eligible.

The package is `src/novacontrol/operations/`. It is an objective/continuity
layer over infrastructure that already existed — it does not execute anything
itself and does not create a second scheduler, task manager or agent loop.

## Architecture

```
/objectives, /operations/*            Operations panel (render-operations.js)
        │                                      │
        ▼                                      ▼
NovaControlApplication  ──────────────►  OperationsService
  │  persist("operations")                 │  tick(now) / run_task()
  │  status()["persistent_operations"]     │  autonomy + PermissionManager
  ▼                                        ▼
JsonStateStore  ◄──────────────  OperationsManager (durable state machine)
                                           │
                     ┌─────────────────────┼─────────────────────┐
                     ▼                     ▼                     ▼
              Automation              handle_request        agents pipeline
              Schedule                 (executor)          (research/developer)
```

- **One execution path.** `OperationsService` is constructed with an injected
  `execute_task` callable. The application injects
  `_execute_operation_task`, which sends the task's instruction through the
  existing `handle_request` pipeline (and its planner, tools, permissions and
  event bus). A task's `agent` value (`research`, `developer`) delegates through
  the existing Phase 28 pipelines; there is no second orchestration engine.
- **One scheduler.** Recurrence uses `automation.schedule.Schedule` and its
  parser; the ticker is a single `asyncio` task started by
  `application.start()` and stopped by `application.stop()`. There is no
  second scheduler loop.
- **One permission authority.** Every executing task is assessed by the existing
  `PermissionManager`; a sensitive task parks in `waiting_approval` at every
  autonomy level and only a recorded approval resumes it.
- **One event bus.** Objective/task/notification transitions publish typed
  events from the existing `EventBus` vocabulary; payloads are validated at
  publish.

## Data model (`models.py`)

| Model | Key fields |
|---|---|
| `Objective` | id, title, intended outcome, kind, status, priority, autonomy, deadline, tags, tasks, milestones, progress, created/updated/completed |
| `OperationTask` | id, objective id, title, instruction, agent, steps, pending/completed steps, evidence, depends_on, recurrence, scheduled_for, sensitivity, state, attempts, checkpoints, approval |
| `Milestone` | id, title, due_at, completed, evidence |
| `TaskCheckpoint` | at, state, detail, evidence — the last verified position |
| `Notification` | id, kind, severity, title, body, objective/task, read, dedupe_key, created_at |
| `MonitoringSignal` | kind, severity, objective/task, detail, actionable, dedupe_key |

Task states: `pending → ready → in_progress → (completed | failed | blocked |
waiting_approval | cancelled)`. `failed`, `blocked` and `waiting_approval` are
sticky until an explicit retry/decision; `refresh()` never resurrects them.

Objective states: `active | paused | completed | cancelled`. An objective is
`completed` only when **every** task is `completed`; a rejected task cancels
that task, not the objective.

## Lifecycle invariants

- **Creation is not completion.** `complete_task` requires `verified=True`;
  the service marks a run `failed` when the executor did not verify its outcome.
- **Dependencies are enforced.** A task becomes `ready` only when all
  `depends_on` tasks are `COMPLETED` and its `scheduled_for` has arrived.
  Cycles are refused by `_assert_acyclic`, and dependencies may only name tasks
  that already exist.
- **Duplicate completion is refused.** Completing an already-completed task
  raises rather than double-counting progress or side effects.
- **One successor per recurrence.** Completing a recurring task spawns exactly
  one successor, computed with `Schedule.next_after` from the tick's clock. A
  duplicate tick finds the successor not yet due and runs nothing.
- **Progress is derived.** `Objective.progress` is the completed-task ratio;
  it is never written by the UI.

## Scheduling

`POST /objectives/tasks` accepts objective-scoped scheduling:

- one-time: `scheduled_for` (timezone-aware ISO-8601) or `delay_seconds`;
- recurring: `recurrence` in `Schedule.parse` vocabulary (`every 1 hours`,
  `daily at 09:00`, `weekly on monday at 08:30`, …).

`OperationsService.tick(now)` is clock-injectable, which is what makes the
deadline and recurrence scenarios deterministic. The application ticker calls
it with the real clock every `operations_tick_seconds`; overlapping ticks are
prevented by the manager's single-threaded transitions and the service's
in-flight guard. Unavailable dependencies (planner, agent, executor) fail the
task with the reason — they are never reported as completion.

## Monitoring and notifications

`OperationsMonitor.scan()` derives signals from durable state only:

| Signal | Condition |
|---|---|
| `deadline_approaching` | active objective due within 24 h |
| `overdue` | active objective past its deadline |
| `stalled` | ready/running task with no state change for 24 h |
| `approval_required` | task in `waiting_approval` |
| `failed` / `blocked` | task in that terminal-ish state |
| `completed` | objective completed since the previous scan |
| `actionable` | ready task the autonomy level may execute |
| `no_change` | nothing above — a real answer, not an empty notification |

`NotificationCenter` deduplicates by `dedupe_key` inside a 6-hour window,
honours mute and enabled-kind preferences, and keeps a bounded history. Every
notification is derived from a signal that came from stored state; no
placeholder or synthetic activity is generated.

## Autonomy and approval

| Level | Executes | Sensitive work |
|---|---|---|
| `observe` | nothing | nothing |
| `recommend` | nothing (signals + notifications) | nothing |
| `approval` | non-sensitive tasks | parks for approval |
| `execute` | non-sensitive tasks | parks for approval |

The level is a **ceiling**, not a grant: `execute` never implies permission for
a destructive/sensitive action. Sensitivity is declared per task and is
assessed again by `PermissionManager.check` with the task's declared risk; the
stricter answer wins. Approvals are recorded with who approved, when, and the
reason, on the task itself (`task.approval`).

## Persistence and recovery

State is stored under the existing `JsonStateStore` namespace `operations`
(one document per application). `OperationsService.to_dict/from_dict` round-trip
the manager, notifications and settings.

Recovery procedure:

1. On boot, the application loads the `operations` snapshot before the ticker
   starts.
2. At load, `OperationsManager.from_dict` turns every stored `RUNNING` task into
   `READY` with `resume_required` (verified checkpoints kept). This happens on
   load, not only when someone presses Resume, so an ACTIVE objective cannot be
   stranded by a restart.
3. `resume` reassesses each interrupted task: it keeps verified checkpoints,
   reads the current conditions, and only then becomes ready again.
4. A task is never replayed from step 0 when it has verified checkpoints; its
   `pending_steps` are what the executor receives.
5. If the snapshot is unreadable, the manager starts empty rather than raising
   into application startup (the store's own contract), and the condition is
   visible in `status()["persistent_operations"]`.

`operations.stop()` persists first, then cancels the ticker and waits for it,
so shutdown cannot leave a half-written run behind.

## API

| Method | Route | Purpose |
|---|---|---|
| GET | `/operations` | board: objectives, notifications, preferences, learning, tick state |
| GET | `/objectives` | objectives only |
| POST | `/objectives` | create (title, outcome, kind, autonomy, priority, deadline) |
| POST | `/objectives/update` | edit title/outcome/priority/deadline |
| POST | `/objectives/control` | pause, resume, cancel, prioritize, deadline |
| POST | `/objectives/delete` | delete a closed objective |
| POST | `/objectives/tasks` | add task (steps, agent, depends_on, schedule, recurrence, sensitivity) |
| POST | `/objectives/milestones` | add milestone |
| POST | `/objectives/run` | execute eligible tasks now |
| POST | `/objectives/tasks/decide` | approve, reject, retry, skip |
| GET | `/operations/notifications` | notification history |
| POST | `/operations/notifications/read` | mark one/all read |
| POST | `/operations/notifications/clear` | clear history |
| POST | `/operations/notifications/preferences` | mute, enabled kinds |
| POST | `/operations/tick` | run one monitoring/execution tick |

All routes are registered in `ApiSurface` and `route_consumers`, and documented
in `docs/API.md` (generated; `--check` must stay green).

## Dashboard

The **Operations** tab (`render-operations.js`) renders the board from
`/operations`: objective cards with progress, milestones, task rows (state,
attempts, schedule, checkpoint evidence), controls (run, pause, resume, cancel,
prioritize, deadline, approve, reject, retry, skip, delete), the notification
rail and the learning summary. Every control posts to the route that performs
that change and then re-reads the board; there is no client-side status
invention. A create form, per-objective task form and milestone form write
through the same routes.

## Learning (Phase 27)

`operations/learning.py` summarises **observed** task outcomes by task kind and
agent: sample count, completion rate, average duration, retries. Evidence is
advisory: it is shown only with at least 3 samples and is reported with
`confidence` (`none`/`low`/`medium`/`high`). It never changes a schedule or
autonomy level on its own, so one unusual run cannot become a preference.

## Collaboration (Phase 28)

A task with `agent: "research"` runs through the research pipeline and returns
a sourced report; `agent: "developer"` runs through the developer pipeline and
returns its verification result. Failure of one workstream leaves the others
intact, the objective stays active, and `retry` re-queues only the failed task.

## Computer operation (Phase 29)

An operation task's instruction may be a computer objective; the executor sends
it through `handle_request`, which uses the same computer/desktop/browser
controllers and their approval gates. When that plan carries a single-use
approval token, the task's OWN recorded approval lets
`_run_approved_operation_plan` consume it through the public
`execute_desktop_command` / `execute_browser_command` door — the task is the
person's approval, so it is not asked twice. Every action must report
`completed`; "executed" is only reported after the controller's own
verification (an app window actually appeared).

## Limitations

- Background execution needs the application process to be running. After an
  OS restart, the application loads the objective and resumes on its next tick;
  it does not install an OS service.
- Deadline/stall thresholds are fixed constants (24 h / 30 min), not per-user
  settings.
- Timezone support is `datetime`'s offset-aware arithmetic; schedules store
  offsets, and DST transitions follow the offset supplied by the caller.
- The Phase 27 summary is observational, not a learned scheduler policy.
- `observe`/`recommend` objectives report signals but intentionally execute
  nothing.
