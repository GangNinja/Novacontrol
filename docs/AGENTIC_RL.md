# Agentic Reinforcement Learning (Phase 20)

Phase 20 adds **Agentic RL**: the infrastructure for learning from complete
multi-step tasks — a goal, a sequence of states, observations, decisions,
actions, outcomes, verifications, rewards and state transitions — rather than
from isolated responses. It is built directly on what Phases 15–19 already own:
Phase 15's trajectories, evaluations and reward vocabulary, Phase 16's trainer
interface, run record, checkpoints, resource estimate and registry, Phase 18's
`Environment`/`Rollout`/`PolicyOptimizer` records, Phase 19's critique
vocabulary, Phase 8's verification, recovery and permission layers, the event
bus and the audit trail. It adds no second store, no second trajectory format,
no second registry, no second verifier, no second retry loop and no second risk
table.

> **Phase 20 is optional and experimental, and it ships one optimizer:
> `mock_agentic_policy`.** All six environments are in-memory, deterministic and
> read-only; every figure a simulated run produces is labelled simulated.
> Normal NovaControl operation works with this package absent: **nothing in
> `src/novacontrol/agentic/` is imported by the application, the API, the CLI or
> the runtime** — the only file outside the package that names it is
> `tests/test_agentic_rl.py`. No model is loaded or downloaded, no CUDA or
> NVIDIA GPU is required or assumed, `dry_run` is `True` by default, and **no run
> starts by itself**.
>
> **Not implemented, on purpose:** Phase 21 (real-time perception +
> abstraction), Phase 22 (world model + memory + state reasoning), Phase 23
> (interactive learning + exploration environments), Phase 24 (planning +
> reasoning + action policy), Phase 25 (embodied / game agents) and Phase 26
> (generalization + ARC + intelligence evaluation). There is no game vision, no
> game control, no embodied agent and no ARC-specific system here. Those phases
> consume the generic infrastructure this one provides. `agentic.DEFERRED_PHASES`
> names all six, and `agentic.overview()` reports them alongside
> `automatic_training: false`, `automatic_model_loading: false`,
> `stores_hidden_reasoning: false`, `cuda_required: false`.

## The shape of it

Phase 15 records what happened, Phase 19 makes a reward something the machine can
re-check, and Phase 20 asks the question those two make answerable: **"over a
whole task, which decision was worth making — and would the same decision be
worth making again?"**

```
User goal → State → Observe → Decide → Plan → Action → Tool / Environment
          → Observation → Verification → Reward → Next state → … → Task ending
          → Trajectory reward → Credit assignment → Policy learning
```

Everything below is one arrow of that picture, and every arrow reuses something
that already exists:

| Arrow | What Phase 20 adds | What it reuses |
| --- | --- | --- |
| State | `AgentState` | the task state machine's state, the Phase 11 context, the tool/capability registry, the permission layer |
| Episode | `Episode`, `StateTransition` | Phase 15's `AgentTrajectory`, Phase 18's `Rollout`/`RolloutStep` |
| Action | `AgentAction`, `ActionMask` | the tool schema, `PermissionManager`, the plan's `StepEffect`/`PlanStep` |
| Policy | `AgentPolicy`, `PolicyDecision` | Phase 18's `Policy`/`PolicyOptimizer` interfaces |
| Rollout | `AgenticRolloutManager` | Phase 18's `Environment`/`RolloutRunner`, Phase 8's `VerificationEngine`/`RecoveryEngine` |
| Reward | `AgenticRewardEngine`, `CreditAssigner` | Phase 15's `RewardComponent`/`RewardPenalty`/`RewardResult` |
| Exploration | `ExplorationPolicy`, `ExplorationBudget` | the mask (nothing is explored into) and the risk levels |
| Curriculum | `CurriculumManager`, `TaskDifficulty` | the environments' own declared difficulty |
| Evaluation | `AgenticPolicyEvaluator`, `ShadowPolicyEvaluator`, `ABPolicyEvaluator` | Phase 15's evaluation records and the Phase 16/18/19 baselines |
| Promotion | `PromotionGate`, `PolicyRegistry` | Phase 16's registry transitions and rollback |
| Trainer | `AgenticRLTrainer`, `run_agentic_dry_run` | Phase 16's `SFTTrainer`/`TrainingRun`/checkpoints/`ResourceEstimator` |

## 20.1 The episode, and what it is made of

