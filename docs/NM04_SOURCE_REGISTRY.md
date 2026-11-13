# NM-04 — Source Registry

The registry is data, not configuration spread through the code: each source is
one `EventSourceDefinition` row in `DEFAULT_SOURCES`
(`src/novacontrol/novamarket/radar/sources.py`). Adding a source means adding a
row, not editing the pipeline.

## 1. Declared sources

All four rows ship with `enabled_default=False` and `verified_live=False`:
**no live feed is fetched by default, and none has been retrieved in this build.**

| `source_id` | Name | Publisher | Type | Reliability | Format | Market | URL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rbi_press` | RBI press releases | Reserve Bank of India | `government` | `official` | `rss` | IN | https://www.rbi.org.in/pressreleases_rss.xml |
| `fed_press` | Federal Reserve press releases | Board of Governors of the Federal Reserve System | `government` | `official` | `rss` | US | https://www.federalreserve.gov/feeds/press_all.xml |
| `bls_latest` | BLS latest releases | US Bureau of Labor Statistics | `official_statistics` | `official` | `rss` | US | https://www.bls.gov/feed/bls_latest.rss |
| `sec_edgar_8k` | SEC EDGAR current 8-K filings | US Securities and Exchange Commission | `regulator` | `official` | `atom` | US | https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom |

Why disabled by default: a build that fetches government feeds on import would
make unrelated work depend on the network, and NM-04 must never turn a network
error into "the market is quiet". Enablement is an explicit action.

## 2. Row fields

| Field | Meaning |
| --- | --- |
| `source_id` | stable identifier used by the API, the store and events |
| `name` | human label |
| `source_type` | `exchange`, `company_ir`, `government`, `regulator`, `official_statistics`, `financial_news`, `secondary` |
| `reliability` | `official`, `primary`, `reputable`, `secondary`, `unknown` |
| `url` | the exact feed address; no scraping of article pages |
| `feed_format` | `rss`, `atom` or `json` |
| `publisher` | who publishes the feed, kept on every evidence record |
| `market` | market code (`IN`, `US`, …) |
| `description` | what the source covers |
| `enabled_default` | whether the row starts enabled (all `false`) |
| `verified_live` | whether a live retrieval has been confirmed (all `false`) |

`SourceCatalog` merges these rows with the store's persisted enablement, so a
toggle survives a restart and a new default cannot silently switch a source on.

## 3. Enablement and permissions

* `POST /market/events/sources/{source_id}/enabled` toggles a declared source;
  an unknown id is a 404, never an implicit addition.
* Reading through `GET /market/events/sources` requires the
  `market.events.sources` capability; searching events requires
  `market.events.read`; refreshing requires `market.events.refresh`.
* `POST /market/events/refresh` with no `source_id` refreshes only enabled rows.

## 4. Retrieval policy

`EventFeedClient` wraps NM-02's `HttpTransport`, `RetryPolicy` and `RateLimiter`;
there is no second HTTP implementation.

| Rule | Behaviour |
| --- | --- |
| Bounded read | at most `max_entries` items per feed per refresh |
| Timeout | inherited from the transport (15 s default); a timeout is `source_timeout` |
| Retry | retryable failures only, within `RetryPolicy.max_attempts` |
| Rate limit | a `429` is `source_rate_limited` and is **not** retried — answering a rate limit with more requests is exactly what it forbids |
| Caching | `ETag` sent as `If-None-Match`; a `304` is recorded as `unchanged` with zero entries, not as an empty feed |
| Malformed content | a DTD/entity declaration is refused (`source_malformed`), never expanded; unparseable XML is typed, not guessed |
| Client errors | `4xx` other than `429` is `source_unavailable` for that run |
| Server errors | `5xx` is `source_unavailable`, retryable within the budget |
| Attributes | `Retry-After` is respected through the shared rate limiter; no paywall, login or credential is used |

Each attempt is recorded per source: status, error code, ETag, entries seen, new
entries and unchanged state. One failing source never stops another, and a
failure is never reported as a successful ingest.

## 5. Timestamps

Parsing preserves the source's own publication time, converts offsets to UTC and
leaves an unparseable date `null`. It is never replaced with the retrieval time,
which would make a stale item look fresh.

## 6. Live verification — not performed

The following steps are what "verified live" would require, and they are **not**
part of this build:

1. enable one source (`POST /market/events/sources/rbi_press/enabled`);
2. `POST /market/events/refresh {"source_id": "rbi_press"}` and inspect
   `entries_seen`, `events_created` and any `error_code`;
3. confirm `GET /market/events` returns classified events whose `evidence[].url`
   resolves, and `GET /market/events/sources` shows `status: "ok"`;
4. repeat for `fed_press`, `bls_latest` and `sec_edgar_8k` (the SEC feed requires
   a descriptive User-Agent under its access policy);
5. only then set `verified_live=True` in the registry row.

Until that is done, the honest status is: **declared but unverified**, tested
against fixtures only.
