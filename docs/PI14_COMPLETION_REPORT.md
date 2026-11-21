# PI-14 — Reward Calculation and Policy Evaluation completion report

**Status:** PASS for the PI-14 scope defined below after an independent re-audit of the implementation, tests and API/frontend integration.

**Phase:** PI-14
**Branch:** `feature/novamarket`
**Contract:** `pi14-reward-v1` (reward result schema `pi14-reward-result-v1`; policy evaluation schema `pi14-policy-evaluation-v1`)
**Date:** 2026-11-16
**Staged files:** 0 (no stage, commit, push, tag or merge was performed)

---

## 1. Initial assessment

PI-01 through PI-13 were verified PASS from their existing completion reports
before implementation. The worktree already contained the PI-01–PI-13 changes;
they were preserved. The repository was on `feature/novamarket`, and no files
were staged at the start or end of PI-14.

PI-14 was implemented as an additive `novacontrol.novamarket.rewards` package
and integrated into the existing `NovaMarketService`, FastAPI application and
React workspace. No production portfolio, ledger, position, paper-execution,
autonomy or operator-permission record was modified.

## 2. Existing components reused

| Existing capability | Reuse in PI-14 |
|---|---|
| PI-13 `RLEnvironment`, action/observation contracts, transitions and SQLite experience database | Reward calculation consumes stored `TransitionView` rows; policy evaluation drives fresh isolated environments |
| PI-13 simulated pricing, fee/slippage and private portfolio book | Execution-quality, cost and profitability evidence; no second execution engine |
| PI-02 dataset membership and PI-04 feature snapshots | Chronological evaluation windows and point-in-time observations |
| PI-08/PI-09 forecast snapshot contract | Decision-quality and forecast-utilization evidence, with validity/uncertainty/interval/horizon/age |
| NM-11 `calculate_metrics` and NM-08-compatible risk calculations | Financial, drawdown, volatility, Sharpe, Sortino and Calmar metrics; no competing risk authority |
| Existing FastAPI auth/error/route conventions and React `DataTable`/nav/status components | `/market/rewards/*` API and the Reward Evaluation page |

No market-data collector, feature engine, portfolio engine, risk engine,
execution engine or historical backtester was duplicated.

## 3. Reward architecture

```text
novamarket/rewards/
├── models.py       immutable configuration, split, result and schema contracts
├── components.py   eight independently registered reward components
├── engine.py       normalization, aggregation, attribution and result hashes
├── policies.py     no-action, rule-based, PI-12 adapter and seeded experimental RL
├── metrics.py      financial/risk/trade/decision/learning metric composition
├── evaluation.py   dataset windows, isolated runs, comparison, walk-forward
├── store.py        four `rl_*` tables in the isolated research SQLite database
└── service.py      RewardService facade consumed by NovaMarketService and FastAPI
```

The engine consumes a validated transition, the previous/next observation and
simulated portfolio state, execution evidence, history, environment metadata
and a versioned reward configuration. It never writes to the environment book.

## 4. Reward component implementation

| Component | Evaluated evidence |
|---|---|
| `profitability` | Equity change, realized/unrealized P&L, return on deployed/available capital, capital utilization; fees are added back before the return calculation and deducted once through the cost component |
| `risk` | Exposure, concentration, drawdown and optional annualized volatility evidence; negative penalties for excess |
| `transaction_cost` | Brokerage/charges/tax, slippage, cost ratio and cost per notional; costs counted once |
| `decision_quality` | Separate BUY, SELL, HOLD, WAIT and NO_ACTION logic using only information available at the decision timestamp; a reasonable loss is not automatically penalized |
| `forecast_utilization` | Directional alignment, expected return, calibrated probability, interval, uncertainty, disagreement, horizon and validity; abstention is allowed and not punished without evidence |
| `discipline` | Repeated/reversing orders, churn/turnover and constraint violations; frequency alone is not treated as bad trading |
| `portfolio_management` | Capital utilization, concentration, exposure balance, stability, cash and drawdown control; deployment is not rewarded for its own sake |
| `execution_quality` | Slippage, fees, fill ratio, participation, order size and rejected/unfilled orders against the simulated execution result |

