# Phase 39 — Portfolio Manager for Multiple AI-Run Ventures

**Status:** implemented, integrated, tested and live-checked; **PASS WITH LIMITATIONS**  
**Date:** 2026-11-24  
**Package:** `novacontrol.portfolio`  
**Persistence namespace:** `portfolio_management`  
**Frontend:** **Portfolio** workspace (`render-portfolio.js`)  
**API surface:** 49 authenticated `/portfolio*` method/path pairs

## 1. Purpose

Phase 39 adds portfolio-level intelligence above the four business phases. It does
not replace them and does not copy their records.

```
Phase 35 opportunity → Phase 36 product → Phase 37 customer → Phase 38 finance
        └──────────────────── portfolio reference and evidence ───────────────────┘
                                      ↓
                 evaluation · risk · allocation · scenarios · decisions
                                      ↓
                     frozen reports · alerts · portfolio dashboard
```

The portfolio is a coordination and assessment layer. It may recommend, rank,
simulate, warn and freeze a report. It cannot move money, buy anything, change a
budget, alter a finalized ledger, launch a product, send a campaign, close a
venture or delete a business record.

## 2. Architecture

| Layer | Module | Responsibility |
|---|---|---|
| Models | `models.py` | Lifecycle, roles, allocation actions, validation, metric helpers |
| Errors | `errors.py` | Not found, conflict, authorization, approval, lifecycle gate |
| Engines | `engines.py` | Deterministic metrics, evaluation, risk, allocation, scenario, recommendation logic |
| Registry | `service.py` | Portfolio and venture references, roles, persistence, snapshots, lifecycle |
| Intelligence | `intelligence.py` | Analytics, rankings, risks, resources, scenarios, decisions, knowledge, alerts, reports, dashboard |
| Facade | `service_facade.py` | `PortfolioService` composition |
| Agent | `agent.py` | Advisory, read-only `portfolio-manager-agent` |
| Runtime | `runtime.py` | `PortfolioModule`, capability declaration, event subscriptions |
| HTTP | `api/app.py` | 49 authenticated routes and exception mapping |
| UI | `render-portfolio.js` | Portfolio workspace, venture drill-down, actions and report state |

The package reuses:

- `JsonStateStore` for one versioned portfolio document;
- the application-wide `EventBus`, `AuditLogger`, `MemoryManager` and
  `KnowledgeManager`;
- the existing `AgentRegistry` and `RuntimeModule` seams;
- Phase 38 `FinOpsService` as the authoritative financial reader;
- Phase 36 and Phase 37 services as source readers for product and customer
  evidence;
- the existing FastAPI authentication middleware, `ApiSurface`,
  `ROUTE_CONSUMERS` and generated API reference.

No second event bus, agent registry, ledger, database, budget engine, scheduler
or frontend framework was introduced. NovaMarket trading records are not part of
venture accounting and are not imported.

## 3. Portfolio and venture model

A portfolio stores:

| Field | Meaning |
|---|---|
| `id`, `name`, `description` | Identity and human context |
| `owner` | Creating principal; always the admin |
| `members` | Actor, role, visible-venture policy |
| `base_currency` | Reporting currency |
| `reporting_period` | Declared reporting period |
| `objectives` | Bounded plain-text objectives |
| `risk_preferences` | Minimum runway, budget overrun tolerance |
| `resource_policy` | Allocation policy labels and limits |
| `status` | `active`, `paused`, `archived` |
| `ventures` | References only: Phase 38 id plus Phase 35/36/37 links |
| `fx_rates` | Explicit sourced rates; base rate is fixed at 1.0 |
| `monitoring` | Intervals, staleness and thresholds |
| `evaluations`, `risks`, `allocations`, `scenarios`, `recommendations`, `decisions`, `resources`, `dependencies`, `knowledge`, `alerts`, `reports` | Portfolio-owned analysis and coordination records |
| `events` | Bounded portfolio event log |
| `created_at`, `updated_at` | Timestamps |

A venture reference holds `id`, lifecycle status and history, priority,
visibility, links and cached *identifiers* only. Revenue, expense, customer,
product and budget records stay in their owning phase.

**Isolation rules**

- One Phase 38 venture id may be bound to one portfolio. A second registration
  is `PortfolioConflict`.
