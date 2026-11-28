# NovaMarket — Reinforcement Learning Evaluation and Adaptive Trading Intelligence

**Status:** Evaluation complete; no production policy promoted; no live trading.
**Date:** 2026-11-21
**Scope:** PI-13 (isolated environment), PI-14 (rewards/evaluation), PI-15 (DQN training), PI-16 (adaptive suitability), PI-17 (advisory RL agent), plus existing strategies and backtesting.
**Compliance:** PI-25 was not created, the 24-PI roadmap was not modified, no live order path was enabled, no policy was activated or promoted, and no commit or push was made.

---

## Verdict

**Evidence is insufficient to demonstrate that Reinforcement Learning provides a measurable improvement to NovaMarket's trading intelligence.**

This is not a neutral “no result” verdict. The current DQN implementation failed the minimum behavioural test: **all ten freshly trained policies made zero trades on the untouched test windows**, and all three existing tested policies in `novamarket_rl.sqlite3` also made zero trades. The policies either held cash or emitted `no_action` for every test observation. A zero-trade cash policy cannot demonstrate trading skill merely because the default reward is positive.

The default reward accumulated **31.82 total reward** across the five default-reward no-op test episodes while portfolio return was **0.0%**. The trading-focused reward accumulated **0.0** because abstention was assigned zero value and every trade paid costs. Both outcomes are reward-design evidence, not profitability evidence.

If the current artifact is judged only on deployed capital, it is **worse than the conventional strategies that actually took positions**: it captured none of the +22.87% buy-and-hold move in `XNSE:TCS`, and its 0.0% return only looks acceptable because three of the five buy-and-hold benchmarks were negative in this particular test window. The correct governance conclusion is **reject the current policy for promotion and require reward/action/state redesign before another out-of-sample claim**.

---

## 1. Existing RL architecture

The repository contains a substantial RL **infrastructure**, but not a production RL trading system.

| Layer | What exists | What it does | Important limit |
|---|---|---|---|
| PI-13 `rl/` | `RLEnvironment`, point-in-time observation builder, private simulated portfolio, experience records | Single-instrument daily `reset/step` environment; next-completed-bar fills; NM-10-style fee/slippage functions | Daily completed bars only; one instrument per environment; no order-book depth; no production book |
| PI-14 `rewards/` | `RewardEngine`, eight default components, optional `cash_drag` experiment, `PolicyEvaluationEngine`, `policy_metrics`, regime slices | Bounded, versioned reward aggregation and policy evaluation | Weights are explicitly experimental; the default reward still permits abstention rewards, while the optional cash-drag configuration excludes them |
| PI-15 `agent_training/` | `DQNAgent`, replay buffer, target network, pure-Python MLP, `TrainingManager`, checkpoints, walk-forward helpers | Chronological training, validation, test, checkpoint selection, policy artifacts | Only DQN is implemented; no Double DQN, PPO, A2C or SAC |
| PI-16 adaptive | Regime suitability and policy catalogue | Advisory regime/suitability evidence | No RL policy can change risk limits, permissions or activation |
| PI-17 agent | `RLPolicyIntelligenceAgent` | Reads eligible `validated`/`tested` policy artifacts and emits an advisory action | It does not choose between strategy families and has no order path |

### Isolation and authority

The service contract reports these isolation flags as `false`:

- `automatic_policy_promotion`
- `activation_available`
- `production_authority`
- `nm10_order_submission`
- `nm08_bypass`
- `production_portfolio_mutation`
- `production_ledger_write`
- `production_fill_creation`
- `live_trading`
- `autonomy_mode_change`
- `operator_authorization_change`
- `broker_connection`

The environment owns a separate simulated cash balance, position, equity, ledger and experience store. It has no controller, order gateway, NM-08 risk handle or production portfolio handle. This is the correct safety boundary and was preserved.

### Existing stored RL evidence

`novamarket/data/novamarket_rl.sqlite3` was inspected read-only:

| Item | Count / observation |
|---|---:|
| Training runs | 6 |
| Training configurations | 6 |
| Checkpoints | 21 |
| Policies | 6 (3 `tested`, 1 `rejected`, 2 `training`) |
| Stored evaluations | 6 |

Every `tested` policy had the same failure mode:

| Split | Transitions | Action distribution | Trades | Total return | Total reward |
|---|---:|---|---:|---:|---:|
| Validation | 44 | 44 × `no_action` or 44 × `wait` | 0 | 0.0% | 3.91690351 |
| Test | 40 | 40 × `no_action` or 40 × `wait` | 0 | 0.0% | 3.47225 |

The stored test comparisons also contained `no_action`, `rule_based` and `pi12_adapter` baselines. All three made **40 no-op decisions**, zero trades and 0.0% return. That comparison was therefore vacuous: it compared several ways of doing nothing.

**Finding:** the repository already contained direct evidence of reward-driven policy degeneration before this evaluation. It had not been interpreted as a trading failure because the reward was positive.

---

## 2. Implemented algorithms

### Implemented

**Deep Q-Network (DQN) only.**

- `agent_training/agent.py`: `DQNAgent`, replay buffer, target network, epsilon-greedy action selection, pure-Python dense network.
- `agent_training/training.py`: chronological split preparation, validation/test evaluation, checkpoint retention, resumption metadata, optional bounded walk-forward.
- Training contract: discrete five-action space, fixed numeric observation projection, offline value learning.
- Algorithm version in the existing contract: `pi15-dqn-pure-python-1`.

### Not implemented

