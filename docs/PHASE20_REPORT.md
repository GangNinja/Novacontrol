# Phase 20 Report — Agentic Reinforcement Learning

## Summary

Phase 20 implements the **Agentic RL** infrastructure: learning from complete
multi-step tasks instead of isolated responses. It is built on Phases 15–19 and
on the Phase 8 reliability layer, and it adds **no** second trajectory format, no
second reward engine, no second registry, no second verifier, no second retry
loop and no second risk table.

What the phase ships: `AgentState`, `Episode`, `StateTransition`, `AgentAction`
+ action masking, `AgentPolicy` (+ rule-based, mock, LLM adapter), `PolicyDecision`,
`AgenticRolloutManager` (rollout with verification in the loop and recovery
bounded by Phase 8), `AgenticRewardEngine` (nine dimensions, safety separate) and
`CreditAssigner`, `ExplorationPolicy` + `ExplorationBudget` + `CurriculumManager`
+ `TaskDifficulty`, `AgenticPolicyEvaluator` + shadow + A/B + baselines,
`PromotionGate` + `PolicyRegistry`, `AgenticRLTrainer` + a mock policy optimizer +
checkpoints + resource estimation, `run_agentic_dry_run` (13 stages) and six
deterministic environments, one per curriculum level.

**Optional and inert.** Nothing in `src/novacontrol/agentic/` is imported by the
application, the API, the CLI or the runtime — the only file outside the package
that names it is `tests/test_agentic_rl.py`. `dry_run` is `True` by default,
`mock_agentic_policy` is the only implemented optimizer (`learns: False`), no
model is loaded or downloaded, no CUDA or NVIDIA GPU is required, and no run
starts by itself.

**Deliberately not implemented:** Phases 21–26 (real-time perception, world
model/memory/state reasoning, interactive learning, planning/action-policy
research, embodied/game agents, generalization + ARC + intelligence evaluation),
real RL training, and any environment that touches anything outside its own
memory. `agentic.DEFERRED_PHASES` names all six phases.

## 1. Files Added

### New package: `src/novacontrol/agentic/` (13 modules, ~9,000 lines)

| Module | What it owns |
| --- | --- |
| `models.py` (1,647) | the phase's records: `AgentState`, `AgentAction`, `StepReward`/`MultiObjectiveReward`, `PolicyDecision`/`PolicyFeedback`, `StateTransition`, `Episode`, `CreditAssignmentResult`, `PolicyRecord`, `AgenticEvaluationResult`, `PromotionDecision`, `TaskDifficulty`, and the ten enums |
| `config.py` (764) | `AgenticRLConfig` + its sub-configs, validation, projections onto Phase 16 |
| `actions.py` (443) | `AgentAction` masking: `ActionMask`, `ActionMasker`, `ActionSpace`, effect/declaration projections |
| `environments.py` (933) | `AgenticEnvironment` + `EnvironmentStep` + the six deterministic environments |
| `policies.py` (652) | `AgentPolicy`, `RuleBasedPolicy`, `MockPolicy`, `LLMPolicyAdapter` |
| `rewards.py` (667) | `AgenticRewardEngine`, `CreditAssigner`, the reward-dimension/signal/penalty vocabulary |
| `exploration.py` (410) | `ExplorationPolicy`, `ExplorationBudget`, four seeded strategies |
| `curriculum.py` (423) | `CurriculumManager`, `CurriculumStats`, `CurriculumDecision` |
| `rollout.py` (1,017) | `AgenticRolloutManager`, `RolloutLimits`, `RolloutOutcome` |
| `evaluation.py` (846) | `AgenticPolicyEvaluator`, `ShadowPolicyEvaluator`, `ABPolicyEvaluator`, baselines |
| `promotion.py` (586) | `PromotionGate`, `PolicyRegistry` |
| `trainer.py` (1,217) | `AgenticRLTrainer`, the mock optimizer, checkpoints, `run_agentic_dry_run` |
| `__init__.py` (388) | the package's public surface, `PHASE`, `DEFERRED_PHASES`, `overview()` |

### New test file

