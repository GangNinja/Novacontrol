# Scheduled Automation and the Audit Trail (Phase 13)

NovaControl can run an approved request **later**, and **repeatedly**, and can
say afterwards what it did, under whose authority, and how it ended. Two
packages implement that, and they are deliberately narrow:

- `novacontrol.automation` owns **WHEN** a stored request runs.
- `novacontrol.audit` owns **WHAT HAPPENED**, as an operational record.

Neither one performs an action. The engine hands the request text to a runner
it is given, and in this application that runner is `handle_request` — so a
scheduled task travels **intent → decision → plan → tool selection → permission
→ execution → verification → recovery** exactly as a spoken request does. There
is no second execution path, and no way for the scheduler to act on its own.

## One pipeline, not a second one

The central promise of the phase is mechanical rather than aspirational: a
scheduled task is not carried out by the scheduler. `AutomationEngine` stores a
request and a schedule, and when the time comes it calls the injected
`AutomationRunner`, which is `NovaControlApplication._run_automation_task`:

1. it sets a context variable naming the automation, so every event and every
   audit row the request produces is filed under that automation;
2. it calls `handle_request(text)` — the ordinary path, with the ordinary
   approval gates;
3. it reads the request's own state (`executed`, `verified`, per-step statuses)
   rather than assuming that a returned call means success.

A loop guard closes the obvious hole: a stored request that still *states* a
schedule ("in 5 minutes push my project") would create another automation every
time it ran, so such a run is reported SKIPPED with that reason instead of
being executed. The scheduler never asks "what can I do"; it asks "what was I
told to run".

## A schedule is data, and it is computed rather than guessed

`Schedule` is a frozen value — a `ScheduleKind` (`once`, `interval`, `daily`,
`weekly`) plus only the fields that kind uses. The constructor refuses the
combinations that would make the next run ambiguous, so a schedule cannot be
half-stated.

`parse_schedule(text)` reads the schedule the *wording* states, and returns
`None` when the wording states none. That is the answer to the phase's
sensitivity requirement: a request that merely mentions a schedule
("when should I run my backup?") parses to nothing, so nothing is scheduled by
accident, and creation still travels the whole pipeline. A parser that invented
a default time would be deciding for the user at the wrong layer.

`Schedule.next_after(now)` always returns a **future** occurrence. A machine
that was asleep over a daily job's hour does not get a burst of catch-up runs
when it wakes: a recurring task's next run is the next occurrence after `now`,
and a finished one-time task returns `None`, which is the *only* thing `None`
means. Intervals compress missed occurrences into one for the same reason — a
backlog of stale runs is a stampede wearing a schedule's clothes.

`strip_schedule(text)` removes the scheduling phrase, because the request that
is stored is the request that will be **run**, and running it goes through the
same parser. `"Remind me at 6 PM to push my project."` stores
`"push my project"`; a stored copy that still said "at 6 PM" would be read as a
request to schedule something on every run.

## Nothing runs without a verdict

Every run is put through the centralized permission layer first, under the
scope `automation.run` — declared HIGH and confirmation-requiring by the
application, beside the code that carries runs out. Three rules follow:

- **only approved tasks are armed.** Creating a task that needs approval stores
  it `PENDING_APPROVAL` and **not** enabled. `approve()` is the only way it
  becomes armed, and `enable()` a way to re-arm — not a way to approve: it
  refuses a task that was never approved with a `PermissionError`.
- **a run is checked at the moment it runs, not only when it was armed.**
  `authorize()` asks the permission layer for a verdict on THIS task now, so
  stored state a person edited is still gated.
- **"no gate" is not permission.** With no permission layer wired the engine
  refuses (`denied`, with that reason) rather than running work it cannot show
  was allowed.

A denial and an unmet condition are their own run outcomes — `denied` and
`skipped` — and **neither counts as a failure**: neither is the work's fault,
and counting either would disable a healthy automation for something it did not
cause. A denial also disarms the task rather than retrying into the same
refusal every tick; a refusal of a task that *was* approved leaves it DISABLED
for an operator to look at, while a refusal of an unapproved one leaves it
`PENDING_APPROVAL` — a state a person can act on.

Failures are counted, and bounded: a failed run increments the task's failure
count, and at the configured limit (default 3) the task disables itself, so a
broken automation stops announcing itself instead of failing every hour
forever. A runner that raises is a recorded failure, not a crashed loop.

## What is tracked

`AutomationTask` carries every field the phase asks for, all readable from
`to_dict()`: `automation_id`, the task definition (`request`), the `schedule`,
`status`, `next_run_at`, `last_run_at`, `failure_count`, `enabled`,
`permissions`, `created_at`, plus `run_count`, `approved`/`approved_by`, the
`condition`, a bounded `runs` history and the `last_outcome`.

Status is a state rather than a flag: `pending_approval`, `scheduled`,
`running`, `completed`, `failed`, `cancelled`, `disabled`. A task that was
created but never authorized is neither scheduled nor cancelled, and "why
didn't it run?" has an answer in the record. State survives a restart — tasks
are persisted with their failure counts and run history.

## Conditions are named checks, never code

