# PI-20 — Prediction Intelligence Dashboard and Decision Explainability

**Status:** PASS, verified by the Python suite and a browser audit of all thirteen views; limitations in section 21  
**Branch:** `feature/novamarket`  
**Phase:** PI-20  
**Last verification audit:** 20 November 2026 (accessibility, twelve-width responsive and performance audit)  
**Scope:** Read-only prediction intelligence composition, explanation and replay. No PI-21 work was started.

---

## 1. Initial repository assessment

The working tree was inspected on `feature/novamarket` before implementation. Existing user changes were preserved. No file was staged, committed, pushed, tagged, merged or published, and no release was created.

PI-01 through PI-19 completion reports were inspected and their status was verified as PASS before PI-20 work began. The existing NovaMarket packages, API registry, route-consumer contract, React pages, shared chart primitives, hash navigation, operator authentication and NM-08/NM-10 boundaries were inspected rather than replaced.

Relevant prerequisites found and reused:

- PI-04 features and market-regime detection.
- PI-05/PI-06/PI-07/PI-08 statistical, gradient-boosting, LSTM and ensemble forecasts.
- PI-09 durable intraday forecast lifecycle.
- PI-10 swing and long-term forecast schedules.
- PI-11 prediction evaluation and backtesting.
- PI-12 autonomous decision integration.
- PI-14 reward and policy evaluation.
- PI-15 RL training and policy artifacts.
- PI-16 adaptive-learning and regime intelligence.
- PI-17 multi-agent decision cycles and evidence.
- PI-18 portfolio optimization output.
- PI-19 financial research and evidence provenance.
- The application-wide event bus and `GET /events/stream` SSE channel.
- Existing operator authentication and autonomy authorization.

No prerequisite failure was found. No second prediction engine, frontend framework, event bus or navigation system was created.

## 2. Existing frontend and API components reused

The dashboard is one page inside the existing React application:

- `web/src/pages/PredictionIntelligence.tsx` for the workspace.
- `web/src/App.tsx` route `predictionintel`.
- `web/src/nav.ts` title and navigation entry.
- Existing `ui.tsx` cards, badges, table, empty/error/loading and page primitives.
- Existing `charts.tsx` chart primitives for price, volume, forecast, comparison and timeline views.
- Existing `useApi` hook, extended with an optional event-driven `refreshKey`.
- Existing hash navigation and API base/token helpers.
- Existing FastAPI application, `ApiSurface`, `ROUTE_CONSUMERS`, authentication dependencies and error mapping.
- Existing PI-04 to PI-19 service methods and normalized source payloads.

Backend implementation is isolated in `src/novacontrol/novamarket/dashboard/` and wired through lazy accessors on `NovaMarketService`. Financial calculations and authoritative prediction/decision logic were not moved into React.

## 3. Dashboard architecture

The package has four modules:

- `models.py` — filters, bounded-query helpers, status constants, authority contract and preference models.
- `store.py` — isolated SQLite presentation store (`pd_dashboard_meta`, `pd_dashboard_configs`) supporting an in-memory path.
- `service.py` — composition, normalization, comparison, explainability, replay and preference orchestration.
- `__init__.py` — public exports.

`DashboardService` receives the live `NovaMarketService`; it reads existing stores and contracts. It does not import NM-07 accounting mutations, NM-08 risk decisions or NM-10 execution commands. It has no financial tables and no order/ledger/position/fill write path.

All list operations are server-side bounded with a limit cap. Replay, instrument history, lifecycle, decisions and research use bounded queries rather than loading complete archives. Missing values are carried as unavailable or as a status plus empty rows, never silently as zero.

Availability normalization is explicit: `AVAILABLE`, `PARTIAL_DATA`, `STALE`, `INSUFFICIENT_EVIDENCE`, `UNAVAILABLE`, `FAILED`. Every response identifies its read-only authority contract and source freshness where available.

## 4. Main prediction dashboard implementation

The Overview view composes:

- Market context, benchmark observations available from stored data, current regimes, volatility and market events.
- Forecast counts: active, eligible instruments, upward, downward, neutral, uncertain, awaiting confirmation, expired and stale/invalid.
- Model inventory, active versions, ensemble status, health, recent performance and disagreement.
- Decision counts for BUY, SELL, HOLD, WAIT, NO_ACTION and evidence-needed recommendations.
- Separate labels for forecasts, research recommendations, autonomous decisions and paper-execution outcomes.

The view uses actual source collections. An empty isolated database correctly renders `INSUFFICIENT_EVIDENCE` with an explanation, rather than a fabricated market summary. Deterministic fixtures exercise populated overview and count behavior.

## 5. Instrument analysis

The Instrument view displays symbol, name, exchange, latest stored price, price timestamp, freshness, market status where supplied, regime, sector and research status.

It provides bounded OHLCV history with volume, moving averages and available technical/regime annotations. Corporate-event and news markers are displayed only when stored records exist. Missing candles or observations are not synthesized. Delayed or historical data is identified through its timestamp and freshness status.

Instrument selection drives the matching forecast, horizon and research reads; it does not invent an instrument or substitute a different symbol.

## 6. Multi-model and multi-horizon visualization

The Forecasts view renders only models that exist and returned valid outputs, including naive/moving-average/exponential-smoothing baselines, ARIMA, XGBoost, LightGBM, LSTM and ensemble where present. Rows preserve model name/version, horizon, reference price, predicted price, expected return, direction, interval, calibrated probability when available, uncertainty, generation time, target time, freshness and validity.

