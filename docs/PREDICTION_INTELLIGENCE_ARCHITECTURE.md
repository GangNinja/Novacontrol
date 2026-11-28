# Prediction Intelligence Architecture (PI-01 through PI-18)

**Status:** PI-01 through PI-18 implemented and integrated. PI-12 connects
stored PI-08/PI-10 forecasts and PI-11 evaluation evidence to the accepted
NM-08/NM-10 autonomous paper-trading path. PI-13 adds an isolated historical
RL research environment. PI-14 adds transparent reward calculation and policy
evaluation on that environment, still with no production risk, execution or
autonomy path. PI-15 adds bounded DQN training, checkpoint recovery, validation,
out-of-sample testing and an experimental policy registry on the same isolated
research database. No PI-15 policy can be activated. PI-16 adds regime-aware
adaptive research, drift/degradation detection and bounded reassessment. PI-17
adds coordinated, read-only financial specialists and advisory aggregation.
PI-18 adds read-only portfolio optimization, capital allocation, diversification,
liquidity, scenario and allocation-backtest intelligence over those layers.
None of PI-16, PI-17 or PI-18 adds an activation, order, broker or live-trading
path; NM-08 and NM-10 remain the operational authorities.
**Programme:** NovaMarket Prediction Intelligence (PI-01–PI-18).
**Scope of PI-01:** contracts, boundaries, lifecycle semantics, data/freshness
interfaces, ensemble eligibility, persistence, and the existing-system
integration. No production model training, scheduler, or reinforcement-learning
algorithm is implemented in that phase.
**Scope of PI-02:** versioned historical OHLCV datasets, acquisition through the
existing provider engine, normalization and quality validation, missing-data
classification, deterministic conflict resolution, leakage-safe chronological
training datasets, and corporate-action adjustment where verified data exists.
**Scope of PI-03:** the opt-in low-latency acquisition/distribution gateway:
separate intraday path, priority/fair rotation, caching/coalescing, provider
contracts, checkpoints, fallback/conflict reporting and measured latency. It
does not implement a forecast scheduler or a trading authority.
**Scope of PI-04:** the versioned feature-engineering and market-regime layer:
point-in-time feature generation over validated observations, multi-timeframe
alignment, market structure, fundamentals/news context, preprocessing and
selection diagnostics, and a deterministic regime result. It adds no model,
prediction, risk or execution authority.
**Scope of PI-05:** statistical baseline forecasts — naive/last-value,
random-walk, historical-return, seasonal-naive, moving averages, exponential
smoothing, Holt/Holt-Winters and a bounded pure-stdlib ARIMA — with
multi-horizon contracts, empirical/model intervals, direction classification,
leakage-safe validation/test selection, walk-forward evaluation and regime-aware
comparison. It adds no risk or execution authority and claims no calibrated
probability.
**Scope of PI-06:** XGBoost and LightGBM gradient-boosting forecasts — native
booster training for return regression and direction classification, all ten
PI-01 horizons, validation-only bounded configuration selection, walk-forward
refits, PI-05 baseline comparison, TreeSHAP-style local contributions, native
artifact persistence and the existing forecast API/UI. It adds no promotion,
risk or execution authority and withholds uncalibrated probabilities.
**Scope of PI-07:** PyTorch CPU LSTM sequence models over chronological PI-04
feature windows — configurable sequence lengths and bounded architectures,
regression and three-class direction heads, all ten PI-01 horizons when the
observed resolution and usable history support them, training-only
preprocessing, validation-selected candidates with early stopping and best-
checkpoint restoration, native state-dict artifacts with checksums, walk-forward
evaluation, PI-05 and PI-06 comparisons, and the existing forecast API/UI. It
adds no promotion, risk or execution authority and withholds uncalibrated
probabilities.
**Scope of PI-08:** the evidence-weighted ensemble over the PI-05 statistical,
PI-06 gradient-boosting and PI-07 LSTM families — eligibility and registry
evidence, leakage-safe weighting and validation-only conformal calibration,
disagreement analysis, prediction intervals, explicit abstention/statistical
fallback, regime evidence, multi-horizon evaluation, versioned persistence and
the existing forecast API/UI. It does not promote models or create trading
authority.
**Scope of PI-09:** durable, market-session-aware forecast lifecycle scheduling —
stable cycle identity, T−20/T−5/T−1/T checkpoints, immutable versions,
PI-03 freshness validation, PI-08 ensemble integration, revision comparison,
target-time evaluation, bounded retries, lease recovery, accuracy history and
the existing forecast API/UI. It adds no trading, risk, portfolio or execution
authority.
**Scope of PI-10:** swing and long-term outlook over the existing PI-01–PI-09
stack — explicit 1d/3d/5d session targets and 30d/90d calendar targets,
point-in-time fundamental/news parsing, validated sector/benchmark/peer
context, horizon-specific orchestration, cross-horizon analysis, horizon-
separated outcome metrics and advisory-only autonomy evidence. It adds no
prediction engine, forecast store, fundamental ingestion service, risk engine
or execution path.
**Scope of PI-12:** the read-only bridge from stored PI-08 ensemble and PI-10
outlook forecasts into the accepted AT-03 decision pipeline — explicit
BUY/SELL/HOLD/WAIT/NO_ACTION decisions, horizon selection, uncertainty and
disagreement policy, portfolio/open-order awareness, NM-08 risk assessment,
NM-10 paper execution through the existing AT-04 path, immutable decision audit
and separate forecast/decision/execution/portfolio outcomes. It does not
regenerate a forecast, calculate risk, mutate a portfolio or submit an order
directly.
**Scope of PI-13:** the isolated, reproducible reinforcement-learning research
environment — point-in-time observations, a long-only bounded action space,
deterministic next-bar simulation, its own virtual cash/position book, episode
lifecycle, reward interfaces, bounded experience storage, recovery, evaluation
and the React research workspace. It has no NM-07/NM-08/NM-10 or autonomy
authority; it never promotes a policy.

**Scope of PI-14:** versioned reward configurations, independently configurable
reward components, attribution/normalization, policy evaluation on identical
simulated conditions, no-action/rule-based/PI-12/experimental-RL baselines,
chronological splits and walk-forward evidence. It remains advisory and cannot
promote or activate a policy.

**Scope of PI-15:** bounded experimental DQN training over PI-13 environments and
PI-14 rewards — immutable training configurations, chronological split
preparation, seeded replay/target-network training, periodic/best/manual/final
checkpoints, recovery, numerical-stability guards, validation and out-of-sample
testing through the PI-14 evaluator, bounded random/grid search, agent comparison,
artifact versioning and the React training workspace. Trained policies are
advisory research artifacts; there is no activation, promotion, NM-08 bypass,
NM-10 order or broker path.

**Scope of PI-16:** isolated regime-aware adaptive research — regime evidence,
suitability, deterministic data/concept drift and degradation detection,
hysteresis-controlled advisory recommendations, point-in-time experience
retrieval and explicit bounded PI-14/PI-15 reassessment. It does not train,
promote, activate or execute anything.

**Scope of PI-17:** coordinated multi-agent financial intelligence — nine
explicit read-only roles, deterministic point-in-time evidence validation,
bounded concurrent orchestration, conflict/correlation handling, evidence-
weighted advisory aggregation, recovery and an explicit handoff into the
existing PI-12 → NM-08 → NM-10 path. It adds no second agent framework,
forecast/risk/execution engine or portfolio authority, and it cannot submit an
order or change autonomy, permissions, policies, brokers or live-trading state.

**Scope of PI-18:** read-only portfolio optimization and capital allocation —
authoritative NM-07 snapshot ingestion, explicit eligibility and freshness
rules, horizon-compatible PI-05–PI-10 expected returns, validated covariance
and correlation, constrained long-only allocation, diversification and
liquidity analytics, historical/hypothetical scenarios, rebalancing
recommendations and point-in-time strategy comparison. Recommendations are
isolated in `po_*` research tables and handed to PI-12 only through an advisory
contract; PI-18 has no portfolio, ledger, NM-08 approval or NM-10 execution
path.

NovaMarket remains paper-only:

- `paper_only = true`
- `live_trading = false`
- `broker_connected = false`
- NM-08 remains the sole risk authority.
- NM-10 remains the sole paper-execution authority.
- Prediction Intelligence never creates an order, fill, position or ledger entry.

## 1. Capability domains

Prediction Intelligence is one programme inside NovaMarket, not a separate
application. Its seven capability domains are declared in
`architecture.PREDICTION_DOMAIN_BOUNDARIES` and validated by
`validate_prediction_architecture()`.

| Domain | Responsibility | Implementation boundary |
|---|---|---|
| Virtual market and financial system | NovaCoin, portfolios, valuation, accounting and ledger context | Existing NM-03/NM-07 authorities; prediction reads only |
| Market data intelligence | Quotes, bars, fundamentals, news, events, provenance and freshness | `PredictionDataGateway` over the existing `MarketDataEngine` |
| Prediction intelligence | Versioned forecast assembly and abstention | `EnsembleAssembler`, `ForecastOutput` |
| Forecast lifecycle and confirmation | T−20, T−5, T−1 and T | `PredictionIntelligenceService`, `ForecastLifecycle` |
| Autonomous trading intelligence | Read-only decision evidence | `DecisionForecastEvidence`, `authorizes_action=false` |
| Reinforcement learning and adaptive intelligence | Advisory experience/policy boundaries | `learning_boundary_contract()`; no training or promotion |
| Performance, evaluation and observability | Forecast errors, direction accuracy, calibration and provider limitations | `ForecastEvaluation`, typed events, status/contracts |

The canonical NM-01–NM-14 roadmap is unchanged. Prediction Intelligence has its
own additive roadmap:

| Phase | Title |
|---|---|
| PI-01 | Architecture, Contracts & Existing-System Integration |
| PI-02 | Historical Market Dataset Engine |
| PI-03 | Low-Latency Market Data Gateway |
| PI-04 | Feature Engineering & Market Regime Detection |
| PI-05 | Statistical Baseline Forecasting Models |
| PI-06 | Gradient-Boosting Forecasting Models |
| PI-07 | LSTM Deep Learning Forecasting Models |
| PI-08 | Ensemble Forecasting & Uncertainty Calibration |
| PI-09 | 20-Minute Forecast and Confirmation Scheduler |
| PI-10 | Swing and Long-Term Forecasting |
| PI-11 | Prediction Evaluation & Backtesting |
| PI-12 | Autonomous Trading Integration |
| PI-13 | Reinforcement-Learning Environment |
| PI-14 | Reward Calculation & Policy Evaluation |
| PI-15 | RL Agent Training & Optimization |
| PI-16 | Performance, Hardening & Program Acceptance |
| PI-17 | Multi-Agent Trading Intelligence |
| PI-18 | Advanced Portfolio Optimization & Capital Allocation |

## 2. Module map

```text
src/novacontrol/novamarket/
├── optimization/
│   ├── models.py       PI-18 configs, horizons, snapshots, statuses and results
│   ├── inputs.py       read-only NM-07/NM-02/PI-04/PI-05..PI-10 adapters
│   ├── analytics.py    covariance, risk, diversification, liquidity and scenarios
│   ├── solvers.py      deterministic long-only constrained allocation solvers
│   ├── backtesting.py  point-in-time strategy comparison with costs
│   ├── store.py        isolated po_* SQLite records and recovery
│   └── service.py      run lifecycle, validation, API surface and handoff
├── multiagent/
│   ├── models.py       PI-17 roles, messages, evidence, decisions and limits
│   ├── agents.py       nine deterministic read-only specialist implementations
│   ├── aggregation.py  conflicts, correlation, weighting and decision authority
│   ├── store.py        isolated ma_* SQLite tables and recovery
│   └── service.py      bounded orchestration, configuration, API surface, handoff
├── evaluation/
│   ├── models.py       PI-11 evaluation records, provenance and config
│   ├── metrics.py      price, return, direction, calibration and leakage metrics
│   ├── store.py        append-only PI-11 report database and verify/reproduce
│   └── service.py      read-only forecast sources and evaluation orchestration
├── prediction/
│   ├── models.py       versioned value contracts and policy
│   ├── data.py         read-only gateway over NM-02
│   ├── ensemble.py     eligibility, weighting, disagreement, abstention
│   ├── lifecycle.py    T−20/T−5/T−1 scheduling and change detection
│   ├── store.py        prediction-owned SQLite tables in the existing file
│   ├── service.py      orchestration, evaluation, evidence, status/contracts
│   └── __init__.py     public PI-01 surface
├── scheduling/
│   ├── models.py       PI-09 cycles, checkpoints, outcomes and horizon policy
│   ├── schedule.py     verified calendar target/checkpoint calculation
│   └── service.py      durable checkpoint coordinator and evaluation
├── outlook/
│   ├── models.py       PI-10 horizon profiles and context contracts
│   └── service.py      point-in-time context, five-horizon and accuracy layer
├── statistical/
│   ├── models.py       PI-05 contracts, model metadata and bounded config grids
│   ├── arima.py        pure-stdlib ARIMA/ADF/residual diagnostics
│   ├── estimators.py   naive, average, exponential and Holt-Winters models
│   ├── metrics.py      price/direction/interval/regime metrics
│   ├── validation.py   chronological walk-forward comparison and selection
│   └── service.py      application orchestration and existing-store persistence
├── gradient/
│   ├── models.py       PI-06 config, schema, artifact, result and explanation contracts
│   ├── training.py     leakage-safe matrices, native boosters, metrics and walk-forward folds
│   ├── artifacts.py    native model save/load, checksums and metadata
│   ├── service.py      orchestration, persistence, inference, selection and API surface
│   └── __init__.py     public PI-06 surface
├── lstm/
│   ├── models.py       PI-07 config, sequence scaler, forecast/report/artifact contracts
│   ├── sequence.py     chronological windows, train-only scaling and inference windows
│   ├── network.py      lazily imported CPU PyTorch LSTM with regression/classification heads
│   ├── training.py     deterministic fits, early stopping, metrics, walk-forward and comparison
│   ├── artifacts.py    atomic native state-dict save/load and checksum verification
│   ├── service.py      orchestration, persistence, inference, explanation and API surface
│   └── __init__.py     public PI-07 surface
├── datasets/
│   ├── models.py       build requests, versions, coverage, gaps, actions
│   ├── store.py        dataset-owned tables in the existing SQLite file
│   ├── validation.py   selection, OHLCV/gap validation, coverage
│   ├── training.py     chronological splits, purge, target construction
│   ├── corporate.py    verified price-factor adjustment on retrieval
│   ├── acquisition.py  bounded range calls through MarketDataEngine
│   ├── service.py      build/read/batch/coverage/quality/training surface
│   └── __init__.py     public PI-02 surface
├── marketdata/gateway.py opt-in low-latency gateway over the NM-02 engine
├── features/
│   ├── models.py       versioned feature, regime, preprocessing contracts
│   ├── definitions.py  the single 122-feature registry and profile map
│   ├── engine.py       point-in-time calculations, MTF aggregation, context
│   ├── regime.py       deterministic primary/secondary regime detector
│   ├── preprocessing.py train-only fit, missing policies, outlier flags
│   ├── selection.py    correlation, redundancy, missingness, leakage diagnostics
│   ├── store.py        feature-owned SQLite tables in the existing file
│   ├── service.py      generation, persistence, reads, status/contracts
│   └── __init__.py     public PI-04 surface
├── service.py          lazy composition root and PI-01–PI-07 status/contracts
├── architecture.py     seven domains, PI roadmap, storage boundaries
├── permissions.py      prediction and dataset operations
└── events.py           prediction, dataset, gateway and feature events
```

PI-01–PI-04 deliberately add no HTTP routes. PI-05, PI-06 and PI-07 extend
the existing `/market/forecasts*` surface rather than creating a parallel one;
PI-06/PI-07 share the one on-demand explanation route. Every route is declared
in `ApiSurface`, `ROUTE_CONSUMERS`, the route-contract tests and `docs/API.md`
together. Frontend consumers use that surface and the service boundary; they
must not infer a forecast from a quote or treat a dataset version as a live
quote.

## 3. Market-data boundary

`PredictionDataGateway` is read-only. It calls only:

- `MarketDataEngine.get_instrument(instrument_id)`
- `MarketDataEngine.latest(instrument_id)`
- `MarketDataEngine.bars(instrument_id, ...)`
- `MarketDataEngine.provider_health(provider)`

It does not create a provider, fetch a URL, mutate a cache policy, bypass the
general 15-minute refresh floor, or change the 20-instrument discovery cap.

The snapshot preserves:

| Field | Meaning |
|---|---|
| `source_observed_at` | when the source says the observation existed |
| `received_at` | when NovaMarket received it |
| `resolution` | spot, 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo |
| `quality` | NM-02 quality class |
| `freshness` | stage-aware freshness assessment |
| `provider` | source identity |
| `provider_health` | measured provider state |
| `provider_conflict` | providers disagree; no price is averaged |
| `missing_observation` | no usable observation was available |
| `rate_limited` | provider reported a rate-limit condition |
| `history_bars` / `history_sufficient` | available history versus horizon recommendation |
| `provenance` / `evidence` | audit trail and the source records |

A cached observation is never upgraded to fresh. If the observation is older
than the stage window, the snapshot is `STALE` and unusable; the forecast is
skipped or abstained. If the source is `CLOSED_MARKET` and still within the
stage window, the market session state is preserved rather than relabelled as a
live quote. A snapshot supplied directly to the service is revalidated against
the request instrument/exchange, stage maximum age, resolution, quality and
freshness; a caller cannot bypass the gateway's freshness rules.

## 4. Data contracts

All are versioned dataclasses in `prediction/models.py`:

| Contract | Purpose |
|---|---|
| `PredictionRequest` | instrument, exchange, horizon, target, lifecycle creation time |
| `MarketDataSnapshot` | exact market observation and data-quality evidence |
| `FeatureDefinition` | named, versioned feature meaning, dtype, source and lookback |
| `FeatureVector` | versioned feature row with `as_of`, values, snapshot ids and lineage |
| `FundamentalSnapshot` | company fundamentals with fiscal period, source and receipt time |
| `NewsEventEvidence` | timestamped news/announcement/corporate-event evidence |
| `HistoricalMarketDataset` | versioned historical series reference and quality |
| `PredictionEvidenceBundle` | references the market, feature, fundamental, news and dataset evidence |
| `ModelMetadata` | model identity, version, family, state, calibration, feature/dataset versions |
| `ModelPrediction` | one model's return/interval/probability contribution and data snapshot |
| `ForecastOutput` | the forecast/response contract: price, return, direction, bounds, uncertainty, probabilities, model/ensemble/data lineage; `PredictionResponse` is the same public shape |
| `ForecastLifecycle` | one lifecycle identity and its three scheduled stages |
| `LifecycleStageRecord` | immutable initial/first/final stage record, changes and skip reasons |
| `ForecastEvaluation` | actual outcome, direction correctness, error, interval coverage, Brier/log loss |
| `DecisionForecastEvidence` | read-only evidence for the existing decision pipeline |

`ForecastOutput` carries `contract_version`, `model_id`, `model_version`,
`ensemble_id`, `member_versions`, `data_snapshot_id`, `feature_set_version`,
`dataset_version`, `generated_at`, `data_observed_at`, `data_received_at`, and
the target. Unsupported values are explicit: `"unspecified"` means a future
model did not declare the version; it is not a fabricated identifier.

## 5. Forecast output semantics

Directions are `up`, `down`, `neutral`, `uncertain` and `invalid`.

- A return above the horizon's neutral band is `up`; below its negative band is
  `down`; inside it is `neutral`.
- Model disagreement above the actionable threshold yields `uncertain`, not a
  manufactured direction.
- Missing/stale/incompatible data yields `invalid`.
- An ensemble can abstain entirely; `EnsembleResult.output` is then `None`.
- Initial and T−5 forecasts are `unconfirmed`; only T−1 is `confirmed`.

Probabilities are withheld (`probability_status=unavailable`, p values `None`)
unless every participating model declares calibrated probabilities. An
arbitrary model score is never relabelled as a statistically valid probability.

## 6. Horizons

