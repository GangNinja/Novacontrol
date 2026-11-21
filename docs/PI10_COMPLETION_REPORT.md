# PI-10 — Swing and Long-Term Forecasting Completion Report

**Phase:** PI-10  
**Branch:** `feature/novamarket`  
**Date:** 2026-11-16  
**Status:** **PASS (scoped)** — implementation and verification complete; the one
remaining horizon limitation is an externally unpublished 2027 NSE holiday
calendar, described in §17.

## 1. Initial repository assessment

- Branch verified as `feature/novamarket`; the worktree already contained the
  PI-01–PI-09 user changes. Nothing was staged, committed, pushed, tagged or
  merged during this phase.
- PI-01 through PI-09 completion reports were read and all record PASS or scoped
  PASS. The prerequisite gate was satisfied before PI-10 code was written.
- Existing capabilities inspected and reused: PI-01 prediction contracts and
  `PredictionStore`; PI-02 chronological datasets; PI-03 market-data gateway and
  exchange calendars; PI-04 feature/regime engine; PI-05 statistical models;
  PI-06 XGBoost/LightGBM; PI-07 LSTM; PI-08 ensemble/uncertainty; PI-09 lifecycle,
  revisions, target evaluation and accuracy; NM-04 radar events; NM-08 risk;
  NM-10 paper execution; existing forecast API and Forecasts page.
- No second prediction engine, forecast store, model registry, fundamental
  ingestion service, risk authority or execution path was created.

## 2. Existing functionality reused

| Concern | Existing authority reused | PI-10 addition |
|---|---|---|
| Forecast contract and storage | PI-01 `ForecastOutput`, `PredictionStore` | Horizon/profile metadata and context evidence attached to the existing immutable forecast |
| Models | PI-05/PI-06/PI-07 via PI-08 ensemble | Horizon orchestration, eligibility visibility, context input plumbing |
| Features | PI-04 engine and swing/long profiles | No duplicate indicator calculation; existing context-feature inputs supplied |
| Market data | NM-02 engine, PI-03 gateway, validated bars | Bounded context reads only; no provider acquisition |
| Fundamentals/news | PI-01 evidence contracts and NM-04 radar | Point-in-time validation, dedupe and model-input filtering |
| Lifecycle | PI-09 cycles/checkpoints/outcomes | Corrected target/checkpoint policy for swing and long horizons |
| Accuracy | PI-09 `ForecastOutcome` rows | Horizon-specific MAE/RMSE/bias/direction/interval metrics |
| Autonomy | Existing strategy and decision evidence paths | Advisory forecast evidence; no new authority |
| API/UI | Existing `/market/forecasts*` family and Forecasts page | Five additive horizon routes and one swing/long card |

## 3. Swing forecasting implementation

`novacontrol/novamarket/outlook/models.py` defines versioned profiles for 1d,
3d and 5d with `target_basis=trading_sessions` and target counts 1/3/5. The swing
profiles use PI-04's existing `SWING` feature profile and the existing PI-08
ensemble; no intraday model is assumed suitable for a multi-day target.

Target defects fixed across the stack:

- PI-09 `default_target` now counts verified sessions for 1d/3d/5d instead of
  elapsed calendar days.
- PI-05 `_target_at` now counts sessions for swing horizons.
- PI-02 daily training labels now use verified session counts for swing, so the
  model is trained on the same horizon PI-10 evaluates. A missing row for a
  verified session makes that label unavailable; it is never replaced with the
  next observed row, which would silently lengthen the horizon.
- A Friday 3d target is Wednesday, not Monday.
- PI-10 horizon profiles report the real PI-04 feature-set version
  (`pi04-feature-set-1.0`), not a fabricated profile version.

The initial swing version is created with the cycle. 1d confirms at T−60m; 3d at
T−1d; 5d at T−2d; all final confirmations are T−1m.

## 4. Long-term forecasting implementation

30d and 90d profiles use `target_basis=calendar_days`, count 30/90 elapsed days,
then select the first verified session close at or after that date. They use the
PI-04 `LONG_TERM` profile and the same PI-08 ensemble. The previous PI-09 policy
set the initial forecast at the target session open; PI-10 now creates the
initial version with the cycle, confirms 30d at T−7d and 90d at T−14d, and takes
the final version at T−2d/T−3d respectively.

If the expected target cannot be verified, only that horizon fails. The service
does not call a model against an unverified target; other horizons continue.
Daily 30d/90d training labels now use the first verified session at or after the
calendar interval and require that session's exact bar. A missing target bar is
reported, never replaced by a later observed row.