The Comparison view restricts comparison to compatible instrument, reference timestamp, horizon, evaluation period and dataset scope. Incompatible forecasts are excluded or shown as not comparable. Historical accuracy and regime-specific performance are displayed only in the scope they were evaluated in; no model is presented as universally best from one metric or one period.

The Horizons view separates the established horizon families: intraday 5/15/20 minutes and 1 hour, swing 1/3/5 trading days and long-term 30/90 calendar days. The dashboard accepts the legacy `60m` spelling as an alias for the PI-09 `1h` contract and normalizes it before filtering. Reference price, target, expected move, return, interval, uncertainty and model disagreement remain per horizon. Targets from different horizons are not combined into one price figure, and disagreement between them is shown explicitly.

## 7. Uncertainty and performance analytics

Uncertainty is presented as separate fields:

- Calibrated directional probability, only when the source explicitly calibrated it.
- Prediction interval and its coverage where evaluated.
- Forecast uncertainty.
- Model disagreement.
- Data-quality status and freshness.
- Evidence completeness.

No uncalibrated confidence is converted into a probability. Green/red direction styling is not used as a substitute for statistical uncertainty; an explicit uncertain or insufficient-evidence state is available.

The Performance view uses PI-11 outputs:

- Regression: MAE, RMSE, MAPE where appropriate and sMAPE where appropriate.
- Directional: accuracy, precision, recall, F1 and confusion matrix.
- Probabilistic: Brier score, log loss, calibration results and interval coverage.
- Scope: horizon, instrument, regime, time and ensemble-versus-individual comparisons.

Each result carries evaluation period, sample size, dataset version, model version and evaluation timestamp where available. Statistically invalid or insufficient samples are labelled rather than ranked as meaningful. Forecast accuracy is visually and textually separated from trading profitability; no profitability claim is derived from prediction metrics.

## 8. Forecast lifecycle integration

The Lifecycle view reads PI-09/PI-10 lifecycle records and shows T−20, T−5, T−1 and target evaluation checkpoints for intraday forecasts, and the actual schedules for swing/long-term forecasts.

Each checkpoint exposes forecast ID, immutable version, target time, actual generation time, reference price, direction, predicted price, freshness, checkpoint status and confirmation result. Missing, delayed, stale and unavailable checkpoints are shown as such. A late checkpoint retains its actual generation time and is not relabelled as an earlier checkpoint.

## 9. Market-regime integration

The Regimes view reads PI-04/PI-16 detector output and shows current classification, secondary classification where supplied, the recorded detector score and its kind (never presented as a probability unless the source says it is calibrated), detection timestamp, transitions, observed duration where supported, volatility, trend strength, drift indicators and policy suitability.

Historical regime observations are charted/tabulated with insufficient-evidence states preserved. Predictions are not assigned to a regime the detector did not report. Performance can be inspected under the recorded regime scope without converting correlation into a regime claim.

## 10. Multi-agent decision visualization

The Decisions view reads PI-17 cycles and displays decision-cycle ID, instrument, exchange, horizon, timestamp, assigned agents, execution status, findings, supporting and contradictory evidence, uncertainty and missing/stale information.

The view covers the Market Research, Technical Analysis, Fundamental Analysis, Prediction Intelligence, Market Regime, Risk Analysis, Portfolio Intelligence, RL Policy Intelligence and Trading Decision agents. Evidence is classified as independent, derived, interpretation, hypothesis or unverified. Repeated interpretations of one source are not counted as independent evidence.

The Evidence view exposes source/provenance records for the selected cycle. An incomplete cycle is shown as incomplete; no agent finding is fabricated for a missing specialist.

## 11. Decision explainability evidence

The Explain view answers the six required questions from stored data:

- **What** — instrument, exchange, action, horizon and recommendation timestamp.
- **Why** — prediction, technical, fundamental, regime, news/event, portfolio and agent contributions that exist.
- **Evidence** — forecast outputs, indicators, research, historical evaluation and verified references.
- **Contradiction** — conflicting models, negative evidence, high uncertainty, risk restrictions, regime disagreement and missing data.
- **Limitations** — freshness, insufficient history, uncalibrated confidence, disagreement and missing financial information.
- **Aftermath** — later checkpoints, actual outcome, accuracy, decision outcome, paper-execution outcome and portfolio impact where recorded.

Prediction accuracy, decision quality, execution quality and financial performance remain separate. If evidence is absent, the explanation reports a limitation instead of generating a narrative.

Feature attribution is displayed only when the source explain endpoint actually returned computed feature importance/contributions. Model version, feature-set version, explanation method and timestamp are preserved; the presentation explicitly says the attribution is not causal proof. No SHAP value or feature contribution is fabricated.

## 12. Financial research integration

The Research view integrates PI-19 summaries: company profile, financial health, revenue/earnings trends, profitability, debt/liquidity, valuation, sector comparison, corporate events, news, thesis, bullish and bearish factors and research uncertainty.

Reported financial facts, derived estimates and AI interpretations are visually separated. Reporting periods, publication times, evidence references, verification status and research validity are preserved. A claim's source can be inspected through the evidence/provenance fields. Missing research is shown as unavailable/insufficient; the dashboard does not create a thesis or valuation.

## 13. Portfolio intelligence integration

The Portfolio view displays authoritative current value, available NC, the INR-equivalent value (recorded when supplied, otherwise the documented 1 NC = 1 INR convention), recorded current allocation, recommended allocation, sector exposure, concentration, diversification, risk indicators, optimization recommendations and rebalancing proposals from PI-18 and existing portfolio services.

