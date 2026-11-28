# Phase 35 — Business Opportunity & Validation Engine

**Status:** implemented and verified on the real application path (see
[PHASE35_IMPLEMENTATION_AND_VERIFICATION_REPORT.md](PHASE35_IMPLEMENTATION_AND_VERIFICATION_REPORT.md)).  
**Package:** `novacontrol.business`  
**API:** 17 authenticated `/business*` routes  
**State:** `data/business_opportunities.json` (schema version 1)  
**External actions:** none are executed by this phase.

## 1. What this phase is — and is not

This is an evidence-backed business validation engine. It can:

- scan permitted sources through the existing Explore service for **source-backed
  candidate signals**;
- record a sourced operator brief or signal as an opportunity;
- collect evidence with a source URL/reference, retrieval time and quality;
- research an opportunity through Explore;
- build market, customer, competitor, feasibility, financial and scoring
  analyses from the recorded evidence;
- plan practical validation experiments;
- derive a verdict and a 15-section report from the evidence;
- record experiment approvals and measured outcomes;
- persist opportunity history, report versions, assumptions and source
  provenance;
- ingest the final report into the existing knowledge index.

It deliberately does **not**:

- invent a customer interview, survey, testimonial, purchase intent, competitor,
  market size or revenue figure;
- turn a search result, a model recommendation or an optimistic assumption into
  `VALIDATED`;
- treat an incomplete competitor search as proof that no competitors exist;
- approve its own experiment or execute customer contact, advertising, spending,
  purchases or any other external action;
- load a heavyweight local model to perform validation. The analysis path is
  deterministic and model-free; Explore is the only optional external research
  provider.

The canonical phase objective asks for autonomous discovery. The implementation
does the part that can be done honestly: it discovers **source-backed candidate
signals** and requires an operator to state the customer problem and proposed
solution before an opportunity record is created. That is a deliberate
constraint, not a missing generator of made-up ideas.

## 2. Architecture

```
NovaControlApplication
  ├─ ExploreService ─────────────── permitted research, source retrieval times
  ├─ KnowledgeManager ──────────── ingest the final report
  ├─ JsonStateStore ────────────── business_opportunities namespace
  ├─ AuditLogger ───────────────── one existing local audit trail
  ├─ EventBus publisher ────────── business.* events
  ├─ HardwareMonitor ───────────── measured RAM/CPU/GPU readings
  ├─ AgentRegistry ─────────────── business-validation-agent (advisory only)
  └─ BusinessOpportunityService
       ├─ models.py       evidence-first domain records and lifecycle
       ├─ financial.py    transparent arithmetic and scenarios
       ├─ feasibility.py  measured/powered resource and risk assessment
       ├─ scoring.py      weighted, explainable dimensions
       ├─ analysis.py     market/customer/competitor/report builders
       ├─ service.py      persistence, research, locks, budgets, audit/events
       └─ agent.py        read-only AgentRegistry bridge
```

The service is a consumer of existing seams. There is no second research stack,
agent registry, scheduler, health registry, permission system, event bus or
database. Business state is one JSON namespace beside the application's other
small module snapshots; the knowledge report is ingested into the existing
knowledge index.

## 3. Component responsibilities

| Responsibility | Implementation | Authority |
|---|---|---|
| Opportunity discovery | `scan_signals()` + `discover()` | Scan proposes sourced candidates; only a recorded brief creates an opportunity. |
| Market research | Explore + `build_market_research()` | Explore stores unclassified secondary references; the operator/source supplies a specific market claim. |
| Customer problem analysis | `build_customer_validation()` | Primary evidence must be supplied and attributed; no generated interviews. |
| Competitive intelligence | `build_competition()` | Only sourced competitor records count; incomplete search remains incomplete. |
| Feasibility | `assess_feasibility()` | Uses supplied judgements, recorded technical evidence and live hardware readings; missing dimensions stay unavailable. |
| Business economics | `analyse_financials()` | Explicit assumptions, three scenarios, visible formulas and missing inputs. |
| Scoring | `score_opportunity()` | Weighted dimensions with coverage, missing reasons and disqualifiers. |
| Validation planning | `plan_experiments()` | Experiments are proposals; external actions require approval and are never run here. |
| Evaluation/report | `decide_verdict()` + `build_report()` | Verdict is derived from evidence, never assigned by an agent. |
| Advisory agent | `BusinessValidationAgent` | Read-only summary through the existing `AgentRegistry`/`handle_task`. |

