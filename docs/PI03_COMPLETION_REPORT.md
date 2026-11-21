# PI-03 Completion Report — Low-Latency Market Data Gateway

**Phase:** PI-03
**Branch:** `feature/novamarket`
**Verdict:** **PASS for the PI-03 scope**
**Date:** 2026-11-13

This is the single consolidated PI-03 report. It records the latency
investigation, the gateway implementation, actual verification evidence, defects
fixed and remaining provider/environment limitations. PI-04 was not started and
no Git staging, commit, push, tag, merge or publish was performed.

## 1. Initial latency investigation

PI-01 and PI-02 were re-read before this phase. PI-01 is recorded as **PASS for
its scope**, with a previously recorded PARTIAL on an unrelated whole-repository
regression; PI-02 is recorded as **PASS with its stated limitations**. The
dependency gate was therefore satisfied.

The existing NM-02 path was inspected end to end: provider adapters and
transports, adapter rate limiters and daily quotas, retry/backoff and
`Retry-After`, circuit breakers, normalization, freshness and quality
assessment, reconciliation, the append-only SQLite store, exchange calendars,
background ingestion, the service composition root, status/contracts, typed
events, prediction reads and the historical dataset engine.

The current tree contains **no NSE/BSE website scraper**. The installed free
providers are Yahoo Finance, CoinGecko and Alpha Vantage. The existing general
background worker has `ingestion.poll_seconds=900` (15 minutes) and
`IngestionSettings.enabled=False` by default. The older NM-02 engine already
performs asynchronous provider calls, retries, per-adapter rate limiting,
daily-quota enforcement, circuit breaking, normalization, validation,
persistence and event publication. It did not have a general priority-aware
batch rotation queue; the gateway adds that policy without creating a second
discovery or provider engine.

The latency causes found in the actual code are therefore:

1. **Provider publication delay.** Yahoo declares a 900-second typical delay and
   is an unofficial third-party source. CoinGecko declares 120 seconds. Alpha
   Vantage is daily-only and keyed. None supplies a separate publication
   timestamp.
2. **General scheduling delay.** The preserved general worker polls every 900
   seconds. This is appropriate for general monitoring but is not a low-latency
   intraday scheduler.
3. **Provider API throttling.** Yahoo is limited to 60 requests/minute with a
   1-second adapter limiter; CoinGecko to 10/minute with a 6-second limiter;
   Alpha Vantage to 5/minute and 25/day on the free tier. HTTP 429 is a typed
   failure with `Retry-After` support.
4. **Provider response time.** Measured here in the hundreds of milliseconds,
   not minutes (see §6).
5. **Closed-market source age.** A real Yahoo read on 2026-11-13 returned a
   previous-session value (source age 45,476.1 seconds). That is a closed-market
   print, not a live quote; freshness must not relabel it.
6. **Parsing, validation and persistence.** These are part of the existing
   pipeline and retrieval log. The engine does not expose separate parsing and
   validation durations; the gateway reports that absence rather than inventing
   a number. SQLite persistence is measured through the stored observation's
   creation timestamp.
7. **Distribution.** The gateway measures consumer-availability delay from the
   persistence/response timestamp to the read. Existing typed events carry the
   validated observation metadata.

## 2. Root causes identified

The dominant avoidable latency was **not** a slow or hidden NSE/BSE scraper. It
was the combination of:

- a 900-second general refresh policy that is intentionally preserved; and
- provider publication delay (Yahoo’s declared 15 minutes, and closed-market
  values that can be hours old).

The gateway addresses the avoidable internal/scheduling component with a
separate opt-in intraday path, priority/fair batching, request coalescing, a
short read cache and checkpoint semantics. It does **not** claim to remove
provider delay or turn a delayed third-party value into exchange real-time data.
No paid subscription, access-control bypass, browser automation or repeated
scraping was introduced.

## 3. Existing components reused