The UI distinguishes current authoritative portfolio state, simulated state, experimental optimization output and proposed allocation change. It never writes portfolio accounting or submits a rebalance. The optimization recommendation remains advisory and cannot change NM-07 state or NM-08 approval.

## 14. RL and adaptive intelligence integration

The Policies view integrates PI-14/PI-15/PI-16 artifacts: experimental agent identity, algorithm, policy version, training status, training reward, validation and out-of-sample performance, regime suitability, drift, degradation indicators and retraining recommendations.

Experimental policies are labelled experimental. Training reward is never presented as portfolio profit. The dashboard exposes no activation, promotion, training start or autonomy change control. Adaptive/regime evidence is read-only and carries its source version.

## 15. Historical replay evidence

The Replay view accepts a past decision cycle and reconstructs its information set from the recorded cutoff. It returns historical market observations, available forecasts, research, regime, agent findings, portfolio state, risk restrictions, final recommendation and execution outcome where recorded.

Every source is filtered to its point-in-time cutoff; subsequently published news, research or financial facts are not presented as known. Original decision and evidence versions are retained. Recorded operational history is distinguished from reconstructed research simulation. The PI-20 test suite includes a point-in-time test that proves post-cutoff records are excluded.

## 16. Real-time update implementation

The page uses the existing app-wide `GET /events/stream` SSE channel rather than a new stream:

- One `EventSource` is opened with the existing auth token.
- It listens for existing NovaMarket forecast, prediction/lifecycle, regime, portfolio-valuation, data-freshness and market-event types, plus the completed-operation types added for this page: `novamarket.agent.cycle.completed`, `novamarket.research.run.completed`, `novamarket.optimization.recommendation.created` and `novamarket.policy.degraded`.
- A relevant event increments a revision; `useApi`'s new optional `refreshKey` refetches the active view.

Those four events are published by the operations themselves through the same `EventBus` and `MarketEventPublisher` used by every other NovaMarket event: the async API seam that awaited the synchronous PI-17 cycle, PI-19 research run, PI-18 optimization run or PI-16 degradation monitor announces the committed result (identifier, status and a non-actionable summary only). Reused idempotent cycles, pending runs and already-stored alerts are not re-announced, so an open view refetches once per real change.
- The page has no `refreshMs` timer and creates no polling loop.
- `vite.config.ts` now proxies `/events` to the same API origin as `/market` for local development.
- A dropped stream does not block the page; the browser reconnects on its own.

The isolated app preview showed `GET /events/stream → 200 (EventSource)` with no console errors. Live verification of the four new types used one real operation each against the running API while the page was open: an agent cycle, a research run, an optimization run and a new persistent-degradation alert. The SSE client received all four frames, and the Agent decisions, Research, Portfolio and RL & adaptive views each refetched its own endpoint exactly once and rendered the new record; a 12-second idle window on the same page produced zero requests and zero events, so the refresh is event-driven and not a poll. Free/delayed sources are never described as exchange-grade real-time data; timestamps and freshness are shown. Dashboard refresh does not bypass the PI-03 15-minute general market-data floor or the approved intraday path.

## 17. API and frontend integration

The HTTP surface adds 22 authenticated routes under `/market/intelligence/*`:

- Status and contracts.
- Overview.
- Instruments and instrument detail.
- Forecasts and model comparison.
- Multi-horizon summaries.
- Lifecycle.
- Performance.
- Regimes.
- Decisions, decision detail and evidence.
- Explanations and feature attribution.
- Research.
- Portfolio.
- Policies.
- Replay.
- Filters, save filters and configuration.

All routes are registered in `api/app.py`, `ApiSurface` and `ROUTE_CONSUMERS`, and `docs/API.md` was regenerated. The reference is in sync at **836 routes**. The page is reached at `#/predictionintel` through the existing navigation without a duplicate page for the same data.

Filters cover instrument, exchange, sector, regime, horizon, model, direction, validity, evaluation period, decision status, agent, portfolio and research status. Filter state is preserved in page/session state and can be saved server-side as presentation preferences.

## 18. Security and financial-isolation evidence

- All 22 endpoints require the existing server-side operator authentication; the PI-20 authorization test proves unauthenticated requests are rejected.
- The dashboard package has no NM-07 portfolio mutation, NM-08 risk decision, NM-10 order/fill or broker integration.
- The only SQLite tables it owns are dashboard preferences. Test coverage asserts that financial tables are absent.
- `authority_contract()` reports `read_only: true` and identifies NM-08 as sole risk authority and NM-10 as sole paper-execution authority.
- No route changes positions, ledger entries, paper fills, permissions, autonomy modes, production policies or brokers.
- UI visibility is not used as a security boundary; authorization remains server-side.
- Dashboard interactions cannot activate, promote or train an experimental RL policy.

## 19. Actual test counts and results

All commands were run from `novamarket/` with `PYTHONPATH="src;tests"`; frontend commands from `novamarket/web/`.

Re-run on 20 November 2026 for this audit:

| Check | Current result (20 Nov 2026) |
|---|---|
| `pytest tests/test_novamarket_pi20.py -q` | **49 passed** (44 earlier + 5 added by these audits) |
| `pytest tests/test_novamarket_api.py -q` | **64 passed, 349 subtests passed** |
| `pytest tests/test_web_platform.py -q` | **46 passed, 974 subtests passed** |
| `mypy` on `dashboard/` and `novamarket/service.py` | **Success, no issues (5 files)** |
| `python scripts/pi20_audit.py` (accessibility, responsive, performance) | **PASS** — 13 views, 279 controls, 12 widths, 13 API timings |
| `python scripts/pi20_live_walk.py` (13 views, interactions, every filter field, saved-filter reload, states, 5 widths) | **PASS** — 0 failures, 0 unexpected console errors, 0 page errors (re-run 21 Nov 2026 with the filter controls) |
| `npm run build` (`tsc -b && vite build`) | **Passed**; JS `982,753 B` (gzip `247,011 B`), CSS `92,270 B` (gzip `16,832 B`) |

Earlier batches, not re-run in this audit (recorded 17 November 2026): dependency/integration batch PI-17/PI-19/evaluation/outlook **125 passed**; regression batch PI-12/PI-16/PI-18/features/forecasting/workbench **234 passed, 35 subtests passed**.

`ruff` is not installed in this environment (`No module named ruff`), so the earlier ruff claim could not be re-run and is not re-asserted here. `tsc` runs as part of `npm run build` and passed.

### 19.1 Accessibility audit (20 November 2026)

Method: `scripts/pi20_audit.py` drives Chromium (Playwright, 1440x900) through every view and inspects the rendered DOM; results in `.ui_audit/pi20_audit.json`. This is an automated review, **not a WCAG conformance certification**. No screen-reader session was run.

| Check | Result |
|---|---|
| Views opened and audited | **13/13** (Overview, Instrument, Forecasts, Comparison, Horizons, Lifecycle, Performance, Regimes, Agent decisions, Research, Portfolio, RL & adaptive, Replay) |
| Controls inspected (form controls, buttons, links) | **279** |
| Tables inspected | **46** (0 missing header cells, 0 missing `scope`) |
| Contrast samples (rendered text nodes) | **1,925** (0 failures) |
| Unlabelled form controls | **0** |
| Buttons/links without an accessible name | **0** |
| Images without `alt` | **0** |
| Duplicate element ids | **0** |
| One `h1` per view; heading order | **13/13**; 0 skips after the fix below |
| Document language, skip link, `main` landmark, labelled `nav` | present on every view |
| Keyboard tab stops (8 presses from the top) | skip link first, 8/8 valid, 8/8 with a visible focus indicator |
| Tablist contract | 13 tabs, exactly one `aria-selected`, roving `tabIndex` (0/-1), every `aria-controls` resolves, panel `aria-labelledby` equals the active tab id |
| Tab keyboard behaviour | `ArrowRight` selects and focuses Instrument, `End` selects Replay, `Home` returns Overview |
| Console errors / page errors across the 13 views | **0 / 0** |

Two defects were found and fixed:

- **The RL & adaptive view crashed the workspace.** PI-14 `validation` evidence arrives as an object (`baselines`, `comparison_id`, `metrics`, `policy`, `split`, ...) and the table rendered it directly as a React child, so React threw and unmounted the whole page - all thirteen tabs disappeared. The table now formats structured payloads through a `cell()` helper (`JSON.stringify` for objects, the scalar otherwise) and every policy column uses it. A regression test pins the behaviour.
- **Heading order skipped a level** on Instrument, Horizons and Replay, whose first content is an empty state (`h1` to `h3`). `EmptyState` now accepts `level={2}` for section-level empty states; those three views (plus the shared "No data yet" gate) use it.

One probe artefact was corrected rather than "fixed" in the product: the first audit counted the eight inputs inside the collapsed "More filters" `<details>` as unlabelled because `innerText` is empty for a closed details body. They are wrapped by a `<label>` that names them, and the accessibility tree excludes the closed content, so the probe now skips closed-`details` content and reads label text with `textContent`. After that correction the remaining findings were the three real heading skips above.

### 19.2 Responsive behaviour (twelve widths, same audit)

Each width was applied to the live page and measured after layout settled; `documentScrollWidth` is the widest laid-out content.

| Width (px) | Document scrollWidth | Horizontal overflow | Tab strip | Table wrapper |
|---|---|---|---|---|
| 1920 | 1920 | none | 1 row, 34 px | fits (1482/1482) |
| 1440 | 1440 | none | 1 row, 34 px | fits |
| 1366 | 1366 | none | 1 row, 34 px | fits |
| 1280 | 1280 | none | 1 row, 34 px | fits |
| 1024 | 1024 | none | 1 row, 34 px | fits |
| 900 | 900 | none | 2 rows, 71 px | fits |
| 820 | 820 | none | 2 rows, 71 px | fits |
| 768 | 768 | none | 2 rows, 71 px | fits |
| 600 | 600 | none | 2 rows, 71 px | scrolls inside (680 in 568) |
| 480 | 480 | none | 3 rows, 107 px | scrolls inside |
| 390 | 390 | none | 3 rows, 107 px | scrolls inside |
| 320 | 320 | none | 4 rows, 144 px | scrolls inside |

At every width: 0 elements extend past the viewport outside a scrollable container, all 13 tabs remain present and unclipped, and the page `h1` stays visible. Wide tables scroll inside their `.table-wrap` from 600 px down, so the page itself never scrolls sideways. This is a CSS-viewport measurement at 100% zoom; browser zoom and OS text scaling are separate and were not measured.

### 19.3 Performance measurements (20 November 2026)

Environment: Vite dev server on `127.0.0.1:5173`, FastAPI on `127.0.0.1:8011`, real local database (market data `STALE`, 21 active forecasts, a 30-sample evaluation). Because this is the dev server, first paint includes on-the-fly module transforms; the production bundle sizes below are the build output.