- `tests/test_agentic_rl.py` (2,403 lines, 175 tests + 6 subtests) in 18 classes:
  `TrajectoryTests`, `PolicyTests`, `RolloutTests`, `ExplorationTests`,
  `CreditAssignmentTests`, `CurriculumTests`, `RecoveryTests`,
  `VerificationTests`, `EvaluationTests`, `ShadowTests`, `ABTests`,
  `PromotionTests`, `ResourceTests`, `DryRunTests`, `TrainerTests`,
  `SecurityTests`, `NoHiddenReasoningTests`, `OptionalityTests`.

### New documentation

- `docs/AGENTIC_RL.md` — the architecture, the interfaces, the vocabulary, the
  safety boundaries and the deferred phases.
- `docs/PHASE20_REPORT.md` (this file).

## 2. Files Modified

- `src/novacontrol/evaluation/models.py` — Phase 15's `AgentTrajectory` gained
  nineteen OPTIONAL agentic fields (`episode_id`, `step_index`, `state`,
  `observation`, `action`, `action_type`, `action_arguments`, `action_result`,
  `next_state`, `step_reward`, `cumulative_reward`, `verification_result`,
  `critique`, `recovery_event`, `policy_metadata`, `value_estimate`,
  `advantage_estimate`, `step_terminal`, `termination_reason`), wired into
  `to_dict`/`from_dict`, plus `AGENTIC_FIELDS`, `agentic_fields()`, `is_agentic`
  and `with_agentic_step(mapping)` (which ignores unknown keys). Every field is
  optional with a usable default, so a Phase 15 row is byte-identical after the
  change and a round-trip still compares equal.
- `README.md`, `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md` — the phase's row,
  its status section and its development-log entry.

## 3. Trajectory and episode changes

Two directions, one record each way:

* **Up** — `Episode.step_trajectories()` projects each step into Phase 15's
  `AgentTrajectory` with the agentic fields filled (and `status`/`success` set
  from the TERMINAL step, so a bounded run is not recorded as a failure);
  `Episode.to_trajectory()` projects the episode; `Episode.to_rollout()` projects
  it into Phase 18's `Rollout`/`RolloutStep`. A phase-15-only row is untouched
  (`is_agentic` is False and every agentic field stays at its default).
* **Down** — `AgentTrajectory.with_agentic_step(mapping)` lets an existing row
  be extended with one structured step, dropping unknown keys on the floor
  (a key that looks like reasoning never lands).

There is **no field for a model's private reasoning** anywhere in the episode,
the transition, the decision or the trajectory, and no intake path writes one.

## 4. Policy architecture

```
AgentPolicy.observe(state) → select_action(state, actions, mask) → PolicyDecision
        ↑                                                                  ↓
        └──────────────── update(PolicyFeedback) ←────────────────── reward/outcome
```

A `PolicyDecision` carries the selected action, a confidence, the alternatives
that were considered, optional value/expected-value estimates, structured
`exploration_metadata`, the policy version and model id,
`requires_confirmation`, and a structured `reason_code` — never prose reasoning.
`RuleBasedPolicy` is the deterministic reference (five ordered rules);
`MockPolicy` replays a script; `LLMPolicyAdapter` accepts a tiny proposal from any
callable and refuses an action the state does not hypothesize, loading nothing.
`build_policy` resolves a name to an implementation. With no learned policy
enabled the existing DecisionEngine and Planner answer as before.

## 5. Rollout architecture

`AgenticRolloutManager.run_episode()` is the loop: reset → observe → **mask** →
decide → **validate the decision** → execute → **verify** → reward → next state →
terminate, recording one `StateTransition` per step and one `Episode` per run.

Order is the design:

1. candidates → mask (nothing below sees an unmasked action);
2. decision validation (unmasked action, invented action, unapproved action);
3. execution through the Phase 18 `Environment`;
4. verification in the loop (environment verdict + optional `VerificationEngine` +
   optional verifier hook, combined so a `fail` outranks a `pass` and a check
   that did not run never overturns one that did — with every verdict's reason
   preserved);
5. recovery bounded by Phase 8's rules and the plan's retry ceiling;
6. explicit limits (`max_steps`, `max_planning_horizon`, `max_retries`,
   `timeout_seconds`, `resource_budget`, `refusal_tolerance`) and an explicit
   termination reason for every ending.

## 6. Exploration system

