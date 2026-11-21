# PI-18 Completion Report — Advanced Portfolio Optimization and Capital Allocation

## 1. Initial repository assessment

Work was performed on `feature/novamarket` with a dirty working tree containing
the user's PI-01–PI-17 work. No file was staged, committed, tagged, merged,
pushed or published. PI-01–PI-17 completion reports were present and recorded
PASS for their implemented scopes; no prerequisite dependency failure was
found. The existing portfolio (`portfolio/`), valuation/accounting, NM-08
(`risk/`), NM-10 (`execution/`), PI-02 market data, PI-04 features/regimes,
PI-05–PI-11 forecasting/evaluation, PI-12 autonomy and PI-16/PI-17 intelligence
layers were inspected and reused read-only.

The pre-existing system is Python/FastAPI with SQLite research stores, numpy
numerical code, a React/Vite workspace and generated API documentation. No
quadratic-programming dependency was present, so the phase uses deterministic
numpy projection rather than adding an unrelated solver stack.

## 2. Existing components reused

- NM-07 `portfolio.detail`, valuation and position/accounting records for the
  authoritative snapshot, cash, invested capital, P&L and current weights.
- NM-08 portfolio risk report as read-only evidence and the sole operational
  risk authority; PI-18 does not approve risk or create a second risk gate.
- NM-10 paper-execution contracts as the sole execution authority; no PI-18
  path can submit, cancel or fill an order.
- PI-02 bars, PI-04 features/regimes and the existing forecast readers for
  PI-05/PI-08/PI-10 expected returns and uncertainty.
- PI-11 backtest conventions for windowing, costs and evaluation vocabulary.
- PI-12 autonomy controls, PI-17 agent coordinator and the existing
  `NovaMarketService` lazy-service pattern.
- Existing API authentication, `ApiSurface`, `ROUTE_CONSUMERS`, generated API
  reference and the shared React design/navigation components.

No second ledger, valuation engine, risk engine, prediction engine or
execution engine was created.

## 3. Optimization architecture

New isolated package: `src/novacontrol/novamarket/optimization/`.

| Module | Responsibility |
| --- | --- |
| `models.py` | Versioned configs, nine horizons, statuses, objectives, snapshot/eligibility/result/scenario/rebalance records |
| `inputs.py` | Read-only context adapters, authoritative snapshot, bars, forecasts, eligibility and liquidity evidence |
| `analytics.py` | Aligned returns, covariance/correlation, risk, diversification, liquidity, scenarios and backtest metrics |
| `solvers.py` | Long-only constrained mean-variance, minimum-variance, risk-parity, equal-weight, inverse-volatility and current-book methods |
| `backtesting.py` | Point-in-time strategy comparison with costs, slippage, turnover and trailing-only visibility |
| `store.py` | Isolated `po_*` SQLite persistence, idempotent recovery and append-only records |
| `service.py` | Config/run lifecycle, validation, cancellation, scenario/backtest/rebalance/handoff APIs |
| `__init__.py` | Public contracts only |

Every run records optimization ID, portfolio ID, timestamp, horizon, universe,
portfolio snapshot version, market-data version, forecast version, NM-08 config
version, optimization config version, solver version and result status.

## 4. Input validation evidence

The context is assembled by `NovaMarketService` and injected into the service;
the optimizer never opens production tables or discovers instruments. It
validates missing/stale position prices, invalid or unsupported exchange
identities, insufficient bar history, stale/absent forecasts, horizon
compatibility, non-finite prices/returns, covariance conditioning and pending
buy commitments. A stale authoritative snapshot blocks the run rather than
being silently repriced.

Focused evidence: `test_portfolio_snapshot_is_read_only_and_preserves_authoritative_values`,
`test_input_validation_excludes_missing_bars_and_stale_prices`,
`test_stale_forecast_and_missing_forecast_fail_explicitly`,
`test_invalid_and_degenerate_covariance_is_handled` and
`test_invalid_configuration_and_infeasible_limits_are_refused_explicitly`.

## 5. Expected-return integration

PI-05–PI-10 forecast records supply expected return (or a target-price-derived
return), direction, prediction interval, uncertainty, model disagreement,
quality and validity. Only forecasts available at the run timestamp, within the
horizon-specific age limit and of usable quality are consumed. Confidence is
not treated as a calibrated probability unless calibration evidence exists.
Horizon-mismatched forecasts are excluded, not blended. Negative and zero
expected returns are preserved rather than clamped.

