# PI-04 Completion Report — Feature Engineering & Market Regime Detection

**Programme:** NovaMarket Prediction Intelligence (PI-01–PI-16)
**Phase:** PI-04
**Branch:** `feature/novamarket`
**Status:** **PASS** (implementation and integration verified; limitations below)
**Date:** 2026-11-14
**Feature set version:** `pi04-feature-set-1.0`
**Regime method:** `pi04-regime-1.0`
**Preprocessing version:** `pi04-preprocess-1.0`
**Selection version:** `pi04-selection-1.0`
**Feature contract:** `pi04.features.1.0`

---

## 1. Initial implementation assessment

Before implementation the mandatory dependencies were checked on this branch:
PI-01 (`docs/PI01_COMPLETION_REPORT.md`), PI-02 (`docs/PI02_COMPLETION_REPORT.md`)
and PI-03 (`docs/PI03_COMPLETION_REPORT.md`) all report PASS. The existing
prediction contracts, historical dataset engine, market-data gateway, technical
indicators, fundamentals, news radar, SQLite stores, configuration, permissions,
events, tests and architecture documents were inspected first.

The reusable foundation already present was substantial:

- `marketdata/` owns acquisition, providers, calendars, quality, append-only
  bars and the `MarketDataEngine`/`MarketDataStore` path.
- `prediction/` owns `FeatureVector`, `FeatureDefinition`, `HORIZON_SPECS`,
  fundamental snapshots and news evidence contracts.
- `datasets/` owns versioned dataset membership and training/leakage rules.
- `strategies/indicators.py` already owned the technical-indicator authority.
- `service.py`, `config.py`, `permissions.py`, `events.py` and
  `architecture.py` own the NovaMarket composition, settings, authorization,
  typed events and storage boundaries.

PI-04 therefore added a composition layer (`novamarket/features/`) rather than a
new collector, provider, database file, validation engine or indicator engine.
No PI-05 work was started.

## 2. Existing components reused

| Component | Reused as |
|---|---|
| `MarketDataEngine.bars` / `Instrument` / `OhlcvBar` | Only market-data read path and observation contract |
| `MarketDataStore` and the existing SQLite file | Source observations; feature tables share the same file |
| PI-02 `DatasetStore.member_rows` | Optional dataset-version membership filtering |
| PI-01 `FeatureVector`, `FeatureDefinition`, `FundamentalSnapshot`, `NewsEventEvidence` | Prediction vector bridge, definitions, fundamental/news provenance |
| `strategies/indicators.py` | The single indicator implementation; new Wilder/EMA/WMA/volume functions were added there |
| `marketdata.calendar` | Session close and exchange-local day/week/month boundaries |
| NM-04 radar (`instrument_events`) | Existing news/event evidence, not a second news collector |
| Existing `NovaMarketService` | Lazy runtime, status, contracts, permissions and events |
| Existing feature/architecture test conventions | Focused pytest fixtures and store immutability checks |

No duplicate technical-indicator, data-validation, market-data or
historical-storage engine was introduced.

## 3. Features implemented

The registry contains **122 named, versioned definitions**:

| Family | Examples |
|---|---|
| Price | close, absolute/percentage/log returns, open-to-close, gap, range, body, wicks, consecutive moves, rolling high/low, distances |
| Trend | SMA 5/10/20/50/200, EMA 5/12/26/50, WMA 10/20, MACD/signal/histogram, ADX, +DI/−DI/DX, MA distances and crossover/slope |
| Momentum | Wilder RSI(14), stochastic %K/%D, ROC(10), momentum(10), Williams %R, normalized MACD |
| Volatility | Wilder ATR(14), ATR %, ATR-normalized move, Bollinger bands/width/position, rolling standard deviation, annualized historical volatility, volatility change, range expansion |
| Volume | 20-bar volume SMA, relative volume, volume ROC, OBV and slope, VPT and slope, MFI(14), session VWAP and distance |
| Structure | confirmed swing high/low, HH/HL/LH/LL, continuation, breakdown, 20-bar breakout/breakdown, range-bound, support/resistance proximity and range position |
| Multi-timeframe | available count, alignment, trend agreement, momentum alignment, volatility ratio, short close inside long range |
| Context | benchmark relative return 1/20, sector relative return, benchmark volatility, breadth |
| Fundamentals | reported revenue, net profit, cash flow and dividend per share; revenue/profit/cash-flow growth, EPS, P/E, P/B, ROE, ROCE, debt/equity, operating and profit margin, dividend yield, filing age |
| News/events | 5/20-day deduplicated counts, maximum importance, maximum supplied relevance, validated sentiment mean, age, earnings/corporate-action/regulatory flags |