The eight phase responsibilities are modules, not eight separate heavyweight
agents. This is the "logical responsibilities" form the requirement permits.

## 4. Domain model

### Opportunity

| Field | Meaning |
|---|---|
| `id` | Unique `opp-…` identifier. |
| `title`, `description` | Human-readable opportunity. |
| `industry`, `category` | Classification. |
| `customer_problem` | The problem the opportunity addresses; required at discovery. |
| `proposed_solution` | The proposed response; required at discovery. |
| `target_audience` | Identified customer group. |
| `signal_type` | Market trend, complaint, industry change, technology change, emerging market, competitor weakness, operational problem, product gap, AI opportunity or operator brief. |
| `discovered_at`, `updated_at` | Timestamps. |
| `stage` | Lifecycle stage; progression is validated. |
| `source_refs`, `evidence` | Source provenance and evidence records. |
| `confidence`, `uncertainty` | Derived from evidence quality/freshness and recorded gaps. |
| `market`, `customer`, `competition` | Analysis snapshots. |
| `feasibility`, `financials`, `score` | Assessment snapshots. |
| `experiments` | Proposed/approved/completed validation tests. |
| `report`, `verdict`, `verdict_rationale` | Derived final decision and its evidence-backed explanation. |
| `history` | Every lifecycle transition with time, actor and reason. |
| `metadata`, `duplicate_key` | Research counts/errors, inputs, paused-from stage and deduplication key. |

### Evidence

`EvidenceRecord` requires a non-empty claim. A measured record (`primary`,
`secondary`, `estimate`) must have a `SourceRef`. A primary record must name its
supplier. `SourceRef` stores title, URL, source type, publisher, retrieval time,
excerpt and supplier. Source age is measured against the 90-day current-market
window.

Evidence quality is explicit:

| Quality | Counts toward measured validation? | Typical use |
|---|---|---|
| `primary` | Yes, when current and sourced | Supplied interview, survey, feedback or first-party record |
| `secondary` | Yes, when current and sourced | Public report, pricing page, competitor source |
| `estimate` | Yes for some evidence-quality signals, but never sufficient alone | Labelled model/operator estimate |
| `synthesis` | No | Derived summary |
| `assumption` | No | Operator belief or unscoped idea |

`MarketSize` is `measured`, `estimated` or `unavailable`. A missing market-size
figure is not replaced by a plausible number.

## 5. Lifecycle

```
DISCOVERED
  → RESEARCHING
  → EVIDENCE_COLLECTION
  → FEASIBILITY_ASSESSMENT
  → EXPERIMENT_PLANNING
  → VALIDATION
  → VALIDATED | CONDITIONALLY_VALIDATED | INSUFFICIENT_EVIDENCE
              | NOT_VIABLE | REQUIRES_FURTHER_RESEARCH

Any live stage → PAUSED (resume returns to the remembered stage)
Any live stage → REJECTED (reason required) → ARCHIVED
Any live stage → ARCHIVED
Validated / not-viable / rejected → ARCHIVED
```

Every transition goes through `transition()`, which validates the allowed target,
records a `LifecycleEvent`, updates `updated_at` and manages the remembered
`paused_from` value. A `VALIDATED`, `NOT_VIABLE`, `REJECTED` or `ARCHIVED` record
is immutable for evidence and assessment; it must be archived or a new
opportunity created. `CONDITIONALLY_VALIDATED` and `INSUFFICIENT_EVIDENCE` remain
open to further evidence and reassessment.

