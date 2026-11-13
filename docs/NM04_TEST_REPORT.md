# NM-04 — Test Report

**Scope:** the NM-04 test suite (`tests/test_novamarket_radar.py`) plus the
NM-01/02/03 regression files that the new wiring touches. All runs were executed
on the developer machine (Windows, `.venv`), from `novamarket/`, with
`PYTHONPATH=src`.

## 1. Commands and results

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_novamarket_radar.py -q` | **70 passed, 7 subtests passed** |
| `python -m pytest tests/test_novamarket_api.py -q` | **24 passed, 42 subtests passed** |
| `python -m pytest tests/test_novamarket_radar.py tests/test_novamarket.py tests/test_novamarket_api.py -q` | **181 passed, 104 subtests passed** |
| `python -m pytest tests/test_novamarket_marketdata.py -q` (NM-02 regression) | **41 passed, 2 skipped** |
| `python -m pytest tests/test_novamarket_wallet.py -q` (NM-03 regression) | **76 passed, 22 subtests passed** |
| `python -m mypy src/novacontrol/novamarket src/novacontrol/api` | **Success: no issues found in 44 source files** |
| `python scripts/generate_api_reference.py --check` | **in sync** (after regeneration) |

The two skipped market-data tests are the pre-existing optional-provider skips;
they are not NM-04 tests and were skipped before this phase.

## 2. What the radar suite pins

| Class | Coverage |
| --- | --- |
| `FeedParsingTests` | RSS and Atom parsing, offset→UTC conversion, unparseable dates staying `null`, DTD refusal, malformed XML, provenance carried into evidence, the four declared sources being disabled and unverified |
| `RetrievalTests` | success + ETag capture, `304` as `unchanged`, `429` as a typed non-retryable failure, `503` as retryable, transport failure never looking like an empty feed, retry within budget, bounded retry |
| `ExtractionTests` | macro rate → `macroeconomic/interest_rate`, earnings → `company`, supply disruption → `sector`, trading halt → `market_structure`, unrelated text counted `unclassified` and stored nowhere, missing summary falls back to the title, occurrence time never invented |
| `VerificationTests` | official → `officially_confirmed`, single reputable → `single_source`, rumour never confirmed, independent wires → `multi_source_corroborated`, syndicated copies stay one origin, conflicting figures kept on one event, withdrawal → `retracted`, illegal transitions raise |
| `ClassificationTests` | band boundaries, recency decay, central-bank decision ranked high, contributions summing to the score, confidence separate from importance, no price/recommendation flags, more mapped instruments raising the score |
| `DedupeTests` | identical digest is a duplicate, different subject is a new event, outside-window report is new, confirming report updates, version 2 with `supersedes=1` and both evidence records kept |
| `MappingTests` | named company maps directly with a reason, unknown company gets no invented instrument, sector-only link never reported as direct, instrument lookup |
| `PersistenceTests` | reopen from disk, full lifecycle path recorded, filters/since/until/pagination, typed not-found, expiry adds a version and keeps history, in-window events not expired |
| `EngineIntegrationTests` | refresh ingest + health, identical refresh is a duplicate, one failing source does not stop another, a source failure never reported as an ingest, publish-after-commit ordering, metrics, contract, status, enablement persistence |
| `VocabularyAndApiTests` | the ten `novamarket.market_event.*` types exist with payload contracts, lifecycle table completeness, the nine routes are declared and consumed, event payload storable as JSON |
| `MarketEventApiTests` (in `tests/test_novamarket_api.py`) | the nine routes through the real application: an empty page before ingest, the four declared feeds reported disabled, enablement round-tripping through the store, unknown source → 404, unknown event → 404 on detail/evidence/history, invalid filters → 422, an empty expiry reporting zero, an unknown instrument returning an empty page |

Only the outbound HTTP seam is faked (`_Transport`, `_SequenceTransport`).
Extraction, dedupe, verification, classification, mapping, storage, the engine
and the API models are the real implementation.

## 3. Defects the suite found (and the fixes)

Testing was not a formality; these were real defects. Items 1–7 were found while
building the suite, items 8–12 by the later requirements audit.

1. **`EventStateError` was raised but never imported** in `radar/models.py`, so
   every verification transition (any update with new evidence) died with a
   `NameError`, which the engine counted as a rejected entry. Fixed by importing
   the error and covered by the transition tests.
2. **Corroboration could never happen**: the engine passed only the *current*
   source definition into verification, so prior evidence had no declared source
   and every event stayed `single_source`. Fixed by passing the whole catalogue.
3. **A `429` was marked retryable**, so a rate-limited source was immediately
   asked again — the opposite of respecting a rate limit. Now non-retryable, and
   pinned by the rate-limit test.
4. **A withdrawal created a second event** instead of retracting the original:
   the retraction's wording changed the comparison subject. Dedupe now accepts a
   `retracting` flag and a 30-day retraction window.
5. **Numbers and punctuation split one story into two**: subject tokens kept
   `6.50`/`6.75` and `cent,`/`cent`, so conflicting reports became unrelated
   events and conflicts were never detected. Subjects now drop numeric tokens and
   strip punctuation.
6. **Response headers were read case-sensitively** (`headers.get("etag")`), so a
   transport that preserved `ETag` lost the cache token. Now read
   case-insensitively.
7. **`sources()` omitted `entries_new` and `unchanged`** even though the health
   value object carries them. Added, so the API surface and the store agree.
8. **Expiry never published `novamarket.market_event.expired`** — the state
   changed silently. `expire_due` is now async and publishes the event after the
   expiry version commits, pinned by `test_expiry_publishes_the_expired_event`.
9. **`discovered` was republished on every update**, so a follow-up report looked
   like a new discovery. It now fires only when dedupe says `creates_event`,
   pinned by `test_an_update_does_not_republish_discovery`.
10. **An unknown event id returned an empty 200** from `/evidence` and
    `/history`. Both now answer 404, like `/market/events/{id}`.
11. **An unknown source id raised `SourceMalformedError` (502)** on enablement and
    refresh; a missing identifier is a missing resource, so it is now
    `EventNotFoundError` (404), pinned through the HTTP surface.
12. **One documentation claim was wrong**: `NM04_EVENT_SCHEMA.md` described a
    `GET /market/events/contract` route that does not exist. The contract is
    served from `GET /status` under `radar`; the document now says so.

## 4. Fixture-only vs live

Every feed in this report is a fixture served by a fake transport. **No live
source was fetched during this phase.** The four declared sources remain
`enabled_default=False` and `verified_live=False`; see
`NM04_SOURCE_REGISTRY.md` for the exact live-verification steps that have *not*
been performed.

## 5. Limitations

* Live feeds may use markup, encodings or date formats not present in the
  fixtures; parsing is defensive but unproven against the real endpoints.
* Fuzzy extraction is deterministic keyword matching, so an unusual headline can
  remain `unclassified` rather than be guessed.
* Corroboration depends on distinct origins; a story republished near-verbatim by
  many outlets is correctly counted once, which may look like "single source".
* No network verification means TLS, redirects, gzip and server-specific
  `ETag` behaviour are covered by NM-02's provider tests, not by NM-04's.
