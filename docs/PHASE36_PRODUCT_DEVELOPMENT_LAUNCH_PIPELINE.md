# Phase 36 — Product Development & Launch Pipeline

**Status:** implemented and verified on this machine (2026-11-24).  
**Workspace:** `C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl`  
**Scope:** convert a Phase 35 validated opportunity into a requirements-driven, tested, release-prepared product, and record a human launch-readiness decision.  
**Explicit boundary:** Phase 36 never deploys, publishes, sends a campaign, makes a commercial commitment, commits or pushes.

## 1. Architecture

Phase 36 is one package, `novacontrol.product`, layered over the existing application. It does not introduce a second orchestrator, database, health system, agent loop, or design system.

```
Phase 35 BusinessOpportunityService.detail()
        │  stored validation report only
        ▼
ProductService ──► ProductProject (durable JSON record)
   │                 ├── PRD revisions / approvals
   │                 ├── Architecture document / ADRs
   │                 ├── Development plan / milestones / tasks
   │                 ├── Tests, gates, build evidence
   │                 ├── Defects, releases, readiness
   │                 └── Events, approvals, condition decisions
   ├── AgentRegistry / AgentTask ── existing execution path
   ├── JsonStateStore ───────────── namespace `product_projects`
   ├── EventBus ─────────────────── `product.*` announcements
   ├── AuditLogger ──────────────── project-scoped audit entries
   ├── memory manager ───────────── project-memory snapshots
   ├── KnowledgeManager ─────────── approved PRD knowledge ingestion
   ├── HardwareMonitor ──────────── measured resource assumptions
   └── ProductWorkspace ─────────── gitignored isolated project directory
```

The composition root builds one `ProductService` as `nova.product_pipeline`, registers one advisory `product-pipeline-agent` in the existing registry, and registers one `ProductModule` capability module on the existing runtime.

## 2. Opportunity intake contract

`ProductService.create_project` reads the Phase 35 record through the real `BusinessOpportunityService.detail` seam. The caller cannot submit a verdict or override the report.

| Phase 35 state | Intake result |
|---|---|
| `VALIDATED` | Accepted if a stored validation report exists. |
| `CONDITIONALLY_VALIDATED` | Accepted only with `approved=true`, a named human actor, a note, and at least one report condition/evidence gap. All recorded conditions are retained; acknowledged conditions are matched when supplied. |
| `INSUFFICIENT_EVIDENCE` | Rejected with `OpportunityNotEligible`. |
| `NOT_VIABLE` | Rejected with `OpportunityNotEligible`. |
| `REQUIRES_FURTHER_RESEARCH` | Rejected with `OpportunityNotEligible`. |
| Unknown / absent verdict | Rejected; no product is created. |

Every project stores:

- `opportunity_id`;
- validation state and a copy of the report reference/version/rationale;
- an opportunity snapshot containing the problem, solution, audience, score and sources;
- unresolved conditions and the conditional approval record;
- the path of its isolated workspace.

One Phase 35 opportunity creates at most one product project. Duplicate intake is refused with the existing project id.

## 3. Product lifecycle

```
DEVELOPMENT → INTERNAL_TESTING → QA_IN_PROGRESS
            → READY_FOR_RELEASE_REVIEW → APPROVED_FOR_LAUNCH
            ↘ BLOCKED                  ↘ ARCHIVED
```

The stored status is a derived lifecycle state, not a badge an API can set. `evaluate_launch` recomputes it from checks. `BLOCKED` results from a hard blocker such as cancellation, an archived product, a high/critical open defect, or a failed security gate. A prior launch approval is invalidated when current gates no longer pass.

`ARCHIVED` retains all records. It is not deletion; workspace cleanup is a separate explicit action.

## 4. PRD structure

`generate_prd` creates a versioned, traceable document with:

- vision, problem statement, target users, personas, user journeys;
- functional, non-functional, core and optional requirements;
- security, performance and accessibility requirements;
- constraints, dependencies, acceptance criteria and success metrics;
- explicit exclusions;
- missing-information list and traceability block.

Each requirement has a stable id (`REQ-001`, `NFR-001`, `SEC-001`, `PERF-001`, `A11Y-001`, `FEAT-001`, `OPT-001`), kind, priority, description, acceptance criteria, status and a trace record containing the opportunity id, validation report version and source.

