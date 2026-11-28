# Phase 35 — Implementation and Verification Report

**Phase:** Business Opportunity & Validation Engine  
**Date:** 2026-11-24  
**Workspace:** `C:\Users\NARASIMHA\OneDrive\Desktop\NovaControl`  
**Branch:** `main` (HEAD `afa5ff6`; the workspace already contained uncommitted Phase 27–34 work)  
**Commit/push:** none performed.

## 1. Repository reconnaissance findings

The existing repository is an event-driven, local-first FastAPI + static-web
application with a large composition root (`application.py`) and completed
Phases 1–34. The following were inspected and verified in implementation, not
just from documentation:

- `ExploreService` and its planner/trending/explainer/synthesizer modules, used by
  the existing Research UI and API.
- `KnowledgeManager` (`ingest_text`, retrieval, versioning) and the memory
  modules.
- `AgentRegistry`, `AgentRole`, `BaseAgent`/`handle_task` and the specialist
  pipeline.
- `JsonStateStore`, its namespace layout and staged durability behavior.
- `EventBus`/`EventJournal` and the existing `business.*` event naming space.
- `AuditLogger` and the application's `_self_management_audit` seam.
- `HardwareMonitor` and the resource governor's measured RAM/CPU/GPU snapshot.
- The authenticated FastAPI router, `require_auth`, API response models,
  `ApiSurface`, `ROUTE_CONSUMERS` and the generated `docs/API.md`.
- The existing static shell, panel renderer dispatch, form/list/detail patterns
  and CSS classes.
- Phase 34 recovery/health, Phase 33 model routing, Phase 32 knowledge, Phase 30
  operations and Phase 27 learning seams.

No implemented business-opportunity or financial-validation package, route or
workspace existed. There was no duplicate research provider, agent registry,
state store or scheduler to reuse incorrectly. The phase was therefore built as
one package over those seams. The pre-existing uncommitted work tree was left
intact; no completed phase was rewritten.

## 2. Existing components reused

| Existing component | Use in Phase 35 |
|---|---|
| `ExploreService` | Injected research provider; source-backed discovery scan and opportunity research. |
| `KnowledgeManager.ingest_text` | Ingests the final 15-section report under project `business`. |
| `JsonStateStore` | One `business_opportunities` namespace; no new database. |
| `EventBus` | All business lifecycle/research/evidence/score/experiment events. |
| `AuditLogger` | Every mutation and research/lifecycle decision under `business:<id>`. |
| `HardwareMonitor` | Measured RAM/CPU/GPU snapshot for resource-aware feasibility. |
| `AgentRegistry` | Registers one advisory `business-validation-agent`. |
| `AgentRole`/`BaseAgent` | New `BUSINESS` role and the existing task protocol. |
| FastAPI auth/router/models | All 17 routes and request validation. |
| `ApiSurface`/`ROUTE_CONSUMERS`/generator | API reference and route contract. |
| Existing web shell/CSS/renderers | Business workspace, no new design system. |
| Existing app composition root | `self.business_opportunities` on the real application. |

## 3. New components implemented

Created `src/novacontrol/business/`:

| File | Lines | Responsibility |
|---|---:|---|
| `__init__.py` | 40 | Public exports. |
| `models.py` | 1,330 | Opportunity, evidence/source provenance, market/customer/competitor, feasibility, finance, score, experiment, report and lifecycle records; validation; persistence. |
| `financial.py` | 222 | Three-scenario arithmetic, break-even, LTV/CAC, cash requirement, missing inputs and methodology. |
| `feasibility.py` | 372 | Eight dimensions, hardware/resource fit, blockers and completeness. |
| `scoring.py` | 453 | Ten weighted dimensions, coverage, missing reasons and disqualifiers. |
| `analysis.py` | 947 | Market/customer/competitor builders, evidence gaps, verdict rules, 15-section report, experiment planner. |
| `service.py` | 1,877 | Discovery/deduplication, provenance checks, Explore research, assessment, locks/cancellation, budgets, persistence, events/audit/knowledge. |
| `agent.py` | 122 | Read-only advisory agent through the existing AgentRegistry. |

Added:

- `tests/test_phase35_business_opportunity.py` (1,098 lines).
- `src/novacontrol/web/static/js/render-business.js` (347 lines).
- `docs/PHASE35_BUSINESS_OPPORTUNITY_VALIDATION_ENGINE.md`.
- This report.