The component registry supports enable/disable, per-component weights, scales,
bounds, named risk/decision/discipline/portfolio thresholds and penalties.
Thresholds are part of the immutable `RewardConfig` hash (`DEFAULT_THRESHOLDS`
includes the high-exposure, concentration, drawdown, volatility, weak-forecast,
repeat-lookback, repeat-penalty and exposure-change thresholds). Unknown
components or thresholds, non-finite values, invalid weights, invalid bounds,
zero/negative thresholds, missing weights for enabled components and invalid
penalty names are rejected.

### Decision-quality boundaries

- SELL is valid only with an existing long position.
- BUY evaluates forecast validity/uncertainty, expected return, cash and existing exposure.
- HOLD evaluates whether the existing position and forecast remained reasonable.
- WAIT/NO_ACTION evaluates insufficient evidence, uncertainty and existing exposure; favorable future prices are not used to call abstention a mistake.
- The attribution explicitly records `future_outcome_used: false`.

## 5. Calculation methodology

For enabled component `c`:

```text
normalized_c = clamp(raw_c / scale_c, -1, 1)
total = clamp(sum(weight_c * normalized_c) - applicable_penalties,
              bounds_min, bounds_max)
```

The component's raw evidence is evaluated against the configured thresholds
before normalization; the threshold map is versioned with the weights, scales,
bounds and penalties. `missing_policy` is `renormalize` (available enabled
weights are renormalized) or `zero`. Raw values, normalized values, weights, scales, penalties, missing
components and excluded components are all retained. The default bounds are
`[-1, 1]`; the episode total is the sum of transition rewards and is therefore
not itself bounded.

The shipped default weights (0.20 profitability; 0.15 risk; 0.15 decision
quality; 0.10 each for cost, forecast, discipline, portfolio management and
execution) and scales are **experimental defaults**, not financially validated
parameters. No claim is made that they maximize real-world return.

Cost handling was checked to avoid double counting: profitability adds simulated
fees back to the equity return, and the transaction-cost component subtracts the
fee/slippage evidence once. Short samples report `null`/insufficient states for
Sharpe, Sortino, Calmar and volatility rather than inventing annualized values.

## 6. Reward reproducibility evidence

- `RewardConfig` is immutable and deterministic: `config_id` and `config_hash`
  are derived from canonical configuration content.
- `RewardResult` hashes the configuration, transition source, component evidence
  and versions into `repro_hash`.
- The focused test calculates the same transition twice and gets the same
  `result_hash` and `reward_id`, while a changed weight produces different
  configuration and result hashes.
- Historical rows are append-only; a changed configuration creates a new
  result/configuration identity and does not rewrite old records.
- Persistence/recovery tests reopen the isolated store and verify stored
  configurations, rewards, evaluations and comparisons remain readable.

## 7. Reward attribution evidence

Each result exposes the source transition/episode/environment/sequence,
configuration ID/version/hash, calculation timestamp, data/dataset and
environment versions, all component contributions and penalties, raw/normalized
values, weights, thresholds, missing/excluded components and a sorted
attribution list. The episode endpoint returns the per-transition reward
payloads alongside a summary that aggregates them and includes an explicit
`trade_attribution` list (one row per simulated BUY/SELL with the component
contributions and total reward). Policy evaluations retain
config/dataset/split/conditions and per-policy reward summaries including
`component_totals`, `component_averages` and `penalty_totals`. No unexplained
reward number is returned by the API.

## 8. Policy evaluation implementation

`PolicyEvaluationEngine` builds a chronological dataset window, creates a fresh
`RLEnvironment` with private cash/positions per policy, steps the policy through
completed bars, calculates every transition reward and aggregates:

- financial: total/cumulative return, annualized return when observations and
  assumptions permit, realized/unrealized P&L and equity progression;
- risk: maximum drawdown, volatility, downside deviation, Sharpe, Sortino,
  Calmar, exposure and concentration, with explicit validity states;
- trading: trades, wins/losses, win rate, average win/loss, profit factor,
  holding duration, turnover and costs;
- decision: action distribution, invalid-action frequency, unnecessary trading
  and constraint violations;