| Existing component | PI-03 use |
|---|---|
| `MarketDataEngine` | Acquisition, normalization, freshness/quality, reconciliation, events, background worker |
| Provider adapters (`YahooAdapter`, `CoinGeckoAdapter`, `AlphaVantageAdapter`) | The only provider implementations; no second adapter registry |
| `MarketDataProvider` base retry/rate-limit/quota machinery | Retry, limiter, daily quota, `Retry-After` and circuit behavior; retry observer added for telemetry |
| `MarketDataStore` | The only observation/bar persistence; one new stored-at lookup |
| Exchange calendars | Intraday open/closed decisions |
| NM-02 availability events | Distribution, extended with source/reception/freshness metadata |
| PI-01 prediction contracts | Read-only consumers; no order authority changed |
| PI-02 historical dataset engine and store | Historical authority preserved through engine delegates |
| `NovaMarketService` | Lazy gateway composition, lifecycle, status and contracts |
| Existing `EventBus`, capability, permission and architecture registries | Typed distribution and three gateway capabilities |

No collector, database, provider registry, validation engine, scheduler queue,
risk authority or execution authority was duplicated.

## 4. Gateway implementation

`src/novacontrol/novamarket/marketdata/gateway.py` adds:

- `GatewaySettings`, an opt-in, validated policy object. `enabled=false` by
  default; the general 900-second floor cannot be lowered through its
  `general_refresh_floor_seconds`; the 20-instrument batch ceiling cannot be
  raised; intraday resolutions are restricted to supported intraday intervals.
- `AcquisitionPriority` with six legitimate application states: active position,
  pending paper order, forecast confirmation, autonomous candidate, watchlist
  and general. `select_batch` gives higher priorities the primary allocation,
  reserves lower-priority slots and rotates each level to prevent starvation.
  The batch remains capped at 20 and an aggregate per-minute request budget is
  enforced in addition to adapter limits.
- A separate `run_intraday_cycle` that selects one bounded fair batch, skips
  closed exchanges unless explicitly configured otherwise, calls the existing
  engine with quote/intraday intervals, and never starts or changes the general
  worker. `run_general_cycle` is an explicit general-path delegate.
- `refresh` with request coalescing and a freshness check; `current`,
  `observation` and `latest` reads from the same engine. Cached results do not
  cache freshness: it is recomputed on every read.
- `checkpoint`, returning explicit `available=false` for missing, delayed,
  stale or conflicting data, plus observation/reception/provider/resolution/
  validation metadata, whether the value is newer than the previous checkpoint,
  and whether any new observation exists. It never fabricates a price.
- Provider contracts that expose identity, exchanges, intervals, quote/history
  support, source class, streaming/publication support, limiter/quota data,
  health, failure class, last failure, cooldown, retries, request counts and
  historical depth. Fallbacks preserve provider identity and name the preferred
  provider’s failure.
- Latency/provenance fields: source-observed, provider-response/reception,
  validation, persistence and consumer-availability timestamps; source age,
  source-to-reception, provider request, persistence, distribution and total
  internal times; P50/P95/P99 summaries from collected samples only.
- An explicit observability surface in gateway status: read/fresh/stale/missing
  counts, fallback and conflict counts, checkpoint availability and stale
  rejections, fresh-observation availability, and the last acquisition state per
  instrument. These counters describe actual reads; they are not synthetic
  latency samples.
- `NovaMarketService.gateway()` plus `gateway_status`, `gateway_contract`,
  subscriptions, prioritize/unsubscribe, refresh, intraday cycle, start/stop,
  provider contracts and latency access. The service starts the gateway only
  when `gateway.enabled=true` and stops it during withdrawal. Three capabilities
  (`market.gateway.read`, `market.gateway.intraday`, `market.gateway.manage`) are
  registered through the existing permission/operation registry.
- PI-02 historical reads delegate to `MarketDataEngine.bars`,
  `MarketDataEngine.observations` and `MarketDataEngine.ingest_history`. No
  dataset or historical store was copied.

No HTTP route was added in this phase, matching the existing PI-01/PI-02
composition pattern. Consumers use the service/status/contracts boundary, and a
later phase may add routes only by updating `ApiSurface`, route consumers,
`docs/API.md` and the route tests together.

## 5. Provider capabilities verified

Capabilities were read from the real adapters, not assumed:

| Provider | Data kind / delay | Source class | Streaming | Publication timestamp | Free limits | Intervals |
|---|---|---|---|---|---|---|
| Yahoo Finance | delayed, 900 s typical | `third_party_delayed` | no | no | 60/min, adapter 1 s limiter | 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo |
| CoinGecko | delayed, 120 s typical | `third_party_delayed` | no | no | 10/min, adapter 6 s limiter | 30m |
| Alpha Vantage | delayed, 900 s typical | `third_party_delayed` | no | no | 5/min, 25/day; credentials required | 1d |

Yahoo is an **unofficial** delayed source, not an exchange feed. CoinGecko is
crypto-only and its free OHLC is 30-minute/one-day with no volume. Alpha Vantage
is daily-only and was not enabled without a credential. None of these providers
supports a streaming/quote-push interface, so the gateway uses controlled
polling. No free provider can honestly be described as exchange-grade
real-time.

## 6. Freshness and latency measurements

### 6.1 Live provider observations (one sample each)

Measured on 2026-11-13 with the real `UrllibTransport` and a single permitted
request per provider:

| Provider | Provider response time | Source age at observation | Result interpretation |
|---|---:|---:|---|
| Yahoo Finance (RELIANCE.NS) | 371.99 ms | 45,476.1 s | Closed-market previous-session value; not a live quote |
| CoinGecko (BTC) | 140.13 ms | 77.2 s | Recent crypto observation |

The existing live-provider test class also passed both real reads (`2 passed in
1.26s`). These are single observations, not percentiles. The gateway therefore
does **not** claim p50/p95/p99 provider or source-age performance from them.

### 6.2 Gateway latency surface

`status.gateway.latency` reports:

- `provider_request_ms`: from the append-only retrieval log; P50/P95/P99 only
  when samples exist;
- `source_age_seconds` and `source_to_reception_seconds`: from the selected
  observation and the read clock;
- `persistence_ms`: from the store's durable write timestamp;
- `distribution_ms`: selected observation to the consumer read;
- `internal_cycle_ms`: gateway cycle duration;
- `parsing_ms` and `validation_ms`: explicitly unavailable as separate
  measurements because the engine does not expose them;
- `source_publication_delay`: unavailable for the installed providers because
  none supplies a separate publication timestamp; when provider metadata does
  supply one, the gateway measures publication-to-observation and reports the
  collected distribution.

No synthetic fixture value is reported as live latency. Closed-market Yahoo data
is classified by the existing calendar/freshness policy and is never presented
as fresh.

## 7. Rate-limit and failover verification

- Adapter limiters, daily quotas, retries/backoff with jitter, `Retry-After`,
  circuit breakers and typed 429 failures remain the provider authority.
- The gateway adds an aggregate `max_requests_per_minute` budget and reports
  used/remaining requests, throttle events, retries, cooldowns and circuit
  states. A budget-exhausted cycle reports an explicit reason rather than
  pretending it ran.
- Focused tests cover 429 classification, retry-observer behavior, rate-limit
  contracts, cache/coalescing, forced refresh, and a provider failure that
  leaves the gateway/engine usable.
- Fallback was exercised with a preferred provider failing and a second provider
  succeeding: the gateway selected the fallback, set `is_fallback=true`,
  preserved the fallback’s provider/source metadata and recorded the preferred
  provider’s failure reason.
- Conflict handling is inherited from the engine: conflicting observations
  produce `CONFLICT`, retain every observation and select nothing; the gateway
  reports `provider_conflict=true` and exposes the retained observation details.
  It never averages or silently substitutes a lower-quality source. The focused
  test now exercises a real two-provider conflict through the engine/store and
  asserts that no price is selected.

## 8. Integration evidence

- **Real application path:** `ServiceGatewayIntegrationTests` enables a real
  `NovaMarketService`, registers an instrument, opens the gateway over the real
  engine/store, refreshes through the real Yahoo adapter with a deterministic
  provider transport, verifies a fresh persisted observation and shuts down
  cleanly. This is not a live Yahoo call; the separate live test/measurement in
  §6 is.
- **Real engine/store:** `_GatewayFixture` tests use the real SQLite
  `MarketDataStore`, real `MarketDataEngine`, real reconciliation/freshness code
  and a deterministic provider adapter.