- Tabular Q-Learning.
- Double DQN.
- Dueling DQN.
- PPO.
- A2C / advantage actor-critic.
- SAC or any continuous-action policy.

`ExperimentalRLPolicy` in `rewards/policies.py` is a seeded, untrained forecast/RSI heuristic used as a comparison baseline. It is **not** a trained RL algorithm and must not be counted as one.

**Decision:** no additional algorithm was added. Adding PPO, Double DQN and actor-critic implementations would not repair a reward that makes abstention optimal, and would only increase the unvalidated surface area. The evidence points to reward, action and state design first.

---

## 3. State and action representation

### State actually encoded

`ObservationEncoder` produces a fixed **21-value** vector from `BASE_PATHS` (PI-15 encoder version `pi15-observation-encoder-v1`):

| Group | Encoded values |
|---|---|
| Market (12) | close, `return_1`, `return_5`, `return_20`, gap %, intraday range %, `volatility_20`, relative volume, price vs SMA20, price vs SMA50, support distance, resistance distance |
| Portfolio (4) | cash, equity, exposure, drawdown |
| Forecasts (5) | expected return, `p_up`, `p_down`, uncertainty width, disagreement |

The full observation contains additional point-in-time evidence (positions, realized/unrealized P&L, quality/provenance, raw OHLCV, support/resistance, evidence of forecast abstention), but the encoder does **not** project most of it.

### Missing state that matters

- Position quantity and average entry price.
- Unrealized P&L and realized P&L trajectory.
- Position concentration versus the 25% position cap.
- Remaining risk budget, order-size limit, available cash after costs.
- Whether a forecast is absent, invalid, stale or explicitly abstained.
- Market regime label or regime probabilities.
- 20-minute / 5-minute / 1-minute confirmation layers.
- Recent trade history, fill status and rejection history.
- Time since entry and holding period.

The observation does contain a `positions` list and `unrealized_pnl`, so the information exists; the encoder discards it. This is a representation gap, not a data-availability claim.

### Future-information protection

The observation builder rejects feature/forecast rows dated after the decision time. The harness used `include_features=False` and `include_forecasts=False`, so this evaluation could not leak future features through those paths. The environment fills at the next completed bar. The test split was never used for training, validation, checkpoint selection or reward-configuration selection.

### Action space

| Action | Effect | Authority |
|---|---|---|
| `buy` | Long entry using fixed `DEFAULT_SIZE_FRACTION = 0.20` of equity (bounded by cash, 25% position cap and ₹25,000 order cap) | Simulation only |
| `sell` | Long exit using the same fixed 0.20 fraction; five sells are normally required to close a full position | Simulation only |
| `hold` | No book change | Simulation only |
| `wait` | No book change | Simulation only |
| `no_action` | No book change | Simulation only |

There is no shorting, leverage, order type, limit price, stop price or direct risk-engine call. The agent proposes an action; `RLEnvironment` validates and can reject it.

### Action-space defects observed

1. **Fixed 20% exit sizing.** A `sell` closes only 20% of the position. In a policy that chose repeated sells, the environment recorded repeated partial exits rather than one exit. The action vocabulary cannot express “exit all.”
2. **Index tie-breaking.** With an untrained or indifferent Q-function, `DQNAgent.act`/argmax can select action index 0 (`buy`) simply because it is first. The evaluation harness applies explicit masking and makes indifference abstain, but the production `TrainedPolicy.select` still uses raw argmax. Invalid orders are rejected by the environment, not prevented by the policy adapter.
3. **Three no-op actions.** `hold`, `wait` and `no_action` are behaviourally identical in the book but are not identical in the reward: `decision_quality` and `forecast_utilization` treat them differently in some branches. That makes the action semantics inconsistent for learning.
4. **No sizing or exit intent.** The agent cannot choose 20%, 50%, 100%, a risk-budgeted size or an explicit exit, so it cannot learn position management beyond a fixed tranche.

None of these defects permits bypassing the risk engine. The environment's 25% position cap, 100% gross cap, cash check and long-only check remain authoritative.

---

## 4. Reward-engine analysis

### Formula

For each transition:

```text
total = clip(
          Σ_i (w_i × clip(raw_i / scale_i, -1, 1)) / Σ_i w_i_available
          - Σ_p (penalty_weight_p × penalty_p),
          -1, 1)
```

The engine uses bounded normalisation, a missing-component policy of `renormalize`, and fixed scales. It charges costs through `transaction_cost` while `profitability` is gross of fees and explicit slippage, avoiding double counting.

Default weights (sum = 1.0):

| Component | Weight | Scale | Main signal |
|---|---:|---:|---|
| `profitability` | 0.20 | 0.02 | Gross mark-to-market equity return |
| `risk` | 0.15 | 0.20 | Excess exposure, concentration, drawdown, volatility |
| `transaction_cost` | 0.10 | 0.01 | Fees plus explicit slippage |
| `decision_quality` | 0.15 | 1.0 | Directional forecast alignment and abstention logic |
| `forecast_utilization` | 0.10 | 1.0 | Forecast use or reasonable abstention |
| `discipline` | 0.10 | 1.0 | Repeated/reversed trades, rejected executions |
| `portfolio_management` | 0.10 | 1.0 | Concentration, drawdown, exposure instability |
| `execution_quality` | 0.10 | 1.0 | Fill ratio and participation |

Default penalties: `constraint_violation = 0.25`, `unfilled_trade = 0.10`, `repeated_trade = 0.05`.