| Measurement | Result |
|---|---|
| TTFB | 6.8 ms |
| DOMContentLoaded | 157.1 ms |
| `load` event | 164.5 ms |
| First paint | 188 ms |
| First Contentful Paint | **284 ms** |
| Page ready in the harness (network idle, 50 requests) | 2.14 s |
| Tab switch to settled panel (13 views) | **35.9-50.3 ms** (budget 4,000 ms) |
| Production JS bundle | 982,753 B raw / 247,011 B gzip |
| Production CSS bundle | 92,270 B raw / 16,832 B gzip |

API latency (3 samples per endpoint, same real database; dashboard budget 2,000 ms):

| Endpoint | Status | Median | Max | Response size |
|---|---|---|---|---|
| `status` | 200 | 16.12 ms | 59.74 ms | 880 B |
| `contracts` | 200 | 15.99 ms | 16.29 ms | 1.2 KB |
| `overview` | 200 | 70.39 ms | 687.33 ms (first, cold) | 71 KB |
| `forecasts` | 200 | 22.54 ms | 31.37 ms | 31 KB |
| `compare` | 200 | 11.34 ms | 25.85 ms | 38 KB |
| `lifecycle` | 200 | 32.33 ms | 41.71 ms | 52 KB |
| `performance` | 200 | 43.09 ms | 48.94 ms | 40 KB |
| `regimes` | 200 | 4.95 ms | 16.59 ms | 30 KB |
| `decisions` | 200 | 15.80 ms | 20.63 ms | 3 KB |
| `research` | 200 | 11.77 ms | 24.45 ms | 208 KB |
| `portfolio` | 200 | 13.42 ms | 27.72 ms | 9 KB |
| `policies` | 200 | 16.68 ms | 29.11 ms | 851 KB |
| `filters` | 200 | 13.31 ms | 27.31 ms | 401 B |

No endpoint exceeded the 2,000 ms budget; the only slow sample was the cold `overview` call at 687 ms, which the next two samples at 70 ms show is first-call warm-up. Two consecutive audit runs both returned PASS. Two observations, not failures: `policies` returns 851 KB and `research` 208 KB for `limit=50`, because they carry nested stored artifacts and evidence; both are bounded reads, and neither is paginated further.

### 19.4 Mandatory test-area traceability (33 areas)

The 33 areas below are the PI-20 acceptance surface (composition, views, uncertainty, lifecycle, performance, regimes, agents, explainability, research, portfolio, RL, replay, filters, API, frontend contract). Each is evidenced by a named test in `tests/test_novamarket_pi20.py`; the file's 49 tests map onto them without gaps, and all 49 pass.

| # | Area | Evidence |
|---|---|---|
| 1 | Read-only status and authority contract | `test_status_and_contract_are_read_only`, `test_authority_contract_blocks_every_mutation` |
| 2 | Isolated preference store (only `pd_` tables) | `test_preferences_persist_in_an_isolated_table` |
| 3 | Store failure reported, not raised | `test_store_failure_is_reported_not_raised` |
| 4 | Overview counts and forecast/decision separation | `test_overview_counts_recorded_forecasts_and_decisions` |
| 5 | Missing source is `FAILED`, never zero | `test_missing_source_is_failed_not_zeroed` |
| 6 | Instrument price, timestamp, freshness, OHLCV | `test_instrument_keeps_price_timestamp_freshness_and_chart` |
| 7 | Missing quote is not invented | `test_instrument_without_a_quote_does_not_invent_one` |
| 8 | Invalid instrument rejected | `test_invalid_instrument_is_rejected` |
| 9 | Uncalibrated confidence is not a probability | `test_uncalibrated_confidence_is_not_a_probability` |
| 10 | Forecast interval, uncertainty and versions | `test_forecast_keeps_interval_uncertainty_and_versions`, `test_real_forecast_contract_fields_are_mapped` |
| 11 | Comparison only within compatible scope | `test_compare_only_joins_matching_reference_targets`, `test_compare_does_not_treat_a_single_model_as_comparable` |
| 12 | Horizon families separated, no merged target | `test_horizons_are_separate_and_never_merged` |
| 13 | `1h` contract with `60m` alias | `test_one_hour_horizon_matches_the_prediction_contract` |
| 14 | Filter validation and bounded pagination | `test_filters_are_validated_and_paginated` |
| 15 | Lifecycle checkpoints, late ones not relabelled | `test_late_checkpoints_are_explicit_and_not_relabelled` |
| 16 | Performance scope, no profitability claim | `test_metrics_keep_scope_and_do_not_claim_profitability` |
| 17 | PI-11 nested metrics and evaluation period | `test_pi11_nested_metrics_and_evaluation_period_filter` |
| 18 | Insufficient sample labelled | `test_zero_sample_is_insufficient_evidence` |
| 19 | Regime classification, score kind, transitions | `test_regimes_keep_detection_time_and_transitions` |
| 20 | Unknown regime stays uncertain | `test_unknown_regime_stays_uncertain` |
| 21 | Decision history pagination and flattening | `test_decision_history_is_paginated_and_flattened` |
| 22 | Agents, findings, evidence and conflicts preserved | `test_decision_preserves_agents_evidence_and_conflicts` |
| 23 | Agent filter reports the true filtered total | `test_decision_agent_filter_scans_paged_history` |
| 24 | Explanation has all six sections | `test_decision_explanation_has_all_six_sections` |
| 25 | Attribution only when computed; provenance kept | `test_attribution_only_uses_computed_contributions`, `test_ensemble_constituents_and_lstm_global_importance_are_not_mixed`, `test_attribution_is_explicitly_unavailable_for_naive` |
| 26 | Missing decision is a not-found | `test_missing_decision_is_a_not_found` |
| 27 | Research thesis, evidence and provenance | `test_research_preserves_thesis_evidence_and_uncertainty`, `test_research_evidence_timeline_keeps_provenance`, `test_thesis_factors_render_readable_text_not_raw_json` |
| 28 | Portfolio authoritative vs experimental state | `test_authoritative_and_experimental_states_are_separate` |
| 29 | RL experimental, reward not profit, structured payloads render | `test_policies_are_experimental_and_not_profit`, `test_structured_policy_payloads_are_rendered_as_text` |
| 30 | Replay excludes post-cutoff records | `test_replay_excludes_future_forecasts_evidence_news_and_bars` |
| 31 | Replay separates recorded history from reconstruction | `test_replay_keeps_recorded_history_separate` |
| 32 | Filter persistence, ordering and HTTP round trip | `test_filters_are_presentation_only_and_persist`, `test_filter_dates_are_ordered`, `test_filter_preset_round_trip_through_http` |
| 33 | Route surface, authentication and frontend contract | `test_every_dashboard_route_is_declared_and_non_render`, `test_dashboard_routes_require_auth`, `test_status_and_overview_are_available`, `test_frontend_uses_the_shared_event_stream_without_polling`, `test_operation_events_are_declared_on_both_sides`, `OperationAnnouncementTests`, `test_frontend_declares_thirteen_accessible_views`, `test_top_level_empty_states_use_a_section_heading`, `test_malformed_instrument_filter_never_reaches_the_api` |