Four seeded strategies (`greedy`, `epsilon_greedy`, `temperature`,
`bounded_stochastic`) over **the actions the mask allowed only**. Exploration
never selects an action that requires confirmation, is irreversible or is rated
HIGH/CRITICAL, and a budget stops it on any of six named reasons (action budget,
rate, failures, safety intervention, cost, disabled). `ExplorationBudget.to_dict()`
reports every counter, so a short exploration explains itself.

## 7. Curriculum system

`TaskDifficulty` derives its level from eight dimensions; `CurriculumManager`
advances on criteria, holds between thresholds, and regresses only after a
configurable patience — never below the starting level. The six built-in
environments declare dimensions that DERIVE the level they are registered at
(the verification pass made those two answers agree; see §16).

## 8. Credit assignment

`CreditAssigner` implements five methods (`terminal_propagation`,
`discounted_return`, `step_accumulation`, `advantage`, `verifier_attribution`)
with a configurable discount factor. Discounted returns are accumulated
BACKWARDS (numerically stable over long episodes), stored to the project's
six-decimal convention, and the result is a per-step credit/return map a future
optimizer can consume.

## 9. Multi-objective reward

Nine dimensions (`task_success`, `verification`, `safety`, `efficiency`,
`latency`, `resource_usage`, `tool_correctness`, `planning_efficiency`,
`recovery_quality`) with six signals and nine penalties, all stored per step
beside the total; **safety is its own dimension with its own weight** and is
never averaged into efficiency. Terminal reward covers task success, final
verification and user-confirmed success; the shaped (intermediate) share is
capped and the cap is reported. `to_reward_result()` projects an episode into
Phase 15's `RewardResult`.

## 10. Recovery integration

Phase 8's `RecoveryEngine` is wired (or the module's own bounded decision table is
used when none is injected): the strategy, diagnosis, attempts, whether the
recovery succeeded and whether it was UNNECESSARY are recorded per step (the
module reports `source: "reliability.recovery"` when the engine answered), a
destructive/external action is never retried, a refusal is never retried into, a
missing dependency is not retried, and the retry ceiling is the plan's own.

## 11. Verification integration

Verification is inside the loop (§5 step 4). Phase 8's `VerificationEngine` is an
additional check, and an engine with no strategy for a tool reports `skipped` —
which never overturns the environment's own observation. An engine that RAISES
becomes `inconclusive` and is preserved in the record rather than dropped.

## 12. Shadow policy

`ShadowPolicyEvaluator` runs the production policy and lets the shadow only
PROPOSE: the observer records the shadow's action, the production action,
whether they agree, and the verified outcome and reward of what actually ran
(`shadow_executed_anything: false`). Agreements/disagreements are counted over
comparable steps; a shadow that could not propose is counted as uncompared and a
shadow that raises is reported as unavailable.

## 13. A/B evaluation

`ABPolicyEvaluator` runs A and B over the same task distribution with the same
seeds and identical criteria, and reports per-metric baseline/candidate/delta.
It switches nothing: `same_task_distribution` is recorded and promotion remains a
separate, gate-checked, human-approved decision.

## 14. Promotion gates

Ten checks (`sample_size`, `task_success`, `verification_success`, `safety`,
`latency`, `resources`, `success_regression`, `safety_regression`,
`latency_regression`, `explicit_approval`), every threshold configurable, every
result carrying its measured value, baseline, tolerance and evidence. The two
safety checks are REJECTING. `explicit_approval` requires a NAMED approver and is
checked last. `PolicyRegistry` tracks the six statuses, refuses a duplicate id,
refuses a promotion the gates did not approve, and can roll back.

## 15. Resource governance

`AgenticResourceEstimator` extends Phase 16's estimator with the rollout /
environment / optimizer / episode components and returns SAFE / WARNING /
UNSAFE with reasons against this machine's real memory. An UNSAFE estimate is a
refusal (`allows_training: false`, `override_required: true`).

## 16. The verification pass — defects found and fixed

Driving the phase against its own specification (rather than against its own
assumptions) found nine defects in the phase's code and five wrong expectations
in the tests written for it. All are fixed, and each is pinned by a test.

**Defects in the phase's code:**

1. **`AgentAction.action_id` was a random uuid drawn per instance.** The same
   action therefore had a different id on every call, which (a) made seeded
   exploration non-deterministic when a test or a comparison rebuilt the
   candidate list, (b) made the mask's membership test instance-based instead of
   content-based — a policy that built the very action the mask allowed was
   refused for making a new object — and (c) forced the environment to compare by
   NAME as a workaround. The id is now a content hash of the action's type,
   tool, capability, arguments, effect and declared permission;
   `with_arguments()` re-derives it, because different arguments are a different
   action.
