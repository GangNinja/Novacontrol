# Phase 37 — Customer Acquisition, Sales & Support

**Status:** implemented and verified with stated limitations  
**Phase:** 37  
**Package:** `src/novacontrol/customer/`  
**Primary records:** one `CustomerWorkspace` per launch-authorized Phase 36 product

## 1. Purpose

Phase 37 makes the customer lifecycle a native NovaControl capability:

```text
Product
  → Target market
  → Customer discovery
  → Lead generation
  → Lead qualification
  → Sales pipeline
  → Customer onboarding
  → Customer support
  → Retention
```

It is not a separate CRM, a second orchestration system or a new database. It
reads the Phase 36 product record, writes through the existing JSON state store,
publishes existing events, uses the existing audit and memory seams, and exposes
the lifecycle through the existing FastAPI and static-web surfaces.

## 2. Repository assessment

The implementation was built against the running seams:

- Phase 35 `BusinessOpportunityService` for the opportunity context retained by a
  product.
- Phase 36 `ProductService.detail()` as the **only** product reader.
- `JsonStateStore` for durable state, under the `customer_growth` namespace.
- `EventBus` through the application's existing announce callback.
- `AuditLogger` and `MemoryManager` through injected callbacks.
- `KnowledgeManager.ingest_text` for approved support knowledge.
- `AgentRegistry` for one advisory specialist.
- Runtime capability modules.
- FastAPI `require_auth`, `ApiSurface`, `ROUTE_CONSUMERS` and the generated API
  reference.
- The static sidebar, panel, renderer dispatch and CSS conventions.

No pre-existing CRM, contact manager, ticket system or outbound email sender was
found. No completed phase was rewritten, and no second CRM or framework was
created.

## 3. Product authorization

A workspace can only be created from a product whose Phase 36 record says:

```text
summary.state == "APPROVED_FOR_LAUNCH"
and
launch_approval.approved is True
```

`READY_FOR_RELEASE_REVIEW`, `DEVELOPMENT`, `BLOCKED`, `ARCHIVED` and any product
without a named launch approval are refused with `ProductNotLaunched` (HTTP 403).
The product snapshot is refreshed explicitly through
`POST /customer/workspaces/{product_id}/refresh`; a refresh that no longer passes
the launch-authorization rule updates the stored snapshot and leaves it visibly
unauthorized, so subsequent mutations are refused.

The snapshot records only what Phase 36 stored:

- product id, name and version;
- description and target segments;
- approved value proposition when the PRD is approved;
- pricing assumptions that actually exist in the opportunity financial record;
- supported features from approved PRD requirements/core features;
- explicit exclusions and missing information as known limitations;
- documentation references from the release record;
- launch status and approval.

Phase 36 has **no separate approved-marketing-claims field**. The snapshot
therefore returns `approved_marketing_claims: []` and says so. It does not infer
claims from a vision statement, a feature list or a price assumption.

## 4. Persistence and isolation

The service serializes all workspaces into the existing state store:

```text
namespace: customer_growth
schema_version: 1
```

Each `CustomerWorkspace` contains:

- `segments`, `icps`, `research`;
- `customers`, `leads`, `deals`, `interactions`;
- `communications`, `knowledge`;
- `tickets`, `feedback`, `onboarding`, `success`;
- `acquisition_costs`;
- `pipeline_stages`, `qualification_criteria`;
- `automation_policy`, `approvals`, `events`;
- the product snapshot and timestamps.

Lookups are scoped to the workspace's `product_id`. A customer from `prod-1` is
not visible through `prod-2`, even when both workspaces live in the same process.
There is no tenant model beyond the product/project boundary already used by the
application.

The state store is forgiving: one corrupt workspace record is skipped and the
rest load. The service reports load errors in `status()` rather than hiding them.

## 5. Segments, ICPs and research

Segments support business/individual categories, industry, company size,
geography, needs, pain points, budget indicators, buying intent, use cases,
decision-maker roles, evidence and assumptions.

An ICP links to a segment and carries criteria, needs, pain points, decision
roles, evidence and assumptions. Multiple ICPs per product are allowed.

Verification is explicit:

- `verified: true` requires recorded evidence;
- otherwise the record is labelled `inferred`;
- evidence and assumptions are stored separately.

Research records require a source URL or reference, a retrieval timestamp, an
evidence summary, confidence in `[0, 1]`, an optional segment and a verification
state:

