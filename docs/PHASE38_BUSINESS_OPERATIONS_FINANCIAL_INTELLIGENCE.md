# Phase 38 — Business Operations & Financial Intelligence

**Status:** implemented and verified with stated limitations  
**Phase:** 38  
**Package:** `src/novacontrol/finops/`  
**Primary records:** one isolated finance document per venture in the JSON namespace `business_finance`

## 1. Purpose

Phase 38 adds a native operating and financial record for the ventures NovaControl
already helps create:

```text
Venture
  → operational objectives, tasks, milestones, workflows and risks
  → revenue booked → invoiced → recognized → collected
  → actual / estimated / committed / projected expenses
  → append-only double-entry journal
  → trial balance, income statement, balance sheet, cash flow
  → working capital, profitability and unit economics
  → budgets and budget-versus-actual
  → forecasts and four-scenario comparison
  → threshold-based business health
  → grounded management reports and an advisory specialist agent
```

It is not a bank, a payment processor, an invoicing provider, an accounting
package replacement or a second ledger for NovaMarket. It records and analyses
business activity through the existing JSON state store, event bus, audit log,
memory, knowledge engine, agent registry, runtime modules, FastAPI surface and
static web shell.

## 2. Repository assessment

The implementation was built against these existing seams:

- `NovaControlApplication` as the composition root.
- `JsonStateStore` for durable state under one new namespace.
- `EventBus` through the application's announce callback.
- `AuditLogger.record` and `MemoryManager.remember` through injected callbacks.
- `KnowledgeManager.ingest_text` for management-report knowledge.
- `AgentRegistry` and the shared `AgentTask` / `AgentResponse` contracts.
- Runtime capability modules and the status surface.
- FastAPI `require_auth`, `ApiSurface`, `ROUTE_CONSUMERS` and the generated API
  reference.
- The static sidebar, panel, renderer dispatch and CSS conventions.
- Phase 35, 36 and 37 services as **read-only readers**, never copied stores.
- NovaMarket's NM-03 append-only double-entry ledger as a design reference only;
  no NovaMarket trading, portfolio or paper-ledger record is imported.

No prior finance, accounting, ERP, budgeting, forecasting or business-health
package existed. No existing service was replaced, and no second event bus,
agent registry, database or frontend framework was added.

The repository has no Phase 38 entry in `ROADMAP.md` or `docs/PHASES.md`. The
user specification is the canonical phase definition; this reconnaissance gap is
reported rather than silently "fixed" by inventing a different roadmap scope.

## 3. Package layout

`src/novacontrol/finops/` contains ten modules:

| File | Responsibility |
|---|---|
| `models.py` | Venture document, chart of accounts, exact-decimal helpers, enums, ids, date/text validation. |
| `ledger.py` | Active-entry/reversal derivation, trial balance, income statement, balance sheet, cash flow. |
| `analytics.py` | Revenue/expense summaries, monthly observations, working capital, profitability, operational dashboard. |
| `forecasting.py` | Conservative/base/optimistic/custom forecasts and scenario comparison. |
| `health.py` | Freshness gate, threshold evaluation, evidence and recommended investigation. |
| `service.py` | Venture, membership, operations, revenue/expense lifecycles, journal, reconciliation, periods, budgets, forecasts, health, reports, dashboard, status. |
| `agent.py` | Advisory read-only `financial-intelligence-agent`. |
| `runtime.py` | `business_finance` capability module. |
| `errors.py` | Typed refusals. |
| `__init__.py` | Public exports. |

Supporting files:

- `src/novacontrol/web/static/js/render-finance.js`
- `tests/test_phase38_finops_service.py`
- `tests/test_phase38_finops_api.py`
- this guide and the implementation report

## 4. Persistence and venture isolation

One durable document per venture is stored under:

```text
namespace: business_finance
schema_version: 1
ventures: [ ... ]
```

Each venture owns its own:

- reporting currency;
- chart of accounts;
- journal entries;
- revenue and expense records;
- operational objectives/tasks/milestones/workflows/risks;
- reconciliations and closed periods;
- budgets, forecasts and scenarios;
- health alerts/history and management reports;
- authorization members;
- event history.

There is no cross-venture account balance, no shared journal and no implicit
currency conversion between ventures. A venture reporting in USD and a venture
reporting in EUR cannot post into each other's accounts.

Amounts are stored as fixed-point decimal strings with two fractional digits.
They are converted to floats only for read models at the API boundary. Bools,
`NaN`, `Infinity`, negative amounts where disallowed and zero where a positive
amount is required are refused.

A corrupt stored venture is skipped and recorded in `status()["load_errors"]`;
it does not prevent the remaining ventures from loading.

## 5. Chart of accounts and double-entry rules

The default venture chart contains:

| Code | Account | Type | Normal side |
|---|---|---|---|
| 1000 | Cash and cash equivalents | asset | debit |
| 1100 | Accounts receivable | asset | debit |
| 1200 | Inventory | asset | debit |
| 1300 | Prepaid expenses | asset | debit |
| 1500 | Equipment and capitalised development | asset | debit |
| 2000 | Accounts payable | liability | credit |
| 2100 | Deferred revenue | liability | credit |
| 2200 | Sales tax payable | liability | credit |
| 2500 | Long-term liabilities | liability | credit |
| 3000 | Owner equity | equity | credit |
| 3100 | Retained earnings | equity | credit |
| 4000 | Revenue | revenue | credit |
| 4100 | Refunds and discounts | contra-revenue | debit |
| 5000 | Cost of goods sold | expense | debit |
| 6000 | Operating expenses | expense | debit |
| 6100–6900 | Category expense accounts | expense | debit |

Every journal entry must:

- have at least two lines;
- name an existing account code;
- use exactly one of a positive debit or a positive credit on each line;
- balance to the cent;
- use the venture's reporting currency;
- carry a cash-flow category of `operating`, `investing` or `financing`;
- have a memo and a date;
- not fall inside a closed period.

Entries are append-only. There is no endpoint that edits or deletes a posted
entry. A correction posts a **new reversal entry** whose debits and credits are
the original's opposite. Reversals cannot themselves be reversed through the
route, and an entry can be reversed only once. Financial statements exclude a
reversed original and include its reversal in the reversal period.

## 6. Revenue lifecycle

Revenue stages are ordered:

```text
booked → invoiced → recognized → collected
```

They are separate observations, not synonyms:

| Stage | Journal effect |
|---|---|
| `booked` | None. A booked record is not revenue and does not touch the ledger. |
| `invoiced` | Debit AR (gross + tax); credit deferred revenue (net); credit tax payable (tax). |
| `recognized` | Debit deferred revenue; credit revenue (net). |
| `collected` | Debit cash (gross + tax); credit AR (gross + tax). |

A record created directly at `recognized` or `collected` posts all stages up to
that point exactly once. Advancing an existing record posts only the stages that
have not been posted. This distinction prevents duplicate invoice and
recognition entries when a recognized record is collected.

Revenue can move forward only. A cancelled record cannot advance. A refund can
be recorded only after recognition or collection, is limited to the recorded net
revenue, and posts:

```text
Debit refunds/contra-revenue; credit cash.
```

The revenue summary reports:

- booked net;
- invoiced net;
- recognized net;
- collected cash;
- deferred revenue (invoiced not recognized);
- outstanding invoices (invoiced not collected);
- overdue receivables (invoice due date passed, no collection);
- refunds;
- tax;
- recurring recognized revenue;
- counts by stage;
- recognized revenue by product and by customer.

Outstanding invoices are never labelled cash. Pipeline deals and forecasts are
never included in any revenue field.

### Idempotency and source references