Evidence: `test_eligibility_requires_exchange_and_horizon_compatible_forecast`,
`test_negative_and_zero_expected_returns_are_preserved` and
`test_multi_horizon_runs_preserve_separate_horizon_inputs`.

## 6. Covariance and correlation results

Log returns are aligned on the intersection of observation timestamps and
winsorized at the 0.5th/99.5th percentiles. Sample, exponentially weighted and
diagonal-shrinkage covariance are implemented. Matrices are symmetrized, checked
for finite values, positive definiteness and condition number; a documented
ridge is applied to a usable but ill-conditioned matrix. No correlation is
fabricated where common history is insufficient.

The run exposes the matrix, annualized instrument volatilities, average
pairwise correlation, correlation-stability evidence and warnings. Focused
tests cover aligned returns, valid covariance, singular/degenerate matrices and
high-correlation detection (`test_aligned_returns_covariance_and_correlation_are_numerically_valid`,
`test_invalid_and_degenerate_covariance_is_handled`,
`test_high_correlation_is_flagged_not_hidden`).

## 7. Optimization methods implemented

| Objective | Supported |
| --- | --- |
| Maximum expected return | Yes |
| Minimum variance | Yes |
| Risk-adjusted return | Yes |
| Maximum Sharpe | Yes, when return/covariance inputs make it meaningful |
| Minimum downside risk | Yes, with sufficient history |
| Diversification | Yes |
| Multi-objective | Configurable return/risk/diversification/turnover/liquidity weights |

| Method | Objective and notes |
| --- | --- |
| `mean_variance` | Maximizes `w'μ - λw'Σw` under all constraints |
| `min_variance` | Minimizes portfolio variance |
| `risk_parity` | Bounded fixed-point equal-risk-contribution approximation |
| `equal_weight` | Deterministic baseline across eligible instruments |
| `inverse_volatility` | Volatility-scaled baseline |
| `current` | Current book as a reference, not an optimized recommendation |

Each is documented in the module and has explicit failure conditions. If the
solver cannot produce a finite, feasible result it returns a structured failure
or a clearly identified baseline; it never silently switches methods. Every run
records the objective, method, solver version and `fallback_used=false`.

## 8. Allocation constraint evidence

Long-only weights, cash as a legitimate residual, minimum cash reserve, maximum
instrument weight, sector cap, maximum position count, maximum turnover,
liquidity participation cap and pending-buy reserve are enforced after every
projection. Constraints are never relaxed to force full investment. A
zero-cash, no-position, single-instrument case and an over-constrained
`max_positions` case are tested. Total recommended capital never exceeds
available capital after reserves.

A constraint set that cannot be satisfied is its own outcome, not a validation
or solver failure. `solve_weights` raises `OptimizationInfeasibleError` when the
instrument, sector and liquidity caps leave no allocatable weight at all, or
when a solve would exceed the permitted total. The service catches it and
persists the run as **`infeasible_constraints`** with `result_status`, the
`constrained_allocation` stage and the reason, and `status()` counts these runs
separately from `failed_runs`; a solve never reports this case as
`invalid_input` or `solver_failed`. Under-investment that the caps merely limit
still completes with the cash residual and a warning, as before.

Evidence: `test_cash_reserve_instrument_sector_and_turnover_constraints`,
`test_max_positions_leaves_capital_in_cash_instead_of_relaxing`,
`test_zero_available_cash_no_positions_and_single_instrument`,
`test_every_allocation_method_returns_long_only_feasible_weights`,
`test_an_allocation_with_no_allocatable_capacity_is_explicitly_infeasible` and
`test_a_solve_with_infeasible_constraints_returns_the_explicit_status`.

## 9. Diversification results

Reports number of holdings, invested-weight HHI, effective holdings, effective
sectors, top weight, average pairwise correlation and diversification ratio
(`weighted volatility / portfolio volatility`). Cash is excluded from HHI.
High correlation is flagged even when the holding count appears adequate, so a
large but correlated book is not mislabeled as diversified. Recommendations are
explainable from the reported concentration and correlation evidence. Every
completed run also includes an `explainability` section with per-position
expected return, forecast quality, risk contribution, binding constraints and a
plain statement of the limits of the claim, rendered in the workspace.

Evidence: `test_risk_and_diversification_metrics_have_documented_meanings`,
`test_high_correlation_is_flagged_not_hidden` and
`test_explainability_reports_drivers_and_binding_constraints`.