An **`Episode`** is one task attempt: `episode_id`, `task_id`, `environment_id`,
`start_time`/`end_time`, `initial_state`, `goal`, `steps`, `total_reward`,
`success`, `termination_reason`, `verification_summary`, `resource_usage`,
`policy_version`, `model_id` — plus the difficulty, the per-dimension totals, the
credits and returns once they are assigned, and the reward breakdown.

A **`StateTransition`** is one arrow: `previous_state`, `action`, `observation`,
`result`, `next_state`, `verification`, `reward`, `step_reward`,
`cumulative_reward`, `recovery_event`, `policy_metadata`, `error`, `executed`,
`terminal`, `termination_reason`, `timestamp`.

A **`AgentState`** is the structured state the policy actually reads: the current
goal, the task state, relevant context, available capabilities, available tools,
observations, previous actions, verification results, active errors, the resource
state, the permissions in force and the environment's own state. It is a
**projection**, never a second source of truth: it is built from the task state
machine, the context the application already assembles and the registries that
already answer "what can this machine do". `AgentState.advanced(...)` returns the
next state (states are immutable) and `fingerprint()` gives a stable identity for
repeat detection.

Two rules shape every record, and both are pinned by tests:

* **No hidden chain-of-thought, ever.** There is no field for a model's private
  reasoning and nowhere to put one. A `PolicyDecision` carries the action it
  chose, a structured `reason_code` (`highest_verified_value`, `required_tool`,
  `recovery`, `exploration`, `safety_constraint`, `user_goal`,
  `planner_decision`), the alternatives it considered and their value estimates.
  A reader can always reconstruct what happened and what it was based on.
  `LLMPolicyAdapter` accepts a deliberately tiny proposal (an action name, a
  confidence, a reason code) and **drops every other key**, including anything
  that looks like reasoning.
* **A figure that was not measured is `None`.** `Episode.success` is
  `bool | None` because `None` means nobody decided whether it worked — a run
  that hit its step ceiling is neither a success nor a failure, and every metric
  that counts failures divides by DECIDED episodes only. A `value_estimate` or
  `advantage_estimate` no critic produced stays `None` rather than becoming a
  zero that reads as a prediction.

**Termination** is explicit and always recorded, from one vocabulary of seven:
`success`, `failure`, `max_steps`, `cancelled`, `timeout`, `safety_stop`,
`environment_error` (plus a `termination_detail` sentence saying which bound was
reached). Every episode ends for one of them — there is no path that returns
without a reason.

## 20.2 The action space, and the mask that keeps a policy inside it

An **`AgentAction`** may represent a tool invocation, a planner step, a recovery
action, information gathering, communication, local computation, an environment
interaction or a noop. It carries `action_id`, `action_type`, `capability`,
`tool`, `arguments`, `risk_level`, `expected_effect`, `reversible`,
`requires_confirmation` and a SHORT human `label` — this is not a second tool
schema: `tool`/`capability` name entries the existing registry and permission
manager already know.

`action_id` is **derived from what the action is** (a content hash of its type,
tool, capability, arguments, effect and declared permission), never drawn at
random. Two actions that mean the same thing are the same action, whether they
came from an environment's candidate list or were built by a policy — which is
what lets a mask say "this exact action is allowed" and a shadow comparison say
"the two policies chose the same thing". `with_arguments(...)` derives a new id,
because different arguments are a different action.

**`ActionMasker`** builds the mask from four questions asked of every candidate:

| Reason | Meaning |
| --- | --- |
| `unavailable` | the state says this installation does not have the tool or capability |
| `unauthorized` | the permission layer refuses it as it stands |
| `unsafe` | it is irreversible (or worse) and nothing approved it |
| `incompatible` | the environment will not accept it in this state |
| `confirmation_required` | it needs a person's approval and this run has none |

The mask is the phase's safety boundary made concrete. It is built from the
EXISTING `PermissionManager`, so an action the rest of NovaControl would refuse
cannot sneak through a policy. `allow_confirmation` never means "ignore the
permission layer": it means a person is reachable, so the mask may KEEP an action
that requires confirmation — and it then names that action in
`ActionMask.confirmation_required`, which the rollout asks about before it runs.
An unanswerable ask is a refusal, never a silent execution.