- A portfolio may contain many ventures; many portfolios may exist.
- De-registration is not exposed; an archived venture is terminal inside the
  portfolio.
- A restricted venture is omitted from analytics, rankings, lists and reports
  unless the actor is explicitly allowed it.
- No copied ledger is stored, so a source correction is visible on the next
  snapshot rather than frozen into portfolio state.
- Duplicate registration, copied statement rows and inter-venture transfers are
  not treated as revenue.

## 4. Roles and authorization

| Role | Capabilities |
|---|---|
| `viewer` | Read visible portfolio, ventures, analytics and reports |
| `analyst` | Generate evaluations, risks, alerts and reports |
| `approver` | Approve lifecycle transitions, allocation recommendations and decisions |
| `admin` | Manage portfolio metadata, members, status, FX rates and monitoring |

The owner is always `admin`. An internal agent actor is added as a `viewer`.
Changing the owner's role is refused. Pausing or archiving a portfolio requires
`approved=true`. Every route authenticates with the existing bearer token.

## 5. Lifecycle management

Statuses:

`OPPORTUNITY_DISCOVERY → VALIDATION → PRODUCT_DEVELOPMENT → INTERNAL_TESTING →
READY_FOR_LAUNCH → CUSTOMER_ACQUISITION → OPERATING`, plus `UNDER_REVIEW`,
`PAUSED`, `RESTRUCTURING`, `WINDING_DOWN`, `ARCHIVED`.

Gates read actual source state:

| Target | Required evidence |
|---|---|
| `VALIDATION` | A recorded Phase 35 opportunity |
| `PRODUCT_DEVELOPMENT` | Opportunity is `validated` or `conditionally_validated` |
| `INTERNAL_TESTING` | Product is approved for launch and at least one test exists |
| `READY_FOR_LAUNCH` | Product readiness/release approval |
| `CUSTOMER_ACQUISITION` | Approved release/readiness, Phase 37 launch authorization, customer workspace, explicit approval |
| `OPERATING` | The customer gates and actual Phase 38 activity, explicit approval |
| `PAUSED`, `RESTRUCTURING`, `WINDING_DOWN` | Evidence plus explicit approval |
| `ARCHIVED` | Explicit approval; terminal |

An unvalidated opportunity cannot become an operating venture. A product without
its release gate cannot be treated as launched. A proposed deal is never
recognized revenue. Every transition records `from`, `to`, timestamp, actor,
reason, evidence and approval state. History is persisted and survives restart.

## 6. Consolidated reporting methodology

`portfolio_analytics()` builds one snapshot per visible venture. A snapshot reads
Phase 38 detail, income statement, expense summary, cash flow, working capital,
budgets and budget report; it also reads linked product, opportunity and customer
records.

Consolidation rules:

1. **Currency.** Each non-base amount needs an explicit rate with a source and
   an as-of date on or before the period end. A missing, malformed or
   future-dated rate excludes the venture from the base-currency total and
   reports the reason.
2. **Currency buckets.** Every venture still appears in its own currency bucket,
   so an excluded venture is visible rather than silently dropped.
3. **Periods.** Analytics uses an explicit start/end; metrics carry source
   timestamps. A prior period is computed separately for growth comparisons.
4. **Completeness.** The aggregate carries `available`, `complete`, `sources`,
   `excluded`, `missing`, `missing_metrics` and `incomplete_data`; each metric
   inside it carries `available`, `value`, `kind`, `method`, `source` and a
   `reason`. A zero is only shown when a real zero was read. If any contributing
   venture lacks a source metric, that metric's total is unavailable rather than
   summed as zero, and the affected currency bucket is marked incomplete.
5. **No double count.** One venture id, one snapshot row and one recognition
   basis. The duplicate-binding check rejects a second portfolio binding, and a
   duplicate reference found in stored data is removed and reported on load.
6. **No incompatible addition.** Revenue, actual expenses, committed/estimated
   expenses, cash, net cash flow and runway remain distinct labelled values.
7. **Customer/product concentration** uses recognized revenue by named customer
   reference and product reference, not pipeline values.

`profitability` is not calculable when revenue is zero, unavailable or
incomplete; the dashboard states why rather than reporting zero profit.

## 7. Evaluation and ranking

The evaluation engine builds explainable components across four dimensions:

- **Financial:** revenue growth, gross margin, operating margin, net cash flow,
  runway, budget performance.
- **Market:** conversion, retention, demand evidence, customer volume.
- **Operational:** delivery progress, product reliability, support workload,
  infrastructure efficiency.
- **Strategic:** Phase 35 opportunity score, scalability, objective alignment,
  resource requirement.

Each component has `name`, `dimension`, `score`, `method`, `confidence`,
`evidence`, `source_metrics` and `missing`. The venture result adds weights,
`raw_weighted_score`, `coverage`, `score`, `eligible_for_ranking`, `confidence`,
`missing`, source timestamps and methodology.

`score = raw weighted score × coverage`. A policy minimum coverage (default 0.5)
decides rankability. A venture with sparse data gets a low or `None` score and is
excluded from the ranking rather than ranked highly. Weights are configurable by
policy (`balanced`, `growth`, `cash`, `risk`, `custom`) and persisted with each
evaluation. Different policies can be compared without mutating the source data.

## 8. Risk management

Assessed risks cover financial, liquidity, revenue concentration, customer
concentration, market, operational, product delivery, technology/vendor
dependency, compliance, security and execution exposure. A risk carries category,
severity, likelihood, impact, scope (`venture`, `multi_venture`, `portfolio`),
observed condition, evidence, mitigation, status, timestamps and source.

Correlation is reported only when the same evidenced condition appears in more
than one venture (for example, the same customer concentration category at high
severity). The payload explicitly does **not** claim statistical diversification;
no covariance, VaR or portfolio variance is computed.

Manual risks require evidence and can be resolved with a note. Assessment never
changes a source record.

## 9. Resource allocation

`propose_allocations()` compares approved Phase 38 budgets, actual expenses,
cash, runway, growth, risk and priority. It produces one of:

`increase`, `maintain`, `reduce`, `delay`, `pause`, `validate_further`,
`reallocate`.

Each item carries evidence, expected benefit, estimated cost, risks, assumptions,
alternatives, confidence and `approval_required=true`. A batch is `proposed`;
approving it records a recommendation decision only. `applied` remains false and
the Phase 38 budget is byte-for-byte unchanged. A budget change must still follow
Phase 38 approval.

Shared resources have a hard capacity, unit and per-venture allocations.
Over-capacity requests are refused. Utilization is calculated from recorded
allocations, not invented.

## 10. Scenario simulation

Scenarios are deterministic and labelled `simulated`:

- new venture;
- launch delay;
- development spend increase;
- operating expense reduction;
- customer-acquisition or marketing change;
- pause;
- scale;
- revenue shortfall;
- infrastructure cost increase;
- custom assumptions.

Each result carries baseline, assumptions, horizon, projected revenue, expenses,
net, ending cash, runway, venture contribution and missing inputs. `kind` is never
`actual`. `applied=false`, `requires_human_authorization=true`, and notes repeat
that the result is not a guarantee. Custom assumptions are bounded and validated.

Scenario mechanics are explicit: a launch delay shifts revenue to the post-delay
months while costs continue; a pause stops revenue while operating and one-time
pause costs remain; a new-venture scenario without a target creates one synthetic
venture with zero actual history instead of duplicating existing ventures.

## 11. Strategic decision engine

Recommendations include continue, increase validation, prioritize product work,
expand acquisition, reduce operating cost, maintain allocation, pause development,
restructure, wind down and explore a new opportunity.

Each recommendation carries context, evidence references, relevant ventures,
financial and operational implications, risks, alternatives, assumptions,
expected outcome, confidence and approval requirements.

A recommendation is **not** a decision. `propose_decision()` creates a `proposed`
record. `decide()` requires the `approver` role and a human note. The approved
record states `applied=false` and "nothing was executed". All decisions and
recommendations remain traceable in history.

## 12. Multi-venture coordination

A portfolio exposes logical responsibilities that reuse the existing agent
registry:

- Portfolio Manager (`portfolio-manager-agent`);
- venture operations, financial analysis, product, growth, risk and strategy
  responsibilities mapped to existing role contracts or the portfolio agent.

The coordination view aggregates:

- venture priorities;
- cross-venture dependencies and cycle detection;
- shared-resource allocation and conflicts;
- escalations from high/critical risks;
- progress at portfolio level.