An agent can read a summary, but it cannot call a "mark validated" operation.
`VALIDATED` is only produced by `evaluate()` after the evidence rules pass, and
the stage move to the matching decided stage is a recorded transition.

## 6. Evidence rules for a validated verdict

`decide_verdict()` checks, in order:

1. **Disqualifying constraints** → `NOT_VIABLE` if the base scenario has no
   positive contribution margin, technical feasibility is below 0.2, a legal
   blocker is recorded, or feasibility records at least four dependency risks.
2. **Current sourced evidence count** → `INSUFFICIENT_EVIDENCE` if fewer than two
   current, sourced, non-assumption records exist, or no score can be computed.
3. **Eligibility** → `VALIDATED` only when:
   - the weighted score is at or above the threshold (default 0.60);
   - evidence coverage is at least 80 % of the model's weight;
   - primary customer evidence exists;
   - market/demand evidence exists;
   - at least one sourced competitor or alternative exists (the search is marked
     complete for the recorded sources);
   - the base financial scenario is calculated;
   - feasibility is complete (every dimension has an input);
   - no evidence source is stale;
   - there are no score disqualifiers.
4. **Conditional** → `CONDITIONALLY_VALIDATED` only when current primary
   customer evidence, current market evidence, a current competitor record, a
   calculated base scenario, complete feasibility and a score above threshold all
   exist, but a non-critical coverage/dimension condition remains.
5. Otherwise → `REQUIRES_FURTHER_RESEARCH`.

An incomplete competitor search can never satisfy rule 3, and an assumption can
never satisfy the "current sourced" requirement. A high weighted score with
missing evidence still fails eligibility.

## 7. Scoring methodology

Default weights (sum 1.00):

| Dimension | Weight | Input |
|---|---:|---|
| Demand evidence | 0.17 | Current measured demand/customer/market records; primary weighted highest |
| Problem severity | 0.11 | Supplied 1–5 customer severity ratings |
| Market accessibility | 0.09 | Target audience plus researched segment/demand |
| Competitive intensity | 0.07 | Sourced competitor count; lower intensity scores higher |
| Differentiation | 0.08 | Explicit supplied 0–1 judgement |
| Technical feasibility | 0.12 | Measured hardware fit and supplied complexity/technical evidence |
| Financial feasibility | 0.12 | Base contribution margin, profit and break-even |
| Execution complexity | 0.06 | Inverse of supplied complexity |
| Risk exposure | 0.08 | Feasibility blockers, gaps and available scores |
| Evidence quality | 0.10 | Source count, primary count and freshness (assumptions excluded) |

`weighted_score = Σ(score × weight) / Σ(available weight)`. Coverage is
`Σ(available weight) / Σ(all weight)`. A dimension with no input has score
`None`, appears in `missing`, and lowers coverage; it is not silently filled with
0.5. Weights and threshold are constructor-configurable through the service;
`status()` reports the threshold and the service exposes the active weights in
the score record.

## 8. Financial methodology

For each scenario:

```
monthly revenue       = price × units_per_month
contribution margin   = price − variable_cost_per_unit
monthly gross profit  = contribution margin × units_per_month
monthly opex          = fixed + infrastructure + marketing
monthly net profit    = gross profit − opex
break-even units      = fixed ÷ contribution margin   (None when margin ≤ 0)
break-even revenue    = break-even units × price
forecast revenue      = monthly revenue × months_to_forecast
customer LTV          = contribution margin × lifetime months
CAC payback months    = customer acquisition cost ÷ contribution margin
cash required         = initial investment + development cost + one month opex
```