**The rollout validates every decision, and trusts none.** A decision is checked
before it is executed: an action that is not in the mask is refused; an action
the policy INVENTED is refused, and if that invented action declares that it
needs approval, cannot be undone or is rated HIGH or worse, the refusal is a
SAFETY refusal rather than a naming mistake. A refusal is recorded as a
transition (`executed: false`, the reason, a safety penalty when it is a safety
matter) and the episode is stopped as a `safety_stop` once the refusal tolerance
is spent — never looped on a question nobody can answer.

## 20.3 Policy abstraction

**`AgentPolicy`** is `observe(state)` / `select_action(state, actions, mask=...)`
/ `update(feedback)` / `reset()` / `describe()`. Four implementations ship:

| Policy | What it is |
| --- | --- |
| `RuleBasedPolicy` | the deterministic reference policy: five ordered rules (context clue → goal-named tool → recovery → gather information → first useful action) |
| `MockPolicy` | replays a script deterministically, and says so in `describe()` |
| `LLMPolicyAdapter` | turns a tiny structured proposal from any callable into a decision; it refuses an action the state does not hypothesize, and **loads nothing** |
| a future learned policy | the `learns=True` seat, whose optimizer boundary is Phase 18's `PolicyOptimizer` |

The learned policy is OPTIONAL: with no learned policy enabled, the existing
DecisionEngine and Planner answer exactly as before. Nothing in this package
installs itself as the application's decision maker.

## 20.4 Rollout

**`AgenticRolloutManager`** runs one episode: reset → observe → mask → decide →
execute → verify → reward → next state → repeat → terminate, and appends one
`StateTransition` per step. Its order is the safety design:

1. **Candidates, then the mask.** Nothing below the mask ever sees an unmasked
   action.
2. **The decision is validated** (see above) — refused decisions are recorded,
   not executed.
3. **The authorized action runs** through the environment (Phase 18's
   `Environment`, extended with a goal, available actions, acceptance,
   verification, difficulty and resource state).
4. **Verification is inside the loop.** An action that changes something is
   verified by the environment's own observation; a wired
   `VerificationEngine` is an ADDITIONAL check; an optional `verifier` hook is a
   third. Their verdicts are combined so the most cautious MEANINGFUL verdict
   wins: a `fail` beats a `pass`, and a check that did not run (`skipped`) or
   could not tell (`inconclusive`) never overturns one that did — but every
   verdict keeps its reason in the record, so a crashed check is preserved
   rather than overwritten. An engine that RAISES becomes `inconclusive`, never a
   pass.
5. **Recovery is bounded by Phase 8's rules.** A destructive or external action
   is never retried, a refusal is never retried into, a missing dependency is not
   retried, and the retry budget is the plan's own ceiling.
6. **Every bound is explicit** (`RolloutLimits`): `max_steps`,
   `max_planning_horizon`, `max_retries`, `timeout_seconds`, `resource_budget`,
   `refusal_tolerance`. Hitting a bound is a recorded reason, and the step
   ceiling cannot be exceeded by retries.

## 20.5 Reward: nine dimensions, kept apart

**`AgenticRewardEngine`** scores a step from one `StepContext` and produces a
`StepReward` whose `total` is a weighted sum and whose `dimensions` keep the
components SEPARATE, because collapsing everything into one opaque number is
how a safety regression hides inside an efficiency gain:

* dimensions: `task_success`, `verification`, `safety`, `efficiency`,
  `latency`, `resource_usage`, `tool_correctness`, `planning_efficiency`,
  `recovery_quality`;
* signals: `correct_tool_selected`, `valid_action`, `successful_subtask`,
  `successful_verification`, `useful_information_gained`, `efficient_action`;
* penalties: `unnecessary_action`, `failed_action`, `incorrect_tool`,
  `invalid_arguments`, `repeated_failure`, `excessive_latency`,
  `excessive_resource_usage`, `unsafe_action`, `unnecessary_recovery`.

**Safety is its own dimension with its own weight**, never averaged into
efficiency: a step that is efficient and unsafe is recorded as exactly that.
The terminal reward covers task success, final verification success and a
user-confirmed success; the shaped (intermediate) reward is capped at a
configurable share of the terminal reward and the cap is REPORTED
(`shaped_share`), so a policy cannot farm step rewards into a success.

`episode_reward(episode)` sums the dimensions and reports the measured total,
the capped total, the terminal and intermediate parts and the cap itself.
`to_reward_result(...)` projects an episode into Phase 15's `RewardResult`, so an
agentic run lands in the existing reward records instead of beside them.