A venture-scoped actor cannot read a restricted venture's data: detail and
dashboard headers are sanitized, and restricted rows are omitted from lists,
analytics, rankings, reports, risks, allocations, recommendations, decisions,
knowledge, coordination, resources and history. Reports store only the
generator's visible venture records, persist `visible_venture_ids`, and re-check
visibility on read. The portfolio agent receives only read methods and cannot
approve, allocate, execute or mutate a source record.

## 13. Knowledge and learning

Knowledge kinds are `verified_fact`, `observation`, `interpretation`,
`hypothesis` and `recommendation`. Every record names its source, evidence,
creator, timestamp, visibility and confidentiality classification.

Cross-venture promotion requires an explicit `authorized_for_cross_venture=true`
plus an admin. A record classified `customer_confidential` cannot be promoted.
Portfolio knowledge is ingested into the existing `KnowledgeManager`; venture
knowledge stays scoped. Verified facts and observations are never silently
converted into recommendations.

## 14. Monitoring and alerts

Monitoring covers lifecycle changes, financial deterioration, runway thresholds,
concentration, budget overruns, churn, sales conversion, delivery delay,
operational/security incidents and resource conflicts.

Thresholds and intervals are persisted per portfolio. Every alert carries
portfolio id, venture id, metric, observed condition, severity, evidence,
timestamps, recommended action, status and occurrences.

Stale source data suppresses threshold alerts instead of raising a false alarm.
Refreshing updates the stored open alert (occurrence count, last seen, evidence)
rather than duplicating it. Resolving an alert records a note and does not change
the source.

The event-driven scheduled refresh runs as a reserved runtime job (`system=True`)
because the internal agent actor is deliberately a read-only viewer. The HTTP
alert-refresh route requires an authenticated analyst or higher; no route can
request the system path.

## 15. Reports

`generate_report()` supports `daily`, `weekly`, `monthly`, `quarterly` and
`custom` (explicit start/end) periods. All 12 required sections are present:

1. portfolio overview;
2. venture lifecycle distribution;
3. revenue and expense summary;
4. cash flow and runway;
5. profitability comparison;
6. customer acquisition and retention;
7. product development status;
8. operational performance;
9. risk assessment;
10. resource allocation;
11. strategic recommendations;
12. outstanding decisions.

A report stores its analytics, ranking, risks, allocations, recommendations,
alerts, pending decisions, source timestamps, generator and a SHA-256 snapshot
hash. It is frozen: later source changes do not rewrite the stored report.

## 16. API

All 49 routes are authenticated. Full method/path descriptions are generated in
[API.md](API.md). Groups:

- portfolio status, create, list, detail, update, status, members, FX, monitoring;
- venture list, register, detail, lifecycle evidence and transition;
- analytics, comparison, evaluation policies, evaluations, ranking;
- risks and resolution;
- allocations and allocation decisions;
- shared resources and allocations;
- scenarios, recommendations, decisions and approvals;
- coordination and dependencies;
- knowledge and promotion;
- alerts, refresh and resolution;
- reports, report snapshots, dashboard and history.

Error mapping:

| Error | HTTP |
|---|---|
| Validation / domain input | 422 |
| Not found | 404 |
| Authorization | 403 |
| Approval required / lifecycle gate | 409 |
| Conflict (e.g. venture already bound) | 409 |
| Source reader unavailable | 503 |

## 17. Security and governance

- Bearer authentication on every route.
- Role checks at the service layer, not only in the UI.
- Restricted ventures removed from all aggregate paths.
- No customer-identifying data is copied into portfolio records; references are
  identifiers.
- Secret-shaped keys and customer PII are rejected from portfolio payloads.
- Audit records for create/update, venture registration, lifecycle, risk,
  resource, scenario, decision, report, alert and knowledge actions.
- Event bus publications use the `portfolio.*` namespace.
- Memory/knowledge integration is optional and injected.
- Hard limits: portfolios, ventures per portfolio, risks, decisions, reports,
  alerts, knowledge and event history.
- Consequential actions are approval-gated and never executed by the service.

The service has **no** funds-transfer, purchase, contract, campaign, launch,
ledger-finalization, venture-closure or record-deletion method.

## 18. Frontend

The **Portfolio** workspace provides:

- portfolio status and creation;
- portfolio list and open action;
- venture registration with Phase 35/36 links and initial lifecycle;
- lifecycle history and restricted-venture count;
- dashboard: overview, lifecycle distribution, venture comparison, capital,
  resources, risks, alerts, rankings, recommendations, scenarios, reports and
  coordination;
- Evaluate/Rank, Assess Risks, Propose Allocations, Refresh Alerts, Strategic
  Advice, Simulate Scenario and Generate Report controls;
- drill-down to the Phase 38 Finance venture workspace;
- explicit `NOT CALCULABLE`, `simulated`, `recommendation` and approval labels.

All calculations come from the API. The browser does not invent a score, total
or projection.

## 19. Tests and evidence

- `tests/test_phase39_portfolio_service.py`: **30 passed**.
- `tests/test_phase39_portfolio_api.py`: **8 passed**.
- Combined Phase 39: **38 passed**.
- Phase 35–38/application/web/route/reference/agent/event/audit regression:
  **540 passed, 1,648 subtests passed** (before the final audit fixes).
- Affected application/web/route/reference/event/audit regression after the
  final fixes: **253 passed, 1,559 subtests passed**.
- `mypy --no-incremental src/novacontrol/portfolio`: clean.
- Generated API reference: **666 routes**, in sync.
- Real loopback uvicorn workflow: **19/19 checks**.
- Browser: Portfolio panel rendered 3 portfolios, refreshed status and opened a
  dashboard; no new console error was observed after authentication.

Performance on this machine (5 ventures, in-memory store, median / max):

| Operation | Median | Max |
|---|---:|---:|
| Consolidated analytics | 3.338 ms | 5.463 ms |
| Evaluate + rank | 3.525 ms | 4.339 ms |
| Risk assessment | 3.515 ms | 4.406 ms |
| Allocations | 4.356 ms | 5.585 ms |
| Dashboard | 13.795 ms | 15.897 ms |
| Monthly report | 46.773 ms | 99.871 ms |
| Persist | 12.681 ms | 14.914 ms |
| List portfolios | 0.002 ms | 0.021 ms |

The persisted namespace was 1,142,071 bytes after 5 reports, 5 ventures,
recommendations, alerts and scenarios. A single report is the largest contributor.

## 20. Known limitations

1. **No canonical Phase 39 roadmap entry.** `ROADMAP.md` covers the legacy
   fifteen-phase plan and `docs/PHASES.md` the original build-out; Phase 39 exists
   in the supplied specification and this implementation. The numbering
   reconciliation is recorded rather than inventing roadmap history.
2. **No live Phase 35/36/37 data in the test fixtures.** Integration is verified
   through the real reader seams and synthetic source-shaped records.
3. **No live payment, bank, vendor, campaign or contract provider.** External
   actions are intentionally absent.
4. **FX rates are manual and explicit.** There is no rate provider, hedge,
   translation reserve or tax consolidation.
5. **No statistical risk model.** Correlated risks are evidence matches, not a
   covariance matrix, VaR or diversification claim.
6. **Scenarios are deterministic**, not Monte Carlo or model-based forecasts.
7. **Customer/product concentration is limited to recorded references.** An
   unnamed customer cannot be attributed.
8. **The portfolio agent is advisory and read-only.** It has no approval or
   action authority.
9. **Reports are full snapshots.** They grow the JSON namespace; the cap bounds
   history but a very large portfolio will produce a large document.
10. **No true SQL migrations.** The existing JSON namespace is versioned
    (`schema_version: 1`); there are no database indexes or foreign keys beyond
    service-level referential checks.
11. **The browser live check used a disposable loopback server** and did not
    exercise every button; all primary actions were exercised over real HTTP.
12. **The full repository suite was not run** in this phase. Only the focused and
    relevant regression groups above were run.

## 21. Using the workspace

1. Start the API with `NOVACONTROL_API_TOKEN` set (loopback only).
2. Enter the token in the sidebar.
3. Open **Portfolio**.
4. Create a portfolio with a base currency and objectives.
5. Register an existing Phase 38 venture id, linking its Phase 35/36 ids.
6. Read the lifecycle evidence, then transition only when gates pass.
7. Run Evaluate, Assess Risks, Propose Allocations, scenario simulation,
   Strategic Advice and Generate Report.
8. Open **Open Phase 38 Finance** on a venture to inspect its real ledger.