| Family | Horizons | Maximum input resolution | Minimum history guidance |
|---|---|---|---|
| Intraday | 1m, 5m, 15m, 20m, 1h | 1m / 1m / 5m / 5m / 15m | 30 / 60 / 48 / 48 / 40 bars |
| Swing | 1d, 3d, 5d | 1d | 80 / 80 / 100 bars |
| Long-term | 30d, 90d | 1d | 120 / 180 bars |

The one-minute horizon has its own scheduler lifecycle (initial at creation,
T−30s first confirmation, T−5s final confirmation) rather than being only the
last checkpoint of a longer horizon. The post-PI-24 multi-timeframe decision
layer reads completed 20m/5m/1m observations independently of these forecast
cycles; see
[MULTITIMEFRAME_TRADING_INTELLIGENCE.md](MULTITIMEFRAME_TRADING_INTELLIGENCE.md).

Target semantics are explicit. Swing targets count 1, 3 or 5 verified trading
sessions after the decision session; long-term targets count 30 or 90 elapsed
calendar days and then take the first verified session close. A Friday 3d swing
target is therefore Wednesday, not Monday. The same rules are used by PI-09
`default_target`, PI-05 `_target_at`, PI-02 training labels (for daily bars) and
PI-10 expected-target validation. Training labels count verified sessions from
the calendar; a missing row for a verified session makes the target unavailable
rather than shifting it to the next observed row.

`HorizonSpec.accepts_resolution()` is enforced. A five-minute forecast cannot
be claimed from daily-only data, regardless of when the daily file was
downloaded. Spot quotes are finer than any bar interval and are accepted where
the horizon permits them; history sufficiency remains visible.

## 7. Ensemble architecture

Supported future families: naive, moving average, ARIMA, XGBoost, LightGBM,
LSTM, ensemble and external. PI-01 implements the assembly boundary, not model
training.

For each request:

1. Validate the market snapshot and horizon/resolution compatibility.
2. Exclude invalid, unregistered, unsupported, duplicate, stale-snapshot or
   future-generated contributions with a reason.
3. Require the prediction's model family to match its registered metadata.
4. Apply model metadata weight, halving degraded models.
5. Cap the participant count and require the configured minimum.
6. Detect weighted disagreement; abstain above the abstention threshold.
7. Aggregate return and interval, widen uncertainty for disagreement.
8. Carry probabilities only when all participants are calibrated.
9. Emit an immutable `ForecastOutput`, or abstain if the aggregate lower
   return would imply a non-positive price.

A single unavailable, invalid or suspended model therefore does not crash the
ensemble; it is excluded and the remaining eligible set is evaluated. If none
remain, the ensemble abstains.

## 8. Lifecycle

For target T:

| Stage | Time | Behaviour |
|---|---|---|
| Initial | T−20 | Generate/persist the initial forecast and evidence |
| First confirmation | T−5 | Re-read latest valid data, recalculate, compare, persist a new version |
| Final confirmation | T−1 | Re-read latest valid data, final confirmation, persist a new version |
| Target | T | Evaluate every preserved version against the actual outcome |

The initial stage is enforced to occur before T−5. Each stage is a separate
immutable row. SQLite triggers reject UPDATE/DELETE on forecasts, stages,
evaluations and evidence. A confirmation cannot overwrite the initial forecast.
`PredictionPolicy.stage_tolerance_seconds` (90 by default) permits bounded
scheduling jitter around a stage; it does not permit skipping a stage,
relabelling a later stage as the initial forecast, or starting after T−5.

If data is missing or stale at T−5, the stage is recorded as `skipped`; the
initial forecast remains untouched. At T−1 the same condition records `invalid`
and the lifecycle becomes `unconfirmed`. No observation is fabricated and no
stale snapshot is silently reused. A forecast cannot authorize an action at
T−20; execution timing remains a separate decision. Evaluation is idempotent for
the recorded actual price and refuses a later evaluation that supplies a
different actual outcome, so one lifecycle has one outcome for its preserved
forecasts.

## 9. Low-latency path

The general refresh floor remains **900 seconds**. The intraday path is separate
and explicitly bounded by `AcquisitionPolicy`:

- minimum intraday interval: 30 seconds;
- intraday request limit: 20/minute;
- discovery batch cap: 20 instruments;
- at most 2 concurrent fetches;
- reserved priority slots for active positions, forecast candidates and the
  watchlist;
- request timeout, retry and failover limits;
- provider latency, health, conflicts and rate-limit state are surfaced in the
  snapshot.

The path is a priority/caching policy boundary, not a new provider authority and
not a bypass of fair rotation. Free providers are not claimed to be
exchange-grade real-time feeds. If a provider cannot satisfy the requested
freshness, the forecast reflects the limitation or abstains.

## 10. Decision integration

`DecisionForecastEvidence` is the only bridge to the existing autonomous
pipeline. It contains instrument, direction, expected return, uncertainty,
probabilities (when calibrated), reference price/time, target, data timestamp,
freshness, final-confirmation status and eligibility.

It always carries:

- `authorizes_action = false`
- `is_forecast = true` / `is_market_observation = false`
- `risk_authority = NM-08`
- `execution_authority = NM-10`
- `decision_authority = existing autonomous trading pipeline`
- `paper_only = true`, `live_trading = false`, `broker_connected = false`

Disabled, monitoring, autonomous, paused, recovery and kill-switch semantics are
unchanged. Long-only and trailing-exit restrictions are unchanged. No
short-selling, forced end-of-day liquidation or target-weight rebalancing is
introduced.

## 11. Reinforcement-learning boundary

`learning_boundary_contract()` defines advisory-only experience, state,
observation, action, reward, penalty, episode, policy and evaluation fields. The
future reward model may consider realised P&L, risk-adjusted return, drawdown,
costs, slippage, capital utilisation, decision quality, unnecessary trading,
avoidable losses and risk compliance.

A profitable outcome does not automatically imply a good decision, and a losing
outcome does not automatically imply an irrational decision. Evaluation uses
only information available at decision time. No algorithm, training run,
automatic promotion or NM-08 policy mutation is implemented in PI-01.

## 12. Persistence and versioning

Prediction tables live in the existing NovaMarket SQLite file:

- `prediction_requests`
- `prediction_lifecycles`
- `prediction_forecasts`
- `prediction_stages`
- `prediction_models`
- `prediction_snapshots`
- `prediction_ensembles`
- `prediction_evaluations`
- `prediction_evidence`
- `prediction_forecast_cycles`
- `prediction_forecast_checkpoints`
- `prediction_forecast_outcomes`

`prediction_meta.schema_version` is owned by the prediction layer. The store
creates only its own tables and does not migrate unrelated financial tables.
Forecasts, stages, evaluations and evidence are append-only; model metadata is
versioned and state-updatable. Each forecast records its model versions, data
snapshot, feature/dataset version, generation time and lifecycle stage.

## 13. API and frontend integration

PI-01 exposes prediction status through `/market/status` and contracts through
`/market/contracts`; no additional route was added. Future UI work should show:

- predicted direction and predicted price range;
- expected return, confidence and uncertainty;
- horizon and target timestamp;
- last market-data timestamp and freshness;
- initial, T−5 and T−1 versions and changed fields;
- prediction accuracy and actual outcome.

A prediction must be visually distinct from an observed market price. The UI
must display `is_forecast=true` / `is_market_observation=false`, the data
timestamp and the validity state; it must not render an unconfirmed forecast as
a confirmed one.

## 14. Performance and resources

`ResourcePolicy` keeps work local and bounded: lazy model loading by default, at
most two loaded models, 10-second inference timeout, 30-second CPU budget, 512 MB
memory budget, prediction-owned cache, mandatory failure isolation and no
distributed workers or background scheduler in PI-01. The existing
Python/FastAPI process, SQLite file, event bus and audit layer are reused.

## 15. Verification and limitations

PI-01 verification is recorded in
[PI01_COMPLETION_REPORT.md](PI01_COMPLETION_REPORT.md) and PI-02 in
[PI02_COMPLETION_REPORT.md](PI02_COMPLETION_REPORT.md); PI-03 in
[PI03_COMPLETION_REPORT.md](PI03_COMPLETION_REPORT.md). The implementation
deliberately does not include model training, a lifecycle scheduler, an RL
environment, HTTP routes, or a complete prediction UI. Those are later phases;
the contracts and authority boundaries they must use are established here.

## 16. Historical Market Dataset Engine (PI-02)

PI-02 is an additive layer over NM-02. It owns dataset definitions, immutable
version metadata and training-sample references; it does **not** own a provider,
transport, quote store or second price history. Every candle it exposes is a
reference to an authoritative NM-02 `bar_id`.

### 16.1 Acquisition

`DatasetAcquisition.collect` asks `MarketDataEngine.ingest_history` for each
requested instrument, sequentially and in the request's provider priority
order. The existing engine still owns adapter selection, retry/backoff, rate
limiting, circuit breakers, fair rotation, normalization, quality assessment
and the append-only store. A build never calls a provider directly.

- Requests are capped at 20 instruments (the existing discovery batch cap) and
  a 10-year range.
- A build fetches the complete requested range by default. `incremental=True`
  starts at the latest stored observation instead, overlapping that candle so a
  provider revision is re-read and deduplicated; the operational fetch mode is
  not part of dataset content identity.
- Provider attempts are sequential so a free-provider rate limit is not turned
  into a parallel burst.
- If a provider fails, the next requested provider is tried; the failure text is
  retained when all providers fail. Previously stored observations remain.
- Partial candles are excluded unless `allow_partial=True` is explicit. A
  partial current weekly/monthly candle therefore never enters a dataset by
  default.
- Repeated ingestion is idempotent through NM-02's unique observation key.

Provider capability is declared by the existing adapters, not assumed:

| Provider | Historical OHLCV used by PI-02 | Coverage and constraints |
|---|---|---|
| Yahoo Finance | 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo | Free, delayed, unofficial; NSE/BSE via `.NS`/`.BO`; intraday depth is provider-limited; no adjusted-close field is trusted as exchange truth |
| CoinGecko | Crypto only | Not an NSE/BSE equity history source |
| Alpha Vantage | Only when a key and the free tier permit | 25 requests/day free tier; not enabled by default |

No paid subscription, access-control bypass or synthetic intraday history is
introduced. A daily series is never relabelled as an intraday series.

### 16.2 Normalization and identity

All observations pass the existing `_normalize_bar` path before membership:
canonical `XNSE:RELIANCE`-style instrument ids, exchange and MIC, exchange
timezone (`Asia/Kolkata` for NSE), UTC `observed_at`, `retrieved_at`, source
 timezone, currency, `Decimal` conversion at the market-data boundary, the
 provider's volume unit (preserved, never converted without a verified unit
 contract), resolution and provider metadata. Provider-specific fields stay in
 `provider_metadata` for traceability. Dataset storage keeps decimal strings, so
 no float round-trip is introduced.

### 16.3 Validation and quality

`datasets/validation.py` is deterministic and never fills a price:

- OHLC bounds (`high >= max(open, close, low)`, `low <= min(...)`), positive
  prices, non-negative volume and finite numerics are enforced structurally by
  the existing bar model and quality engine. Invalid bars remain stored and are
  excluded from trusted membership.
- Duplicate provider observations are identified by the store's unique key;
  same-timestamp observations from different providers are provider conflicts,
  not duplicates.
- Timestamps must be timezone-aware and aligned to the resolution's candle
  boundary; out-of-order input is detected and ordered deterministically.
- Exchange sessions and holidays come from the existing calendars; a weekday is
  never assumed to be a trading day.
- Volume spikes and large price moves are warned about, not silently deleted.
  `SUSPECT` observations are retained in NM-02 and excluded from trusted
  membership with a counted reason; they can be inspected and re-admitted only
  by an explicit future policy, never by a filler.

Dataset quality is separate from NM-02 bar quality: `valid`,
`valid_with_warnings`, `incomplete`, `invalid`, `unverified` and
`provider_conflicted`. `STALE` historical bars may belong to a dataset (history
is expected to be old) but rank below `VALID`/`WARNING` during conflict
resolution. Training admission requires `valid` or `valid_with_warnings`.

### 16.4 Missing data and market gaps

A gap is recorded only for an **expected** observation that is absent. Weekends,
exchange holidays and declared closures are counted in coverage, not reported as
data errors. The gap vocabulary distinguishes missing provider observation,
exchange holiday, market closure, trading halt, suspension, no-trade interval,
provider outage and incomplete coverage. Halts, suspensions and no-trade
intervals require an explicit `MarketAnnotation` with a source; they are never
inferred from a missing candle. Provider outages are recorded from acquisition
errors.

No forward-fill, interpolation or imputation is applied. Adjusted series are
derived on retrieval from verified corporate actions; the raw observations stay
recoverable. A synthetic observation is never labelled as an exchange
observation.

### 16.5 Deduplication and conflict resolution

One selected bar per `(instrument, resolution, observed_at)` is stored in a
dataset version. When providers disagree, selection is deterministic:

1. higher NM-02 quality (`VALID`, `WARNING`, `STALE`, then others);
2. declared provider priority from the build request;
3. non-null volume before null volume;
4. earliest retrieval time;
5. provider name, then `bar_id` as stable tie-breakers.

The chosen bar and every alternative are recorded in `dataset_selections`, so a
correction is auditable and the same inputs produce the same version. A
lower-quality observation never overwrites a validated one; the store is
append-only and dataset versions are immutable.

### 16.6 Storage and retrieval

Dataset tables share the existing NovaMarket SQLite file:

- `dataset_meta`, `dataset_builds`, `dataset_versions`;
- `dataset_segments`, `dataset_members`, `dataset_selections`;
- `dataset_gaps`, `dataset_corporate_actions`, `dataset_annotations`;
- `training_versions`, `training_samples`, `training_splits`.

Members store `bar_id` references, not duplicated OHLCV. Indexes cover dataset
and version, instrument/exchange/time, resolution, provider, quality and
observed time. Versions, members, selections, gaps and training rows are
protected by immutability triggers. Retrieval is paginated and batch-capable:
by instrument, exchange, date range, resolution, version and quality; plus
metadata, coverage, missing-data, quality and version queries. NM-02 reads are
batched in chunks of 500 bar ids.

### 16.7 Versioning and reproducibility

A version records dataset id, version label, instruments and exchanges, range,
resolution, providers, collection/validation timestamps, normalization version,
price basis (raw or adjusted), quality and coverage summaries, missing summary,
corporate-action references, build configuration and acquisition reports. Its
`content_digest` is computed from the selected members, segments, normalized
configuration, coverage/validation summaries, quality and missing summary. Rebuilding identical content returns the existing version with
`duplicate=true`; an explicit label with different content is refused. A later
correction therefore creates a distinguishable version and the old version
remains readable.

### 16.8 Training datasets and leakage prevention

`build_training_dataset` groups observations by decision timestamp, splits them
chronologically into train/validation/test, and constructs targets only from a
strictly later bar. Supported horizons are 5m, 15m, 20m, 1h, 1d, 3d, 5d, 30d
and 90d. A sample is emitted only when the target bar exists and is valid; a
missing target is reported, never synthesized. Windows contain only bars at or
before the decision timestamp. Intraday targets use the first valid bar at or
after the horizon duration; daily swing/long targets use the verified calendar's
session target and require the exact bar for that session. A missing session row
is a missing target, not an invitation to use a later row. Every target
timestamp must be strictly greater than its decision timestamp.

Splits are purged: a sample whose target overlaps a later split is removed from
the earlier split, so test observations cannot influence training. Sample
indices are grouped by decision time, never randomly shuffled; `random` is not
used anywhere in construction. `verify_stored_training` re-checks chronology,
window and purge rules on read. The leakage rule is documented and tested:
features <= decision < target, test data is never used for fitting or model
selection, and corporate actions carry their actual `announced_at` when known.

### 16.9 Corporate actions

Only sourced, explicitly verified actions with a supplied `price_factor` are
applied. Raw and adjusted series are separate: membership and storage remain
raw, while `bars(adjusted=True)` derives an adjusted view and reports the
actions used. Splits, bonuses, rights, dividends, symbol changes, mergers and
delistings are represented; an unverified or factor-less action is recorded as
a limitation and changes no price. Adjustment factors are never invented.

### 16.10 Performance and failure handling

Builds read one instrument at a time, release candle objects after extracting
lightweight member references, paginate storage reads and use the engine's
retry/backoff/rate-limiter. A failed acquisition or invalid payload produces a
visible report and an `incomplete`/`invalid` version, not a complete one.
Previously stored observations are preserved; a failed build cannot delete or
rewrite a version.

Known limitations: Yahoo is delayed and unofficial; intraday depth and history
are provider-limited; no adjusted-close field is trusted, so adjusted views
depend on verified action records; halts/suspensions/no-trade intervals require
operator annotations; NSE/BSE corporate-action coverage is not assumed; and no
HTTP route or UI was added in PI-02.

## 17. Low-Latency Market Data Gateway (PI-03)

PI-03 adds `MarketDataGateway` as the single controlled market-data access path
for new consumers. It composes the existing `MarketDataEngine`, provider
adapters, `MarketDataStore`, calendars, freshness/quality validation and
historical acquisition; it does not add a provider, collector, database or
validation authority. `NovaMarketService.gateway()` exposes the lazy runtime and
`status.gateway` / `contracts.gateway` expose its state without adding routes.

The 900-second general polling floor is preserved. The intraday path is opt-in
(`gateway.enabled=false` by default), runs a separate bounded quote/intraday-bar
cycle, and never mutates the general worker. Both paths share adapters and the
same append-only store. Six application-state priorities receive the primary
allocation; a reserved lower-priority slice and per-level rotation prevent
starvation, while the existing 20-instrument cap and provider limits remain
authoritative.

Reads cache the engine result only for the short configured TTL and recompute
freshness on every read. `checkpoint` returns an explicit unavailable/stale
answer when no accepted fresh observation exists and never fabricates a value;
it reports whether the observation is newer than the previous checkpoint. The
PI-09 forecast scheduler remains responsible for calling it at T−5/T−1.

Provider contracts distinguish exchange, authorized redistribution, delayed
third-party, cached, historical, scraped and unknown sources. The installed free
providers are delayed third-party sources; none is presented as exchange
real-time or streaming. Fallbacks preserve provider identity and record the
preferred provider's failure. Conflicts remain the engine's no-average,
no-selection result. Source-observed, reception, persistence and consumer
timestamps are retained; source publication delay and separate parsing/
validation durations are reported as unavailable where the provider/engine
cannot supply them. P50/P95/P99 use collected samples only, never fixture values.

## 18. Feature Engineering & Market Regime Detection (PI-04)

PI-04 adds `src/novacontrol/novamarket/features/` as the single feature-
engineering and regime layer. It composes the NM-02 market-data engine and
store, the PI-02 dataset membership/reference rules, PI-01 prediction
contracts, PI-03 validation decisions already persisted by the engine,
`strategies/indicators.py`, existing fundamentals and the NM-04
`NewsEventEvidence` radar. It adds no provider, collector, database file,
indicator authority, risk authority or execution path.

`NovaMarketService.feature_service` is the lazy runtime. Its status and contract
are exposed under `status.features` and `contracts.features`; feature/regime
reads and preprocessing/selection calls are service methods. PI-04 adds no HTTP
route. Generation emits typed `NOVAMARKET_FEATURES_*`, `NOVAMARKET_REGIME_*` and
`NOVAMARKET_FEATURE_SELECTION_*` events.

### 18.1 Point-in-time input contract

`FeatureEngine` accepts bars directly (tests, callers with validated data) or
reads them from `MarketDataEngine.bars`; neither path acquires data. A feature
is calculated at an explicit `as_of` and only when all of the following hold:

- the bar's source observation time is at or before `as_of`;
- the bar's interval is complete by `as_of` (exchange session close when a
  calendar is known, otherwise the nominal interval end);
- the bar's quality is usable; invalid bars are excluded;
- the request is bounded to the most recent 5,000 accepted bars and to the
  requested `max_bars` window;
- optional PI-02 membership restricts reads to an explicit dataset version.

Duplicate bar ids are removed deterministically, out-of-order observations are
ordered by source time, and duplicate/gap/invalid/partial counts are recorded in
feature lineage. `source_last_at <= as_of` is an invariant; a value that cannot
be supported by completed observations is `UNAVAILABLE`/`INSUFFICIENT`, never
filled from a later bar. No feature uses the current unfinished candle as a
confirmed breakout; breakout/breakdown and range features compare a completed
close with the prior 20 completed bars.

