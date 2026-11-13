# NM-05 — Watchlists and Membership Lifecycle

## 1. Watchlist types

Six kinds exist; each has a distinct behavioural rule, so no two lists are
synonyms.

| kind | populated by | difference |
| --- | --- | --- |
| `discovery` | every cycle: the best `top_n` candidates | the general list |
| `activity` | candidates whose `market_activity` factor ≥ 0.5 | only instruments moving |
| `event_driven` | confirmed/corroborated high-importance events | only event-relevant instruments |
| `liquidity` | candidates whose `liquidity` factor ≥ 0.6 | only measurable traded value |
| `sector` | one list per instrument sector (`Sector: Energy`) | grouping, created on demand |
| `custom` | explicit API/engine calls | operator-owned, never auto-populated |

Four lists are created at first use: `discovery`, `activity`, `event_driven`,
`liquidity`. A `sector` list exists only once an instrument with that sector is
evaluated; `custom` lists exist only when created.

`WatchlistRecord.to_dict()` carries `watchlist_id`, `name`, `kind`,
`description`, `custom`, `created_at`, `updated_at`, `entries`.

## 2. Entry record

| field | meaning |
| --- | --- |
| `entry_id` | deterministic from `(watchlist_id, instrument_id)` |
| `watchlist_id`, `instrument_id` | membership identity |
| `added_at`, `updated_at` | lifecycle timestamps |
| `reason` | why it entered (discovery reason text) |
| `score`, `last_evaluated_at` | latest evaluation |
| `status`, `status_reason` | current state and why |
| `event_ids` | NM-04 evidence behind it |
| `below_threshold_count` | consecutive weak evaluations |

A unique index on `(watchlist_id, instrument_id)` makes duplicate **active**
membership impossible. Adding a removed instrument reinstates the same record
with an `active` history row; the previous `removed` row is preserved.

## 3. States and transitions

```
            ┌──────────── removed (terminal)
            │
 active ────┼── under_review ──┬── active
   │        │                  ├── downgraded
   │        ├── downgraded ────┼── active / under_review
   │        ├── stale ─────────┼── active / downgraded / under_review
   │        └── suspended ─────┴── active / under_review
```

`ENTRY_TRANSITIONS` is the single table and `ensure_entry_transition` raises on
an illegal move (`removed → active` is refused; reinstatement is an explicit add,
not a state transition). Every accepted move writes a history row.

## 4. Decision rules (maintenance)

`_next_status(entry, evaluation, instrument)` is evaluated in this order:

1. **Suspended** — the instrument’s NM-02 trading status is a suspended/delisted
   state.
2. **Stale** — `DataQuality.INSUFFICIENT` (“insufficient market data to confirm
   relevance”) or `DataQuality.STALE`.
3. **Removal counter** — `score < remove_threshold` (25) increments
   `below_threshold_count`; at `remove_after_cycles` (3) the entry becomes
   `removed`, otherwise `under_review`. The reason names the score and the count.
4. **Downgrade** — a drop of at least `downgrade_delta` (12) points from the
   stored score.
5. **Recovery** — a `downgraded`/`under_review`/`stale`/`suspended` entry whose
   new score reaches `add_threshold` (55) returns to `active`.
6. **Upgrade** — a rise of at least `upgrade_delta` (8) points returns `active`.
7. Otherwise the entry keeps its state and reason.

Consequences, all pinned by tests:

* A price decline **alone** never removes: the score must fall below 25 for three
  consecutive evaluations.
* A **stale or failed source** marks `stale` or skips the member; it never
  removes.
* A **delisted** instrument is suspended, not removed.
* Removal is **evidence-based**: the reason states the score, the threshold and
  the number of evaluations.

## 5. History

`discovery_entry_history` is append-only and records `from_status`, `to_status`,
`reason`, `score` and `at` for every add, reevaluation-driven change and manual
removal. `GET /market/discovery/history` (optionally filtered by watchlist or
instrument) reads it. Membership records are never deleted; a removed row keeps
its reason and timestamps.

## 6. Manual operations

* `create_watchlist(name, kind, description)` — deterministic id; creating the
  same `(kind, name)` twice returns the same list. Blank names and unknown kinds
  are refused with `DiscoveryValidationError` (HTTP 422).
* `add_instrument(watchlist_id, instrument_id, reason)` — instrument must exist in
  NM-02; enforces `max_entries_per_watchlist`; duplicate active → 422.
* `remove_instrument(watchlist_id, instrument_id, reason)` — a missing active
  member is `DiscoveryNotFoundError` (404); removal is terminal and recorded.
* `watchlist(watchlist_id)` — members ranked by score, with status and reason.

## 7. Events emitted

`watchlist.created`, `watchlist.instrument.added`,
`watchlist.instrument.updated`, `watchlist.instrument.downgraded`,
`watchlist.instrument.removed` (all prefixed `novamarket.`) and
`watchlist.evaluation.completed` after maintenance. Each is published **after**
the store write it describes, carries `watchlist_id` and `instrument_id`, and
payloads match `EVENT_PAYLOAD_FIELDS`.

## 8. Event retraction and expiry

`consume_event` treats a `retracted` or `expired` NM-04 event as a withdrawal of
evidence, not a new inclusion signal:

1. every instrument the event maps is re-evaluated against current data;
2. each membership whose evidence cites a retracted/expired event is re-checked
   by asking the radar for the current verification of every cited id — the
   withdrawn id is dropped (`_live_event_ids`);
3. the ordinary lifecycle decision applies, so the drop is evidence-based: a
   score fall of 12+ downgrades, below 25 counts toward removal, and a stale or
   failed source never removes;
4. the change is written to membership history ("event ... was retracted") and
   published as `watchlist.instrument.updated`/`downgraded`.

`POST /market/discovery/events/{event_id}` returns `status: "retracted"` with
`instruments`, `evaluated` and `affected`.