Missing sections are not filled with invented copy. They become `missing_information` entries. A new PRD revision preserves the previous complete snapshot in `prd_history` (bounded to 20). Approval requires a named human actor and a note. A revision request changes the status to `revision_requested`.

## 5. Planning methodology

`create_plan` requires an approved PRD. The default decomposition creates one implementation task for each implementable requirement and one verification task per implementation task, with the verification task depending on its implementation.

Operator-supplied plans can define tasks and milestones. The planner validates:

- unique, path-safe task ids;
- known requirement ids;
- known agent roles;
- known dependency ids;
- no self-dependency and no cycle;
- milestone task references.

Each task carries priority, role, dependencies, milestone, `parallel_safe`, optional effort estimate, estimate basis, estimate confidence, assigned agent and bounded attempts. Missing estimates remain `not estimated`; no number is invented.

The critical path is the longest dependency path using supplied estimates. `delivery_plan.calendar_dates_committed` is always `false`; milestones are ordered, not dated.

Resource assumptions come from the live `HardwareMonitor` and the configured maximum parallelism. Availability is measured, not reserved.

## 6. Architecture and ADRs

`generate_architecture` requires an approved PRD. It records:

- component decomposition and responsibilities;
- API contracts;
- database design;
- data flows and integrations;
- technology selection;
- deployment, security, scalability and observability sections;
- architecture decision records with alternatives, consequences, rationale, major flag and approval status;
- missing information.

If no components are supplied, one component is derived from approved requirements and explicitly labelled `derived_from_approved_prd`. The service does not choose a stack, database or deployment target on its own.

Architecture approval requires a named human actor and a note. It approves the ADRs together with the document. Rejection records the decision. Previous design versions are retained in `architecture_history`.

## 7. Agent responsibilities

The logical responsibilities are task roles, not eight new heavyweight agents:

| Responsibility | Existing role used |
|---|---|
| Product management / planning | `planning` or `project_manager` |
| Software architecture | `planning` |
| Backend/frontend implementation | `coding` |
| Database engineering | `coding` |
| Testing | `testing` |
| Security review | `review` |
| Documentation | `documentation` |

`run_task` builds an `AgentTask` and dispatches it through `AgentRegistry.first_by_role`. A registered agent's response is not accepted as completion unless it supplies verified evidence. Otherwise the task becomes `needs_review`. An injected executor may return a `TaskOutcome` with verified evidence and generated files; files are written only through `ProductWorkspace`.

One advisory `product-pipeline-agent` summarises projects through the existing registry. It cannot approve, execute or launch.

## 8. Workspace isolation

Default root: `build_workspace/products/`, already gitignored and outside NovaControl's source tree.

- Every product has one root keyed by its safe project id.
- Paths must be relative, non-empty, traversal-free and drive-free.
- Symlinks and writes through symlinked parents are refused.
- Existing files are never overwritten by a generated task unless the path was previously generated by this product and its current hash matches.
- An explicit reviewed overwrite requires `expected_sha256`.
- Secret-shaped content is refused before it reaches disk.
- File count and file size are bounded.
- Release archives and dependency/cache directories are excluded from snapshots.
- Cleanup deletes only the product workspace and requires `approved=true`.
- A repository reference may be stored, but Phase 36 never commits, pushes, checks out or writes into that repository.

## 9. Development workflow

1. Create the product from an eligible opportunity.
2. Generate and approve the PRD.
3. Generate and approve the architecture.
4. Create a plan and start development.
5. Run dependency-ready tasks. Tasks marked `parallel_safe` may run concurrently up to the configured limit; unsafe tasks run sequentially.
6. A verified outcome writes generated files and moves the task to `completed`; an unverified outcome becomes `needs_review`; a dependency failure blocks dependents.
7. Retry failed/review/blocked/cancelled tasks within the attempt budget.
8. Run environment validation, record build output, run or record tests, and inspect gates.
9. Open defects, create bounded corrective tasks, retry, re-run the relevant gate, and resolve only with passing test evidence.
10. Prepare and approve a release.
11. Evaluate launch readiness and record the human launch decision.

Cancellation marks pending/ready/running/blocked tasks cancelled and blocks readiness. Resume clears the product cancellation without reviving cancelled tasks. On load, a task left `running` by a restart is recovered as `pending` with an explicit interrupted note.

## 10. Quality gates

Default required gates:

| Gate | Accepted evidence kinds |
|---|---|
| `unit` | `unit` |
| `integration` | `integration`, `api`, `e2e`, `ui` |
| `security` | `security`, `dependency`, `secret_scan` |
| `static_analysis` | `static_analysis`, `type_check`, `lint` |
| `regression` | `regression` |
| `acceptance` | `acceptance` |

Gates are configurable at project creation. A gate passes only when its latest recorded result is `passed` with a positive test count, zero failures and a consistent passed/total count. A build record is separate from test evidence.

The built-in HTTP runner executes only `python -m pytest` inside the isolated workspace. It reports unsupported security/static/dependency/performance gates as `not_run`; it never relabels a unit suite as a security result. Operators record those tools' real output through `/tests` or configure a different runner.

Requirement acceptance is tracked separately. Launch readiness counts only requirements marked `verified`; a completed task does not verify a requirement.

## 11. Defects and iteration

Defects carry severity, status, requirement/task/test trace, detection actor/time, resolution, fix task, passing test ids and history. Resolution requires at least one passing recorded test; the fix task (when supplied) must be completed. `verified=true` moves the defect to `verified`; otherwise it is `fixed`. Reopening retains history.

`create_corrective_tasks` creates at most one open corrective task per failed gate, blocking defect or unmet requirement. The iteration budget is bounded (default 3). There is no unbounded self-modification loop.

## 12. Release preparation

Release preparation is refused unless every required quality gate passes and a recorded build is validated. It produces:

- a validated version string;
- a release target (`local`, `desktop`, `web`, `api`, `container`);
- release notes derived from recorded tasks, gates, defects, conditions and risks;
- a source-bundle ZIP in `workspace/releases/`;
- SHA-256 and byte size;
- deployment documentation;
- configuration and environment checks;
- database migration file checks (when migrations are supplied);
- rollback plan;
- security checklist and secret scan;
- operational requirements supplied by the operator;
- known defects.

The artifact manifest explicitly records `external_deployment_performed: false`.

Release approval requires a named human actor, a note, passing gates and a validated build. A rejected release remains recorded.

## 13. Launch-readiness rules

`evaluate_launch` returns explicit checks, blockers, warnings, a score, requirement completion, known risks, release id and approval validity. It evaluates:

- originating opportunity and conditional conditions;
- PRD approval and requirement verification;
- architecture approval;
- development task completion;
- build validation;
- required quality gates;
- blocking and lower-severity defects;
- release preparation and release approval;
- release documentation;
- operational requirements;
- security gate and architecture;
- performance evidence (warning when requirements exist but no performance result was recorded);
- lifecycle/cancellation state.

`APPROVED_FOR_LAUNCH` is possible only when:

1. readiness is `READY_FOR_RELEASE_REVIEW`;
2. the release is approved;
3. every required gate passes;
4. there are no hard blockers;
5. a named human submits `approved=true` with a note.

The approval is an authorization record. No deployment, publication, campaign or commercial commitment follows from it. A later failed gate invalidates the approval and returns readiness to `BLOCKED`.

## 14. API