### 18.2 Registry, profiles and versioning

`features/definitions.py` is the only registry: 122 named definitions with
family, formula, lookback, version, profile membership and provisional flag.
Feature-set version is `pi04-feature-set-1.0`; definitions are returned by
`FeatureEngine.definitions()` and persisted with their digest. The profile map
is intentionally different for each horizon:

| Profile | Focus |
|---|---|
| Intraday | returns, short MAs, momentum, short volatility, relative volume, session VWAP, short structure, regime, intraday MTF |
| Swing | daily/hourly trend, momentum, ATR/volatility, volume trend, structure, benchmark/sector context, fundamentals, events, regime |
| Long-term | weekly/monthly trend and volatility, valuation/profitability/cash-flow/debt fundamentals, sector context, corporate events, regime |

Adding a profile or feature is a registry edit plus a calculation/status mapping;
the engine, storage and vector contract do not need a parallel implementation.

### 18.3 Price, trend, momentum, volatility and volume formulas

The authoritative formulas are the registry entries; the principal definitions
are:

| Group | Definition |
|---|---|
| Returns | `return_n = close_t / close_t-n - 1`; `log_return_1 = ln(close_t / close_t-1)`; open-to-close uses the current open |
| Candle/range | `(high-low)/close`, body `abs(close-open)/close`, wicks relative to close, gap `open_t/close_t-1-1` |
| SMA/EMA/WMA | arithmetic mean; EMA seeded with the first SMA and `2/(n+1)` smoothing; WMA weights 1..n oldest to newest |
| MACD | EMA12 − EMA26, signal EMA9 of that line, histogram line − signal |
| RSI | Wilder smoothing, 14; a flat series is explicitly 50 |
| Stochastic | `100*(close-low14)/(high14-low14)`; %D is the 3-bar SMA of %K |
| ADX/DMI | Wilder-smoothed TR and ±DM; ADX from DX; +DI/−DI reported separately |
| ATR | Wilder-smoothed true range, 14 |
| Bollinger | 20-bar SMA ± 2 population standard deviations; width and band position derived |
| Historical volatility | sample standard deviation of 20 log returns × √252 |
| Volume | 20-bar volume SMA, relative volume, 5-bar volume ROC, OBV/VPT and their 10-bar normalized slopes, MFI(14) |
| VWAP | sum(typical price × volume)/sum(volume) over the latest completed intraday session only; unavailable on daily/weekly/monthly bars or zero volume |

`rsi_wilder`, `atr_wilder`, `ema_series`, `macd`, `directional_movement`,
`stochastic`, `wma`, `money_flow_index`, `vwap`, `obv_series` and `vpt_series`
live in the existing `strategies/indicators.py`. The older `rsi` definition is
left unchanged for NM-09 compatibility; PI-04 explicitly uses `rsi_wilder`.

### 18.4 Market structure and multi-timeframe rules

Structure uses confirmed 20-bar pivots: a pivot high/low is the centre of a
five-bar window (two bars on each side) and only becomes known two bars after
the centre. Flat tops/bottoms are valid swings; equal adjacent pivot values are
collapsed so a plateau is not compared with itself. HH/HL/LH/LL, continuation
and breakdown are therefore confirmed-bar features. Breakout/breakdown use the
latest completed close versus the prior 20-bar high/low; range position and
support/resistance proximity are derived from the same prior range.

Requested resolutions are 1m, 5m, 15m, 30m, 1h, 1d, 1wk and 1mo. An exact
validated series is used when available. If it is unavailable, a finer validated
series is aggregated using exchange-local session boundaries for daily bars and
calendar-week/month buckets; only COMPLETED buckets at or before `as_of` are
included, so the current partial week/month is never present as a weekly/monthly
feature. The derived timeframe and its source are recorded in lineage
(`derived_timeframes`). MTF features report available count, directional
alignment, trend agreement, momentum alignment, short/long volatility ratio and
the short close's position inside the longest available range. Missing
timeframes remain absent from `timeframes` and are never represented as
verified.

### 18.5 Market regime detection

`RegimeDetector` is a transparent threshold model, version `pi04-regime-1.0`.
It votes with close vs SMA20, SMA20 vs SMA50, SMA50 vs EMA50, +DI vs −DI, RSI
55/45, 20-bar return and MTF alignment. Strong trends additionally require
ADX >= 25 and a vote score >= 4; weak trends require a score >= 2 and ADX >= 20
(or a non-strong directional result). Weak MTF agreement cancels a weak score.

Primary labels are `strong_up`, `strong_down`, `weak_up`, `weak_down`,
`sideways`, `high_volatility`, `low_volatility`, `potential_reversal`,
`breakout`, `breakdown` and `uncertain`. Volatility is high when range
expansion >= 1.25, Bollinger width >= 0.10 or volatility change is elevated; low
uses the corresponding 0.75/0.025 thresholds. Reversal is a non-trending RSI /
band / momentum condition. Breakout and breakdown remain structural conditions;
they are not predictions.

Every result carries instrument, exchange, observation time, primary label,
simultaneous secondary conditions, supporting features, feature-set version,
detection method/version, data-quality status and any transition. A transition
reports `previous`, `current` and `changed` (including `changed=false` when the
label is unchanged). `confidence` is always `None` until a later calibration
phase measures it. Invalid or insufficient
features produce `uncertain` with a reason, not a guessed regime.

### 18.6 Fundamentals, news and context provenance

Fundamental features read the existing `FundamentalSnapshot`; they do not
acquire or invent statements. A snapshot is used only when its availability/
publication time is at or before `as_of`. Reported values carry
`provenance_kind=reported`; P/E, P/B, ROE, ROCE, margins, debt/equity, dividend
yield and growth are marked `derived` only when computed from reported inputs.
Missing metrics are `UNAVAILABLE`, never zero. Fundamental features are in swing
and long-term profiles only.

News/event features consume the existing radar's `NewsEventEvidence` (or the
same validated evidence objects). Publication/availability time must be at or
before `as_of`; source, instrument, category, importance, sentiment (only when a
validated component exists), age and deduplicated counts are preserved. Duplicate
articles collapse by stable evidence identity, not headline equality. Without a
validated sentiment component the sentiment feature is `UNAVAILABLE`; PI-04
makes no sentiment-accuracy claim.

Benchmark, sector and breadth features are generated only when corresponding
validated bars are explicitly supplied. The lineage
records supplied/missing context; breadth is the advancing share among supplied
context instruments and no market-wide value is invented.

### 18.7 Preprocessing, missing values and selection

`PreprocessingFit` supports standard, robust (median/MAD with IQR fallback),
min-max and L2 scaling. `fit` receives training rows only and stores the fitted
centers/scales; `transform` reuses those parameters, so validation/test rows
cannot move the statistics. Missing policies are explicit: `error`, `drop`,
`mean`, `median` and `zero`; the default is `median`, fitted on the training
rows and always reported as `imputed` rather than passed off as an observation.
Every transformed row retains status (`value`, `zero`, `missing`, `unavailable`,
`invalid`, `imputed`) and completeness. Outliers are reported separately by a
fitted-scale z-score threshold; flags do not silently delete values or turn a
legitimate extreme market move into invalid data.

`diagnose` reports pairwise correlation, zero/low variance, missingness,
rolling instability and target-correlation leakage candidates. It records a
decision for every feature (`keep`, `review`, `leakage_suspect`) and never
auto-deletes a correlated feature. Reports are immutable and versioned
(`pi04-selection-1.0`).

### 18.8 Persistence, reproducibility and performance

Feature tables (`feature_definitions`, `feature_sets`, `regimes`,
`preprocessing_fits`, `selection_reports`, `feature_generations`) live in the
existing NovaMarket SQLite file and are immutable by trigger, following the
PI-01/PI-02 store patterns. A feature-set id is a deterministic digest of the
request, accepted source bar ids and computed values; identical regenerations
are duplicate-detected. Lineage includes dataset version/membership,
definition digest, source first/last timestamps, generation time, bar counts,
duplicate/gap/partial/invalid counts, derived timeframes, context, fundamental
and news provenance, and the regime id. A failed generation is recorded as a
failed status, not a fabricated feature set.

Calculations are vectorized where practical (single forward passes for Wilder
indicators and OBV/VPT) and bounded to the accepted window; the engine keeps a
small LRU feature-set cache and supports incremental requests through
`max_bars`/`as_of`. Feature generation is separate from risk authorization and
paper execution. No distributed compute, new scheduler or new database is
introduced.

### 18.9 Known limitations

- VWAP is session-scoped only when intraday bars and volumes are validated;
  daily and higher bars report it unavailable.
- Free providers remain delayed and provider-limited; PI-04 does not upgrade
  their freshness or history depth.
- Fundamentals/news coverage depends on the existing services; missing data is
  explicit and never backfilled.
- Regime confidence is uncalibrated (`None`); labels describe the current
  environment, not a guaranteed future direction.
- Advanced feature selection and model-specific importance belong to later PI
  phases; PI-04 supplies diagnostics only.
- No HTTP route or frontend panel was added in PI-04.

## 19. Statistical Baseline Forecasting (PI-05)

PI-05 adds `src/novacontrol/novamarket/statistical/` as an additive baseline
layer. It consumes PI-01 contracts, PI-02 dataset references (or the same
validated market-data engine), PI-03 freshness/quality decisions and PI-04
feature/regime outputs. It owns no provider, dataset, feature, risk or execution
engine and creates no new database. Forecasts are the existing immutable
`prediction_forecasts` records; evaluation and training history use the existing
NM-06 `ForecastStore` tables; model metadata uses the existing
`prediction_models` registry.

### 19.1 Model families and definitions

| Canonical id | Family | Method |
|---|---|---|
| `naive_last` | naive | Latest completed close is the point forecast; no interval is claimed |
| `random_walk` | naive | Latest close is the point forecast; historical level changes provide a random-walk empirical interval |
| `historical_return` | naive | Mean historical log return applied to the latest close; empirical return interval |
| `seasonal_naive` | naive | Equivalent phase from the prior configured season (default 5 bars) |
| `sma` / `wma` / `ema` | moving average | Recent level is explicitly forecast as the next price; the average is a forecast method, not merely a technical indicator |
| `ses` | exponential smoothing | Level-only smoothing with configured alpha |
| `holt` | exponential smoothing | Level + trend, optional damped trend |
| `holt_winters` | exponential smoothing | Additive level/trend/seasonality; refused when seasonality is not distinguishable from noise |
| `arima` | arima | Bounded ARIMA(p,d,q) conditional least squares implemented in the standard library, with ADF-style stationarity diagnostics, AR companion stability check, Ljung-Box residual diagnostic and normal-residual interval |

Aliases such as `last_value`, `random_walk_baseline`, `historical_mean`,
`simple_moving_average` and `arima_p1_d1_q1` are normalised to those canonical
ids. Every fit records its config, observations, training start/end, residual
mean/std, stationarity report, fit status, artifact location
(`inline://deterministic-parameters`) and dependency version (`python-stdlib`).

### 19.2 Configurations and horizon compatibility

`ModelConfig` carries the window (2–500 bars), smoothing parameters, seasonal
period, ARIMA order (p,d,q each 0–3, p+q bounded), interval level, neutral band,
minimum observations and an opt-in `auto_differencing` flag. The service uses a
bounded candidate grid per model family. `arima` is always evaluated with the
stationarity/differencing evidence; it is never silently replaced by a
different model.

The ten PI-01 horizons are accepted:

| Horizon | Family | Finest accepted input |
|---|---|---|
| 5m, 15m, 20m, 1h | intraday | 1m, 5m, 5m, 15m |
| 1d, 3d, 5d | swing | daily |
| 30d, 90d | long-term | daily |

A daily bar is rejected for a five-minute or one-hour claim. PI-05 additionally
rejects intraday bars for the daily/session horizons: one hourly bar is not one
trading day of model steps, so daily horizons require daily observations rather
than silently reinterpreting the interval. The target time is
computed from the exchange calendar when the instrument has one: intraday
horizons step through actual sessions and daily horizons land on the correct
session close, not on naive calendar time. Without a verified calendar the
service records a conservative calendar fallback and never presents it as an
exchange session.

### 19.3 Forecast, direction and uncertainty semantics

Each result is a PI-01 `ForecastOutput` with instrument, exchange, reference
price/time, predicted price, expected return, target time, model id/version,
horizon, config digest, dataset version, feature-set version, interval bounds and
validity. Direction is one of `up`, `down`, `neutral` or `uncertain`; the neutral
band is configurable (default 0.5%). A model whose interval crosses zero returns
`uncertain` rather than a directional claim.

Probabilities and confidence are always withheld: `p_up/p_down/p_neutral` are
`None`, probability status is `unavailable`, calibration status is
`not_applicable`, and `confidence` is `None`. No arbitrary score is labelled a
probability. Point baselines (`naive_last`) explicitly report
`interval_supported=false`; the interval equals the point and uncertainty is
`unknown`. Empirical intervals are finite-sample residual/return intervals;
ARIMA intervals are normal-residual intervals. The response's
`uncertainty_detail` records interval level/bounds/method, residual standard
deviation and sample count, data-quality/freshness, coverage/suitability, and
limitations. Neither kind of interval includes parameter, model, event,
liquidity or regime-shift uncertainty, and neither is a guarantee that the
market price will fall inside it.

### 19.4 Evaluation and leakage controls

`validation.evaluate_models` makes one chronological split from the ordered
bars: an initial training prefix, a validation window and a final test window.
Walk-forward origins step by the horizon, so evaluation targets do not overlap.
For every candidate configuration the model is fitted only on `closes[:origin]`
and its outcome is the next target; the model/configuration is selected using
validation return MAE (with directional accuracy as a tie-break). Test values
are scored only after selection and are never fed back into it. No random
shuffling, future features, future fundamentals/news or test-set selection is
performed. Reports include exact dataset id/version, train/validation/test start
and end timestamps, bar counts, validation/test origin counts, model failure
reasons and the selected configuration.

Metrics are separated by instrument, horizon, model and (when PI-04 is attached)
regime: price MAE/RMSE/MAPE/sMAPE, mean error, bias, return MAE/RMSE, directional
accuracy, per-class precision/recall/F1, confusion matrix, bullish/bearish/neutral
accuracy, interval coverage, mean interval width and by-regime/by-horizon
breakdowns. Percentage errors are omitted where the denominator is zero or
negative. Regimes with fewer than five evaluation samples are reported as
insufficient rather than interpreted.

### 19.5 Persistence, API and failure isolation

`NovaMarketService.statistical_service()` opens PI-05 lazily over the existing
PI-01 prediction store and NM-06 forecast store. `generate` persists the
contract and registers model metadata (state `eligible`, calibration
`uncalibrated`, no automatic promotion); `evaluate` persists the training run
with its selected model/config and measured performance. PI-05 does not write
the shared `forecast_selection` slot, which remains NM-06-owned; a later
statistical `generate` reads the latest PI-05 training run and reuses its
model/config; an explicit model overrides the model, and an explicit config
replaces the selected configuration. Model
metadata includes dataset/feature versions, config digest, fit status, metrics
and artifact location. Reopening the same SQLite file retrieves the same
forecast and model metadata.

Existing forecast routes are reused, not duplicated: `POST /market/forecasts`
with `engine=statistical` creates a baseline; `GET /market/forecasts` with
`engine=statistical` lists them; `GET /market/forecasts/models` returns model
metadata plus suitability when instrument/horizon are supplied;
`POST /market/forecasts/train` and `POST /market/forecasts/evaluate` with
`engine=statistical` run the chronological comparison; `GET
/market/forecasts/performance` returns evaluations; calibration responds
`not_applicable` because no probability is claimed. The default calls remain
NM-06 probabilistic forecasts. Status/contracts are nested under
`status.statistical` and `contracts.statistical`.

A fit failure is recorded with its reason and an `unavailable` model record; it
does not crash another model or the service. Failed identity is never rewritten
as another model. At the application boundary, statistical
`StatisticalModelError`/validation/data errors map to HTTP 422 and unknown ids
to 404. No model output authorizes risk or paper execution: NM-08 and NM-10
remain sole authorities.

### 19.6 Performance and known limitations

Fitting is bounded to the latest 5,000 observations (the service keeps the
newest rows and rejects a larger input window) and is separate from forecast
inference. The service reuses a selected model/config and an in-process
bounded fit cache keyed by model, config, horizon steps and the exact bar
series, so an unchanged observation window does not refit; evaluation is the
explicit refit/compare operation. Configuration
grids and ARIMA orders are bounded; all work is in-process standard library code,
with no distributed serving. A bounded history still means longer horizons and
daily data cannot support intraday claims; missing/insufficient data produces
`unavailable`, not a fabricated forecast.

The ARIMA implementation is deliberately bounded rather than a full
statsmodels/SARIMA replacement: it supports non-seasonal ARIMA with a one-lag
ADF-style test, conditional least squares, residual diagnostics and
normal-residual intervals. It does not include SARIMA, exact maximum-likelihood
parameter covariance or parameter-uncertainty propagation. `statsmodels` was not
installed in the project environment, so adding it would have introduced an
unrequested dependency; the pure-stdlib implementation is documented here as the
actual artifact. It fails gracefully on insufficient/unstable fits.

No calibrated probability, no interval guarantee and no automatic model
promotion are claimed. The consolidated evidence report is
[PI05_COMPLETION_REPORT.md](PI05_COMPLETION_REPORT.md).

## 20. PI-06 — Gradient-boosting forecasting

**Status:** implemented, integrated, tested and evaluated. Consolidated report:
[PI06_COMPLETION_REPORT.md](PI06_COMPLETION_REPORT.md).

### 20.1 Architecture and public contracts

`novacontrol.novamarket.gradient` is an additive forecasting family over the
same contracts and stores as PI-05. It does not create a second dataset,
feature, prediction, persistence or execution system.

| Module | Responsibility |
|---|---|
| `models.py` | `GradientConfig`, `FeatureSchema`, `GradientForecast`, `FamilyResult`, `GradientEvaluationReport`, `GradientExplanation` and model/artifact metadata. |
| `training.py` | Chronological matrix construction, target/label generation, feature statistics, native booster fits, bounded candidate selection, metrics, regime slices, walk-forward folds and TreeSHAP contributions. |
| `artifacts.py` | Native XGBoost/LightGBM JSON save/load, checksums, dependency versions and feature-schema metadata. |
| `service.py` | Lazy orchestration: dataset/feature acquisition, training, evaluation, baseline comparison, persistence, inference, explanation and suitability. |

`NovaMarketService.gradient_service()` constructs the family lazily. Its
status and contract are nested under `status.gradient` and
`contracts.gradient`; the service never becomes the authoritative quote,
portfolio, risk or execution path. Forecasts use the existing PI-01
`PredictionStore.prediction_forecasts` record and the existing model registry.

Every forecast carries instrument/exchange, model id and version, horizon,
reference and predicted price, expected return, direction, optional range,
generation/target/observation timestamps, dataset and feature-set versions,
missing-feature fraction, out-of-distribution flag and validity. The range is
a validation-residual interval, not a calibrated probability. `p_up` and
`p_down` are null because the classifier probabilities have not been
calibrated.

### 20.2 Models, targets and horizons

XGBoost and LightGBM use their native Python boosters (`XGBRegressor` /
`XGBClassifier`, `LGBMRegressor` / `LGBMClassifier`). Each horizon gets an
independent model; no model is assumed to transfer across horizons. The
regressor predicts the forward simple return
`future_close / decision_close - 1`; price is derived at inference from the
reference close, and a price-scaling safeguard rejects material extrapolation.
The classifier predicts `down`, `neutral`, `up`.

Direction thresholds are configurable. The default is a 0.5% neutral band, or
the larger of that band, the configured round-trip cost (10 bps) and a
horizon-specific band. A direction is `uncertain` only when a supported
interval crosses zero. The band is recorded with every target and evaluation.

