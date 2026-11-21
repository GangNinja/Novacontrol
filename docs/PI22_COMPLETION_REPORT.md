# PI-22 — Advanced Risk Intelligence and Portfolio Protection: Completion Report

Date: 2026-11-18
Branch: `feature/novamarket`
Status: **PASS** (focused and affected-area verification; limitations listed below)

## 1. Initial repository assessment

- Confirmed the work was on `feature/novamarket` with no staged files before implementation.
- Reviewed the consolidated PI-01 through PI-21 reports and treated their scoped PASS verdicts as the prerequisite baseline. PI-21 was already verified PASS; no prerequisite regression was found by the affected-area tests run for this phase.
- Inspected NM-08 risk engine, metrics and persistence; NM-10 paper execution and reconciliation; PI-11 forecast evaluation; PI-12 autonomy orchestration; PI-16 regime evidence; PI-17 agent findings; PI-18 optimisation; PI-19 research; PI-20 dashboard contracts; and PI-21 portfolio-management composition.
- Inspected the authoritative NM-07 portfolio, valuation, position, cash and ledger services, plus existing alert, event and persistence infrastructure.
- Reused existing risk metrics, valuation snapshots, forecast evidence, research/agent evidence, PI-18 outputs, pending orders, reservations and autonomy runtime status. No second risk engine, execution engine, portfolio ledger or valuation service was created.
- No PI-23 work was started.

## 2. Existing components reused

| Capability | Reused authority/contract |
|---|---|
| Portfolio, positions, cash and valuation | Existing NM-07 portfolio service and authoritative valuation snapshots |
| Volatility, historical VaR/CVaR, correlation, HHI | Existing NM-08 `risk.metrics` implementations |
| Risk decisions and restrictions | Existing NM-08 portfolio and position risk assessments |
| Paper execution and reconciliation | Existing NM-10 through PI-12; PI-22 never submits an order |
| Autonomy modes, operator permission, kill switch, lease, recovery | Existing AT-01/PI-12 runtime and recovery paths |
| Forecasts, uncertainty and model agreement | PI-05 through PI-11 stored forecast views |
| Regime and instrument evidence | PI-04/PI-16 stored regime and instrument evidence |
| Multi-agent findings | PI-17 stored decisions |
| Allocation context | PI-18 stored optimisation output |
| Research and corporate events | PI-19 stored research evidence |
| Presentation | PI-20 React components, `useApi`, hash navigation and toast system |

## 3. Implemented risk architecture

The advisory implementation lives in `src/novacontrol/novamarket/risk_intelligence/`:

- `models.py`: risk vocabulary, analysis statuses, liquidity classes, alert severities, recommendation vocabulary, validated `RiskThresholds`, authority contract, stable IDs and typed errors.
- `analytics.py`: bar normalisation, exact-timestamp return alignment, NM-08 volatility/VaR/CVaR/correlation/HHI reuse, rolling correlation change, drawdown episodes, concentration, liquidity classification, scenario impact and freshness helpers.
- `evidence.py`: `RiskIntelligenceSources` callback contract and a per-analysis cached `RiskEvidenceBook`. Every source failure is recorded as evidence error and never converted into a risk value.
- `store.py`: isolated `novamarket_risk_intelligence.sqlite3` containing only `ri_*` tables. Assessment, position, metric, scenario, scenario-run, stress, alert, recommendation, event and recovery records are separated from financial accounting.
- `service.py`: coordinator for status/contract/navigation/config, analysis, position and portfolio analytics, concentration, correlation, volatility, drawdown, liquidity, regime, warnings, recommendations, scenarios, stress and recovery.
- `__init__.py`: public package API.

Every analysis records the assessment ID, portfolio ID, horizon, snapshot version, market-data versions, configuration version/digest, evidence errors, data freshness, warnings, NM-08 references and timestamps. The headline risk level is the maximum explained component level; no arbitrary weighted composite score is produced.

## 4. Portfolio risk evidence