The base scenario requires price, monthly units, variable cost and fixed monthly
cost. If any is missing the scenario is `calculated=False`, all derived fields
are `None`, and the missing names are returned. `initial_investment` and
`development_cost` are required for `cash_required`; a partial cash figure is
returned as `None`. Optional infrastructure/marketing costs are exposed as
`null` and only treated as zero in the arithmetic; the assumptions note says so.

Unless all three scenarios are supplied, conservative and optimistic are derived
from the base with explicit multipliers:

- conservative: 0.6× units, 0.9× price, 1.15× costs;
- optimistic: 1.4× units, 1.05× price, 0.9× costs.

All figures are labelled modelled estimates, never actual revenue or guaranteed
profit. A price below variable cost produces a visible negative contribution and
a `NOT_VIABLE` verdict.

## 9. Feasibility methodology

Dimensions: technical, operational, financial, market, legal, resources,
execution and dependencies.

- **Technical** uses supplied RAM/GPU requirements and complexity, checked
  against the live `HardwareMonitor` reading used by the resource governor. No
  declared requirement means "not supplied"; it is not scored as average.
- **Resources** uses measured available RAM and CPU. A GPU is only "available"
  when the hardware monitor actually detects one.
- **Market** is based on current primary customer evidence or secondary demand
  evidence; no demand evidence means unavailable.
- **Financial** comes from the base scenario's contribution, profit and
  break-even signals.
- **Operational, legal, execution, differentiation and dependencies** come from
  explicit operator inputs. Missing inputs are reported as missing.
- Legal blockers and four-plus dependency risks are surfaced as blockers and
  feed `NOT_VIABLE`.

## 10. API

All routes require the existing authentication dependency. Mutations validate
their bodies and map business errors to real HTTP responses:

| Method | Path | Meaning |
|---|---|---|
| GET | `/business/status` | Service status, limits, boundaries, measured resource snapshot |
| GET | `/business/opportunities` | List/filter (`q`, `stage`, `industry`, `limit`, `offset`) |
| POST | `/business/discovery/scan` | Explore scan for sourced candidate signals; no opportunity is created |
| POST | `/business/opportunities` | Record a sourced brief/signal; duplicate detection |
| GET | `/business/opportunities/compare` | Compare 2–5 opportunities (`ids=a,b`) |
| GET | `/business/opportunities/{id}` | Full evidence, analyses, score, experiments, report, history |
| POST | `/business/opportunities/{id}/research` | One budgeted Explore research pass |
| POST | `/business/opportunities/{id}/evidence` | Add a sourced claim with quality and provenance |
| POST | `/business/opportunities/{id}/assess` | Market/customer/competitor/feasibility/financial/score assessment |
| POST | `/business/opportunities/{id}/experiments/plan` | Generate experiments from actual gaps |
| POST | `/business/opportunities/{id}/evaluate` | Derive verdict and 15-section report |
| POST | `/business/opportunities/{id}/experiments/{exp}/approval` | Record approve/deny |
| POST | `/business/opportunities/{id}/experiments/{exp}/outcome` | Record an approved experiment's measured outcome |
| POST | `/business/opportunities/{id}/pause` | Pause and remember the stage |
| POST | `/business/opportunities/{id}/resume` | Resume the remembered stage |
| POST | `/business/opportunities/{id}/reject` | Reject with a required reason |
| POST | `/business/opportunities/{id}/archive` | Archive, retaining evidence/history |

Examples:

```bash
curl -X POST http://127.0.0.1:8000/business/discovery/scan \
  -H 'Content-Type: application/json' \
  -d '{"query":"agencies complaining about invoice follow-up","signal_type":"customer_complaint"}'

curl -X POST http://127.0.0.1:8000/business/opportunities \
  -H 'Content-Type: application/json' \
  -d '{"title":"Agency invoice follow-up","customer_problem":"Manual chasing","proposed_solution":"Automated reminders","target_audience":"Small agencies","industry":"SaaS","source_reference":"operator brief"}'

curl -X POST http://127.0.0.1:8000/business/opportunities/<id>/assess \
  -H 'Content-Type: application/json' \
  -d '{"financial_inputs":{"price":50,"units_per_month":100,"variable_cost_per_unit":5,"fixed_monthly_costs":500},"feasibility_inputs":{"operational_score":0.8,"legal_score":0.9,"technical_complexity":0.3,"execution_complexity":0.4,"dependency_risks":[]},"assessment_inputs":{"differentiation":0.7}}'
```

