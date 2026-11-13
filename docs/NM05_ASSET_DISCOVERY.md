# NM-05 — Asset Discovery

## 1. Universe

`UniverseFilter` is the boundary of a discovery run. It is a value object, not a
claim: it is initialized with the classes this build can actually evaluate.

| field | default | meaning |
| --- | --- | --- |
| `asset_classes` | `("equity", "etf", "index")` | NM-02 asset classes scanned; validated against `AssetClass` by the store |
| `exchanges` | `()` (all) | explicit exchange/MIC allow-list |
| `max_instruments` | `200` | hard cap per cycle (1…2000) |
| `exclude_unknown_trading_status` | `False` | skip instruments whose status is not known |

Because the universe is applied **through NM-02’s catalogue query**
(`list_instruments(asset_class=…, exchange=…, limit=…)`), an asset class that the
catalogue does not carry contributes nothing. Adding “commodities” or “crypto” to
the configuration would not invent them; those classes are excluded from the
default universe because the current data path is the NM-02 instrument registry,
not a live exchange listing feed.

## 2. Where candidates come from

| Source | Call | Notes |
| --- | --- | --- |
| Instrument catalogue | `MarketDataEngine.list_instruments` | only real, registered instruments; no symbol strings are fabricated |
| Market data | `MarketDataEngine.bars` / `latest` | OHLCV history and the newest observation |
| Event intelligence | `MarketDataEngine.radar.instrument_events` | NM-04 direct mappings for the instrument |
| Sector intelligence | `MarketDataEngine.radar.search(EventQuery(sector=…))` | NM-04 sector context |
| Event trigger | `radar.event(event_id)` via `consume_event` | an operator or the radar can ask “re-evaluate what this event touches” |

There is **no exchange-listing scraper and no provider symbol-universe scan** in
this phase. That is a documented limitation, not a hidden one: discovery reads
whatever NM-02 actually holds, plus NM-04 associations.

## 3. Candidate record

Every candidate carries the fields the mandate requires, derived from NM-02 or
NM-05’s own evaluation:

| field | source |
| --- | --- |
| canonical instrument id | NM-02 `Instrument.instrument_id` (e.g. `XNSE:ALPHA`) |
| symbol, name, exchange, currency | NM-02 `Instrument` |
| asset category | NM-02 `AssetClass` |
| sector / industry | NM-02 `Instrument.metadata["sector"]` (or `["sectors"]`) |
| market status | NM-02 `trading_status` |
| data-source references | the providers on the bars/observations actually used |
| discovery timestamp | `discovery_candidates.discovered_at`, written once |
| discovery reason | `DiscoveryReason` (`catalogue`, `market_activity`, `liquidity`, `event_intelligence`, `sector`, `configured`) |
| data-quality status | `DataQuality` of the latest evaluation |

Unknown instruments are never created. `evaluate("XNSE:NOPE")` raises
`DiscoveryNotFoundError`; a watchlist add for an unknown instrument does the same.
An instrument whose trading status is unknown is scored, but the lifecycle treats
it conservatively (it is not treated as suspended, and it is never silently
accepted as tradeable).

## 4. Duplicate and incremental protection

* Candidate identity is the canonical instrument id — a second discovery updates
  the same row and increments `evaluations`, it does not insert a duplicate.
* Membership identity is `(watchlist_id, instrument_id)` with a unique index;
  adding an instrument already active in that list raises
  `DiscoveryValidationError`.
* A second cycle inside `min_interval_seconds` returns `status: "throttled"` with
  `retry_after_seconds`; `force=True` is the explicit override.
* Membership writes are also idempotent when the same instrument is offered to a
  list twice in one cycle (the second attempt is counted, never duplicated).

## 5. Source health and failure reporting

`discovery_sources` holds one row per source (`market_data`, `event_radar`).
After every cycle the engine records a status (`ok`/`error`), last success, last
error code and counters. A cycle whose catalogue read failed reports:

```json
{"status": "degraded", "scanned": 0, "failed_sources": 1,
 "errors": ["market data is not initialised"]}
```

and publishes `novamarket.asset.discovery.failed`. It never reports a successful
scan of zero instruments.

## 6. Limits and efficiency

* `max_instruments` caps the catalogue page; only that page is read per cycle.
* `top_n` caps the automatic membership and the ranked publication per cycle; evaluations still cover the whole scanned page.
* Bars are requested once with `min_history_bars * 4` as the limit, and the same
  slice feeds activity, liquidity and volatility — one read, three factors.
* Sector events are fetched once per distinct sector per instrument; the radar’s
  own limits apply.
* The scheduler never overlaps cycles, and the throttle prevents a hot loop.

## 7. Trading sessions

`discovery/sessions.py` is the only place a market schedule is stated: one row
per exchange MIC with its IANA zone and local open/close window (XNSE/XBOM,
XNYS/XNAS/XASE, XLON, XTKS). `session_state(exchange, at)` returns
`open`/`closed`/`unknown`; `sessions_for` maps the exchanges a cycle covers.

A **scheduled** cycle returns `{"status": "market_closed", "sessions": {...}}`
without scanning when every covered exchange is closed. A manual or forced cycle
always runs. `UNKNOWN` (an exchange with no declared session) never blocks. The
cycle response and `status()` carry the session map; `contract()["sessions"]`
publishes the states and the declared exchanges.

Limitations: weekdays only (no holiday calendar), and only the declared MICs.