The latest assessment reports total value, invested capital, available virtual cash, reservations, pending-order exposure, unrealised and realised P&L, position count, NC values and INR equivalents. The capital section records invested/cash/available-cash weights, pending buy and sell notional, pending-to-available-cash ratio and the NM-07/NM-10 sources. Pending buy exposure above available cash raises a high-severity warning; missing cash evidence raises a watch warning instead of being treated as zero. Missing valuation fields remain `null` rather than zero. The overview contains the current assessment, previous assessment reference, trend, risk contributors, warnings, evidence timestamp, data freshness and limitations. Assessment confidence is `null` with an explicit reason: no calibrated probability model is claimed.

## 5. Position risk evidence

Every position reports size, entry/current price, market value, weight, return, unrealised P&L, historical volatility, downside deviation, price drawdown, liquidity class, position/ADV days, spread when supplied, forecast direction/uncertainty/agreement, research/agent evidence, regime, correlation exposure and the NM-08 position reference. Output levels are `normal`, `elevated`, `high`, `critical` and `insufficient_data`. A negative return alone does not produce `critical`; critical position drawdown or very-low liquidity can. A position without bar history is `insufficient_data`, not normal.

## 6. Concentration and correlation evidence

Concentration uses normalised position weights and the NM-08 HHI, with instrument, sector, industry, largest-sector/industry, top-one/top-three and effective-holdings evidence. Portfolios that look diversified by instrument count but share a sector or correlated return path are flagged.

Correlation is calculated only on returns paired at identical observation timestamps. Unequal calendars are intersected; unmatched observations are excluded. The matrix carries a diagonal of `1.0` for instruments with stored history and `null` otherwise, alongside pairwise correlation, rolling correlation change, sector averages, benchmark correlation when configured, insufficient-observation reasons and high/elevated pair lists. Correlation is explicitly treated as historical and unstable, not a forecast.

## 7. Volatility and drawdown evidence

Volatility uses the existing NM-08 implementation: population standard deviation of simple returns, annualised with `sqrt(periods_per_year)`, plus downside deviation, one-day historical VaR and CVaR/expected shortfall at the configured confidence. The contract records the observation window, sample size, minimum sample and historical-simulation assumption. VaR is labelled a quantile loss, never a maximum possible loss, and historical estimates are separated from forecasts.

Drawdown uses stored authoritative valuation snapshots only. It records current and maximum drawdown in percentage and NC, peak/current value, duration, episodes and position contributions. PI-22 reads the most recent bounded valuation window, not the oldest slice; the analytics also sort timestamped observations defensively. Current and maximum NC drawdown are derived from historical peak/episode values, not by applying a percentage to the current total. Prices are not reconstructed from inconsistent data. Position-level price drawdown uses stored bars as analytical evidence and is labelled separately.

## 8. Liquidity analysis evidence

Liquidity uses mean `close × volume` over up to 20 stored daily bars, position value, position/ADV ratio and days-to-liquidate. Classes are `high_liquidity`, `normal_liquidity`, `low_liquidity`, `very_low_liquidity` and `insufficient_data` using validated, increasing day thresholds. Positions without volume or value evidence are listed as uncovered, excluded from the covered weighted-days calculation, and at least elevate the portfolio liquidity level; they are never treated as liquid. Bid-ask spread is used only when supplied. Market depth is reported as unavailable unless a provider supplies order-book data; a spread is labelled `quote_only`, never treated as depth. Estimated impact ranges state 5%–20% participation assumptions. No bid-ask spread, depth or execution price is fabricated.

## 9. Scenario and stress-test evidence

Scenario kinds include market decline/rally, sector decline/rally, instrument decline, volatility expansion, liquidity deterioration, correlated decline, cash depletion, multiple pending orders and combined. Each scenario records its ID, portfolio, snapshot/assessment reference, configuration, assumptions, methodology, position/sector contributions, estimated portfolio value, monetary and percentage change, remaining virtual cash and pending exposure. Cash depletion is modelled as loss of available cash, not as a forced position sale. Pending-order scenarios apply the shock only to pending exposure and report `pending_order_loss_nc` with no position contributions; no pending exposure is `insufficient_data`, not a zero-loss success. Liquidity-deterioration and combined scenarios add the modelled per-position liquidation cost, report `liquidity_cost_nc`, `liquidity_cost_coverage_pct` and `missing_liquidity_evidence`, and fail closed when no position has impact evidence. Volatility expansion applies a scaled one-day historical VaR (`volatility_multiplier`); correlated decline applies simultaneous one-day VaR under an explicit perfect-correlation assumption. A target sector/instrument that matches no position is `insufficient_data`. A saved scenario must belong to the requested portfolio. Every result carries `hypothetical: true`, `is_prediction: false` and no financial mutation.