## 20.6 Credit assignment

**`CreditAssigner`** estimates which actions contributed to the final result, in
five configurable methods (`CreditMethod`):

| Method | What it answers |
| --- | --- |
| `terminal_propagation` | the outcome shared across the steps that led to it |
| `discounted_return` | `G_t = r_t + γ r_{t+1} + γ² r_{t+2} + …`, computed BACKWARDS for numerical stability |
| `step_accumulation` | the running sum of what each step earned |
| `advantage` | the return minus a baseline (the episode mean by default) |
| `verifier_attribution` | what verification observed about each step, side by side |

The discount factor is configurable, stored with the run, and the arithmetic is
stable over long episodes (400-step returns keep the recurrence identity to the
stored precision). The result is a `CreditAssignmentResult` of per-step credits
and returns — a clean abstraction a future optimizer can consume, not a
mathematically heavy algorithm pretending to be PPO.

## 20.7 Exploration, inside a budget and a mask

**`ExplorationPolicy`** implements four seeded strategies — `greedy`,
`epsilon_greedy`, `temperature` (a numerically stable softmax) and
`bounded_stochastic` — and **`ExplorationBudget`** stops it:

* exploration chooses only from the actions the MASK allowed, so an unsafe or
  unapproved action is not reachable by "trying something new";
* it never selects an action that requires confirmation, is irreversible or is
  rated HIGH or CRITICAL, even when a person is available to approve it —
  asking somebody to approve a randomly chosen action wastes their attention and
  teaches a policy to spam approvals;
* it stops on any of six named reasons: the action budget, the exploration rate,
  too many failed explorations, a safety intervention, the cost budget, or being
  switched off. A stopped exploration leaves the episode running greedily.

Every strategy is seeded, so the same run explores the same way twice and a test
can assert exactly which action exploration picked.

## 20.8 Curriculum: six levels, earned one at a time

**`TaskDifficulty`** describes a task on eight dimensions (steps, tools,
branching factor, ambiguity, verification complexity, recovery requirement,
resource requirement, planning horizon) and **derives** its level from them —
two places that both claim to know how hard a task is will eventually disagree,
and the derived one is the one that can be checked. The six levels are the ones
the specification names:

| Level | Task |
| --- | --- |
| 1 | simple deterministic tasks |
| 2 | multi-step tasks |
| 3 | tasks with tool selection |
| 4 | tasks with failures and recovery |
| 5 | long-horizon tasks |
| 6 | ambiguous or context-dependent tasks |

**`CurriculumManager`** advances only when configurable criteria are met
(`min_episodes_per_level`, `min_success_rate`, `max_failure_rate`), holds when a
level is between the success and failure thresholds, and regresses only after a
configurable patience — never below the starting level, because a level that is
failing at the start has nothing simpler to fall back to and the honest answer is
to stop rather than pretend a harder task is progress. Failure handling and
regression prevention are therefore two named decisions with their evidence
attached, not a side effect of a number going down.

## 20.9 Verification, recovery, and long-horizon bounds

* **Verification stays in the loop** (§20.4 step 4): a policy cannot assume an
  action succeeded where verification is available. A step recorded as
  `skipped` is never a pass.
* **Recovery is learned from, not bolted on**: a successful recovery, a failed
  recovery, an unnecessary recovery and a repeated failure are separate signals
  with separate penalties, and recovery stays bounded by the retry limits, the
  permission layer and the effect rules that Phase 8 already enforces.
* **Long-horizon tasks are bounded** by the episode step ceiling, the planning
  horizon, the retry ceiling, a wall-clock timeout and a resource budget — every
  episode has explicit termination conditions, and the reason it ended is stored.

## 20.10 Policy evaluation

**`AgenticPolicyEvaluator`** runs a policy over a task set and reports what it
measured, never a single number: task success rate, verification pass rate, mean
episode reward, mean terminal reward, mean steps, steps per SUCCESS, recovery
success rate, failure rate, undecided rate, safety rate, mean unnecessary
actions, mean latency and mean resource use — with the **safety block and the
efficiency block kept separate**, and unmeasurable figures `None` with a reason.