The eight named phase responsibilities are implemented as the logical modules
above, not as eight heavyweight agents. The requirement explicitly allows that
form; the one registered agent is read-only and advisory.

## 4. Modified files

| File | Change |
|---|---|
| `src/novacontrol/application.py` | Import; instantiate `BusinessOpportunityService`; Explore adapter, knowledge, events, audit, hardware and agent wiring; expose `business_opportunities`; add status snapshot. |
| `src/novacontrol/api/app.py` | 17 authenticated business routes and error mapping. |
| `src/novacontrol/api/models.py` | Business request/response `ApiSurface` rows. |
| `src/novacontrol/api/route_consumers.py` | Route-consumer mappings. |
| `src/novacontrol/agents/models.py` | `AgentRole.BUSINESS`. |
| `src/novacontrol/agents/base.py` | Business response label. |
| `src/novacontrol/web/static/index.html` | Business sidebar destination and panel markup. |
| `src/novacontrol/web/static/app.js` | `setupBusinessOpportunities`, discovery/evidence/assessment/experiment/lifecycle handlers, scan and detail loading. |
| `src/novacontrol/web/static/js/render-utils.js` | `type === "business"` dispatch. |
| `src/novacontrol/web/static/js/render-business.js` | All Phase 35 renderers. |
| `docs/API.md` | Regenerated; 488 routes. |
| `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`, `docs/ARCHITECTURE.md`, `docs/AGENTS.md`, `README.md` | Phase 35 status/architecture/agent-roster/feature documentation. |
| `tests/test_phase35_business_opportunity.py` | New phase suite. |

No unrelated subsystem was intentionally modified. `git status` also lists
earlier Phase 27–34 changes that were already present in the work tree.

## 5. Architecture and integration

`BusinessOpportunityService` is constructed in the application composition root
and receives existing services through narrow callables:

```python
self.business_opportunities = self._build_business_opportunities()
```

The builder supplies:

- `research` → `ExploreService.research` adapter;
- `knowledge` → `KnowledgeManager.ingest_text`;
- `event_sink` → the application's existing event publisher (`_announce_soon`);
- `audit` → the existing `_self_management_audit`/audit logger path;
- `resource_snapshot` → measured `HardwareMonitor` state;
- the existing `JsonStateStore` with namespace `business_opportunities`.

The agent is registered in the existing `AgentRegistry` under
`business-validation-agent`, role `business`. It can summarize status and
opportunities, but its `handle_task` result explicitly says it cannot validate,
approve or execute an external action.

All domain arithmetic is deterministic. Explore is optional research input; no
model is loaded for financial, feasibility, scoring, verdict or report
generation. The service therefore works in offline/disabled-model operation for
already-recorded evidence.

## 6. API and frontend changes

Seventeen authenticated routes were added:

```
GET  /business/status
GET  /business/opportunities
POST /business/discovery/scan
POST /business/opportunities
GET  /business/opportunities/compare
GET  /business/opportunities/{opportunity_id}
POST /business/opportunities/{opportunity_id}/research
POST /business/opportunities/{opportunity_id}/evidence
POST /business/opportunities/{opportunity_id}/assess
POST /business/opportunities/{opportunity_id}/experiments/plan
POST /business/opportunities/{opportunity_id}/evaluate
POST /business/opportunities/{opportunity_id}/experiments/{experiment_id}/approval
POST /business/opportunities/{opportunity_id}/experiments/{experiment_id}/outcome
POST /business/opportunities/{opportunity_id}/pause
POST /business/opportunities/{opportunity_id}/resume
POST /business/opportunities/{opportunity_id}/reject
POST /business/opportunities/{opportunity_id}/archive
```

Request models validate required fields; `ValueError` maps to `422`;
missing opportunity/experiment to `404`; duplicate/concurrency/lifecycle
conflicts to `409`; provider unavailability to `503`. Authentication is the
existing dependency. The API reference is in sync at **488 routes**.

The existing web shell gained a **Business** destination. The workspace shows:

- discovery scan and sourced brief form;
- search, stage and industry filters;
- opportunity cards with stage, score, coverage, evidence count and verdict;
- detail view with evidence quality/source/staleness, market size status,
  customer evidence, competitor completeness, feasibility dimensions and gaps,
  financial scenarios and formulas, score dimensions/disqualifiers, experiments,
  report and lifecycle history;