### Why abstention won

With no forecast available, the default components pay:

| Action when flat | `decision_quality` | `forecast_utilization` | Weighted contribution |
|---|---:|---:|---:|
| `no_action` / `wait` | +0.40 | +0.35 | **+0.095** |
| `hold` | +0.15 | +0.35 | **+0.0575** |
| `buy` | -0.35 | -0.35 | **-0.0875 before costs/risk** |

Thus a no-op is not neutral in the default configuration; it is a positively rewarded action. A buy without forecast evidence is penalized twice, before transaction costs. The observed sign is exactly what the population-level DQN learned.

The evaluation also used a **trading-focused** configuration, fixed before test:

| Component | Default | Trading-focused |
|---|---:|---:|
| `profitability` | 0.20 | 0.60 |
| `risk` | 0.15 | 0.10 |
| `transaction_cost` | 0.10 | 0.15 |
| `decision_quality` | 0.15 | 0.00 |
| `forecast_utilization` | 0.10 | 0.00 |
| `discipline` | 0.10 | 0.15 |
| `portfolio_management` | 0.10 | 0.00 |
| `execution_quality` | 0.10 | 0.00 |

This removed the abstention subsidies, but then a no-op paid exactly **0.0** and every trade paid non-negative transaction costs. With no opportunity-cost term, cash remained the risk-free optimum. The policy learned `hold` for every test step, with zero trades and zero reward.

### Experimental cash-drag configuration

The optional `cash_drag` component was added without changing the default reward: `default_reward_config()` still enables only the original eight components, and `RewardEngine()` still exposes only those eight. The experiment uses a separate `EXPERIMENTAL_COMPONENT_REGISTRY` and applies:

- `cash_drag`: `-max(0, cash_weight × expected_return)`; one-sided, so it never pays for holding cash. When the baseline observation has no forecast, it uses the decision-time trailing `market.return_5` proxy, never a future bar.
- No `decision_quality` and no `forecast_utilization`: both contain unconditional positive values for `wait`/`hold` without evidence.
- Weights: `profitability` 0.35, `risk` 0.10, `transaction_cost` 0.10, `discipline` 0.05, `portfolio_management` 0.05, `execution_quality` 0.05, `cash_drag` 0.30.

The same five instruments, chronological split, 20 training episodes and seed as the main evaluation were trained on 2026-11-21. The experiment is reproducible with `evaluation/rl_strategy/run_cash_drag_experiment.py`; its compact evidence is `evaluation/rl_strategy/cash_drag_results_compact.json`.

**Out-of-sample result through the adapter: 5 executed trades, one per instrument.** The test episode now selects with `TrainedPolicy.select`, the masked-argmax path the adapter exposes; on these zero-Q checkpoints the tie selects the first legal action, BUY, so all five test policies choose `buy` for every observation (87–90 buys each) and one fill executes before the environment rejects the rest (433 constraint rejections in total; `invalid_actions` stays 0). The five policies returned −1.48% to +4.30% (mean +0.32%) with ₹39.34 in fees and −218.3620 total reward. This is not learned trading: every test Q-value vector is identical (zero spread), so the trades are tie-breaking, not skill, and the policy should not be promoted.

### Reward-hacking assessment

**Yes, the existing reward is hackable by abstention.** The evidence is direct, not hypothetical:

- Existing tested policies: 0 trades, positive test reward 3.47225.
- Fresh default-reward policies: 0 trades, positive test reward 31.82 total across five episodes (individual totals 5.0025–8.55).
- Fresh trading-reward policies: 0 trades, exactly 0.0 reward.
- The difference between the two configurations is the reward’s own subsidy for no-ops and forecast-missing abstention.

Other reward findings:

| Concern | Finding |
|---|---|
| Unrealized gains treated as completed profits? | `profitability` uses gross equity return, which includes mark-to-market unrealized changes. `realized_pnl_delta` and `unrealized_pnl_delta` are reported but are not separately weighted. The reward therefore credits unrealized moves; it does not claim they are realized cash, but the distinction is not enforced in the scalar. |
| Deposits counted as reward? | No deposit or funding API exists in the environment; initial capital is fixed. No deposit reward was observed. |
| Repeated actions generate artificial reward? | Repeated no-ops receive positive default reward. `DisciplineComponent` penalizes repeated **trades** and reversals, not repeated abstention. |
| Excessive trading penalized? | Yes, through transaction costs, turnover, discipline and execution penalties. In this sample the penalty was strong enough to make any trade unattractive. |
| Risk violations rewarding? | No. A rejected execution gives `decision_quality = -1`, `execution_quality = -1` and a constraint penalty; it is not a profitable branch. |
| Costs counted once? | Yes. `profitability` is gross; `transaction_cost` owns fees and explicit slippage. |

**Conclusion:** the scalar is internally consistent but misaligned with the objective. It optimizes “avoid unsupported trading” and, in the trading configuration, “avoid trading costs.” Neither is the same as “maximize risk-adjusted portfolio return relative to available alternatives.”

---

## 5. Training methodology

### Isolated harness

A dedicated harness lives at `evaluation/rl_strategy/run_rl_evaluation.py`. It reads `data/novamarket.sqlite3` read-only, writes runtime output under `tmp/rl_evaluation/`, the tracked compact extract `evaluation/rl_strategy/results_compact.json`, tracked policy artifacts under `evaluation/rl_strategy/artifacts/policies/`, and exact test-split bars under `evaluation/rl_strategy/artifacts/test_bars/`. It does not open the production portfolio, ledger, risk engine, order gateway, autonomy state or policy registry.