## 10. Liquidity-analysis results

Liquidity capacity is estimated as average close × volume × configured
participation rate. This is explicitly an estimate, not confirmed executable
capacity. Missing volume is either excluded or retained with a limitation
according to the configured `exclude`/`warn` policy. A binding participation cap
reduces the allocation and leaves the residual in cash rather than presenting an
unvalidated size as executable.

Evidence: `test_liquidity_participation_cap_and_missing_liquidity_policy`.

## 11. Multi-horizon implementation

All nine established horizons are supported: 5m, 15m, 20m, 60m, 1d, 3d, 5d, 30d
and 90d. Each run has exactly one horizon and its own bar interval, forecast
age limit, price freshness limit, annualization and risk defaults. Separate runs
preserve separate recommendations and inputs; no mixed-horizon objective exists.

Evidence: `test_multi_horizon_runs_preserve_separate_horizon_inputs`.

## 12. Rebalancing recommendation evidence

Rebalancing compares current and target weights and emits `INCREASE_POSITION`,
`REDUCE_POSITION`, `MAINTAIN_POSITION`, `OPEN_POSITION`, `CLOSE_POSITION`,
`HOLD_CASH` or `NO_REBALANCING_REQUIRED`. Thresholds, minimum notional, turnover
caps, transaction costs and slippage are configurable. The result reports
current/target expected return, volatility and risk change, cost drag, net
expected improvement, and a funding gap against cash available after pending
buys and the configured minimum cash reserve. A cash residual is an explicit recommendation. Nothing is executed;
PI-12, NM-08 and NM-10 still own the operational path.

Evidence: `test_rebalancing_recommendations_and_thresholds` and
`test_high_transaction_costs_are_visible_in_rebalancing_and_backtests`.

## 13. Scenario-analysis results

Historical replay and hypothetical stress are separate. Scenarios cover
market-wide and sector declines, volatility and correlation increases, a
largest-position shock, liquidity deterioration, forecast deterioration and
regime transitions, with position and sector contributions that reconcile to
the portfolio change. Pure correlation/volatility scenarios report risk changes
and zero deterministic value change instead of inventing a loss. Hypothetical
results carry `is_forecast=false`; they are stress estimates, not predictions.
Operator shocks must name allocated instruments and stay within ±100%.

Evidence: `test_scenario_analysis_separates_historical_and_hypothetical`.

## 14. Backtesting results

`backtesting.py` compares equal-weight, current-book, minimum-variance,
risk-parity, mean-variance and inverse-volatility over identical periods,
universe, initial capital, costs, slippage and rebalance frequency. The current
book is now in the default comparison set. Each solver sees returns strictly
before the period it is about to earn, so the rebalance period's return cannot
leak into the allocation. Turnover and costs are charged; annualization uses the
selected horizon's periods per year, including intraday bars. Metrics include cumulative/annualized return, volatility, maximum
drawdown, Sharpe, Sortino, Calmar, turnover, cost, concentration and benchmark
comparison. Point-in-time correctness is tested; survivorship bias remains a
disclosed limitation because the available dataset has no historical
constituent record.

The comparison baseline is a REAL market series when a run configures
`benchmark_instrument_id` (any registered `MIC:SYMBOL` series, e.g. a market
index): `NovaMarketService._optimization_context` reads its PI-02 bars
read-only, the analysis window is intersected with the benchmark's observation
keys (`analytics.align_benchmark`), and the backtest reports a buy-and-hold
benchmark (`benchmark.kind = "market_series"`, no costs or slippage) with
per-method excess return, excess annualized return, excess Sharpe and drawdown
difference. Without a configured series the baseline is labelled
`benchmark.kind = "strategy"` (equal weight unless `benchmark` names another
method), so a fallback is never presented as a market comparison. The same
series feeds the `market_model` covariance method (single-index: beta_i beta_j
var_market + residual variance). `market_model` refuses a config without a
benchmark, and a run whose benchmark bars cannot cover the window returns
`insufficient_data` with the reason instead of silently substituting an
equal-weight proxy. The benchmark is read-only: it is never an eligible
instrument, never allocated, and no portfolio, cash, ledger or order state is
written.

