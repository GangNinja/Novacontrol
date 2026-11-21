# PI-15 — RL Agent Training and Optimization completion report

**Status:** PASS for the PI-15 software scope defined below.

**Phase:** PI-15 — RL Agent Training and Optimization

**Branch:** `feature/novamarket`

**Date:** 2026-11-16

**Verdict scope:** this report verifies a working, isolated, reproducible training
subsystem inside the actual NovaMarket application. It does **not** claim that a
trained DQN policy is profitable, reliable or suitable for live use. The runs
below are small deterministic software fixtures, not a trading strategy study.

No files were staged, committed, pushed, tagged, merged or published. PI-16 was
not started.

---

## 1. Initial repository assessment

- Working branch: `feature/novamarket`.
- The existing PI-01 through PI-14 completion reports were reviewed and all state
  PASS for their defined scope.
- The working tree already contained the PI-01–PI-14 implementation as unstaged
  and untracked changes; those were preserved. `git diff --cached` was empty at
  the start and end of PI-15.
- No staging, commit, push, tag, merge or release operation was performed.
- PI-13 contracts were not changed to accommodate an algorithm. PI-14 was extended
  additively so PI-15 can request a custom experimental policy evaluation through
  the existing baseline-comparison path.
- PI-16 (performance, hardening and programme acceptance) was not started.

## 2. Existing components reused

| Existing capability | PI-15 reuse |
|---|---|
| PI-02 historical datasets / NM-11 bars | Loaded indirectly through the PI-14 chronological dataset loader; no second dataset engine. |
| PI-04 feature and regime evidence | Consumed through the PI-13 point-in-time observation contract and recorded as `feature_set_version = pi13-observation-v1`. |
| PI-05–PI-10 forecasts and horizons | Consumed through the PI-13 observation's forecast snapshot; PI-15 does not regenerate forecasts. |
| PI-11 evaluation/backtesting contracts | Financial, risk, trade and decision metrics come from the PI-14 evaluator, which uses the established NM-11/NM-08 metric functions. |
| PI-12 decision adapter | Compared as the `pi12_adapter` baseline; it is never replaced or activated by an RL policy. |
| PI-13 RL environment | Sole simulation environment for training, validation and test episodes. |
| PI-14 reward/evaluation engine | Sole reward calculation and policy-comparison path. PI-15 adds `RewardService.evaluate_experimental_policy(...)` for custom trained policies. |
| Existing isolated research SQLite | `novamarket_rl.sqlite3` is extended with `rl_train_*` tables. No new database engine or production store is introduced. |
| Existing API and React conventions | New routes use the existing bearer-auth FastAPI app, `ApiSurface`, route-consumer registry and NovaMarket navigation/components. |

No duplicate production model registry was created. PI-15 policies are research
artifacts under the isolated `rl_training/` directory because the existing
`/training/*` SFT run/model registry has different semantics and belongs to a
separate phase. PI-14 `rl_policy_evaluations` remains the evaluation evidence
store.

## 3. Selected RL algorithm and rationale

**Primary and only training algorithm: DQN (Deep Q-Network).**

- PI-13 exposes exactly five discrete actions: `buy`, `sell`, `hold`, `wait`,
  `no_action`. DQN estimates one Q-value per discrete action.
- The PI-13 observation is a bounded fixed-width numeric projection; a small MLP
  is sufficient.
- Experience replay and a target network fit the existing episode/transition
  contract and provide stable off-policy updates.
- The implementation is pure Python (`pure-python-stdlib`), seeded, and needs no
  torch/numpy base dependency. This keeps installation and CI deterministic.
- PPO and A2C are policy-gradient alternatives that add complexity without a
  continuous action space or a demonstrated benefit here. SAC requires continuous
  actions and is inapplicable. No comparison algorithm was added merely to increase
  algorithm count.
- Known DQN limitations are recorded: value overestimation, sensitivity to reward
  scale, replay-memory use, and no guarantee of profitable behaviour.