- learning: episode reward distribution, `episode_completion`,
  `episode_truncated`, `episode_cancelled` and an explicit `training not
  implemented` marker.

Invalid policy actions are counted and safely replaced by WAIT; they are not
silently treated as successful decisions. Evaluations are bounded by
`max_transitions` and the environment's own step limit; reaching either limit is
reported as `truncated`, never as a completed episode. Trade metrics use the
simulated execution time (`execution.at`), not the earlier decision timestamp.

## 9. Baseline comparison results

Four baselines use the same dataset, period, initial virtual capital, instrument,
simulation/fee/slippage assumptions and risk constraints:

| Baseline | Type | Behaviour |
|---|---|---|
| `no_action` | deterministic control | always NO_ACTION |
| `rule_based` | deterministic rule | forecast/drawdown entry and exit rule |
| `pi12_adapter` | read-only evidence adapter | PI-12-style forecast confidence/age rule; not the production pipeline |
| `experimental_rl` | seeded, non-trained comparison policy | forecast/RSI heuristic; not a trained model |

The deterministic fixture comparison (`40` transitions, one instrument) produced
the same condition fingerprint for all four policies, confirming identical
conditions. All four runs were truncated at the configured evaluation bound, not
claimed as completed episodes:

| Baseline | Actions | Transitions | Trades | Costs (INR, fees + slippage) | Synthetic fixture return |
|---|---|---:|---:|---:|---:|
| `no_action` | 40 NO_ACTION | 40 | 0 | 0 | 0.0000% |
| `rule_based` | 1 BUY, 39 HOLD | 40 | 1 | 17.77 | 6.7945% |
| `pi12_adapter` | 1 BUY, 39 HOLD | 40 | 1 | 13.35 | 5.0959% |
| `experimental_rl` | 1 BUY, 39 HOLD | 40 | 1 | 16.01 | 6.1151% |

These are deterministic synthetic-fixture results, not real-market performance
and not a policy recommendation. The fixture's monotonic price path produces
unrealistically smooth risk ratios, so no profitability or Sharpe claim is made
from it. Win rate is `null` because no SELL closed a trade, which is the honest
insufficient-sample state. The earlier report table showed fees only; after the
audit fixed NM-11 slippage accounting, the cost column includes both fees and
slippage, and the regression fixture was recomputed above.

## 10. Chronological validation evidence

- Named splits must be chronological, ordered and non-overlapping; overlapping
  windows, a test window that starts at the training boundary or before the
  training window, and missing split names are rejected.
- A split must lie inside the environment window; evaluation loads history up to
  the split, never rows after it, and feature snapshots during the split receive
  the pre-split history needed for indicator warm-up.
- `split="all"` is the only mode allowed without explicit split definitions.
- The test evaluates the test window and asserts the resulting dataset start/end
  exactly match the test split.
- No random time-series split, test fitting, reward-normalization fitting or
  test-result reuse is implemented.

## 11. Leakage-prevention evidence

- Feature and forecast loaders are point-in-time and the PI-13 observation marks
  post-observation feature rows as `future_leak_blocked`.
- Forecast utilization records `future_outcome_used: false`; decisions use the
  forecast snapshot available at the decision timestamp.
- Chronological split validation prevents train/test overlap.
- Evaluation records the dataset hash/version and split definition.
- No learned normalization exists; the fixed configuration scales are versioned
  and stored with each result.

## 12. Stability and robustness results

The service exposes walk-forward windows and a stability comparison over
multiple chronological test windows, with the same policy/config conditions.
Focused tests exercise walk-forward and stability endpoints/service methods and
verify that results are returned as bounded experimental evidence.

Honest limitation: the repository does not contain a seeded, representative
multi-regime historical dataset for a genuine bull/bear/sideways, high/low
volatility or multi-instrument generalization study. PI-14 therefore does not
claim policy stability or generalization from its synthetic fixture. Small
configuration sensitivity is supported by the same walk-forward surface but is
not claimed as a validated hyperparameter study.

## 13. API and frontend integration

The additive surface has **17 FastAPI routes over 15 distinct paths** under
`/market/rewards`, covering status, contracts, health, baselines, configuration
create/list/detail, episode calculation/detail, result detail, evaluation
create/list/detail/cancel, comparisons, walk-forward and stability.