The harness was originally run end-to-end on 2026-11-21 and rerun on 2026-11-24; the current tracked compact evidence in `evaluation/rl_strategy/results_compact.json` and the tracked policy artifacts in `evaluation/rl_strategy/artifacts/policies/` are from the rerun, and the full runtime output is in `tmp/rl_evaluation/results.json`. The exact test-split bars used by the no-retraining adapter evaluator are tracked under `evaluation/rl_strategy/artifacts/test_bars/`.

### Instruments and chronological split

Five daily instruments with at least 248 stored bars were used:

- `XNSE:RELIANCE`
- `XNSE:HDFCBANK`
- `XNSE:TCS`
- `XNSE:ITC`
- `XNSE:TATASTEEL`

| Split | Window | Bars per instrument |
|---|---|---:|
| Train | 2025-11-04 to 2026-04-30 | 120 |
| Validation | 2026-05-01 to 2026-07-01 | 40 |
| Test | 2026-07-02 to 2026-11-04/09 | 88 (4 instruments), 91 (RELIANCE) |

The test window was frozen before training. It was not used to choose the reward configuration, hyperparameters, checkpoint or action mask.

### DQN configuration

| Parameter | Value |
|---|---|
| Input dimension | 21 |
| Hidden layers | (24,) |
| Activation | ReLU |
| Parameters | 653 |
| Optimizer | Adam |
| Learning rate | 0.003 |
| Gamma | 0.95 |
| Epsilon | 1.0 → 0.10, decay 0.995 |
| Replay capacity | 2,000 |
| Batch size | 32 |
| Target update interval | 20 |
| Training episodes | 20 per instrument/configuration |
| Seed | 7 |
| Validation checkpoint selection | Every 5 episodes; score = validation return − 0.5 × |drawdown|; highest score wins |
| Test selection | Frozen best validation checkpoint; no test feedback |

Two reward configurations were trained independently for each instrument: `evaluation-default` and `evaluation-trading`. Ten policies were produced. Each policy is a local JSON artifact, explicitly marked `simulated`, `advisory_only`, `production_authority: false`, `activation_available: false`.

### Existing training-manager capabilities

The production training manager already supports:

- Chronological train/validation/test preparation.
- Validation-selected policies and a separate held-out test report.
- Checkpoints, pause/resume and bounded retention.
- Walk-forward fold construction with overlap refusal.
- Training metadata, resource snapshots, stability checks and non-finite loss handling.
- Policy status transitions with an explicit rejected/archived path.

This evaluation used one split per instrument, not a multi-fold walk-forward. That is a limitation of the evidence, not a missing production capability.

---

## 6. Dataset and validation methodology

### Data inventory used

| Item | Observed |
|---|---|
| Source | `novamarket/data/novamarket.sqlite3`, `market_bars`, `interval = '1d'`, `partial = 0` |
| Instruments in database | 150 |
| Instruments with ≥ 200 daily bars | 124 |
| Instruments selected | 5 |
| Test bars | 443 total |
| RL scored transitions | 438 (next-bar fill consumes one bar per instrument) |
| Intraday data | 3 instruments, 1-minute only; no native 5-minute series |
| Forecast coverage for test | Sparse: 24 `prediction_forecasts`, only RELIANCE/ADANIENT; no clean five-instrument point-in-time series |

### Baseline definitions

| Label | Catalog strategy | Configuration | Interpretation |
|---|---|---|---|
| `day_momentum` | `momentum_breakout` | lookback 5, volume factor 1.1, exit period 3, horizon 1 day | Daily-bar day-trading proxy, not intraday |
| `swing_trend` | `trend_sma` | fast 10, slow 30, horizon 5 days | Swing proxy |
| `long_horizon_proxy` | `trend_sma` | fast 20, slow 60, horizon 20 days | Long-horizon **proxy only**; 88–91 test bars cannot support a true 200-day long-term test |
| Buy-and-hold | backtest benchmark | first available close, no fees/slippage | Reference for capital deployment, not a tradable strategy result |

All strategy baselines used the same test bars, ₹100,000 initial capital, 25% maximum position weight, ₹20,000 order notional and the default `ExecutionConfig`:

- 10% participation.
- 8 bps spread.
- 12 bps impact coefficient.
- 150 bps maximum slippage.
- 3 bps brokerage, 0.297 bps exchange, 0.01 bps SEBI, 0.015 bps stamp, 18% GST.

The RL environment used the same market-impact/fee configuration and next-bar-open fills. The backtest engine and RL environment are separate implementations, so exact fill equality is not claimed; the assumptions are identical and both are isolated from production execution.

### Baselines intentionally not run

- **Multi-timeframe (20m → 5m → 1m):** not comparable. Stored intraday coverage is three instruments, 1-minute only, with no native 5-minute series. The existing MTF report used two sessions and two enhanced trades; its enhanced profit factor was 0.0712 (negative after costs). Running it on daily RL windows would fabricate a comparison that the data cannot support.
- **Forecast-assisted:** not comparable across five instruments. Only 6 RELIANCE and 2 ADANIENT point-in-time forecasts exist in the inspected horizon, and prior forecast calibration evidence was mixed (four accepted 1-session cases, sample-weighted Brier 0.6635 → 0.6564, one regression). A forecast-assisted RL policy would be evaluated on a different, much smaller sample, not the same conditions.

### Uncertainty