- evidence, assessment, research, experiment approval/outcome, pause/resume,
  reject and archive controls;
- explicit labels for measured evidence, estimates, assumptions, stale sources
  and unavailable data;
- explicit statement that no external action is executed.

The renderer uses the existing DOM `el()` helper and text nodes (no untrusted
HTML injection), existing classes and the existing panel dispatch.

## 7. Database changes and migrations

No SQL database or schema migration was added. Phase 35 uses the application's
existing JSON state store under the `business_opportunities` namespace, schema
version `1`. It stores opportunities, evidence/source records, analysis snapshots,
experiments, reports and lifecycle history. Corrupt rows are skipped and reported
through `load_errors`; valid rows remain available in memory and are persisted on
the next mutation. The runtime state file is empty (0 opportunities) after the
test cleanup.

## 8. Tests executed and exact results

All commands used `PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1`.

| Command / scope | Result |
|---|---|
| `python -m pytest tests/test_phase35_business_opportunity.py -q` | **55 passed**, exit 0 |
| `tests/test_web_platform.py tests/test_web_api.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py tests/test_api.py` | **152 passed, 1,184 subtests**, exit 0 |
| `tests/test_application.py tests/test_phase34_self_management.py tests/test_phase30_operations.py tests/test_continual_learning.py tests/test_multi_agent_collaboration.py tests/test_phase33_adaptive_models.py tests/test_agents.py` | **278 passed, 15 subtests**, exit 0 |

### 8.1 Acceptance-audit repairs (post-implementation)

The original 38-test pass did not expose several real defects. The audit added
17 regression tests and fixed:

1. **Zero-division on a completely missing feasibility assessment.** All
   dimensions `None` made `scoring.py` divide by zero; the score now reports the
   risk dimension missing instead of inventing a neutral 0.5.
2. **A validated record could be paused.** `transition()` and the service guard
   now refuse pause from `VALIDATED` and `NOT_VIABLE`; `resume()` also requires
   the record to actually be paused, not merely to carry stale `paused_from`
   metadata.
3. **Stale analysis survived an evidence change.** Adding evidence or completing
   research now clears market/customer/competition/feasibility/financials/score/
   report/verdict snapshots while preserving the report version.
4. **Evidence quality gates counted assumptions, estimates and stale records.**
   Market/customer/competitor builders, scoring, feasibility and report/planner
   helpers now use only current sourced records for current readings, and
   estimates cannot satisfy the measured-market or sourced-competitor gates.
5. **Stale primary customer evidence was treated as current.** The customer
   snapshot now separates `primary_evidence_ids` from
   `current_primary_evidence_ids`; scoring, verdicts, feasibility, experiment
   planning and next steps use the current set.
6. **Recovery was blocked from the pre-verdict stage.** `evaluate()` now accepts
   `VALIDATION` (an interrupted evaluation can finish) while still refusing
   paused, rejected, archived, not-viable and validated records.
7. **A queued operation could mutate a terminal record.** Stage and research-budget
   guards are re-checked after acquiring the per-opportunity lock.
8. **Malformed values crashed or inflated results.** Malformed market years,
   unreadable RAM/readings, non-finite financial inputs/confidence/differentiation
   and a GPU-only requirement are now reported as missing or refused, never
   silently scored as measured.
9. **A target audience alone produced a market-accessibility score.** It now
   requires researched segments or a current primary customer record.
10. **A partial financial reassessment erased stored assumptions.** Financial
    inputs now merge with stored inputs; blank fields are omitted.
11. **Missing source provenance was promoted to "retrieved now".** A persisted
    source without a valid `retrieved_at` is a load error, not current evidence.
12. **UI controls were offered on terminal records and internal experiments hid
    their outcome control.** Pause/research/assess/evaluate/plan visibility now
    follows the actual transition set; internal (no-approval) experiments expose
    "Record outcome" directly, and financial assumptions/resources/expected
    evidence are displayed.
13. **A blank assessment form could overwrite stored financial/feasibility
    inputs with nulls.** The browser payload now omits fields the operator left
    blank.

The phase suite covers:

1. Transfer/duplicate discovery and sourced-brief requirements.
2. Duplicate refusal (exact and near-duplicate).
3. Evidence provenance: measured records need a source; primary records need a
   supplier; invalid URLs refused.
