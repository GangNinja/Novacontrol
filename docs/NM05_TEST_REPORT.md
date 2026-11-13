# NM-05 — Test Report (implementation + audit)

**Scope:** the NM-05 suite (`tests/test_novamarket_discovery.py`) plus the
NM-01…NM-04 regression files the wiring touches. Every run was executed on the
developer machine (Windows, `.venv`), from `novamarket/`, with
`PYTHONPATH=src`.

**No live discovery, no live market-data retrieval and no paid provider call was
made.** All instruments, quotes, bars and events are fixtures stored through
NM-02’s real store or ingested through NM-04’s real radar pipeline. The only seam
replaced in the NM-05 suite is the market-data facade (`_Seam`: four reads over
the real `MarketDataStore`); scoring, membership, lifecycle, persistence,
sessions and publication are the implementation.

## 1. Commands and results (final, after the audit fixes)

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_novamarket_discovery.py -q` | **50 passed** |
| `python -m pytest tests/test_novamarket.py tests/test_novamarket_api.py -q` | **120 passed, 110 subtests passed** |
| `python -m pytest tests/test_novamarket_radar.py -q` (NM-04) | **70 passed, 7 subtests passed** |
| `python -m pytest tests/test_novamarket_marketdata.py -q` (NM-02) | **41 passed, 2 skipped** |
| `python -m pytest tests/test_novamarket_wallet.py -q` (NM-03) | **76 passed, 22 subtests passed** |
| all six files together | **357 passed, 2 skipped, 139 subtests passed** |
| `python -m mypy src/novacontrol/novamarket src/novacontrol/api` | **Success: no issues found in 50 source files** |
| `python scripts/generate_api_reference.py --check` | **in sync** (after regeneration) |

The two skipped market-data tests are the pre-existing optional-provider skips.

## 2. Suite layout

| Class | Coverage |
| --- | --- |
| `ScoringModelTests` | weights sum to 1.0 and are published, every factor explained, `contribution == value × weight × 100`, activity rising with the move, missing volume unknown (not zero) with the `PARTIAL` cap, stale ≤ 35, no data ≤ 15, digest reproducibility, confirmed > unverified, retracted/out-of-window events contribute zero, no recommendation/prediction flags |
| `DiscoveryCycleTests` | discover → evaluate → watchlist with publish-after-commit, ranked discovery list, unsupported asset classes not scanned, exchange boundary, no duplicate membership, throttling and `force`, missing market-data source degraded, unknown instrument typed 404, evaluation persisted with factors/version |
| `WatchlistTests` | idempotent creation, blank name refused, one active membership, removal + history + reinstatement, typed errors, member limit, ranking, `removed → active` refused |
| `MaintenanceDecisionTests` | decline downgrades, recovery reactivates, weak score needs repeated evidence, stale marks stale (never removes), delisted suspends, failing source skips without changing membership |
| `EventIntegrationTests` | official event drives event-driven inclusion with its `event_id`, single-source event ignored with the reason, sector context raises sector relevance only, missing radar is typed |
| `PersistenceTests` | reopen keeps candidates/watchlists/history/schema, reevaluation appends, evaluation table refuses `UPDATE` |
| `DiscoveryAuditTests` | **the defects fixed in this audit** (see §3) |

## 3. Audit findings and fixes

| # | Defect found | Fix | Pin |
| --- | --- | --- | --- |
| 1 | A failing instrument catalogue was swallowed; the cycle reported `status: "ok"`, `scanned: 0`, and then overwrote the source-health row with `ok` | `_universe_items` returns the failures; the cycle counts `failed_sources`, sets `degraded`, records the real error and publishes `asset.discovery.failed` with the actual reason/source | `test_a_catalogue_failure_is_a_degraded_cycle` |
| 2 | Reevaluation with unchanged inputs was silently dropped: the evaluation id was the input digest, so a later identical evaluation hit `INSERT OR IGNORE` | the id now hashes digest + evaluation moment + cycle id; digest stays the input digest | `test_reevaluation_with_unchanged_inputs_is_a_new_record` |
| 3 | `_maintain` evaluated an instrument once per **membership**, so one instrument in four lists produced four reads/evaluations per pass | entries are grouped by instrument; one evaluation is applied to every membership | `test_maintenance_evaluates_each_instrument_once` |
| 4 | `_touch_existing` scanned every active entry for each evaluated instrument (O(instruments × entries)) | `DiscoveryStore.entries_for_instrument` + one `_reconcile_instrument` pass | covered by 3 and the cycle tests |
| 5 | A retracted/expired NM-04 event was reported `ignored` and its evidence stayed on memberships | `consume_event` routes retractions to `_retract_event`: re-evaluate the mapped instruments, drop retracted/expired ids from the evidence, apply the lifecycle decision, publish | `test_a_retracted_event_clears_membership_evidence` |
| 6 | Consuming an event never reconciled existing memberships’ scores/status | `_reconcile_instrument` runs for every consumed event | covered by 5 |
| 7 | `top_n` was documented as a ranking/membership cap but only sliced the `discovered` publishes; `candidate.ranked` was published once for #1 | membership and reconciliation are limited to the top `top_n`, `candidate.evaluated` is published per evaluated candidate and `candidate.ranked` per ranked candidate | `test_top_n_caps_automatic_membership` |
| 8 | Cycle history was persisted but unreachable through the API | `status()` now returns `recent_cycles` (latest 10) and `sessions` | `test_status_exposes_recent_cycles_and_sessions` |
| 9 | §10’s market-calendar requirement was not implemented at all | `discovery/sessions.py` declares sessions per MIC; scheduled cycles skip with `market_closed` only when every covered exchange is closed; `UNKNOWN` never blocks; status/contract/report expose it | `test_a_scheduled_cycle_waits_while_every_market_is_closed`, `test_a_declared_session_is_not_guessed` |
| 10 | `asset.discovery.failed` always claimed “market data is not initialised”, even for a catalogue failure | the event carries the real reason and source id | covered by 1 |
| 11 | `consume_event` returned `instruments` as a **count**, not the affected ids | returns the ids, plus `added` and `evaluated` | `test_an_official_company_event_drives_an_event_watchlist` |

## 4. Regressions

NM-01/02/03/04 results match their own phase reports (NM-04 baseline: radar
70/7, group 181/104, marketdata+wallet 117 passed/2 skipped/22 subtests); the
group figure grew with the NM-05 pins and API tests and the discovery file. No
earlier functionality regressed, and the audit itself required no changes to
NM-01…NM-04 code.

## 5. Remaining issues and unverified requirements

1. **No live discovery evidence.** The build has no exchange-listing or provider
   symbol-universe feed, so discovery reads the NM-02 catalogue. No live
   instrument was discovered, and none is claimed.
2. **Event forwarding is explicit, not subscribed.** `AssetDiscoveryEngine.
   forward_market_event` takes a published `novamarket.market_event.*` payload
   and consumes it, and `POST /market/discovery/events/{event_id}` triggers the
   same path on demand. The service does **not** yet subscribe the radar bus to
   discovery, so an ingested event does not automatically start an evaluation.
   This is the one §9 behaviour that is implemented but not wired end-to-end.
3. **Sessions are weekday windows only.** No holiday calendar; seven MICs are
   declared; any other exchange is `UNKNOWN` and never blocks a cycle.
4. **Concurrency is guarded, not stress-tested.** The `_running` lock plus the
   throttle prevent overlap; no two-client stress run is claimed.
5. **Sector relevance depends on NM-04 mapping.** A sector story with no mapped
   instrument contributes nothing (NM-04 behaviour, not re-derived in NM-05).
6. **Scheduler behaviour** is verified through start/stop/status and the
   `market_closed` path, not by waiting a wall-clock interval.