All routes require the existing bearer authentication. Evaluation and calculation
work run through `asyncio.to_thread` with bounded transition counts. Cancellation
is cooperative at simulation step boundaries, keyed by `request_id`; the partial
evaluation is persisted and can be rerun from its stored configuration. It is not
a mid-run checkpoint/resume mechanism.

The React **Reward Evaluation** workspace displays configuration/version/
weights/bounds/experimental status, component attribution and penalties, policy
metrics (return, drawdown, Sharpe/Sortino, win rate, profit factor, costs,
turnover, action distribution, episode reward), evaluation dates and comparison
conditions. It has no policy-promotion, activation or live-trading control.

`docs/API.md` was regenerated with **688 documented routes** and the generator
reports it in sync. (The generator is run with `PYTHONPATH=src` from the
`novamarket` directory so it imports the NovaMarket API surface, not the outer
NovaControl package.) The running app reports 694 FastAPI route objects in
total; the reward-specific count is the 17 above.

## 14. Isolation and security evidence

- The reward package imports no `ExecutionEngine`, `ExecutionStore`,
  `PortfolioEngine`, `PortfolioStore`, `RiskEngine` or `AutonomyRuntime`; the
  focused source-scan test enforces this.
- Reward/evaluation records live in `novamarket_rl.sqlite3` under `rl_*` tables,
  separate from production financial records.
- No route can create a production fill, position, ledger entry, NM-10 order or
  autonomy change; no promotion/activation endpoint exists.
- Existing long-only semantics are preserved: SELL requires an existing long
  position.
- Policy evaluation uses a private environment per policy, so policies cannot
  share mutable portfolio state.
- NM-08 remains the sole authoritative risk engine and NM-10 the sole
  authoritative paper-execution engine; PI-14 computes research metrics and
  consumes simulated evidence only.

## 15. Actual test counts and results

| Check | Command | Result |
|---|---|---|
| Focused PI-14 | `PYTHONPATH=src python -m pytest tests/test_novamarket_pi14.py -q` | **45 passed** |
| Affected regression | `PYTHONPATH=src python -m pytest tests/test_novamarket_pi13.py tests/test_novamarket_backtesting.py tests/test_novamarket_api.py tests/test_novamarket_pi12.py -q` | **173 passed, 273 subtests passed** |
| Route contract/web surface | `PYTHONPATH=src python -m pytest tests/test_web_platform.py -k "route or consumer or surface or docs" -q` | **4 passed, 42 deselected, 701 subtests passed** |
| API reference | `PYTHONPATH=src python scripts/generate_api_reference.py --check` | **in sync (688 routes)** |
| Ruff | `python -m ruff check src/novacontrol/novamarket/rewards tests/test_novamarket_pi14.py` | **All checks passed** |
| Mypy | `PYTHONPATH=src python -m mypy src/novacontrol/novamarket/rewards` | **Success: no issues in 9 source files** |
| Frontend build | `cd web && npm run build` | **PASS** (`tsc -b` + Vite; only existing dynamic-import/chunk-size warnings) |
| Diff hygiene | `git diff --check`; `git diff --cached --name-only` | **No whitespace errors; 0 staged files** |

The full project test suite and a real browser interaction pass were not run;
the focused feature tests, affected dependency/API regression and production
frontend build are the evidence used here. The Reward Evaluation page was not
visually inspected in a browser in this phase.

## 16. Defects fixed

1. Exposure-ratio reward used a denominator of portfolio value although the
   portfolio field is already a ratio; corrected to avoid double division.
2. Reward context compared the wrong pre-trade exposure value; corrected.
3. Short samples omitted `sharpe`, `sortino`, `calmar` and `volatility` keys;
   added explicit `null` insufficient-sample fields.
4. SELL decision-quality attribution used an invalid reason for a missing
   position; corrected to the explicit long-position requirement.
5. A focused test leaked a SQLite connection and locked its temporary database
   on Windows; the test now closes the service in `finally`.
6. The evaluation API rejected `request_id` as an unknown configuration field;
   transport metadata is now stripped before immutable contract validation.
