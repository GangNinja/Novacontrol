# PI-02 Completion Report — Historical Market Dataset Engine

**Phase:** PI-02
**Branch:** `feature/novamarket`
**Verdict:** **PASS**
**Date:** 2026-11-13

This is the single consolidated PI-02 report. It covers assessment, implementation,
verification, defects and limitations. No PI-03 work was started.

## 1. Initial implementation assessment

PI-01 was re-read before implementation. Its completion report records **PASS for
the PI-01 scope** (contracts, lifecycle, evidence and existing-system
integration), with a previously recorded PARTIAL on the unrelated whole-repo
regression. The PI-01 phase gate was therefore satisfied. PI-01 already provided:

- immutable prediction contracts, lifecycle stages and decision evidence;
- `PredictionDataGateway` as a read-only market-data boundary;
- the seven prediction domains and the additive PI-01–PI-16 roadmap;
- storage-boundary declarations and the pattern for extending capabilities,
  permissions and typed events without adding routes.

The existing NM-02 market-data layer already owned providers, transport, retry,
rate limiting, circuit breakers, fair rotation, normalization, calendars,
quality assessment, caching, the append-only SQLite store and live acquisition.
NovaMarket already had instrument metadata, exchange/MIC identity, NSE/BSE
symbol mapping, daily/intraday bars, quality states and a 20-instrument
discovery cap. No separate historical collector, database or price authority was
needed.

The PI-02 gap was dataset semantics: range acquisition, versioned membership,
coverage/gap classification, conflict records, corporate actions and
leakage-safe training samples. Those were added as an additive layer.

## 2. Existing components reused

| Existing component | PI-02 use |
|---|---|
| `MarketDataEngine` | Provider selection, rate limits, retries, circuit breakers, normalization and quality |
| `MarketDataStore` | Authoritative OHLCV, instruments, unique-key deduplication, quality retention |
| `MarketDataEngine.ingest_history` (new, thin) | Bounded range call through the existing pipeline |
| Provider adapters | Yahoo (NSE/BSE via `.NS`/`.BO`), CoinGecko, Alpha Vantage |
| Exchange calendars | Sessions, holidays, closures and candle boundaries |
| `DataQuality` | Per-observation validation and conflict ranking |
| Existing SQLite file | Dataset tables, builds, versions, members, gaps, actions, training |
| Existing permissions/events | Three dataset capabilities, three policies and five events |
| `NovaMarketService` | Lazy composition, status and contracts surfaces |
| `architecture.py` validator | Seven PI-02 storage boundaries and owned data |

No duplicate collector, provider registry, transport, quote store or database was
created. Dataset members reference authoritative `bar_id`s; they do not copy
prices.

## 3. Features implemented

`novamarket/datasets/` now contains:

- **Models:** `DatasetBuildRequest`, `DatasetVersion`, `DatasetSegment`,
  `DatasetMember`, `SelectionRecord`, `CoverageSummary`, `ValidationSummary`,
  `MissingSummary`, `GapRecord`/`GapKind`, `DatasetQuality`, `CorporateAction`,
  `MarketAnnotation`, `TrainingSample`, `SplitSummary`, `TrainingDataset`.
- **Acquisition:** sequential, bounded calls through
  `MarketDataEngine.ingest_history`, provider fallback in requested priority
  order, per-instrument failure reports and idempotent re-ingestion. Full-range
  builds are the default; `incremental=True` starts at the latest stored bar
  (overlapping it for deduplication) so routine updates do not re-download the
  full history.
- **Validation:** deterministic provider selection, OHLCV and candle-boundary
  checks, duplicate/provider-conflict counting, coverage measurement and gap
  classification.
- **Storage:** dataset-owned tables in the existing SQLite file, immutable
  versions/members/selections/gaps/training rows, indexes, pagination and
  batched bar reads.
- **Versioning:** content digest over normalized configuration and membership,
  identical-content reuse, explicit-label immutability and retained acquisition
  reports.
- **Training:** chronological train/validation/test construction, purged splits,
  decision windows, strictly later targets and all nine horizons (5m, 15m, 20m,
  1h, 1d, 3d, 5d, 30d, 90d). Daily bars refuse intraday horizons.
- **Corporate actions:** sourced, verified actions with explicit price factors;
  raw membership with adjusted retrieval views; unverified actions change no
  price.
- **Integration:** `NovaMarketService.dataset_service()` plus dataset status,
  build, read, batch, coverage, quality, missing, training and action methods;
  `/market/status` and `/market/contracts` include the dataset surface.
- **Governance:** `market.dataset.read/build/manage` capabilities and
  permissions; five typed dataset events. Route count remains **223**.