- `public_research`
- `direct_validation`
- `operator_supplied`
- `assumption`

Only `direct_validation` is stored as verified/direct customer validation. Public
research is never relabelled as an interview, survey or demand measurement.

## 6. Leads

A lead stores:

- id, name/organization, lawful contact fields, source and source detail;
- product interest, segment and location;
- status, status history and owner;
- consent status/channels/source;
- qualification inputs and qualification history;
- interaction ids, linked customer and lost reason;
- next action and timestamps.

Accepted sources are website inquiry, voluntary registration, waitlist, approved
referral, public business directory, manual import, approved integration, event
and operator-supplied.

`public_directory`, `manual_import` and `approved_integration` require a recorded
lawful basis. Contact details are only what the operator supplies; the service
does not scrape, infer or bulk-collect addresses. There is no bulk-message path.

### Lead statuses

```text
NEW → RESEARCHING → QUALIFIED / UNQUALIFIED
QUALIFIED → CONTACT_PENDING_APPROVAL → IN_CONVERSATION → CONVERTED
any open state → LOST (reason required) / ARCHIVED
```

Transitions are checked against a table. Direct `CONVERTED` is refused; the
convert action creates the customer record. `QUALIFIED` and
`CONTACT_PENDING_APPROVAL` require a positive (`QUALIFIED`) qualification result,
not merely a stored score. Conversion and deal creation require that same
positive result. `LOST` requires a reason. A recorded
`disqualification_reason` overrides a positive score and recommends `UNQUALIFIED`.

Consent can be updated with
`POST /customer/workspaces/{product_id}/customers/{customer_id}/consent` or
`POST /customer/workspaces/{product_id}/leads/{lead_id}/consent`. A grant from
`unknown`/`withdrawn` needs a recorded source; withdrawal is allowed with a note.
Approval and send re-read the current consent rather than trusting the draft's
snapshot.

## 7. Explainable qualification

Qualification is deterministic. It never asks a model and never treats a score as
verified purchase intent.

The default criteria and weights are:

| Criterion | Weight | Required |
|---|---:|---|
| Product fit | 0.20 | yes |
| Problem relevance | 0.15 | yes |
| Budget compatibility | 0.15 | no |
| Purchase intent | 0.15 | no |
| Decision-making authority | 0.10 | no |
| Timeline | 0.10 | no |
| Engagement history | 0.15 | no |

Each criterion accepts either a number `0..5` or an object:

```json
{
  "score": 4,
  "evidence": "recorded discovery call",
  "verified": true
}
```

The result contains:

- score on `weighted_0_to_1`, calculated only from criteria that were actually
  recorded;
- per-criterion score, evidence, verified flag and weight;
- missing information and required-missing criteria;
- human-readable reasons;
- recommended status;
- `purchase_intent_verified`;
- a note that an unverified score is not purchase intent.

`purchase_intent_verified` is true only when the purchase-intent input itself has
both evidence and `verified: true`. Missing required evidence keeps the lead in
`RESEARCHING` even when the weighted score is high. An explicit recorded
disqualification overrides any weighted score and produces `UNQUALIFIED`.

## 8. Sales pipeline

Default stages:

```text
Lead → Qualified → Discovery → Demonstration → Proposal → Negotiation
     → Closed Won / Closed Lost
```

Stages are stored per workspace and can be configured at the data layer. Deals
record value, currency, probability, expected close, owner, next action,
objections, competitor alternatives, proposal history, decision history, stage
history and lost reason.

A deal can be created from a qualified/converted lead or an existing customer. A
lead and customer from different records cannot be combined, and a deal cannot
be created from an unqualified lead. Deal value is always labelled
`value_is_estimate: true`. Closing a deal won may record a separate
`realized_amount`; only that explicit amount contributes to realized revenue. A
closed deal cannot move again. A lost deal requires a reason. Mixed-currency
open-deal/pipeline/realized/CAC totals are returned unavailable rather than summed
without conversion.

Analytics derive:

- lead-to-qualified conversion;
- qualified-to-customer conversion;
- stage conversion from recorded stage history;
- sales-cycle days from creation/close timestamps;
- pipeline value as the sum of recorded open-deal estimates;
- win/loss ratio;
- lost-deal reasons.

Pipeline value is never called revenue or bookings.

## 9. Sales assistance