7. Persisted evaluation provenance omitted a stable dataset ID and dataset
   version; both are now stored with the dataset evidence.
8. Baseline verification exposed that the PI-09 fixture used `expected_return`
   while PI-13's observation contract consumes `predicted_return`; the test
   fixture now carries the actual PI-09 field, so rule-based and PI-12 baselines
   genuinely trade in the comparison instead of all abstaining.
9. Cancellation requests could accumulate without a bound; the pending-request
   registry is now limited to 128 IDs with bounded request-ID length, and the
   overflow behaviour is tested.
10. Chronological split validation accepted a test split that started exactly at
    the training window's end and a test window ordered before training; both
    now fail validation, and the pre-split feature warm-up history is retained.
11. PI-13 environment truncation at its step limit was reported as a completed
    evaluation; it now reports `truncated` and the learning metrics carry
    `episode_completion = false` and `episode_truncated = true`.
12. Trade metrics used the decision timestamp as `executed_at`; they now use the
    simulated fill time from `execution.at` with an explicit fallback only when
    the field is absent/invalid.
13. NM-11 trade metrics multiply `Trade.slippage` by quantity, but PI-14 passed
    the total fill slippage; slippage and `total_costs` were therefore
    overstated. PI-14 now converts to per-unit slippage before constructing the
    trade, with fees and slippage retained as separate totals.
14. Annualized return was reported from samples too short for the stated
    annualization basis; it is now `null` alongside the existing short-sample
    Sharpe/Sortino/Calmar/volatility states.
15. A SELL with no valid forecast was scored as unconditionally positive; it is
    now neutral-to-negative unless drawdown itself supplies exit evidence.
16. `unnecessary_trading` counted every reversal; it now counts only the
    repeated-trade condition, leaving ordinary position changes out of the
    penalty.
17. A policy exception escaped as an unwrapped implementation error; policy
    failures are now `PolicyEvaluationError` with policy and transition context.
18. Evaluation concurrency, comparison history, split count, policy count,
    walk-forward window count and thresholds were unbounded or hard-coded;
    concurrency is capped at two, storage/history is bounded, split/policy/window
    counts are validated, and risk/decision/discipline/portfolio thresholds are
    configurable and versioned in the reward configuration hash.

## 17. Remaining limitations

- No RL training algorithm (PPO/DQN/A2C/SAC) is implemented; `experimental_rl`
  is a seeded heuristic baseline, not a learned or promoted model.
- PI-13 supports one instrument per environment; no cross-sectional or
  multi-instrument generalization is claimed.
- Completed daily bars and synthetic next-open slippage/fees only; no intraday,
  order-book, queue or latency simulation.
- No seeded real-market dataset or representative multi-regime sample was
  available for a meaningful robustness/stability study; fixture results are
  not real-world profitability evidence.
- The risk component uses available transition/portfolio risk evidence; it does
  not invoke the production NM-08 decision path, by design and isolation.
- Forecast utilization measures decision-time alignment, not forecast accuracy;
  no duplicate accuracy calculation was introduced. PI-11 evaluation data is not
  ingested into the isolated reward store.
- Cancellation persists partial evidence but does not checkpoint/resume a
  half-finished environment run.
- The threshold defaults are experimental, like the weights and scales; the
  audit made them configurable and versioned but did not claim that any setting
  is financially validated.
- The frontend was compiled but not visually exercised; no screenshot or
  browser button walkthrough is claimed.
- Only focused and affected-area regressions were run; the entire repository
  suite was intentionally not run.

## 18. Final acceptance verdict

**PASS for PI-14 scope.** The reward engine, eight components, configurable and
versioned weights/scales/bounds/thresholds/penalties, episode/policy/trade
attribution, persistence, policy evaluation, four baselines, chronological
split/leakage controls, walk-forward/stability surfaces, API, frontend,
cancellation and financial-isolation checks are implemented and verified by the
focused and affected regressions above (**45 focused tests; 173 regression tests
plus 273 subtests**). The limitations in section 17 are explicit and do not turn
synthetic results into a profitability or generalization claim.

PI-15 was not started. No files were staged, committed, pushed, tagged or merged.