Errors: unknown opportunity `404`; duplicate `409` with the existing id; concurrent
run or invalid lifecycle `409`; missing/invalid evidence `422`; unavailable
research provider `503`; unauthorized request `401/403` through the existing
authentication layer.

## 11. Frontend

A **Business** sidebar destination opens the Business Opportunities workspace in
the existing shell. It uses the existing classes and renderer conventions (no new
design system). It provides:

- a discovery scan and sourced brief form;
- search, stage and industry filters;
- opportunity cards with stage, evidence/source count, score, coverage and verdict;
- an evidence viewer that labels primary/secondary/estimate/assumption/stale;
- market, customer and competitor sections, including unavailable market size and
  incomplete competitor search;
- feasibility dimensions with missing reasons and blockers;
- modelled financial scenarios with calculation methodology;
- score dimensions with missing reasons and disqualifiers;
- experiment plans and approval/outcome controls;
- the full report, limitations and lifecycle history;
- lifecycle controls (pause, resume, reject, archive);
- explicit text that no external experiment is executed automatically.

The renderer lives in `web/static/js/render-business.js`; dispatch is
`type === "business"` in `render-utils.js`; listeners and refresh/load logic are
`setupBusinessOpportunities()` in `app.js`.

## 12. Security, reliability and resource governance

- Every route is authenticated; mutation bodies are validated and typed.
- The service never executes external actions. Experiment approval only records
  an operator decision; recording an outcome requires prior approval when the
  experiment declares one. `Experiment.to_dict()` reports
  `external_action_executed: false`.
- Measured evidence requires a source; primary evidence requires a named
  supplier; invalid URLs are refused; research results without an http(s) URL are
  skipped with a warning.
- Research is injected from Explore, not a new network client, and inherits the
  existing privacy/execution-mode policy. Provider failure returns a warning and
  creates no evidence.
- Per-opportunity async locks and an active-operation set reject duplicate
  concurrent research/assessment/evaluation runs; cancellation releases the
  guard. A global discovery scan is also single-flight.
- Budgets: 4 research runs per opportunity, 6 sources per research, 45-second
  research timeout, 20 discovery scans per process, 500 opportunities, 200
  evidence records per opportunity, 20 experiments retained per opportunity.
- Intermediate work is persisted after discovery, evidence, research
  start/completion/failure, assessment, experiment planning, approval/outcome,
  lifecycle moves and evaluation.
- A corrupt stored row is skipped and reported in `status().load_errors`; it does
  not hide valid rows. The store's staged writes remain the durability mechanism.
- Stale sources (older than 90 days) are labelled, excluded from current
  validation eligibility and counted in the report.
- No local model is loaded by this phase. Explore may use the application's
  existing configured planning model, but the deterministic scoring, financial,
  feasibility and report path does not.

## 13. Events and audit

Events published on the existing bus (all traceable by `opportunity_id` where an
opportunity exists):

`business.opportunity.discovered`, `business.discovery.scan.completed`,
`business.research.started`, `business.research.completed`,
`business.research.failed`, `business.evidence.added`,
`business.assessment.completed`, `business.score.calculated`,
`business.experiment.proposed`, `business.experiment.approval.granted`,
`business.experiment.approval.denied`,
`business.experiment.outcome.recorded`, `business.lifecycle.changed`,
`business.report.generated`, `business.validation.completed`.