## 5. Supported horizons and target rules

| Horizon | Family | Target rule | Initial | Confirmation | Final |
|---|---|---|---|---|---|
| 1d | swing | next verified session close | cycle creation | T−60m | T−1m |
| 3d | swing | 3 verified sessions ahead | cycle creation | T−1d | T−1m |
| 5d | swing | 5 verified sessions ahead | cycle creation | T−2d | T−1m |
| 30d | long | 30 calendar days, first verified close | cycle creation | T−7d | T−2d |
| 90d | long | 90 calendar days, first verified close | cycle creation | T−14d | T−3d |

`GET /market/forecasts/horizons/status` returns these profiles as data with
`pi10-horizon-profiles-1.0`.

## 6. Fundamental-data integration evidence

- Accepts PI-01 `FundamentalSnapshot` or validated mapping equivalents.
- Requires both a source/publication timestamp and an acquisition/availability
  timestamp; missing values are rejected, not substituted.
- Drops every snapshot observed or received after the forecast cutoff.
- Deduplicates by fiscal period and uses the latest received snapshot, allowing
  later restatements without future leakage.
- Preserves instrument, fiscal period, metric values, currency, the full
  provenance payload (including units), source, quality, freshness and both timestamps.
- Missing fundamentals remain missing and do not invalidate an otherwise
  eligible model.

Deterministic tests prove that a valid Q1/Q2 set is accepted, a future Q3
snapshot is dropped, a wrong-instrument record is rejected and only point-in-time
snapshots reach the model.

## 7. Sector and benchmark integration evidence

Mappings are read only from validated instrument metadata:
`sector_instrument_id`/`sector_index`/`sector_benchmark` and
`benchmark_instrument_id`/`benchmark_index`. Mapped ids must be canonical
registered instruments. Free-text sector labels are never converted into index
symbols. Daily bars are read through the existing market-data engine, filtered to
the cutoff, deduplicated and excluded when partial/invalid.

The context reports instrument, sector and benchmark 1-/20-bar returns,
volatility, SMA/trend, sector-relative and benchmark-relative performance, and a
bounded 20-peer breadth set. Tests verify available mappings, relative returns,
peer breadth and explicit `sector=null`/`benchmark=null` when no mapping exists.
A mapping that is present but names an unregistered instrument or malformed id
is also reported explicitly and excluded. The same bars are passed into the
existing PI-04 context features through PI-08 → PI-06/PI-07.

## 8. Corporate-event integration evidence

- Accepts PI-01 `NewsEventEvidence` and existing NM-04 radar rows.
- Preserves event/evidence id, instruments, category, headline, source,
  publication and acquisition timestamps, quality, verification status,
  relevance/importance, checksum and source URL.
- Future publication/acquisition records are dropped.
- Duplicate checksums/headlines are deduplicated; the best verified/quality row is
  retained.
- Only explicitly confirmed/verified rows reach model inputs. The canonical
  NM-04 states `officially_confirmed` and `multi_source_corroborated` are
  accepted. `single_source`, `unverified`, `conflicting_evidence`, `retracted`,
  `expired` and quality-only rows remain informational context with
  `model_input=false`; a retracted or expired lifecycle status also blocks a
  row even when its historical verification state was confirmed.
- Rumours stay in informational context with `model_input=false`.
- The context count exposes supplied, point-in-time, deduplicated, model-eligible,
  unverified and future-dropped records.

## 9. Model and ensemble evaluation

PI-10 does not train or promote models. Each horizon runs through PI-08's
existing eligibility, validation evidence, weights, fallback/abstention,
disagreement and calibration policy. The API exposes per-horizon eligibility and
disagreement; a horizon with no eligible constituent abstains rather than forcing
a direction. Existing PI-05/PI-06/PI-07 tests continue to pass. Probabilities
remain withheld when the ensemble cannot claim calibrated directional
probabilities.

The profile explicitly lists statistical, XGBoost, LightGBM, LSTM and ensemble
as candidate families, but does not force any family to participate. Context is
available to PI-06/PI-07 inference and optional training matrices; the statistical
family continues to operate without fundamental context. PI-06/PI-07 training
matrices now pass the instrument's verified calendar into PI-02 label
construction, so their daily swing/long labels use the same session rules.

## 10. Cross-horizon consistency results

`cross_horizon` reports direction, return, uncertainty, target, validity,
freshness, model and disagreement for every available horizon. It distinguishes:

- `aligned` — all actionable directions agree;
- `mixed` — disagreement without an up/down opposition;
- `divergent` — both up and down exist;
- `insufficient` — fewer than two actionable horizons.

