# Phase 34 — Trustworthy Self-Management: Acceptance Report

**Date:** 2026-11-24  
**Branch / HEAD:** `main` / `afa5ff6` (worktree changes uncommitted)  
**Verdict:** **PASS** (after an acceptance audit that repaired the defects listed in §2.10)  
**Scope:** Phase 34 only. No Phase 35 work, no commit, no push, no full-repository suite.

---

## 1. Objective

Make NovaControl able to observe its own operational health, detect and diagnose
failures with honest confidence, track incidents, perform only authorized and
bounded recovery, maintain its own temporary state safely, validate and roll
back configuration, expose autonomy and approval controls, and prove all of it
against the real application — without becoming an unrestricted
self-modification system.

The implementation deliberately builds on the existing Phase 14 diagnostic
roster, `PermissionManager`, `AuditLogger`, `JsonStateStore`, Phase 30
operations/notifications, Phase 27 continual learning, Phase 32 personal memory
and Phase 33 model router. It adds **one control-plane coordinator**, not a
second health registry, recovery engine, scheduler or configuration store.

---

## 2. Implemented Capabilities

### 2.1 Unified health from live observations

`novacontrol.self_management.HealthCollector` runs the application's existing
`DiagnosticManager` roster. Phase 34 registers nine operational components in
addition to the existing rows, for a live roster of **34 components**:

| Added component | Real observation |
|---|---|
| Event bus | Publish/subscribe round trip with a unique token and timeout |
| API server | Whether this process actually started the HTTP surface, and its declared route count |
| Desktop interface | The real `index.html` and JavaScript asset directory |
| Agent execution | The live agent registry |
| Memory services | The live personal-memory index/load state |
| Background workers | The automation and operations ticker tasks |
| Scheduled workflows | The scheduler's stored-task and loop state |
| Computer operation | The real computer supervisor |
| Hardware manager | The resource governor's measured CPU/RAM reading |

The existing hardware, storage, model-provider, training, safety and subsystem
rows remain the source of truth. Optional GPU/NPU readings that a CPU-only host
cannot produce stay `UNKNOWN` and do not create incidents or make the whole
system `UNKNOWN`. A component is never reported healthy merely because a
process exists: the event bus performs a round trip, the API reports its own
startup flag, the UI checks its files, workers inspect their tasks, and the
provider performs a live status probe.

Health metrics include free/critical RAM, CPU percent and ceiling, queue depth,
background task count and process RSS. Measured probe durations are carried into
diagnoses and incident evidence.

### 2.2 Evidence-labelled diagnosis

Deterministic rules map observed component failures, measured resource pressure,
provider reachability and configuration validity into findings labelled
`confirmed`, `probable`, `possible` or `unknown`. Every finding carries the
component, measured evidence, rationale, severity, impact, recommendations and
a stable fingerprint. Optional hardware absence is reported without an incident;
a real failing probe is not softened into "possible".

Recent failure events are correlated into evidence when the event bus history
shows repeated failures for the affected component. No diagnosis is derived by
executing commands found in logs.

### 2.3 Durable incidents

`IncidentManager` persists incidents through the existing `JsonStateStore` and
supports the required states: `detected`, `investigating`, `diagnosed`,
`recovery_pending`, `recovering`, `resolved`, `escalated`, `unresolved`.
Fingerprint deduplication updates evidence rather than creating duplicate noise;
a recurrence after resolution becomes a new related record with a recurrence
counter. Evidence is redacted before storage, lifecycle transitions are checked,
and history survives process restarts.

### 2.4 Bounded, allowlisted recovery

Only registered actions can run:

| Action | Components | Mode | Approval | Verification |
|---|---|---|---|---|
| `restart-scheduler` | Scheduler | Automatic-safe | No | Live Scheduler probe |
| `restart-background-workers` | Background workers | Automatic-safe | No | Live workers probe |
| `reconnect-provider` | Ollama | Automatic-safe | No | Live provider probe |
| `reconnect-model-manager` | Model manager | Automatic-safe | No | Live model-manager probe |
| `reload-memory` | Memory services, Context | Controlled | Yes | Live memory probe |
| `reload-context` | Memory services, Context | Controlled | Yes | Live memory probe |
| `restore-known-good-config` | Configuration | Controlled | Yes | Configuration validation |

