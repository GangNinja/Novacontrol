# NM-05 — Autonomous Asset Discovery & Watchlists: Architecture

**Status:** implemented and verified on fixtures (2026-11-02). NM-05 discovers,
scores and monitors instruments; it places no order, allocates no capital and
produces no forecast or recommendation.

## 1. Position in NovaMarket

NM-05 is a **consumer** of the two phases before it and a **producer** for the
two after it. It introduces no new provider, registry, event bus, scheduler or
database engine.

| Phase | NM-05 uses | Direction |
| --- | --- | --- |
| NM-01 | contracts/errors, event vocabulary, permission model, route surface, state store conventions | reuses |
| NM-02 | `MarketDataEngine` (`get_instrument`, `list_instruments`, `latest`, `bars`), canonical `Instrument` model, SQLite storage | reads |
| NM-03 | nothing | untouched |
| NM-04 | `MarketEventRadar` (`instrument_events`, `search(EventQuery(sector=…))`, `event`) | reads |
| NM-06/07 | `AssetDiscoveryEngine.contract()`, candidates, watchlists, evaluation history | provides |

## 2. Package layout

```
src/novacontrol/novamarket/discovery/
├── models.py    value types, lifecycle table, ids, digest, METHODOLOGY_VERSION
├── scoring.py   the seven-factor model, quality caps, tiers, describe_model()
├── store.py     SQLite: candidates, append-only evaluations, watchlists,
│                entries + per-entry history, cycles, source health
├── engine.py    AssetDiscoveryEngine: cycles, evaluation, membership
│                maintenance, event consumption, scheduler, contract
└── __init__.py  the public surface
```

Composition happens in `NovaMarketService`: it is constructed with the existing
market-data engine and the NM-04 radar, owns one `DiscoveryStore`, and exposes
the engine as `service.discovery`. Nothing in the package imports NM-06 or later.

## 3. The discovery cycle

```
             ┌──────────────────────────────────────────────┐
             │ 1. universe scan (NM-02 list_instruments)    │
             │    asset class + exchange + instrument cap   │
             └──────────────────┬───────────────────────────┘
                                │ canonical Instrument records
             ┌──────────────────▼───────────────────────────┐
             │ 2. evidence collection per instrument        │
             │    bars (NM-02)  quote (NM-02)               │
             │    direct events + sector events (NM-04)     │
             └──────────────────┬───────────────────────────┘
             ┌──────────────────▼───────────────────────────┐
             │ 3. normalize + score (scoring.py)            │
             │    factors, missing fields, quality, digest  │
             └──────────────────┬───────────────────────────┘
             ┌──────────────────▼───────────────────────────┐
             │ 4. persist evaluation (append-only)          │
             │    upsert candidate, record discovery reason │
             └──────────────────┬───────────────────────────┘
             ┌──────────────────▼───────────────────────────┐
             │ 5. membership (policy-gated)                 │
             │    discovery / activity / liquidity / event  │
             │    + sector watchlists by instrument sector  │
             └──────────────────┬───────────────────────────┘
             ┌──────────────────▼───────────────────────────┐
             │ 6. publish after commit + persist cycle row  │
             └──────────────────────────────────────────────┘
```

Each stage is a method on the engine, so a cycle is testable stage by stage and
an event-triggered evaluation reuses **stage 2 onwards** rather than a second
implementation.

### Failure isolation

* Every instrument is evaluated inside its own `try`; one bad record is counted
  in `skipped` and named in `last_error`, never allowed to abort the cycle.
* A missing market-data source is a **degraded** report (`failed_sources: 1`)
  with the source marked `error` in source health — never an empty “ok”.
* A data-source failure during maintenance **skips** the member. It can never
  remove, downgrade or stale a membership.

## 4. Persistence

One SQLite database per `DiscoveryStore`, opened through the same
`persistence`/path conventions NM-02 and the NM-04 radar use.

| table | shape | guarantee |
| --- | --- | --- |
| `discovery_candidates` | one row per instrument | `discovered_at` preserved; `last_score`, `reason`, `quality`, `evaluations` |
| `discovery_evaluations` | append-only | a reevaluation inserts, never updates; a trigger refuses `UPDATE` |
| `discovery_watchlists` | one row per list | deterministic id from `(kind, name)` |
| `discovery_entries` | unique `(watchlist_id, instrument_id)` | duplicate membership impossible in the schema |
| `discovery_entry_history` | append-only | every add / status change / removal with reason and timestamp |
| `discovery_cycles` | one row per cycle | the report is what was persisted, not a projection |
| `discovery_sources` | one row per source | status, last success, last error code, counters |

`SCHEMA_VERSION = 1`; the schema is created by the store on first open, so no
separate migration runner is introduced.

## 5. Scheduling

The scheduler is an **explicit `asyncio.Task`** owned by the engine:

* `start_scheduler(interval_seconds)` / `stop_scheduler()`; default **off**.
* Interval is floored at `DiscoveryPolicy.min_interval_seconds` (30 s).
* A cycle that is already running is not started twice (`_running` guard).
* Two manual cycles closer than the minimum interval are **throttled** unless
  `force=True`.
* `NovaMarketService.stop()` closes the discovery store, which stops the
  scheduler first.

**Trading sessions.** `discovery/sessions.py` declares one session per exchange
MIC (IANA timezone + local open/close). A **scheduled** cycle is skipped with
`status: "market_closed"` only when every covered exchange is `CLOSED`; manual
and forced cycles always run. An exchange with no declared session is `UNKNOWN`
and never blocks a cycle. Weekdays only — no holiday calendar is claimed.

No daemon thread, thread pool or process is created anywhere in NM-05.

## 6. Boundaries

| Boundary | Enforced by |
| --- | --- |
| no order / no capital | no execution code exists; `MarketOperation` grants only read/score/manage |
| no recommendation | `CandidateEvaluation.to_dict()` carries `is_trading_recommendation: false`, `is_price_prediction: false`; scoring has no direction |
| no unverified event inclusion | `consume_event` refuses anything but `officially_confirmed` / `multi_source_corroborated` at `high`/`critical` importance |
| no price-decline removal | removal needs `score < remove_threshold` for `remove_after_cycles` consecutive evaluations |
| no source-failure removal | maintenance catches and skips |
| no invented instruments | every candidate comes from NM-02’s catalogue; unknown ids raise `DiscoveryNotFoundError` |