The browser audit in 19.1-19.3 is the evidence for the presentation requirements the Python suite cannot reach: the rendered accessibility tree, keyboard order, tab contract, layout at twelve widths and measured latency. It is a scripted run, reproducible with `python scripts/pi20_audit.py`, not a certification.

Actual application integration:

- The audit ran against the running application: Vite on `127.0.0.1:5173`, API on `127.0.0.1:8011`, real local database.
- All 13 views were opened in the browser, not just Overview and Research; the workspace held 21 active forecasts, a 30-sample evaluation, a portfolio value and stored research, and the views that had no records (Regimes, and Replay before a cycle is chosen) honestly showed `INSUFFICIENT EVIDENCE` or an empty state.
- `GET /events/stream` remains the only update channel; the page held no polling loop (pinned by test).

### 19.5 Live walk of all thirteen views (20 November 2026)

`scripts/pi20_live_walk.py` drove the running app (Vite on 5173, API on 8011, real local database) and opened every view, performed the view's own interactions, and measured layout at five widths. Result: **PASS**, artifact `.ui_audit/pi20_live_walk.json`.

| View | Observed data state | Interaction exercised | Rows / tables |
|---|---|---|---|
| Overview | `STALE`; banner "No regime call" (detector uncertain) | — | 18 / 2 |
| Instrument | empty state "Select an instrument" before lookup; `STALE` after `XBOM:ADANIENT` | typed a symbol and pressed Open instrument; price, OHLCV chart, indicators and research loaded | 0 → 4 / 2 |
| Forecasts | `AVAILABLE`; per-row `Stale` badges preserved | — | 27 / 1 |
| Comparison | `AVAILABLE`; "Scoped comparison" and "Reference mismatch" banners | — | 27 / 16 |
| Horizons | empty "Choose an instrument"; then the swing family table | set the shared instrument filter | 0 → 1 table |
| Lifecycle | `AVAILABLE`; "Recorded checkpoints" banner | — | 80 / 20 |
| Performance | `AVAILABLE`; "Accuracy is not profitability" banner | — | 76 / 1 |
| Regimes | insufficient evidence: "Regime unknown, no narrative regime is invented" | — | 3 / 2 |
| Agent decisions | `AVAILABLE` | selected a cycle; detail, evidence and explain panels loaded | 6 → 4 tables |
| Research | `AVAILABLE`; run auto-selected; "No corporate events" and "No stored news" empty states | selected a run; company, financials, valuation, sector, thesis loaded | 23 / 2 |
| Portfolio | `AVAILABLE`; "Authoritative vs advisory" banner; "No holdings" and "Position weights unavailable" | — | 0 rows |
| RL & adaptive | `AVAILABLE`; "Experimental, advisory-only" banner | — | 4 / 1 |
| Replay | empty "Choose a past decision" | reconstructed `mac_fa06f1bd4d584295bc9b2c9d7992af77`: 8 sections, "Recorded history vs reconstruction" banner | 0 → 2 tables |

States were observed as follows (each recorded in the artifact):

- **Loading** — Overview and Agent decisions were held by network latency; both rendered the `aria-busy` block with the "Reading NovaMarket…" label and skeleton rows, then content.
- **Error** — a forced 500 on `forecasts` rendered `role="alert"` "Could not load this surface" with the server message and a Retry control; after the request was restored the view loaded normally (1 table). An unknown replay cycle produced a not-found error block naming the cycle. A malformed instrument symbol now produces a local "Instrument not searched" banner and issues no request.
- **Empty** — real empty states on Instrument, Horizons and Replay, plus "No holdings"/"Position weights unavailable" on Portfolio, "No corporate events"/"No stored news" on Research, and an induced empty `decisions` payload that rendered "No agent decisions".
- **Stale** — Overview reports `STALE` with the stored/delayed qualifier and Instrument detail reports `STALE`/delayed-historical for the same quote; neither is labelled live.