Evidence: `test_backtesting_uses_point_in_time_history_and_charges_costs`, `test_market_benchmark_series_drives_covariance_and_backtest_comparison`, `test_market_model_covariance_estimates_a_single_index_structure`, `test_market_model_covariance_refuses_a_missing_benchmark_series`, `test_backtest_without_a_benchmark_series_labels_the_strategy_fallback`, `test_benchmark_alignment_uses_only_covered_observations`; the earlier evidence also covers the current-book baseline, the strict pre-rebalance return window and horizon-aware annualization.

## 15. Multi-agent integration

The latest completed optimization is attached to the PI-17 context under
`portfolio.optimization`. The Portfolio Intelligence Agent exposes it as
advisory evidence, uses it for portfolio-context interpretation and warns on
run-status or horizon mismatch. It cannot alter an optimization result; a new
versioned optimization run is required. This uses the existing PI-17
coordinator rather than a second orchestration system. Evidence:
`test_pi17_portfolio_agent_receives_advisory_optimization_context`.

## 16. API and frontend integration

- 24 authenticated routes under `/market/optimization/*`: status, contracts,
  health, configs (create/list/get), validate, run/list/history/latest/detail,
  allocations, diversification, risk, liquidity, rebalancing, scenarios,
  backtests (run/list/detail), handoff, cancel.
- Every route is declared once in `ApiSurface`, registered in
  `ROUTE_CONSUMERS` as `no-render`, and generated into `docs/API.md`
  (790 routes total, in sync).
- React `PortfolioOptimization.tsx` provides portfolio overview, validated
  configuration, target allocation and differences, correlation and
  diversification, risk/scenario evidence, rebalancing costs and improvement,
  strategy comparison and the advisory boundary. It is wired into
  `App.tsx` and navigation.
- A browser verification then exercised the page against a running API and the
  Vite workspace: it loaded the workspace, validated and ran a mixed NSE/BSE
  configuration, and opened the history, allocation, risk, rebalancing and
  comparison panels. It found five defects that the automated suite could not
  see — BSE-listed instruments rejected as an unknown exchange, fresh bars
  reported as stale, a funding section that never rendered, a broken
  history-header layout and a false sector-cap warning. All five are fixed
  (section 19, items 25–29); the corrected page loaded and rendered the funding,
  history and diversification panels, and the PI-18/API suites and the
  production build were re-run afterwards (section 18).

Evidence: `test_api_routes_are_declared_once_with_non_render_consumers`,
`test_actual_application_registers_the_optimization_routes`,
`test_frontend_workspace_and_navigation_are_wired`,
`test_real_application_serves_status_contract_and_health` and the Vite build.

## 17. Financial-isolation evidence

The optimizer store contains only `po_configs`, `po_runs`, `po_scenarios`,
`po_backtests` and `po_events`. It has no position, cash, ledger, order or fill
table. Source inspection tests confirm there is no portfolio/execution write
path, and the status/health contract reports `production_authority=false`,
`order_submission_available=false`, `advisory_only=true`,
`risk_authority=NM-08` and `execution_authority=NM-10`. The PI-12 handoff is
explicitly advisory and cannot bypass autonomy, NM-08 or NM-10.

Evidence: `test_optimization_store_contains_only_isolated_research_tables`,
`test_optimizer_source_has_no_portfolio_or_execution_write_paths` and
`test_real_application_serves_status_contract_and_health`.

## 18. Actual test counts and results

| Check | Result |
| --- | --- |
| PI-18 focused suite (after the browser-verification fixes) | **52 passed** |
| PI-17 regression | **35 passed** |
| PI-12 regression | **29 passed** |
| PI-13 regression | **40 passed, 3 subtests passed** |
| PI-14 regression | **45 passed** |
| PI-15 regression | **19 passed** |
| PI-16 regression | **41 passed** |
| API suite, incl. exact route set (re-run after the fix) | **64 passed, 349 subtests passed** |
| Total focused/affected | **325 tests passed + 352 subtests** (PI-12–PI-17 rows from the previous pass; PI-18, optimization and API rows re-run after the browser-verification fixes) |
| mypy (`src`, 717 files) | **Success: no issues found** |
| `tests/test_optimization.py` regression | **75 passed, 5 subtests passed** |
| Ruff (new optimization package, changed agent/test) | **All checks passed** |
| `generate_api_reference.py --check` | **in sync, 790 routes** |
| React production build | **passed** (`tsc -b && vite build`) |
| `git diff --check` | **passed** (line-ending warnings only) |
| Staged files | **0** |

