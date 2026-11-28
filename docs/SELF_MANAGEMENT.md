# Trustworthy Self-Management

Phase 34 gives NovaControl one operational control plane over the health,
diagnosis, recovery, maintenance and configuration services it already had. It
does not add a second health registry, a parallel recovery engine, a background
shell, or a self-modifying writer. The coordinator is
`novacontrol.self_management`.

## What it can and cannot do

| Capability | Implementation |
|---|---|
| Observe component health | The existing `DiagnosticManager` roster (34 components) plus Phase 34's live checks for event-bus round trips, API/UI assets, agents, memory, workers, workflows, computer operation and the hardware governor. |
| Diagnose a problem | Deterministic rules over live probe results, measured durations, recent failure events and resource readings. Confidence is explicit: `confirmed`, `probable`, `possible`, `unknown`. |
| Open and track incidents | Durable records in `self_management.json`: state, severity, symptoms, evidence, cause confidence, recovery attempts, resolution, recurrence and related incidents. |
| Recover | Only registered actions in `RecoveryRegistry`; each is permission-checked, optional-approval-gated, timeout-bounded, attempt-bounded, cooldown-bounded, verified by a live probe and audited. |
| Maintain | Preview/run over `data_dir/tmp`, `data_dir/cache` and `data_dir/logs` only. Protected names and suffixes are never removed. Approval-required items cause a refusal rather than a silent partial run. |
| Manage configuration | Validated settings snapshots with checksums, version history, known-good restore and corruption detection. A restore writes a new version and preserves the invalid body. |
| Self-modify source code | **Not supported.** No route or service installs dependencies, edits source, changes permissions, disables safety or deploys code. The status payload states these boundaries. |
| Run arbitrary commands from logs | **Not supported.** Recovery handlers are Python closures registered at boot, not commands parsed from incident text. |

## Composition

```
NovaControlApplication
  ├─ DiagnosticManager (Phase 14) ── live component probes
  ├─ EventBus / Event journal ───── recent failure evidence
  ├─ PermissionManager ──────────── risk and approval decisions
  ├─ AuditLogger ────────────────── one local, redacted trail
  ├─ JsonStateStore ─────────────── incidents and config history
  ├─ OperationsService ─────────── notifications, autonomy, workflows
  ├─ ContinualLearningEngine ───── verified recovery evidence (advisory)
  ├─ AgentRegistry ─────────────── optional debug specialist (advisory)
  └─ SelfManagementService
       ├─ HealthCollector
       ├─ diagnosis rules
       ├─ IncidentManager
       ├─ RecoveryManager
       ├─ MaintenanceService
       └─ ConfigurationManager
```

The health collector reads the application's existing diagnostics; it does not
start services to answer a check. A component whose probe cannot answer is
`unknown` or `failing`, never `ok` because a process exists.

## Health model

- **Availability and responsiveness** come from each component's own live
  probe and its measured duration. The shared `DiagnosticManager` converts an
  exception, timeout or malformed result into a failing row.
- **Event bus** performs a real publish round trip and reports subscriber
  failures.
- **API server** reports `SKIPPED` in an embedded process and `DEGRADED` when a
  declared surface has not received its startup signal.
- **Desktop interface** checks the actual static entry point and module count.
- **Agent execution, memory services, background workers, scheduled workflows
  and computer operation** read their real registries/tickers/stores.
- **Hardware manager** reads the resource governor's measured assessment.
- **Resources** include free RAM, the configured critical-RAM line, CPU, queue
  depth and process RSS where the platform provides it.
- A CPU-only host reports `GPU`/`NPU` as `unknown`; that optional reading is not
  turned into a recurring incident and does not make the whole system unknown.

## Diagnostics and confidence

1. A failing component probe is `confirmed` for the component's failure, not for
   an unobserved deeper cause.
2. A degraded probe is `probable`.
3. A probe that could not be read is `possible` (or `unknown` when no rule
   applies); an optional accelerator reading is reported without an incident.
4. At least two recent failure events elevate a matching finding to `probable`
   (or `confirmed` when the probe is also failing), and the event examples are
   retained as evidence.
5. Resource pressure is `confirmed` only when the measured RAM/CPU reading crosses
   the configured line.
6. Configuration invalidity is `confirmed` by the same validator the settings
   layer uses.

Every finding carries evidence and a rationale. `explain_unknown` is used when
an incident's component is no longer failing during investigation; it does not
invent a cause.

## Incidents