2. **An approved action could be refused, and an action kept because "a person is
   reachable" could run unasked.** The rollout's confirmation check consulted only
   `allow_confirmation`, ignoring the masker's `approved` set, so an explicitly
   approved action was refused as unapproved. In the other direction, the masker
   may KEEP a confirmation-requiring action on the strength of
   `allow_confirmation=True` — and nothing asked about it. Now the mask reports
   `ActionMask.confirmation_required` (the allowed actions kept only because
   somebody can be asked), the rollout asks about every one of them through an
   optional `confirmation` hook, an approval already given is not re-asked, and an
   unanswerable ask or a hook that raises is a SAFETY refusal — never a silent
   execution.
3. **An invented action that declared itself unsafe ended as a plain failure.**
   A policy that proposed an action nobody offered, and declared it needed
   approval or could not be undone, produced `termination_reason: "failure"`. The
   refusal of such an action is now a safety refusal (the episode stops as
   `safety_stop`, with the safety penalty on the step) while an ordinary invented
   action still ends as a failure.
4. **A crashed verification engine left no trace.** `_combine` kept only the
   winning verdict, so a check that RAN and raised disappeared behind the
   environment's own pass. Non-winning verdicts are now preserved with their
   reasons (`other_verdicts`), so absence of evidence is recorded rather than
   overwritten.
5. **The default task set could not meet its own minimum.** `default_tasks()`
   returned six tasks while `EvaluationConfig.minimum_sample_size` defaulted to
   eight, so the default evaluation could never report
   `meets_minimum_sample: true`. The default set is now ten deterministic tasks
   covering every slice more than once (two tool selections, two context clues,
   two planning budgets, two recovery depths), and every one of them is solved by
   the reference policy so the baseline is a real 1.0.
6. **Two answers to "how hard is this task".** `ENVIRONMENT_LEVELS` declared
   `simple` at level 1 and `multi_step` at level 2, while `TaskDifficulty.level`
   derived 3 for both (any task offering two or more tools was called
   tool-selection). The derivation now puts recovery before a long horizon (as
   its own docstring already said), treats tool selection as the level for a task
   whose HARD PART is choosing among tools (few steps, several tools), and accepts
   that a decoy is not a tool the task needs; `SimpleEnvironment` declares the one
   tool that actually completes it. All six environments now derive the level
   they are registered at.
7. **`AgenticRLTrainer.evaluate()` recorded an evaluation against a policy that
   was never registered** (`KeyError: no policy 'agentic-candidate' is
   registered`). `evaluate()` now registers the candidate as `experimental`
   first, so a measurement always lands on a record — still without promoting
   anything.
8. **The shadow comparison could not say "the shadow proposed nothing".** A step
   where the shadow had no action counted neither as agreement nor as
   disagreement, which made the three counters look inconsistent.
   `ShadowComparison.comparable`/`uncompared` now name the distinction and
   `agreement_rate` is computed over comparable steps only.
9. **Eight mypy errors** in the new package (an un-narrowed `float | None`, a
   `str` passed where `tuple[str, ...]` was declared, an un-narrowed optional
   engine, a dict whose key type disagreed with its annotation, and the widened
   `step()` parameter of `AgenticEnvironment`). All fixed; no suppressions were
   added, and both platforms are clean.

**Wrong expectations in the test suite** (fixed in the tests, not by weakening the
assertions — each was replaced by the stronger property the code actually
guarantees):

10. A synthetic episode with `success=None` was stored with
    `termination_reason="failure"`, turning an UNDECIDED run into a failure. The
    helper now records `max_steps` (a bounded run), and the evaluation test pins
    the decided-denominator rule.
11. A 400-step discounted return was compared against the INFINITE-horizon sum
    (`0.1/(1-0.99)`), which is wrong for a finite episode whose tail contributes
    `0.99**400 ≈ 1.8%`. The test now pins the finite closed form, the recurrence
    identity and finiteness/positivity.
12. The step-ceiling test expected `max_steps` for a run that actually stopped
    because its RETRY budget was spent; the reason is now pinned as `failure`
    with the retry detail, and a separate case (a task needing two steps inside a
    one-step ceiling) pins `max_steps` with `success is None`.
