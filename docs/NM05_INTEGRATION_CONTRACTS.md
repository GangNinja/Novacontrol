# NM-05 — Integration Contracts

NM-05 reuses NM-01’s contracts, NM-02’s instruments/market data and NM-04’s
events. It adds no parallel registry, provider, event bus or scheduler.

## 1. Inputs consumed

### NM-02 — `MarketDataEngine`

| call | use |
| --- | --- |
| `list_instruments(asset_class=…, exchange=…, limit=…)` | the universe scan |
| `get_instrument(instrument_id)` | canonical record for one evaluation |
| `bars(instrument_id, limit=…, ascending=True)` | activity, liquidity, volatility |
| `latest(instrument_id).selected` | newest price and its freshness |

Only the stored catalogue is scanned. An instrument that NM-02 does not hold
cannot become a candidate.

### NM-04 — `MarketEventRadar`

| call | use |
| --- | --- |
| `instrument_events(instrument_id, limit=…)` | direct event signals |
| `search(EventQuery(sector=…, limit=…))` | sector context for `sector_relevance` |
| `event(event_id)` | event-triggered reevaluation via `consume_event` |

Verification and importance are read from NM-04 and **not re-derived**: a
`single_source` event stays single-source inside NM-05.

### NM-03

The wallet/ledger is neither read nor modified. No capital is allocated.

## 2. Outputs provided

### Engine surface

| method | returns |
| --- | --- |
| `run_cycle(force=False, trigger=…, limit=None)` | the persisted cycle report |
| `evaluate(instrument_id, reason=…)` | evaluation body + whether a candidate was created |
| `candidates(limit=…, offset=…, min_score=…)` | ranked candidate page |
| `candidate(instrument_id)` | one candidate with its latest evaluation |
| `evaluations(instrument_id)` | append-only evaluation history |
| `watchlists()` / `watchlist(watchlist_id)` | lists and ranked members |
| `create_watchlist` / `add_instrument` / `remove_instrument` | membership changes |
| `history(watchlist_id=…, instrument_id=…)` | membership history |
| `reevaluate(watchlist_id=None)` | maintenance report |
| `consume_event(event_id)` | event-driven evaluation result |
| `start_scheduler(interval)` / `stop_scheduler()` / `scheduler_status()` | scheduling |
| `status()` / `metrics()` / `contract()` | observability and the stable consumer contract |

`contract()` is the NM-06/NM-07 entry point. It returns the universe, the
watchlist catalogue, entry states, the scoring model (`describe_model()`), the
policy, the ten event names and the four safety guarantees. Consumers must read
scores as **discovery priority**, never as expected return.

### HTTP routes (13)

| method | path |
| --- | --- |
| GET | `/market/discovery/status` |
| GET | `/market/discovery/candidates` |
| GET | `/market/discovery/candidates/{instrument_id}` |
| POST | `/market/discovery/cycle` |
| POST | `/market/discovery/reevaluate` |
| POST | `/market/discovery/events/{event_id}` |
| POST | `/market/discovery/scheduler` |
| GET | `/market/discovery/history` |
| GET | `/market/watchlists` |
| POST | `/market/watchlists` |
| GET | `/market/watchlists/{watchlist_id}` |
| POST | `/market/watchlists/{watchlist_id}/members` |
| DELETE | `/market/watchlists/{watchlist_id}/members/{instrument_id}` |

Every route is registered in `ApiSurface` and `ROUTE_CONSUMERS`; unknown
candidates/watchlists are 404, invalid bodies 422. No route places an order.

### Permissions

| operation | risk | granted |
| --- | --- | --- |
| `market.discovery.run` | LOW | yes |
| `market.watchlist.read` | LOW | yes |
| `market.watchlist.manage` | LOW, reversible | yes |

No new HIGH/CRITICAL operation was introduced. `trade.paper` and `trade.live`
remain ungranted and unreachable.

## 3. Events published

All ten are in `EventType` and `EVENT_PAYLOAD_FIELDS`, and are published only
after the corresponding store write.

| event | key payload |
| --- | --- |
| `novamarket.asset.discovered` | `instrument_id`, `score`, `reason` |
| `novamarket.asset.candidate.evaluated` | `instrument_id`, `score`, `data_quality` |
| `novamarket.asset.candidate.ranked` | `instrument_id`, `score`, `tier`, `rank` |
| `novamarket.watchlist.created` | `watchlist_id`, `kind`, `name` |
| `novamarket.watchlist.instrument.added` | `watchlist_id`, `instrument_id`, `score`, `reason` |
| `novamarket.watchlist.instrument.updated` | `watchlist_id`, `instrument_id`, `status` |
| `novamarket.watchlist.instrument.downgraded` | `watchlist_id`, `instrument_id`, `from_status`, `to_status` |
| `novamarket.watchlist.instrument.removed` | `watchlist_id`, `instrument_id`, `reason` |
| `novamarket.watchlist.evaluation.completed` | `evaluated`, `upgraded`, `downgraded`, `removed` |
| `novamarket.asset.discovery.failed` | `reason`, `source_id` |

Payloads never use the envelope’s reserved keys (`source`, `correlation_id`,
`causation_id`).

## 4. Data-quality requirements

* A candidate must have a quote **or** at least two bars, else it is
  `INSUFFICIENT` and capped at 15.
* A quote older than `stale_after_hours` (72) is `STALE` and capped at 35.
* Any missing factor caps the score at 60 (`PARTIAL`).
* Unverified events contribute 0.15 of their importance; retracted/expired
  contribute 0.
* No score is served without its factors, digest, methodology version and
  timestamp.

## 5. What NM-06 may rely on

1. `candidates()` is stable and ranked, with an explanation per row.
2. `contract()["scoring"]` publishes weights and caps; any change bumps
   `METHODOLOGY_VERSION`.
3. `evaluations()` gives the full, immutable history for backtesting an
   evaluation policy.
4. A watchlist entry never implies a position, order or recommendation.
5. Nothing in NM-05 mutates NM-02 or NM-04 data.

## 6. Sessions, status and event forwarding

* `status()` adds `sessions` (declared state per covered exchange) and
  `recent_cycles` (the latest 10 persisted cycle reports).
* `contract()["sessions"]` publishes the three states, the declared exchanges
  and `unknown_never_blocks: true`.
* A cycle response carries `sessions` alongside the persisted report.
* `forward_market_event(name, payload)` accepts a published
  `novamarket.market_event.*` payload carrying `event_id` and runs
  `consume_event`; any other name returns `forwarded: false` with a reason.
  The service does not yet subscribe the radar bus automatically, so forwarding
  is explicit (endpoint or caller) — see the test report's remaining issues.