States: `detected → investigating → diagnosed → recovery_pending → recovering →
resolved`, plus `escalated` and `unresolved`. Illegal transitions are refused.
An open incident is updated by fingerprint rather than duplicated; resolved
incidents that recur create a related record with a recurrence count. Evidence
is capped and redacted before storage. Incidents survive restarts through the
application state store.

## Recovery actions

| Action | Component | Risk | Automatic | Approval | Verification |
|---|---|---|---|---|---|
| `restart-scheduler` | Scheduler | low, reversible | yes | no | live `Scheduler` probe |
| `restart-background-workers` | Background workers | low, reversible | yes | no | live workers probe |
| `reconnect-provider` | Ollama | low, reversible | yes | no | live `Ollama` probe |
| `reconnect-model-manager` | Model manager | low, reversible | yes | no | live `Model manager` probe |
| `reload-memory` | Memory services | medium, reversible | no | yes | live memory probe |
| `reload-context` | Context | medium, reversible | no | yes | live `Context` probe |
| `restore-known-good-config` | Configuration | medium, reversible | no | yes | config validator |

Automatic eligibility is a property of the action: `automatic`, reversible,
non-destructive, LOW risk and no approval requirement. An action claiming
automatic execution while failing one of those conditions is refused at
registration. `AUTOMATIC` mode runs only those actions; controlled mode executes
an approved action; recommend mode never executes.

Each attempt uses a timeout, a per-action attempt ceiling, a cooldown and a
window budget. Failed verification is a failure, not a success; repeated failure
escalates the incident. A refusal is audited too. No handler is derived from log
text. Destructive maintenance and configuration restore always require an
explicit approval.

## Autonomy levels

| Level | Behaviour |
|---|---|
| Observe | Health passes, incidents and diagnostic reports only. |
| Recommend | Adds recommendations and risk/impact text; executes nothing. |
| Controlled | Executes an explicitly approved action. |
| Authorized automatic | Executes only registered safe automatic actions, within budget. |

The global pause stops execution; it does not stop observation. Autonomy mode,
the pause flag and the window budget are persisted through the state store under
`self_management_policy` and restored before monitoring starts, so a paused
system does not silently resume automatic recovery after a restart. Pause also
blocks maintenance and configuration restore, not only recovery.

## Maintenance

- Allowlisted roots: `data_dir/tmp`, `data_dir/cache`; log rotation under
  `data_dir/logs`.
- Preview reports every item, its bytes, whether it is destructive, whether it
  needs approval, and whether it is protected.
- Temp/cache files are deleted only after the retention window; logs rotate only
  above the configured size; SQLite `quick_check`/`PRAGMA optimize` are bounded
  and the connection is closed.
- Protected names (`.env`, credentials, settings, audit, task and self-management
  state, SQLite) and protected suffixes (`.py`, `.md`, `.toml`, `.yaml`, `.key`,
  `.pem`, `.db`, …) are never deleted. A protected item is reported and skipped.
- Stale-process and resource-leak items are inspection-only; they never kill a
  process. Stale-process detection matches a real `python -m novacontrol` runtime
  (or the packaged entrypoint) and skips the live process's parent chain; a path
  that merely contains the project name is not a stale runtime.
- A configuration item in the preview is routed to the real versioned restore
  control and reported as `manual_action_required`; maintenance never claims it
  restored settings itself.
- Dependency and background-service **health** is checked by the live health
  roster, not duplicated as filesystem maintenance.
- Destructive work without approval is a `PermissionError`/HTTP 403, not a
  partial run.

## Configuration integrity

`ConfigurationManager` reads the persisted `settings` body (not the in-memory
default, which would hide corruption), validates it through `SettingsManager`
plus raw vocabulary checks, and stores versions under
`self_management_config`. Each version has a SHA-256 checksum. A stored checksum
that no longer matches its body is not eligible for restore. `restore_target`
refuses a non-known-good or now-invalid version; a restore applies the snapshot
through the application's existing settings/privacy/brain/audit seams, writes a
new version, and keeps the invalid one in history. A boot baseline is recorded
on first construction.

The environment/file configuration is read at boot; self-management snapshots
and restores the persisted user-settings surface. Environment changes still
require a restart.

## Integrations

- **Phase 27 learning**: every verified or failed recovery is captured as an
  execution with the live probe as verification, then recorded through
  `ContinualLearningEngine`. Recommendations are advisory; learning never
  promotes an action into the automatic set.