PI-01–PI-11 were not re-run as broad suites; their verified reports were reused
because PI-18 did not modify their engines. The full API suite and PI-12–PI-17
regressions cover the shared service, authentication and coordinator surfaces
that PI-18 touched.

## 19. Defects fixed

1. Initial objective default used a removed `MEAN_VARIANCE` enum; price-based
   defaults now use `RISK_ADJUSTED`.
2. Fractional quantities were passed where integer whole units were required;
   conversion now uses the existing `whole()` helper.
3. The configuration accepted slippage that the model did not retain; it is now
   versioned and consumed by backtests.
4. Horizon risk defaults used incompatible volatility periods; defaults are now
   horizon-specific.
5. Zero expected returns were lost by truthiness handling; explicit zero is
   preserved.
6. Missing liquidity under `warn` could produce a false failure; it now yields a
   warning and an explicitly unvalidated allocation.
7. Equal-weight and inverse-volatility backtest branches were unreachable; both
   now run.
8. `completed_with_warnings` returned `ok=false`; it now returns success while
   preserving warnings.
9. Backtest current-book weights were empty; they are now derived from the
   authoritative snapshot.
10. Risk caps used the wrong horizon periods; annualization is now correct.
11. Solver cancellation was reported as `solver_failed`; cancellation now maps
    to `cancelled`.
12. The API exact-route allowlist and consumer documentation were stale; both
    were updated and regenerated.
13. The rebalancing UI panel was initially missing; it now renders costs,
    expected improvement and restrictions, and loading state is reset on error.
14. Ruff SIM117 in the HTTP test and a line-length issue in the agent were fixed.
15. Scenario position contributions applied the portfolio-weight multiplier to
    an already total dollar change, so position contributions did not reconcile
    to the scenario loss. Contributions now sum to the reported change.
16. Backtests included the return of the rebalance period in the solver window,
    which was look-ahead bias. The window now excludes that period; the current
    book is included in the default comparison set, and intraday annualization
    uses the selected horizon instead of hard-coded 252.
17. The 20m horizon was labelled with 15m bars and one period. It now uses 5m
    bars with four periods and the correct annualization factor.
18. Forecast rows without a usable expected-return field were accepted with a
    zero return, and minimum forecast quality was not enforced. Such rows are
    now excluded; `min_forecast_quality` is versioned and enforced.
19. Rebalancing had no `expected_improvement` even though the UI referenced it,
    omitted slippage from estimated costs, and had no funding check. It now
    reports net improvement, risk and return changes, slippage-inclusive cost,
    and available-cash funding status.
20. Cancellation could overwrite an already-cancelled run with a completion;
    terminal statuses now reject cancellation, and the run loop rechecks the
    cancellation/deadline before persisting. Handoff and scenario/backtest
    operations now require a completed run, and `latest` no longer scans only
    the most recent 100 rows.
21. Currency, universe truncation, quality and intraday liquidity were
    under-validated. Mixed/non-INR prices, instruments outside `max_instruments`,
    below-minimum forecasts and daily-volume scaling are now explicit.
22. PI-04 regimes were loaded but not surfaced. The run now records a regime
    summary and warns when a selected instrument is in a high-uncertainty regime.
23. Explainability was implicit in scattered warnings. Completed runs now carry
    an `explainability` section with per-position return, quality, risk
    contribution, binding constraints and an explicit statement of what is not
    claimed; the workspace renders it as a dedicated panel.
24. `INFEASIBLE_CONSTRAINTS` was declared but never produced. The solver's
    infeasibility check was an `OptimizationValidationError`, so an allocation
    the configured caps could not satisfy was persisted as `invalid_input`, and
    an all-zero capacity was silently treated as a full-cash allocation. The
    solver now raises the dedicated `OptimizationInfeasibleError` (zero
    allocatable capacity, or a solve exceeding the permitted total), the service
    records `infeasible_constraints` with the reason and stage, the failure
    mapper reports it as that status, and `status()` counts it outside
    `failed_runs`.
25. **BSE-listed instruments were rejected as an unknown exchange.** The
    eligibility gate compares the exchange the run allows with the label the
    context reports for the instrument, and the context reports a BSE listing as
    `BSE` — the label its bars are stored under — while the workspace submitted
    only MIC codes (`XNSE, XBOM`). Every BSE instrument was excluded with
    `exchange BSE is not allowed` although its bars were present. The
    allowed-exchange default and its hint now name the label the data carries
    (`XNSE, BSE`) and state that BSE-listed bars report as `BSE`, not `XBOM`; the
    gate still refuses any label the run does not allow. Verified against a
    BSE-labelled context: excluded with the MIC-only list, eligible with the
    label list.