Profiles are not identical: 77 definitions are available to intraday, 119 to
swing and 120 to long-term. The profile map is data-driven in
`features/definitions.py`; adding a profile does not change the engine.

## 4. Indicator calculation verification

`tests/test_novamarket_features.py::test_indicators_match_independent_reference_values`
pins hand-derived values for SMA, EMA, WMA, Wilder RSI, Wilder ATR, MACD,
stochastic, directional movement, ROC, momentum, Williams %R, rolling standard
deviation, historical volatility, OBV, MFI, VWAP, Bollinger bands, true range and
VPT. Short-series tests pin `None` rather than invented warm-up values.

Definitions are explicit:
- EMA is seeded with the first SMA and `2/(n+1)` smoothing.
- RSI and ATR use Wilder smoothing; flat RSI is explicitly 50.
- Bollinger uses 20-bar population standard deviation × 2.
- Historical volatility is the sample standard deviation of 20 log returns × √252.
- VWAP is typical-price/volume over the latest completed intraday session only.
- The pre-existing `rsi` remains unchanged for NM-09 compatibility; PI-04 uses
  `rsi_wilder`.

**Result:** PASS for deterministic indicator fixtures. No live-market price
calculation was used as evidence.

## 5. Market-regime verification

`RegimeDetector` is a deterministic, versioned threshold model. Tests cover:

- strong upward trend and a transition to a later state;
- strong/weak downward trend;
- flat series → sideways/low-volatility;
- high-volatility secondary condition;
- uncertain/insufficient evidence with a reason;
- direct detection with a previous label;
- `confidence is None` and `confidence_calibrated is False` in every path;
- service-level transition derived from the persisted previous regime.

Primary and secondary conditions are separate. A regime is not treated as a
price prediction. Primary labels implemented: strong up/down, weak up/down,
sideways, high volatility, low volatility, potential reversal, breakout,
breakdown, uncertain.

## 6. Multi-timeframe verification

Tests verify:

- 1h and 1d alignment with only completed bars;
- completion-based inclusion (a daily bar observed before the cutoff but not
  closed by it is excluded);
- aggregation of completed daily bars into weekly bars;
- omission of the current partial weekly bucket;
- record of the derived timeframe/source in lineage;
- explicit absence of an unavailable timeframe rather than a fabricated row.

The engine uses an exact validated series first, then aggregates from a finer
validated series using exchange-local session/day boundaries and calendar
week/month buckets. Only buckets whose end is at or before `as_of` are kept.

## 7. Leakage and timestamp verification

Safeguards verified by tests and code inspection:

- Source observation time must be at or before `as_of`.
- A bar is accepted only when its exchange session close (or nominal interval
  end without a calendar) is at or before `as_of`.
- Future, partial, invalid and duplicate observations are excluded and counted
  in lineage.
- Out-of-order observations are sorted by source time.
- `FeatureSet.__post_init__` refuses `source_last_at > as_of`.
- A partial daily bar seen intraday cannot become a completed daily feature.
- Fundamentals are eligible only when `received_at <= as_of`; a later filing
  test proves it cannot be selected.
- News evidence requires both `published_at` and `received_at` at or before
  `as_of`, plus usable quality.
- Historical MTF values never use later bars; weekly/monthly buckets are closed
  before inclusion.