All routes require the existing API authentication. Verified total after this phase: **523 routes**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/product/status` | Limits, configuration, workspace root, boundaries. |
| GET | `/product/projects` | Portfolio list with query/state/limit/offset. |
| POST | `/product/projects` | Create from an eligible Phase 35 opportunity. |
| GET | `/product/projects/{id}` | Full project document and progress. |
| GET | `/product/projects/{id}/history` | Events, approvals, revisions, conditions. |
| POST | `/product/projects/{id}/prd` | Generate a PRD revision. |
| POST | `/product/projects/{id}/prd/approval` | Human requirements decision. |
| POST | `/product/projects/{id}/architecture` | Generate design/ADRs. |
| POST | `/product/projects/{id}/architecture/approval` | Human architecture decision. |
| POST | `/product/projects/{id}/plan` | Create plan/milestones/tasks. |
| POST | `/product/projects/{id}/plan/approval` | Human plan decision. |
| POST | `/product/projects/{id}/development/start` | Require approved PRD/architecture. |
| POST | `/product/projects/{id}/tasks/run-ready` | Run dependency-ready tasks. |
| POST | `/product/projects/{id}/tasks/{task_id}/run` | Run one task. |
| POST | `/product/projects/{id}/tasks/{task_id}/result` | Record verified external result. |
| POST | `/product/projects/{id}/tasks/{task_id}/retry` | Bounded retry. |
| POST | `/product/projects/{id}/environment/validate` | Validate workspace/runtime. |
| POST | `/product/projects/{id}/workspace/files` | Reviewed workspace write. |
| POST | `/product/projects/{id}/workspace/cleanup` | Approved isolated cleanup. |
| POST | `/product/projects/{id}/build` | Record a real build result. |
| POST | `/product/projects/{id}/tests` | Record real test evidence. |
| POST | `/product/projects/{id}/quality/run` | Built-in pytest gate run. |
| POST | `/product/projects/{id}/defects` | Open a defect. |
| POST | `/product/projects/{id}/defects/{defect_id}/resolve` | Resolve with passing evidence. |
| POST | `/product/projects/{id}/defects/{defect_id}/reopen` | Reopen with reason. |
| POST | `/product/projects/{id}/requirements/{requirement_id}/verify` | Requirement acceptance. |
| POST | `/product/projects/{id}/corrective-tasks` | Bounded iteration tasks. |
| POST | `/product/projects/{id}/release` | Prepare release bundle. |
| POST | `/product/projects/{id}/release/approval` | Human release decision. |
| POST | `/product/projects/{id}/launch-readiness` | Recompute readiness. |
| POST | `/product/projects/{id}/launch/approval` | Human launch authorization. |
| POST | `/product/projects/{id}/conditions/resolve` | Resolve a condition with evidence. |
| POST | `/product/projects/{id}/cancel` | Cancel execution. |
| POST | `/product/projects/{id}/resume` | Resume without reviving tasks. |
| POST | `/product/projects/{id}/archive` | Archive with history retained. |

Error mapping: 404 not found, 403 approval required, 409 lifecycle/quality conflict, 422 malformed/ineligible input, 429 budget exhausted.

## 15. Frontend

The sidebar destination **Products** opens the Product Development workspace:

- portfolio list and status;
- opportunity import with conditional approval note;
- PRD form, missing-information list and requirement verification;
- architecture JSON and decisions;
- plan JSON, milestones, critical path and task controls;
- build/test evidence forms, gates and pytest quality run;
- defect tracking with evidence-bound resolution;
- release preparation and approval;
- launch readiness, approval/rejection, cancel and archive;
- approval history and project events.

The UI labels automated recommendations and human decisions separately and states that no external launch, deployment or commit occurs.

## 16. Security and resource governance

- Existing API authentication is required on every product route.
- Project workspaces are isolated under one configured root; traversal, symlink and secret-shaped writes are refused.
- Existing files cannot be silently overwritten.
- Release packaging scans workspace files for secret patterns.
- Test/build output and event details are redacted for credential shapes.
- Test runner execution is explicit, cwd-bound, time-bounded, environment-sanitized and limited to pytest.
- Budgets: products, tasks, tests, defects, events, attempts, iterations, parallel tasks, workspace files and file bytes.
- Cancellation and restart recovery are persisted.
- No external launch, deployment, campaign, commit or push path exists in this phase.
- Demo/local authentication principal is the existing operator identity; product-level multi-tenant ACLs were not added.

## 17. Testing

Phase 36 tests cover intake, conditional rules, PRD revisions/traceability, plan dependencies/critical path, parallel execution, workspace isolation, quality gates, defect lifecycle, release/launch approvals, failed-gate bypass refusal, cancellation, persistence, memory/knowledge callbacks, API authorization, frontend serving and malformed inputs.

Exact results are recorded in `docs/PHASE36_IMPLEMENTATION_AND_VERIFICATION_REPORT.md`.

## 18. Known limitations

1. The built-in test runner supports pytest only. Security, dependency, lint, type-check and performance gates require recorded tool output or a configured runner.
2. Environment validation checks workspace and runtime availability only. It does not install dependencies or prove a build.
3. No Git implementation was added: the repository field is a reference; no clone, branch, diff, commit or push is performed.
4. Release artifacts are source bundles with documents, not compiled installers or container images. The target build remains the product's own build command.
5. The default registered agents generally return advisory output without `verified` metadata, so their tasks become `needs_review` unless a real executor supplies verified evidence. This is intentional: an agent response is not completion evidence.
6. Conditional-validation conditions are retained and surfaced as warnings in launch readiness; resolving them records evidence but does not replace a quality gate.
7. No per-product ACL or multi-user role model was added beyond existing API authentication.
8. The UI was served and its wiring checked by HTTP tests, but no visual browser click-through or screenshot review was performed in this implementation pass.
9. No live external model execution is required by this phase; planning and readiness are deterministic.