Stress modes are historical, hypothetical, volatility, correlation, liquidity and combined. Historical stress uses the stored valuation drawdown; hypothetical modes apply explicit deterministic shocks or per-position VaR/liquidity costs. Every stress record includes scenario ID, portfolio snapshot ID, input-data versions, assumptions, methodology, result, limitations, digest and execution timestamp. Identical inputs produce identical stress IDs and digests.

## 10. Early-warning evidence

Warnings are generated for stale prices, pending exposure, pending buy exposure above available cash, missing pending-cash evidence, high position risk, low liquidity, missing liquidity evidence, forecast degradation, drawdown, concentration, high/elevated correlation, correlation shifts, elevated volatility, portfolio liquidity, defensive/uncertain regime and missing/insufficient data. Each warning includes ID, portfolio/instrument reference, category, severity, evidence, observed time, review condition, recommended monitoring response and `advisory_only: true`.

Alerts use a stable deduplication key and a configurable cooldown. A repeated condition increments `occurrences` instead of creating a duplicate alert. Operators can acknowledge, resolve or reopen an alert; these actions do not change any financial state.

## 11. Protective recommendation evidence

Supported advisory actions are `maintain_position`, `review_position`, `reduce_exposure`, `exit_review`, `retain_cash`, `reduce_new_entries`, `avoid_new_exposure`, `review_sector_concentration`, `review_correlated_holdings`, `wait_for_better_liquidity` and `increase_monitoring`. Every recommendation records ID, portfolio/position reference, category, severity, triggering evidence, historical context, expected impact, suggested response, horizon, NM-08 reference where available, configuration version, timestamp and workflow. Recommendations are always `advisory_only`, `execution_authorized: false`, `order_submission: false` and `financial_mutation: false`.

## 12. NM-08 authority verification

NM-08 remains the sole authoritative risk engine. PI-22 reads portfolio and position risk assessments and records their outcome/decision reference. It does not create a risk decision, override a rejection, bypass a restriction or independently authorize an order. The authority contract reports `risk_override: false`, `risk_decision_creation: false`, `order_submission: false` and `risk_authority: NM-08 RiskEngine (sole authoritative risk engine)`. The real-workflow boundary is PI-21 review → NM-08 assessment → PI-12 orchestration → NM-10 paper execution.

## 13. PI-21 and PI-12 integration evidence

PI-22 is composed by `NovaMarketService` beside PI-21 and uses the same authoritative portfolio, valuation, forecast, instrument, research, agent, optimisation, pending-order, reservation, risk and runtime callbacks. Its recommendations carry the PI-21/NM-08/PI-12/NM-10 workflow. PI-22 never calls PI-12 execution directly and never mutates PI-21 state. Autonomy modes remain external: scheduled scans are refused in `disabled`; operator authorization, kill switch and recovery-to-disabled are preserved. Recovery only settles interrupted analytical records and never resumes trading.

## 14. API and frontend integration

The backend exposes 26 authenticated routes under `/market/risk-intelligence/*`: status, contracts, navigation, config read/update, analyze, overview, positions, concentration, correlation, volatility, drawdown, liquidity, regime, history, trend, recommendations, alerts list/action, scenario list/create, scenario run/list, stress list/run and recover. Reads require API authentication; analysis, configuration, alerts, scenario, stress and recovery actions additionally require the dedicated autonomy-operator token, checked server-side. No route contains an order, execution, fill or ledger mutation.

`docs/API.md` is regenerated from `ApiSurface` and `ROUTE_CONSUMERS` and is in sync at **883 routes**.