- **PI-01/PI-02:** the affected regression includes prediction, datasets,
  market-data, API and forecasting suites; all passed. The historical gateway
  methods delegate to the PI-02 authority and no dataset code was changed.
- **Capabilities:** the API status test now reports 57 capabilities, including
  the three gateway capabilities, and passes.
- **Events:** validated observation tests assert that `novamarket.data.received`
  and `novamarket.data.validated` carry provider, source time, reception time and
  freshness metadata.
- **General policy:** the gateway status reports the preserved 900-second
  general floor; the general-cycle test asserts an explicit empty-universe
  result, the bounded selection path and that the general worker task was not
  started by the gateway.

## 9. Tests and actual results

| Check | Result |
|---|---|
| Focused PI-03 tests (`tests/test_novamarket_gateway.py`) | **18 passed** |
| Affected regression: datasets, prediction, novamarket, marketdata, API, forecasting, gateway | **320 passed, 2 skipped, 319 subtests passed in 92.37 s** |
| Live provider tests (`NOVAMARKET_LIVE_TESTS=1`) | **2 passed in 1.26 s** |
| `python -m mypy src` | **Success: no issues found in 589 source files** |
| `validate_architecture()` | **`()`** |
| `git diff --check` | exit 0 (line-ending warnings only) |

The two skips are the existing live-quote tests when
`NOVAMARKET_LIVE_TESTS` is not set; they were run separately above and passed.
The whole-repository suite was not rerun for this phase; the affected regression
is the evidence used here, and the earlier PI-01 whole-repo PARTIAL remains a
separate historical limitation.

## 10. Defects fixed during PI-03

1. **Telemetry read failed silently.** The gateway asked `retrieval_log` for
   5,000 rows; the store caps a page at 1,000, so the exception was swallowed and
   provider failure/latency evidence appeared empty. Changed to the supported
   limit.
2. **Cached freshness could stay fresh.** The read cache stored a freshness
   assessment; a cache hit could return it after the quote aged out. Freshness is
   now recomputed on every read while only the engine result is cached.
3. **Spot interval could reach the bar path.** An intraday configuration with
   `spot` would be passed to the engine’s OHLCV collector. Spot is now filtered
   out of the bar interval list; quotes are still collected.
4. **Fallback was not identified when the preferred provider produced no row.**
   Fallback was inferred only from providers present in the reconciliation.
   Selection now compares against the first configured provider that declares
   the instrument's market and records that provider’s failure.
5. **Request budget edge case.** `select_batch(limit=0)` treated zero as
   “default”; it now honors zero, and the intraday cycle handles an exhausted
   budget explicitly.
6. **General-cycle error was sticky.** A later successful general cycle left the
   previous error in status; it now clears on success.
7. **Provider contract last-failure used a successful row.** It now selects the
   newest non-OK row, so the failure class reflects the actual failure.
8. **No source-class/streaming/publication declaration existed.** Added the
   three capability fields with safe delayed/unknown defaults and exposed them in
   provider contracts.
9. **No stored-at lookup existed.** Added `observation_stored_at` so persistence
   latency is measured from the durable row, not guessed.
10. **Retry telemetry was absent.** Added an opt-in retry observer to the
    existing adapter retry path and counted retries by provider; the observer
    cannot change retry policy.
11. **A non-supporting provider could be mislabeled as a failed fallback.** The
    gateway previously treated the first enabled provider as preferred even when
    it did not declare the instrument's market. Preferred selection now checks
    asset class/exchange/MIC support before claiming a fallback.
12. **Closed instruments could consume a batch slot.** A batch of closed
    high-priority instruments spent no request budget but still occupied all 20
    slots, starving open lower-priority names. Closed instruments are now removed
    before fair selection and reported as skipped; open names fill the batch.
13. **The general gateway delegate bypassed the batch cap and rotation.** It
    now selects a rotated, priority-aware batch capped at 20 and updates the
    same fairness bookkeeping as the intraday path.
14. **Gateway policy could be enabled at runtime without starting.** A
    configuration change containing `gateway` now follows the enabled/disabled
    lifecycle just like an ingestion change, instead of leaving a stale worker
    state.
