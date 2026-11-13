# NovaMarket — NM-02 Market Data Reliability

**Status:** implemented and verified (PASS with one explicitly excluded live
check, 2026-11-02; see §19). Supersedes the "Contract only" entry for NM-02 in
`docs/NOVAMARKET.md`.

**Scope:** a dependable market-data foundation — acquisition, validation,
normalization, timestamping, persistence, retrieval, freshness and monitoring —
for every later NovaMarket phase. NM-02 performs **no** forecasting, asset
discovery, portfolio management, order placement, paper execution or
backtesting, and enables **no** real-money trading (`mode` stays `observe`).

---

## 1. What NM-02 added

| Layer | Module | Responsibility |
| --- | --- | --- |
| Provider adapters | `marketdata/providers/` | Yahoo, CoinGecko and Alpha Vantage behind one adapter contract; HTTP transport, retries, rate limits, quotas, circuit breakers, secret resolution |
| Canonical models | `marketdata/models.py` | `Instrument`, `PriceObservation`, `OhlcvBar`, `ProviderCapabilities`, `ProviderHealth`, append/reconciliation/ingestion results |
| Normalization | `marketdata/engine.py` | provider payload → canonical decimal/timestamp/currency records, identity hashing, partial-bar handling |
| Validation and quality | `marketdata/quality.py` | structural validation, anomaly flags, cross-provider reconciliation, daily-gap detection |
| Freshness | `marketdata/freshness.py` | per-asset-class / per-interval / per-session freshness verdicts |
| Market calendar | `marketdata/calendar.py` | dated session calendars per MIC, holidays, early closes |
| Persistence | `marketdata/store.py` | append-only SQLite repository with idempotent appends, corrections, retrieval log, quality events, provider health |
| Ingestion | `marketdata/engine.py` | one bounded background worker, per-instrument cycles, cancellation, restart |
| Integration | `service.py`, `api/app.py`, `core/events.py` | lifecycle-bound engine, authenticated routes, typed events |

---

## 2. Provider selection

Selection started from the NM-02 list, then excluded every source that could not
be used within its published terms, without a paid subscription, or without
bypassing authentication.

| Provider | Decision | Reason |
| --- | --- | --- |
| Yahoo Finance chart endpoint | **Integrated** | No credentials; real quotes and OHLCV for NSE/BSE (`.NS`/`.BO`), US equities, indices, ETFs, FX and crypto; delayed and unofficial, which is documented rather than hidden |
| CoinGecko public API | **Integrated** | No credentials; real crypto spot and OHLCV with documented free-tier limits; provides a genuine second provider for the crypto asset class |
| Alpha Vantage | **Integrated, credential-gated** | Real free tier (25 requests/day, 5/minute) for US equities/ETFs and daily OHLCV; requires the operator's own key, so its live path is unverified here (§19 H) |
| Twelve Data | Not integrated | Free tier requires an account key; no key was supplied and none was purchased |
| Financial Modeling Prep | Not integrated | Same credential constraint; overlapping coverage with Yahoo/Alpha Vantage |
| Direct NSE/BSE feeds, broker APIs | Not integrated | Licensed/paid exchange data; using them without a subscription would breach the providers' terms |

The set is deliberately small: three adapters covering equities, ETFs, indices,
crypto, FX and mutual funds, rather than a long list of unverified integrations.

---

## 3. Provider capability matrix

Declared capabilities come from each adapter's `ProviderCapabilities`;
`verified_operations` is written only from calls that actually succeeded and is
persisted in `market_provider_health`.

| | Yahoo | CoinGecko | Alpha Vantage |
| --- | --- | --- | --- |
| Asset classes | equity, ETF, index, crypto, currency, mutual fund | crypto | equity, ETF |
| Exchanges (MIC) | XNSE, XBOM, XNAS, XNYS, ARCX, XASE, CRYPTO (+ others via symbol mapping) | CRYPTO | XNAS, XNYS, ARCX, XASE |
| Intervals | 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo | 30m | 1d |
| Operations | quote, OHLCV, lookup | quote, OHLCV, lookup | quote, OHLCV |
| Data kind | delayed (typical 900 s) | delayed (typical 120 s) | delayed (typical 900 s) |
| Credentials | none | none | required (`env:NAME` / `store:NAME`) |
| Declared limits | 60 req/min | 10 req/min | 5 req/min, 25 req/day |
| History depth | up to 3 650 days (intraday windows narrower) | 1 day at 30 m on the free tier | 100 days |
| Verified here | quote, OHLCV, probe | quote, OHLCV, probe | none (no key) |