Every attempt checks the permission layer, preserves state, executes within a
timeout, records the attempt, verifies with a live probe, and audits the result.
Retries are limited by `max_attempts`, a per-action cooldown and a rolling window
budget. Failed verification never reports success; repeated failures escalate
instead of looping. Refusals (missing permission, pause, approval, cooldown,
budget) are audited. Automatic mode only considers actions declared
automatic-safe; it cannot run an approval-required action. There is no shell
command or arbitrary code path in the action registry.

### 2.5 Maintenance with explicit boundaries

`MaintenanceService` previews impact and only touches the application's own
`data_dir/tmp`, `data_dir/cache` and `data_dir/logs` roots. It covers temporary
files, stale cache, log rotation, SQLite integrity/optimization, stale processes,
resource-leak observation, storage free-space and configuration validation.
Dependency and background-service **health** is covered by the live health
roster; maintenance does not duplicate that surface. Source files, user
documents, credentials, settings, audit history, databases and known-good
snapshots are protected by path, name and suffix rules. Destructive items require
an explicit approval flag; the preview shows what would change. A configuration
item is reported as `manual_action_required` and routed to the real versioned
restore control, so maintenance cannot claim it restored settings. Stale-process
detection matches a real NovaControl runtime rather than any path containing the
project name, and excludes the live process's parent chain. SQLite handles are
closed after maintenance on Windows.

### 2.6 Configuration integrity and rollback

The persisted settings body is validated, checksummed and versioned through the
existing settings seams. A boot snapshot is recorded. Invalid settings are
detected from validation results and from a corrupted persisted body; tampered
snapshots are not treated as known good. Restore applies only a validated
version through the application's real `_apply_self_management_configuration`
seam, writes a new version, and preserves the invalid version in history. The
operator is told what was restored and a fresh health pass is returned.

Boot/file/environment configuration remains restart-bound; snapshots cover the
runtime settings surface, not arbitrary `.env` or source configuration.

### 2.7 Autonomy and approvals

Autonomy levels map onto the Phase 30 model:

- **Observe** — health and incident reports only.
- **Recommend** — explains possible actions and impact, executes none.
- **Controlled** (default) — executes only explicitly approved actions.
- **Authorized automatic** — executes only registered safe automatic actions
  inside cooldown and window budgets.

Pause/resume is global and persisted in the state store (`self_management_policy`)
together with the autonomy mode and window budget, so a restart cannot silently
re-enable automatic recovery. Pause blocks recovery, maintenance and
configuration restore. Manual approval is explicit in the request and recorded in
the audit trail.

### 2.8 Self-modification boundaries

The control plane has no path to rewrite source, edit permission/security
policy, disable safety, install dependencies, run arbitrary commands or deploy.
Its recovery registry is fixed at composition time. Source-code changes,
dependency changes, destructive migrations and deployment remain governed by the
existing permission/approval architecture. `reinitialize-core`,
`reinitialize-event-bus` and `reinitialize-storage` are recommendation-only:
there is no genuine registered procedure, so they cannot be executed.

### 2.9 Integration

| Phase | Integration | Evidence |
|---|---|---|
| 27 | Verified/failed recovery outcomes are recorded as `self_management_recovery` evidence; advice is advisory and never auto-promotes an action | `test_real_scheduler_recovery_and_phase27_learning`; live scheduler recovery |
| 28 | An explicitly requested debug specialist supplies a second opinion with no authority through its real `handle_task` interface; deterministic diagnosis remains the finding and recovery stays in the central permission system | `test_real_advisory_agent_uses_the_public_task_interface`; `test_memory_reload_and_agent_advice_are_real_and_non_authoritative` |
| 30 | Reuses autonomy settings, notification delivery/dedupe and verified objective/task checkpoints rather than duplicating them | Live A/B/E/G; application tests |
| 32 | Memory reload re-reads the existing personal-memory index without deleting records | `test_memory_reload_and_agent_advice_are_real_and_non_authoritative` |
| 33 | Provider/model health is read from the existing manager/router; reconnect uses the existing runtime | Live A; `test_provider_outage_is_diagnosed_and_reconnect_is_verified` |

Monitoring and diagnosis are deterministic and do not require an external model.
Model/provider failure degrades one component but cannot stop health checks or
the other recovery actions.

### 2.10 Acceptance-audit repairs

The verification pass found real defects in the first implementation and fixed
them with regression coverage:

1. **Escalated incidents were reopened every monitor pass.** Fingerprint lookup
   now matches every non-resolved state, including `ESCALATED`, so an incident
   that needs an operator is updated, not duplicated.
2. **Self-healed incidents ran unnecessary recovery.** A later healthy pass
   resolves an open, never-attempted incident directly, while skipping
   `ESCALATED` and `RECOVERING`.
3. **Recovery could act on a closed/escalated incident.** `RecoveryManager` now
   refuses resolved and escalated records.
4. **Policy changes were not audited.** Pause, resume and autonomy/window policy
   changes now write audit records.
5. **Maintenance ignored pause.** Maintenance run refuses while paused and audits
   the denial.
6. **Configuration restore ignored autonomy and pause.** It refuses in `OBSERVE`
   and `RECOMMEND`, refuses while paused, and audits denial, failure and success.
7. **Snapshotting was unaudited.** Configuration snapshots now write an audit
   record with version, known-good state and checksum.
8. **The UI discarded action results.** Investigate, recommend, recover,
   maintenance preview/run, snapshot and restore handlers now return their real
   result, and `render-panels.js` renders each shape instead of only refreshing
   status. The action selector auto-selects the first available recommendation.
9. **Phase 28 callback used the wrong interface.** The optional specialist is
   invoked through the real `handle_task(AgentTask(...))` protocol, not a
   non-existent `run_task` method; the new test registers a real fake agent and
   exercises that path.
10. **Maintenance configuration check was silently disabled.**
    `MaintenanceService(config_report=configuration.report)` passed a
    `ConfigReport` where a mapping was expected and the exception was swallowed;
    it now passes `lambda: configuration.report().to_dict()`.
11. **Active incidents omitted escalated records in the dashboard.** Status now
    carries `active_incidents` (open + escalated) and the UI uses it, while
    `pending_approvals` does not offer recovery for an escalated incident.
12. **A healthy generic row could close a live configuration incident.**
    `dismiss_healthy` now accepts the current diagnosis components and never
    closes a component that still has a diagnosis in the same pass.
13. **Pause and autonomy were process-local.** Mode, pause and window budget now
    persist in `self_management_policy` and reload before monitoring starts.