The contract endpoint reports the algorithm, action/observation compatibility,
limitations and `comparison_algorithms: []`.

## 4. Training architecture

```text
novamarket/agent_training/
├── models.py       immutable TrainingConfig, TrainingRun, CheckpointRecord,
│                   PolicyArtifact, OptimizationTrial and lifecycle rules
├── agent.py        fixed observation encoder, dense MLP, replay buffer,
│                   DQN update, target network, Adam/SGD, trained-policy adapter
├── datasets.py     PI-14 chronological dataset/train/validation/test preparation
├── store.py        isolated rl_train_* SQLite tables and bounded reads
├── training.py     bounded worker, orchestration, monitoring, checkpoints,
│                   recovery, validation/test, bounded search and comparison
└── __init__.py     public façade
```

Supporting integration:

- `NovaMarketService` lazily creates the training manager and closes it before the
  reward service on shutdown.
- API prefix: `/market/training/*`.
- React workspace: `web/src/pages/RLTraining.tsx`, navigation id `rltraining`.
- Storage: existing `data/novamarket_rl.sqlite3`; checkpoints and policy JSON under
  `data/rl_training/checkpoints/` and `data/rl_training/policies/`.
- Maximum concurrent training workers: **1**. Maximum optimization trials: **20**.
  Checkpoint retention: **20 per run**.

## 5. Dataset and split methodology

`TrainingDataPreparer` builds each split independently through
`PolicyEvaluationEngine.dataset(EvaluationConfig)`:

1. The environment window is the PI-13 `RLEnvironmentConfig`.
2. Train, validation and test must each have distinct starts/ends.
3. `train.end < validation.start` and `validation.end < test.start` are required,
   both at configuration validation and again after data loading.
4. Each split's history is bounded at that split's end, so feature/forecast warm-up
   cannot read future bars.
5. Test data is never passed to the trainer or to hyperparameter selection.
6. Provenance records `dataset_version`, `feature_set_version`,
   `environment_version`, `reward_version`, `reward_config_hash`,
   `environment_config_hash`, split hashes, a combined `data_hash` and warnings.
7. `feature_set_version` is restricted to the PI-13 observation schema
   (`pi13-observation-v1`); an unverified feature-set label is refused rather than
   recorded as if it were true.
8. Reward normalization is not fitted on data; observations use fixed projections,
   so there is no train/test normalization leakage.
9. Walk-forward use is supported as successive immutable configurations with
   rolling chronological split windows; each fold is a separate run/artifact.
   There is no automatic fold scheduler, and no test window from one fold is
   reused for another fold's training or selection.

## 6. Training configuration

`TrainingConfig` is immutable and versioned (`pi15-training-config-v1`). It carries:

- agent name, algorithm, feature-set version;
- full PI-13 environment configuration (dataset, instrument, window, capital,
  execution assumptions, seed);
- full PI-14 reward configuration;
- train/validation/test split windows;
- episodes, steps, episode length, batch size, learning rate, gamma;
- epsilon start/end/decay, hidden layers, activation, optimizer;
- target update interval, replay capacity;
- checkpoint/validation intervals, validation/test transition bounds;
- wall-clock and memory limits, random seed and description.

Unknown fields and invalid values are rejected with explicit errors. `max_episodes`,
`max_steps`, episode length, batch size, replay capacity, network widths, learning
rate, gamma, epsilon ordering, split ordering and resource bounds are all bounded.
A run starts from exactly one configuration hash; changing any field creates a new
configuration identity.

## 7. Training execution evidence

Deterministic fixture used for the evidence below:

- 90 synthetic weekday bars for `XNSE:ALPHA`, point-in-time forecasts;
- train bars 0–49, validation 50–69, test 70–89;
- 4 episodes, 80-step cap, episode length 20, batch 2, hidden layer `[8]`,
  learning rate 0.001, gamma 0.99, target update 5, seed 7;
- 20-step validation and 30-step test bounds; 20-step checkpoint interval.

Actual result (two runs started from the same saved configuration):