Aggregate uncertainty is reported as a 4,000-draw bootstrap of the mean across the five instrument-level test returns (95% percentile interval). This is a small-sample interval, not a claim of statistical significance. RL returns have zero variance in this run, so Sharpe/Sortino are undefined rather than zero.

---

## 7. Baseline comparisons

### Aggregate test results (five instruments, equal weight)

| Policy | Mean return | 95% bootstrap CI | Mean max DD | Mean Sharpe | Trades | Mean turnover | Fees | Slippage |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| RL default | **0.0000** | [0.0000, 0.0000] | n/a | n/a | **0** | 0.000 | ₹0.00 | ₹0.00 |
| RL trading | **0.0000** | [0.0000, 0.0000] | n/a | n/a | **0** | 0.000 | ₹0.00 | ₹0.00 |
| Day momentum | **+0.2543%** | [−0.3960%, +1.1814%] | −0.9386% | 0.0511 | 40 | 1.5990 | ₹312.58 | ₹327.66 |
| Swing trend | **−0.4361%** | [−0.8347%, +0.0071%] | −0.8232% | −0.2442 | 8 | 0.3135 | ₹61.31 | ₹63.94 |
| Long-horizon proxy | **−0.0910%** | [−0.2731%, 0.0000%] | −0.1082% | −1.3728 | 1 | 0.0405 | ₹7.94 | ₹8.21 |
| Buy-and-hold benchmark | **+2.0555%** | — | — | — | — | — | — | — |

Buy-and-hold benchmark range: −7.4672% (HDFCBANK) to +22.8690% (TCS). It is not cost-adjusted and is shown only to quantify missed market exposure.

### Instrument-level test returns

| Instrument | Test bars | Buy-and-hold | RL default | RL trading | Day momentum | Swing trend | Long proxy |
|---|---:|---:|---:|---:|---:|---:|---:|
| HDFCBANK | 88 | −7.4672% | 0.0000% | 0.0000% | −0.3866% | −0.7552% | 0.0000% |
| ITC | 88 | −3.5481% | 0.0000% | 0.0000% | +0.3577% | −0.4847% | 0.0000% |
| RELIANCE | 91 | +1.2538% | 0.0000% | 0.0000% | −0.5285% | +0.3283% | 0.0000% |
| TATASTEEL | 88 | −2.8298% | 0.0000% | 0.0000% | −0.1498% | −1.0747% | 0.0000% |
| TCS | 88 | +22.8690% | 0.0000% | 0.0000% | +1.9787% | −0.1944% | −0.4552% |

### Paired differences on identical test windows

| Difference | Mean | 95% bootstrap CI | Reading |
|---|---:|---|---|
| RL default − day momentum | −0.2543 pp | [−1.1814, +0.3960] pp | RL did worse on average; CI crosses zero |
| RL default − swing trend | +0.4361 pp | [−0.0071, +0.8347] pp | Small cash-policy “win” in a falling sample; not meaningful |
| RL default − long proxy | +0.0910 pp | [0.0000, +0.2731] pp | Tiny; long proxy barely traded |

The RL result is exactly identical for both reward configurations because both converged to no trading. There is no RL distribution to compare, no win rate, no profit factor and no trade-level cost sample. Reporting an RL Sharpe would be misleading.

**Baseline conclusion:** day momentum was the only conventional strategy with positive average return, but its confidence interval includes zero and its costs were material. Swing and long-proxy lost money on average. The market path was mixed: cash happened to beat two of three active baselines and lose to day momentum and buy-and-hold. That is a statement about this five-instrument window, not evidence of RL skill.

---

## 8. Out-of-sample results

### What the policies actually did

| Policy family | Test actions | Trades | Costs | Return | Total reward |
|---|---|---:|---:|---:|---:|
| RL default (5 policies) | 261 × `hold`, 177 × `no_action` | **0** | ₹0 | **0.0%** | **31.8225** |
| RL trading (5 policies) | 438 × `hold` | **0** | ₹0 | **0.0%** | **0.0000** |

No `buy`, `sell` or `wait` action was emitted by any test policy. No policy reached the 25% position cap because no position was opened. No policy paid a fee or slippage. No policy had a drawdown because equity never moved.

### Training behaviour

Training did execute some trades, but the learned policy did not carry them into validation or test:

- Default-reward training episodes ended with small returns (for example +0.59% on RELIANCE at episode 20) and a handful of trades; validation immediately collapsed to `no_action` or `hold`.
- Trading-reward training episodes typically ended with one trade or none; validation collapsed to `hold`.
- Mean training loss was on the order of 0.004, which is a fitting diagnostic only; it is not a trading result.

### Validation and test separation

- Validation score was 0.0 for every selected checkpoint because every validation policy made no trades and had zero drawdown.
- The first validation checkpoint therefore won by tie-break; later checkpoints did not improve validation.
- The test split was scored once per policy with the frozen validation-selected checkpoint. No test result fed back into training, reward selection or action masking.

### Out-of-sample judgment

The minimum requirement for an RL improvement claim is that the policy **trades** and beats the conventional alternatives after identical costs on unseen data. The current policies do not trade. Therefore:

- There is **no repeatable out-of-sample RL improvement**.
- The default reward's positive total is **not** a return claim; it is a reward-specification artifact.
- A 0.0% cash return is not a risk-adjusted victory. It is an uninvested portfolio. Its “max drawdown of zero” is the absence of a strategy, not evidence of risk control.
- The RL result is not statistically distinguishable from zero because it is exactly zero; the baselines' intervals are wide and mostly cross zero. The honest conclusion is **evidence insufficient**, with a firm negative finding on the current artifact's ability to trade.