## 4. Provider and historical coverage verified

### Fixture verification

The focused suite uses deterministic fake-provider rows for acquisition,
normalization, quality, gaps, conflicts, versioning, actions and training. These
are explicitly synthetic and are not presented as provider evidence.

### Real provider verification

A one-off bounded probe (not a committed test) used the real Yahoo adapter
through `MarketDataEngine.ingest_history` for `XNSE:RELIANCE`
(`RELIANCE.NS`) on 2026-11-13. Each resolution was requested once, sequentially,
with no forced outage and no paid subscription.

| Resolution | Requested | Received | Inserted | Rejected | Outside range | Result |
|---|---:|---:|---:|---:|---:|---|
| 1m | 2 days | 723 | 723 | 0 | 0 | ok |
| 5m | 5 days | 295 | 295 | 0 | 0 | ok |
| 15m | 5 days | 100 | 100 | 0 | 0 | ok |
| 30m | 5 days | 52 | 52 | 0 | 0 | ok |
| 1h | 10 days | 49 | 49 | 0 | 0 | ok |
| 1d | 30 days | 19 | 19 | 0 | 0 | ok |
| 1wk | 120 days | 19 | 16 | 2 | 1 | ok |
| 1mo | 400 days | 14 | 13 | 1 | 0 | ok |

All eight resolutions returned real bars with `provider="yahoo"`, zero errors,
UTC timestamps and INR currency. The weekly/monthly rows were not complete
provided candles: the default `allow_partial=False` and the requested end
boundary excluded them. Accepted rows were persisted; nothing was back-filled.
This verifies Yahoo's current free delayed coverage for one NSE instrument and
one short range, not every instrument or every historical depth.

An end-to-end live dataset build then ran through `HistoricalDatasetService`
with the same temporary market store: 29 daily bars over 30 expected sessions,
`quality="valid_with_warnings"`, `complete=false`, coverage `0.966667`, and one
`missing_provider_observation` for the 13 November session whose daily bar had
not been published by the provider at retrieval time. The missing observation
was reported, not forward-filled. Training construction on
that real version produced 22 samples across train/validation/test
(15/3/4), purged 1 training target and 1 validation target at split boundaries,
and passed all chronology/purge checks. The builder now emits 11 checks because a
later audit added the direct training-target-versus-test purge check; the live
probe predated that addition.

Provider limits remain as declared: Yahoo is delayed/unofficial and
provider-limited; CoinGecko is crypto-only; Alpha Vantage needs a key and its
free daily request tier. No access control was bypassed.

## 5. Storage and retrieval verification

Focused tests verify:

- dataset tables are created in the existing SQLite file and survive reopen;
- repeated identical builds return the existing version with `duplicate=true`,
  while changed content creates a distinguishable new version;
- an explicit version label with different content is refused;
- version, member and training rows are immutable;
- retrieval by instrument, exchange, date range, resolution, version, quality,
  provider and pagination works;
- multi-instrument batch retrieval and batch bar lookup work;
- coverage, quality, missing-data and version queries return measured summaries;
- rejected/invalid observations remain in the authoritative store while being
  excluded from trusted membership.

## 6. Dataset quality and leakage verification

- Quality states are dataset-specific and distinct from NM-02 `DataQuality`.
- Invalid and partial observations are retained, counted and excluded.
- Holidays, weekends and declared closures are coverage closures, not gaps.
- Missing provider observations, provider outages, halts, suspensions and
  no-trade intervals are separate classifications; halts/suspensions require an
  annotation with a source.
- No forward-fill or interpolation exists.
- Conflict resolution is deterministic and records alternatives.
- Training construction is chronological, grouped by decision time and purged.
  Tests assert every feature timestamp is `<= decision_at`, every target is
  strictly later, no test sample enters training, and target fabrication is
  refused when the required bar is absent.
- `verify_stored_training` rechecks chronology and purge rules on read.

## 7. Tests executed and actual results

| Check | Result |
|---|---|
| `pytest tests/test_novamarket_datasets.py -q` | **23 passed** (including incremental-vs-full refresh and all-horizon assertions) |
| Affected regression: datasets, prediction, NovaMarket core, market data, API, forecasting | **302 passed, 2 skipped, 319 subtests passed** |
| `mypy` on `datasets/`, dataset-related marketdata files and `service.py` | **clean** |
| `validate_architecture()` | **`()`** (no violations) |
| Route count | **223**, unchanged |
| Real Yahoo resolution probe | **8/8 resolutions returned data; 0 errors** |
| Real end-to-end dataset build | **29 bars, valid_with_warnings, complete=false, 1 reported gap** |
| Real training build | **22 samples (15/3/4), 2 boundary purges; 10 checks at probe time, 11 in the current builder** |

