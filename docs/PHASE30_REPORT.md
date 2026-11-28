# Phase 30 — Persistent Autonomy & Personal Operations

**Verdict: PARTIAL (phase open)**
**Date:** 2026-11-10
**Branch/HEAD:** `main` / `afa5ff6`, uncommitted worktree. No commit, push or deployment was made.

## 1. Objective and scope

Phase 30 turns NovaControl from a request/response assistant into a persistent
personal operations system: long-running objectives survive restarts, track
tasks/milestones/deadlines, run authorized work through the existing execution
architecture, monitor for action, ask for approval where required, and report
only what actually happened.

The implementation was recovered from the canonical phase text in the request
and the repository's roadmap/status documents. No roadmap file was edited.

## 2. Dependency assessment (Phase 29 PARTIAL)

Phase 29 is recorded PARTIAL for verification that needs physical/environmental
conditions: a second monitor, a live resolution change, OS sleep/resume, a
forced application crash, a multi-hour soak and macOS/Linux desktop sessions.
Its core operator, browser approval routing, persistence and live Windows
workflows are implemented and verified.

Those outstanding items are **verification gaps, not functional dependencies of
Phase 30**. Phase 30 calls the same computer/desktop/browser controllers through
`handle_request`; it does not re-implement or modify them. Phase 30 was therefore
implemented in parallel, and this phase's verdict is kept independent: Phase 30
does not inherit Phase 29's PASS, and Phase 29's gaps remain open. No deferral
approval was requested or invented.

## 3. What was built

`src/novacontrol/operations/` is a durable objective/continuity layer over the
existing `Schedule`, `TaskCenter`, `PermissionManager`, `JsonStateStore`,
`EventBus`, planner and agent pipelines.

- **Objectives** (`models.py`): outcome, kind (personal/academic/professional/
  research/development/business/recurring), status, priority, autonomy ceiling,
  deadline, tags, tasks, milestones, derived progress, timestamps.
- **Tasks**: instruction, steps, pending/completed steps, evidence, dependencies,
  schedule/recurrence, sensitivity, agent, attempts, checkpoints, approval.
- **Manager** (`manager.py`): create/edit/prioritize/deadline/pause/resume/
  cancel/delete; dependency refresh; cycle refusal; duplicate-completion refusal;
  sticky failed/blocked/waiting states; one recurrence successor per completion;
  verified checkpoints; resume reassessment; persistence.
- **Service** (`service.py`): tick/run, autonomy enforcement, `PermissionManager`
  assessment, approval parking, crash containment, signal + notification emission,
  learning summary in status.
- **Monitor** (`monitor.py`): deadline approaching, overdue, stalled, approval
  required, failed, blocked, completed, actionable, no_change; deduplicating
  `NotificationCenter` with mute, enabled kinds and bounded history.
- **Learning** (`learning.py`): Phase 27 evidence by task kind/agent with sample
  count, completion rate, duration, retries and a confidence label; advisory only.
- **Application wiring** (`application.py`): service construction, subscription,
  ticker start/stop, `persist("operations")`, `status()["persistent_operations"]`,
  objective/task/notification methods, executor through `handle_request`,
  reassessment, research/developer delegation.
- **API**: 15 routes in `api/app.py`, `ApiSurface` rows in `api/models.py`, and
  `route_consumers` declarations. `docs/API.md` regenerated at **419 routes**.
- **Dashboard** (`render-operations.js`, `#operationsPanel`, `setupOperations()`):
  create/edit/task/milestone forms, objective cards, task rows, notification rail,
  learning summary, and controls for run/pause/resume/cancel/prioritize/deadline/
  approve/reject/retry/skip/delete. Every control posts to the route that performs
  it and re-reads `/operations`.
- **Approved-plan follow-through**: a task whose instruction plans a desktop or
  browser action now consumes the plan's single-use token only after the TASK
  is approved, through the same public `execute_desktop_command` /
  `execute_browser_command` executor a person's Approve button uses. The
  controller's per-action result is the verification; a token alone is not.

## 4. Acceptance matrix