13. The A/B test used `MockPolicy(script=["wait", "wait"])` as "the policy that
    fails", but an exhausted script deliberately falls back to the first useful
    action — so the candidate SUCCEEDED. The candidate is now a policy that always
    takes the slow path on a one-step budget, and the delta is pinned negative.
14. Three assertions had the sign or the actor wrong: a safety penalty is
    NEGATIVE (not positive), a read-only no-op's verdict comes from the
    environment with its own wording, and the "denied action" test asserted an
    empty episode for an environment where only ONE of three candidates was
    denied. The last is now the stronger property: the denied action appears in
    neither the recorded transitions nor the environment's own step log.

**A pre-existing defect the Phase 20 gate surfaced (outside this phase's code):**

15. Three `tests/test_config.py` cases went red
    (`ConfigTests.test_mapping_loads_module_settings`,
    `PlanningSettingsTests.test_a_mapping_can_bound_the_plan_loops`,
    `PlanningSettingsTests.test_an_unbounded_loop_is_not_a_configuration`). The
    cause is neither Phase 20 nor the tests: `RLVRSettings` — added to
    `core/config.py` by **Phase 19**, and part of that phase's uncommitted change
    — is a `@dataclass(frozen=True, slots=True)`. `slots=True` makes
    `dataclasses` build a **new** class object, while a method's zero-argument
    `super()` keeps pointing at the pre-slots class, so **both**
    `RLVRSettings.from_mapping` and `RLVRSettings.to_mapping` raised
    `TypeError: super(type, obj): obj (type RLVRSettings) is not an instance or
    subtype of type (RLVRSettings)`. Because `NovaControlConfig.from_mapping`
    reads every section, config *loading itself* raised. Fixed with the explicit
    two-argument form (`super(RLVRSettings, cls)` / `super(RLVRSettings, self)`),
    which resolves the base through the live class. `RLHFSettings` and
    `PreferenceSettings` were unaffected only because they inherit
    `TrainingSettings.from_mapping` instead of overriding it. An AST sweep of the
    whole tree (`class … slots=True` whose body contains a zero-arg `super()`)
    confirms this was the only occurrence.

## 17. Tests and results

```
tests/test_agentic_rl.py   175 passed, 6 subtests passed
tests/test_config.py       6 passed   (the 3 that were red are green again)
mypy src                   Success: no issues found in 333 source files
mypy src --platform win32  Success: no issues found in 333 source files
ruff check (2 paths)       0 non-E501 findings
```

`ruff` is a local convenience, not a CI gate: `pyproject.toml` selects
`E,F,I,UP,B,C4,SIM`, but the repository carries ~956 pre-existing `E501`
(long-line) findings across `src/`, and CI runs only `pytest` and the two `mypy`
views. The new package's only remaining `ruff` findings are `E501`, consistent
with the rest of the tree; the three real findings it started with (`F841`,
`SIM102`, `C416`) are fixed.

Coverage by requirement area (the phase's §37 list): trajectory (episode creation,
state transitions, action recording, step/terminal/cumulative rewards), policy
(interface, deterministic, mock, masking, invalid-action rejection), rollout
(reset, step, observation, verification, reward, termination), exploration (four
strategies, budget, safety limit), credit assignment (cumulative, discounted,
step attribution, terminal propagation), curriculum (levels, progression, failure
handling, regression prevention), recovery (failure, recovery, successful
recovery, bounded retries, no-retry-for-destructive), evaluation (short/long,
success/failure/recovery, safety, efficiency), shadow (proposal, no execution,
comparison), A/B (same distribution, deltas), promotion (thresholds, safety gate,
regression gate, explicit approval), resource (estimation, unsafe refusal, CPU
fallback, no CUDA), dry run (all 13 stages), security (permission enforcement,
masking, destructive blocking, cancellation, approval), plus
`NoHiddenReasoningTests` (no reasoning field anywhere, adapter drops
reasoning-looking keys) and `OptionalityTests` (nothing imports the package; the
application runs unchanged).

## 18. Deferred items

* **Phases 21–26** — real-time perception and abstraction; world model, memory and
  state reasoning; interactive learning and exploration environments; planning,
  reasoning and action-policy research; embodied and game agents; generalization,
  ARC and intelligence evaluation. All are named in `DEFERRED_PHASES` and
  reported by `overview()`.
* **Real optimizers** — PPO, GRPO, actor-critic and policy gradient are named in
  the vocabulary (`PLANNED_AGENTIC_ALGORITHMS`) and refused as not implemented.
  The optimizer boundary is Phase 18's `PolicyOptimizer`; the mock reports
  `learns: False` and every figure it produces is labelled simulated.
* **Real environments** — a visual environment, an interactive simulation, a
  robotics simulator, a game that explicitly permits automation, an ARC-style
  environment and computer-use benchmarks register into `ENVIRONMENT_FACTORIES`
  and inherit the pipeline; none is implemented here.
* **A learned policy in production** — the seat exists (`learns=True`), the gates
  and the registry exist, and nothing is promoted: production policy selection
  remains a person's explicit decision.
* **No HTTP surface.** Phase 20 adds no route, so no `ApiSurface` row,
  route-consumer declaration or `docs/API.md` regeneration is involved; the API
  reference check stays green with the route set unchanged.

## 19. Compatibility

* **Phase 15 rows are unchanged.** Every agentic field on `AgentTrajectory` is
  optional, `to_dict`/`from_dict` round-trip, and `tests/test_evaluation.py`
  (108 tests + 6 subtests) passes unchanged.
* **The application is untouched.** `grep` for `novacontrol.agentic` outside the
  package finds only the new test file; importing `novacontrol.application`,
  starting the API and running a request do not touch this phase.
* **Phase 16–19 boundaries are respected.** The trainer subclasses Phase 16's
  `SFTTrainer`, runs are Phase 16's `TrainingRun`, the registry is Phase 16's
  transition table, the rollouts are Phase 18's records and the critique
  vocabulary is Phase 19's. No Phase 16–19 test needed a change.
* **Phase 8 is reused, not reimplemented.** Permissions, verification, recovery
  and the plan effect/retry rules are the existing layers, asked through their
  own interfaces.
* **No hidden chain-of-thought** enters any record, and **no permission or
  verification bypass exists**: a policy can only choose from the mask, and the
  loop verifies before it rewards.
* **One out-of-phase fix landed.** The `RLVRSettings` `slots=True` / zero-argument
  `super()` fix (§16 item 15) is a two-line repair inside Phase 19's uncommitted
  change to `core/config.py`. It is included because the Phase 20 gate could not
  go green while config loading raised; it changes no behaviour other than making
  two methods that always crashed work. The values it reads and writes are
  unchanged.

### A pre-existing, environment-only Phase 19 test failure (found, not caused, since repaired)

Full-suite validation also surfaced **one** red test that Phase 20 did not cause.
It belonged to Phase 19's test suite and was left untouched by the phase itself;
the later Phases 15–20 verification pass repaired it where it belonged:

```
tests/test_rlvr.py::NoReasoningRegressionTests::test_the_package_imports_no_optional_heavy_dependency
```

That test asserts `psutil` is absent from `sys.modules` — but the same test
module imports `novacontrol.api.app` at line 43 for its HTTP tests, and
`api/app.py` ends with a module-level `app = create_app()`, which runs
`NovaControlApplication.__init__` → `application.py:1344`
(`self.hardware_monitor = HardwareMonitor()`) → `telemetry/hardware.py:323` →
`_import_psutil()` → `import psutil`. **Every one of those files is either at HEAD
or Phase 19's own untracked test file**, and none of the files Phase 20 touched
references psutil at all. So the assertion can only hold on a machine *without*
psutil, which is why CI is green: psutil is not a declared dependency
(`pip install -e ".[dev]"`) and the probe is optional by design. On a developer
machine that happens to have psutil installed (this one does, 7.1.3) the
assertion was unsatisfiable. The check's *intent* — that the RLVR package does not
drag in a heavy optional dependency — is what it now asserts: the test runs
`import novacontrol.rlvr` in a FRESH interpreter (a `subprocess` with only `src`
on `PYTHONPATH`) and requires the child's `sys.modules` to contain none of
`torch`, `transformers` and `psutil`. That is meaningful on every machine — it
passes where the property holds and names the dependency that leaked where it does
not — instead of passing vacuously without psutil and failing spuriously with it.