Responsive: all 13 views at **1440, 1024, 768, 480 and 390 px** (65 measurements). At every width `documentScrollWidth` equalled the viewport, no element extended past the viewport outside a scroll container, all 13 tabs were present, and the panel rendered a heading, empty state or error block.

Defects found and fixed by the walk:

- **Research widened the page to 2650 px at a 1440 px viewport.** The bullish/bearish factors are PI-19 objects; the page fell back to `JSON.stringify`, producing 563-character unbroken strings that forced horizontal scrolling on the whole document (all other views were clean). Factors now render through `FactorList` as the recorded `rationale` plus an evidence-reference count, and `styles.css` wraps long tokens in `.plain-list li`.
- **One malformed instrument symbol broke every view.** The Instrument search pushed its raw text into the shared filter, so `NOT A SYMBOL` made all 13 views answer 422 "instrument_id must be a MIC-qualified symbol". The search and the shared filter now validate the MIC shape locally, show an inline message, and never send an invalid filter; the walk confirms the other views keep loading after the attempt.
One harness note, not a product defect: re-clicking the already-active tab does not remount the view, so recovery after a failed request is the in-page Retry control or a tab switch; the walk switches away and back.

Console: the only two console errors were the induced 500 and 404 responses; there were no unexpected console errors and no page errors.

### 19.6 Filter drawer and saved filters (21 November 2026)

The 20 November walk expanded "More filters" and counted its inputs but never typed into it, and never pressed Save filters — the two PI-20 controls left unexercised. The walk now does both against the same live app and records the outcome in the same artifact, under `filter_controls`.

- **Collapsed drawer fields.** All eight inputs behind "More filters" were expanded, filled and read back: Exchange `XNSE`, Sector `Technology`, Regime `bull`, Evaluation start `2026-10-01`, Evaluation end `2026-11-01`, Agent `agent-walk`, Portfolio `pf-walk`, Research status `completed`. Every input held exactly the value typed; applying all eight produced no error block and no console error, and the fields were cleared afterwards.
- **Saved filters.** With instrument `XBOM:ADANIENT` and horizon `5d` in the toolbar, "Save filters" issued `PUT /market/intelligence/filters` and answered `200`; the stored record then read `saved: true` with `instrument_id: "XBOM:ADANIENT"` and `horizon: "5d"`, and after a full page reload the Instrument input and Horizon select still held those values. The walk wrote the pre-walk filters back (`200`) and reloaded again to confirm the toolbar returned to its previous (empty) filters, so the rest of the run measured the same page it did before.

Result: **PASS** — 0 failures, 0 page errors; the only console errors were the induced 500 and 404. Artifact regenerated 21 November 2026.

Residue worth knowing: the first extended run found no saved filter and the preference store has no delete route, so the restore left a `default` preset holding empty filters; later runs found that preset and restored to it. Its values are the defaults, so the dashboard behaves as it did before.

## 20. Defects fixed

Initial implementation fixes:

- The dashboard preference save path ignored an explicitly supplied filter name when the direct caller left the default; it now honors the payload's name.
- Windows SQLite test cleanup did not close the connection reliably; the test fixture now closes it explicitly.
- The first frontend integration used timer polling. It was replaced with the existing authenticated SSE channel, a shared `useApi` refresh key and a Vite `/events` proxy; a regression test now forbids `refreshMs` in the PI-20 page.

Audit fixes (17 November 2026):

- **Horizon contract:** the dashboard used `60m` while PI-09 emits `1h`. Filters, family mapping, lifecycle schedules and the frontend select now use `1h`; `60m` is accepted only as a compatibility alias.
- **Agent-name lists:** the generic mapping helper discarded string agent lists, so `assigned_agents`, `supporting_agents`, `contradicting_agents` and the agent filter were empty. Dedicated name preservation now handles strings and dictionaries.
- **Filtered decision totals:** status/agent filtering previously counted only the current page. Local filters now page through the available history (bounded), deduplicate cycles, report the true filtered total and paginate the filtered rows.
- **PI-11 periods and scope:** evaluation period filtering now reads `config.start/end` and creation timestamps; instrument and horizon filters use the report's nested configuration; a detail outside the requested scope is discarded rather than displayed.
- **PI-11 metric mapping:** nested price, direction, probability, sample, horizon, regime, stability, model-comparison and leakage-audit sections are exposed; sample sufficiency controls validity.
- **Attribution shapes:** gradient `local_contributions`, LSTM global `feature_importance`/`global_importance`, and ensemble `constituents`/`weights`/`disagreement` are consumed without relabelling. Ensemble constituent contributions remain separate from feature attribution, and `generated_at` is preserved when present.
- **Point-in-time replay:** research and regime selection now uses the latest record at or before the cutoff, supports creation/generation/completion timestamps, counts excluded future research/regimes, and counts future forecasts excluded.
- **Research filters and payloads:** an explicit status filter selects the first matching run; `company`, financials, valuation, sector, peers, macro, corporate events, news, thesis factors and limitations are exposed. String-valued factors/limitations are preserved.
- **Portfolio:** recorded current allocation, optimization diversification, actual dict-shaped rebalancing payloads and 1 NC = 1 INR conversion are preserved; partial source failures no longer report a clean `AVAILABLE` state.
- **RL policies:** PI-15 artifacts expose `artifact_version` and nested training/validation/test metrics. The dashboard now maps policy version, training reward, validation, out-of-sample metrics, regime suitability and drift without presenting reward as profit.
- **Fresh contract fields:** real `forecast_horizon`, generation/target/reference timestamps, `lower_price`/`upper_price`, `data_freshness_status`, `forecast_validity`, lifecycle stage and calibrated `p_up/p_down/p_neutral` are mapped and covered by focused tests.
- **Status honesty:** overview includes evaluation/research/cycle evidence in its data-state decision; decisions/compare/regimes/portfolio surface partial or failed source states; source failures are not converted to empty success.
- **Frontend gaps:** the workspace now renders prediction-interval ranges, comparison bars, calibration probabilities, PI-11 slices/confusion/leakage, research sections, portfolio current/recommended allocation, RL validation/OOS fields, detailed agent findings, replay regime/execution evidence, and accessible tab keyboard navigation. Saved filters load on mount, stale-data errors are shown, and the selected view is remembered locally.

