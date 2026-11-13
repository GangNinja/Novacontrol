# NM-08 — Alerts and Monitoring

Alerts are the monitoring output of the risk engine. They are persisted, traceable
and independent of the decision that created them.

## Alert fields

`alert_id`, category, severity (`INFO`, `WARNING`, `HIGH`, `CRITICAL`), portfolio id,
instrument id, code, title, explanation, triggering metric, threshold, observed
value, recommended action, status (`open`, `acknowledged`, `resolved`), timestamps
and the originating decision id.

## Creation

Alerts are created when a decision records a breach, when a portfolio report finds a
position or portfolio limit violation, and when data, forecast or event conditions
Severity follows the constraint exactly: hard breaches are `CRITICAL`, review constraints `HIGH`, soft constraints `WARNING`, and informational conditions `INFO`.
`CRITICAL`, review constraints `HIGH`, soft constraints `WARNING`, informational
conditions `INFO`.

## Deduplication

While an alert with the same `portfolio | code | instrument | sector` is open or
acknowledged, a repeat detection is suppressed instead of inserting another row.
Resolution allows a new alert when the condition recurs. A
new alert is created only after the previous one is resolved or the identifying
state changes.

## Lifecycle

`POST /market/risk/alerts/{id}/acknowledge` and `/resolve` require an actor string,
record the transition time, and publish `risk.alert.acknowledged` /
`risk.alert.resolved`. Historical retrieval is available through
`GET /market/risk/alerts` with status filters. Critical alerts are never deleted by
the alert lifecycle; resolution is a recorded state, and the row remains queryable.

## Monitoring surfaces

`/market/risk/status`, `/market/risk/portfolio/{id}`, `/market/risk/exposure` and
`/market/risk/decisions` expose current risk state. Alert-driven monitoring does not
liquidate positions; it recommends review or restriction for an authorized actor.