All PI-01 horizons are supported by the target generator: `5m`, `15m`, `20m`,
`1h`, `1d`, `3d`, `5d`, `30d` and `90d`. Support is conditional on the input
resolution and coverage. Intraday targets require bars at or below the
horizon's maximum input interval; a daily-only series cannot produce a 5m
claim. The service infers the series interval from the supplied bars and
refuses a horizon whose observed resolution is too coarse.

### 20.3 Features, preprocessing and leakage controls

The service reads the validated PI-04 feature set (`pi04-feature-set-1.0`) and
the server-computed feature snapshot at each decision timestamp. It uses
PI-04's OHLC/return, trend, momentum, volatility, volume/liquidity and
market-regime families. Availability is explicit: a feature missing for an
instrument is represented as missing, not as an actual zero. Feature order is
the persisted `FeatureSchema`; training and inference reject a schema mismatch
rather than reinterpreting columns.

Tree-native missing handling is used (`missing=np.nan` for XGBoost and
LightGBM). Numeric validation rejects non-finite/invalid targets. Rows above
the configured missing fraction are excluded; inference abstains above the
smaller inference threshold. Residual quantiles and direction bands are fitted
on the validation window only. Dataset splits are chronological with a purge
gap of the target window, so a training label cannot overlap validation or test
observations. Selection reads validation metrics only; the test block is scored
once by the selected configuration. The report records
`test_used_for_selection: false`.

PI-06 does not manufacture or backfill fundamentals/news data. The inference
API accepts optional rows only when the caller supplies validated PI-04
evidence, passes them to the feature engine, and leaves publication-time and
availability checks with PI-04. No row is imputed or borrowed from a later
period. The stored evidence used in this report did not include fundamentals or
news, so those features remain a coverage limitation rather than a claimed
capability.

### 20.4 Training and configuration

The default configurations are intentionally bounded: XGBoost uses depth 4,
160 estimators, learning rate 0.05, 0.8 subsample/column sample, `reg_lambda`
1.0, min child weight 2.0 and 20 early-stopping rounds; LightGBM uses 15
leaves, depth 4, 160 estimators, learning rate 0.05, 0.8 feature/bagging
fraction, min child samples 20, `reg_lambda` 1.0 and 20 early-stopping rounds.
Both use seed 42, two threads and no GPU. The candidate grid has three bounded
configurations (base, shallower/slower, deeper/more regularised); it is a
selection grid, not an unrestricted search. Validation MAE selects the
candidate, then the selected configuration is refit on train + validation.
Inference abstains when the validation-residual interval is wider than the
configurable `max_interval_width` (50% by default); this is an uncertainty
guard, not an accuracy threshold.

Training is recorded with dataset version, feature-set version, target
definition, horizon, configuration digest, split ranges, sample counts,
selected candidate, metrics, dependency versions, random seed, artifact paths
and checksums. Re-running the same stored data and configuration produces the
same version digest and comparable metrics; it does not retrain on every quote.

Walk-forward evaluation refits the already-selected configuration on expanding
windows. The held-out tail is split into three contiguous blocks; each fold
trains only on observations before its test block and uses the last part of
that past for early stopping/interval calibration. Rows whose target window
extends into the fold's test block are purged, so a multi-day label cannot leak
across the fold boundary. Earlier test blocks become available to later folds,
which is the defining property of walk-forward validation, not a random split.

### 20.5 Evaluation, selection and uncertainty

Evaluation reports MAE, RMSE, MAPE, sMAPE, forecast bias, return and price
errors, directional accuracy, precision/recall/F1, confusion matrices, bullish
and bearish accuracy, neutral-class performance, interval coverage/width and
walk-forward variation. Regime slices are reported only above the minimum
sample count; smaller slices are marked insufficient rather than given a
misleading score.

The same test origins are scored by PI-05's last-value/random-walk, SMA, SES and
ARIMA baselines. The per-family suitability result is one of
`gradient_suitable`, `baselines_preferred`, `provisional` or
`insufficient_data`; the report's overall preference is the family name or
`statistical_baseline`. Candidate and family selection use validation price
MAE, with directional accuracy only as a tie-break. Suitability requires the
test price MAE to be strictly below the price-winning baseline, direction not
below that same baseline on the same origins, and interval coverage not below
the nominal level's binomial lower bound. There is no universal accuracy cut-off. It never
promotes a model, and a lower training loss cannot select a model. A failed
family keeps its failure identity and status; it is never relabelled as a
trained or selected model.

Uncertainty is the validation-residual 80% interval. It is explicitly labelled
`calibrated_probability: false` and `guarantee: false`. Forecast validity may
be `valid`, `stale`, `out_of_distribution`, `missing_features`,
`unsupported_horizon` or `unavailable`; the service abstains with the actual
reason. Feature importance is never described as confidence or probability.

### 20.6 Explainability