Audit rows use the existing `AuditLogger` with a `business:<opportunity-id>`
task id, the authenticated actor, the action, stage/verdict/evidence decision
metadata and success/refusal outcome. Research start/failure and every lifecycle
move are audited. The report is ingested through
`KnowledgeManager.ingest_text(..., project="business")`; an ingestion failure is
recorded in metadata and does not undo the validation decision.

## 14. Testing evidence

Phase suite:

```bash
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_phase35_business_opportunity.py -q
```

**55 passed** (exit 0), including 17 regression tests added by the acceptance
audit. It covers transparent financial arithmetic and missing inputs, sourced
discovery, duplicate refusal, provenance enforcement, primary customer
attribution, assumptions and high scores failing validation, missing primary
customer evidence, stale primary evidence (including the current/historical
split), unavailable market size, complete evidence reaching `VALIDATED`,
negative economics and legal blockers reaching `NOT_VIABLE`, invalid direct
lifecycle moves, pause/resume/reject/archive, validated/not-viable/rejected/
archived immutability,
research success/failure/budget/cancellation/concurrency, discovery scan
candidates without invented briefs, experiment approval/outcome enforcement,
event/audit/knowledge recording, list/compare, persistence and corrupt-row
recovery, advisory agent authority, frontend static contracts and the real
FastAPI routes. The audit also pins malformed year/RAM handling, non-finite
values, GPU-only requirements, queued-operation terminal rechecks, partial
financial-input merges, report-version preservation and analysis invalidation.

See the verification report for the exact broader regression counts and static
checks.

## 15. Known limitations

1. **No synthetic idea generation.** Candidate discovery returns sources; an
   operator must record the problem and solution. This is intentional to avoid
   fabricated opportunities.
2. **Explore is the research provider.** If search is unavailable or blocked by
   privacy/execution policy, research fails safely with a warning; business logic
   continues from whatever evidence exists.
3. **Research references are unclassified.** Explore results are stored as
   `other` secondary evidence. They do not automatically count as a market-size,
   customer or competitor claim; an operator or a specific sourced claim must
   classify them.
4. **Customer evidence is supplied, not generated.** No interview, survey,
   purchase intent or testimonial is synthesized.
5. **90-day freshness window.** A long sales cycle may require fresh sources;
   stale evidence remains visible and historical but cannot currently validate.
6. **No external action execution.** Experiments need an operator to run them;
   the system only plans, approves, records and reports.
7. **JSON state.** State is a local JSON snapshot; there is no relational store or
   migration history for this phase. The schema version is `1`.
8. **Scoring weights are constructor-configured.** The API reports the active
   threshold/weights but does not offer a mutable scoring-profile editor.
9. **UI verification.** The workspace is wired and passes the static/contract and
   API tests; this session did not click through every control in a browser.
10. **No full repository suite.** Per project policy, only targeted and relevant
    regressions were run; the exact commands and counts are in the report.

## 16. Operational instructions

1. Start NovaControl normally (the API/web app composes the service).
2. Open **Business** in the sidebar.
3. Scan a permitted source query if desired. The scan returns candidates and
   never creates an opportunity by itself.
4. Record a brief with a source URL or named reference. Record the actual customer
   problem and proposed solution; do not put unverified claims in the solution
   field.
5. Add evidence. Use `primary` only for a real supplied interview/feedback record,
   name `supplied_by`, and include a source reference. Use `secondary` with a URL
   for public research. Use `assumption` for beliefs; it will not validate.
6. Run assessment with explicit financial and feasibility inputs. Leave unknown
   values blank; they remain missing.
7. Plan experiments and approve only those you are prepared to run yourself.
8. Evaluate. A verdict is derived from the recorded evidence; an
   `INSUFFICIENT_EVIDENCE` or `REQUIRES_FURTHER_RESEARCH` verdict means collect
   the named gaps, not lower the standard.
9. Read the report and its limitations. Archive or reject with a reason when the
   opportunity is closed.

The service is disabled only if the application is not composed; `GET
/business/status` says whether Explore is configured and lists the active limits.