Second audit fixes (20 November 2026):

- **RL & adaptive view crashed the workspace.** A PI-14 `validation` object was rendered as a React child, which threw and unmounted every tab. Policy cells now go through `cell()` (objects as JSON text, scalars unchanged); regression test added.
- **Heading-order skips.** Instrument, Horizons, Replay and the shared no-data gate started at `h3` under the page `h1`. `EmptyState` gained `level`, and those section-level states render `h2`.
- **Research layout and factor text (20 November live walk).** Thesis factors rendered as raw JSON strings widened the document to 2650 px; they now render the rationale and evidence count, and long list tokens wrap.
- **Malformed instrument filter (20 November live walk).** An invalid symbol in the search poisoned the shared filter and produced 422 errors on every view; the filter now validates locally.
- **Audit harness corrections.** The first probe treated inputs inside the collapsed "More filters" `<details>` as unlabelled and read label text with `innerText`. Closed details content is excluded from the accessibility tree and those inputs are wrapped by a naming `<label>`; the probe now skips closed details and reads `textContent`. This was a measurement defect, not a product defect, and is recorded so the earlier 104 flag count is not mistaken for 104 unlabelled controls.

No blocking PI-20 defect remains known. Remaining items are limitations or unverified requirements, not known crashes.

## 21. Remaining limitations

- On 17 November the isolated preview database had no populated prediction records, so that app honestly displayed `INSUFFICIENT_EVIDENCE`; populated behaviour was verified then with deterministic fixtures. The 20 November audit ran against the populated local database instead (21 active forecasts, a 30-sample evaluation, stored research, a portfolio value), and the views without records still showed `INSUFFICIENT_EVIDENCE` rather than zeros.
- The audit opened all 13 views and inspected them at 12 widths; the separate live walk operated the per-view interactions (instrument lookup, decision selection, research-run selection, replay reconstruction) at five widths. A 21 November follow-up clicked every field of the collapsed filter drawer and saved filters, reloaded the page and read the instrument and horizon back; table controls (sorting, row actions) remain unexercised. No screenshots were produced.
- Accessibility evidence is automated only: no screen-reader session (NVDA/JAWS/VoiceOver), no manual keyboard-only journey beyond the skip link and tab contract, and no WCAG conformance certification is claimed.
- Performance was measured on the Vite dev server with a warm local database. The production bundle sizes are recorded, but a production preview was not served (the Vite dev proxy supplies the loopback operator credential, which a static preview would not).
- Responsive evidence is CSS viewports at 100% zoom; browser zoom and OS text scaling were not measured.
- `policies` (851 KB) and `research` (208 KB) return large bounded payloads for `limit=50`; the views render them without a request failure, but no further pagination was added in this audit.
- The PI-20/API/dependency/regression/web checks above passed. The entire repository-wide suite was not run.
- The legacy 60m horizon is accepted as an alias but the dashboard emits and filters on the PI-09 `1h` label; external consumers that still request 60m receive 1h in the normalized filter payload.
- Some source reports do not supply every optional field (for example an LSTM explanation timestamp or a PI-11 by-instrument slice). The UI shows those fields as unavailable rather than synthesizing them.
- Real-time refresh covers event types the existing services actually publish. PI-17 agents, PI-18 optimization and PI-19 research do not emit dedicated dashboard event types in the current event vocabulary; their views refetch on navigation and on the shared data/forecast/regime/portfolio events, not through fabricated per-subsystem events.
- Feature attribution depends on the source explain endpoint returning computed values. Where a model does not expose attribution, the dashboard states that limitation; no SHAP values are generated.
- Free or delayed market sources remain delayed. The dashboard labels timestamps and freshness and does not claim exchange-grade real-time updates.
- Performance and calibration statements inherit the source sample size, dataset version and evaluation period. The dashboard does not create additional statistical evidence.
- The focused suites listed above passed; the entire repository-wide test suite was not run again because the changed surface was PI-20, its API contracts and the affected PI-11 to PI-19/web regressions.
- The working tree remains uncommitted and unstaged by design.

## 22. Final acceptance verdict

**PI-20: PASS**, with the evidence above and the limitations in section 21.

The unified dashboard, instrument workspace, multi-model and multi-horizon views, uncertainty presentation, lifecycle monitor, performance analytics, regime intelligence, multi-agent decisions, explainability, evidence provenance, financial research, portfolio intelligence, RL/adaptive view, point-in-time replay, filters, authenticated API and React integration are implemented using the existing services. The page is read-only, NM-08 and NM-10 retain sole authority, and no broker or live-trading path was added.

PI-20 did not start PI-21; that later phase is recorded in `PI21_COMPLETION_REPORT.md`.