---

## 9. Market-regime performance

The RL regime slices are descriptive, point-in-time labels from each observation's own market evidence. They are not used for training or selection.

For RELIANCE default (90 scored transitions):

| Regime | Samples | RL mean return | RL mean reward |
|---|---:|---:|---:|
| `high_volatility` | 5 | 0.0% | 0.095 |
| `low_volatility` | 1 | 0.0% | 0.095 (insufficient) |
| `sideways` | 37 | 0.0% | 0.095 |
| `weak_down` | 37 | 0.0% | 0.095 |
| `weak_up` | 10 | 0.0% | 0.095 |

The other four instruments show the same pattern: zero return and zero trades in every sufficient bucket. **There is no regime-dependent RL behaviour to evaluate.**

The active baselines do show regime sensitivity, but with small samples and descriptive labels:

- Day momentum was positive in bull buckets (for example TCS `bull_high_volatility` +2.5982% over 48 observations; RELIANCE `bull_high_volatility` +0.5517% over 6; HDFC `bull_low_volatility` +0.3805% over 13) and negative in bear/sideways buckets.
- Swing trend was positive mainly in bull buckets and negative in bear/sideways buckets; TCS swing was −0.1944% overall despite a bull-heavy test window because its entries were late.
- The long proxy barely traded (one trade), so its regime rows are mostly zero.

These are descriptive slices, not a basis for a regime-timing claim. The sample is five instruments × ~88 days, with buckets as small as one observation.

### Adaptive strategy selection

**No RL strategy selector exists.** The current `RLPolicyIntelligenceAgent` reads eligible policy evidence and the adaptive suitability registry, then emits an advisory action. It does not choose among `trend_sma`, `mean_reversion_rsi`, `momentum_breakout`, `volatility_breakout`, `event_driven` or `forecast_assisted`.

The existing adaptive layer can label regime suitability, but:

- It has no trained policy that trades.
- It has no strategy-return attribution model.
- It cannot modify risk limits, permissions or activation.
- The eligible-policy list in the stored database contains only no-trade policies, so the agent's advisory output would be based on non-trading evidence.

**Conclusion:** RL-based adaptive strategy selection is **not implemented and not supported by this evidence**. A contextual bandit or classifier over strategy returns could be a later experiment, but only after a policy demonstrates non-zero, cost-aware out-of-sample trading and after a pre-registered selection rule exists.

---

## 10. Risk and reward-hacking assessment

### Risk controls preserved

| Control | Evidence |
|---|---|
| Long-only, no shorting | Environment rejects sells without a position and quantities above the position |
| Position cap | 25% of equity; buy rejection at the boundary |
| Gross exposure cap | 100% |
| Order cap | ₹25,000 |
| Cash constraint | Buy notional plus fees must be available |
| No NM-08 bypass | No risk-engine handle in the RL environment |
| No NM-10 order submission | No order gateway/controller path |
| No production book mutation | Private simulated cash, position, ledger and experience store |
| No automatic promotion | `automatic_policy_promotion = false`; no activation endpoint |
| No live trading | `live_trading = false`; no broker connection |

The harness recorded zero invalid actions, zero constraint violations and zero rejected executions for the ten test policies because they made no trades. That is **not** evidence that the action space is safe under an active policy; it is evidence that an inactive policy cannot violate constraints. The earlier probe, which had no tie-break abstention, did produce repeated `sell` tranches and rejected executions, showing that an active untrained policy can generate invalid/repeated orders that rely on environment rejection. The environment correctly refused them; the policy adapter did not prevent them.

### Reward hacking: confirmed

The strongest evidence is the joint observation:

1. Default reward is positive for no-op decisions even when return is zero.
2. Removing the no-op subsidy makes no-op reward exactly zero and trades cost-negative.
3. In both cases, the DQN converges to no trades.
4. The existing registry already contains tested policies with 0 trades and positive reward.

This is a **specification failure**, not a model-capacity failure. More layers, more episodes or PPO would not fix it. A reward that pays for abstention and does not charge for missed opportunity will produce abstention.

### Unrealized gains and deposits

- No deposits are possible in the environment; no deposit reward was observed.
- `profitability` is gross equity return. It includes unrealized mark-to-market moves and therefore can reward holding a winning position before exit. The component reports realized and unrealized deltas separately, but the scalar does not distinguish them.
- If the investment objective is completed-profit reward, the current scalar is too permissive. If the objective is mark-to-market portfolio return, it is defensible, but it must be paired with an opportunity-cost or cash-drag term so that hiding in cash is not free.

### Risk-adjusted interpretation

A 0% return with 0% exposure has undefined Sharpe/Sortino and no meaningful max drawdown. It must not be presented as a low-risk strategy. Its risk is **opportunity risk**: it forgoes the market's positive paths and cannot discover whether its exit logic works.

---

## 11. Resource consumption

Measured on the evaluation host:

| Resource | Observation |
|---|---|
| OS / Python | Windows 11 build 26300; Python 3.13.0 |
| CPU | Intel64 Family 6 Model 170; 18 logical CPUs |
| GPU / NPU | Not used; no CUDA assumption; Intel Arc/NPU not tested |
| Model | 21 → 24 → 5 MLP; **653 parameters** |
| Training time | 22.51–31.68 seconds per policy; 10 policies ≈ 5 minutes |
| Artifact size | 25,462–45,900 bytes per policy JSON |
| Inference | **38.6 µs** per `q_values` call (20,000 calls, single CPU, zero-state benchmark) |
| Memory | Process RSS was not captured; no evidence of memory exhaustion |