The advisory `customer-growth-agent` reads one workspace and summarises recorded
state. It cannot move a deal, change a price, approve a discount, send a message,
issue a refund or promise contractual terms.

`POST /customer/workspaces/{product_id}/sales/assist` builds a deterministic,
traceable sales brief for a lead or deal. It contains:

- customer requirement analysis from recorded qualification evidence and
  segment/ICP needs;
- product-fit rows that mark undocumented requirements as gaps to verify, not as
  proof of missing capability;
- recorded research and its verification state;
- meeting agenda, discovery questions and follow-ups;
- objection analysis with an explicit "no response recorded" default;
- a proposal draft built from documented features and recorded pricing, marked
  draft-only with discounts and contract terms unauthorized;
- recorded competitor alternatives without superiority claims;
- a conversation summary made only from recorded interaction summaries;
- source records for every section.

The API records proposal and decision history with source references. Proposal
terms are recorded text, not an executed agreement. No route performs a purchase,
payment, refund, discount approval, paid campaign or external commitment.

## 10. Communication, consent and approval

Every communication starts as a `draft` with:

- related lead/customer, channel, recipient, subject and body;
- consent status and the reason consent does or does not permit the channel;
- `requires_approval: true`;
- approver, approval note and timestamp when decided;
- sender outcome, message id and failure text.

Supported channels: email, phone, meeting, product demo, onboarding and other.

### Consent

Consent is `unknown`, `granted`, `withdrawn` or `not_required`. A channel-specific
grant accepts only listed channels. Withdrawn consent is always blocked. The
consent endpoints update the source record; approval and send re-read it so a
withdrawal cannot be bypassed by an older draft. A later grant can unblock a stale
draft only when explicit approval or an enabled narrow policy also permits it.

### Approval

A draft needs a named approver and note. Approval of a draft whose consent does
not allow the channel raises `ConsentRequired` (HTTP 409) instead of approving it.

### Narrow automation policy

The default policy is disabled. To enable one, the operator must record:

- `enabled: true`;
- allowed channels;
- `max_recipients` from 1 to 10;
- a daily limit within the service cap;
- an approval note.

A policy does not bypass consent. It only permits a consenting, single-recipient
communication on an allowed channel without a separate per-message approval.

### Sender

There is no external email provider wired by default. The service accepts an
injected synchronous sender. Without one:

- the communication is retained;
- its status becomes `not_configured`;
- the failure says nothing was sent;
- no delivery is claimed.

With a sender, delivery is `sent` only when the sender returns
`{"sent": true, ...}`. Sender failure is recorded as `failed` with the error.
Every attempt is in the communication audit and in workspace events. There is no
bulk endpoint and no unsolicited-message path.

## 11. Support tickets

Tickets are classified by a deterministic keyword classifier when no category is
supplied. A customer-visible message on a `RESOLVED` ticket reopens it and
increments the reopen counter. Tickets support:

- customer and product association, subject, description and category;
- priority and severity (`low`, `normal`, `high`, `critical`);
- assignee and attachments (recorded references, not uploaded blobs);
- status, messages, internal notes, resolution and timestamps;
- escalation history, reopen count and assistance runs.

Categories: technical issue, account issue, billing inquiry, product usage,
feature request, bug report and general inquiry.

Statuses:

```text
OPEN → TRIAGED → IN_PROGRESS ↔ WAITING_FOR_CUSTOMER
     → ESCALATED → RESOLVED → CLOSED
RESOLVED/CLOSED → OPEN (reopen count increments)
```

Resolution requires recorded resolution text. Escalation requires a reason.
Closing a ticket does not delete its history.

## 12. Grounded support assistance

`assist_ticket` is deterministic and uses only knowledge entries where:

```text
approved is True and verified is True
```

It:

1. classifies the ticket with explicit categories or a keyword classifier;
2. matches approved knowledge by title, body and keywords;
3. returns the matched sources and recorded troubleshooting steps;
4. drafts a response citing those sources;
5. sets `grounded: false` and recommends escalation when no approved evidence
   matches;
6. also escalates a grounded article that has no recorded troubleshooting steps,
   with explicit `escalation_reasons`;
7. requires human review for account, billing, refund, legal, contract,
   security or high/critical-priority matters;
8. records `actions_performed: []`.

It does not invent a troubleshooting result, claim an action ran, disclose
secrets or perform an account change, refund, billing adjustment or contractual
commitment. A human must resolve sensitive requests.