| Field | Run 1 | Run 2 |
|---|---|---|
| Status | completed | completed |
| Episodes / steps | 4 / 75 | 4 / 75 |
| Mean training reward | -2.4201404711 | -2.4201404711 |
| Reward history | `[-1.7190, -2.3595, -1.8348, -3.7672]` | identical |
| Validation score | -0.2501850318 | -0.2501850318 |
| Test score | -0.2575951753 | -0.2575951753 |
| Validation total return | 3.30276% | 3.30276% |
| Test total return | 2.84673% | 2.84673% |
| Test max drawdown | 0.0% | 0.0% |
| Test trades / costs | 2 / 35.320044 | 2 / 35.320044 |
| Policy status | tested | tested |
| Training signature | `367060224d19e8af…` | identical |

The positive simulated return alongside a **negative training/validation score** is
intentional evidence that the reward is an optimization signal, not a profitability
claim. The score penalizes decision quality and other components even when a small
long-only sample rises. No conclusion about strategy quality is drawn from it.

## 8. Hyperparameter optimization evidence

- Supported methods: bounded random search and grid search.
- Only the declared search space is accepted; unsupported fields are rejected.
- Trial count is limited to `1..20`; candidates are generated from the saved seed.
- Every trial is persisted with its configuration hash, status, run id and
  validation score.
- Only the validation score is read for selection. The test split is never exposed
  to the optimizer.

Actual bounded grid run over `learning_rate = [0.001, 0.003]` with 2 trials:

- both trials completed;
- both recorded validation score `-0.2501850318`;
- `best_score = -0.2501850318`;
- `test_data_used = false`;
- `production_authority = false`.

A tie on this tiny fixture is an honest result, not evidence that learning rate is
irrelevant. No larger search was run because it would not add software evidence.

## 9. Checkpoint and recovery evidence

Checkpoint kinds observed on the completed run:

```text
final, periodic (3), best_validation
```

All listed records reported `status = complete` and a non-empty integrity hash.
Each checkpoint file contains:

- agent network weights and optimizer moments;
- full configuration;
- progress and metrics;
- episode RNG state;
- environment, reward, dataset and feature-set versions;
- framework (`pure-python-stdlib`) and Python version;
- dataset hash;
- an integrity hash over the complete body.

Verification performed:

- resume from a periodic checkpoint creates a new run and continues from its step;
- tampering with the checkpoint body is rejected by integrity validation;
- a numerical-instability run is marked failed and its policy rejected, then a
  retry succeeds as a new run;
- automatic retention preserves the newest checkpoints while keeping the best
  validation checkpoint;
- on manager startup, a run left non-terminal by a previous process is marked
  `failed` with `stop_reason=interrupted`; its checkpoint remains resumable and
  its incomplete policy is rejected rather than reported as successful;
- there is no automatic activation after resume or recovery.

## 10. Validation results

Final validation runs the trained policy and all three baselines on the identical
validation conditions:

| Policy | Total return | Total reward | Trades |
|---|---:|---:|---:|
| PI-15 trained DQN | 3.30276% | -8.5343626169 | 2 |
| no_action | 0.00000% | -0.444 | 0 |
| rule_based | 1.70074% | 1.8708697912 | 1 |
| pi12_adapter | 1.27554% | 1.8314293261 | 1 |

The trained policy has a higher simulated return but a negative reward score and a
worse decision-quality/discipline profile than the rule-based baseline on this
fixture. The evaluator therefore does not select on return alone. These are
historical simulation observations, not a profitability claim.

Validation output records financial, risk, trading and decision metrics, action
distribution, invalid-action counts, warnings, baseline results, dataset identity
and `conditions_identical`. If final validation or the out-of-sample test raises,
the run is stored as `failed` with `stop_reason=validation_failed` or
`test_failed`; it is never reported as a successful completed run merely because
training episodes finished.

## 11. Out-of-sample results

The separate test split was evaluated only after validation:

| Policy | Total return | Total reward | Trades |
|---|---:|---:|---:|
| PI-15 trained DQN | 2.84673% | -9.242180058 | 2 |
| no_action | 0.00000% | -0.47175 | 0 |
| rule_based | 1.47104% | 1.9408496947 | 1 |
| pi12_adapter | 1.10328% | 1.9070117572 | 1 |

- Test data was not used for training, checkpoints, hyperparameter selection or
  policy selection.
- Test max drawdown was 0.0% and costs were 35.320044 in the fixture.
- The test result is one small sample. It does not establish generalization,
  statistical significance or future performance.
- No policy was promoted, activated or connected to production autonomy.

## 12. Baseline comparisons

The same comparison path is used for validation and test. It includes:

1. `no_action`;
2. `rule_based`;
3. `pi12_adapter`;
4. the trained PI-15 policy.

All four use the same historical window, instrument, dataset hash, environment
configuration, reward configuration, initial virtual capital and execution
assumptions. PI-14 records the comparison; PI-15 does not rewrite it.

## 13. Agent-comparison results

Two policies from identical configurations and seeds were compared through
`compare_policies`:

- `conditions_identical = true`;
- two results returned;
- `production_authority = false`;
- original evaluation records remained intact.

Incompatible policies (different data hash, environment/reward version or reward
configuration hash) are refused rather than compared without qualification.
Policy IDs and artifact hashes are distinct even when training math is identical,
because each artifact embeds its own policy identity and creation metadata. The
training signature and reward history remain identical.

## 14. Reproducibility evidence

For the same configuration and seed:

- `training_signature` matched across both runs;
- episode reward history matched;
- validation score and test score matched;
- validation/test returns, drawdown, trades, costs and action distribution matched;
- no test data influenced training.

Preserved metadata includes configuration hash, seed, algorithm version
(`pi15-dqn-pure-python-1`), failure/contract versions, dataset/feature/environment/
reward versions, data hash, framework (`pure-python-stdlib`), Python 3.13.0,
hardware snapshot, checkpoint hashes and training signature.

This is verified determinism for the same runtime/seed/configuration. It is **not**
a claim of bit-for-bit identity across different Python versions, CPUs or operating
systems.

## 15. Resource and performance results

Environment used for verification:

- Windows 11 (`Windows-11-10.0.26300-SP0`);
- Python 3.13.0;
- 18 logical CPUs;
- CPU-only pure-Python training, no GPU path required.

Controls verified:

- one concurrent training worker (`TrainingResourceError` on a second run);
- wall-clock limit fails the run without claiming success;
- memory limit checked against process RSS on each bounded step;
- replay capacity, episodes, steps, transitions and checkpoint retention bounded;
- optimization trials bounded to 20;
- checkpoint/policy storage counted and exposed;
- worker failure is persisted; retry is an explicit new run.

The focused fixture completes in seconds. No claim is made about throughput on a
large real dataset; that was not measured.

## 16. API and frontend integration

API (28 route methods across 25 paths) under `/market/training/*`:

- status/contracts/health;
- configuration create/list/read/validate;
- run start/list/read/metrics/checkpoints;
- pause/resume/cancel/retry;
- checkpoint read/resume;
- bounded optimize and search read;
- evaluations read;
- policy list/read/validate/test/archive;
- policy comparison.

All routes are in `ApiSurface`, declared as `no-render` consumers, authenticated by
the existing bearer layer and documented in `docs/API.md` (716 routes total,
generator `--check` passes).

React workspace:

- training dashboard and status;
- configuration form with algorithm, dataset, feature-set, instrument, reward,
  chronological splits and hyperparameters;
- save-and-start and start-saved-configuration actions;
- reward/loss curves, action distribution, resource and checkpoint monitor;
- policy table with validation/test scores, returns, drawdown and costs;
- baseline comparison, policy comparison and artifact metadata;
- every policy labelled experimental/advisory; no activation control.

`npm run build` passes. The page was not driven click-by-click in a browser in this
verification; it is type-checked and production-built, not visually reviewed.