14. **Stale-process detection produced false positives.** It matched any command
    containing `NovaControl` (including other projects under the same folder and
    the server's parent shell). It now requires a real `python -m novacontrol` or
    packaged entrypoint and excludes the live process's ancestors.
15. **Maintenance implied configuration restore.** A configuration maintenance
    item now returns `manual_action_required` and points at the real restore
    control; the run reports zero changed files.
16. **The monitor interval was fixed.** It is configurable with
    `NOVACONTROL_SELF_MANAGEMENT_INTERVAL`, clamped to 5–3600 seconds.
17. **Type-level repairs.** The Phase 28 task wrapper, risk-level alias and
    recovery verifier callbacks are typed; targeted mypy is clean for the
    package. The only remaining application error is the pre-existing
    `VisionProvider.see(..., structured=True)` issue outside Phase 34.
18. **A degraded hardware reading was worded as "unsafe" and its cause was the
    generic title.** A tight/comfort-line warning is now titled "tight machine
    reading" (unsafe is reserved for a failing probe), row diagnoses carry the
    measured probe message as their cause instead of replacing it with the
    title, and an open incident follows the current diagnosis title. Probe
    latency moved to metadata instead of being appended as a new unique
    `probe duration` evidence row on every monitoring pass.

---

## 3. API and Dashboard

### API

Seventeen authenticated routes under `/self-management`:

`GET /self-management`, `POST /health`, `POST /diagnose`,
`GET /incidents`, `GET /incidents/{id}`,
`POST /incidents/{id}/investigate`, `POST /incidents/{id}/recommend`,
`POST /incidents/{id}/recover`, `POST /pause`, `POST /resume`,
`POST /policy`, `GET /maintenance`, `POST /maintenance/preview`,
`POST /maintenance/run`, `GET /configuration`,
`POST /configuration/snapshot`, `POST /configuration/restore`.

All require the existing API authentication dependency. Mutations use JSON
bodies with typed validation; unauthorized recovery is a real 403/denied
decision, not a UI-only warning. `docs/API.md` is regenerated and in sync at
**471 routes**.

### Dashboard

The existing interface has a **Self-Management** navigation entry and panel. It
exposes overall/component health, measured metrics, active incidents, diagnosis
confidence and evidence, autonomy mode, window budget, approval-gated actions,
maintenance status, configuration integrity and version history. Controls are
wired to the real routes: Refresh, Run Health Check, View Incident History,
Investigate Incident, Recommend Recovery, Approve & Recover, Pause/Resume,
Apply Autonomy Level, Preview/Run Maintenance, Snapshot Configuration and
Restore Known-Good Configuration.

Manual browser verification on the running application confirmed:

- the page and all 18 JavaScript modules load with HTTP 200;
- the panel renders real DEGRADED health with 34 components and measured values;
- Run Health Check calls `POST /self-management/health` and updates the panel;
- View Incident History renders the stored incident record;
- Pause and Resume call their routes and the persisted `recovery.paused` value
  changed `true` then `false`;
- no current-page JavaScript errors were present (the two console errors in the
  log were connection resets from deliberately restarting the local test server).

A later audit session rechecked the panel after the result-renderer changes: the
live maintenance preview rendered its real report (`0 item(s), 0 changed` after
the stale-process false positives were removed), and the recommendation,
investigation, recovery-verification and configuration-snapshot renderers were
exercised in the running page with real response shapes. The `Pending approvals`
heading was renamed `Approval-gated actions` because the system approves
synchronously and has no queued approval inbox.

The first session exposed and fixed one real UI defect: `refreshSelfManagement`
passed the render type and data in the wrong order, so the panel rendered the
string `selfManagement` instead of the health object. It was corrected and the
health panel was rechecked live. Metric labels/formatting were then improved
(bytes → GB, percent → `%`); the final formatted panel was captured and visually
inspected in `tmp/phase34/dashboard-final.png` (readable 34-component health,
metrics, autonomy mode and incidents; no layout breakage).

---

## 4. Real Application Verification (A–H)

Live script: `tmp/phase34/live_verify.py`, against real `NovaControlApplication`
instances with isolated temporary data. Evidence: `tmp/phase34/evidence.json`;
console record: `tmp/phase34/live_output_audit.txt` (latest run) and
`tmp/phase34/live_output.txt` (first run). Final run: **8/8 scenarios, exit 0,
total 3.365 s**.

| Scenario | What was done | Observed result |
|---|---|---|
| **A — Provider failure** | Made the model runtime report unreachable in a controlled test | Ollama component `degraded`; one `probable` incident; `reconnect-provider` recovered and was verified healthy; incident `resolved`. Detection **42.29 ms**, recovery **9.27 ms**, 4 provider probes |
| **B — Component failure** | Stopped the real scheduler loop | Scheduler incident `diagnosed`; `restart-scheduler` started the existing ticker and verified it running; incident `resolved`. Detection **125.06 ms**, recovery **6.81 ms** |
| **C — Resource exhaustion** | Applied a controlled tight resource reading | Resource diagnosis `confirmed`, overall `degraded`, application still answered `/status` in **62.69 ms**; health **36.62 ms** |
| **D — Invalid configuration** | Wrote an invalid `brain_mode` through the real settings store | Validation identified the exact issue; approved restore from version 1 wrote version 2, kept history and validated afterwards. Restore **22.21 ms** |
| **E — Interrupted workflow** | Started a persistent task with a verified step, restarted the application, then resumed | After restart state `ready`, `resume_required`, checkpoint `["first"]` preserved; after resume checkpoint still `["first"]`, no duplicate step, task listed for reassessment |
| **F — Maintenance** | Ran preview and approved maintenance on controlled temp/cache data with a protected file present | 2 preview items, protected item reported; stale file removed, protected file preserved, no errors, app responsive. Maintenance **148.32 ms** |
| **G — Unauthorized action** | Attempted an approval-required recovery without approval | Blocked in **3.11 ms**; incident stayed `diagnosed`; three audit records, last outcome `denied` |
| **H — Self-diagnosis** | Introduced a controlled storage-probe defect | Storage identified as the affected component with evidence and rule `storage_unavailable`, confidence **confirmed**, severity `error`; diagnosis **40.59 ms** |

No destructive failure was introduced into the user's normal data. The server
used for the browser check was started in a separate `tmp/phase34/run` working
directory with its own `data` state.

---

## 5. Performance and Reliability

Measured on this Windows machine, Python 3.13, CPU-only diagnostic path:

| Measurement | Result |
|---|---|
| Full 34-component health pass | 29.722 ms in the latest live dashboard check (29.7–48.5 ms across samples); 36.62 ms in scenario C |
| Provider failure detection | 42.29 ms |
| Provider reconnect + verification | 9.27 ms |
| Scheduler failure detection | 125.06 ms |
| Scheduler restart + verification | 6.81 ms |
| `/status` under controlled resource pressure | 62.69 ms (135,366-byte response) |
| Configuration restore | 22.21 ms |
| Maintenance preview + run | 148.32 ms |
| Self-diagnosis | 40.59 ms |
| Unauthorized refusal | 3.11 ms |
| Whole live A–H run | 3.365 s |

The monitor ticker defaults to 30 seconds (configurable 5–3600 s through
`NOVACONTROL_SELF_MANAGEMENT_INTERVAL`) and sleeps before its first pass, so a
3.7-second scenario correctly records `monitor_cycles: 0`; the health checks in
the scenarios are explicit calls. No runaway recovery loops occurred; attempts,
cooldowns and budgets are unit-tested. A real application test now drives
authorized automatic recovery end-to-end (`test_authorized_automatic_mode_recovers_a_safe_action_for_real`),
including the live ticker restart and audited recovery. Recovery and health run
in bounded threads/tasks with timeouts. SQLite maintenance closes handles on
Windows, so temporary databases remain removable.

---

## 6. Test Results

### Phase 34 tests

```bash
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_phase34_self_management.py -q
```

**45 passed, 13 warnings in 11.96 s** (exit 0). Warnings are FastAPI
`on_event`/Starlette TestClient deprecations, not failures. Coverage includes
live component probes, optional-unknown handling, probe timeout, confidence
labels, configuration findings, incident dedupe/persistence/recurrence/
transitions/redaction/escalation/self-healing/current-diagnosis closure,
recovery success/failed verification/timeout/retry ceiling/cooldown/approval/
missing permissions/pause/window budget/refusals/automatic safety, real
application automatic recovery, policy persistence across restart, maintenance
protection/SQLite integrity/stale-process filtering/configuration routing,
configuration validation/tamper/corruption/restore, interrupted-workflow
checkpoint restart, real scheduler recovery + Phase 27 learning, provider outage
+ reconnect, application configuration restore, memory reload + real Phase 28
`handle_task` advice and authority boundary, application status/active incidents,
and API routes/auth/approval/pause/resume.

### Modified-module and API regressions

```bash
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python -m pytest \
  tests/test_web_platform.py tests/test_web_api.py \
  tests/test_route_shape_contract.py tests/test_api_reference_sync.py \
  tests/test_api.py -q
```

**152 passed, 1,150 subtests passed** in 158.36 s (exit 0).

The diagnostics-roster assertion in `tests/test_web_api.py` and
`tests/test_optimization.py` was updated from **25 to 34** because Phase 34
intentionally adds nine live operational components; the new assertions also
require `Event bus`, `Background workers`, `Scheduled workflows` and `Memory
services`. This is a deliberate contract update, not a weakened assertion.

### Neighbouring-phase regressions

```bash
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python -m pytest \
  tests/test_application.py tests/test_phase30_operations.py \
  tests/test_continual_learning.py tests/test_multi_agent_collaboration.py \
  tests/test_phase33_adaptive_models.py -q
```

**228 passed, 15 subtests passed** in 119.92 s (exit 0).

### Static checks

```bash
PYTHONPATH=src python -m mypy src/novacontrol/self_management \
  src/novacontrol/application.py
```

The Phase 34 package is clean (**9 source files**). The same check on
`application.py` reports only the pre-existing
`VisionProvider.see(..., structured=True)` error at line 6110, outside Phase 34;
all Phase 34 errors in that file were fixed. This is a targeted check, not a
claim that the whole repository is mypy-clean.

```bash
node --check src/novacontrol/web/static/app.js
node --check src/novacontrol/web/static/js/render-panels.js
node --check src/novacontrol/web/static/js/render-utils.js
PYTHONPATH=src python scripts/generate_api_reference.py --check
git diff --check
```

All exit 0; `docs/API.md: in sync`.

The full repository suite was **not** run, per the mandatory testing-scope
policy.

---

## 7. Acceptance Matrix

| ID | Requirement | Implementation evidence | Verification evidence | Verdict |
|---|---|---|---|---|
| 34.1 | System health monitored from genuine runtime evidence | `HealthCollector` + 34-row live roster in `application.py` | Health tests; live health 34 components; dashboard check | PASS |
| 34.2 | Component failures detected | Probe failures → diagnoses/incidents | Scenario B scheduler; H storage; health tests | PASS |
| 34.3 | Evidence-backed diagnostics | Findings carry evidence, rationale, impact, rules | Diagnosis tests; A/B/H evidence | PASS |
| 34.4 | Root-cause confidence represented accurately | `confirmed`/`probable`/`possible`/`unknown` rules | `test_confidence_labels_follow_the_evidence`; A/B/H | PASS |
| 34.5 | Authorized automatic recovery works | Allowlisted actions, permission checks, approval, live verification | Recovery tests; A/B live; real application automatic-mode test restarts the ticker and audits the result | PASS |
| 34.6 | Failed recovery safely escalated | Attempt ceiling, cooldown, no-success-without-verification, escalation | `test_retry_ceiling_escalates_instead_of_looping`, failed verification test | PASS |
| 34.7 | Preventive maintenance within boundaries | `MaintenanceService` roots, protected rules, preview, approval | Maintenance tests; live F protected-file preservation | PASS |
| 34.8 | Configuration integrity and rollback | Validation, checksums, versions, known-good restore, corruption detection | Config tests; live D; application restore test | PASS |
| 34.9 | Self-modification restrictions enforced | No source/dependency/permission/deploy action path; existing approval boundary preserved | Registry inspection; unauthorized live G; static tests | PASS |
| 34.10 | Incidents tracked and persisted | Durable `IncidentManager`, lifecycle, recurrence, correlation, redaction | Incident tests; restart persistence; API history | PASS |
| 34.11 | Autonomy levels and approvals work | Observe/Recommend/Controlled/Automatic; pause/resume; policy route; policy persisted across restart | Recovery tests; live UI pause/resume; API test; `test_autonomy_mode_and_pause_survive_application_restart` | PASS |
| 34.12 | Learning improves diagnostics safely | Phase 27 evidence for verified/failed recovery; advice only; no auto-promotion | `test_real_scheduler_recovery_and_phase27_learning` | PASS |
| 34.13 | Multi-agent diagnostic integration where appropriate | Optional debug-specialist advice through `handle_task`, never authoritative | `test_real_advisory_agent_uses_the_public_task_interface`; authority-boundary test | PASS |
| 34.14 | Model/compute integration without external-AI dependency | Deterministic monitoring; Phase 33 provider health/reconnect; AI failure isolated | Live A; provider test; health tests with no model | PASS |
| 34.15 | Dashboard reflects actual backend state | 17 real routes; panel reads/writes real APIs and renders each action's result | Manual browser health/history/pause/resume/maintenance-preview checks; result renderers exercised live; static/route tests | PASS |
| 34.16 | Security and auditability pass | Auth on all routes; permission checks; audited refusals/successes; redaction; no arbitrary commands | Recovery security tests; live G; API 403 test | PASS |
| 34.17 | Real application verification succeeds | Live A–H against real application instances | `tmp/phase34/evidence.json`, 8/8, exit 0 | PASS |
| 34.18 | Required tests and regressions pass | Phase + modified module + neighbour tests | 45 + 152 (1,150 subtests) + 228 (15 subtests) | PASS |
| 34.19 | Documentation synchronized | API reference, report, guide, status, architecture, log | `generate_api_reference.py --check`; docs review | PASS |

### Detailed sub-requirements

| Area | Evidence | Verdict |
|---|---|---|
| Component availability/responsiveness/error rates/latency/resources/queues/memory/CPU/GPU/provider/database/background tasks | Live probes and metrics; GPU/NPU `UNKNOWN` handled honestly; provider/DB/worker tests | PASS |
| Correlated failures, logs/traces, severity/impact, recommendations, explanation | Event history evidence; diagnosis `impact`/`recommendations`/`rationale`; live A/B/H | PASS |
| Thresholds, severity, false-alarm/repeat suppression | Resource thresholds from governor settings; incident fingerprint dedupe and recurrence | PASS |
| Recovery steps: identify, appropriateness, permissions, preserve state, execute, verify, record | `RecoveryManager` path; tests for each gate; state preserved in maintenance and workflow scenarios | PASS |
| Bounded retries/timeouts/budgets/cooldowns/no loops | Recovery tests: timeout, retry ceiling, cooldown, budget, escalation | PASS |
| Maintenance types: temp, cache, storage, database, logs, stale processes, leaks, config; dependency and background-service health | Maintenance preview/run for its own filesystem/SQLite/log/process/config boundaries; dependency and worker health is checked by the live 34-component roster instead of a duplicate maintenance probe; tests for temp/protected/SQLite/stale filtering/config routing; live F | PASS |
| Config change history, compatibility/migration validation, safe rollback, previous version preserved | Versioned snapshots, checksum, restore validates against the current settings validator and writes a new version; tests D and app restore. No settings-schema version exists, so there is no migration runner; a future schema version would need one (documented limitation) | PASS (compatibility validation); migration runner not applicable |
| Self-mod inspection/analysis/proposed patches/tests/evaluation/reports; no architecture/security/permission/safety/dependency/deploy changes | No mutation action exists; inspection is through read-only findings; protected changes remain under existing approvals | PASS |
| Incident identity/component/time/severity/symptoms/evidence/cause/impact/attempts/outcome/status/related | `Incident` model and persisted records; tests and live A–H | PASS |
| Autonomy Observe/Recommend/Controlled/Automatic; global pause | Four modes, persisted pause, policy route; tests and UI | PASS |
| Phase 27 repeated failures/successes/failed attempts/incorrect diagnoses/approved actions measured | Continual-learning evidence records and advice; test verifies `self_management_recovery` evidence | PASS |
| Phase 28 log/perf/config/recovery/test agents advisory only | Opt-in `use_agent`; real `handle_task` path; no authority; focused test | PASS |
| Phase 33 lightweight deterministic monitoring, reasoning optional, privacy, model failure isolation | Deterministic health path; provider failure live A; no external model needed | PASS |
| Security: role/permission actions, audit, credentials, protected config, command restriction, approvals, verification, emergency stop, tamper-resistant history | Auth routes; risk declarations; audit on every attempt/refusal/policy change; persisted emergency stop; redaction; no command execution from logs | PASS |
| Dashboard content and controls (health, incidents, history, resources, findings, recovery, maintenance, config, approvals, settings) | All present; real endpoints; manual check | PASS |
| Performance: overhead, detection, recovery, resource use, concurrent/repeated failures, loops, responsiveness, persistence | Measured table; 34 components in ~32–42 ms; response under pressure; restart persistence tests | PASS |
| Testing categories: health, diagnostics, recovery, maintenance, configuration, security, integration | 45 phase tests cover all listed categories | PASS |
| A–H real scenarios | 8/8 live, evidence file | PASS |
| Documentation: spec/report, architecture, health, diagnostics, incidents, recovery, maintenance, config, security, dashboard, API, tests/benchmarks | `PHASE34_REPORT.md`, `SELF_MANAGEMENT.md`, `ARCHITECTURE.md`, `STATUS.md`, `DEVELOPMENT_LOG.md`, `API.md`, README | PASS |

---

## 8. Five Acceptance Gates

| Gate | Result | Evidence |
|---|---|---|
| **1 — Functional completeness** | **PASS** | Live health, diagnosis, recovery, maintenance, configuration, autonomy and dashboard controls all executed against the real application; no mock-only completion |
| **2 — Integration completeness** | **PASS** | Real reuse of diagnostics, permissions, audit, state store, Phase 27/28/30/32/33; neighbour regressions pass |
| **3 — Testing completeness** | **PASS** | 45 phase tests, 152 modified/API tests with 1,150 subtests, 228 neighbour tests with 15 subtests; all exit 0; package mypy clean, remaining `application.py` error pre-existing and outside Phase 34; JS/API reference checks pass |
| **4 — Safety and reliability** | **PASS** | Allowlisted actions, permission and approval gates, bounded timeouts/retries/cooldowns/budgets, audited refusals, verified success, protected files, preserved history/checkpoints |
| **5 — Documentation and evidence** | **PASS** | Requirement matrix, actual commands/counts, live evidence, limitations and files recorded; API reference in sync |

---

## 9. Files

### Created

- `src/novacontrol/self_management/__init__.py`
- `src/novacontrol/self_management/models.py`
- `src/novacontrol/self_management/health.py`
- `src/novacontrol/self_management/diagnosis.py`
- `src/novacontrol/self_management/incidents.py`
- `src/novacontrol/self_management/recovery.py`
- `src/novacontrol/self_management/maintenance.py`
- `src/novacontrol/self_management/configuration.py`
- `src/novacontrol/self_management/service.py`
- `tests/test_phase34_self_management.py`
- `docs/SELF_MANAGEMENT.md`
- `docs/PHASE34_REPORT.md`
- `tmp/phase34/live_verify.py`, `evidence.json`, `live_output.txt` (evidence artifacts, not product code)

### Modified

- `src/novacontrol/application.py` — roster registration, composition, action registry, runtime seams, monitor ticker, status
- `src/novacontrol/api/app.py` — startup flag and 17 self-management routes
- `src/novacontrol/api/models.py` — API surface entries
- `src/novacontrol/api/route_consumers.py` — route consumers
- `src/novacontrol/web/static/index.html` — navigation and panel
- `src/novacontrol/web/static/app.js` — controls, refresh, history and pause/resume wiring
- `src/novacontrol/web/static/js/render-panels.js` — `renderSelfManagement`, metrics and history
- `src/novacontrol/web/static/js/render-utils.js` — render dispatch
- `tests/test_web_api.py`, `tests/test_optimization.py` — roster count/names updated from 25 to 34
- `docs/API.md`, `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`, `docs/ARCHITECTURE.md`, `README.md`
- `src/novacontrol/web/static/js/render-explore.js`, `voice.js`, `styles.css` etc. were already modified before Phase 34 and were preserved, not authored here

---

## 10. Limitations and Unsupported Cases

1. **In-process scope.** Health covers NovaControl's own components. It does not
   monitor external applications, remote SLAs, cloud-provider internals or
   network reachability beyond the provider status probe.
2. **Restart-bound configuration.** Persisted runtime settings can be
   snapshotted/restored; `.env`, boot config and code-level configuration still
   require a restart. This is documented rather than claimed as live reload.
3. **Provider restart is not possible.** Recovery can reconnect/re-probe an
   external provider but cannot start an Ollama service that is not running.
4. **No boot auto-rollback.** Corrupt settings are detected at startup and can
   be restored through an approved action; the system does not silently replace
   settings before an operator sees the failure.
5. **No registered core/event-bus/storage reinitialization.** Those actions are
   recommendation-only because no genuine procedure exists. No fake handler was
   registered to make the list look complete.
6. **No source-patch generation or application.** Phase 34 inspects and reports
   operational state; it does not generate/apply code patches autonomously, and
   it cannot change security, permissions, dependencies or deployments.
7. **Optional GPU/NPU monitoring.** On this machine both probes are `UNKNOWN`;
   they are not presented as failures. GPU/NPU incident handling on supported
   hardware was not verified here.
8. **UI scope.** The panel was manually checked for load, health, history,
   pause/resume, a real maintenance preview, and live result rendering for
   recommendation/investigation/recovery/snapshot shapes. Recovery and
   configuration outcomes were verified through real application tests and live
   scenarios, not by clicking every control end-to-end in the browser.
9. **Maintenance scope.** Dependency and background-service health comes from
   the live roster; maintenance itself covers filesystem, SQLite, logs, stale
   processes, resource readings, storage space and configuration detection.
   Configuration restoration is deliberately routed to its own versioned control.
10. **No settings-schema migration runner.** Snapshot restore validates against
    the current settings validator, which is the compatibility check this build
    needs. There is no schema-version migration engine; a future settings schema
    change would require one.
11. **No full suite.** Per policy, the complete repository suite was not run.
    Targeted affected and neighbouring suites are reported above.
12. **No commit/push.** The worktree remains uncommitted as required.

---

## 11. Final Verdict

**PASS** — all nineteen mandatory acceptance criteria and the five gates are
implemented and verified with targeted tests and real A–H application evidence.
No mandatory requirement is marked PARTIAL, BLOCKED or FAIL.

The implementation keeps authority in the existing permission, audit and
approval architecture. It observes, diagnoses, tracks, recovers, maintains and
rolls back within explicit bounds; it does not rewrite or re-permission itself.