| ID | Requirement | Implementation evidence | Verification evidence | Verdict |
|---|---|---|---|---|
| 1 | Manage persistent objectives | `operations/models.py`, `manager.py`, `/objectives*` | 43 phase tests; live Scenarios A–G; dashboard create | PASS |
| 2 | Decompose into tasks and milestones | `OperationTask`, `Milestone`, dependency refresh | Unit tests; Scenario A chain; UI task form | PASS |
| 3 | Task state survives app and system restarts | `persist("operations")`, `to_dict/from_dict` | Scenario A app restart preserved tasks/evidence; **OS restart not performed** | PARTIAL |
| 4 | Interrupted workflows resume safely | checkpoints, `resume` reassessment, pending steps | Scenario D: verified checkpoint kept, only pending step ran | PASS |
| 5 | One-time and recurring scheduling | `Schedule`, `scheduled_for`, `next_after` | Scenario B: first run, successor at +1h, early/duplicate ticks no-op; unit tests | PASS |
| 6 | Deadline and stalled monitoring | `OperationsMonitor` | Scenario C: approaching + overdue + dedupe; stalled pinned by tests | PASS |
| 7 | Authorized operations execute autonomously | service → injected executor → `handle_request` | Scenarios A/B/D/E/F/G through the real service and permission layer | PASS |
| 8 | Sensitive operations keep approval | per-task `sensitive`, `waiting_approval`, recorded decision | Scenario E: pause → approve → one execution; reject → zero | PASS |
| 9 | Progress reflects execution evidence | `verified` gate, derived progress | Unverified outcomes fail; duplicate completion refused; Scenarios A/F | PASS |
| 10 | Notifications reflect real events | signal-derived `NotificationCenter` | Scenario C stored real unread notices; dashboard rendered persisted notices | PASS |
| 11 | Phase 27 learning integration | `learning.py`, `status()["operations"]["learning"]` | `LearningEvidenceTests`; confidence thresholds | PARTIAL — surfaced as evidence, does not yet adjust schedules/plans automatically |
| 12 | Phase 28 collaboration integration | `agent` dispatch to `research_task` / `developer_task` | Scenario F: two workstreams, one failed and recovered, no duplicate; live agent pipelines not invoked | PARTIAL |
| 13 | Phase 29 computer-operation integration | executor → `handle_request`; approved plans run through `execute_desktop_command` / `execute_browser_command` | **Scenario G**: approved objective opened Calculator and completed on the controller's verified action | PASS |
| 14 | Recovery prevents duplicate side effects | checkpoints, single successor, duplicate guards | A10/A11, D5/D6, F6 | PASS |
| 15 | Real workflows show persistent autonomy | real app + real service + real store + real desktop controller | Scenarios A–G, including a live external desktop objective and dashboard creation; **OS restart and multi-hour soak not performed** | PARTIAL |
| 16 | Tests and regressions pass | Phase + modified-module tests | **43 passed**; phase + web platform **89 passed / 462 subtests**; targeted API/web regression in this pass | PASS |
| 17 | Documentation and report | this report, `PERSISTENT_OPERATIONS.md`, STATUS, log, README, AGENTS, API | `generate_api_reference.py --check` in sync | PASS |

Supporting requirements:

| Area | Evidence | Verdict |
|---|---|---|
| Objective types (personal → recurring) | `ObjectiveKind` enum; kind selector in UI | PASS |
| Prioritization and deadline changes | `update_objective`, `control_objective("prioritize"/"deadline")`; unit tests | PASS |
| Schedule conflicts | `scheduled_for` gating and duplicate-tick protection; no automatic conflict-resolution policy | PARTIAL |
| Task requiring user input | `waiting_approval` + notifications | PASS |
| Monitoring states (no change … failed) | `SignalKind` table, `no_change` sentinel | PASS |
| Notification preferences | mute, enabled kinds, mark read/clear, dedupe window | PASS |
| Autonomy boundaries | ceiling semantics + per-task approval + `PermissionManager` | PASS |
| Isolation between objectives | task instructions/approvals scoped per objective; no cross-objective execution | PASS |
| Cancellation/shutdown | `cancel_objective`, `stop()` persists then joins ticker | PASS |
| Performance/resource use | benchmark below | PASS (bounded measurement) |

## 5. Live application verification (Scenarios A–G)

Script: `tmp/phase30/verify_phase30.py` → `tmp/phase30/evidence.json`.
Executor: a **controlled local filesystem worker** that performs real writes and
verifies each file before reporting success for Scenarios A–F. Scenario G uses
the application's REAL `_execute_operation_task` and real desktop controller
instead. **45/45 checks passed.**

- **A — long-running project:** created a three-task dependent project, ran task 1,
  paused, constructed a second `NovaControlApplication` on the same data dir,
  verified objective/task/evidence restored, resumed, ran the remaining two.
  First task was not re-executed; objective reached progress 1.0 only at the end.