Documented limitations (also served by `GET /market/data/providers`):

- **Yahoo** — unofficial chart endpoint, not a licensed real-time feed; intraday
  history windows are 7 days (1m) / 60 days (5–30m) / 730 days (1h); the endpoint
  may change without notice; some indices and FX pairs publish no volume, stored
  as absent rather than zero; NSE/BSE symbols use `.NS`/`.BO` through the
  instrument's `provider_symbols`.
- **CoinGecko** — free-tier rate limits are low and changeable; free OHLC is
  30-minute granularity over a recent one-day window and publishes **no volume**;
  prices are an aggregate across exchanges (recorded in provenance); commercial
  use or higher limits require a paid plan.
- **Alpha Vantage** — without a resolvable key the adapter reports an
  authentication failure and returns no data; the free-tier counters are enforced
  locally but are process-local and reset on restart.

---

## 4. Adapter contract

`MarketDataAdapter` (`marketdata/providers/base.py`) is the only place a provider
response format exists. Each adapter declares `capabilities` and, where the
provider supports it, implements `fetch_quote`, `fetch_ohlcv`, `lookup` and
`probe`. The shared base supplies:

- one HTTP transport seam (`UrllibTransport`) with per-request timeouts;
- bounded retries with exponential backoff and `Retry-After` handling;
- a per-provider rate limiter and optional daily quota;
- a per-provider circuit breaker (closed / open / half-open, one probe allowed);
- credential resolution through `SecretResolver` (`env:NAME` / `store:NAME`) —
  values never enter configuration, logs or API responses;
- JSON parsing that separates a schema change, an unknown symbol, a rate limit,
  an authentication failure, a timeout and a provider outage into distinct errors;
- normalization into `ProviderQuote` / `ProviderBar` / `ProviderInstrument` before
  anything leaves the adapter.

Only operations a provider genuinely supports are implemented; an unsupported
capability reports "unsupported" instead of an empty success.

---

## 5. Canonical models

- **`Instrument`** — `instrument_id` is `MIC:SYMBOL` (`XNSE:RELIANCE`), so
  identical symbols on different exchanges cannot collide. Carries symbol, name,
  MIC, exchange, currency, asset class, trading status, provider symbol mappings
  and metadata. Construction validates the identity and rejects a mismatched
  MIC/symbol pair.
- **`PriceObservation`** — observation id, instrument id, provider, decimal
  price, currency, source timestamp (`observed_at`), retrieval timestamp
  (`retrieved_at`), source timezone, quality, freshness, reasons, provider
  metadata and optionally the raw payload. The exchange is carried by the MIC
  prefix of `instrument_id` and by the canonical instrument record; the provider's
  own venue code stays in `provider_metadata`. Construction rejects a non-positive
  price, a naive timestamp and an invalid instrument id.
- **`OhlcvBar`** — open/high/low/close as `Decimal`, optional volume (`None` when
  the provider publishes none — never a fabricated zero), interval, currency,
  timestamps, `partial` flag, quality, freshness, reasons and metadata.
- **Enums** — `AssetClass`, `DataInterval` (`spot` for a latest price, plus
  1m…1mo), `DataQuality`, `Freshness`, `ProviderStatus`, `CircuitState`,
  `AppendStatus`, `ReconciliationStatus`, `TradingStatus`.

Monetary values are `Decimal` throughout; no binary float enters a price path.

---

## 6. Normalization rules

1. Provider symbol → canonical instrument through the instrument's
   `provider_symbols` mapping (Yahoo `.NS`/`.BO`, CoinGecko coin id).
2. Parse prices as `Decimal`; reject NaN/inf/negatives.
3. Normalize timestamps to timezone-aware UTC while preserving the provider's
   original timezone name (`source_timezone`) and metadata.
4. Keep `observed_at` (provider source time) and `retrieved_at` (this process)
   distinct; neither is ever substituted for the other.
5. Record the provider's currency explicitly. A quote whose currency differs from
   the instrument's currency is **rejected as invalid** — it is never converted
   or silently treated as INR (`_currency_mismatch`).
6. Identify records deterministically (`observation_id` / `bar_id` from
   instrument, provider, interval and source time) so re-ingestion is idempotent.
7. Skip provider rows that cannot be parsed instead of inventing values; a bar
   with missing OHLC fields is not fabricated.
8. Mark a bar `partial` when its session has not closed; partial bars are stored
   but not usable.
9. Preserve provider metadata (aggregation flags, timestamp basis, venue code)
   for audit.