- Preprocessing fits only on supplied training rows and transform reuses fitted
  parameters; future/test rows cannot move centers or scales.
- No feature marks a breakout/breakdown from an unfinished candle; those use the
  latest completed close against the prior 20 completed bars.

## 8. Integration evidence

1. **Live application composition:** `test_actual_novamarket_service_generates_
   from_the_existing_market_store` starts a real `NovaMarketService`, registers
   an instrument, appends valid bars to the real `MarketDataStore`, calls
   `generate_features`, and verifies persistence, latest feature set, latest
   regime, feature status/contract and the `market.feature.generate` permission.
2. **Actual PI-02 dataset membership:** `test_feature_generation_honours_an_
   actual_pi02_dataset_membership` builds a real `DatasetStore` version through
   `NovaMarketService.build_historical_dataset`, then requests features with that
   `dataset_id`/`dataset_version`. The resulting feature row records the dataset
   version and reads the same stored bars.
3. **PI-01 vector bridge:** feature sets map through `to_prediction_vector()` to
   the existing `FeatureVector`; status counts, quality, digest, resolution and
   dataset versions travel in lineage.
4. **Store/persistence:** feature definitions, feature sets, regimes,
   preprocessing fits, selection reports and generation status use the existing
   NovaMarket SQLite file with immutable/versioned rows and duplicate detection.
5. **NM-04 radar shape:** a test feeds the real radar `search()` event shape
   (`instruments`, `retrieved_at`, `classification.score`, `importance` band)
   and verifies it is accepted without the PI-01-only `received_at` spelling.
   Publication, occurrence, availability, importance and radar identity remain
   in provenance.
6. **Service status/contracts:** `status.features` and `contracts.features`
   report implementation, versions, authority=false and no order emission.
7. **No route/UI:** PI-04 adds no HTTP route, preserving the existing route
   contract and the NM-08 risk / NM-10 execution authorities.

## 9. Tests and actual results

Focused PI-04 suite:

```
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_features.py -q
35 passed in 1.76s
```

Coverage includes all 20 requested areas plus insufficient history, flat price,
zero volume, gaps/market closure, high volatility, duplicate observations,
out-of-order timestamps, invalid source data, legitimate extreme moves,
fundamental/news timestamps, preprocessing leakage, versioning, incremental
caching and actual PI-01/PI-02/PI-03-path integration.

Affected-area regression:

```
PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest \
  tests/test_novamarket_datasets.py tests/test_novamarket_prediction.py \
  tests/test_novamarket.py tests/test_novamarket_marketdata.py \
  tests/test_novamarket_api.py tests/test_novamarket_forecasting.py \
  tests/test_novamarket_gateway.py tests/test_novamarket_strategies.py \
  tests/test_novamarket_features.py -q
395 passed, 2 skipped, 319 subtests passed in 107.01s
```

Static checks:

```
python -m mypy src/novacontrol/novamarket
exit 0 (144 source files)

validate_architecture() -> ()
validate_prediction_architecture() -> ()
git diff --check -> exit 0
```

Local fixture performance (500 daily bars, no cache, CPython, this machine):
**13.10 ms average per feature row**. This is a deterministic local measurement,
not a production latency or provider-latency claim.

The full repository suite was not rerun; the affected NovaMarket suite and
focused phase suite were used as required by the phase's efficient-testing rule.
Two tests skipped under their own conditions in the affected files.

## 10. Defects fixed

- Added the missing PI-04 indicator implementations/exports in the existing
  `strategies/indicators.py` instead of creating a second indicator engine.
- Made pivot detection accept real flat tops/bottoms and collapse equal adjacent
  pivots, so HH/HL features are not silently unavailable on valid plateaus.
- Scoped VWAP to the latest completed intraday session instead of the whole
  supplied intraday history.
- Added completed finer-bar aggregation so weekly/monthly features are real and
  never include the current partial bucket.
- Passed the configured regime thresholds into the engine (the service had
  constructed a second default detector that ignored settings).