The React **Risk Intelligence** workspace (`web/src/pages/RiskIntelligence.tsx`, mounted at `#/riskintelligence` through `App.tsx` and `nav.ts`) has seven views: Risk overview, Position risk, Concentration & correlation, Volatility & drawdown, Liquidity, Scenarios & stress, and Alerts & recommendations. It displays actual backend evidence, dashboard values, capital/cash weights, market regime, scenario/stress results and alert/recommendation history, and includes the `multiple_pending_orders` scenario kind. It has no order, execution, cash, ledger or fill control. `npm run build` (`tsc -b && vite build`) passes; the existing 500 kB chunk-size warning remains. The page was compiled but not visually inspected in a browser during this audit.

## 15. Persistence and audit evidence

The store contains only `ri_*` tables and SQLite internals; it has no financial tables. Assessments, positions, metric snapshots, scenario configurations, scenario runs, stress runs, alerts, recommendations and events are immutable analytical records. Reopening the same database returns the same assessment and snapshot references; a repeated stress calculation returns the same digest. The store is closed on application shutdown, including when initialization or an analysis fails.

## 16. Actual test counts and results

| Check | Result |
|---|---|
| Focused PI-22 suite `tests/test_novamarket_pi22.py` | **41 passed** |
| PI-21 + PI-20 affected integration batch | **122 passed** |
| NovaMarket API route contract subset | **3 passed, 339 subtests** |
| API reference synchronization `tests/test_api_reference_sync.py` | **6 passed, 883 subtests** |
| Full `tests/test_novamarket_api.py` after route registration | **62 passed, 347 subtests** |
| Frontend production build `npm run build` | passed (existing chunk-size warning only) |
| Backend focused `ruff check` / `ruff format --check` | passed for the PI-22 package |
| Backend focused `mypy --platform win32` | passed for the PI-22 package, service and API |

The focused suite covers initialization, diversified and concentrated portfolios, position risk, drawdown, concentration, correlation alignment and misalignment, volatility/VaR/CVaR, liquidity including uncovered positions, regime, scenario analysis, cash depletion, volatility/correlation/combined scenario separation, cross-portfolio scenario rejection, pending exposure above cash, a missing-market-data headline that cannot remain normal, non-finite/non-numeric stress shocks (service and HTTP), recovery payloads, the recent valuation window, stress modes and reproducibility, alert deduplication and status changes, recommendations, NM-08 rejection/unavailability, disabled-mode scans, persistence across restart, resource limits, configuration validation, API authentication and operator authorization, a real created portfolio in the FastAPI application, and frontend/navigation wiring.

## 17. Defects fixed

- Pending orders were not extracted from the real `orders` response key; pending exposure read as zero. Fixed in the evidence reader.
- Insufficient-liquidity evidence omitted `risk_level`, causing a position-analysis crash on missing bars. Fixed to return explicit insufficient class/level and null metrics.
- Missing valuation fields were rendered as `0.00` for invested capital and P&L. Fixed to preserve `null` (missing is not zero).
- Hypothetical price scenarios incorrectly subtracted portfolio losses from cash. Fixed so price shocks change portfolio value, not available cash; cash depletion has its own explicit scenario.
- Pending-order and liquidity scenarios initially reused price-shock contributions. Fixed so pending orders affect only pending exposure and liquidity deterioration adds an explicit liquidation cost.
- The service store remained open during FastAPI shutdown, locking the test data directory on Windows. Added PI-22 shutdown closure.
- Scenario creation had no bounded-count check. Added the configured `MAX_SCENARIOS` guard.
- A saved scenario could be run against a different portfolio. Added ownership validation to scenario and stress execution.
- Non-finite or out-of-range stress shocks reached the analytics. Added finite/range validation in the service and a 422 response for a non-numeric HTTP body value.
- Portfolio liquidity ignored positions with insufficient evidence and could report a lower risk level. Added uncovered-position/weight evidence, a minimum elevated level and an explicit warning.
- Combined and liquidity scenarios treated missing impact evidence as zero cost. Added coverage, missing-evidence fields and fail-closed behaviour when no position is covered.
- Drawdown currency used the current total rather than the historical peak, and PI-22 could read the oldest valuation window on long histories. Added historical-peak currency, defensive timestamp sorting and a latest-window read path.
- The correlation matrix had no diagonal. Added `1.0` for instruments with stored history and `null` otherwise.
- Recovery changed the stress status column but left the persisted payload at `calculation_failed`. Recovery now updates the payload and records the recovery time.
- A normal headline could survive identical missing bar history for every holding. The headline now becomes `insufficient_data` when no market-risk component can justify normal.