## 13. Feedback, onboarding and retention

Feedback kinds: feature request, bug report, complaint, satisfaction, common
question, usage difficulty, churn reason and improvement suggestion. Records can
link to product, version, customer, segment, ticket, feature, requirement and
task. Verified feedback needs a source. A satisfaction rating is `1..5`.

Onboarding workflows contain a checklist, account guidance, tutorials,
documentation, initial configuration, adoption score, success milestones and next
follow-up. Completing an item needs recorded evidence. Status becomes `complete`
only when every item is complete.

Customer success records expose:

- interaction, ticket, open-ticket and feedback counts;
- adoption score;
- risk level and explicit reasons;
- renewal date and next follow-up;
- a note that risk is an explanatory indicator, not a prediction.

Churn requires a reason and records a churn-reason feedback entry.

Erasure requires explicit approval and a reason. It removes the customer and
directly related deals, tickets, feedback, onboarding, success records,
communications and interactions, and records an audit event without reproducing
the erased personal data.

## 14. Analytics

Four families are computed from recorded state only:

**Acquisition:** leads generated, source counts, segment distribution, qualified
leads, qualification rate, converted leads and actual acquisition cost (only when
cost records exist).

**Sales:** pipeline estimate, deal value, realized revenue only from explicit
realized amounts, conversions, stage rates, sales-cycle days, win/loss ratio and
lost reasons.

**Support:** open/total tickets, resolution time, escalation rate, categories,
reopened tickets and satisfaction only when ratings exist.

**Retention:** customers, churned customers, churn/retention rate from recorded
status, onboarding completion, adoption score, repeat-issue customers and churn
feedback records.

Unavailable values are returned as:

```json
{
  "available": false,
  "value": null,
  "reason": "No actual acquisition-cost records exist; CAC is not fabricated."
}
```

No CAC, revenue, demand, retention or satisfaction value is inferred.

## 15. API

All routes are authenticated and scoped to a product workspace:

| Method | Path | Purpose |
|---|---|---|
| GET | `/customer/status` | Service status, configured seams, budgets and boundaries |
| GET/POST | `/customer/workspaces` | List/create workspaces |
| GET | `/customer/workspaces/{product_id}` | Full workspace and analytics |
| GET | `/customer/workspaces/{product_id}/history` | Events, approvals, communication/ticket audit |
| POST | `/customer/workspaces/{product_id}/refresh` | Re-read the Phase 36 snapshot |
| GET | `/customer/workspaces/{product_id}/analytics` | Four analytics families |
| POST | `.../segments`, `.../icps`, `.../research` | Segmentation and sourced research |
| GET/POST | `.../customers` | Customer list/create |
| GET | `.../customers/{customer_id}` | Customer 360 view |
| POST | `.../customers/{customer_id}/consent` | Update/withdraw customer consent |
| POST | `.../customers/{customer_id}/success` | Refresh success indicators |
| POST | `.../customers/{customer_id}/churn` | Record churn and reason |
| POST | `.../customers/{customer_id}/erasure` | Approved customer erasure |
| GET/POST | `.../leads` | Lead list/create |
| POST | `.../leads/{lead_id}/consent` | Update/withdraw lead consent |
| POST | `.../leads/{lead_id}/qualify` | Explainable qualification |
| POST | `.../leads/{lead_id}/status` | Checked status transition |
| POST | `.../leads/{lead_id}/convert` | Create the customer |
| POST | `.../interactions` | Record a real interaction |
| POST | `.../sales/assist` | Traceable sales brief for a lead/deal |
| POST | `.../deals` | Create an estimated deal |
| POST | `.../deals/{deal_id}/stage` | Checked pipeline transition |
| POST | `.../deals/{deal_id}/proposals` | Proposal history |
| POST | `.../deals/{deal_id}/decisions` | Decision history |
| POST | `.../policy` | Narrow communication policy |
| POST | `.../communications` | Draft a communication |
| POST | `.../communications/{id}/approval` | Approve/cancel a draft |
| POST | `.../communications/{id}/send` | Attempt delivery through configured sender |
| POST | `.../knowledge` | Approved product knowledge |
| GET/POST | `.../tickets` | Support inbox/create |
| GET | `.../tickets/{ticket_id}` | Ticket detail |
| POST | `.../tickets/{id}/triage` | Categorise/assign/triage |
| POST | `.../tickets/{id}/messages` | Internal note or customer response |
| POST | `.../tickets/{id}/status` | Checked ticket transition |
| POST | `.../tickets/{id}/assist` | Grounded assistance |
| POST | `.../feedback` | Linked feedback |
| POST | `.../onboarding` | Start onboarding |
| POST | `.../onboarding/{id}` | Complete an item with evidence |
| POST | `.../costs` | Actual acquisition cost |