A client may send `idempotency_key` on revenue, expense, journal, refund and
payment operations. A repeated key returns the existing record with
`deduplicated: true` instead of creating another record or posting another
entry. Refunds and payments without a supplied key derive a deterministic key
from the record, amount and date, so a replay does not double-charge the ledger.
Two genuinely identical same-day refunds/payments should carry distinct keys.

A duplicate `source_ref` for the same `source` is a conflict, not a retry. That
separates "this is the same attempt" from "this is a different document with the
same reference".

Non-reporting currencies require an explicit `fx_rate_to_reporting` **and**
`fx_rate_source`. The original amount, rate, source and reporting amount are
stored. A rate on a record already in the reporting currency is refused rather
than silently accepted.

## 7. Expense lifecycle

Expense statuses are:

```text
actual | estimated | committed | projected
```

Only an **approved actual** is a financial actual. Estimated, committed and
projected records stay visible and labelled but never post to the ledger and are
never included in actual expense totals.

An actual expense begins `approval_status = pending`. Approval:

- requires an approver role and a note;
- posts the accrual exactly once;
- debits the category expense account;
- credits cash only when payment evidence already says paid, otherwise credits
  accounts payable.

Re-approving is safe: an existing accrual is detected and not duplicated. A
failed approval (for example a closed period) leaves the record `pending`, so a
later retry can post it instead of being short-circuited by a stale approval flag.

Payment:

- requires an approved actual;
- cannot exceed the remaining amount;
- posts debit accounts payable, credit cash;
- supports partial payments;
- records `paid_amount`, `payment_status` and `paid_at`;
- never moves money. It records that a payment was observed elsewhere.

An actual expense defaults to `unpaid`. It is not assumed paid merely because it
is actual.

## 8. Statements

All statements derive from posted journal entries only.

### Trial balance

Every account's signed balance is split into debit or credit; total debits must
equal total credits. The response includes exact strings, float display values
and a `balanced` flag. Empty data reports `available: false`.

### Income statement

```text
net revenue       = revenue credits − contra-revenue debits
gross profit      = net revenue − cost of goods sold
operating profit  = gross profit − operating expenses
net profit        = operating profit
```

The response includes revenue/COGS/expense rows, amounts, margins and a reason
when no entry exists for the period.

### Balance sheet

Assets, liabilities and equity are grouped from account balances. Because
current-period activity has not been closed into retained earnings, the
statement exposes `current_period_earnings` as an explicit component and checks:

```text
assets = liabilities + equity + current-period earnings
```

It reports `balanced` rather than hiding an unbalanced result.

### Cash flow

Only cash-account (1000) lines are counted, classified by the entry's explicit
cash-flow category. No category is silently inferred. Closing cash is the cash
account balance at the period end.

### Period close

Closing a period snapshots its income statement and trial balance. Posting any
entry dated inside a closed period is refused, including approvals, payments,
refunds and journal entries. A reversal must be posted in an open period; it
does not change the closed period's preserved snapshot.

## 9. Analytics and working capital

`working_capital` reports:

- cash, AR, inventory, prepaid, AP, deferred revenue and tax payable balances;
- current assets and current liabilities;
- working capital;
- current ratio and quick ratio;
- receivables, payables and inventory turnover;
- DSO, DPO, DIO and cash conversion cycle;
- cash runway from observed net cash burn.

Each ratio is returned as a metric object:

```json
{
  "available": false,
  "status": "NOT_CALCULABLE",
  "value": null,
  "reason": "The required inputs were not recorded.",
  "method": "...",
  "kind": "actual | estimate",
  "note": "..."
}
```

A ratio is not fabricated from a missing denominator. Runway requires at least
two observed months of cash flow and positive burn.

`profitability` reports revenue, gross/operating/net profit, margins, revenue per
product, cost per transaction, break-even revenue, ARPC, CAC, LTV and payback.
CAC/LTV/payback require Phase 37 customer evidence; without it they remain
`NOT_CALCULABLE`.