The two skips are the pre-existing live quote tests that require
`NOVAMARKET_LIVE_TESTS=1`; they were not run. The separate real history probe
above was run instead and is reported as such. No provider outage was forced and
no rate limit was exceeded.

The focused suite was rerun after the final audit edits. The affected
regression was rerun after those edits as well; its count increased from the
earlier 298 because the PI-02 regression assertions were added to the focused
file and the same files were exercised together. The full repository suite was
not run, as requested.

## 8. Defects fixed

1. **Gap lookup used the wrong version** in the invalid/partial test; the
   assertion now reads the newly built version rather than `v1`.
2. **Holiday/gap fixture had no hole** (it fetched all five sessions); the test
   now appends four sessions and expects one missing observation.
3. **Builds retained every selected candle** while validating later
   instruments; membership is now collected as lightweight references and the
   bar sequence is released per instrument.
4. **Provider priority was ignored when more than one provider was requested**;
   acquisition now tries the requested providers in order and only the engine
   performs the actual work.
5. **Version provider list was computed from released bar sequences**, which
   made it empty; it is now derived from the built segments.
6. **Union-typed resolution/action/split fields produced mypy errors**; explicit
   casts/normalization now keep the public input flexible and the internals
   typed.
7. **Pinned governance counts** in the existing tests were updated for three
   capabilities, three permissions and five events.
8. **Dataset quality assertion was too strict** (`valid` is correct when there
   are no warnings); it now accepts both honest states.
9. **The fetch toggle dropped `incremental` when rebuilding the request**, so a
   full refresh silently behaved incrementally; the request is now reconstructed
   completely. Fetch mode is excluded from content identity so an identical
   membership with a different acquisition path reuses the same version.
10. **Dataset completeness used the quality threshold**, so a version with one
    absent expected session could report `complete=true`. `complete` is now
    literal coverage (trusted segments, verified calendar coverage, a usable
    quality state, no selected partial candle, a positive expected count, zero
    missing observations and measured coverage at least equal to expectations).
    The same dataset can still be `valid_with_warnings` and usable for training
    while `complete=false`.

### Audit follow-up (verification pass)

11. **Out-of-order observations were counted after sorting**, hiding the defect.
    `select_bars` now measures order on the original input before grouping; the
    count remains in the version validation summary and forces a warning.
12. **Intraday and current-session expected slots were not clipped to the
    request boundary**, creating false gaps at the start/end of a partial
    session. Slots are now clipped to the requested instants, so an unfinished
    session is not called missing.
13. **Weekly/monthly boundary buckets could be expected as complete candles**
    even when the request covered only part of the bucket. Partial week/month
    buckets at either boundary are excluded from expected coverage and gaps.
14. **Missing volume and missing observations did not always affect quality.**
    A selected bar with no published volume, and any measured missing
    observation, now produce `valid_with_warnings`; no zero or estimated volume
    is substituted.
15. **`provider_adjusted` was silently treated as action-adjusted.** It is now
    refused for retrieval because no installed provider supplies a verified
    provider-adjusted series; callers must choose raw, split-adjusted or
    total-return-adjusted from recorded actions.
16. **A dataset segment omitted its currency.** Segments now retain exchange
    currency (and timezone) for normalization/provenance and version identity,
    and a bar whose currency differs from its instrument is refused rather than
    silently relabelled.
17. **Immutability was incomplete.** Selections, gaps, segment metadata,
    corporate actions, annotations, training versions, training splits and
    training samples now have append-only update/delete triggers, so a
    correction creates a new version rather than rewriting evidence.
18. **Dataset pagination and retrieval were not fully deterministic.** Member,
    gap, selection and training reads now include stable tie-breakers; the
    latest version ordering handles labels such as `v2` and `v10`; and dataset
    reads support a provider filter in addition to instrument, exchange, date,
    resolution, version and quality. Provider, exchange and resolution indexes
    were added for those filters.
19. **The duplicate-build response was not shape-compatible** with a new build;
    it now includes the same PI-01 `historical_datasets` references.
20. **Adjusted retrieval hid unapplied actions and the requested basis.** Reads
    now report `price_basis`, `adjustment_limitations` and
    `unapplied_action_ids`, and every adjusted row reports its basis.
21. **A training rebuild with the same content returned a new `created_at`**
    while reusing the training id. Duplicate training builds now return the
    persisted immutable payload, and the store refuses sample/split counts that
    disagree with the declared totals.