4. Assumptions and synthesis excluded from measured evidence.
5. Customer problem validation and missing primary evidence.
6. Stale primary/secondary source handling; 90-day boundary.
7. Market size measured/estimate/unavailable.
8. Competitor records and incomplete-search semantics.
9. Transparent financial arithmetic, missing inputs, negative contribution,
   break-even, LTV/CAC and cash requirement.
10. Conservative/base/optimistic scenario behavior.
11. Feasibility dimensions, hardware resource fit, missing dimensions and
    legal/dependency blockers.
12. Explainable scoring weights, coverage, missing dimensions and disqualifiers.
13. High score with missing/fabricated/assumption evidence cannot produce
    `VALIDATED`.
14. Full sourced/primary evidence produces `VALIDATED` when all gates pass.
15. `NOT_VIABLE` for negative unit economics and legal blockers.
16. 15 report sections in the required order and verdict rationale.
17. Lifecycle legal/illegal transitions, pause/resume, reject reason, archive,
    validated/not-viable/rejected/archived immutability and history.
18. Experiment specification completeness, approval enforcement, denied outcome
    refusal and measured outcome recording.
19. Research success, provider failure, budget exhaustion, cancellation and
    duplicate concurrent-run refusal.
20. Discovery scan returns source-backed candidates and invents no problem or
    solution.
21. Event/audit/knowledge writes.
22. Persistence/reload, corrupt-row recovery and comparison.
23. Advisory agent has no validation/approval/external authority.
24. Real FastAPI route creation, list/detail/assess/evaluate/approval/lifecycle
    responses, duplicate HTTP `409`, invalid body `422`, missing `404`, auth
    dependency presence and the application `/status` business snapshot.
25. Frontend static integration: sidebar/panel IDs, renderer registration,
    labels for unavailable/assumption/stale evidence, and no external execution.

Adversarial fixtures explicitly included:

- an opportunity with a high numerical score but only an assumption;
- an old primary interview plus fresh secondary sources;
- a complete secondary-only picture with no primary customer evidence;
- a fabricated/unsourced market claim;
- a stale competitor record;
- an assertion of `VALIDATED` after an illegal direct lifecycle move;
- an unapproved experiment outcome;
- a duplicate concurrent research request;
- a cancelled research request that must release its active lock;
- a provider failure that must not create evidence or a score;
- a corrupt persisted row that must not hide a valid row.

The full repository suite was not run; the project's focused-verification policy
was followed. No test was skipped or weakened to obtain these results. The
pre-audit 38-test run was not treated as sufficient evidence: all fixes above
have a regression test, and the final phase suite is 55 tests.

## 9. Type-checking and build results

- `python -m mypy src/novacontrol/business --no-error-summary` → **exit 0**
  (clean).
- `python -m mypy src/novacontrol/application.py src/novacontrol/api/app.py
  --no-error-summary` → **exit 1**, with exactly one error:
  `application.py:6121: Unexpected keyword argument "structured" for "see" of
  "VisionProvider"`. This is a pre-existing Phase 24/vision issue, not introduced
  by Phase 35. The business package and its public service are clean.
- `node --check` on `app.js`, `render-panels.js`, `render-business.js` and
  `render-utils.js` → **exit 0**.
- `python scripts/generate_api_reference.py --check` → **in sync** (488 routes).
- `git diff --check` → **exit 0**; only LF→CRLF notices.
- No separate bundler/build step exists for the static frontend.

## 10. Performance and resource measurements

Measured on this machine in a temporary state directory, with a complete
opportunity, five sourced evidence records (including one primary customer
interview), full financial/feasibility inputs, assessment and evaluation:

| Operation | Time | Result |
|---|---:|---|
| `discover()` | 1.19 ms | Opportunity created |
| Five `add_evidence()` calls | 5.20 ms | 5 records retained |
| `assess()` | 1.61 ms | Market/customer/competitor/feasibility/financial/score snapshots |
| `evaluate()` (verdict + report) | 2.51 ms | `VALIDATED`, 15 sections, 1 experiment |
| State file after the run | 45,117 bytes | JSON schema v1 |

The deterministic path allocates no model process and performs no network call.
Explore calls are the only external research path and are timeout-bounded at
45 seconds with a 6-source/4-run per-opportunity budget. Processes are bounded to
20 discovery scans; state to 500 opportunities, 200 evidence records and 20
experiments per opportunity.

