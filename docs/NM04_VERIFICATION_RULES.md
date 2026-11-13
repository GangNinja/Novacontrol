# NM-04 — Verification Rules

Verification answers one question: **what does the retrieved evidence support?**
It never answers "is this good news", "will the price move", or "should I buy".

## 1. States and what they permit

| State | Meaning | May be presented as confirmed |
| --- | --- | --- |
| `unverified` | nothing credible has been retrieved yet | no |
| `single_source` | one independent origin reports it | no |
| `multi_source_corroborated` | independent origins report the same claim | no (not official) |
| `officially_confirmed` | an official publisher states the claim | **yes** |
| `conflicting_evidence` | sources disagree; the conflict is preserved | no |
| `retracted` | the claim was withdrawn | no |
| `expired` | outside the category's review window; history retained | no |

`describe_state(...)` is the single place that answers the last column, so a UI
cannot decide for itself that a corroborated report is "confirmed fact".

## 2. Legal transitions

`VERIFICATION_TRANSITIONS` is the graph; `ensure_verification_transition(...)`
raises `EventStateError` (a `ValueError`, HTTP 409) for anything else.
`retracted` is terminal — a withdrawn claim is not silently re-promoted by a
later feed item — while `expired` may be re-verified if new evidence arrives.
A no-op move (state to itself) is accepted so re-ingesting identical evidence is
not treated as an illegal transition.

The same rule applies to the lifecycle: `STATUS_TRANSITIONS` governs
`EventStatus`, and illegal moves raise instead of overwriting the record.

## 3. Evidence and independence

Evidence is grouped by **origin**, not by article:

* two records with the same `independence_key` (publisher host) are one origin;
* two records whose text similarity is at or above `SYNDICATION_THRESHOLD`
  (0.85) are treated as syndicated copies, even across different hosts;
* only `status == retrieved` records participate.

`independent_sources` is the number of groups, not the number of rows. Two
copies of one wire story therefore cannot manufacture corroboration.

## 4. Rules, in order

1. **Official statement** — when the record's declared source type is official
   (`exchange`, `company_ir`, `government`, `regulator`, `official_statistics`)
   and its reliability is `official`/`primary`, the state is
   `officially_confirmed`. A secondary publisher quoting an official is still
   `single_source`.
2. **Conflicts** — figures are compared by **context and unit**
   (`numeric_claims`): `"repo rate 6.50 per cent"` against
   `"repo rate 6.75 per cent"` is a conflict; `"revenue 12%"` against
   `"margin 12%"` is not. A conflict produces `conflicting_evidence`, keeps both
   records, and lists the disagreement in `verification_detail.conflicts`.
3. **Retraction** — a withdrawal or correction (`retraction_frame`) moves the
   event to `retracted`, and the report is attached to the event it withdraws
   rather than becoming a second, unrelated event.
4. **Denial** — a denial (`denial_frame`) is preserved as evidence of the denial;
   it does not upgrade the claim it denies.
5. **Corroboration** — two or more independent groups reporting the same claim
   give `multi_source_corroborated`.
6. **Single origin** — otherwise `single_source`; with no usable evidence at all,
   `unverified`.
7. **Expiry** — `expiry_for(subcategory)` sets the review window; expiry adds a
   version with `verification = expired` and `status = expired`, keeping every
   earlier version.

## 5. Guarantees

* A claim is never shown as `officially_confirmed` because of repetition.
* A conflict is never resolved by picking a winner; both records stay readable
  through `GET /market/events/{id}/evidence`.
* A retraction never deletes the original report, and the history endpoint keeps
  the full transition log.
* An event whose evidence cannot be retrieved is reported with a typed source
  error; it is not silently marked verified.
* Verification is deterministic: the same evidence produces the same state, so a
  disputed event can be re-derived rather than argued about.