## 17. Financial-isolation evidence

- The training package contains no import of `ExecutionEngine`, `ExecutionStore`,
  `PortfolioEngine`, `PortfolioStore`, `RiskEngine` or `AutonomyRuntime`.
- Training writes only `rl_train_*` tables in the isolated research database and
  JSON artifacts under `rl_training/`.
- A direct SQLite inspection of that database found no table name containing
  `production`, `portfolio`, `order`, `ledger`, `paper`, `autonomy`, `wallet` or
  `broker`.
- The API status exposes all isolation flags as `false`, including production
  portfolio/position/ledger/paper-fill mutation, NM-10 order submission, NM-08
  bypass, autonomy/operator changes, promotion, live trading and broker connection.
- There is no API route that submits an order, changes autonomy or activates a
  policy from the training surface.
- `TrainingManager.advisory_proposal(...)` is the controlled PI-12-facing adapter:
  it verifies the persisted artifact hash, reconstructs the agent, and returns one
  advisory action with `advisory_pending_nm08`, `risk_review_required = true`,
  `authorizes_action = false`, `execution_allowed = false`, `orders_possible = false`
  and `activation_available = false`. It has no proposal store, order gateway,
  risk engine or autonomy handle; archived/rejected policies are refused. The
  focused test exercises this adapter directly.
- NM-08 remains the sole risk authority; NM-10 remains the sole paper-execution
  authority. PI-15 policies are advisory research artifacts only.

## 18. Actual tests and results

Commands run from `novamarket` with `PYTHONPATH=src`:

| Check | Result |
|---|---|
| `python -m pytest tests/test_novamarket_pi15.py -q` | **19 passed** |
| `python -m pytest tests/test_novamarket_pi14.py -q` | **45 passed** |
| `python -m pytest tests/test_novamarket_pi13.py tests/test_novamarket_backtesting.py tests/test_novamarket_api.py tests/test_novamarket_pi12.py -q` | **173 passed, 301 subtests passed** |
| `python -m mypy src/novacontrol/novamarket/agent_training` | **Success, 6 files** |
| `ruff check` on `agent_training` and PI-15 tests | **All checks passed** |
| `python scripts/generate_api_reference.py --check` | **in sync** |
| `npm run build` in `web` | **passed** (existing chunk-size/dynamic-import warnings) |
| `git diff --check` | **clean** |

Focused PI-15 coverage includes:

- DQN initialization/action compatibility and deterministic observation encoding;
- configuration boundaries, unknown fields, overlapping splits and feature-set
  rejection;
- complete lifecycle, reward integration, validation, test and checkpoints;
- deterministic repeated runs and agent comparison;
- resume from checkpoint, checkpoint tampering, manual checkpoints and
  interrupted-run startup recovery;
- numerical instability, policy rejection and retry;
- final validation failure is stored as failed, not completed;
- pause/resume/cancel and concurrent-worker enforcement;
- wall/resource limits;
- bounded search and absence of test-set use;
- no financial tables and explicit isolation flags;
- archive does not activate, and the PI-12-facing advisory adapter returns no
  execution authority;
- API status/config/start/metrics/checkpoint/policy/compare surfaces on the real
  FastAPI application.

The API test uses the actual `create_app()` application with an isolated temp data
directory and confirms the training surfaces integrate with the app rather than a
mock.

## 19. Defects found and fixed

1. **Duplicate reward-result identity.** Re-evaluating the same transition with the
   same reward configuration raised `RewardStoreError` because the PI-14 store
   inserted a duplicate `(transition_id, config_hash)`. The store now reuses the
   existing row, making repeated evaluation idempotent.
2. **Leaked test SQLite connection on Windows.** A focused test left a connection
   open, producing `PermissionError` during temp-directory cleanup. The test now
   closes it explicitly.
3. **API route contract not updated.** The existing no-live-route inventory did not
   list the new training paths. The inventory and route-consumer coverage were
   extended; all 286 subtests pass.