It computes swing-versus-long mean returns, `short_long_divergence`, return
dispersion and model disagreement. Divergence is not treated as an error and is
not forced into one direction. A generated forecast whose `target_at` does not
equal the verified expected target is marked failed for that horizon and is
excluded from cross-horizon comparison; a mismatched target is never averaged as
if it were the same horizon. The deterministic fixture deliberately produces
three upward swing forecasts and two downward long forecasts; the result is
`divergent=True`, short mean positive and long mean negative.

## 11. Uncertainty and interval results

PI-10 preserves the PI-08 interval, uncertainty class, calibration status,
probability status and constituent disagreement. It does not derive a narrower
interval or a confidence percentage. Long-horizon uncertainty is visibly
different in the fixture (`high` versus `medium`), and interval coverage is
evaluated separately for each horizon. Missing or uncalibrated probabilities stay
`null`/`unavailable`.

## 12. Forecast lifecycle integration

- Existing PI-09 cycle identity, immutable versions, revisions and outcomes are
  unchanged.
- Target semantics are now consistent across PI-09 scheduling, PI-05 targets,
  PI-02 labels and PI-10 expected-target validation.
- Swing/long initial versions no longer wait for the target session open.
- Longer horizons use horizon-appropriate confirmation leads rather than the
  intraday T−60m/T−1m cadence.
- Every forecast remains an immutable PI-01/PI-09 record; PI-10 overwrites
  nothing.
- The 90d target in an uncovered year fails before generation with an explicit
  reason.
- The PI-09 scheduler accepts an optional context provider. When supplied by the
  composition root, every lifecycle-generated forecast receives the same PI-10
  point-in-time fundamentals, news, benchmark, sector and context bars. A
  provider failure is recorded but cannot prevent the lifecycle checkpoint from
  running; it is not converted into a fabricated forecast input.

## 13. Autonomous trading integration

The result exposes direction, expected return, interval, uncertainty, horizon,
model evidence, fundamental/sector/benchmark context and validity. Every result
and the status/contract declare:

- `risk_authority = NM-08`
- `execution_authority = NM-10`
- `forecast_authorizes_action = false`
- no portfolio, ledger, fill or order mutation
- paper-only operation

PI-10 creates no short position and no forced signal. A downward long-term
forecast is advisory evidence for the existing decision/risk architecture, not a
trade. Existing PI-08/PI-09 and autonomy regression tests pass unchanged in
authority semantics.

## 14. API and frontend integration

Additive authenticated routes:

- `POST /market/forecasts/horizons`
- `GET /market/forecasts/horizons/status`
- `GET /market/forecasts/horizons/performance`
- `GET /market/forecasts/horizons/{instrument_id}/context`
- `GET /market/forecasts/horizons/{instrument_id}`

They are present in FastAPI, `ApiSurface.default()`, `ROUTE_CONSUMERS` and the
generated `docs/API.md`. The market route count increased from 232 to 237; the
generated reference is in sync at 644 total routes.

The React Forecasts page adds a swing/long card for selecting an instrument,
generating all five horizons, loading stored forecasts, showing sector/benchmark/
event context, cross-horizon divergence and horizon-separated accuracy. Its
latest-forecast badge is driven by `available_horizons`, and an all-abstained
response is shown as informational with generated/abstained/failed counts rather
than as an error. It labels forecasts as uncertain and not guaranteed returns.
The production web build (`tsc -b && vite build`) passed.

## 15. Tests and actual results

Commands were run with `PYTHONPATH=src` from `novamarket/`.

| Check | Result |
|---|---|
| New `tests/test_novamarket_outlook.py` | **20 passed** |
| `tests/test_novamarket_scheduler.py` | **24 passed** |
| `tests/test_novamarket_datasets.py` | **25 passed** |
| PI-10 + scheduler + datasets together | **71 passed** |
| Statistical, ensemble, gradient, LSTM and market API suites | **162 passed, 259 subtests passed** |
| Prediction, features, autonomy and forecasting suites | **144 passed, 35 subtests passed** |
| NovaMarket composition/isolation suite (`test_novamarket.py`) | **87 passed, 53 subtests passed** |
| API-reference sync and web API suites | **81 passed, 720 subtests passed** |
| Full mypy, normal platform | **629 source files, no issues** |
| Full mypy, `--platform win32` | **629 source files, no issues** |
| API reference sync | **644 routes, regeneration check passed** |
| Web production build | **passed** (existing chunk-size/dynamic-import warnings only) |

These are the checks requested for this verification pass: the phase suite first,
then only the dependency and integration suites affected by the fixes. The full
repository suite was not run.