`AutomationCondition` is a named `ConditionKind` (`always`, `path_exists`,
`tests_pass`) with an argument, evaluated by an injected callable. An
automation that stored executable text would be a second, unreviewed execution
path beside the one the permission layer gates, so none exists. `always` is the
default, so a task without a condition is unconditional rather than unset, and
`tests_pass` is deliberately strict: with no recorded test run there is no
pass, so a conditional task waits for evidence instead of firing on an
assumption.

## The audit trail

`novacontrol.audit` is distinct from `novacontrol.core.audit`, which records one
executed *controller action* (a click, a navigation, a message) as it happens.
This package records one **user request or one scheduled run, end to end**:
task id, timestamp, request, automation id and source, route, intent, the
decision, the plan joined to what each step ended up as, tools, actions,
verification (what was checked *and* what was not), failures, recovery, model,
provider, latency, resource usage and the permission decisions.

It records **no chain-of-thought**: there is no prompt, no reasoning, no model
output, and a test pins their absence. That is a design decision rather than an
omission — an audit trail is kept for a long time and read by more people than
a debug log, so a free-text field of a model's reasoning would be the largest
privacy surface in the system and the least useful thing in the file. The named
fields are what "what did this system do, under whose authority, and how did it
end" is actually answered from.

Recording is **best-effort by construction**: `_record_audit` swallows every
exception, because an audit trail that can fail a request is a new way for the
request to fail. It reads only what the request already produced — the decision
that was made, the plan that was built, the state the executor published, the
telemetry row that was just closed — and invents nothing.

## Privacy: redacted on the way in, local, bounded

Three properties, each a property of the code path rather than a promise:

- **local only.** The durable trail is a JSONL file inside this application's
  data directory. `AuditLogger.attach_sink` *refuses* a sink that declares
  itself remote, so "the audit stays here" cannot be undone by configuration.
- **redacted on the way in.** `Redactor` filters the request text and the
  structured metadata *before* the record is built, so a secret is never
  written and then cleaned up. It recognises a private-key block, an
  `Authorization` header, a bearer token, a JWT, an `sk-`/`ghp_`/`xox`/`AKIA`
  credential, credentials embedded in a URL, and `key = value` assignments —
  and a mapping key that names a secret (`password`, `token`, `api_key`, …)
  hides its value whatever the value looks like. Prose is **not** eaten: "the
  token expired" stays, because a redactor that eats prose is one people turn
  off. Each redaction is counted and its kind recorded, so "this request
  carried a credential" is visible without the credential being visible.
- **bounded by policy.** A retention period and a hard record cap (both
  operator settings, both clamped on the way in from a hand-editable file) are
  applied by `prune()`, with explicit `delete`/`clear` for the case where a
  record is wrong and should not wait for an expiry. A record with a timestamp
  this build cannot read is **kept**, because an unreadable date is not
  evidence that a record is old.

## How the rest of the build reaches it

- **Application:** `schedule_automation`, `approve_automation`,
  `cancel_automation`, `enable_automation`, `disable_automation`,
  `run_automation`, `run_due_automations`, `automation_status`,
  `automation_tasks`, `automation_enabled`, plus `audit_status`,
  `audit_entries`, `audit_prune`, `audit_delete`, `audit_clear` and
  `apply_audit_settings`.
- **Plan actions:** a scheduling request compiles to a `schedule_task` step
  (a LOCAL_WRITE), and "run my backup" compiles to a `run_automation` step.
  The step stores the task **disarmed** — pending approval — unless the caller
  approved that step, so a spoken mention produces work that waits rather than
  work that runs. Scheduling is a plan action like any other.
- **Events:** `automation.created`/`updated`/`cancelled`/`started`/`completed`/
  `failed`/`denied`/`skipped`, plus `audit.recorded` and `audit.pruned`, with
  their required payload fields declared in `core/events.py`.
- **HTTP:** `GET /automation`, `POST /automation/{schedule,approve,cancel,enable,disable,run,run-due}`,
  `GET /audit`, `GET /audit/entries`, `POST /audit/{prune,delete,clear}`. The
  operator settings these surfaces read — `automation_enabled` and the three
  audit retention fields — are accepted by `POST /settings`, which then
  re-applies the live retention policy through `apply_audit_settings`, so a
  change takes effect without a restart.
- **Background ticker:** started by `start()`, stopped by `stop()`, sleeping
  `config.automation.tick_seconds` (default 15, clamped) and ticking only after
  that sleep — a restart is never a burst. One broken automation cannot kill
  the loop.

## What this does not do

- No cron syntax, no distributed or multi-machine scheduling, no per-second
  resolution below the one-second floor.
- Conditions are the three named checks; there is nothing that runs a script to
  decide.
- A scheduled task still needs approval, and its inner actions still pass the
  approval gateway at run time — an automation is not a standing authorization
  for whatever the request will later be read to mean.
- Audit retention is bounded by a period and a cap; there is no archival to
  another machine, by design.

## Tests

`tests/test_automation.py` (50 tests) covers the phase's areas for scheduling:
creation, execution, cancellation, recurrence, failure handling, permission
enforcement and persistence — driven either by the engine directly, by the plan
compiler that turns a scheduling request into a step, or through the
application's own surfaces. `tests/test_audit.py` (25 tests) covers record
creation, redaction (text and structured), retention/pruning, deletion and
local-only enforcement. `tests/test_web_api.py::AutomationAuditApiTests` pins
the HTTP surface, including that scheduling stores a pending task and **runs
nothing**.