Global importance uses native gain (the model's own split/gain statistic) and
is persisted with the model. Local explanations are computed on demand with
XGBoost `pred_contribs=True` and LightGBM `pred_contrib=True`, which expose
TreeSHAP-style contributions for the exact feature vector. The explanation
records model id/version, instrument, horizon, methodology, feature values,
per-feature contributions, base value, prediction and the limitation that a
local attribution is not a causal claim or a confidence score. It is not
computed for every forecast; the UI requests it only when the user asks.

### 20.7 API, UI and persistence

The existing forecast routes accept `engine=gradient` (and the family aliases
`xgboost`/`lightgbm`): generate, list, get, models, performance, train,
evaluate and calibration. One route is added for on-demand explanation:
`GET /market/forecasts/{forecast_id}/explain`. Model metadata, training runs,
forecasts and artifacts all use the existing prediction/forecast stores plus
native files under the store's data directory
(`data/novamarket/gradient_models/<instrument>/<version>/` in the application).
The version digest includes instrument, horizon, target-step count, dataset and
feature-set versions, training range and config digest; lookup is scoped to the
requested instrument and rejects a mismatched target-step count.

The Forecasts UI (`web/src/pages/ForecastModels.tsx`, re-exported by
`Intelligence.tsx`) distinguishes statistical, XGBoost and LightGBM engines;
shows model status, selection outcome, test metrics, uncertainty and validity;
offers bounded training and on-demand explanation. It does not present an
experimental model as production-ready and does not add approval/execution
controls.

### 20.8 Performance and failure handling

The fit workload is bounded to the latest 5,000 samples, with two threads and a
bounded candidate grid. Inference is a single booster prediction per family;
training and evaluation are explicit calls, never a side effect of a quote
refresh. Artifacts are native JSON (roughly 100–370 KB per booster in the
recorded evidence). There is no GPU requirement.

Missing dependencies produce an unavailable family with an explicit reason.
Insufficient samples, invalid targets, schema mismatch, booster failure,
serialization/load failure and unsupported horizons are isolated per family or
per horizon; one failure does not abort the other family or the prediction
subsystem. A failed model's forecast identity is never replaced by another
family's output. No gradient model receives risk or execution authority.

### 20.9 Known limitations

- Real evidence on the current NovaMarket database is limited to RELIANCE daily
  bars and one ADANIENT 1-minute session. The baseline comparison is therefore
  small and not statistically conclusive.
- No evaluated horizon beat the best PI-05 baseline on the test block. The
  overall preference is `statistical_baseline` for all evaluated
  instrument/horizon pairs, and every trained family is `provisional` because
  its residual-interval coverage was below the nominal level's binomial lower
  bound. This is the honest result, not a UI defect.
- 90d (RELIANCE) and 1h (ADANIENT) were refused for insufficient usable rows.
- Intraday direction accuracy of 1.0 is an artifact of one short, monotonic
  session with a neutral-heavy classifier; it is not evidence of skill.
- Classifier probabilities are not calibrated and are withheld; uncertainty is
  a validation-residual interval, not a guaranteed coverage band.
- Fundamentals/news rows were not present in the stored evidence. The API can
  pass caller-supplied validated rows through PI-04, but that path is not
  exercised here and adds no claimed skill. Regime-specific models are not
  trained; regimes are reported as performance slices only where samples
  suffice.
- Walk-forward folds show material variation and, on the small evidence set, do
  not establish stable superiority over statistical baselines.
- Ensemble-disagreement abstention is not implemented: each request serves one
  independent family, so there is no ensemble vote to compare. No synthetic
  disagreement score is emitted.

## 21. PI-07 — LSTM deep-learning forecasting

**Status:** implemented, integrated, tested and evaluated. Consolidated report:
[PI07_COMPLETION_REPORT.md](PI07_COMPLETION_REPORT.md).

### 21.1 Architecture and reuse

`novacontrol.novamarket.lstm` is an additive sequence-model family over the
same PI-01 contracts, PI-02 training datasets, PI-03 freshness rules, PI-04
features, PI-05 baselines and PI-06 comparison infrastructure. It is not a
second dataset, feature, prediction, model-registry or execution system.

| Module | Responsibility |
|---|---|
| `models.py` | `LSTMConfig`, `SequenceScaler`, `LSTMArtifact`, `LSTMResult`, `LSTMEvaluationReport`, `LSTMForecast`, `LSTMLoaded` and version derivation. |
| `sequence.py` | Chronological window construction from PI-06's `GradientMatrix`, training-only robust scaling, explicit missing masks and inference-window construction through PI-04. |
| `network.py` | Lazily imported PyTorch CPU LSTM: shared encoder, LayerNorm, return-regression head and three-class direction head. |
| `training.py` | Deterministic bounded candidate fits, early stopping with best-state restoration, Huber/MSE regression loss, weighted classification loss, validation-residual intervals, metrics, walk-forward folds and comparisons. |
| `artifacts.py` | Atomic native `state_dict` persistence with SHA-256, byte-size verification, compatibility checks and failed-load isolation. |
| `service.py` | Lazy orchestration: data/feature acquisition, training, evaluation, baseline and PI-06 comparison, persistence, inference, explanation and status/contract. |

`NovaMarketService.lstm_service()` constructs the family lazily. Status and
contract are nested under `status.lstm` / `contracts.lstm`. Model metadata,
preprocessing metadata, feature schema, training provenance, metrics and
artifact checksums use the existing `PredictionStore.prediction_models`;
evaluation evidence uses the existing `ForecastStore.forecast_training_runs`;
forecasts use `prediction_forecasts`. Native PyTorch state dictionaries live
under the application data directory (`data/novamarket/lstm_models`).

### 21.2 Sequence preparation and feature learning

A window contains `sequence_length` consecutive observations ending at the
decision timestamp; the target is the forward return
`future_close / decision_close - 1` for that row. Windows are built separately
with target rows inside the train, validation and test index blocks produced by
PI-02's chronological split. A window's historical input may precede its split
boundary, but its target row stays inside that split and no observation at or
after the target time enters the input. Training rejects
non-monotonic timestamps, duplicate decision times, schema mismatch, a target
before its decision, and insufficient post-window rows rather than padding.

The input is the PI-04 feature vector already used by PI-06 (price/return,
trend, momentum, volatility, volume/liquidity, market-regime and optional
point-in-time fundamental/news context). Sequence tensors carry scaled values and an explicit per-feature missing
mask, fed to the network as two concatenated input channels. Missing is never
encoded as a real zero: the transform imputes the training median for the value
channel, the mask channel stays zero, and inference reports the missing
fraction. An observed zero therefore remains distinct from a missing value. Feature order
is the persisted `FeatureSchema`; inference refuses a different schema.

At inference, the whole supplied series is filtered to observations at or
before `as_of` before the feature window and reference close are selected, so
a caller-supplied page containing later rows cannot leak into the forecast.

The default sequence profiles are horizon-specific: 30 observations for 1m
intraday, 20 for coarser intraday, 30 for 1d/3d/5d, and 15 for 30d/90d.
`sequence_length` can override all profiles. A user request still has to pass
the data-resolution and usable-row checks; a daily series cannot establish a
5-minute claim.

### 21.3 Models, targets and configurable training

The network is deliberately small: one LSTM layer with hidden size 32 by
default, dropout 0.10, LayerNorm, a regression head and a three-class
(`down`, `neutral`, `up`) head. The bounded candidate set is base,
shallower/slower and deeper/more regularised; validation price MAE selects one,
then that candidate's best validation checkpoint is retained; it is not refit
on validation, so validation loss and interval calibration stay independent of
the final weights. Test data is scored once and never selects a candidate or
threshold.

Configurable controls are sequence length, hidden size, layers (1–3), dropout,
learning rate, batch size, `adam`/`adamw`, Huber/MSE loss, epoch ceiling,
patience, weight decay, gradient clipping and classification-loss weight.
Defaults are CPU-first: 40 epochs, patience 6, batch 32, seed 42 and two
threads. PyTorch is optional (`pip install -e ".[lstm]"`); the rest of
NovaMarket loads without it.

Targets are the forward simple return and the direction label. The neutral
band is the larger of the configured 0.5% band and the 10 bps round-trip cost;
targets and bands are recorded with the run. Class weights are derived from the
training labels only. Regression and classification losses are bounded and
combined; a classifier probability is never emitted as a calibrated
probability.

### 21.4 Preprocessing, leakage and uncertainty

`SequenceScaler` is fitted only on training sequences: robust median/IQR for
features, median/IQR for the target, with scales floored and a target scale
derived from mean absolute training target when IQR is zero. Validation, test
and inference reuse the fitted transform. Targets are inverse-transformed
before price reconstruction and metrics. The scaler and feature schema are
persisted with the model and loaded together; incompatible metadata is a load
failure, not a silent reinterpretation.

Uncertainty is the validation-residual 80% interval, stored with its nominal
level and marked `calibrated_probability: false` and `guarantee: false`.
Inference abstains when the interval is wider than the configured 50% bound,
when the feature vector is too incomplete, when the input is outside the
training distribution by the bounded z-score rule, or when the model/version
cannot be loaded. Direction becomes `uncertain` when the interval crosses zero;
probabilities stay unavailable.

### 21.5 Training, evaluation and comparison

Every run records dataset id/version, feature-set version, target definition,
horizon, resolution, sequence length, architecture/hyperparameters, split
ranges and sample counts, training date range, selected candidate, validation
and test metrics, walk-forward fold evidence, dependency versions, seed,
duration, memory peak, epoch/best-epoch/early-stop state, artifact path,
checksum and version. Early stopping selects the best validation epoch and
restores those weights; a failed fit leaves prior artifacts and models intact.

Metrics include MAE, RMSE, MAPE, sMAPE, bias, price/return errors, directional
accuracy, precision/recall/F1, confusion matrices and per-class direction
accuracy, interval coverage/width, walk-forward variation, latency and memory.
Regime slices appear only above the minimum sample count. PI-05 baselines are
scored on exactly the same test origins. PI-06 XGBoost and LightGBM are trained
on the exact same `GradientMatrix` for comparison but are intentionally not
registered as new PI-06 models.

Suitability is `lstm_suitable`, `baselines_preferred`, `provisional` or
`insufficient_data`. It requires test price MAE strictly below the best
baseline, direction not below that baseline and interval coverage not below the
nominal level's binomial lower bound. There is no arbitrary accuracy target,
no test tuning, no automatic promotion and no financial authority. A simpler
model remains preferable when it is better, more stable or more
resource-efficient.

### 21.6 API, UI and persistence

The existing `/market/forecasts*` routes accept `engine=lstm` (alias:
`sequence`) for generate, list, get, models, performance, train, evaluate and
explain. No new prediction route or dashboard was added.
`GET /market/forecasts/{forecast_id}/explain` serves the LSTM branch: global
validation-occlusion importance and a recorded limitation that it is neither a
local Shapley claim nor a confidence score.

The React Forecasts page (`web/src/pages/ForecastModels.tsx`, re-exported by
`Intelligence.tsx`) has an explicit `LSTM sequence models` engine. It shows the
experimental/provisional badge, PyTorch backend availability, sequence length,
model version, dataset/feature versions, predicted price/return/direction,
target time, residual interval, validity/abstention reasons and evaluation
history. It does not merge LSTM output with another family and does not present
it as guaranteed market behaviour.

### 21.7 Hardware, failure isolation and limitations

The supported runtime is CPU-only PyTorch (the recorded environment is
2.14.0+cpu, CUDA false, 14 host threads; training uses two). There is no GPU
requirement and no automatic accelerator selection. Training is bounded to
5,000 samples, three candidates, 40 epochs and a small network; ordinary
inference is separate and does not retrain. Recorded training took 1.2–6.9 s
per successful case, inference 0.04–0.12 ms, and peak process memory 9–95 MB.
These are measurements on the development machine, not universal guarantees.

Insufficient sequence length, missing observations, invalid features, target
alignment, NaN/infinite values, divergence, overfitting, memory exhaustion,
unsupported acceleration, checkpoint/artifact/load errors, incompatible
preprocessing metadata and unsupported horizons are explicit failures. One
failed LSTM leaves statistical and gradient families operational; no other
model's output is relabelled as LSTM.

The recorded NovaMarket database contains only RELIANCE daily bars and one
ADANIENT 1-minute session. Eight of ten LSTM cases trained; RELIANCE 90d and
ADANIENT 1h were refused because only 24 and 25 training rows remained after
the target-window purge, below the required 30 and with no usable validation
block. Every trained LSTM was `provisional`; none reached `lstm_suitable`.
The 30d case had a lower test price MAE than the best PI-05 baseline
(65.68 vs 72.54) but its 5.6% interval coverage invalidated a suitability
claim; seven other cases lost to the best baseline. LSTM beat both PI-06
boosters on four of eight measured cases, but no case was eligible for
preference, so this evidence does not establish an LSTM advantage. Interval
coverage (5.6%–52.6%) was below the nominal 80% binomial lower bound, and
intraday directional accuracy of 1.0 is a neutral-heavy single-session
artifact, not skill. Classifier probabilities
remain withheld; fundamentals/news rows were absent; regime models were not
trained. The full evidence is in the PI-07 completion report.

## 22. PI-08 — Ensemble forecasting and uncertainty calibration

PI-08 adds `novacontrol/novamarket/ensemble/` as an additive composition layer.
It does not create a second prediction contract, model registry, forecast store,
feature pipeline or HTTP route family.

### 22.1 Assembly path

`EnsembleForecastingService.generate()` performs one bounded request:

1. Resolve the PI-01 horizon and validate resolution compatibility.
2. Read PI-01 `ModelMetadata` for the instrument and horizon, excluding models
   with no usable validation/walk-forward evidence, unsupported horizons,
   unavailable/suspended/retired states, `baselines_preferred` and other
   unsuitable evaluation statuses, unreadable artifacts/scalers and future
   training timestamps for persistent gradient/LSTM artifacts.
3. Produce a reproducible eligibility and weight decision for every candidate.
4. Select the evidence-ranked contributors within the configured maximum and
   call each family's existing service exactly once.
5. Validate each returned PI-01 contract for instrument, exchange, currency,
   reference price, feature-set/dataset version, target time and point-in-time
   data currency; future-observed, missing, provider-unavailable and genuinely
   stale bars are rejected. Isolate a failed constituent instead of aborting
   the request.
6. Recompute weights over the contributions that survived validation.
7. Build `ModelPrediction` objects and call the existing PI-01
   `EnsembleAssembler`, which performs price/return aggregation, disagreement
   thresholds, direction-probability eligibility and abstention.
8. Apply or withhold validation-only conformal calibration, persist the exact
   config, forecast and evaluation, and expose the existing service/API/UI
   surfaces.

The assembler is the only aggregation authority; the Python service does not
reimplement its arithmetic. Statistical baselines are refitted by PI-05 on the
supplied point-in-time bars. Persistent gradient/LSTM artifacts are excluded
when their `trained_at` is after the cutoff. Historical bars whose stored
ingestion freshness is stale are accepted only when the bar is current for the
cutoff under the horizon's maximum input interval; an actually old bar remains
ineligible. This is the point-in-time rule pinned by
`test_stale_bar_is_accepted_only_when_it_is_current_for_the_cutoff`.

### 22.2 Eligibility and weighting

`weighting.py` records one `EligibilityDecision` per candidate with an explicit
reason. Weights are non-negative, finite, normalized, bounded by
`min_weight`/`max_weight`, smoothed against the previous registered config and
derived only from validation or walk-forward evidence:

- inverse relative MAE against the eligible model median;
- a direction-accuracy factor when validation accuracy exists;
- per-regime validation error and direction accuracy only when the requested
  regime has at least `min_regime_samples` observations; otherwise the ordinary
  validation/walk-forward evidence is used;
- a recency decay with a configurable half-life;
- a degraded-state penalty;
- prior-config smoothing and bounded-simplex projection.

Test-set results are not used to choose weights. The config digest and the full
decision list are persisted, so a historical forecast can be reproduced from
the exact model versions, weights and eligibility reasons.

### 22.3 Calibration, intervals and disagreement

`calibration.py` fits split-conformal residual quantiles on **validation**
observations only. The test report is scored after fitting and is never fed back
into the quantile or weights. The 80% interval is marked `provisional` until
the configured minimum sample count is met, and carries `guarantee=False`.
Interval coverage, mean width and per-regime coverage are evaluated separately.
If no registered calibration exists, the assembler's weighted constituent
interval is labelled `weighted_constituent_validation_intervals` rather than
presented as conformal.

`fit_calibration` refuses any observation whose `validation` flag is false, so
even a direct caller cannot fit on test rows or a mixed block.

A calibration fitted for a different nominal level is not applied; the result
keeps the constituent interval and records the registered level. A calibration
or previous weight config created after the forecast cutoff is likewise
ineligible.

`_disagreement_detail()` reports weighted return dispersion, a direction vote
distribution, directional disagreement and the methodology string. The PI-01
assembler applies the configured warning/abstention thresholds; disagreement is
evidence of uncertainty, not a reversal signal.

Direction probabilities are only carried when every participating contributor
has calibrated probabilities. Otherwise `p_up`, `p_down` and `p_neutral` are
withheld (`probability_status=unavailable`); raw regression outputs are never
converted into probabilities.

### 22.4 Abstention, fallback and failures

No eligible model produces an honest `abstained` result. Fewer than the required
models after validation also abstains. A single surviving eligible model is a
transparent `fallback` with the original model id/version, `fallback=True`, a
reason, and an `ensfb_` identifier; it is never labelled as an ensemble. A
constituent failure is retained in the result's failure list while other
eligible models continue. Persistence, calibration and interval failures are
recorded without turning an abstention into a buy/sell signal.

### 22.5 Persistence, API and UI

PI-08 adds three tables through the existing `PredictionStore`:
`prediction_ensemble_configs`, `prediction_ensemble_calibrations` and
`prediction_ensemble_evaluations`. Runs use the existing `ForecastStore` with
`kind="ensemble"`. Configuration rows retain the ensemble version, constituent
ids/versions, methodology, weights, eligibility, calibration method/dataset,
train/validation periods, horizon, instrument scope, regime scope, creation
time and status; calibration rows retain the fitted-on boundary and quantile.
Old rows are append-only, so a new config cannot rewrite an earlier forecast.
The requested regime is recorded on the config row, ensemble result, forecast
metadata and snapshot provenance; a caller-supplied regime is not inferred
from future data.

The existing `/market/forecasts*` routes accept `engine=ensemble` for generate,
list, get, models, performance, calibration, evaluate and explain. The React
Forecasts page exposes the Ensemble option and shows the main forecast,
participating model contributions and weights, disagreement, calibration status,
abstention/fallback reasons and the explain view. It deliberately does not show
a decorative confidence score.

### 22.6 Recorded evidence and limitations

The reproducible harness is `tmp/pi08/run_real_evidence.py`; its output is
`tmp/pi08/evidence.json`. It uses 256 real RELIANCE daily bars
(2025-10-29 to 2026-11-11), trains on an early prefix, fits calibration on
validation origins and scores the later test origins. Synthetic fixtures in
`tests/test_novamarket_ensemble.py` are test evidence, not market performance.

| Horizon | Train bars | Validation/test origins | Ensemble test price MAE | Test directional accuracy | Test interval coverage (80%) |
|---|---:|---:|---:|---:|---:|
| 1d | 179 | 10/20 | 14.73 | 0.05 | 0.80 |
| 5d | 179 | 10/20 | 36.15 | 0.25 | 0.60 |
| 30d | 204 | 10/20 | 94.05 | 0.00 | 0.00 |

The 1d ensemble included LSTM plus statistical baselines; the 5d ensemble
included LightGBM plus statistical baselines. PI-06 labeled both boosters
`baselines_preferred` in this dataset, so they were excluded where that status
applied. The 30d run could not train fresh boosters (4 validation rows) or the
statistical family (204 bars versus the 240-bar requirement); it used the
existing registered statistical artifacts and is therefore a weaker evidence
case. A 90d ensemble was not scored because the available daily history cannot
provide a leakage-safe 90-bar target window plus the required validation/test
origins; this is an explicit data limitation, not a fabricated result. The
ensemble did **not** beat the best individual model on
the tested 1d/5d/30d cases (for example 1d LightGBM MAE 12.27 versus ensemble
14.73), and interval coverage for 5d/30d was below the nominal 80%. These
results are recorded without claiming an ensemble advantage.

## 23. PI-09 — Durable forecast lifecycle scheduling

PI-09 adds `novacontrol/novamarket/scheduling/` as an additive lifecycle
coordinator. It does not add a market-data provider, model, forecast database,
risk engine or execution path. The existing PI-03 `MarketDataGateway`, PI-08
`EnsembleForecastingService`, PI-01 `PredictionIntelligenceService`/`PredictionStore`
and PI-04 exchange calendars are the only authorities it composes.

### 23.1 Cycle identity and immutable versions

One target has one stable cycle id derived from instrument, exchange, horizon
and the UTC target time. The cycle id is also the PI-01 lifecycle id. The
initial T−20, first T−5 and final T−1 records are separately persisted
`ForecastOutput` versions with deterministic version ids; the SQLite
immutability triggers and `UNIQUE(lifecycle_id, stage)` constraint prevent a
later confirmation from replacing an earlier version. PI-09 adds only the
cycle, checkpoint and outcome tables to the existing prediction store. It does
not create a second forecast store.

### 23.2 Scheduling policy

The policy is explicit per horizon in `HORIZON_SCHEDULE_POLICIES`:

| Horizon family | Target | Initial | Confirmation | Final |
|---|---|---|---|---|
| 5m | session-aware duration | T−4m | T−2m | T−1m |
| 15m | session-aware duration | T−12m | T−5m | T−1m |
| 20m | session-aware duration | T−20m | T−5m | T−1m |
| 1h | session-aware duration | T−20m | T−5m | T−1m |
| 1d | next verified session close | cycle creation | T−60m | T−1m |
| 3d | 3 verified sessions ahead | cycle creation | T−1d | T−1m |
| 5d | 5 verified sessions ahead | cycle creation | T−2d | T−1m |
| 30d | 30 calendar days, first verified close | cycle creation | T−7d | T−2d |
| 90d | 90 calendar days, first verified close | cycle creation | T−14d | T−3d |

The short intraday leads are deliberately compressed: a five-minute forecast
cannot have a meaningful T−20 checkpoint. Swing and long horizons no longer
wait for the target session open to create their first version, and do not reuse
the intraday T−60m/T−1m confirmation cadence for a month or quarter. The
20-minute lifecycle remains the required intraday case.

Target calculation uses `calendar_by_mic`, `is_trading_day` and
`session_bounds`; it never assumes a weekday is a trading day. A missing
calendar, unknown timezone, uncovered year, holiday/weekend target or target
outside the verified session raises `ScheduleValidationError` before a cycle is
created. Calendar coverage is 2026 only, so a target in an uncovered year fails
closed. PI-10 validates the expected target independently for each horizon and
marks only the uncovered horizon failed; it never asks a model to forecast
against an unverified target.

### 23.3 Checkpoints, leases and recovery

`prediction_forecast_checkpoints` stores one stable row per cycle/kind. A
checkpoint is claimed with a conditional SQL `UPDATE`; a second worker receives
`None`, and an expired lease is returned to the due queue by
`recover_expired_checkpoint_leases`. Execution is idempotent: an existing
immutable version is reused instead of creating a duplicate, and the cycle and
checkpoint ids never change across retries.

The scheduler persists the planned `scheduled_at`, the real `executed_at`, a
late flag, attempt count, next retry time, terminal/dead-letter state and the
reasons. A missed T−20 is executed only while the target is still in the
future and is recorded with its real execution timestamp; it is never labelled
on time. A checkpoint whose target has already passed is skipped, not executed
as a fresh confirmation. Evaluation observations that are unavailable or too
late remain `evaluation_pending` and retry on a bounded backoff; later valid
observations record the actual observation timestamp and delay. A final
confirmation that exhausts its retries becomes a dead letter and marks the
cycle/lifecycle `unconfirmed`; a late final failure cannot overwrite an already
completed evaluation.

The service also performs bounded automatic planning from the scheduler tick
loop and the existing runtime tick. It prioritizes held instruments, then
watchlist members, then the registered universe; deduplicates uppercase
`MIC:SYMBOL` identities; skips a live pending cycle; and caps new cycles at
`auto_schedule_limit` (20). Planning creates default 20-minute cycles only, makes
no provider call, and is throttled to at most once per 60 seconds. Per-
instrument failures are reported in `auto_plan.errors` and do not stop the
remaining instruments.

### 23.4 Data and forecast evidence

Forecast checkpoints call `MarketDataGateway.checkpoint(..., accepted_freshness=(FRESH,))`.
The scheduler additionally requires a usable observation, a fresh
source-timestamp classification, the expected instrument and a newer source
timestamp than the previous checkpoint. Provider errors, stale observations
and missing observations produce a skipped/unconfirmed checkpoint; no stale
price is reused as a fresh confirmation.

The PI-08 ensemble is called only through its public `generate()` surface. An
ensemble abstention is stored as an abstained checkpoint with reasons and no
forced direction. Each preserved version records the ensemble id, member
versions, feature-set/dataset versions, interval, uncertainty, data
observation timestamp and the checkpoint's actual execution timestamp. The
native PI-08 target is retained in metadata; the lifecycle target remains the
canonical comparison target. This makes a target mismatch visible rather than
silently relabeling it.

### 23.5 Revision and outcome handling

`ForecastRevision` compares a confirmation with the immediately preceding
preserved version: price and return revisions, direction change, probability
changes, interval and uncertainty changes, and an explicit classification
(`stable`, `strengthened`, `weakened`, `direction_changed`, `uncertain`,
`invalidated`). Classification is revision evidence, never accuracy.

At target time PI-09 validates the actual observation, requires its source
timestamp to be inside the verified target exchange session, rejects future
observations and delay beyond the reconciliation window, calls the existing
`PredictionIntelligenceService.evaluate()` so every preserved version is
evaluated independently, and stores PI-09 `ForecastOutcome` rows with actual and
predicted price/return, absolute and percentage price error, direction result,
interval containment/width, observation delay and model/ensemble provenance.
Stored outcome decimal fields are normalized on read, so crash/recovery reads
and the API remain numeric rather than returning serialized strings.
Accuracy views report per-stage sample counts, directional accuracy, MAE,
return MAE, interval coverage, abstention frequency, missing-evaluation
frequency and confirmation success rate. A sample below the configured
threshold is labelled unreliable; forecast accuracy is never combined with
paper-trading P&L.

### 23.6 API, UI and non-goals

The existing `/market/forecasts*` family now includes cycle creation/listing,
cycle detail, scheduler health, a bounded due-checkpoint tick, missed
checkpoints, pending evaluations and accuracy history. Existing forecast detail
and outcome routes also resolve PI-09 `pfc_` versions from the PI-01 lifecycle
store; unknown cycles return 404 and scheduling/lifecycle validation failures
return 422. All routes remain authenticated; there is no route that accepts an
order, portfolio mutation, ledger mutation or fill. The React Forecasts page
adds a lifecycle card with the checkpoint timeline, preserved versions,
revisions and target outcomes, using the canonical public forecast keys.

The scheduler exposes forecast evidence only. NM-08 remains the risk authority
and NM-10 the paper-execution authority. Monitoring/autonomous mode semantics
are unchanged; a T−20, T−5 or T−1 forecast does not authorize an order. The
scheduler status reports `orders_created`, `portfolio_mutations`,
`ledger_mutations` and `fills_created` as false.

### 23.7 Known limitations

- Calendar coverage is the existing verified 2026 NSE/BSE/US/crypto set. A
  target outside that coverage is refused rather than approximated.
- PI-08's constituent models do not accept an arbitrary target override. The
  scheduler therefore records the native ensemble target and normalizes the
  version to the cycle target; this is visible in metadata and is not a claim
  that a different-horizon model output is exact.
- Provider freshness is limited by the installed free/delayed providers. A
  checkpoint that cannot receive a fresh observation is honestly skipped or
  left pending; no live-market guarantee is made.
- Automatic planning covers the default 20-minute horizon. Other horizons are
  schedulable through the explicit cycle API but are not auto-planned.
- The React lifecycle card was built and type-checked but was not visually
  exercised in a browser during this verification pass.

## 24. PI-10 — Swing and long-term forecasting

PI-10 adds `src/novacontrol/novamarket/outlook/` as an additive orchestration and
context layer. Every numeric forecast is still produced by the PI-08
`EnsembleForecastingService` from PI-05/PI-06/PI-07 constituents, persisted by
the existing PI-01 `PredictionStore`, and evaluated by the existing PI-09
outcome tables. PI-10 adds no model registry, forecast store, feature engine,
fundamental ingestion system, risk engine or execution engine.

### 24.1 Horizon profiles and target semantics

`HORIZON_OUTLOOK_PROFILES` is versioned as `pi10-horizon-profiles-1.0` and has
exactly five entries:

| Horizon | Family | Target basis | Count | Feature profile |
|---|---|---|---|---|
| 1d | swing | trading sessions | 1 | swing |
| 3d | swing | trading sessions | 3 | swing |
| 5d | swing | trading sessions | 5 | swing |
| 30d | long-term | calendar days | 30 | long_term |
| 90d | long-term | calendar days | 90 | long_term |

Each profile also reports the real PI-04 feature-set version
(`pi04-feature-set-1.0`); no profile-specific version is fabricated. The
profile's declared feature family is the one PI-06/PI-07 already select from the
horizon and resolution. PI-06/PI-07 training matrices pass the instrument's
verified calendar into PI-02 label construction; without one, the builder keeps
its historical row-count fallback for direct callers.

`GET /market/forecasts/horizons/status` exposes the profiles, target rules,
confirmation policy, supported model families and the unchanged authority
declaration. `POST /market/forecasts/horizons` validates every requested horizon
before opening the forecast stack and isolates failures per horizon. A missing
target session, unsupported horizon or failed constituent cannot disable the
other four horizons.

### 24.2 Point-in-time fundamentals and corporate events

PI-10 accepts supplied PI-01 `FundamentalSnapshot` records or validated mapping
equivalents. A mapping must carry a source/publication timestamp and an
acquisition/availability timestamp; missing timestamps are rejected rather than
filled. Records after the forecast cutoff are excluded, and only the latest
received snapshot for each fiscal period is eligible after restatements. Metric
values, currency and units/provenance are retained. Missing fundamentals remain
missing and do not invalidate an otherwise eligible model.

News and corporate-event rows are accepted from PI-01 `NewsEventEvidence` or the
existing NM-04 radar output. Every row preserves event id, instruments, category,
publication time, acquisition time, source, relevance/verification metadata and
checksum. Records after the cutoff are dropped, repeated checksums/headlines are
deduplicated, and only explicitly confirmed/verified evidence reaches model
inputs. The canonical NM-04 `officially_confirmed` and
`multi_source_corroborated` states are accepted; `single_source`, `unverified`,
`conflicting_evidence`, `retracted`, `expired` and quality-only records remain
informational. A retracted or expired lifecycle status blocks a row even when
its historical verification state was confirmed. Rumours remain visible with
`model_input=false`. A valid warning record without an explicit confirmation is
not treated as a deterministic signal.

### 24.3 Sector, benchmark and peer context

Mappings come only from validated instrument metadata:
`sector_instrument_id`/`sector_index`/`sector_benchmark` and
`benchmark_instrument_id`/`benchmark_index`/`benchmark`. Free-text sector labels
are not converted into invented index symbols. A mapping must be a canonical
`MIC:SYMBOL` for a registered instrument. Daily bars are read through the
existing market-data engine at or before the cutoff, with partial and unusable
bars excluded. The context reports instrument/sector/benchmark 1- and 20-bar
returns, volatility, moving average, trend, relative performance and a bounded
20-peer breadth set. An absent mapping, absent history or invalid mapping is
recorded in `notes`; no default benchmark or sector is fabricated. A mapping
that is present but malformed or names an unregistered instrument is excluded
and reported, never used as a substitute index.

The same validated benchmark, sector and peer bars are passed into PI-04's
existing context features through PI-08 → PI-06/PI-07 inference and optional
training matrices. Horizon-specific feature profiles remain the PI-04 swing and
long-term profiles; PI-10 does not duplicate an indicator formula.

### 24.4 Uncertainty, disagreement and cross-horizon analysis

The existing PI-08 interval, uncertainty, calibration and constituent
disagreement fields are preserved. PI-10 does not recompute an interval or
invent a probability. Cross-horizon comparison sorts every available horizon,
reports its direction and expected return, counts directional agreement, and
distinguishes aligned, mixed, divergent and insufficient evidence. A generated
forecast whose target does not equal the independently verified expected target
is failed for that horizon and excluded from the comparison; it is never treated
as if it shared the same target. Short-term versus long-term divergence is
reported as multi-timeframe evidence, not an error. A missing horizon remains
missing.

### 24.5 Evaluation and monitoring

Outcome evaluation remains PI-09's target-time observation and immutable
`ForecastOutcome` record. PI-10's
`GET /market/forecasts/horizons/performance` groups only by the requested
horizon and stage, and computes price MAE/RMSE/bias/percentage MAE, return
MAE/bias, directional accuracy, per-class precision/recall/F1, neutral accuracy,
interval coverage and width. It deliberately returns
`combined_accuracy=null`: unrelated horizons are never merged into one accuracy
number. Reliability uses the service's configured `small_sample_size`, not a
hard-coded table. Small samples are labelled as such. Forecast accuracy remains
separate from portfolio P&L.

### 24.6 API, UI and autonomy boundary

The five additive routes live under `/market/forecasts/horizons*`, reuse the
existing authentication and forecast stores, and are listed in `ApiSurface`,
`ROUTE_CONSUMERS` and `docs/API.md`. The React Forecasts page adds a swing/long
card for generating all five horizons, loading stored forecasts, showing
sector/benchmark/event context and displaying horizon-separated outcome metrics.
It states that forecasts are uncertain and not guaranteed returns.

The latest read returns the stored PI-10 context alongside each horizon and
filters forecasts generated after the requested `as_of`, so a later record can
never be presented as an earlier decision input. The PI-09 scheduler accepts an
optional PI-10 context provider; when wired by the composition root, every
lifecycle-generated forecast receives the same point-in-time fundamentals, news,
sector, benchmark and peer bars. A provider failure is recorded on the scheduler
without blocking the checkpoint, and no input is fabricated to replace it.

The API returns `authorizes_action=false` and names NM-08 as risk authority and
NM-10 as execution authority. Autonomy may read direction, expected return,
interval, uncertainty, horizon, model evidence, context and validity from the
existing decision/strategy surfaces; PI-10 produces no order, fill, position,
portfolio mutation or ledger mutation. Its autonomy evidence is the canonical
`DecisionForecastEvidence` contract, and a forecast that has not reached final
confirmation is ineligible. An uncertain, stale or abstained forecast is not
converted into a forced signal.

### 24.7 Known limitations

- Exchange-calendar coverage remains the verified 2026 set. A 90d target that
  falls in 2027 is refused with an explicit per-horizon failure until the
  exchange publishes verified 2027 holidays; it is never approximated.
- Fundamental evidence is point-in-time correct when supplied, but PI-10 does
  not add a filing provider or scraper. Instruments without validated
  fundamentals still generate forecasts with missing fundamental features.
- Sector, benchmark and peer context requires explicit instrument metadata.
  No mapping is inferred from a free-text sector name.
- Deterministic fixtures verify orchestration and semantics; they are not live-
  market accuracy evidence. Any accuracy displayed is historical outcome
  evidence with its sample-size caveat.
- The pre-existing automatic scheduler planner still creates only the PI-09
  20-minute intraday lifecycle. Swing and long-term cycles are created through
  the authenticated per-instrument cycle API, which accepts all five PI-10
  horizons and uses the same PI-09 lifecycle; this phase did not change the
  auto-planner's intraday default.

## 25. PI-11 — Prediction evaluation and historical backtesting

PI-11 adds a bounded, read-only evaluation layer over the forecast records that
already exist. It does not create a second forecasting, risk, execution,
portfolio or accounting authority. Forecast accuracy and hypothetical trading
performance are reported as separate evaluations and are never combined into a
single accuracy or profitability claim.

### 25.1 Two evaluation meanings

The forecast evaluation engine scores immutable forecast versions against their
recorded target observations. The replay engine feeds the same point-in-time
forecasts into the existing NM-11 strategy replayer, which invokes NM-08 policy
and NM-10 execution assumptions against isolated simulation state. A model can
therefore be directionally accurate and still lose money after costs; the
report keeps those outcomes in separate sections.

### 25.2 Evidence sources and provenance

`novacontrol/novamarket/evaluation/` reads two existing sources:

- PI-09 `prediction_forecast_outcomes` joined to their immutable
  `ForecastOutput` contracts. These are original operational forecasts and are
  evaluated verbatim.
- NM-06 persisted forecasts and outcomes. These retain their own model, horizon
  and target provenance. When an original forecast has no stored outcome, the
  evaluator may use the first completed bar at or after its recorded target,
  within the evaluation cutoff. It records the actual observation timestamp and
  delay, labels the resolution method, and never uses a later bar as a silent
  substitute. The forecast itself is never regenerated.

Caller-supplied records are accepted only for research reconstruction or
synthetic testing. Every record declares `historical_operational`,
`research_reconstruction` or `synthetic_fixture`; the service rejects an explicit
record with no origin rather than defaulting it to operational evidence. A
reconstructed record must state the model-training timestamp and information
cutoff; both must not follow generation. The report labels reconstruction explicitly and never claims it was
generated at the historical timestamp. No forecast is regenerated during
evaluation.

PI-05/06/07/08 training scorecards are shown in a separate `model_evidence`
section. They are aggregate out-of-sample evidence, not per-target forecast
records, and are ranked only within a matching horizon.

### 25.3 Leakage prevention

An evaluation record is rejected unless its target follows generation, its
reference data is available at generation, and its actual observation does not
precede the target. Stored forecasts generated after the evaluation `as_of`
cutoff are excluded. Walk-forward folds use only preceding records and a fixed
configuration; no fold tunes hyperparameters or selects a model. A leakage
audit reports rejected records, look-ahead violations, delayed observations and
missing dataset/feature versions. The NM-11 forecast loader separately excludes
a contract generated after the decision timestamp or whose data cutoff is later
than the decision timestamp.

### 25.4 Forecast metrics

The engine reports price MAE, RMSE, MAPE, sMAPE, median absolute error, bias and
error distribution; return MAE, RMSE, bias, over/under-estimation rates and
downside/upside errors; direction accuracy, per-class precision/recall/F1,
confusion matrix, false bullish/bearish rates, and explicit uncertain and
abstained treatment. Neutral outcomes use a configurable threshold. Uncertain
and abstained predictions are counted separately and are excluded from the
default accuracy denominator; the alternative policy counts them as incorrect
and reports that choice.

Probability metrics use only calibrated directional probabilities; arbitrary
NM-06 model scores and uncalibrated probabilities are withheld. Uncertainty
labels and stored ensemble disagreement are summarized separately, without
reinterpreting disagreement as a probability or residual variance.
Brier, log loss, calibration bins, ECE and per-class ECE are reported. Interval
coverage is compared with the configured nominal level; a nominal interval is
never assumed to be calibrated.

### 25.5 Lifecycle, regimes and stability

PI-09 T−20, T−5 and T−1 checkpoints are grouped and paired by cycle. The report
counts whether confirmation improved or worsened absolute error, how often
direction changed, whether confirmation was unavailable, and how data freshness
relates to accuracy. Later confirmation is not assumed to be better.

Regime slices use the stored PI-04 regime label on each record and report sample
size plus a sufficiency caveat; insufficient slices do not support a
conclusion. Stability windows and first/second-half degradation are descriptive.
Walk-forward folds purge preceding records whose target observation had not
ended before the test window began, preventing overlapping-target leakage. No
result changes model weights, promotes a model or creates a trading policy.

### 25.6 Historical replay and authorities

The backtesting workspace uses the existing NM-11 `forecast_assisted` strategy
and its existing isolated store. PI-09 contracts are adapted to the NM-06 input
shape (`5m/15m/20m/1h/1d → next_session`, `3d → few_sessions`, `5d →
one_week`, `30d → one_month`, `90d → three_months`). The adapter preserves the
contract's generated time, data cutoff, target, model/ensemble versions,
uncertainty and validity. It does not rewrite a forecast or fabricate a
probability.

The replay uses existing NM-08 risk policy snapshots and NM-10 fee, spread,
slippage and liquidity assumptions. Daily OHLCV data cannot establish intraday
fills, queue position or a consolidated bid/ask. The default synthetic spread
and impact are conservative simulation assumptions, not exchange-grade
execution evidence. Results are labelled simulated; they never mutate the paper
wallet, real portfolio or ledger and never submit an order.

### 25.7 Reproducibility and failure handling

Every evaluation stores its full configuration hash, source scope, filters,
as-of cutoff, metrics, provenance and a deterministic report hash. The
append-only PI-11 database (`novamarket_evaluation.sqlite3`) rejects updates and
deletes with triggers. `verify` recomputes the report hash; `reproduce` reruns
the same configuration read-only and the records must match the same evidence
without appending a second report. Replaying an identical backtest
configuration returns the persisted run rather than overwriting it.

A missing source, unreadable forecast contract, insufficient sample or invalid
configuration is reported as a source error or as an explicit insufficient
sample. A failed read does not turn an empty result into a successful accuracy
claim. Evaluation failure cannot mutate financial state because the layer has
no ledger, portfolio or execution handle.

### 25.8 API and interface

Seven additive routes expose status, contracts, list/create, detail, verify and
reproduce under `/market/evaluations`. They reuse authenticated API conventions,
`ApiSurface`, `ROUTE_CONSUMERS` and the generated `docs/API.md`. The React
Prediction Evaluation workspace selects stored evidence, shows price, return,
direction, calibration, interval, lifecycle, regime, model and scorecard
sections, and can create a separate forecast-assisted replay with explicit
capital, benchmark, brokerage, spread and impact assumptions. It links to the
existing Backtesting workspace for the full run history.

### 25.9 Known limitations

- Evaluation requires already persisted forecast outcomes. An empty local
  database produces an honest empty evaluation, not fabricated history.
- Regime labels are only as good as the stored PI-04 evidence; missing labels
  remain `unknown`.
- Historical universe membership is not reconstructed by PI-11. Existing
  survivorship limitations therefore remain for any universe supplied to a
  replay.
- Daily-bar replays cannot prove intraday entry/exit quality. Fees and slippage
  are configurable simulation assumptions, not a broker tariff guarantee.
- Aggregate PI-05/06/07/08 training scorecards may cover different instruments
  or windows. They are presented separately and are not mixed into per-target
  metrics.

## 26. PI-12 — Autonomous trading integration

PI-12 is an integration layer, not another prediction, risk, portfolio or
execution engine. `ForecastDecisionSource` reads stored forecasts through the
existing PI-08/PI-10 read services and stores no forecast. `ForecastRiskAdapter`
is a read-only NM-06-shaped view used by NM-08. `ForecastOutcomeTracker` reads
the existing market-data and NM-10 stores and writes only PI-12 outcome and
audit records.

### 26.1 Stored-forecast contract

The source consumes `forecast_id`, lifecycle stage, validity, direction,
expected return, interval, calibrated probabilities, uncertainty, ensemble
disagreement, model/ensemble versions, execution horizon, generation/target
timestamps, data freshness/quality and snapshot identity. It accepts the PI-08
`ForecastOutput.to_dict()` names (`forecast_generation_timestamp`,
`forecast_target_timestamp`, `underlying_data_timestamp`,
`forecast_validity`, `data_freshness_status`, `ensemble_disagreement`) as well
as the internal short names; the adapter does not rewrite or regenerate the
forecast. PI-09 final-stage (`t_minus_1`) and valid/confirmed records are
required by default. PI-10 30d/90d outlooks and their target/status context are
merged by `forecast_id`; conflicting target timestamps are not combined.

### 26.2 Decision semantics

`ForecastDecisionEvaluator` is deterministic and returns one explicit decision:

- BUY only when an eligible upward forecast, calibrated probability, expected
  return, uncertainty, disagreement, freshness, data quality, technical/event
  checks and portfolio cash/exposure policy all pass.
- SELL only for an existing long holding and an eligible downward forecast or
  approved exit condition; without a holding the result is NO_ACTION and no
  short proposal can be created.
- HOLD retains an existing position when the evidence does not justify a
  change, including disabled automatic add-on exposure.
- WAIT records excessive uncertainty, disagreement, absent evidence, an
  unreconciled existing order or a horizon conflict.
- NO_ACTION records that no eligible action is justified; a candidate with
  selected forecast evidence is owned by PI-12 and is not re-traded by an
  independent NM-09 strategy in the same run. With no selected forecast, the
  pre-existing strategy path remains available.

### 26.3 Multi-horizon policy

The default short-term set is 5m/15m/20m/1h/1d/3d with priority 1h, 5m, 15m,
20m, 1d, 3d; medium-term is 1d/3d/5d/30d with priority 5d, 3d, 1d, 30d;
long-term is 30d/90d with priority 90d, 30d. A conflict between upward and
downward eligible horizons yields WAIT (or NO_ACTION when configured) rather
than merging incompatible targets. The selected horizon, all horizon evidence
and conflicts are stored on the decision. The policy is versioned and can be
read or replaced through the authenticated forecast-policy API; unknown fields
and incompatible priority/horizon sets are rejected.

### 26.4 Portfolio, risk and execution boundaries

Before a decision the evaluator reads the existing portfolio detail (holding,
average cost, market value, unrealized P&L, cash, concentration and sector) and
checks existing NM-10 open orders. A BUY/SELL becomes an ordinary AT-03 signal
and `DecisionProposal`; HOLD/WAIT/NO_ACTION never create a proposal. NM-08
independently assesses every proposal using its existing policy. Only an
approved proposal can be forwarded by the existing AT-04 paper-execution path;
PI-12 never calls a fill, portfolio, wallet or ledger mutation. A risk rejection
stays rejected with no alternate execution route. A stop, pause, kill or lost
lease blocks new actions without claiming submitted orders were cancelled;
submitted orders are reconciled by NM-10.

### 26.5 Audit and outcomes

Decision rows are immutable and append-only events record generation, proposal,
risk/execution transitions and outcome measurement. Every row preserves the
decision ID, monitoring-cycle/run identity, instrument/exchange, horizon,
forecast and ensemble/model versions, feature/regime/event/evaluation evidence,
portfolio context, rationale, contradictions, expected return, uncertainty,
probabilities, policy digest and NM-08/NM-10 references. Outcomes are stored
separately and explicitly split into forecast accuracy, decision correctness,
NM-10 execution state and paper-trade P&L. A missing market bar leaves the
outcome pending; a repeated tracker pass does not create a second outcome or
order. No model weight or policy is promoted.

### 26.6 API and workspace

Six additive routes under `/market/autonomy/decisions` expose prediction
decisions, decision detail, outcomes, due-outcome measurement, and GET/PUT
forecast policy. They reuse the existing bearer token and autonomy-operator
permission. The existing React Autonomy workspace shows PI-12 decision counts,
the horizon policy, decision evidence and outcomes, with refresh and measure
controls; it calls the server as the authority and does not derive trading
state locally.

### 26.7 Known limitations

- The local database may contain no stored PI-08/PI-10 forecasts or PI-11
  reports. In that case decisions are honestly NO_ACTION/WAIT; no operational
  forecast evidence is fabricated.
- PI-12 consumes forecasts; it does not train models, run the PI-09 scheduler or
  backfill historical forecasts during an autonomy cycle.
- The NSE/BSE calendar is verified for 2026 only; autonomy outside verified
  coverage fails closed as documented by AT-02.
- Outcome measurement requires the first completed market bar at or after the
  recorded target. A provider outage leaves the outcome pending rather than
  inventing a price.

## 27. PI-13 — Reinforcement-Learning Environment (implemented)

PI-13 is an additive research subsystem under `novamarket/rl/`. It composes the
existing PI-02 dataset, PI-04 FeatureService, PI-09 forecast contracts, NM-10
pricing/fee functions and the existing FastAPI/React architecture. It does not
create another market-data collector, feature engine, portfolio engine, risk
engine, execution engine or backtester.

### 27.1 Module map

```text
src/novacontrol/novamarket/rl/
├── models.py       versioned configuration, observations, actions, rewards,
│                   transitions, episodes and portfolio contracts
├── observation.py  point-in-time builder and strict validation
├── environment.py  deterministic next-bar simulation and reward interface
├── store.py        isolated SQLite episode/experience persistence
├── service.py      environment manager, recovery, evaluation and replay
└── __init__.py     public PI-13 surface
```

The API is additive under `/market/rl/*`; the React page is `RL Research`
(`web/src/pages/RLResearch.tsx`). The experience database defaults to
`novamarket_rl.sqlite3` in the existing data directory and contains only
`rl_*` tables.

### 27.2 Observation space

Every observation is a completed daily bar at timestamp T with `available_at <=
T`, the instrument identity, a market section (OHLCV, returns, volatility,
relative volume, moving-average/support/resistance distances), an optional PI-04
feature row, an optional PI-09 forecast row, the isolated simulated portfolio,
provenance and quality. A feature or forecast row with a future `as_of` or
`generated_at` is withheld and recorded in `quality.future_leak_blocked`; its
values are never exposed. Missing, zero, imputed and invalid values remain
separate, and the observation schema/feature-set version are persisted.

PI-13 deliberately supports exactly one instrument per environment. This keeps
the first release's cross-sectional timing and accounting testable; the model
contract already carries an instruments tuple for a later validated expansion.

### 27.3 Action space and transitions

The action space is long-only BUY/SELL/HOLD/WAIT/NO_ACTION with bounded
quantity, notional or position-fraction sizing. Every action is validated
server-side against instrument identity, available simulated cash/position,
maximum notional, maximum position weight, maximum gross exposure, liquidity and
the long-only rule. Invalid actions raise a typed refusal and increment the
episode's invalid-action counter without advancing the market.

A decision after bar T fills no earlier than the next completed bar's open. The
execution price reuses the NM-10 `execution_price` synthetic spread/impact model
and `fee_breakdown`; partial fills are bounded by the configured participation
of next-bar volume. Daily OHLCV is never presented as intraday execution. The
transition records observation, action, execution assumptions, reward
components, next observation, termination/truncation, dataset/feature/environment
versions and the simulation-config hash. Episode termination (historical
exhaustion or configured period end) is distinct from step-limit truncation.

### 27.4 Portfolio, episode and experience isolation

Each environment instance owns private virtual cash, one private simulated
position, realized/unrealized P&L, fees, equity, exposure and drawdown. Values
are INR-equivalent because `1 NovaCoin (NC) = ₹1`. No NM-07 portfolio, NM-10
order/fill, wallet, ledger or production position table is read for mutation or
written. Every episode has an environment ID, episode ID, immutable config,
timeline cursor and reproducibility evidence; reset clears its experiences;
cancellation is terminal; a failed or corrupted episode cannot alter another.

Experience rows are bounded per episode and globally, stored in the RL SQLite
file, and retrieved by episode. Payloads retain dataset/feature/environment
versions, the simulation config hash and the action/outcome provenance rather
than duplicating the market database. Recovery reconstructs the timeline from
the immutable dataset config, verifies the timeline signature, configuration
hash, cursor, transition count and experience count, then resumes only a running
episode.

### 27.5 Reward interface

`RewardInterface` exposes raw simulated transition components: realized and
unrealized P&L deltas, equity return, risk exposure, drawdown, transaction cost,
slippage, decision consistency, forecast direction/validity/uncertainty,
unnecessary trading and constraint violations. Its `total` is deliberately zero
and `authoritative_formula=false`. PI-14 owns any reward formula, weights and
policy evaluation. Profit alone is never asserted as a good trading policy.

### 27.6 API, workspace and safety boundaries

The additive surface provides status, contracts, health, environment creation/
list/detail, episode start/list/detail/step/reset/cancel, and bounded transition
and experience reads. All routes require the existing bearer authentication and
return `simulated`, `advisory_only` and `production_authority=false` markers.
The React page displays environment configuration, episode monitoring, the
observation snapshot, transition/experience history and resource health without
any control that can activate production trading.

The RL environment has no execution-engine, risk-engine, portfolio, wallet,
ledger or autonomy dependency. It cannot submit NM-10 orders, create production
fills, mutate production positions, bypass NM-08, change autonomy modes or
operator authorization, promote a policy or enable live trading. There is no
automatic promotion path and no Gymnasium/deep-learning dependency in PI-13.

### 27.7 Known limitations

- One instrument per environment; no cross-sectional or multi-asset RL episode.
- Completed daily bars only; no intraday execution or order-book depth claim.
- Raw (unadjusted) PI-02 bars are used; the dataset's action evidence is
  retained, but PI-13 does not claim a verified corporate-action adjustment.
- Reward total is zero until PI-14 supplies an authoritative policy.
- No algorithm (PPO/DQN/A2C/SAC) is implemented and no training/profitability
  result is claimed.
- Feature generation is bounded to 500 bars and may report missing/invalid
  features honestly when insufficient history exists.

---

## 28. PI-14 — Reward calculation and policy evaluation (implemented)

PI-14 is an additive `novacontrol.novamarket.rewards` package. It consumes the
stored transition rows owned by PI-13 and drives PI-13 `RLEnvironment` instances
for policy evaluation. It does not own a second market-data, feature, forecast,
portfolio, risk, execution or backtesting implementation.

### 28.1 Module map

```text
novamarket/rewards/
├── models.py       versioned configuration, split, result and validation contracts
├── components.py   eight independent reward components
├── engine.py       normalization, aggregation, attribution and result hashes
├── policies.py     no-action, rule-based, PI-12 adapter and seeded experimental RL
├── metrics.py      NM-11/NM-08-aligned financial, risk, trade and decision metrics
├── evaluation.py   chronological dataset windows, isolated environment runs,
│                   identical-condition comparison and walk-forward windows
├── store.py        four append-only `rl_*` tables in `novamarket_rl.sqlite3`
└── service.py      application façade used by FastAPI and NovaMarketService
```

The API is additive under `/market/rewards/*`; the React workspace is **Reward
Evaluation** (`web/src/pages/RewardEvaluation.tsx`). Reward and evaluation
records stay in the existing research database, never in production portfolio,
ledger, order, fill or risk stores.

### 28.2 Reward components and formula

| Component | What it evaluates | Main source |
|---|---|---|
| `profitability` | Simulated equity return, realized/unrealized P&L, capital utilization; costs are added back so they are not double-counted | PI-13 transition and private book |
| `risk` | Exposure, concentration, drawdown and optional validated volatility evidence | PI-13 portfolio view and NM-08-compatible metric functions |
| `transaction_cost` | Fee, tax/charge, slippage and cost ratio, exactly once | PI-13 simulated execution |
| `decision_quality` | Whether BUY/SELL/HOLD/WAIT/NO_ACTION was reasonable using timestamp-safe forecast, uncertainty, exposure, cash and position evidence | Observation/forecast snapshot |
| `forecast_utilization` | Directional alignment, expected return, uncertainty, disagreement, interval and horizon; abstention is allowed | PI-08/PI-09 forecast snapshot |
| `discipline` | Repeated/reversing orders, unnecessary turnover and constraint violations; legitimate high activity is not penalized by frequency alone | Transition history |
| `portfolio_management` | Capital utilization, concentration, exposure balance, stability, cash and drawdown control | Before/after private portfolio |
| `execution_quality` | Slippage, fees, fill ratio, participation, order size and unfilled orders | PI-13 simulated execution |

For enabled components `c`, the default aggregation is:

```text
normalized_c = clamp(raw_c / scale_c, -1, 1)
total = clamp(sum(weight_c * normalized_c) - applicable_penalties,
              bounds_min, bounds_max)
```

Scales make unlike raw quantities comparable; the bounded total is a learning
signal, not a valuation or a promise that losses are small. Raw component values,
scaled values, weights, missing components and penalties are retained in the
attribution payload. `missing_policy` is either `renormalize` over the available
enabled weights or `zero` for a missing component.

The shipped default weights (0.20 profitability, 0.15 risk, 0.15 decision
quality, 0.10 each for costs, forecasts, discipline, portfolio management and
execution) and scales are **experimental defaults**. They are configuration, not
empirically validated finance. A changed configuration gets a new deterministic
configuration ID and hash; stored historical results are never rewritten.

### 28.3 Reproducibility and attribution

Every `RewardResult` records the source transition/episode/environment IDs, the
configuration ID/version/hash, component contributions and penalties, raw and
normalized values, missing/excluded components, calculation timestamp and data/
environment versions. Its `repro_hash` hashes the canonical configuration and
result evidence. Calculation is deterministic for the same stored transition and
configuration; it does not mutate the environment book.

Episode summaries aggregate transition rewards and expose attribution at the
episode level. Policy evaluations retain their configuration, dataset, split,
conditions and policy version for policy-level comparison.

### 28.4 Policy evaluation and baselines

Evaluation drives complete isolated `RLEnvironment` episodes over completed
daily bars and computes:

- financial: total/cumulative return, annualized return when observations
  permit, realized/unrealized P&L and equity progression;
- risk: maximum drawdown, volatility, downside deviation, Sharpe, Sortino,
  Calmar, exposure and concentration, with explicit insufficient-sample states;
- trading: trades, wins/losses, win rate, average win/loss, profit factor,
  holding duration, turnover and costs;
- decision: action distribution, invalid actions, unnecessary trading and
  constraint violations;
- learning: episode reward distribution, completion and an explicit
  “training not implemented” marker.

Four baselines share the same dataset, date window, initial virtual capital,
simulation assumptions, fee/slippage functions, instrument and risk constraints:
`no_action`, `rule_based`, `pi12_adapter` and seeded `experimental_rl`. Each run
gets a fresh private environment; no mutable portfolio state is shared. The PI-12
adapter consumes only the stored decision contract through a read-only hook when
available and is clearly marked as an adapter, not a production policy.

When `split` is not `all`, chronological train/validation/test definitions are
required and validated for order and non-overlap. Evaluation of a named split
uses only that window. Walk-forward evaluation trains nothing in PI-14; it runs
successive chronological test windows and records the evidence honestly. There
is no random time-series split, no fitting on test data, no future-bar feature or
forecast use, and no learned normalization. Policy results are experimental
historical simulations and are not evidence of future profitability.

### 28.5 Persistence, API, cancellation and isolation

The research SQLite database contains `rl_reward_configs`,
`rl_reward_results`, `rl_policy_evaluations` and `rl_policy_comparisons`, with
bounded reads and append-only reward results. Evaluation writes partial evidence
when cancelled at a step boundary; `POST /market/rewards/evaluations/cancel`
requests that cancellation by `request_id`. Evaluations use `asyncio.to_thread`,
bounded transition counts and a fresh environment per policy so experimental
work does not block the event loop or mutate production state.

All routes require existing bearer authentication. The backend imports none of
`ExecutionEngine`, `ExecutionStore`, `PortfolioEngine`, `PortfolioStore`,
`RiskEngine` or `AutonomyRuntime` in the reward package. It cannot write a
production ledger/position/fill, submit an NM-10 order, bypass NM-08, alter
autonomy/operator permissions, activate a policy or enable live trading. The UI
has no promotion or activation control.

### 28.6 Known limitations

- PI-13 currently supports one instrument per environment; PI-14 therefore does
  not claim cross-sectional or multi-instrument generalization.
- Evaluation uses completed daily bars and synthetic next-open slippage/fee
  assumptions; there is no intraday, order-book or latency model.
- No RL training algorithm exists in this phase. `experimental_rl` is a seeded
  deterministic policy used only to exercise the evaluation path.
- Default weights/scales are experimental, and the project has no validated
  historical dataset proving profitability. Results are simulation evidence.
- Walk-forward windows are evaluation windows, not a training procedure;
  hyperparameter tuning and test-set reuse are not implemented.
- Cancellation is cooperative at simulation step boundaries and persists a
  partial experimental result; it does not checkpoint-and-resume a policy run.

## 29. PI-15 — RL agent training and optimization (implemented)

PI-15 is an additive `novacontrol.novamarket.agent_training` package. It owns no
market-data, feature, forecast, reward, risk or execution implementation: the
chronological datasets come from the PI-14 `PolicyEvaluationEngine`, the
simulated episodes come from PI-13 `RLEnvironment`, and validation/test metrics
come from PI-14's baseline evaluator.

### 29.1 Selected algorithm and rationale

**DQN** is the only training algorithm. PI-13 exposes five discrete actions
(BUY, SELL, HOLD, WAIT, NO_ACTION) and a bounded observation vector. DQN fits
discrete action values directly, supports a small pure-Python MLP with target
network and experience replay, and avoids a heavy ML framework or a second
policy-optimization stack. PPO/A2C were not adopted because PI-13 has no
continuous action space and the pure-Python implementation already exercises
the complete training contract; SAC is inapplicable. DQN's known limitations —
sample consumption, replay-memory requirements and value overestimation — are
recorded rather than hidden.

### 29.2 Module map

```text
novamarket/agent_training/
├── models.py       immutable configs, runs, checkpoints, artifacts, trials
├── agent.py        deterministic observation encoder, MLP, replay, DQN,
│                   target network, Adam/SGD and the policy adapter
├── datasets.py     PI-14 chronological train/validation/test loader + provenance
├── store.py        isolated `rl_train_*` tables in `novamarket_rl.sqlite3`
├── training.py     bounded worker, lifecycle, checkpoints, recovery, monitoring,
│                   validation/test, bounded search, comparison and archive
└── __init__.py     public service façade
```

Artifacts are JSON files under `data/rl_training/policies/`; checkpoints are under
`data/rl_training/checkpoints/` beside the existing isolated research database.
They are never written to portfolio, position, ledger, paper-fill or risk stores.

### 29.3 Configuration and data isolation

`TrainingConfig` rejects unknown fields, requires an environment and PI-14 reward
configuration, and validates every hyperparameter, resource limit and split. The
feature-set version defaults to the PI-13 observation schema and another value is
refused. Splits must be chronological and non-overlapping inside the environment
window; `TrainingDataPreparer` independently reloads each split with the PI-14
loader, verifies train < validation < test, and persists `dataset_version`,
`feature_set_version`, environment/reward versions, split hashes and a combined
`data_hash` with every run and policy. Test data is not used for training,
checkpoint selection or hyperparameter selection. Walk-forward operation is
available by submitting successive immutable configurations with rolling split
windows; each fold is isolated, and no fold's test window is reused as another
fold's training data. There is no automatic fold scheduler.

### 29.4 Execution, recovery and monitoring

At most one training worker runs at a time. The worker uses its own PI-13
environment instances; the API starts it through `asyncio.to_thread` and never
blocks the event loop. Supported controls are start, pause at a step boundary,
resume, cancel, retry, manual checkpoint and status/metrics reads. Checkpoints
store agent weights, optimizer moments, configuration, split provenance,
progress, episode RNG state and versions, plus an integrity hash over the whole
file. Resume loads and validates that file into a new run. Automatic retention
keeps the newest 20 checkpoints per run while preserving the latest and
best-validation records.

Monitoring exposes reward history, rolling mean/variance, episode length, loss
history, action distribution, invalid-action count, epsilon, throughput,
average environment-step latency, estimated remaining time when computable,
resource usage and checkpoint history. Training reward is explicitly labelled
an optimization signal, never financial profit. On startup, a run left
non-terminal by a previous process is failed with `stop_reason=interrupted` and
its incomplete policy rejected; checkpoints remain available for explicit
resume.

### 29.5 Validation, test and comparison

Final validation and a separate test run the trained adapter and the
`no_action`, `rule_based` and `pi12_adapter` baselines through PI-14's
`evaluate_experimental_policy` on identical conditions. Each policy artifact
records validation/test scores, financial/risk/trade/decision metrics, action
distribution, baseline results, warnings and dataset provenance. Comparison
refuses policies whose data hash, environment version, reward version or reward
configuration hash differ unless the caller explicitly records them as
incompatible; it never mutates the original evaluation records.

### 29.6 Safety boundaries and limitations

The package and API do not import or write production portfolio, position,
ledger, paper-order or risk state. Policy statuses are research lifecycle states
only (`created → training → trained → validation_pending → validated → tested`,
plus `rejected`/`archived`); there is no activation route, promotion control or
autonomy setting. NM-08 remains the sole risk authority and NM-10 remains the
sole paper-execution authority.

`TrainingManager.advisory_proposal(policy_id, observation=…)` is the controlled
PI-12-facing adapter. It verifies the persisted artifact hash, reconstructs the
DQN and returns a single advisory action with `status=advisory_pending_nm08`,
`risk_review_required=true`, `authorizes_action=false`, `execution_allowed=false`,
`orders_possible=false` and `activation_available=false`. It has no proposal store,
order gateway, risk engine or autonomy handle; an archived/rejected policy is
refused.

Limitations: PI-13 supports one instrument per environment, so PI-15 does not
claim multi-instrument generalization; completed daily bars and synthetic
next-open execution assumptions are not an intraday/order-book model; the
focused fixture is deliberately small and is software evidence, not a profitable
strategy; no GPU path is required; DQN is the only algorithm; and determinism is
verified for fixed seed/configuration on the same Python/runtime, not claimed as
bit-for-bit across hardware or Python versions. The latest trained weights, not
necessarily the best-validation checkpoint, are used for the final policy;
selection quality is reported separately.

## 30. PI-16 — Adaptive Learning and Market-Regime Intelligence

PI-16 is an isolated research package, `novacontrol/novamarket/adaptive/`, that
consumes PI-04 regime labels, PI-08 forecast uncertainty, PI-10 evidence,
PI-11 evaluation records, PI-13 experiences, PI-14 reward evaluations and PI-15
policy artifacts. It does not re-detect regimes, forecast prices, evaluate a
policy a second way, train a model or assess portfolio risk. Its output is
evidence, suitability research and an advisory recommendation.

### 30.1 Evidence model and persistence

The service persists only under its own `adp_*` tables in the existing isolated
research SQLite file: configurations, regime evaluations, suitability records,
market changes, data/concept drift, degradation records, recommendation
history, retraining recommendations, reassessment jobs, annotations and state.
There is no production portfolio, position, ledger, paper-fill, risk or order
write path. Record counts are bounded by `AdaptiveConfig.max_records`; list and
reassessment inputs are bounded as well.

`RegimeSnapshot` preserves PI-04's primary label, secondary conditions,
confidence (including `None` when the source is uncertain), supporting features,
feature quality, feature-set version and method version. An alias or a new string
is never silently converted into a different regime; unknown labels remain
`uncertain`. Regime annotations and experience retrieval require the evidence's
`available_at`/timestamp to be at or before the requested `as_of`, so a historical
query cannot see a label that was published later.

### 30.2 Detection methods

The detectors are deterministic and dependency-free: population stability index,
two-sample Kolmogorov-Smirnov with an asymptotic p-value, Jensen-Shannon
divergence, Page-Hinkley, CUSUM, Pearson/Fisher-z relationship change, rolling
error ratio and total-variation action-distribution shift. Thresholds and windows
come from `AdaptiveConfig`; detector and configuration versions are stored with
each result. Market-change evidence needs multiple signals, a corroborating
persistence window or a severe threshold before it is classified as sustained;
a single weak window is `normal_variation`. Drift and degradation likewise carry
`insufficient_evidence` / `data_unavailable` states instead of claiming a change
from a tiny or stale window. A future observation timestamp, outcome timestamp
or `available_at` is rejected before calculation.

### 30.3 Suitability and recommendation policy

`assess_suitability` scores regime-specific total return, risk-adjusted return,
drawdown, volatility, forecast uncertainty, policy stability, costs, invalid
action rate, sample size and freshness. Hard gates return `insufficient_evidence`,
`stale_evaluation` or `not_evaluated` rather than a number; an artifact-version
mismatch is reported as `stale_evaluation` with the mismatch reason.
When metrics are not supplied, the latest sufficient stored evaluation for that
policy, regime and policy artifact version is reused; an evaluation for a
different artifact version is `stale_evaluation`. Every result carries
`production_authority=false` and the explicit warning that `suitable` is a
research classification, never NM-08/NM-10 authorization.

Recommendations are advisory (`continue_monitoring`,
`no_adaptation_required`, `reevaluate_policy`, `run_new_backtest`,
`retrain_recommended`, `reassess_features`, `review_reward_configuration`,
`policy_unsuitable`, `insufficient_evidence`). Cooldown and hysteresis suppress
low-evidence switches, while a
severe escalation may bypass cooldown so genuine deterioration is not hidden.
Recommendations record their triggers and can only be created or acknowledged;
there is no activation, promotion, policy mutation, training start, broker or
live-trading control in the package, service wrappers or API.

### 30.4 Reassessment and experience retrieval

`TrainingManager.evaluate_regime_window` runs PI-15's existing bounded policy
evaluation for one explicit historical window through PI-14's
`evaluate_experimental_policy`. It never trains, selects hyperparameters,
activates a policy or writes a production artifact. Reassessment jobs are
versioned, idempotent by policy/regime/window, reusable when a valid stored
result exists, and fail honestly when no policy or evaluator is available. The
result records the window, policy/data/configuration versions, the PI-14
`no_action`/`rule_based`/`pi12_adapter` baseline comparison and conditions.
PI-14 labels the evaluator adapter version as `policy_version`; the adaptive
boundary maps that field to the immutable PI-15 artifact version before storing
regime evidence, so adapter labels cannot masquerade as policy artifacts. A
focused integration test trains a real DQN policy and asserts this mapping.

A reassessment may submit 2–20 explicit `windows` for a walk-forward run. Windows
must be chronological and non-overlapping; each fold receives its own bounded
transition budget, its own regime evaluation record and a fold index. The job
fails without claiming completion if the elapsed-time bound is exceeded. There is
no automatic fold scheduler: callers submit the windows, and a single in-flight
evaluator call is bounded by its transition cap rather than preemptively
cancelled.

Regime-aware experiences are read-only filtered views of PI-13 records with the
regime annotation joined at query time. PI-16 does not mutate the source
experience, its reward or the regime label. Concept drift links changed
feature/outcome relationships to a retraining recommendation, but it does not
start that retraining.

### 30.5 API and workspace

Thirty-one `/market/adaptive/*` routes expose status, contracts, health,
dashboard, configuration, regime analysis/annotations/evaluations, experience
retrieval, change/drift/degradation detection, suitability, recommendations,
retraining and reassessments. All are authenticated with the existing API
authorization dependency, are `no-render` routes, and are declared in both the
`ApiSurface` registry and `ROUTE_CONSUMERS`. `docs/API.md` contains 747 routes.
The React `AdaptiveIntelligence` workspace renders regime state, stored regime
evaluations, suitability, changes/data-and-concept drift/degradation,
recommendations, retraining history and reassessment history. Suitability reuses
stored PI-14 evidence rather than accepting hand-entered metrics with a fresh
timestamp; the workspace can create and run an explicit single-window
reassessment but never starts training.

### 30.6 Limitations

The package is research-only and the fixture evidence is software evidence, not
a profitable strategy. PI-16 does not include an automatic walk-forward
scheduler (windows are explicit), a per-regime hyperparameter search, a
browser-driven end-to-end test, or preemptive interruption of a single long
evaluator call. Nothing here is a substitute for NM-08 risk review or NM-10
paper-execution authority, and no broker or live-trading surface is enabled.

## 31. PI-17 — Multi-Agent Trading Intelligence

PI-17 adds `novacontrol/novamarket/multiagent/` and the React
`MultiAgentIntelligence` workspace. It is a NovaMarket-specific research
coordinator, not a second general agent framework: the existing NovaControl
desktop/research `agents` pipeline is sequential tool execution and is not used
for concurrent financial evidence. The coordinator has no portfolio, ledger,
risk-approval, paper-order or autonomy handle.

### 31.1 Roles and evidence boundary

Nine explicit roles are registered: market research, technical analysis,
fundamental analysis, prediction intelligence, market regime, risk analysis,
portfolio intelligence, RL policy intelligence and the advisory trading-decision
agent. The five required specialists for a decision cycle are technical,
prediction, regime, risk and portfolio; research, fundamental and RL policy are
optional, and the decision agent consumes the validated specialist responses.
Each definition declares its read-only capabilities, provider, model version and
the actions its role may return. A response outside that contract is rejected.
Pinning optional specialists is additive: the five required roles are always
assigned, so a caller cannot turn a partial research run into a BUY/SELL without
the risk and portfolio evidence the decision contract requires.

Evidence types are verified, derived, inferred, retrieved, experimental and
unavailable. Every item retains source identity, source reference, timestamps,
freshness, validation, data/model/forecast versions, correlation group and
independence. A missing, stale, invalid or future-dated required item produces
an explicit insufficient-evidence or abstain result; it is never replaced by a
guess. Confidence remains `None` unless a valid calibration exists, so the
workspace reports qualitative evidence strength instead of implied probability.
Evidence strength counts only independent, verified, valid, non-stale evidence;
invalid or unverified critical evidence carries zero directional weight and
forces WAIT. Every response status, including FAILED, is schema-validated for
action, payload, evidence and prohibited authority fields.

The service reads PI-04 features/regime, PI-05/PI-08 forecasts for the declared
horizon only, NM-08's read-only portfolio risk report, NM-07 portfolio state,
PI-15 policies and PI-16 adaptive evidence. The fundamental agent currently
abstains unless point-in-time fundamental evidence is supplied; the long-term
path can therefore return NO_ACTION honestly rather than invent fundamentals.

### 31.2 Bounded orchestration and aggregation

Cycles have one instrument, exchange and horizon. Horizons are never mixed;
5m/15m/20m/1h, 1d/3d/5d and 30d/90d map to explicit forecast labels. A cycle is
created idempotently, records the assigned/required roles and runs with a
bounded thread pool (`MAX_CONCURRENCY=6`), per-agent timeout (20 seconds by
default), overall cycle timeout (90 seconds) and explicit cancellation. Stale
runs left by a restart are recovered as degraded, decision-less cycles with
their history retained; a cancelled or already-running cycle cannot be run
twice. Cycles, runs, messages, evidence,
conflicts, decisions and events are persisted in the isolated
`novamarket_multiagent.sqlite3` store using only `ma_*` tables; no production
financial table is opened.

Conflict detection retains directional disagreement, stale/invalid data, high
forecast uncertainty, authoritative NM-08 restrictions, portfolio impediments
and experimental-RL disagreement. Correlated evidence is discounted, forecast
uncertainty cannot be promoted to confidence, and unresolved authoritative
restrictions prevent a BUY/SELL. A non-approved NM-08 outcome or any unresolved
breach list is treated as a restriction, not as advisory colour. Aggregation is
evidence-weighted rather than a vote. The result is `buy`, `sell`, `hold`,
`wait` or `no_action`, plus supporting and contradictory agents, restrictions,
provenance and an explanation.

WAIT is used when critical evidence is stale, invalid or materially uncertain;
NO_ACTION is used when required evidence failed or the context could not be
loaded. Neither action submits an order. `hold` is an advisory view, not an
instruction to change a position.

### 31.3 PI-12 handoff and authority preservation

The only downstream contract is an explicit advisory handoff. It reports the
recommendation, horizon, evidence strength (never invented confidence),
unresolved conflicts, NM-08 restrictions and `production_authority=false`,
`advisory_only=true`, `order_submitted=false`. The documented execution path is
PI-12 autonomy orchestration → NM-08 risk reassessment → NM-10 paper execution.
The multi-agent package has no method that can mutate a portfolio or ledger,
submit/cancel an order, change an autonomy mode, grant an operator permission,
activate a policy, connect a broker or enable live trading. Operator
permissions, the kill switch, single-cycle concurrency, idempotency,
reconciliation and recovery-to-disabled behavior stay in their existing owners.

### 31.4 API and workspace

Eighteen paths / nineteen authenticated routes under `/market/multiagent/*`
expose status, contracts, health, agents and configuration, cycles (create,
detail, agents, run, cancel), evidence, conflicts, decision, handoff, history and
performance. They are `no-render` consumers declared in `ApiSurface` and
`ROUTE_CONSUMERS`; `docs/API.md` now contains 790 routes after PI-18. The workspace creates a
cycle, runs it, and renders the monitor, agent health/configuration,
analysis/evidence detail, conflicts, aggregation/authority boundary and
performance. The browser displays the same isolated coordinator state as the
API; it does not calculate a decision in React.

### 31.5 Verification and limitations

Focused tests cover deterministic agents and fixtures, required versus optional
failure, conflicts and correlated evidence, multi-horizon selection, WAIT and
NO_ACTION, provenance and future-data rejection, cross-cycle isolation,
permissions, API/UI registration, resource limits, untrusted content and static
runtime financial isolation. An HTTP test uses the real `create_app()` factory
and proves a cycle is advisory and leaves wallet/portfolio state unchanged.
Live application verification produced a completed cycle for
`XBOM:ADANIENT` with all five required agents, a stale-data conflict, WAIT and
`order_submitted=false`; the workspace loaded it through the Vite proxy with no
console errors.

Limitations: specialist reasoning is deterministic rule evaluation over stored
evidence, not a new prediction model; no calibrated multi-agent confidence is
claimed. Fundamental and research evidence depend on callers/parent systems
supplying point-in-time records. RL evidence is experimental and discounted. The
live environment's bar history ended several days before the verification date,
so the real cycle correctly returned WAIT rather than an actionable
recommendation. A focused fixture is software evidence, not a profitable
strategy, and the full repository test suite was not run.
## 32. PI-18 — Advanced Portfolio Optimization and Capital Allocation

PI-18 adds `novacontrol/novamarket/optimization/` and the React
`PortfolioOptimization` workspace. It answers a portfolio-level question — how
should available virtual capital be distributed across eligible instruments
under explicit risk, diversification, liquidity and turnover constraints — and
does not evaluate instruments as independent trades. The package has no import
of the NM-07 portfolio engine, NM-08 risk engine or NM-10 execution engine. It
receives a read-only context mapping assembled by `NovaMarketService` and owns
only the isolated `novamarket_optimization.sqlite3` research file.

### 32.1 Inputs and eligibility

The context loader reads the authoritative NM-07 portfolio detail and
valuation, NM-02 bars for the declared interval, PI-04 features and regimes,
PI-05/PI-08 forecast records through the existing forecast readers, and NM-08's
read-only portfolio risk report. Pending validated portfolio proposals are
counted as a buy reserve. The optimizer never discovers instruments, ingests
prices, values a portfolio or approves risk.

Every run records the portfolio ID, horizon, instrument universe, portfolio
snapshot version, market-data version, forecast version, NM-08 configuration
version, optimization configuration version, solver version and result status.
A candidate is excluded with a reason when it has no exchange-qualified
identity, an unsupported exchange, a stale or missing price, less than the
configured return history, no usable liquidity observation under the configured
policy, or no horizon-compatible validated forecast where the method requires
one. A holding flagged stale makes the snapshot non-runnable; the optimizer does
not fill the price. Forecasts are filtered by generation/availability time,
validity, the configured minimum quality and the horizon-specific maximum age.
A forecast with no usable expected-return field is excluded rather than replaced
with zero. Forecast confidence is never promoted to a calibrated probability.
Bars and the portfolio valuation must be INR; mixed or foreign price currencies
are excluded explicitly.

### 32.2 Expected returns and covariance

PI-18 consumes forecast `expected_return`/`predicted_return` and derives a
return when only a target price is available. Prediction intervals, forecast
uncertainty, model disagreement, validity and quality are retained as evidence.
Every run has one explicit horizon: 5m, 15m, 20m, 60m, 1d, 3d, 5d, 30d or 90d.
The horizon selects the bar interval, forecast age limit, price age limit,
periods-per-year conversion and default allocation caps. There is no native
20-minute bar, so the 20m horizon consumes five-minute bars and treats the
horizon as four observation periods rather than relabelling 15-minute data.
Different horizons are separate runs; their recommendations are never blended.

Returns are aligned on identical observation timestamps (intersection only) and
log returns are winsorized at the 0.5th/99.5th percentiles. Sample, exponentially
weighted and diagonal-shrinkage covariance are supported. Matrices are
symmetrized, checked for finite values and positive definiteness; a singular
matrix receives a documented ridge rather than fabricated correlations or an
unrelated fallback. Correlation stability, condition number and warnings are
returned with every run.

### 32.3 Objectives, solvers and constraints

Supported objectives are maximum expected return, minimum variance,
risk-adjusted return, maximum Sharpe, minimum downside risk, diversification
and configurable multi-objective weights over return, risk, diversification,
turnover and liquidity. Supported practical methods are mean-variance,
minimum-variance, risk-parity, equal-weight, inverse-volatility and the current
book as a reference baseline. The implementation is deterministic
projected-gradient/projection over numpy; no new solver dependency is added.
Risk-parity uses a bounded fixed-point approximation.

Long-only weights, cash as a legitimate residual, minimum cash reserve,
maximum instrument weight, sector caps, maximum positions, maximum turnover,
liquidity participation and pending-buy reserve are enforced after every
projection. If constraints cannot deploy capital, the residual remains cash;
constraints are never silently relaxed. A solver failure returns
`SOLVER_FAILED`; a timeout returns `TIMED_OUT`; infeasible or invalid input is
reported explicitly. No unrelated method is substituted and
`fallback_used=false` is recorded.

### 32.4 Risk, diversification and liquidity analytics

The optimizer reports annualized volatility, Sharpe, Sortino, Calmar, historical
maximum drawdown, downside deviation, VaR/CVaR at 95%, per-position risk
contributions, sector weights, turnover and regime-sensitive warnings. These
are research estimates with documented assumptions, not an NM-08 approval and
not a guarantee against loss. NM-08 remains the sole operational risk authority;
the handoff explicitly requires its independent reassessment.

Diversification reports the number of holdings, HHI, effective holdings,
effective sectors, top weight, average pairwise correlation and diversification
ratio (`weighted volatility / portfolio volatility`). HHI is computed on
invested weights, excluding cash, and a high correlation warning is emitted
even when the holding count looks adequate.

Liquidity uses average historical close x volume notional, scaled to a daily
estimate for intraday bars, multiplied by the configured participation rate. It is explicitly an estimate, not confirmed
executable capacity. Missing volume under the `exclude` policy removes the
instrument; under `warn` it remains eligible with a limitation and is not
presented as liquidity-validated.

### 32.5 Rebalancing, scenarios and backtests

Rebalancing compares current and target weights and emits
`INCREASE_POSITION`, `REDUCE_POSITION`, `MAINTAIN_POSITION`, `OPEN_POSITION`,
`CLOSE_POSITION`, `HOLD_CASH` or `NO_REBALANCING_REQUIRED`. Thresholds, minimum
notional, turnover caps, transaction costs and slippage are configurable. The
result reports current/target expected return, volatility, risk change, net
expected improvement, transaction-cost drag and a funding gap against available
cash after pending buys and the configured minimum cash reserve. A cash residual is an explicit recommendation. Nothing
is executed automatically; PI-12, NM-08 and NM-10 still own the operational path.

Every completed run includes an `explainability` section: objective/method,
invested and cash weights, per-position expected return, forecast quality and
risk contribution, plus the binding instrument, sector, liquidity, cash,
position-count, turnover and risk limits. It states what is not claimed and
never invents a causal security forecast.

Scenario analysis separates historical replay from hypothetical stress. It
covers market-wide and sector declines, volatility/correlation increases,
position shocks, liquidity deterioration, forecast deterioration and regime
transitions, with position and sector contributions that reconcile to the
portfolio change. Pure correlation and volatility scenarios report risk changes
and a zero deterministic value change rather than inventing a loss.
Operator-defined shocks must be fractions and must name allocated instruments;
hypothetical outcomes are labelled `is_forecast=false`.
Backtesting compares equal-weight, current-book, minimum-variance, risk-parity,
mean-variance and inverse-volatility methods over identical trailing windows,
capital, costs, slippage and rebalance frequency, including the current-book
baseline. Each solver sees returns strictly before the period it is about to
earn, so the rebalance period's return cannot leak into the allocation.
Transaction costs and turnover are charged, and annualization uses the selected
horizon's periods per year rather than a hard-coded 252. Survivorship bias is disclosed as unresolved
because the available dataset contains the current instrument universe, not a
historical constituent history.

### 32.6 Persistence, API and authority

Configurations, runs, snapshots, eligibility/exclusions, forecast references,
covariance versions, target allocations, scenarios, rebalancing intents and
backtests are append-only in `po_configs`, `po_runs`, `po_scenarios`,
`po_backtests`, `po_events` and related indexes. Re-running the same
idempotency key returns the recorded run rather than overwriting it. The store
does not contain portfolio, ledger, order or fill tables. A run records
`financial_mutation=false`, `order_submitted=false`, `production_authority=false`
and `advisory_only=true`.

The API adds 24 authenticated routes under `/market/optimization/*`
for status, contracts, health, configurations, validation, runs, allocations,
diversification, risk, liquidity, rebalancing, scenarios, backtests, handoff,
history and cancellation. They are `no-render` consumers in `ApiSurface` and
`ROUTE_CONSUMERS`. The workspace renders portfolio overview, configuration,
allocation, correlation, diversification, risk, scenarios, rebalancing,
strategy comparison and the handoff boundary. The UI performs no financial
calculation of its own; all numbers come from the backend.

The PI-17 Portfolio Intelligence Agent receives the latest completed
optimization under `portfolio.optimization` as structured advisory context. It
can identify a horizon mismatch or non-completed status, but it cannot alter the
optimization result, and a new optimization run is required to change it. This
is integration into the existing coordinator, not a second orchestration system.

### 32.7 Failure handling and limitations

Explicit statuses are `pending`, `running`, `completed`,
`completed_with_warnings`, `insufficient_data`, `invalid_input`,
`infeasible_constraints`, `solver_failed`, `timed_out`, `cancelled` and
`failed`. Concurrent runs are capped (two by default), instrument counts are
bounded (50), run timeouts are bounded and cancellation is checked inside the
solver. Missing history, stale forecasts, invalid covariance, unavailable
liquidity and resource exhaustion produce structured failures rather than
fabricated weights.

Known limitations: covariance and scenario estimates are backward-looking and
can fail during structural breaks; VaR/CVaR assume the historical distribution
and are not guarantees; daily volume is not executable liquidity; expected
returns inherit forecast error; backtests use the current universe and may
retain survivorship bias; and the optimizer's output is a constrained
mathematical recommendation, not a promise of profit. No broker, live trading,
autonomy change, NM-08 approval or NM-10 order is available from this phase.

## 33. PI-20 — Prediction Intelligence Dashboard and Decision Explainability

PI-20 is an isolated read-only workspace over the existing PI-04 through PI-19
services. It is not a second prediction engine, model registry, evaluation
system, agent coordinator, optimizer or execution path. The package is
`novacontrol/novamarket/dashboard/` and its SQLite file contains only
`pd_dashboard_meta` and `pd_dashboard_configs` (filter presets and timestamps).
The selected view is a browser-local preference. No financial table is read
through a dashboard-owned store or written by dashboard code.

### 33.1 Composition and contracts

`DashboardService` receives the live `NovaMarketService` and calls its existing
read surfaces. It does not duplicate forecasting, feature, regime, evaluation,
agent, research, portfolio or RL calculations. Source payloads keep their model
and feature-set versions, generation and target timestamps, dataset/evaluation
versions and evidence identifiers. The dashboard adds presentation metadata
(status, freshness, `as_of`, pagination and limitations) around those values.

Every read is bounded. List endpoints accept `limit` (and instrument/horizon
filters) with a server-side cap; replay and instrument history are paginated or
limited rather than returning a complete archive. The service normalizes an
unavailable source to one of `AVAILABLE`, `PARTIAL_DATA`, `STALE`,
`INSUFFICIENT_EVIDENCE`, `UNAVAILABLE` or `FAILED`. A missing count is reported
as unavailable, not silently coerced to zero. An empty useful state (for example
no forecast in the isolated preview database) is `INSUFFICIENT_EVIDENCE`, not a
fabricated forecast.

### 33.2 Views and explainability rules

The React workspace has thirteen views: Overview, Instrument, Forecasts,
Comparison, Horizons, Lifecycle, Performance, Regimes, Decisions, Research,
Portfolio, Policies and Replay. The dashboard UI uses the existing `ui.tsx`,
`charts.tsx`, `useApi` and hash navigation. The frontend formats and charts
backend values; it does not own prediction arithmetic or authoritative
recommendation logic.

- Overview separates forecasts, research recommendations, autonomous
decisions and paper orders. A forecast is never rendered as an executed trade.
- Instrument views preserve the latest price timestamp and freshness, OHLCV
  coverage, regime and research status. Missing candles are not synthesized.
- Multi-model and multi-horizon views compare only compatible scope; separate
  horizon targets are never merged into one price target. Model disagreement is
  shown as disagreement, not as a probability.
- Calibrated directional probability, prediction interval, forecast
  uncertainty, model disagreement, data quality, freshness and evidence
  completeness are separate fields. An uncalibrated detector or agent score is
  shown with its recorded kind and is never labelled a probability; directional
  colour is not used as statistical uncertainty.
- Lifecycle checkpoints preserve each immutable forecast version. Missing late
  checkpoints remain missing and are never relabelled as earlier T−n
  confirmations. Swing and long-term schedules use their own evaluation times.
- Performance keeps MAE/RMSE/MAPE/sMAPE, directional metrics and confusion
  matrix, Brier/log loss/calibration/coverage and their evaluation period,
  sample size, dataset version and model version. Forecast accuracy is not
  profitability.
- Regime views show the detector's classification, recorded score/kind,
  transitions and observed duration. Uncertain detection remains uncertain.
- Decision views show agent assignments, findings, supporting/contradictory
  evidence, uncertainty and missing information, distinguishing independent
  evidence, derived evidence, interpretation and hypotheses.
- Explainability returns what/why/evidence/contradiction/limitations/aftermath
  only from stored sources. Feature importance or contributions appear only when
  the source explain endpoint computed them; they are labelled non-causal and
  carry model, feature-set and explanation timestamps. No SHAP value is invented.
- Research separates reported facts, derived estimates and AI interpretation
  and preserves reporting and publication times. Portfolio distinguishes the
  authoritative current state, simulated state, experimental optimization and
  proposed rebalance. RL displays experimental algorithm/policy/training reward
  separately from validation and out-of-sample performance, and cannot promote
  a policy.
- Replay applies the decision cutoff to every source, preserving the original
  decision and evidence versions. It labels reconstructed simulation separately
  from recorded operational history.

### 33.3 Updates, authority and integration

The page uses the existing app-wide `GET /events/stream` SSE channel. A single
`EventSource` listens for existing NovaMarket forecast, prediction-lifecycle,
regime, portfolio-valuation, data-freshness and market-event types and bumps a
revision that `useApi` uses to refetch active views. There is no dashboard timer
polling and no second event bus. The stream is authenticated with the same
token as the API; the Vite dev server proxies `/events` to the API origin and
displays the network EventSource response. If the stream is interrupted the
browser reconnects on its own; data remains readable from the last successful
response. Existing events that a subsystem does not publish cannot be invented
by the dashboard.

All 22 routes require the existing server-side operator authentication. The
dashboard has no portfolio, position, ledger, fill, order, NM-08 or NM-10
mutation path. `authority_contract()` reports `read_only: true`; the only write
surface is saved dashboard filters/preferences. NM-08 stays the sole risk
authority and NM-10 the sole paper-execution authority. No broker, live trading,
autonomy bypass or policy activation is exposed.

Verification commands and exact counts are in
[PI20_COMPLETION_REPORT.md](PI20_COMPLETION_REPORT.md). The 20 November 2026
audit drove `scripts/pi20_audit.py` through all thirteen views against the
populated local database: 279 controls and 45 tables inspected with 0
accessibility findings, keyboard and tablist contracts executed, no horizontal
overflow at twelve widths from 1920 to 320 px, FCP 284 ms on the dev server, and
no dashboard endpoint above the 2 s budget. Deterministic fixtures still cover
the populated views in the Python suite. This is an automated review, not a WCAG
certification. A separate live walk (`scripts/pi20_live_walk.py`) opened all
thirteen views, drove their interactions (instrument lookup, decision and
research selection, replay reconstruction) and observed loading, empty, stale and
error states at 1440, 1024, 768, 480 and 390 px with no overflow.

## 34. PI-24 Event-Aware Intelligence and Final Closure

PI-24 is the `novamarket/event_intelligence` package, an advisory composition
over NM-04 event evidence, PI-19 research and the existing forecast, portfolio
and risk services. It does not create another agent system, forecast engine,
risk authority or execution path. The complete accepted roadmap ends at PI-24;
there is no PI-25.

### 34.1 Evidence and normalization

`EventIntelligenceService` normalizes the NM-04 `MarketEvent.to_dict()` contract
without rewriting it. Corporate, economic, market and company-specific groups
come from NM-04's declared subcategories. Every normalized event preserves the
event id and version, separate event date/publication/retrieval timestamps,
source references from the detail endpoint's evidence, verification reasons and
conflicts, affected instruments, companies, sectors, exchange, importance,
mappings and related events. Instrument names/exchange are resolved through the
existing radar instrument view, not fabricated. `sync` is append-only and
idempotent by event version; malformed rows are reported as skipped.

Status mapping is evidence-based: officially confirmed or multi-source
corroborated is `verified`; single-source/unverified is `unverified`;
conflicting evidence is `conflicted`; retracted/expired is `expired`; resolved
is `completed`; explicit duplicate/irrelevant radar states are preserved; an
unresolved extraction is `detected`. A status is never promoted merely because
an event is old.

### 34.2 Point-in-time rules

For every `as_of`: future publications are excluded, missing timestamps are
`insufficient_evidence`, event features exclude future events and future
outcomes, and outcome statistics include only records observed by the cutoff.
Feature snapshots carry `point_in_time=true`,
`future_outcomes_included=false` and a version/provenance. Event outcome bars
are filtered to valid/non-partial observations at or before the cutoff. The
forecast context is non-mutating: `event_adjustment.applied=false`, no price
adjustment, no direction, no abstention recommendation and no model version
change. The existing PI-09/PI-10 forecast lifecycle and immutable forecast
versions are not touched.

### 34.3 Warnings, impact and agent evidence

Warnings are categorical and horizon-aware. They expose approaching events,
unverified/conflicting reports, high-importance volatility risk, stale evidence
and source insufficiency, with no calibrated event probability unless a
separately validated event model exists. Impact reports channel categories and
observed historical co-movement, explicitly not causation. Outcome studies
group stored outcome records by horizon and mark samples below the configured
minimum insufficient.

PI-17's Market Research Agent reads `market.events` supplied by the existing
multi-agent context; verified and unverified events stay separate and external
text is treated as data. PI-22 receives a read-only portfolio event context
through its injected `event_loader`. Neither handoff creates a risk decision,
changes a forecast or authorizes an order.

### 34.4 Surface and limitations

There are 23 authenticated routes under `/market/event-intelligence/*`, all
declared in `ApiSurface` and `ROUTE_CONSUMERS`, with `docs/API.md` regenerated at
931 routes. The React workspace has eight views. Known limitations: event
coverage is limited to configured NM-04 sources; PI-24 does not fetch paid or
exchange-grade real-time feeds; event impact is correlational and cannot claim
causation; a history without stored outcomes or bars returns
`insufficient_data`; and the event outcome study is only as representative as
the events that were actually recorded. Verification and exact counts are in
[PI24_FINAL_COMPLETION_REPORT.md](PI24_FINAL_COMPLETION_REPORT.md).