## 11. Security and reliability checks

- **Authentication:** every business route uses the existing auth dependency; the
  phase does not add an unauthenticated surface.
- **Authorization:** external actions are never executed. Experiment approval is
  an operator decision record only; an outcome is refused before approval when
  the experiment declares approval, and `external_action_executed` is always
  false. No spending, contact, publishing or purchasing path exists.
- **Input validation:** required discovery fields, evidence claim/quality/
  provenance, target audience, experiment fields, approval decision and reject
  reason are validated. Invalid lifecycle moves and terminal edits are refused.
- **Provenance:** measured evidence requires a source URL/reference and retrieval
  time; primary evidence requires a supplier. Research references without an
  http(s) URL are skipped.
- **Staleness:** sources older than 90 days are labelled stale, excluded from
  current validation eligibility and reported as gaps.
- **Fabrication resistance:** assumptions, synthesis and unclassified research
  references cannot satisfy primary-customer, current-market, sourced-competitor
  or current-source gates. An agent cannot set a verdict.
- **Concurrency:** per-opportunity async locks plus an active set prevent
  duplicate assessment/research/evaluate; a global scan lock prevents overlapping
  discovery scans. Cancellation releases the active guard (tested).
- **Recovery:** every meaningful intermediate state is persisted; corrupt rows
  are isolated; reload restores valid records; a failed provider leaves the
  opportunity intact with a warning.
- **Audit/events:** discovery, research, evidence, assessment, score, experiment
  proposal/approval/outcome, lifecycle changes, report generation and validation
  are recorded through the existing audit/event seams. Knowledge ingestion
  failures do not silently erase the decision.
- **Resource governance:** no model load; research/source/scan/opportunity/
  evidence/experiment budgets; Explore failure safe.
- **Injection:** frontend renders untrusted report/evidence text with text nodes;
  no `innerHTML` path for user/source data.

## 12. Known limitations

1. **Autonomous idea synthesis is intentionally constrained.** Discovery returns
   source-backed candidate signals through Explore but does not invent a customer
   problem, solution, customer interview or market. A sourced operator brief is
   required before an opportunity record exists. This is the anti-fabrication
   design, but it means full end-to-end "idea generation" is not autonomous.
2. **Explore availability.** Without a configured/permitted Explore provider,
   research and scan return a safe warning; manual evidence still works.
3. **Research results are unclassified secondary references.** They are not
   automatically a specific market-size, customer or competitor claim.
4. **Customer evidence is supplied.** No interview, survey, testimonial or
   purchase intent is generated.
5. **90-day freshness** may be too short for long B2B sales cycles; stale evidence
   remains visible and historical but cannot currently validate.
6. **Experiments are not executed.** Operators must run them; the system plans,
   approves, records and reports only.
7. **JSON persistence** has no relational migration history. Schema version is 1.
8. **Scoring profiles are constructor-configured**; there is no mutable profile
   editor in the API/UI, though the active weights/threshold and all missing
   reasons are exposed.
9. **Visual UI not manually click-checked in this session.** The workspace markup,
   renderer dispatch and static contracts were tested; a browser click-through of
   every control was not performed.
10. **Full repository suite not run.** Only the phase suite, the 152-test web/API
    group and the 278-test application/Phase 30–34/agent neighbour group were
    executed. The application mypy run retains one pre-existing vision error.
11. **The audit did not perform a live Explore call or a browser click-through.**
    Research success/failure is exercised through injected providers; static UI
    contracts and API rendering paths are tested, but no control was manually
    clicked in this session.

## 13. Incomplete requirements

| Item | State | Reason |
|---|---|---|
| Autonomous problem/solution synthesis from a signal | **PARTIAL** (deliberate) | Source-backed candidate scan is implemented; an operator must supply the problem and solution so no opportunity is fabricated. |
| Live external Explore research / scan | **PARTIAL** | Success/failure paths are exercised with injected providers; no live external query was run in this environment. |
| Full visual browser click-through | **PARTIAL** | Static/frontend contracts and API behavior tested; controls not manually clicked. |
| Configurable scoring profile UI/API | **NOT_IMPLEMENTED** | Constructor configuration only; active weights/threshold are reported. |
| SQL persistence/migrations | **NOT_IMPLEMENTED** (not required) | Uses the project's established JSON state store. |
| Autonomous external actions | **PASS (disabled by design)** | No external action path exists; approval is recorded only. |