---

## 7. Data-quality engine

Classifications: `VALID`, `WARNING`, `STALE`, `SUSPECT`, `INVALID`,
`UNAVAILABLE`. Only `VALID`, `WARNING` and `STALE` are usable by a financial
calculation; `SUSPECT`, `INVALID` and `UNAVAILABLE` never are. `SUSPECT` records
are **retained** for audit, not deleted.

| Check | Rule | Class |
| --- | --- | --- |
| Structural (quote) | non-positive price, naive/invalid timestamp, unknown instrument, invalid instrument id | `INVALID` (rejected) |
| Structural (bar) | high < low, open/close outside the high–low range, negative volume, non-positive prices, bad timestamp | `INVALID` (rejected) |
| Currency | quote currency ≠ instrument currency | `INVALID` (rejected, never converted) |
| Staleness | freshness verdict is `STALE` | `STALE` |
| Price jump | ≥ 20 % vs previous same-currency observation | `WARNING` |
| Price jump | ≥ 50 % vs previous same-currency observation | `SUSPECT` (flagged, not used) |
| Volume | ≥ 10× / ≥ 50× the 20-bar median | `WARNING` / `SUSPECT` |
| Provider disagreement | inside the 300 s window, relative difference > 0.5 % | `CONFLICT` → `SUSPECT`/unavailable, never averaged |
| Gaps | missing sessions between stored daily bars | reported by `detect_daily_gaps` |

A genuine large move is flagged, never deleted: the jump thresholds produce
`WARNING` below the suspect ratio and `SUSPECT` above it, both with the computed
percentage in the reason. Rejections are persisted in `market_quality_events`, so
the reason survives the request that produced it.

---

## 8. Freshness engine

Verdicts: `FRESH`, `DELAYED`, `STALE`, `CLOSED_MARKET`, `MISSING`,
`PROVIDER_UNAVAILABLE`, `UNKNOWN`.

Rules are per asset class, interval and live market session — there is no single
universal threshold. Defaults (`freshness` config block):

| Setting | Default |
| --- | --- |
| `spot_fresh_seconds` | 120 |
| `spot_delayed_seconds` | 900 |
| `intraday_fresh_bars` / `delayed` / `stale` | 1 / 3 / 10 |
| `daily_delayed_sessions` / `stale` | 2 / 5 |
| `weekly_stale_days` / `monthly_stale_days` | 21 / 90 |
| `closed_market_max_age_days` | 7 |
| `provider_delay_grace_seconds` | 60 |