`operations_dashboard` reports recorded tasks, completion rate, blocked and
overdue tasks, average delivery days, average resource utilization, operational
cost, open risks, objectives, milestones, workflows and dependency issues. It
does not infer that a scheduled task was completed.

Monthly series report `observed: false` for months without records; they are not
filled with invented zero observations.

## 10. Budgets

A budget has:

- name, scope, period and version;
- one or more lines with label, account code, category, product/department and
  amount;
- optional per-line overrun threshold;
- draft/approved/rejected status and approver note;
- a revision link to the previous version.

Budget-versus-actual compares an approved budget line only with **approved actual
expenses** in the period. Draft or rejected budgets display as comparisons but
are not authorized spending. A revision creates a new draft version; it never
mutates the approved version. Revision lines go through the same validator as
creation, without temporarily persisting a throwaway budget or emitting a false
`budget_created` event.

## 11. Forecasts and scenarios

Forecasts report:

- scenario, horizon, as-of date and currency;
- starting cash;
- data sources and the number of observed months;
- operator assumptions;
- method;
- confidence and a note that confidence is evidence quantity, not measured
  accuracy;
- limitations and missing inputs;
- monthly rows and totals;
- cash runway within the horizon.

A forecast requires at least two observed months of recognized revenue and two
observed months of approved actual expenses. Missing history returns
`available: false`, not a model output. A forecast is never posted and never
counted as revenue.

Scenario comparison returns four independent forecasts: conservative, base,
optimistic and custom. Each carries its own factors and assumptions.

## 12. Business health

`refresh_health` is deterministic and evidence-backed:

- it does not use an LLM;
- it does not create an alert from missing data;
- it does not create an alert from stale data;
- it does not create a threshold alert unless the metric and threshold are
  present;
- it records the observed value, threshold, severity, evidence, timestamp and a
  recommended investigation;
- it deduplicates open alerts by metric and appends an update history;
- it preserves the last 24 health snapshots.

Configured thresholds include cash runway, revenue concentration, expense
growth, revenue decline, CAC growth, gross-margin decline, churn, overdue
receivable days, budget overrun, infrastructure growth, blocked tasks and stale
data days.

An empty venture reports `data_fresh: false` with a "no record observed" reason
and zero alerts. A stale venture reports its latest observed date and creates no
trend alert. A "no threshold breach" informational row may be returned for fresh
data, but it is not stored as an open alert.

Revenue concentration ignores the `unassigned` customer bucket: missing customer
evidence is not one giant customer.

## 13. Management reports and advisory agent

A management report includes:

- executive summary;
- revenue and expense sections;
- income statement and cash flow;
- profitability and working capital;
- budgets and latest forecast;
- operational performance;
- risks and open alerts;
- recommendations with evidence and `requires_human_authorization: true`;
- methodology and limitations;
- nine explicit sections labelled `actual`, `estimate`, `forecast` or
  `observation`.

A forecast appears only as a forecast section, never as revenue or an actual.

`financial-intelligence-agent` is read-only and advisory. It reports recognized
revenue, collected cash, outstanding/overdue invoices, approved actual expenses,
unapproved actuals, net profit, runway, alerts and evidence. It cannot pay,
purchase, approve an invoice, change banking details, commit spending or mutate a
finalized record. Its responses carry `advisory_only: true` and
`external_action_executed: false`.

The service's configured actor receives a read-only membership on creation so
this internal specialist can analyse ventures created through an HTTP principal.
It does not receive a mutation role and does not bypass the public role checks.

## 14. API

The phase adds **49 authenticated method/path pairs** (42 unique paths) under
`/finance`:

```text
GET  /finance/status
GET  /finance/ventures
POST /finance/ventures
GET  /finance/ventures/{venture_id}
GET  /finance/ventures/{venture_id}/dashboard
POST /finance/ventures/{venture_id}/status
POST /finance/ventures/{venture_id}/members
GET  /finance/ventures/{venture_id}/operations
POST /finance/ventures/{venture_id}/objectives
POST /finance/ventures/{venture_id}/tasks
POST /finance/ventures/{venture_id}/tasks/{task_id}
POST /finance/ventures/{venture_id}/milestones
POST /finance/ventures/{venture_id}/workflows
POST /finance/ventures/{venture_id}/risks
GET  /finance/ventures/{venture_id}/revenue
GET  /finance/ventures/{venture_id}/revenue/summary
POST /finance/ventures/{venture_id}/revenue
POST /finance/ventures/{venture_id}/revenue/{revenue_id}/advance
POST /finance/ventures/{venture_id}/revenue/{revenue_id}/refund
GET  /finance/ventures/{venture_id}/expenses
POST /finance/ventures/{venture_id}/expenses
POST /finance/ventures/{venture_id}/expenses/{expense_id}/approval
POST /finance/ventures/{venture_id}/expenses/{expense_id}/payment
GET  /finance/ventures/{venture_id}/journal
POST /finance/ventures/{venture_id}/journal
POST /finance/ventures/{venture_id}/journal/{journal_id}/reverse
POST /finance/ventures/{venture_id}/reconciliations
POST /finance/ventures/{venture_id}/periods/close
GET  /finance/ventures/{venture_id}/trial-balance
GET  /finance/ventures/{venture_id}/statements/income
GET  /finance/ventures/{venture_id}/statements/balance-sheet
GET  /finance/ventures/{venture_id}/statements/cash-flow
GET  /finance/ventures/{venture_id}/statements/comparison
GET  /finance/ventures/{venture_id}/cash-flow
GET  /finance/ventures/{venture_id}/working-capital
GET  /finance/ventures/{venture_id}/profitability
GET  /finance/ventures/{venture_id}/budgets
POST /finance/ventures/{venture_id}/budgets
POST /finance/ventures/{venture_id}/budgets/{budget_id}/approval
POST /finance/ventures/{venture_id}/budgets/{budget_id}/revisions
GET  /finance/ventures/{venture_id}/budgets/{budget_id}/report
GET  /finance/ventures/{venture_id}/forecasts
POST /finance/ventures/{venture_id}/forecasts
POST /finance/ventures/{venture_id}/scenarios
GET  /finance/ventures/{venture_id}/alerts
POST /finance/ventures/{venture_id}/health/refresh
POST /finance/ventures/{venture_id}/alerts/{alert_id}/resolve
GET  /finance/ventures/{venture_id}/reports
POST /finance/ventures/{venture_id}/reports
```

HTTP mapping:

- 401 missing/invalid bearer token when a token is configured;
- 403 the acting principal has no role or an insufficient role;
- 404 unknown venture, journal, budget, expense or alert;
- 409 invalid transition, already reversed, duplicated source reference, closed
  period, already paid or another conflict;
- 422 malformed amount/date/body/unknown enum;
- 429 venture/record/journal capacity exhausted.

Every route is authenticated. `ApiSurface`, `ROUTE_CONSUMERS` and
`docs/API.md` are generated from one route list; the finance list, method/path
set and consumer map are pinned by tests. The reference is in sync at **617
routes**.

## 15. Frontend

`render-finance.js` owns the Finance & Operations workspace. It:

- lists ventures and opens one;
- creates a venture with model, currency, description, products and read-only
  Phase 36/37 references;
- records revenue at an explicit stage and valid currency/rate;
- records actual/estimated/committed/projected expenses;
- approves and pays an expense by recorded ID;
- posts and reverses journal entries;
- closes a period;
- creates/approves a budget and compares it with actuals;
- creates forecasts and compares four scenarios;
- refreshes health and lists alerts;
- generates and lists management reports;
- renders the dashboard's business health, revenue, expenses, cash/working
  capital, profitability, statements, six-month observations, budgets and the
  actual/estimate/projection/forecast legend.