The pure-Python DQN is small and CPU-friendly. Training ten policies did not destabilize the host. The bottleneck is not compute; it is the learning signal. A larger network would increase cost without addressing the observed collapse.

**Resource recommendation:** keep the 21→24→5 model for experiments until a non-degenerate policy exists. Do not add GPU/NPU complexity for a 653-parameter model. If state/action expansion is justified later, benchmark a single instrument first and keep checkpoint retention bounded.

---

## 12. Model governance

### Confirmed controls

- `automatic_policy_promotion = false`.
- `activation_available = false`.
- `production_authority = false`.
- No promotion or activation endpoint for experimental policies.
- Tested policies remain `tested`; they are not made active.
- The advisory agent requires an artifact hash/version and validation or test evidence; it can abstain when none exists.
- The generated policy artifacts are tracked JSON under `evaluation/rl_strategy/artifacts/policies/`; they were **not** registered as policies and are not discoverable by the production registry. The matching test-split OHLCV snapshots are tracked under `evaluation/rl_strategy/artifacts/test_bars/`, so the adapter evaluator can reproduce the evidence without the untracked database.

### Lifecycle vocabulary mismatch

The requested governance vocabulary was `EXPERIMENTAL`, `EVALUATING`, `APPROVED`, `PRODUCTION`, `DEPRECATED`, `REJECTED`. The implemented PI-15 `PolicyStatus` enum is:

`created`, `training`, `trained`, `validation_pending`, `validated`, `tested`, `rejected`, `archived`.

There is **no `APPROVED` or `PRODUCTION` status** and no automatic transition to either. This is a real gap between the requested lifecycle language and the implemented registry. It should not be papered over by treating `tested` as approval. `tested` means an out-of-sample report exists, not that the policy is authorized.

### Promotion recommendation

Keep the current no-promotion boundary. Before any future approval:

1. Require a non-zero, cost-aware out-of-sample trading record on multiple folds/instruments.
2. Require independent review of the reward configuration and test protocol.
3. Add an explicit, operator-authorized lifecycle state (for example `approved`/`production`) only if the governance requirement is formally adopted.
4. Keep `automatic_policy_promotion = false`; promotion must be a separate, logged, human action.
5. Preserve rollback/archival and leave the risk engine as sole decision authority.

No policy was promoted or activated in this work.

---

## 13. Known limitations

1. **Small sample.** Five instruments, 88–91 test bars each, one test path. The bootstrap intervals are wide and are not a substitute for a multi-year, multi-regime study.
2. **Single seed and short training.** Twenty episodes per policy and one seed (7) are sufficient to expose collapse, not to estimate achievable DQN performance.
3. **No features or forecasts in the harness.** The encoder has forecast inputs, but the evaluation disabled them to keep a clean five-instrument comparison. That removes any forecast-assisted state advantage and any forecast-related leakage risk.
4. **No multi-timeframe state.** Daily bars only; the 20m/5m/1m hierarchy was not integrated. The existing MTF evidence is two sessions and two trades, so it cannot support a comparison.
5. **No forecast-assisted baseline.** Sparse point-in-time forecast coverage (only RELIANCE/ADANIENT in the inspected horizon) and mixed prior calibration results make a five-instrument comparison invalid.
6. **Long-term baseline is a proxy.** The 20/60 SMA on 88–91 bars is not a long-term strategy test; it is a slower trend proxy.
7. **No walk-forward folds.** The harness used one chronological split. The production manager supports walk-forward, but this evidence does not establish fold stability.
8. **No average exposure metric for baselines.** RL exposure was 0% by construction; baseline capital utilization is represented by turnover, not a full exposure time series. The existing backtest result contract does not expose average gross exposure.
9. **No live session.** The market was closed; all evidence is stored daily history. No live paper-trading claim is made.
10. **No transaction-level intraday evidence.** Daily next-bar-open fills cannot validate intraday timing, queue position, partial fills or market impact at 1-minute resolution.
11. **RL risk ratios undefined.** Zero-variance cash returns make Sharpe/Sortino undefined; no RL drawdown distribution exists.
12. **Memory not measured.** Training wall time, model size and inference latency were measured; peak RSS was not captured.
13. **No adaptive selector.** The adaptive strategy-selection question is unanswered because no RL policy traded and no selector was implemented.

These limitations do not weaken the central negative finding: the current policies made zero trades on unseen data. They do limit any claim about how a *repaired* RL system would perform.

---

## 14. Recommendations

### P0 — do not promote; do not add algorithms yet

- Keep all RL policies experimental and advisory.
- Keep `automatic_policy_promotion = false`, `activation_available = false`, `production_authority = false` and no live path.
- Do not add PPO, Double DQN or actor-critic implementations as a response to this result. The failure is upstream of the optimizer.
- Record the zero-trade outcome as a **rejected-for-promotion** result, not as a neutral experiment.

### P0 — repair the reward before retraining