- **B — recurring operation:** `every 1 hours` parsed; first tick ran once; exactly
  one successor at +1h; an early tick ran nothing; the due tick ran once; a
  duplicate tick ran nothing.
- **C — deadline monitoring:** a +5h deadline produced `deadline_approaching` and
  one stored notification; a second scan inside the window stored nothing; a +6h
  scan produced `overdue`; an observe-only objective executed nothing.
- **D — interrupted workflow:** one verified checkpoint written to disk and
  persisted; after restart, `resume` flagged reassessment; the task completed with
  `("prepare","send")` and the prepare file existed exactly once; only the pending
  step was executed.
- **E — approval boundary:** sensitive task parked, zero execution; approval led to
  exactly one execution; a second sensitive task was rejected and executed nothing.
- **F — multi-agent objective:** research completed, developer failed once (progress
  0.5), retry completed it (progress 1.0), attempts recorded, research ran once.
- **G — approved external desktop objective:** the real service planned
  `open calculator`, parked the task for approval, and — after the recorded
  approval — executed the plan through `execute_desktop_command` with its
  single-use token. The controller reported the action `completed` (window
  verification), the task carried that evidence, and the objective reached 1.0.
  This opened Calculator on the host desktop as the verification side effect.

Dashboard: served UI at `http://127.0.0.1:8011/` (data dir `tmp/phase30/ui_data2`).
The Operations panel rendered persisted state and notifications, the form produced
`POST /objectives → 200`, the board re-read and displayed the new objective, and
the console was clean.

## 6. Performance and resources

`tmp/phase30/bench.py` on Windows (`win32`), 100 objectives / 400 tasks:

| Metric | Result |
|---|---|
| Create all objectives/tasks | 14.59 ms |
| Snapshot size | 354,406 bytes |
| Serialise snapshot | 5.61 ms median (6.76 max) |
| Restore snapshot | 8.10 ms median (8.53 max) |
| Monitor scan (all objectives) | 0.65 ms median (1.08 max) |
| Notification store | 0.006 ms each |
| Retrieve all objectives | 0.024 ms |
| RSS | 80.2 MB |

The ticker is a single `asyncio` task; no model call is made when nothing is due.
No long-running soak was performed, so sustained resource behaviour is unverified.

## 7. Safety, security, reliability

- Sensitive work requires a recorded per-task approval at **every** executing
  autonomy level; `execute` is a ceiling, not a grant.
- `PermissionManager` is consulted per task and can only tighten the decision.
- Unverified executor outcomes are FAILED; completion cannot be faked by status.
- Duplicate completion, dependency cycles and duplicate recurrence are refused.
- Shutdown persists before cancelling and awaiting the ticker.
- Notifications are derived from stored state; no synthetic activity.
- No external network or paid model was used in verification.

## 7.1 Defects found and fixed during verification

| Defect | Fix | Regression evidence |
|---|---|---|
| A resumed objective stopped after one task because `run_objective` read the ready list once | The loop re-reads eligibility after each completion, bounded by `max_tasks_per_tick` | Scenario A: two dependent tasks completed in one resume |
| Recurrence was scheduled from the wall clock, not the controlled tick | `run_task` threads its `clock` through complete/record/fail/wait | Scenario B: successor exactly at +1 h |
| A rejected task auto-cancelled its whole objective | `refresh()` no longer derives `CANCELLED` from one rejected task | Scenario E: one completed + one rejected task, objective stays active |
| A stored `RUNNING` task was stranded after a restart; an ACTIVE objective never ran it again | `OperationsManager.from_dict` recovers `RUNNING` → `READY` + `resume_required`, and `resume_objective` reports those tasks for review | `InterruptedRunRecoveryTests`; Scenario D |
| Approve/reject could revive cancelled, completed or queued non-sensitive tasks; block/wait/fail could rewrite closed tasks | Terminal-state guards on `approve_task`, `reject_task`, `block_task`, `wait_for_approval`, `fail_task`, `cancel_task`; rejection still allowed for a queued sensitive task | `TransitionGuardTests` |
| A missing permission layer parked the task forever, so approving could never run it | `run_task` blocks with `"no permission layer"` instead of asking for approval; an already-approved task the layer still refuses is blocked with the real reason | `PermissionWiringTests`; `test_no_permission_layer_*` |
| An approved desktop objective dead-ended on its own token-bearing plan | `_run_approved_operation_plan` consumes the plan token through the public executor only when the task is approved; per-action `completed` is required | Scenario G |
| Monitoring claimed every ready task was "authorized" even for observe/recommend objectives | The `actionable` detail now states that a person must start it | `MonitorTruthfulnessTests` |
| The architecture doc said stalled detection used 30 minutes; code uses 24 hours | Doc corrected to match the code | inspection |