- A spot observation inside the fresh window is `FRESH`; inside the delayed window
  (plus the provider's own declared delay grace) it is `DELAYED`; beyond it,
  `STALE`.
- Intraday bars are counted in bars, daily bars in completed sessions, weekly and
  monthly bars in days.
- While an exchange is shut, the last completed session's close is
  `CLOSED_MARKET`: **not** invalid, and **not** a live quote. `live` is `False`.
- `MISSING` means nothing is stored; `PROVIDER_UNAVAILABLE` means stored
  observations exist but none could be used. A closed market with nothing stored
  is `MISSING` with the session state in the reason, never a provider outage.
- Every freshness verdict carries `market_state`, `coverage`,
  `last_completed_session`, `age_seconds`, `delay` and human-readable `reasons`.
- `GET /market/data/quote/{id}` always returns a `freshness` block, including when
  `quote.selected` is `null`, so a consumer can tell "the exchange is shut" from
  "a provider failed".

---

## 9. Fallback, reconciliation and circuit breakers

- Providers are attempted in the configured `data_providers` order (index =
  priority). Fallback happens per instrument and per operation, so one failing
  provider cannot block another.
- Each attempt records its outcome in the retrieval log with latency, and provider
  health moves to `degraded`/`unavailable` with the last error.
- `reconcile()` considers only observations of the same instrument; the newest
  usable observation anchors a 300 s window, and every observation inside it must
  agree within 0.5 %. Prices are **never averaged**.
- Disagreement, or a currency mismatch inside the window, yields `CONFLICT` with
  `selected = None` and the reasons; observations outside the window are retained
  but not compared (two sessions are not a disagreement).
- A provider's circuit opens after repeated failures, is skipped while open, and
  admits exactly one probe when half-open; recovery publishes
  `novamarket.data.provider_recovered`.
- Retries are bounded with exponential backoff; the daily quota and rate limiter
  stop a provider from being over-spent.

---

## 10. Storage and caching

**Engine:** SQLite through the standard library — the same persistence technology
NovaControl already uses, so no second database engine was introduced. The
repository opens at `<data_dir>/novamarket.sqlite3` (or an explicitly supplied
path, or in-memory for tests).

**Schema version 1**, recorded in `PRAGMA user_version`; a database from a newer
build is refused with an explicit error rather than read optimistically.

| Table | Holds |
| --- | --- |
| `market_instruments` | canonical instrument identities and provider symbol maps |
| `market_observations` | append-only price observations |
| `market_bars` | append-only validated OHLCV bars |
| `market_provider_health` | per-provider status, last error, latency, verified operations |
| `market_retrieval_log` | every provider call: operation, outcome, latency, error |
| `market_quality_events` | validation/quality outcomes with reasons |
| `market_corrections` | traceable corrections when a re-ingested value differs |

Historical integrity: a new price never overwrites an older observation. An
identical re-ingest is `DUPLICATE` (idempotent); a differing value for the same
identity is a `CONFLICT` stored as a correction, so both values remain traceable.

**Caching** (`cache` config block): latest quotes 15 s, instruments 300 s,
provider capability/health 60 s. A cache hit still re-evaluates freshness against
the current clock and session, so cached data is never presented as live. The
history page limit is 200 rows by default, and every read is bounded.

---

## 11. Ingestion scheduling and market calendar

One worker task per engine (`novamarket-market-data`), never one per instrument:

- `poll_seconds` 900 with `jitter_seconds` 30 to avoid synchronized bursts;
- `max_concurrency` 2, bounded per cycle;
- session-aware: quotes are requested while the market is open (`open`,
  `continuous`), and outside hours only when `run_when_closed` is set and the
  last completed session has no observation;
- daily bars are requested after the close unless `allow_partial_bars` is set;
- `stop_ingestion` signals, then awaits the task with a 5 s ceiling, and survives
  a worker exception;
- a restart is refused while a worker is live (no duplicate workers), and a
  failed cycle records the error on the subsystem health instead of crashing it;
- each cycle produces an `IngestionReport` with per-instrument quotes, bars,
  rejected counts, attempts, failures and duration.

Calendars (`calendar.py`), all dated 2026:

| MIC | Exchange | Timezone | Session | Holidays |
| --- | --- | --- | --- | --- |
| XNSE | National Stock Exchange of India | Asia/Kolkata | 09:15–15:30 | NSE circular NSE/CMTR/71775 + circular 03/2026 (15 Jan 2026) |
| XBOM | BSE Ltd. | Asia/Kolkata | 09:15–15:30 | same 2026 equity list as NSE |
| XNAS / XNYS / ARCX / XASE | Nasdaq, NYSE, NYSE Arca, NYSE American | America/New_York | 09:30–16:00 | NYSE Group 2026 calendar; early closes 2026-11-27 and 2026-12-24 at 13:00 |
| CRYPTO | continuous | UTC | 00:00–23:59:59 | none (trades continuously) |

An unknown MIC has no calendar: the state is `UNKNOWN` and freshness falls back
to interval thresholds instead of a guessed session.

---

## 12. API and events

All routes are authenticated and drive the one lifecycle-bound engine.

| Route | Purpose |
| --- | --- |
| `GET /market/data/status` | store stats and integrity, providers, ingestion worker, cache, circuits, last report |
| `GET /market/data/providers?probe=true` | declared capabilities, measured health, priorities, verified operations; `probe` runs a real liveness call |
| `GET /market/instruments` | paginated canonical instrument catalogue |
| `POST /market/instruments` | register one `MIC:SYMBOL` identity |
| `POST /market/instruments/lookup` | resolve and register instrument metadata through one provider |
| `POST /market/data/ingest` | one bounded cycle over the universe or named instruments, with per-instrument outcomes |
| `GET /market/data/quote/{instrument_id}?refresh=` | latest reconciled observation with quality, freshness and provenance, or `selected: null` plus the freshness block |
| `GET /market/data/history/{instrument_id}` | paginated observations |
| `GET /market/data/bars/{instrument_id}` | paginated validated OHLCV bars |

Events publish through the existing bus with typed payloads (no second bus, no
payload key shadowing the envelope):

| Event | Payload |
| --- | --- |
| `novamarket.data.received` | `instrument_id`, `provider`, `quality` |
| `novamarket.data.available` | `assets`, `sources` |
| `novamarket.data.unavailable` | `reason` |
| `novamarket.data.rejected` | `instrument_id`, `provider`, `reason` |
| `novamarket.data.stale` | `asset`, `provider`, `age_seconds` |
| `novamarket.data.freshness_changed` | `asset`, `fresh` |
| `novamarket.data.quality_changed` | `instrument_id`, `previous`, current quality |
| `novamarket.data.provider_unavailable` | `provider`, `reason` |
| `novamarket.data.provider_recovered` | `provider`, `circuit_state` |
| `novamarket.data.ingestion_failed` | `error_type`, `error` |
| `novamarket.data.validated` | declared (`instrument_id`, `quality`); `received` already carries the quality, so no publisher emits it yet |

---

## 13. Configuration

Added to the NovaMarket config contract (`GET /market/config` serves the resolved
values and schema):

```
data_providers: []            # provider names, in fallback priority order
provider_credentials: {}      # provider -> secret reference (never a value)
credentials_ref: ""           # default secret reference
universe: []                  # instrument ids collected by the worker
resources: {max_assets: 50, max_quotes_per_asset: 500, max_concurrent_fetches: 2}
ingestion: {enabled: false, poll_seconds: 900, intervals: ["1d"], include_quotes: true,
            include_bars: true, run_when_closed: false, allow_partial_bars: false,
            max_concurrency: 2, history_limit: 250, jitter_seconds: 30}
freshness: {...}              # see §8
quality: {jump_warning_ratio: 0.2, jump_suspect_ratio: 0.5, volume_warning_multiple: 10.0,
          volume_suspect_multiple: 50.0, reconciliation_tolerance_ratio: 0.005,
          reconciliation_window_seconds: 300, volume_median_window: 20}
cache: {quote_ttl_seconds: 15, instrument_ttl_seconds: 300, provider_ttl_seconds: 60}
history_page_limit: 200
```

To collect data: register instruments (`POST /market/instruments`), list them in
`universe`, name providers in `data_providers`, then enable ingestion or call
`POST /market/data/ingest`. A provider that requires a credential and has none is
skipped rather than reported as ready.

---

## 14. Security and licensing

- No credential is stored in source, configuration or documentation. Only secret
  *references* are stored, and `SecretResolver` reads them from the environment or
  the state store.
- Responses carry a reference and a redacted hint at most; values are never logged
  or returned.
- Rate limits and daily quotas are enforced locally; the adapters stop on a
  provider rate-limit response instead of retrying harder.
- No scraping bypass, subscription circumvention or unauthorized purchase exists in
  the code; only documented free endpoints are used.
- Data is never labelled real-time: every provider is declared `delayed` with its
  typical delay, and every observation carries its source timestamp.
- Licensing limits (Yahoo unofficial/delayed, CoinGecko aggregate + free tier,
  Alpha Vantage free tier) are recorded in each adapter's
  `capabilities.limitations` and are served by the API.
- No real-money trading path exists; NM-02 adds no order route.

---

## 15. Observability and performance

`GET /market/data/status` exposes: store stats per table and schema version, store
integrity and last error, provider registry rows (health, latency, last error,
circuit state, priority, verified operations), ingestion worker state and last
report, cache entries/hits/misses/hit rate with TTLs, and cycle count.

Measured on this machine (`scripts/verify_marketdata_live.py`, 2026-11-02, real
providers, no fixtures):

| Instrument | Outcome | Duration |
| --- | --- | --- |
| XNSE:RELIANCE | 247 daily bars; no quote (NSE closed) | 1 930 ms |
| XNAS:AAPL | 1 quote (342.885 USD, fresh, live) | 413 ms |
| CRYPTO:BTC | 1 quote (78 012 USD, fresh, live) + 249 bars, 3 provider attempts | 5 545 ms |

Store held 3 instruments, 3 observations and 496 bars; cache reported 21 hits /
6 misses (hit rate 0.778); provider health `coingecko: ok`, `yahoo: ok`,
`alpha-vantage: unknown` (no key); verified operations `ohlcv`, `probe`, `quote`.

---

## 16. Testing

`tests/test_novamarket_marketdata.py` (41 passed, 2 skipped):

| Spec area | Coverage |
| --- | --- |
| Adapters (§17) | normalization, schema change vs unknown symbol, bounded retries, timeout, authentication failure, missing volume, declared capabilities |
| Normalization | decimal/currency/timezone handling, cross-currency rejection, duplicate identity |
| Quality | genuine large move flagged not deleted, previous close stale not invalid, impossible bars refused, volume outlier against median |
| Storage | idempotent re-ingest, traceable correction, history across reopen, bounded pagination, provider health + retrieval log persisted |
| Fallback | secondary provider after primary failure, both failing, circuit opening, half-open single probe, disagreement not averaged |
| Lifecycle | data through the real lifecycle, history across disable/enable, background start/stop/never duplicate, failure degrades health with a reason, unknown provider reported |
| Integration | full HTTP pipeline (register → ingest → read → report), unavailable quote still explains why, distinct invalid-input statuses |
| Live | Yahoo and CoinGecko real-provider tests behind an explicit opt-in guard so the default suite never touches the network |

Commands and counts are recorded in §19 G.

---

## 17. Known limitations

1. **Delayed data only.** No integrated provider offers licensed real-time NSE/BSE
   or US data; every observation is labelled delayed with its delay.
2. **Alpha Vantage is unverified live** — no key was supplied, so its live quote
   and OHLCV paths are exercised only by fixtures.
3. **CoinGecko free OHLC** is 30-minute, recent-day and volume-free; the adapter
   declares exactly that.
4. **Yahoo is an unofficial endpoint** and may change or throttle without notice.
5. **Calendars cover 2026 only** for NSE, BSE and the four US venues; a date
   outside the covered years reports `UNKNOWN` session state rather than a guessed
   schedule. No comprehensive multi-year holiday coverage is claimed.
6. **No corporate-action adjustment.** Splits and dividends are not normalized; a
   split appears as a large move (flagged `WARNING`/`SUSPECT`), so long-horizon
   series must be treated with care until NM-11 backtesting addresses adjustment.
7. **No FX conversion.** A foreign-currency price is never converted to INR; it is
   stored with its currency and refused as an INR price. Timestamped exchange-rate
   ingestion is not part of NM-02.
8. **Process-local counters.** Alpha Vantage's daily quota and the rate limiters
   reset on restart.
9. **`novamarket.data.validated` is declared but not emitted** (`received` carries
   the quality; `quality_changed` carries transitions).
10. **Single-process SQLite** — adequate for the observe mode this build ships; a
    multi-process collector would need a different write path.
11. **No streaming/websocket feed**, and no order-book or trade-print data.
12. **No UI** — the final NovaMarket workspace remains deferred until after
    NovaControl Phase 39; the API and CLI path is the surface.

---

## 18. Decisions and rationale

- **SQLite, not a second database engine** — NM-01 already persists through
  SQLite; NM-02 adds tables in its own file with a versioned schema instead of
  introducing PostgreSQL/Redis for a single-process observer.
- **`MIC:SYMBOL` identity** — symbols are not unique across exchanges; encoding
  the MIC prevents an NSE and a BSE listing from merging.
- **Append-only plus corrections** — history must never be silently rewritten, so
  a changed value is a new traceable correction record, not an update.
- **Refuse, don't convert** — a cross-currency quote is rejected as invalid
  because NM-02 has no timestamped FX source; converting would invent a rate.
- **Priority reconciliation, never averaging** — averaging two providers' prices
  produces a value nobody quoted.
- **`verified_operations` separate from declarations** — a capability is not
  advertised until a call has actually succeeded and been persisted.
- **Freshness always attached to a quote read** — "no observation" is an answer a
  consumer must be able to act on, and a closed market must not look like an
  outage.
- **Exchange carried by the MIC** — `instrument_id` is the market identifier code
  plus symbol, which identifies the venue without duplicating it in every row; the
  provider's own venue code is preserved in `provider_metadata`.

---

## 19. Verification record — NM-02 acceptance (2026-11-02)

### A. Overall verdict

**PASS**, with one explicitly excluded check: *live* secondary-provider fallback
(see criterion 14 below), which NM-02 §20 permits to be marked separately while
the fallback contract itself is verified with controlled tests. Criterion 14's
mandatory requirement — fallback tested where multiple compatible providers are
configured — is verified by `FallbackAndCircuitTests`.

| # | Acceptance criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | At least one genuinely accessible provider integrated and verified | **PASS** | Yahoo (AAPL 342.885 USD) and CoinGecko (BTC 78 012 USD) live, §19 C |
| 2 | Adapters use the NM-01 architecture and contracts | **PASS** | registry/engine built inside `NovaMarketService`, routes behind `require_auth`, events on the shared bus |
| 3 | Data normalized into canonical models | **PASS** | `ProviderQuote`/`ProviderBar` → `PriceObservation`/`OhlcvBar`, §5–6 |
| 4 | Observations preserve provenance and timestamps | **PASS** | `observed_at` ≠ `retrieved_at`, source timezone, provider metadata, `provenance()` |
| 5 | Currency and decimal handling correct | **PASS** | `Decimal` prices, explicit currency, cross-currency rejected |
| 6 | OHLCV validation where supported | **PASS** | structural bar validation plus stored bars for RELIANCE/AAPL/BTC |
| 7 | Data-quality validation works | **PASS** | jump/volume/stale/structural rules, rejections persisted |
| 8 | Freshness and stale handling work | **PASS** | §8; a stale close is never reported live |
| 9 | Invalid data cannot silently enter downstream use | **PASS** | `usable` excludes SUSPECT/INVALID/UNAVAILABLE; reconciliation returns no price on conflict |
| 10 | Historical observations persisted reliably | **PASS** | reopen test; 496 bars and observations across cycles |
| 11 | Duplicate ingestion deterministic | **PASS** | `DUPLICATE` idempotency plus `CONFLICT` correction tests |
| 12 | Cache behaviour respects freshness | **PASS** | TTLs re-checked against clock/session on read; §10 |
| 13 | Provider failure handling works | **PASS** | health/retrieval log, circuit breaker, degraded status, failure isolation |
| 14 | Fallback tested with multiple compatible providers | **PASS (contract)**, live secondary **NOT PASS** | controlled tests cover secondary success, both failing, disagreement, half-open; no two live providers share a covered instrument |
| 15 | Background ingestion start/stop/restart safe | **PASS** | worker tests, single-task guard, 5 s stop ceiling |
| 16 | NovaControl can request and inspect validated data | **PASS** | full HTTP pipeline test plus the live script |
| 17 | Events and health information work | **PASS** | typed `novamarket.data.*` payloads, status/health surfaces |
| 18 | Provider limitations and licensing documented | **PASS** | §2–3, §14, `capabilities.limitations` |
| 19 | Financial and data invariants preserved | **PASS** | INR/NC untouched, no conversion, no order path, `mode` stays `observe` |
| 20 | Relevant integration and regression tests pass | **PASS** | §19 G |
| 21 | NovaForge and unrelated functionality undisturbed | **PASS** | only `novamarket/`, its tests, `core/events.py`, `api/*`, docs and the live script changed; regression suites green |

### B. Defects found and repaired in this verification pass

1. **`freshness_of` called a closed market a provider outage.** It derived
   `provider_unavailable` from `ReconciliationStatus.UNAVAILABLE`, which also
   covers "nothing has ever been stored" — so an instrument with no observation
   because its exchange was shut reported `PROVIDER_UNAVAILABLE`, sending a
   consumer after a failure that never happened. Now `provider_unavailable` is
   derived from stored-but-unusable observations only
   (`marketdata/engine.py`, `freshness_of`).
2. **`missing_assessment` did not say why nothing was stored.** Its reason was the
   constant "no observation is stored for this instrument" even while the exchange
   was shut. It now appends the session state, e.g. "the NSE market is pre open; no
   live quote is collected while it is shut" (`marketdata/freshness.py`).
3. **`/market/data/quote` had no top-level freshness verdict.** A consumer could
   not distinguish "market closed" from "provider failed" when `selected` was
   null. `market_quote` now always returns a `freshness` assessment block
   (`service.py`).
4. **`docs/API.md` was out of sync** with `ROUTE_CONSUMERS` after the market-data
   routes landed (`generate_api_reference.py --check` failed); regenerated.
5. **`docs/NOVAMARKET.md` disagreed with the code** (NM-02 still "Contract only",
   "no route for prices", the durable repository still listed as deferred);
   corrected, with this document as the NM-02 record.

Each repair is pinned by a test: `EnginePipelineTests.
test_a_closed_market_with_nothing_stored_is_missing_not_an_outage`, the
`PROVIDER_UNAVAILABLE` assertion in `test_a_conflicting_repeat_is_retained_as_correction`,
and `MarketDataApiTests.test_an_unavailable_quote_still_explains_why`.

### C. Real data verification

`PYTHONPATH=src ... scripts/verify_marketdata_live.py` — real providers, real
network, no fixtures:

```
-- XNSE:RELIANCE --
  ingest ok=True quotes=0 bars=247 rejected=0 attempts=1 failures=0 duration_ms=1929.967
  quote status=unavailable freshness=missing market_state=closed
  reasons=no observation is stored for this instrument; the NSE market is closed; ...
-- XNAS:AAPL --
  selected: 342.885 USD from yahoo observed=2026-11-02T14:37:16+00:00 freshness=fresh quality=valid live=True
  assessment=fresh market_state=open age_seconds=1.126 last_completed_session=2026-10-30
-- CRYPTO:BTC --
  selected: 78012 USD from coingecko observed=2026-11-02T14:36:50+00:00 freshness=fresh quality=valid live=True
  assessment=fresh market_state=continuous age_seconds=32.682 last_completed_session=2026-11-01
market-data status:
  initialised=True store_ok=True instruments=3 observations=3 bars=496 schema_version=1
  cache hits=21 misses=6 hit_rate=0.7778
  circuits={'coingecko': 'closed', 'yahoo': 'closed'}
  verified_operations: ['ohlcv', 'probe', 'quote']
  provider_health: [('alpha-vantage', 'unknown'), ('coingecko', 'ok'), ('yahoo', 'ok')]
LIVE VERIFICATION OK
```

Fixture coverage is separate (§16) and is not counted as provider verification.

### D. Data integrity

- **Timestamps** — source time and retrieval time are distinct and timezone-aware;
  Yahoo's `America/New_York` provenance and CoinGecko's UTC `last_updated_at` both
  arrived intact.
- **Currency** — AAPL and BTC store as USD with their real currency; no INR
  reinterpretation; a cross-currency quote is rejected (test-pinned).
- **Decimals** — prices and volumes are `Decimal`; serialized with
  `format(value, "f")`.
- **OHLCV** — 247 NSE daily bars, 250 US daily bars and 249 crypto bars stored;
  impossible bars refused by tests; missing volume stored as absent.
- **Freshness** — the closed-market case reports `missing` + `closed` (repaired,
  §19 B), never a fake live price and never an invented outage.
- **Provenance** — provider, observed/retrieved times, timezone, quality,
  freshness and provider metadata accompany every selected quote.

### E. Reliability

Controlled tests cover primary failure with secondary success, both providers
failing (`unavailable`, nothing stored), circuit opening (no further provider
spend), half-open admitting exactly one probe, provider recovery, disagreement
without averaging, bounded retries and a timeout. Live evidence: CoinGecko and
Yahoo both `ok` with circuits closed; Alpha Vantage correctly `unknown` without a
key rather than falsely `ok`.

### F. Performance

Ingestion latency and cache behaviour are in §15. The worker is a single task with
bounded concurrency, a 900 s poll and 30 s jitter; there is no per-instrument
worker and no unbounded queue, and collection runs off the request path, so the
GUI is unaffected.

### G. Tests run

| Check | Command | Result |
| --- | --- | --- |
| NM-02 phase suite | `pytest tests/test_novamarket_marketdata.py -q` | **41 passed, 2 skipped** (live opt-in) |
| NM-01 contracts | `pytest tests/test_novamarket.py tests/test_novamarket_api.py -q` | **103 passed**, 75 subtests |
| Integration/regression | `pytest tests/test_web_api.py tests/test_optimization.py -q` | **150 passed**, 77 subtests |
| Types | `mypy src/novacontrol/novamarket` | clean, 24 files |
| API reference | `python scripts/generate_api_reference.py --check` | in sync |
| Live pipeline | `scripts/verify_marketdata_live.py` | `LIVE VERIFICATION OK` |

The full suite was not run, per the phase instruction; the focused phase suite,
its NM-01 dependencies and the shared-API regression set were.

### H. Remaining limitations and unverified requirements

- **Live secondary-provider fallback: NOT PASS.** Neither live provider covers the
  other's instruments (Yahoo equity vs CoinGecko crypto), so a live
  cross-provider failover could not be demonstrated without a second credentialed
  provider. Alpha Vantage is installed for exactly that role and would provide it
  once an operator supplies `env:ALPHA_VANTAGE_API_KEY`.
- **Alpha Vantage live path unverified** (no key).
- **No real-time feed**, no corporate-action adjustment, no FX conversion
  (§17 items 1–7).
- **Calendars are 2026-only**; other years report `UNKNOWN` rather than a guess.
- **`novamarket.data.validated`** is declared, not emitted.

### I. NM-03 readiness

Ready. NM-03 (NovaCoin Wallet & Deterministic Ledger) can consume normalized,
validated, timestamped observations through the engine's internal reads —
`latest()`, `observations()`, `bars()`, `freshness_of()` — instead of touching a
provider, and every observation carries an explicit currency and a `Decimal`
price, so ledger postings can require INR explicitly and refuse anything else.
Two constraints carry forward: NC = ₹1 means a foreign-currency price is **not**
postable until a timestamped FX source exists, and `usable` must remain the gate,
because `SUSPECT`/`INVALID` observations are retained by design and are not
ledger-safe.