The new tests cover all five horizons, session/calendar targets, fundamental
publication/acquisition timestamps, point-in-time filtering, full provenance,
registered and unregistered sector/benchmark mappings, canonical NM-04
verification states, retracted/expired event status, news dedupe/verification,
failure isolation, cross-horizon alignment/divergence, latest as-of cutoff,
canonical autonomy evidence, mismatched generated targets, invalid
quality/freshness input, per-horizon price/direction/interval metrics,
configured small-sample reliability, route registration, and the actual FastAPI
app both unseeded and seeded with real model evidence. A scheduler test proves
PI-10 context is forwarded to the existing generation call and that a
context-provider failure is recorded without blocking the checkpoint. Dataset
tests prove a missing verified session makes a swing label unavailable, and that
a missing first verified 30d target session does not fall through to the next
observed row.

The unseeded actual app test registers a real instrument, reads PI-10 status,
rejects `20m`, invokes all five horizons through the real application, reads
latest and context routes and confirms failure isolation/authority. That app
holds no trained constituent evidence, so every horizon abstains there; the 90d
target also falls outside verified 2026 calendar coverage. Both are explicit and
did not disable the other horizons.

The seeded application test
(`test_pi10_actual_app_generates_and_stores_with_seeded_evidence`) shows that
this abstention is about missing evidence, not a missing capability. It trains
real statistical models through `POST /market/forecasts/train` over HTTP (ARIMA
and historical-return on a 1d target at `as_of` 2026-09-15, with at least five
validation samples), then calls `POST /market/forecasts/horizons` for `1d`. The
response reports `generated: 1`, `abstained: 0`, `failed: 0`,
`target_aligned: true` and a real `ens_` contract whose target is the next
verified session close (`2026-09-16T10:00:00+00:00`).
`GET /market/forecasts/horizons/XNSE:PI10VERIFY` returns that horizon as
`stored` with the same `forecast_id`, and `GET /market/forecasts/{forecast_id}`
returns the persisted 1d contract with `data_quality: "valid"`,
`risk_authority: "NM-08"` and `execution_authority: "NM-10"`. The actual isolated
app therefore does not only abstain: it abstains when it has no eligible
evidence, and it generates and stores a 1d forecast when it does. A second seeded
test seeds five 30-day validation origins and generates and stores a real `30d`
forecast over the same routes.

`30d` remains an explicit abstention in the app's default state, because only
three validation samples are available for it and the policy requires five
(`min_validation_samples = 5`). It is reported per horizon with that reason and
does not affect the horizons that do generate.

## 16. Defects fixed

1. **Swing target was calendar-day based.** 3d/5d training, PI-05 targets and
   PI-09 scheduling now count verified trading sessions.
2. **Long/swing initial checkpoint was target-session open.** Initial versions
   now start at cycle creation, allowing a 30/90-day lifecycle to exist for its
   full duration.
3. **Long-horizon confirmation reused intraday cadence.** 3d/5d/30d/90d now have
   explicit one-, two-, seven- and fourteen-day confirmation leads and long final
   leads.
4. **Fundamentals/news had no point-in-time orchestration.** Added timestamp
   validation, future filtering, restatement selection, dedupe, verification
   filtering and unit/provenance preservation.
5. **Sector/benchmark context existed in PI-04 but was not passed by the
   forecast services.** Added optional context plumbing through PI-08, PI-06 and
   PI-07 inference and training matrices.
6. **A horizon could generate against an unverified target.** Expected target is
   now validated first; only that horizon fails.
7. **Accuracy could be blended across unrelated horizons.** PI-10 exposes only
   per-horizon metrics and explicitly refuses a combined number.
8. **A time-dependent scheduler API test used a fixed past target.** The test
   now derives the next weekday target, preserving deterministic behavior.
9. **PI-10 advertised a fabricated feature-profile version.** The profile now
   reports the real PI-04 `FEATURE_SET_VERSION` (`pi04-feature-set-1.0`).
10. **Data quality alone could mark a news row verified.** Verification now
    requires an explicit accepted state; quality is only an additional gate.
    The canonical NM-04 `officially_confirmed` and
    `multi_source_corroborated` states are handled, and retracted/expired
    lifecycle status overrides an old confirmation.
11. **A present but unregistered sector/benchmark mapping was treated as an
    index.** Mappings must now resolve to canonical registered instruments;
    otherwise the context is explicit and no bars are substituted.
12. **Fundamental units were reduced to a synthetic summary.** The full
    provenance payload, including units, is now preserved in the context.