15. **Gateway status lacked required observability counters.** Added explicit
    fresh/stale/missing, fallback, conflict, checkpoint and stale-rejection
    counters plus per-instrument acquisition state.
16. **Publication delay was inverted.** The first implementation subtracted the
    observation time from the publication time, producing zero for a normal
    provider timestamp. It now measures publication-to-observation and exposes
    the distribution only when a provider actually supplies a publication
    timestamp.
17. **The general gateway delegate could bypass the request budget.** Its
    selected list is now capped by the remaining per-minute budget and it spends
    that budget explicitly; an empty selected list can no longer fall through to
    an unbounded engine-universe call.
18. **An all-closed intraday batch reported a false budget failure.** Budget
    exhaustion is now reported only when there is an open instrument that could
    have been requested.

## 11. Remaining limitations

- **No exchange-grade real-time data.** Yahoo is unofficial and delayed;
  CoinGecko is delayed/limited; Alpha Vantage is daily/keyed. None is an
  exchange-originated or streaming feed. The gateway reports this rather than
  relabelling the source.
- **Yahoo’s 15-minute declared delay cannot be optimized away.** On a closed
  market the source age can be many hours. The live sample above is a
  closed-market value, not a current NSE quote.
- **No separate publication timestamp from the installed providers.** Source
  publication delay is unmeasurable from Yahoo/CoinGecko/Alpha Vantage as they
  currently respond. Source-to-reception is measurable; actual publication delay
  is not. The gateway has a tested path for a provider that does supply one.
- **No separate parsing/validation timing.** The engine does not expose those
  durations; they are reported unavailable, not fabricated.
- **Validation completion is represented by the retrieval time.** The gateway
  exposes validation status/reasons but does not have a distinct persisted
  validation timestamp beyond the existing engine/store fields.
- **Live latency is a single sample per provider.** No p50/p95/p99 claim is made
  for live provider latency or source age.
- **The gateway has no HTTP route in PI-03.** It is reachable through the
  service/status/contracts boundary, following PI-01/PI-02. A future route must
  update the route registry, consumers, API reference and tests together.
- **The PI-09 forecast scheduler is not implemented here.** `checkpoint` is the
  acquisition answer the future scheduler will call at T−5/T−1. PI-03 does not
  claim to schedule 5/15/20-minute forecast checkpoints itself.
- **Priority is an explicit contract, not automatic cross-domain inference.**
  The gateway accepts `active_position`, `pending_paper_order`,
  `forecast_confirmation`, `autonomous_candidate`, `watchlist` and `general`
  subscriptions from the owning application components. It does not independently
  query positions, paper orders, autonomy state or watchlists to infer priority;
  doing so would duplicate those authorities. Callers must set and update the
  subscription when that state changes.
- **Provider cooldowns are reported, not independently timed.** The existing
  adapter limiter/quota and engine circuit breaker enforce cooldowns; the gateway
  reports the resulting state and does not maintain a second cooldown clock.
- **The general gateway delegate is still explicit.** The existing background
  worker remains the general scheduling authority; `run_general_cycle` is a
  bounded manual/delegated pass and does not replace that worker.
- **Fair rotation is a gateway policy addition.** The NM-02 engine had no
  general priority rotation; the gateway supplies bounded rotation without
  duplicating discovery or provider scheduling.
- **No paid data.** The phase deliberately does not add a subscription,
  credential, private exchange API or access-control bypass.

## 12. Final acceptance verdict

**PASS for PI-03.** The central gateway is implemented over the existing
provider/engine/store authorities; general and intraday paths are separate; the
900-second floor and 20-instrument cap are preserved; priorities and fair
rotation prevent starvation; rate limits, retries, cooldowns and fallbacks use
the existing machinery; freshness is recomputed rather than cached as fresh;
source/provider/reception/persistence/latency metadata is exposed; checkpoints
are explicit about unavailable/stale data; PI-01 and PI-02 integrations were
regressed successfully; capabilities, configuration, architecture and docs are
updated; and no PI-04 work was started.

The provider limitations above are real environmental constraints, not
successful low-latency delivery claims. A verified PASS for PI-04 may begin only
after the user authorizes the next phase.