Error mapping:

| Error | HTTP |
|---|---|
| workspace/record not found | 404 |
| product not launch-authorized | 403 |
| consent/approval conflict, invalid transition | 409 |
| invalid payload | 422 |
| budget exhausted | 429 |
| sender not configured | 503 (record retained as `not_configured`) |

Malformed nested objects, booleans and lists are rejected rather than coerced.

## 16. Frontend

The **Customer & Growth** sidebar workspace contains:

- workspace creation and list;
- product context and launch authorization;
- acquisition/sales/support/retention analytics;
- segments, ICPs and sourced research;
- customers and leads, including qualification outcomes, consent
  withdrawal/restoration, success refresh, churn and approved erasure;
- sales pipeline and deal actions;
- communication audit with approve/cancel/send;
- approved support knowledge, support inbox, ticket detail and customer
  message/reopen controls;
- grounded assistance output;
- feedback, onboarding and success indicators;
- forms for segments, ICPs, research, customers, leads, deals, drafts, knowledge,
  tickets, feedback and onboarding.

All mutations refresh the workspace through the panel's own listeners, so a
mutation never replaces the panel with a raw JSON card.

## 17. Security and operational safety

- Every `/customer*` route uses `require_auth`.
- Customer data is isolated by product workspace.
- Secrets are rejected if a credential-shaped value appears in a customer record
  or message body.
- Consent is channel-specific and checked at draft, approval and send.
- Sensitive support topics require human review.
- No unsolicited bulk messaging, scraping, purchased contact lists, payments,
  refunds, billing changes, paid advertising or contractual commitments exist.
- Budgets bound workspaces, customers, leads, deals, tickets, communications,
  feedback, events, ticket messages and daily sends.
- Customer erasure and communication policy changes are approval-recorded.
- Events and audit entries are written through the application's existing seams.
- Support knowledge is appended to the existing knowledge engine only when it is
  approved.

## 18. Tests

Focused suite:

```bash
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python -m pytest \
  tests/test_phase37_customer_service.py \
  tests/test_phase37_customer_api.py -q
```

Coverage includes:

- product authorization and snapshot honesty;
- segment/ICP/research evidence rules;
- customer isolation and lead qualification;
- lead lifecycle, conversion and lost reason;
- deal transitions, estimates and realized revenue;
- manual qualification bypass refusal and mixed-currency availability;
- consent updates, withdrawal/restoration, approval, policy and unconfigured sender;
- configured sender delivery and stale-draft consent rechecks;
- traceable sales assistance and unauthorized-term boundaries;
- grounded support, sensitive escalation and ticket lifecycle;
- feedback, onboarding, success, churn and unavailable metrics;
- erasure, persistence, event/audit/memory integration and budgets;
- API authentication, malformed payloads, missing records and data isolation;
- static workspace and renderer wiring.

Results are recorded in
[PHASE37_IMPLEMENTATION_AND_VERIFICATION_REPORT.md](PHASE37_IMPLEMENTATION_AND_VERIFICATION_REPORT.md).

## 19. Known limitations

- No live email/communication provider is configured. Delivery is implemented
  behind an injected sender; without one, every send is honestly
  `not_configured`.
- No attachment uploader exists; attachments are recorded references.
- No live customer research, interviews, surveys or demand evidence is produced.
  Research must be recorded from actual sources by an operator.
- Qualification is deterministic and criteria-based, not an ML intent model.
- Sales stages are configurable in the stored workspace but there is no stage
  editor UI.
- Pipeline automation, quote generation, e-signature, invoicing and payment
  collection are out of scope.
- Analytics are descriptive; no forecast, CAC model or retention prediction is
  claimed.
- The JSON state store rewrites a workspace on each mutation, so very large
  single-product records make writes progressively slower. Measurements are in
  the verification report.
- The workspace was checked through HTTP/API tests and static/JS checks; the page
  was not manually clicked through in a browser during this pass.
- Phase 38 was not started.