- **Phase 28 agents**: investigation can optionally ask a registered debug
  specialist for a second opinion. Its report is labelled advisory and cannot
  authorize or execute recovery.
- **Phase 30 autonomy**: the four operational levels, notification centre
  dedupe, objective/task checkpoint model and interrupt/resume behaviour remain
  the source of truth.
- **Phase 32 memory**: memory health reads the real index state; `reload-memory`
  re-reads stored records without deleting them.
- **Phase 33 models**: provider health and recovery use the existing model
  manager/router. Monitoring is deterministic and does not require an external
  model; provider failure cannot disable health or local recovery.

## Dashboard

The **Self-Management** panel provides Run Health Check, Refresh, View Incident
History, Investigate, Recommend Recovery, Approve & Recover, Pause/Resume,
autonomy-level selection, maintenance preview/run, configuration snapshot and
known-good restore. Every control calls a real route that invokes the service;
the panel is not fed by hard-coded status. Each action renders its own result
(recommendation, investigation, recovery verification, maintenance report,
snapshot or restore outcome) instead of discarding it and refreshing status
only. "Pending approvals" is labelled **Approval-gated actions**, because this
system approves synchronously and has no queued approval inbox.

## API

Authenticated routes are documented in [API.md](API.md):

`GET /self-management`, `POST /self-management/health`,
`POST /self-management/diagnose`, `GET /self-management/incidents`,
`GET /self-management/incidents/{id}`,
`POST /self-management/incidents/{id}/investigate`,
`POST /self-management/incidents/{id}/recommend`,
`POST /self-management/incidents/{id}/recover`,
`POST /self-management/pause`, `POST /self-management/resume`,
`POST /self-management/policy`, `GET /self-management/maintenance`,
`POST /self-management/maintenance/preview`,
`POST /self-management/maintenance/run`,
`GET /self-management/configuration`,
`POST /self-management/configuration/snapshot`,
`POST /self-management/configuration/restore`.

A refused recovery/configuration/maintenance action is HTTP 403; an unknown
incident/version/action is 404; an invalid body is 422. Recovery `approved_by`
is taken from the authenticated principal, never from a client-supplied name.

## Security and audit

- Recovery is checked by the shared permission manager and fails closed when it
  is not wired.
- `approved`, `approved_by`, autonomy changes and maintenance approvals are
  recorded; the API never accepts a client-provided approver identity.
- The audit trail records attempts, failures, refusals, maintenance and
  configuration restores with the same local redaction as other operations.
- Diagnostic evidence and incident text are redacted before persistence.
- No secret is returned by a self-management route; logs are not used as a
  command source.
- The service has no source/dependency/deployment mutation path. Those remain
  explicit operator workflows outside Phase 34.

## Performance (this machine)

Live run `tmp/phase34/evidence.json`: provider detection 42.3 ms, provider
recovery 9.3 ms; scheduler detection 125.1 ms, recovery 6.8 ms; resource health
36.6 ms; `/status` 62.7 ms; configuration restore 22.2 ms; maintenance 148.3 ms;
self-diagnosis 40.6 ms; refusal 3.1 ms; total 3.365 s. The monitor loop defaults
to 30 s, is configurable through `NOVACONTROL_SELF_MANAGEMENT_INTERVAL` (clamped
to 5–3600 s) and runs one bounded roster pass at a time. Status polling reads
cached/stored state and does not run probes.

## Known limitations

- Health/diagnostics only cover components this build exports; external
  applications are not monitored.
- Configuration restore applies the persisted settings surface, not a process
  environment or file-config change; those remain restart-bound.
- Autonomous recovery cannot start an external provider; reconnecting only
  clears health when the provider answers a fresh probe.
- No automatic boot rollback is performed when persisted settings are corrupt;
  the invalid body is detected, retained and offered for approved restore.
- The `reinitialize-core`, `reinitialize-event-bus` and `reinitialize-storage`
  recommendations from older prose are intentionally **unsupported** as actions:
  no genuine registered procedure exists, so they stay recommendations.
- There is no versioned settings-schema migration runner. Restore validates a
  snapshot against the current settings validator, which is the compatibility
  check this build needs; a future schema version would require an explicit
  migration step.
- The dashboard was exercised through its real routes, JS syntax/contract tests
  and a live maintenance preview, and all result renderers were exercised live
  with synthetic payloads. Every control was not clicked end-to-end in the
  browser; recovery and configuration outcomes are covered by real application
  tests and live scenarios.