1. Remove unconditional positive reward for `wait`, `no_action` and `hold` when no forecast/decision evidence supports abstention. An abstention should be neutral or justified by explicit evidence, not a flat bonus.
2. The optional **opportunity-cost / cash-drag** term now exists, but its first five-instrument experiment still produced zero trades. Do not treat its presence as a repair; revisit the action interface and training signal before another promotion claim.
3. Decide whether `profitability` is mark-to-market or realized-only. If completed profits are the objective, do not let unrealized gains dominate the scalar; if MTM is intended, state it and pair it with the opportunity-cost term.
4. Penalize repeated abstention/no-trade streaks, not only repeated trades. The current `DisciplineComponent` is asymmetric.
5. Keep costs charged once and keep risk/rejection penalties non-positive.
6. Freeze reward weights and thresholds before validation; report reward-component attribution alongside return, never instead of return.
7. Pre-register a success criterion: positive net-of-cost return versus buy-and-hold and versus the best active baseline across multiple folds/instruments, with non-zero trades and no risk violation.

### P0 — repair the action interface

1. Add an explicit `exit`/`sell_all` action or size buckets (for example 0%, 20%, 50%, 100%) so the agent can close a position in one decision.
2. Mask invalid actions inside the policy adapter as well as the environment: no buy at the position cap, no sell without a position. Environment rejection is a safety net, not a policy design.
3. Make `hold`, `wait` and `no_action` semantically consistent, or collapse the duplicates. If they remain distinct, their reward treatment must be documented and intentional.
4. Keep the risk engine authoritative; the policy may propose size but must not set caps or bypass NM-08/NM-10.

### P1 — expand state only after the reward/action fix

Add point-in-time, versioned inputs for:

- Position quantity, average entry, unrealized P&L.
- Concentration and remaining position/risk budget.
- Forecast availability/abstention/quality, not just zeros.
- Market regime label or probabilities.
- 20m/5m/1m confirmation when native data exists.
- Recent fill/rejection history and holding period.

Version the encoder and keep old artifacts readable. Do not retrofit a new dimension onto an existing policy without a migration path.

### P1 — strengthen the evidence

- Run at least three seeds and three walk-forward folds per instrument.
- Use a longer history and more instruments; include cross-sectional and sector splits.
- Compare against buy-and-hold, day momentum, swing trend and a genuine long-term baseline on identical costs.
- Report per-instrument and pooled results with confidence intervals, not only averages.
- Keep the test split untouched until the final report; never tune reward or action masks on it.

### P1 — adaptive selection is a separate experiment

Only after a policy trades should a strategy-selection experiment be considered. A contextual bandit/classifier over strategy returns would need:

- A pre-registered action set and reward.
- Regime labels that are available at decision time.
- Exploration safeguards and no authority over risk limits.
- A held-out evaluation against a fixed “always use strategy X” baseline.

Do not describe the existing advisory agent as an adaptive strategy selector; it is not one.

### P2 — governance lifecycle

If the organization formally requires `EXPERIMENTAL`/`EVALUATING`/`APPROVED`/`PRODUCTION`/`DEPRECATED`, add those states explicitly with an operator-authorized transition and audit log. Until then, do not equate `tested` with `approved`. Keep manual promotion and rollback.

---

## Reproduction

From `novamarket/`:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe evaluation/rl_strategy/evaluate_trained_adapters.py
```

This is the no-retraining adapter path. It loads the tracked policy artifacts under `evaluation/rl_strategy/artifacts/`, reads the exact test-split bars from the tracked snapshots under `evaluation/rl_strategy/artifacts/test_bars/`, selects every action with `TrainedPolicy.select` (masked argmax) and re-evaluates the test split. It does **not** open `data/novamarket.sqlite3`; the source database is not required for reproduction. It writes `tmp/rl_evaluation/adapter_evaluation.json`, and it regenerates both `evaluation/rl_strategy/results_compact.json` and `evaluation/rl_strategy/cash_drag_results_compact.json`; both compacts record this command as `reproduction_command` and list the policy artifacts plus the test-bar snapshots in `source_artifacts`. It refuses any `source_artifacts` path that resolves outside `evaluation/rl_strategy/artifacts/`. The console lists every policy's trades, return and action distribution. The recorded run evaluates 15 policies and 13 executed trades, including all five cash-drag policies with one trade each (5 cash-drag trades).

To retrain and rewrite the tracked artifacts from scratch, run:

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe evaluation/rl_strategy/run_rl_evaluation.py
```

That writes `tmp/rl_evaluation/results.json`, `evaluation/rl_strategy/results_compact.json`, `evaluation/rl_strategy/artifacts/policies/*.json`, and the test-bar snapshots under `evaluation/rl_strategy/artifacts/test_bars/*.json`. The cash-drag harness (`evaluation/rl_strategy/run_cash_drag_experiment.py`) additionally writes `evaluation/rl_strategy/cash_drag_results_compact.json` and the cash-drag artifacts, and refreshes the same test-bar snapshots. Alternatively, the adapter evaluator above refreshes both tracked compact extracts from the saved artifacts without retraining or opening the database. Runtime outputs under `tmp/rl_evaluation/` are regenerated and intentionally not tracked; the policy artifacts, test-bar snapshots and compact extracts cited by the adapter evidence are tracked under `evaluation/rl_strategy/`.

The source database is opened with SQLite `mode=ro`. No production table, portfolio, ledger, risk decision, order, autonomy flag or policy status is written.

---

## Final statement

NovaMarket's RL stack is structurally isolated and governance-safe, but the current learning objective is not a trading objective. The tested DQN did not trade, and the positive default reward was generated by abstention subsidies rather than portfolio performance. The correct verdict is **evidence insufficient for an RL improvement**, with a firm rejection of the current policy for promotion. The next cycle should repair reward, action semantics and state representation before adding algorithms or claiming an edge.
