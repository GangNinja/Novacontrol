# NM-08 — Governance and Overrides

Risk policies cannot be bypassed silently. The supported boundary is an explicit,
audited override record, not a public "approve anyway" endpoint.

## Override rules

- Only `RESTRICTED` and `REQUIRES_REVIEW` decisions may be overridden. An override
  records acceptance but never authorizes a restricted decision at full size:
  a restricted action must be resubmitted at an adjusted size and reassessed.
- `REJECTED` and `INSUFFICIENT_DATA` are not overridable. A hard safety constraint
  stays hard; changing the policy itself is a separate, versioned, audited action.
- Every override records the decision id, constraint code, authorizing actor,
  reason, creation time and expiry time.
- An override without an actor, reason or future expiry is refused.
- Expired overrides are not returned as active by `active_override`.
- Overrides are append-only and exposed through `GET /market/risk/overrides`.

## Authorization contract

NovaMarket currently has no authenticated administrative identity provider. The
engine therefore does not invent one: it requires an `actor` string and persists it
as the authorization claim, and documents that a deployment must place this API
behind its authenticated administrative boundary. The endpoint cannot be used to
turn a rejection into an approval, and no route exposes a "mark approved" operation.

## Policy updates

`POST /market/risk/policies/{policy_id}` accepts a partial document, rejects unknown
fields, creates a new version from the canonical body, appends history and publishes
`risk.policy.updated`. Updates require the same administrative boundary as overrides.

## Audit trail

Decisions, alerts, overrides, policy versions, stress runs and consumption records
are stored with timestamps. Decision rows are append-only at the database level;
alerts move through explicit states with actor and time recorded. The shared
`novamarket.risk.*` events mirror each committed change for subscribers.