Every mutation uses the shared `run(...)` loading/error path and refreshes the
venture, list and status. A mutation never replaces the panel with a raw JSON
card. `renderFinanceList`, `renderFinanceDetail` and `renderFinanceDashboard` are
dispatched from `render-utils.js`.

The panel is deliberately explicit about boundaries: "No payment, purchase, bank
change or external financial commitment is executed here."

## 16. Security and operational safety

- All monetary records are venture-scoped; no query can read another venture's
  ledger by supplying only a record id.
- Role hierarchy: viewer < analyst < approver < admin. Reads require viewer;
  recording revenue/expenses and journal entries requires analyst; approvals,
  reversals, reconciliation and period close require approver; membership and
  operating-status changes require admin.
- Credentials, card numbers, CVVs and banking secrets in a finance payload are
  rejected.
- No bank, payment, purchase, invoice-approval, external-commitment or
  bank-detail endpoint exists.
- No unreviewed external message is sent.
- Audit, event and memory callbacks are optional seams; a callback failure never
  rolls back a financial record.
- The agent is advisory and read-only; the internal reader role cannot mutate.
- No NovaMarket trading or paper-ledger data enters venture accounting.

## 17. Known limitations

1. **No live external financial provider.** Payments, bank feeds and invoice
   delivery are intentionally disabled. "Paid" is a recorded observation, not a
   transfer executed by NovaControl.
2. **No live Phase 35/36/37 linkage data in this workspace.** Readers are wired,
   tests use injected fakes, and missing links are reported rather than copied or
   invented. The customer reader enriches CAC/LTV/health when a Phase 37
   workspace exists.
3. **The ledger is a single-entry-per-stage operational ledger**, not a
   tax-compliance package. It has no multi-book consolidation, tax filing,
   payroll, inventory costing method or foreign-currency revaluation engine.
4. **Forecasts are simple deterministic models.** They use observed monthly
   averages, scenario factors and stated growth. No seasonality, ARIMA,
   backtested accuracy or machine learning forecast is claimed.
5. **The balance sheet includes current-period earnings as an explicit component**
   because closing entries into retained earnings are not automated.
6. **Monthly observations exclude the current incomplete month.** A current-month
   record is reflected in the live statements and summaries but does not become a
   "complete month" observation until the month ends.
7. **No batch import/export or CSV bank reconciliation.** Reconciliation records
   a statement balance against the ledger; it does not ingest a statement file.
8. **The UI uses JSON textareas for journal and budget lines.** The service
   validates them, but there is no line-by-line journal grid.
9. **No Phase 38 roadmap entry existed.** The user prompt is the authority and the
   gap is recorded in the implementation report.

## 18. Tests and evidence

- `tests/test_phase38_finops_service.py` — 22 service tests covering isolation,
  roles, revenue stages, refunds, expense approval/payment, journal rules,
  reversals, closed periods, statements, working capital, budgets, forecasts,
  health freshness, operations, Phase 37 reader behavior, reports, capacity,
  persistence, audit/memory seams and the advisory agent.
- `tests/test_phase38_finops_api.py` — 7 HTTP tests covering the served
  workspace and renderer wiring, authentication, the 49-route contract, a full
  lifecycle, journal/period/budget/forecast/health/report flows, error mapping
  and operations.

Exact results are recorded in
[PHASE38_IMPLEMENTATION_AND_VERIFICATION_REPORT.md](PHASE38_IMPLEMENTATION_AND_VERIFICATION_REPORT.md).

## 19. Using the workspace

1. Start the server on a loopback port:

   ```bash
   PYTHONPATH=src python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
   ```

2. Open the **Finance** tab.
3. Create a venture with its reporting currency.
4. Record revenue only at the stage actually observed.
5. Record an actual expense, then approve it when the evidence is reviewed.
6. Post journal entries for opening balances, funding or corrections.
7. Close a period only after its entries are reviewed.
8. Refresh health after fresh records exist.
9. Treat every forecast as a scenario, never as revenue.
