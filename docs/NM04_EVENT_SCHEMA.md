# NM-04 — Event Schema

Every value below is enforced in construction (`radar/models.py`), not merely
described. A payload that cannot satisfy these rules is refused with
`EventValidationError` (HTTP 422) rather than stored.

## 1. Enumerations

| Enum | Values |
| --- | --- |
| `EventCategory` | `company`, `macroeconomic`, `sector`, `market_structure`, `unclassified` |
| `EventStatus` | `discovered`, `extracted`, `normalized`, `verification`, `classified`, `active`, `updated`, `resolved`, `expired`, `retracted` |
| `VerificationState` | `unverified`, `single_source`, `multi_source_corroborated`, `officially_confirmed`, `conflicting_evidence`, `retracted`, `expired` |
| `Importance` | `low`, `moderate`, `high`, `critical` |
| `SourceType` | `exchange`, `company_ir`, `government`, `regulator`, `official_statistics`, `financial_news`, `secondary` |
| `SourceReliability` | `official`, `primary`, `reputable`, `secondary`, `unknown` |
| `Relationship` | `direct`, `indirect`, `uncertain` |
| `EvidenceStatus` | `retrieved`, `unavailable`, `malformed`, `rejected` |
| `FeedFormat` | `rss`, `atom`, `json` |

`unclassified` is a counted outcome, not a stored category: an entry that matches
no subcategory is reported as `unclassified` and never becomes an event.

## 2. `MarketEvent` — one immutable version

| Field | Type | Meaning |
| --- | --- | --- |
| `event_id` | str | stable across versions |
| `event_key` | str | subject + subcategory + publication day |
| `version` | int | starts at 1; an update inserts `version + 1` |
| `supersedes` | int \| null | the version this one replaced |
| `title`, `description` | str | the source's own words, clipped |
| `category`, `subcategory` | enum / str | from the subcategory registry |
| `status` | `EventStatus` | lifecycle position |
| `verification` | `VerificationState` | what the evidence supports |
| `importance` | `Importance` | explainable band |
| `confidence` | 0.0–1.0 | extraction/evidence confidence, separate from importance |
| `occurred_at` | ISO \| null | when the thing happened, if the source says |
| `published_at` | ISO \| null | when the source announced it |
| `retrieved_at` | ISO | when this build fetched it |
| `discovered_at`, `updated_at` | ISO | when the radar first saw it / wrote this version |
| `market` | str | market code from the source definition |
| `instruments` | list[str] | canonical instrument ids that matched |
| `sectors` | list[str] | sectors carried by those mappings |
| `evidence_count` | int | how many evidence records exist |
| `independent_sources` | int | distinct origins, after syndication grouping |
| `fingerprint` | str | content-derived, for inspection |
| `expiry_days` | int | the subcategory's review window |
| `classification` | object | score, band, factors, explanation, confidence |
| `verification_detail` | object | state, reasons, evidence ids, conflicts |
| `mappings` | list | instrument mappings with their justification |
| `is_price_prediction`, `is_trading_recommendation` | bool | always `false` — the model has no other value to offer |

**Three times, three fields.** A missing occurrence time stays `null`; it is never
filled from the publication time. An unparseable feed date stays `null` rather
than being replaced by "now".

## 3. `EvidenceRecord` — what a source actually said

| Field | Meaning |
| --- | --- |
| `evidence_id` | content-derived id |
| `event_id` | the event this supports or contradicts |
| `source_id`, `publisher` | which declared source, and who published it |
| `url` | the exact entry URL |
| `reliability` | the declaration's reliability, not a guess |
| `title`, `excerpt` | the retrieved text |
| `content_hash` | digest of the entry, used for duplicate detection |
| `independence_key` | publisher host (or source id), used for corroboration |
| `retrieved_at`, `published_at` | two different moments, both kept |
| `status` | `retrieved`, `unavailable`, `malformed`, `rejected` |
| `reasons` | extraction notes |

A contradictory record is a `retrieved` record. Conflicts are not deleted,
downgraded or overwritten.

## 4. `EventMapping` — why an instrument is involved

| Field | Meaning |
| --- | --- |
| `instrument_id` | canonical NM-02 id, e.g. `XNSE:RELIANCE` |
| `relationship` | `direct` when the text names the instrument, `indirect` when the link is sector-level, `uncertain` when neither is established |
| `reason` | the matched term, quoted |
| `sector` | the instrument's sector, when the mapping has one |

An instrument is never attached by similarity alone, and an unknown company
produces no mapping rather than an invented one.

## 5. Transition log

Each row records `from_status`, `to_status`, `from_verification`,
`to_verification` and a `reason`. A first ingestion records the full path
(`discovered → extracted → normalized → verification → classified → active`) plus
the verification entry; an update records `… → updated → active` with
`new evidence re-evaluated the event`.

## 6. API payloads

| Route | Shape |
| --- | --- |
| `GET /market/events` | `{ok, events, total, limit, offset}` |
| `GET /market/events/{id}` | `{ok, ...event, evidence, transitions, verification_detail, mappings}` |
| `GET /market/events/{id}/evidence` | `{ok, event_id, evidence, total}` |
| `GET /market/events/{id}/history` | `{ok, event_id, versions, transitions, total}` |
| `GET /market/events/instrument/{id}` | `{ok, instrument_id, events, total}` |
| `GET /market/events/sources` | `{ok, sources, total}` |
| `POST /market/events/sources/{id}/enabled` | `{ok, source_id, enabled}` |
| `POST /market/events/refresh` | `{ok, sources, sources_checked, sources_ok, sources_failed, entries_seen, events_created, events_updated, duplicates, unclassified, rejected, started_at, finished_at}` |
| `POST /market/events/expire` | `{ok, expired, count, at}` |

`GET /status` carries the radar contract under its `radar` key (also available
as `NovaMarketService.radar_contract()`). It reports the categories, importance
bands and weights, verification states and transitions, dedupe thresholds,
timestamp fields and limits — the consumer-facing description of everything in
this document.
