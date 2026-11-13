# NM-04 — Evidence-Backed Market Event Radar: Architecture

**Status:** implemented and verified against fixtures (see `NM04_TEST_REPORT.md`).
No live feed has been retrieved in this build; see `NM04_SOURCE_REGISTRY.md` for
which sources are declared and which remain unverified.

NM-04 turns declared public feeds into **evidenced, classified, deduplicated
market events**. It does not forecast prices, rank securities for purchase, or
recommend a trade; there is no field in the model that could carry such a claim.

## 1. One pipeline, one owner per stage

```
declared feed ─► EventFeedClient ─► parse_feed ─► extract ─► dedupe
                                                              │
                                                              ▼
                        store ◄── publish ◄── classify ◄── verify
                          │
                          ▼
                 /market/events* API  +  novamarket.market_event.*
```

| Stage | Module | Responsibility |
| --- | --- | --- |
| Declared sources | `radar/sources.py` (`DEFAULT_SOURCES`, `SourceCatalog`) | what may be fetched, and whether it is enabled |
| Retrieval | `radar/sources.py` (`EventFeedClient`) | bounded HTTP with retry, rate limit, ETag and typed failures |
| Parsing | `radar/sources.py` (`parse_feed`) | RSS 2.0, Atom and JSON entries with their own timestamps |
| Extraction | `radar/extraction.py` | deterministic keyword rules → `ExtractedEvent`, or `unclassified` |
| Dedupe | `radar/dedupe.py` | `new` / `update` / `duplicate`, with reasons |
| Verification | `radar/verification.py` | what the evidence supports, including conflicts and retractions |
| Classification | `radar/classification.py` | explainable importance plus a separate confidence |
| Mapping | `radar/mapping.py` | instrument and sector links justified by text |
| Storage | `radar/store.py` (`EventStore`) | append-only versions, evidence, mappings, transitions |
| Sequencing | `radar/engine.py` (`MarketEventRadar`) | ordering, failure isolation, publish-after-commit |

Nothing in the package imports a later phase. NM-05 onward are not implemented.

## 2. What NM-04 reuses instead of duplicating

| Existing piece | How NM-04 uses it |
| --- | --- |
| `novamarket.marketdata.providers.base` (NM-02) | `HttpTransport` / `UrllibTransport`, `HttpResponse`, `RetryPolicy`, `RateLimiter` — the radar adds no second HTTP client |
| NM-02 SQLite conventions | the same database file, the same append-only discipline and immutable `(event_id, version)` rows |
| NM-01 `NovaMarketService` | the radar is wired into the one service, its capability table and its status surface |
| NM-01 event vocabulary | ten `novamarket.market_event.*` types in `core/events.py` with validated payload fields |
| NM-01 permissions | `market.events.read`, `market.events.refresh`, `market.events.sources` |
| NM-02 instrument catalogue | `InstrumentView.from_payload(...)` adapts canonical instruments for mapping; no second instrument store |
| State store | radar sources/settings persist through the existing store; one namespace |

No second database engine, no second event bus, no second instrument registry.

## 3. Sequencing and failure isolation

`MarketEventRadar.ingest(definition, entries)` is the unit of work:

1. extraction runs first; an entry that classifies as nothing is **counted as
   `unclassified` and never stored as an event**;
2. mapping runs before dedupe so shared instruments can be compared;
3. dedupe decides `duplicate` (nothing stored), `update` (version + 1, previous
   version kept) or `new` (version 1);
4. evidence from earlier versions is carried forward, so a corroborating report
   sees the whole record rather than only itself;
5. verification and classification see the full evidence set, the catalogue of
   all declared sources, and the previous verification state;
6. the store commits the version, evidence, mappings and transitions in one
   transaction;
7. only then are the `novamarket.market_event.*` events published; a publish
   failure is recorded (`last_event_error`) and never rolls back a stored event.

A failure is always typed and local: a transport error fails one source, a
malformed entry is counted `rejected`, and `refresh()` continues with the next
source. `refresh()` reports per-source counts, so "no events" can never be
mistaken for "the feed was unreachable".

## 4. Storage model

`EventStore(path, clock=...)` keeps, in the NovaMarket database:

* `radar_meta` — schema version (`RADAR_SCHEMA_VERSION = 1`) and settings;
* immutable event versions keyed by `(event_id, version)`, with `supersedes`
  pointing at the version they replaced;
* one row per evidence record, with its own retrieval time and content hash;
* instrument mappings and the lifecycle/verification transitions;
* per-source health (`status`, `entries_seen`, `entries_new`, error code, ETag).

Writes are append-only: an update inserts a new version, it does not edit the old
one, so history answers "what did we know, and when" rather than "what is true
now". Expiry follows the same rule — it adds an `expired` version, publishes
`novamarket.market_event.expired`, and keeps the earlier ones.

## 5. HTTP surface

Nine routes, registered in `api/app.py`, `api/models.py` and
`api/route_consumers.py`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/market/events` | filtered search (`category`, `subcategory`, `verification`, `importance`, `instrument_id`, `sector`, `market`, `source_id`, `text`, `since`, `until`, `limit`, `offset`) |
| GET | `/market/events/{event_id}` | one event with evidence, transitions, classification and mappings |
| GET | `/market/events/{event_id}/evidence` | every evidence record, conflicts included |
| GET | `/market/events/{event_id}/history` | all versions and the transition log |
| GET | `/market/events/instrument/{instrument_id}` | events mapped to one instrument |
| GET | `/market/events/sources` | declared sources merged with observed health |
| POST | `/market/events/sources/{source_id}/enabled` | enable/disable a declared source |
| POST | `/market/events/refresh` | fetch and ingest (`source_id` optional) |
| POST | `/market/events/expire` | expire events past their review window |

Errors are typed: unknown event → 404, unknown source → 404, unknown filter value
→ 422, `EventStateError` → 409, a source failure → 502, rate limiting → 429.

## 6. Published events

```
novamarket.market_event.discovered
novamarket.market_event.extracted
novamarket.market_event.normalized
novamarket.market_event.verified
novamarket.market_event.classified
novamarket.market_event.updated
novamarket.market_event.retracted
novamarket.market_event.expired
novamarket.market_event.source_unavailable
novamarket.market_event.evidence.conflict_detected
```

Every payload carries `event_id`; payload keys never shadow the envelope's
reserved `source`, `correlation_id` or `causation_id`. `discovered` fires only
for a new event: a further report of a known event publishes `verified`,
`classified` and `updated` instead, so a consumer counting discoveries is not
counting follow-ups.

## 7. Boundaries

* No price, direction, target, expected return or recommendation exists in any
  model, response or event.
* No scraping beyond the declared feeds, no paywall bypass, no credential use.
* No LLM is required: extraction, verification, classification, dedupe and
  mapping are deterministic, so the same input yields the same record.
* NM-05 (discovery/watchlists), NM-06 (forecasting) and later phases are not
  implemented here.