22. **Training purge skipped the test split when validation was empty.** An
    empty validation split (or a ratio/grouping that produced one) could let a
    training target reach into test. Training is now purged against test when
    validation is empty, and both the builder and stored-training verifier check
    train-target-versus-test overlap directly.
23. **The build journal was persisted but not reachable through the service.**
    `HistoricalDatasetService.builds()` and
    `NovaMarketService.dataset_builds()` now expose failed, running and
    completed runs without creating a second audit store.
24. **Completeness could be claimed outside verified calendar coverage or with
    an empty expected range.** It now requires verified calendar coverage and a
    positive measured expectation, and a `valid` dataset with incomplete
    coverage is downgraded rather than presented as complete.
25. **`allow_partial=True` was not honored by dataset membership.** Acquisition
    could store a partial candle, but selection always discarded it. Selection
    now honors the explicit request, counts `bars_selected_partial`, excludes
    partial candles from `complete` and never admits them to training.
26. **A segment did not reject a currency mismatch between its bars and the
    instrument.** A mismatched observation now refuses the build instead of
    being labelled with the instrument's currency.
27. **A build that failed while resolving an unregistered instrument left no
    journal entry.** Build resolution now runs after the journal is opened, so
    the failure is recorded and emitted as `dataset.build.failed`.
28. **Provider fallback failures were discarded when a later provider
    succeeded.** The successful acquisition report now retains the earlier
    failure text, while the service does not classify that success as a provider
    outage.

## 9. Integration evidence

- `NovaMarketService` constructs and closes the dataset service with the
  application lifecycle.
- `dataset_status()` and the dataset contract are present in
  `/market/status` and `/market/contracts`.
- Dataset capabilities report honest availability; they are not marked
  `available` merely because the class exists.
- The dataset build test drives the real `MarketDataEngine` and
  `MarketDataStore` with a fake provider, and the real-service test initializes
  `NovaMarketService` and consumes a dataset.
- Existing market-data, prediction, forecasting and API suites pass unchanged
  apart from the governance-count updates.
- Working tree remains on `feature/novamarket`; nothing was staged, committed,
  pushed, tagged, merged or published.

## 10. Remaining limitations

- Yahoo is free, delayed and unofficial; its endpoint can change and intraday
  depth is limited. Only one NSE instrument and short ranges were live-verified.
- No provider in this build supplies a trusted adjusted-close field. Adjusted
  views depend on verified corporate-action records; if those are absent, the
  dataset says so and changes no price.
- Adjusted retrieval is a derived view over the verified actions recorded at
  read time; the dataset version's raw membership and metadata remain immutable.
- Adjusted-price behavior is covered by deterministic fixture tests; no real
  corporate-action feed was queried, so live adjustment evidence is unverified.
- A full live intraday dataset build was not run; the live probe verified raw
  intraday acquisition for all resolutions, while the end-to-end version and
  training build used daily bars.
- Weekly/monthly dataset versions were not trained because the PI-01 horizon
  compatibility rules accept daily-or-finer input for every training horizon;
  weekly/monthly acquisition and gap classification were still exercised.
- Halts, suspensions, no-trade intervals and some closures require explicit
  annotations; they are not inferred from missing candles.
- Volume values remain in the provider's units and are null when the provider
  publishes none. No verified cross-provider volume-unit conversion exists in
  this build, so volume is preserved rather than silently rescaled.
- NSE/BSE corporate-action coverage is not assumed complete. Unverified actions
  are recorded as limitations.
- Dataset builds are sequential and bounded for free-provider safety; this is
  intentionally not a distributed or parallel ingest system.
- PI-02 adds no HTTP route or UI. Consumers use the service and status/contracts
  boundary.
- The two live quote tests remain skipped without `NOVAMARKET_LIVE_TESTS=1`; the
  live history probe is evidence for historical acquisition, not for those
  quote paths.
- No full-repository suite was run; verification is the focused PI-02 suite,
  the explicit affected regression listed above, mypy and the architecture
  validator. Tests outside those areas were not re-run.

## 11. Final acceptance verdict

**PASS (with explicitly reported limitations).**

All PI-02 acceptance requirements are implemented or explicitly limited:
historical acquisition goes through the approved provider engine; all eight
resolutions are represented and were live-verified for one NSE instrument;
normalization, quality, gaps, deduplication and conflict resolution are
implemented; datasets persist in the existing SQLite file with immutable,
reproducible versions; retrieval is indexed, paginated and batch-capable;
training datasets are chronological and leakage-guarded; corporate actions are
handled only from verified, sourced factors; existing authorities and route
counts are preserved; focused and affected tests pass; documentation is updated;
and no known PI-02 blocking defect remains.

PI-03 must not begin without a separate verified phase gate.