Phase 36 was not started.

## 14. Final acceptance matrix

| Requirement | Verdict | Evidence |
|---|---|---|
| Repository reconnaissance and reuse | **PASS** | Existing Explore/knowledge/state/events/audit/hardware/agent/API/web seams inspected and wired; no duplicate stack. |
| Opportunity discovery and deduplication | **PASS** | Sourced brief discovery, sourced Explore candidate scan, exact/near-duplicate refusal, unique IDs, timestamps, sources, confidence, uncertainty. |
| Market research | **PASS** | Market/customer/segment/trend/geography/maturity/growth/regulatory fields; measured vs estimated vs unavailable; source URL/timestamp. |
| Customer problem validation | **PASS** | Severity/frequency/alternatives/WTP/unmet demand; primary evidence requires supplier; no fabricated interviews. |
| Competitive intelligence | **PASS** | Sourced competitors/pricing/features/positioning/differentiation; incomplete search is not "no competitors". |
| Feasibility assessment | **PASS** | Technical/operational/financial/market/legal/resource/execution/dependency dimensions; measured hardware and missing reasons. |
| Financial and business economics | **PASS** | Investment, development, infrastructure, opex, CAC, pricing, gross/contribution margin, break-even, revenue, cash; three scenarios; formulas/assumptions/missing inputs. |
| Opportunity scoring | **PASS** | Ten weighted dimensions, explicit weights, coverage, missing reasons, disqualifiers; high score cannot bypass evidence. |
| Validation/experiment planner | **PASS** | Full experiment specification; approval; no external execution; outcomes require approval. |
| Business validation report | **PASS** | 15 required sections in order; five verdicts derived from evidence. |
| Opportunity lifecycle | **PASS** | Validated transitions, pause/resume/reject/archive, history; validated/not-viable/rejected/archived immutability; no agent-only validation. |
| Agent integration | **PASS** | Existing AgentRegistry/BaseAgent/AgentRole; one read-only advisory specialist; no approval authority. |
| Memory and knowledge integration | **PASS** | Durable history/evidence/assumptions/reports in JSON state; report ingestion via KnowledgeManager; list/compare; 90-day stale handling. |
| API integration | **PASS** | 17 authenticated routes, validation, pagination/filtering, existing error/auth conventions, 488-route reference in sync. |
| Frontend integration | **PASS with visual limitation** | Existing shell/CSS/renderer dispatch, workspace and all controls implemented; static contracts pass; not manually clicked. |
| Event and audit integration | **PASS** | Opportunity-traceable events and audit decisions tested. |
| Reliability and security | **PASS** | Source/provenance enforcement, concurrency locks, cancellation, persistence/recovery, budgets, no external action, no model load. |
| Testing | **PASS** | 55 phase tests including 17 audit regressions, 152 web/API tests/1,184 subtests, 278 neighbour tests/15 subtests, all exit 0. |
| Documentation | **PASS** | Architecture/guide, verification report, API reference, STATUS, DEVELOPMENT_LOG, ARCHITECTURE and README updated. |
| Preserve Phases 1–34 / no Phase 36 | **PASS** | Phase 34 suite passes; no Phase 36 work started; existing uncommitted tree preserved. |
| No commit or push | **PASS** | No git commit/push executed. |
| Full autonomous idea generation | **PARTIAL (deliberate)** | Sourced candidates only; operator brief required to avoid fabricated opportunities. |
| Application-wide type check | **BLOCKED by pre-existing error** | `application.py:6121` `VisionProvider.see(..., structured=True)`; business package mypy is clean. |

**Overall:** Phase 35's mandated evidence-backed validation path is implemented,
integrated, exercised through the real FastAPI application, and now has a
completed acceptance audit. The audit found and repaired real defects in
terminal-state immutability, stale-evidence handling, estimate/assumption gates,
recovery, concurrency, malformed-value handling and the frontend payload; 17
regression tests pin those repairs. The one material scope limitation remains
deliberate: the engine discovers sourced candidate signals but does not
manufacture an unsourced business idea. The workspace was not visually
clicked through, no live external Explore research was performed in this audit, and
the full repository suite was not run; these are stated as limitations rather
than claimed as passes.