26. **Fresh bars were reported as stale.** `_optimization_context` requested the
    instrument series with `limit=320` and the default ascending order, but the
    stored bars page holds fewer rows than the request, so the context received
    the oldest rows. The freshness check measured the age of the last bar it
    received, which was older than the freshness limit, and reported a freshly
    ingested series as stale — excluding instruments that had current prices. The context now
    reads the newest rows (`ascending=False`) and reverses them into
    chronological order for the optimiser; benchmark bars follow the same rule.
    `test_the_optimization_context_reads_the_newest_bars_not_the_oldest` fails if
    either the newest-first request or the reversal is lost.
27. **The funding section never rendered.** The rebalancing plan has reported a
    `funding` object since defect 19, but the workspace panel rendered only
    costs, expected improvement and restrictions, so funding status could not be
    inspected. The panel now renders funding status, available cash, buy and sell
    notional, net cash required, pending buy reserve and funding gap, and states
    the gap when the book is unfunded.
28. **The optimization-history header broke its card.** The header is a
    non-wrapping flex row whose children could not shrink, so the run selector
    and the run-meta badges (long run ids, timestamps, method/objective/horizon
    labels) pushed past the card and the meta row inherited the right-aligned
    action layout. Header items now wrap, header children and grid columns have
    `min-width: 0`, selects are capped at the card width, the meta row spans the
    fact grid (`.fact-list-full`) and left-aligns its badges.
29. **The diversification panel warned about a sector cap the allocation did
    not breach.** The warning compared invested-only shares — normalised to
    100% of invested capital — with `max_sector_weight`, which is configured
    against total portfolio value. Any book holding one sector at a modest
    portfolio weight therefore reported 100% in that sector and warned
    regardless of its cap. The check now uses absolute sector exposure over
    total portfolio value; sector weights remain invested shares for the
    diversification metrics. Covered by
    `test_sector_cap_warning_uses_portfolio_exposure_not_invested_share`.

## 20. Remaining limitations

- Covariance, VaR/CVaR and scenarios are backward-looking estimates and can fail
  during structural breaks. They are not guarantees against loss.
- Expected returns inherit forecast error; forecast confidence is not treated as
  a calibrated probability.
- Daily volume is a liquidity proxy, not confirmed executable capacity.
- Backtests use the current instrument universe; survivorship bias cannot be
  fully removed from the available dataset.
- The 20m horizon is a five-minute-bar proxy because NovaMarket has no native
  20-minute interval. It is explicitly documented rather than labelled as 15m.
- A market benchmark is used only when a run names one
  (`benchmark_instrument_id`). No index instrument is ingested in the current
  dataset, so a real listed series (for example `XBOM:TCS`) stands in until an
  index is registered; the comparison is then against that series, not an
  index. Sector correlation is represented through instrument covariance, not a
  separate sector-return model.
- The browser verification was a manual pass in one browser and viewport
  against local data. It is not a cross-browser, responsive or accessibility
  test, and the layout fixes were judged visually rather than measured.
- The optimization result is a constrained mathematical recommendation, not a
  profitability claim.
- No broker, live trading, autonomy change, NM-08 approval or NM-10 order is
  available from PI-18.
- PI-18 does not automatically execute recommendations; PI-12 must independently
  apply autonomy, NM-08 and NM-10 controls.

## 21. Final acceptance verdict

**PASS** for the implemented PI-18 scope, subject to the limitations above.

All twenty-nine acceptance areas are implemented and verified at the level
described in this report: architecture, authoritative snapshot, validation,
eligibility, expected returns, covariance/correlation, risk analytics,
objectives, capital constraints, long-only allocation, diversification,
liquidity, multi-horizon, rebalancing, scenarios, backtesting, explainability,
multi-agent integration, PI-12 boundaries, persistence/versioning, API, frontend, resource
and failure controls, financial isolation, NM-08/NM-10 authority, actual
application integration, focused/affected tests, no known blocking defect,
documentation and this single consolidated report.

The post-fix workspace was re-opened in the browser and the corrected funding,
history and diversification panels rendered, so the verdict rests on both the
automated suites and a manual frontend check.

No PI-19 work was started. No Git history or publication state was changed.