4. **Policy comparison/UI read empty financial fields.** Validation/test summaries
   stored metrics under `policy.metrics`, while the UI and comparison reader looked
   at top-level `financial`/`risk`/`trading`. The summary now exposes the metric
   subset both nested and at the top level; tests assert the fields exist.
5. **Feature-set provenance was not first-class.** The configuration now includes
   a validated `feature_set_version` and the dataset provenance uses it instead of
   an optional environment key.
6. **Artifact metadata omitted the runtime.** Policy `creation_metadata` and
   checkpoints now record the framework and Python version alongside the hardware
   snapshot.
7. **Interrupted runs stayed `running` after a restart.** A new training manager
   now marks every non-terminal record from a previous process as `failed` with
   `stop_reason=interrupted`, rejects its incomplete policy, and preserves its
   checkpoints for explicit resume.
8. **Validation/test gaps could be reported as a completed run.** If final
   validation or out-of-sample testing raised, the run was still marked
   `completed`. It is now `failed` with `validation_failed`/`test_failed`, while
   the artifact remains available for explicit follow-up.
9. **Monitoring omitted throughput, latency and repetition signals.** Metrics now
   include elapsed time, estimated remaining time when computable, steps/second,
   average environment-step latency, dominant action/frequency, and repetition /
   invalid-action warnings.
10. **The wall-limit test was timing-flaky.** The test now drives a deterministic
    advancing monotonic clock; the production check still uses real elapsed time.

## 20. Remaining limitations

- PI-13 supports one instrument per environment; PI-15 does not claim
  cross-sectional or multi-instrument generalization.
- The verification dataset is a deterministic fixture with completed daily bars,
  synthetic next-open execution assumptions and no order-book/intraday latency.
- No production historical dataset was used to establish financial performance.
  All return figures are simulation evidence from a small sample.
- DQN is the only training algorithm; no PPO/A2C/SAC comparison was implemented
  because the discrete environment does not justify one.
- The final policy uses the latest trained weights. The best-validation checkpoint
  is preserved but is not automatically substituted for the latest state; the
  selection limitation is reported rather than hidden.
- Determinism is verified for the same Python/runtime/seed; bit-for-bit identity
  across hardware or Python versions is not claimed.
- Resource metrics use optional `psutil`; when unavailable, CPU/memory fields are
  reported as unknown rather than fabricated.
- No GPU or distributed training path is implemented.
- The React page was production-built and type-checked but not manually clicked
  through in a browser during this verification.
- No statistical-significance, automatic walk-forward retraining schedule or
  hyperparameter-stability study was performed on a sufficiently large dataset.
  Walk-forward folds can be submitted manually as successive immutable configs;
  the system does not auto-generate a fold schedule.
- Per-market-regime metric breakdowns are computed per split from each PI-13
  observation's own evidence: PI-04's classifier labels every observation that
  carries a feature snapshot, and the declared market-only fallback runs only
  where no feature snapshot exists. The fallback reports PI-04's own vocabulary
  (`weak_up`/`weak_down`, since market evidence cannot confirm a strong trend),
  so the slices share one regime vocabulary and one classifier threshold set.
  Regime rows are evidence only; they never train, tune or select a policy.
- The full repository test suite was not run; verification was limited to the
  focused PI-15 suite and the affected PI-12/PI-13/PI-14/API/backtesting
  regressions listed above. The React page was built, not manually clicked
  through in a browser.

## 21. Final acceptance verdict

PI-15 is **PASS for its software scope**: the isolated DQN training subsystem,
chronological data preparation, lifecycle controls, bounded search, checkpoint
recovery, PI-14 validation/test integration, baseline and policy comparison,
artifact versioning, API integration, React workspace and financial-isolation
boundaries are implemented and verified in the actual NovaMarket application.

This verdict does **not** say that a trained agent is profitable or reliable.
The focused evidence shows successful software verification, including negative
optimization reward alongside positive simulated return, which is exactly why
training reward must not be presented as financial performance. Trained policies
remain experimental and advisory. PI-16 was not started.