## 18. Remaining limitations

- Historical volatility, VaR and CVaR depend on the stored observation window and provider data; they are not forecasts or maximum-loss guarantees.
- Correlation is unstable, requires exact timestamp overlap and can be unavailable for short or misaligned histories.
- Liquidity uses close × volume and explicit participation assumptions. It does not estimate true market depth or guarantee liquidation at the last price.
- Drawdown and valuation use stored authoritative snapshots; no prices or missing accounting values are reconstructed.
- Scenarios and stress tests are deterministic assumptions, not path simulations or probability forecasts. Tail dependence, intraday gaps and non-linear effects are not fully modelled.
- Regime evidence is only as good as the stored PI-04/PI-16 classification and confidence; uncertain regimes are not treated as confirmed.
- The risk model has not been calibrated against realised losses in this phase, so no probability-of-loss or confidence score is claimed.
- The frontend production build retains the pre-existing >500 kB chunk-size warning.
- The Risk Intelligence page was not visually inspected in a browser in this audit; only TypeScript/build checks were run.
- No live provider with order-book depth was available, so true market-depth behaviour remains unverified; the implementation reports depth as unavailable rather than fabricating it.
- No calibrated probability-of-loss model exists; assessment confidence remains deliberately `null`.
- Existing repository-wide `ruff check` findings outside the PI-22 package were not changed; the focused package, service and API checks pass. No broad cleanup was performed.

## 19. Objective-by-objective audit

| Objective / requirement | Result | Evidence |
|---|---|---|
| Portfolio overview and capital evidence | Met | Authoritative NM-07 values, cash/reservation weights and pending exposure; missing values null |
| Position, concentration and correlation risk | Met | NM-08 metric reuse, sector/industry/top-N/HHI, exact-timestamp correlation and matrix diagonal |
| Volatility, downside, VaR/CVaR | Met | NM-08 historical implementations with sample/assumption metadata |
| Drawdown, liquidity and market regime | Met | Latest valuation window, uncovered-liquidity fail-closed handling and stored regime evidence |
| Scenarios and stress | Met | Distinguished price, VaR, liquidity, pending, cash and combined semantics; deterministic digests |
| Early warnings and protective recommendations | Met | Deduplicated alerts, cash/pending warnings and advisory-only workflow |
| Evidence, persistence and auditability | Met | Isolated `ri_*` store, immutable records, provenance, recovery payload update |
| NM-08/NM-10/PI-21/PI-12 authority boundaries | Met | No order, fill, ledger, cash or portfolio mutation path; test and contract assertions |
| API and frontend integration | Met (compiled) | 26 routes, 7-view React workspace, API reference sync; visual browser check not run |
| Missing data never becomes zero risk | Met (with documented limits) | `insufficient_data`, warnings, uncovered liquidity and explicit coverage fields |
| Resource limits and failure handling | Met | Position/pair/history/alert/scenario bounds; 422/404/409 errors; non-finite shocks refused |

### Unverified or externally constrained requirements

- True market depth and executable liquidation prices cannot be verified without a provider that supplies order-book data. The implementation reports them as unavailable.
- Probability calibration against realised losses is not claimed and was not tested; no confidence score is fabricated.
- Browser rendering and interaction of the Risk Intelligence page were not visually verified.
- No deployment or live multi-user concurrency test was performed.

## 20. Final acceptance verdict

**PASS.** PI-22 is implemented in the actual NovaMarket application, uses authoritative snapshots and existing NM-08/NM-10/PI-21/PI-12 paths, provides instrument/position/sector/portfolio/regime/liquidity/capital/autonomy risk evidence, persists reproducible analytical history, exposes the required API and frontend, fails closed on missing or stale data, and has no path to financial mutation or order submission. The limitations above are documented analytical and environmental constraints, not unresolved blocking defects. PI-23 has not been started.