Reports are sliced by horizon (`short` / `long`), by outcome
(`no_failure` / `failure_or_recovery`) and by derived difficulty, so "it got
better on easy tasks" is visible as exactly that. The default task set
(`default_tasks()`, ten deterministic tasks over all six environments) carries
at least the default minimum sample size, so the default configuration can
produce a report that meets its own minimum.

**A higher average reward is not evidence.** `reward_improvement_is_not_evidence`
states the rule: an increase has to come with task success, verification and
safety that did not go backwards before anybody treats it as improvement.

**Baselines** (`baseline_table`) name the six comparison points the
specification lists — the deterministic orchestration, SFT, DPO/ORPO,
RLHF/RLAIF, RLVR and the agentic candidate. Two of them (the deterministic
orchestration and this phase's own candidate) RUN here; the four model-backed
ones are reported as unavailable WITH the reason (no model is loaded), which is
the honest answer rather than a fabricated comparison.

## 20.11 Shadow policies and A/B evaluation

In **shadow mode** (`ShadowPolicyEvaluator`) the production policy executes and
the shadow only PROPOSES. Every step records what the shadow proposed, what the
production policy chose, whether they agree, and the verified outcome and reward
of what actually ran — `shadow_executed_anything: false`, always. A shadow that
cannot propose (or raises) is reported as unavailable, never hidden. Agreements
and disagreements are counted over the steps where a comparison was possible;
a proposal the shadow could not make is counted as UNCOMPARED, never silently as
a disagreement.

**`ABPolicyEvaluator`** runs policy A and policy B over the SAME task
distribution with the SAME seeds and identical criteria, and reports per-metric
baselines, candidates and deltas. It switches nothing: promoting a policy is a
separate decision that runs through the promotion gates and a person's approval,
and one experiment never changes production.

## 20.12 Promotion gates and the policy registry

**`PromotionGate`** evaluates ten checks —
`sample_size`, `task_success`, `verification_success`, `safety`, `latency`,
`resources`, `success_regression`, `safety_regression`, `latency_regression`
and `explicit_approval` — each against a configurable threshold, each returning
its measured value, its baseline, its tolerance and its evidence. The two safety
checks are REJECTING: a safety failure is not "not approved yet", it is rejected.
`explicit_approval` is last (so the report shows what was being approved) and is
never satisfied by a number — a promotion needs a NAMED approver.

**`PolicyRegistry`** tracks learned policies with `policy_id`, `model_id`,
`policy_version`, `training_run_id`, `environment`, `curriculum_version`,
`reward_version`, `verifier_versions`, evaluation results and a status from
`experimental → evaluating → approved → production → deprecated` (or `rejected`).
A fresh record is `experimental`; `promote` refuses a policy the gates did not
approve; `rollback` returns a policy to paper and says who decided. Nothing is
promoted automatically, and a duplicate policy id is refused rather than
overwritten.

## 20.13 Resource governance

`AgenticResourceEstimator` extends Phase 16's estimator with the components an
agentic run adds — rollout memory, environment memory, optimizer memory, the
episode/dataset budget — and returns **SAFE / WARNING / UNSAFE** with its
reasons against this machine's real memory reading, plus CPU/GPU/NPU
availability and `cuda_required: false`. An UNSAFE estimate is a REFUSAL
(`allows_training: false`, `override_required: true`): **unsafe training does not
start**, it is reported.

## 20.14 Dry run

`run_agentic_dry_run(config, ...)` walks the whole pipeline without training
anything, through thirteen stages:

`environment` → `state` → `policy` → `action` → `execution` → `verification` →
`reward` → `credit_assignment` → `state_transition` → `episode_termination` →
`evaluation` → `checkpoint` → `registration`

Each stage reports what it did and what it measured, with `trained: false` and
`model_loaded: false` stated outright. The checkpoint payload carries the policy
version, model version, training/reward/curriculum configuration, environment
version, optimizer metadata and the curriculum state, with an integrity digest
and a rollback target. Registration lands the candidate as `evaluating` — never
promoted — and the dry run is what makes "the machinery connects" a measurement
rather than a claim.

## 20.15 Test environments

Six deterministic, in-memory, read-only environments, one per curriculum level:

| Environment | Level | What it tests |
| --- | --- | --- |
| `simple` | 1 | one action finishes it; the other candidates are decoys |
| `multi_step` | 2 | dependent steps (read needs open, write needs read) |
| `tool_selection` | 3 | several plausible tools, one correct |
| `recovery` | 4 | a tool fails a deterministic number of times, then succeeds |
| `planning` | 5 | reach the goal inside an efficient step budget |
| `contextual` | 6 | the right action depends on a clue in the context |

`ENVIRONMENT_FACTORIES` is the registry a future phase extends (a visual
environment, an interactive simulation, a robotics simulator, a game that
explicitly permits automation, an ARC-style environment or a computer-use
benchmark registers here and inherits the whole pipeline) — but Phase 20 ships
none of those.

## 20.16 Safety boundaries

* **Nothing destructive is reachable.** The default environments are mock,
  simulator, sandboxed and deterministic; the mask refuses what the permission
  layer refuses, and exploration cannot reach past the mask.
* **An external side effect needs explicit authorization.** An action that
  requires confirmation runs only when a NAMED approval exists (the masker's
  `approved`) or a confirmation hook says yes at the moment of the decision; an
  unanswerable ask is refused.
* **A refusal is never retried into**, a destructive action is never retried
  automatically, and a refusal that the SAFETY rules produced ends the episode as
  a safety stop rather than a quiet failure.
* **No unrestricted autonomous real-world behaviour is implemented.** There is no
  computer control here, and no environment that touches anything outside its own
  memory.
* **A learned policy cannot bypass permissions or verification**: it can only
  choose from the mask, and the loop verifies before it rewards.

## 20.17 Checkpoints, resume and the trainer

**`AgenticRLTrainer`** extends Phase 16's `SFTTrainer`, so a run IS a
`TrainingRun` with the same lifecycle, checkpoints, retention, integrity checks,
resume/cancel/finalize and registry. It prepares trajectories, creates episodes,
constructs the state/action transitions, computes rewards, assigns credit,
collects rollouts, invokes the policy optimizer (the mock one, by default),
evaluates, checkpoints, and registers a candidate — and it refuses a real run
whose optional training dependencies are absent (`TrainingBackendUnavailable`)
rather than pretending. `evaluate()` registers the candidate as `experimental`
if it is not registered yet, so an evaluation always lands on a record.

## 20.18 Configuration

`AgenticRLConfig` is the one validated place an agentic run's parameters live:
episode bounds and ceilings, the discount factor, the credit method, the reward
weights (safety separate), the exploration strategy and its budget, the
curriculum criteria and its version, the promotion thresholds, the algorithm, the
environment list, the policy id, the reward/curriculum/verifier versions and
`dry_run`. Out-of-range is an error, not a clamp; `ppo`/`grpo`/`actor_critic`/
`policy_gradient` are named as PLANNED and refused as not implemented;
`dry_run=False` is a WARNING that says what it would need; the whole
configuration round-trips through `to_mapping`/`from_mapping` with a stable
`fingerprint()`, and `as_training_config()` projects onto Phase 16 so the run
record, resource estimate and checkpoints are the existing ones.

## 20.19 The vocabulary, in one place

```
Phase        agentic (PHASE = "phase20"), versions phase20.1 / schema 1
Actions      tool_invocation, planner_step, recovery_action, information_gathering,
             communication, local_computation, environment_interaction, noop
Endings      success, failure, max_steps, cancelled, timeout, safety_stop, environment_error
Reason codes highest_verified_value, required_tool, recovery, exploration,
             safety_constraint, user_goal, planner_decision
Statuses     experimental, evaluating, approved, production, deprecated, rejected
Credit       terminal_propagation, discounted_return, step_accumulation,
             advantage, verifier_attribution
Exploration  greedy, epsilon_greedy, temperature, bounded_stochastic
Rewards      task_success, verification, safety, efficiency, latency, resource_usage,
             tool_correctness, planning_efficiency, recovery_quality
Policies     rule_based, mock, llm_adapter, scripted
Environments simple, multi_step, tool_selection, recovery, planning, contextual
Deferred     Phases 21–26
```

## 20.20 What this phase is not

* **Not a real optimizer.** `mock_agentic_policy` is the only implemented
  algorithm; it reports `learns: False`. PPO, GRPO, actor-critic and policy
  gradient are named and refused.
* **Not a model loader.** Nothing loads, downloads or requires a model, and
  `overview()` states it.
* **Not a second execution path.** The package is imported by nothing in the
  application; the existing DecisionEngine and Planner answer as before.
* **Not Phases 21–26.** No real-time perception, no world model, no interactive
  exploration framework, no embodied or game agent, no ARC evaluation.