## 8. Tests, types and documentation checks

| Check | Command | Result |
|---|---|---|
| Phase 30 | `PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python -m pytest tests/test_phase30_operations.py -q` | **43 passed** |
| Phase + web platform contract | `... pytest tests/test_phase30_operations.py tests/test_web_platform.py -q` | **89 passed, 462 subtests passed** |
| API/web regression | `... pytest tests/test_phase30_operations.py tests/test_web_platform.py tests/test_api.py tests/test_web_api.py -q` | **169 passed, 537 subtests passed** (only FastAPI/Starlette deprecation warnings) |
| Type check | `python -m mypy src/novacontrol/operations ...` | New package clean; only the pre-existing Phase 29 `VisionProvider.see(..., structured=True)` error at `application.py:5616` |
| API reference | `python scripts/generate_api_reference.py --check` | in sync, 419 routes |
| JS syntax | `node --check src/novacontrol/web/static/js/render-operations.js` | clean |
| Whitespace | `git diff --check` | clean |

A full repository-suite run was deliberately not performed, per the testing-scope
policy. Files not exercised by the above are not claimed as regression-covered.

## 9. Known limitations and unverified items

1. **OS-level restart** was not performed; application restart on the same durable
   store was. The on-disk design implies OS restart safety, but it is not claimed.
2. **Multi-hour soak** was not run; background resource behaviour over hours is
   unverified.
3. **Live external-application objective**: now verified by Scenario G, which
   opened Calculator through the approved plan path. The script left Calculator
   open; no other external application was driven by Phase 30.
4. **Phase 28 live agents**: delegation is dispatched by agent label and failure/
   recovery is verified with a controlled dispatcher; live research/developer
   pipelines were not invoked from an objective.
5. **Phase 27 learning** is evidence/advisory only; it does not automatically alter
   planning or scheduling.
6. **Schedule conflict resolution** is limited to due-time gating; there is no
   cross-objective resource allocator.
7. Only Windows 11 was exercised; other OSes are untested for this phase.
8. The pre-existing Phase 29 mypy error remains (not introduced here).

## 10. Files

**New:** `src/novacontrol/operations/{__init__,models,manager,monitor,service,learning}.py`;
`tests/test_phase30_operations.py`; `src/novacontrol/web/static/js/render-operations.js`;
`docs/PERSISTENT_OPERATIONS.md`; `docs/PHASE30_REPORT.md`;
`tmp/phase30/{verify_phase30.py,bench.py}` and their JSON evidence.

**Modified:** `src/novacontrol/application.py`, `src/novacontrol/core/events.py`,
`src/novacontrol/api/app.py`, `src/novacontrol/api/models.py`,
`src/novacontrol/api/route_consumers.py`, `src/novacontrol/web/static/index.html`,
`src/novacontrol/web/static/app.js`, `docs/API.md`, `docs/STATUS.md`,
`docs/DEVELOPMENT_LOG.md`, `README.md`, `AGENTS.md`.

## 11. Gate verdicts

| Gate | Verdict |
|---|---|
| 1 — Functional completeness | PARTIAL (OS restart, multi-hour soak, automatic learning use) |
| 2 — Integration completeness | PARTIAL (Phase 27 advisory, Phase 28 controlled dispatch; Phase 29 approved desktop execution is live-verified) |
| 3 — Testing completeness | PASS for the executed scope; unverified scenarios are listed above |
| 4 — Safety and reliability | PASS (approval, persistence, duplicate prevention, cancellation) |
| 5 — Documentation and evidence | PASS |

**Overall: PARTIAL. The phase remains open; it must not be presented as complete.**

## 12. Group 1 audit addendum (2026-11-26)

The application-restart gap in limitation 1 is narrowed. `tmp/group1/verify_resilience.py`
scenario 3 writes an objective and its task in a child process that then calls
`os._exit(9)` — no clean shutdown and no explicit flush — and reopens the store
in a fresh application instance. The objective **and its task** were restored
(4/4 checks passed; `tmp/group1/resilience_evidence.json`).

An actual operating-system reboot was **not** performed: rebooting the host is
outside the scope of a code audit, and a process exit is not an OS restart.
Matrix rows 3 and 15 therefore stay PARTIAL, and the multi-hour soak in
limitation 2 remains unverified.

Full record: `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` defect D5.