- Derived service regime transitions from the persisted previous regime.
- Increased dataset-membership read depth before filtering so an older allowed
  bar is not lost when the newest bars are outside the requested dataset.
- Included benchmark/sector/breadth bar identities in the cache digest so
  changed context cannot reuse a stale feature row.
- Preserved genuine zero fundamental and news values (removed `or` chains that
  treated `0` as missing) and normalised string boolean sentiment flags.
- Accepted the real NM-04 radar event shape (retrieved/occurred timestamps and
  banded/classification importance) instead of silently dropping every radar
  event for lacking PI-01's `received_at` key.
- Added configuration-driven missing policy to service preprocessing.
- Stopped the primary feature row from falling back to another resolution and
  rejected bars whose interval does not match the requested series.
- Enforced the request's `max_bars` bound after quality filtering, validated
  supported resolutions, and made dataset membership read the *latest* allowed
  rows within the cutoff instead of the oldest page.
- Excluded bars retrieved after the cutoff (counted as partial, not future) and
  bars whose observed timestamp is future; added timestamp and interval
  validation before a bar contributes to an indicator.
- Corrected weekly/monthly bar-close boundaries and session-aligned intraday
  aggregation, so a completed bucket is not mislabeled or made available one
  period late.
- Corrected duplicate resolution to prefer better quality on an equal retrieval
  timestamp rather than replacing a valid bar with a worse one.
- Added the news relevance feature, raw reported revenue/net profit/cash flow
  and dividend-per-share features, and reported-value fallbacks for P/E, P/B,
  ROE, ROCE, debt/equity, margins and dividend yield.
- Made low/high-volatility regimes reachable as primary labels before plain
  sideways classification, and included regime parameters in result identity.
- Emitted the defined preprocessing-fitted and selection-completed events from
  synchronous service paths, with delivery failures contained.
- Included row content, leakage mode and outlier threshold in selection and
  preprocessing identities so different fits/reports cannot collide.
- Kept feature schema metadata in feature-owned tables instead of overwriting
  the shared SQLite `user_version`, and made failure records append-only.
- Included raw feature definitions, context identity and source bars in
  reproducibility digests; included news relevance/importance/timing changes in
  the cache identity.
- Added explicit skipped/partial reasons for primary-data absence in context,
  fundamental and multi-timeframe blocks.

## 11. Remaining limitations

- **No live-market verification:** all evidence is deterministic fixture data
  and the real application/store code paths. No live provider response, live
  exchange session, or production market history was used to claim accuracy.
- Free providers remain delayed and depth-limited; intraday VWAP/history depends
  on validated intraday bars being available.
- Fundamentals and news coverage depends on the existing services; missing
  values are explicit and are never backfilled.
- Regime confidence is deliberately uncalibrated (`None`) in PI-04; labels
  describe current conditions, not guaranteed future direction.
- No feature-selection model, model importance or automated deletion is
  implemented; diagnostics only, as required before model phases.
- Feature generation is service/API-boundary only; no HTTP route or UI was added
  in this phase.
- Unverified in this verification pass: live-provider responses and a production
  market account, calibrated regime confidence, and the full repository test
  suite (deliberately not run under the efficient-testing rule). The affected
  suite reported two tests skipped under their own conditions.
- The local 13.10 ms measurement is not a claim about production machines or
  provider-inclusive end-to-end latency.

## 12. Final acceptance verdict

PI-04 meets the requested implementation scope: centralized versioned feature
generation, all required indicator families, market structure, MTF aggregation,
regime detection with honest uncertainty, context/fundamental/news provenance,
profiles, preprocessing, selection diagnostics, persistence, bounded
performance and actual NovaMarket/PI-01/PI-02/PI-03-path integration.

The focused and affected tests pass, mypy is clean for the changed surface, the
architecture validators pass, and no unresolved PI-04 blocking defect is known.
The phase is **PASS** with the data/production limitations recorded above.

**PI-05 was not started.** No files were staged, committed, pushed, tagged or
merged; the user's existing changes remain preserved.