13. **`latest` could return a forecast generated after the requested cutoff.**
    Stored forecasts are now filtered by `generated_at <= as_of`.
14. **PI-10 autonomy evidence used a duplicate schema.** It now returns the
    canonical `DecisionForecastEvidence` contract and refuses non-final
    forecasts.
15. **A generated target mismatch could still feed cross-horizon analysis.**
    A mismatched `target_at` now fails that horizon and excludes it from the
    comparison; authority flags remain present on failure results.
16. **The configuration small-sample threshold was ignored.** PI-10 reliability
    now follows the instance's `small_sample_size`.
17. **Lifecycle-generated forecasts did not receive PI-10 context.** The PI-09
    scheduler now forwards optional PI-10 fundamentals/news/sector/benchmark/
    peer context into the existing generation path, and isolates provider
    failures.
18. **Daily training labels counted dataset rows, not verified calendar
    sessions.** A missing swing session or a missing first verified 30d/90d
    target session now makes the label unavailable instead of shifting the
    target forward to the next observed row. PI-06/PI-07 training matrices now
    supply the instrument's verified calendar to that label builder.
19. **The React latest badge and abstention toast misreported state.** The badge
    now checks `available_horizons`, and an all-abstained generation is an
    informational result with counts, not an error.
20. **`docs/API.md` was stale for the PI-05–PI-10 forecast routes.** It was
    regenerated from the shared registries and the 644-route check now passes.
21. **The route-namespace pin was stale.** `test_novamarket.py` still expected
    258 `/market` decorators; the application has 272 unique registrations
    (including the PI-10 horizon routes and the earlier PI-05–PI-09 additions).
    The pin was updated to the actual non-duplicated count and the suite passes.
22. **Invalid quality/freshness strings escaped as a 500.** Supplied fundamental
    and news mappings now translate unknown enum values into
    `OutlookValidationError`, which the existing forecast route maps to 422.

## 17. Remaining limitations

- **Unpublished 2027 NSE holiday calendar.** Verified coverage remains 2026.
  NSE had not published its 2027 trading-holiday circular as of 2026-11-16
  (external checks report no 2027 list yet; the 2026 circular was issued in
  December 2025). A 90d target from mid-November 2026 falls in February 2027 and
  is therefore refused explicitly. 90d forecasts were verified with a covered
  cutoff (2026-09-15 → 2026-12-14). No 2027 holiday is fabricated or approximated.
- No live-market accuracy evidence is claimed. All generation tests use
  deterministic market/model fixtures. The actual isolated app abstains by
  default because it holds no eligible constituent evidence, not because it
  cannot generate: once real statistical models are trained through the HTTP
  training route, the seeded application test generates and stores a `1d`
  forecast end to end. `30d` stays an explicit abstention in that default state
  because it has only three validation samples, below the five the policy
  requires.
- PI-10 parses validated fundamental evidence but adds no filing provider or
  scraper. Instruments without validated fundamentals operate with missing
  fundamental features.
- Sector/benchmark/peer context requires explicit instrument metadata. A
  free-text sector name is not converted into a mapping.
- The PI-02 label builder keeps its historical row-count fallback for direct
  callers that supply no verified calendar. Production dataset and PI-06/PI-07
  training paths supply one; without it, session verification cannot be claimed.
- Probability outputs remain withheld unless the existing ensemble calibration
  supports them.
- The React card was type-checked and production-built, but was not visually
  inspected in a browser during this verification pass.
- The existing automatic scheduler planner remains the bounded 20-minute intraday
  lifecycle from PI-09. Swing and long-term cycles are available through the
  authenticated `POST /market/forecasts/cycles` surface, which accepts all five
  PI-10 horizons and uses the same PI-09 lifecycle; this pass did not change the
  auto-planner's 20-minute default because that is pre-existing PI-09 behavior.

## 18. Final acceptance verdict

**PASS (scoped).**

All five horizon implementations are complete and reuse the existing prediction
architecture. Swing and long target semantics are correct and consistent across
scheduling, training and evaluation. Fundamental/news handling is point-in-time
and explicit, sector/benchmark context is validated, uncertainty and
cross-horizon divergence are represented honestly, immutable versions and
target-time outcomes remain PI-01/PI-09 responsibilities, and NM-08/NM-10 retain
sole financial authority. The remaining scope limitations are the external 2027
NSE holiday calendar, which prevents a 90d target that crosses into 2027 from
being verified today, and the pre-existing automatic planner's 20-minute
intraday default (all five horizons are schedulable through the existing cycle
API). No work on PI-11 was started, and no Git staging, commit, push, tag or
merge was performed.
