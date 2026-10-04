# Reinforcement Learning from Human and AI Feedback (Phase 18)

Phase 18 turns the feedback NovaControl already receives into a reward, the
reward into a versioned dataset, and the dataset into a policy update that only a
measured comparison can approve. It is built directly on Phase 15's trajectories,
evaluations and reward engine, Phase 16's datasets, runs, checkpoints, resource
verdict, evaluation gate and model registry, and Phase 17's deterministic splits
and dry-run discipline. It adds no second store, no second registry, no second
event bus and no second trajectory format.

> **Phase 18 is an optional, experimental subsystem, and it ships one
> optimizer: `mock_policy`.** `ppo` and `grpo` are names in the vocabulary that
> are refused with "not implemented" — the interface exists so a later phase can
> fill it, not so a mistyped run can start something. Normal NovaControl
> operation works with every RL dependency absent: nothing in `rlhf/` imports
> `torch`, `transformers` or `peft` at module level, nothing downloads a model,
> and no CUDA or NVIDIA GPU is required or assumed. `dry_run` is true by
> default, and **no run starts by itself**.
>
> **Not implemented, on purpose:** RLVR, critique-based learning, RLCD-style
> training, agentic RL and game agents. This phase is RLHF and RLAIF only.

## The shape of it

Phase 15 records what happened, Phase 16 supervises it, Phase 17 prefers one
observed outcome over another, and Phase 18 asks a harder question: *what is a
run worth, who says so, and can that reward be trusted before anything learns
from it?* Four ideas carry the phase.

* **A reward is a claim with provenance.** It says which source produced it
  (a person, an evaluator, a rule, a verifier, or several composed), how
  confident it is, which policy and version produced the number, which
  components and penalties it was built from, and what evidence supports it.
  A `RewardResult` never arrives as a bare float.
* **A reward is audited before it is taught.** `RewardIntegrityChecker`
  classifies every row VALID / SUSPICIOUS / INVALID / NEEDS_REVIEW with named
  findings — an unsafe run scoring high, a failed run scoring at all, a
  suspiciously long answer with no measured gain, a repeated action inflating
  the total, a verification that was never completed, two sources reading far
  apart. A suspicious row can be held without being destroyed.
* **Human and AI feedback are different things.** Every row records whether its
  source is human, an evaluator, a rule or a verifier; a composite total carries
  the per-source weights; and when a person and an evaluator disagree, the
  `FeedbackDisagreementDetector` records the disagreement and never picks a
  winner for them. A human-only run cannot silently learn from an AI score.
* **A higher reward is not proof of improvement.** The verdict on a model comes
  from measured behaviour on held-out examples
  (`reward_metrics_consulted: false`), and approval stays the registry's own
  explicit step. A reward curve is never read as an evaluation.

## 18.1 Two modes, one pipeline

`RLMode` is exactly `rlhf` (human feedback is the signal) and `rlaif` (an
evaluator's structured rating is the signal). A reward dataset declares its mode,
and a build refuses rows whose only support came from the other loop — with the
reason named (`human_feedback_required`, `mode_mismatch`).

Both loops walk the same ten pipeline stages, in this order:

| # | Stage | What it answers |
|---|-------|-----------------|
| 1 | `data_preparation` | is there a dataset version with usable rows? |
| 2 | `reward_generation` | does every row have a reward with provenance? |
| 3 | `reward_validation` | does it survive the integrity check? |
| 4 | `training_configuration` | is the configuration valid and complete? |
| 5 | `resource_estimation` | can this machine hold it? |
| 6 | `rollout_simulation` | what does the mock environment say? |
| 7 | `policy_optimization` | can the named optimizer run here at all? |
| 8 | `checkpointing` | where would checkpoints land and how many? |
| 9 | `evaluation` | how will base vs candidate be compared? |
| 10 | `registration` | which registry row would this become? |

`RLPipeline.plan()` builds the stage-by-stage plan and **never raises**; a plan
answers `ok` and names the blocked stages instead. `rlhf dry-run` validates,
prices, plans and simulates — and records `started: false` so nothing that reads
it can mistake it for a run.

## 18.2 RewardProvider — one abstraction, four voices

`RewardProvider` is the seam. `RewardRequest` carries the observable facts (a
traceable trajectory, its Phase 15 evaluation, structured feedback and ratings),
and every provider returns a `RewardResult` with the phase's provenance fields:
`reward_id`, `trajectory_id`, `evaluation_id`, `reward_source`, `confidence`,
`evaluator_id`, `total_reward`, `normalized_reward`, `component_rewards`,
`penalties`, `penalty_breakdown`, `reward_version`, `weights`,
`explanation_summary` and `evidence`.

| Provider | Source | What it reads |
|----------|--------|---------------|
| `HumanRewardProvider` | `human` | the person's structured verdict — accept, reject, prefer, rating, correction, error/unsafe report |
| `AIRatingRewardProvider` | `ai` | an evaluator's criterion scores, confidence and evidence |
| `EvaluationRewardProvider` | `rule`/`verifier` | Phase 15's weighted `RewardEngine`, re-used, never re-implemented |
| `CompositeRewardProvider` | `composite` | several of the above, weighted per source |

`auto` resolves to whatever the run's data can support (a composite); naming a
provider explicitly wires only its own sources, so a human-only run cannot
accidentally learn from an AI score. Default weights keep the sources
distinguishable: human `1.0`, verifier `0.8`, rule `0.6`, AI `0.4`. Each
feedback type maps to a signed value on a `-1..1` scale
(`accept +1.0` … `report_unsafe -1.0`, `rating` carries its own value), and an
evaluator that cannot answer raises `EvaluatorUnavailable` / returns a refusal —
it never guesses a number to fill the gap.

**Normalisation and clipping preserve meaning.** A raw total is always kept
beside the normalised signal, and a safety penalty stays its own component
(`keep_safety_separate`) with a floor of `-1.0`: "average behaviour was good but
this run was unsafe" must remain readable rather than being averaged away. A
refusal is not penalised, exactly as in Phase 15 — punishing the safety layer
would teach the next phase to avoid it.

## 18.3 Reward integrity — the hacking rails

`RewardIntegrityChecker` is the gate between a reward and a dataset. It returns a
`RewardIntegrityCheck` with a status and structured findings, never a boolean:

| Status | Meaning |
|--------|---------|
| `valid` | evidence is present, consistent and plausible |
| `suspicious` | usable only if a policy says so — held by default |
| `invalid` | must not become training data |
| `needs_review` | a person has to look before it is taught |

Named findings include `unsafe_action_with_positive_reward`,
`task_failed_but_reward_high`, `response_length_gaming`,
`repeated_actions_inflating_reward`, `reward_without_verification`,
`reward_without_observable_evidence`, `low_confidence_reward`,
`reward_source_not_allowed` and `source_disagreement`. Thresholds
live in `RewardPolicyConfig`: a reward above `0.9` with nothing verified, any
positive reward for a failed run, answers over `600` tokens with no measured
gain, the same action repeated more than twice. The dataset rules then decide
what happens: `require_integrity` (default true) and `hold_suspicious` (default
true) mean a suspicious row is **held and shown**, not deleted — `rlhf held`
lists exactly what was excluded and why.
## 18.4 Human feedback and the quality filter

`HumanFeedback` is deliberately narrow: a verdict (`accept`, `reject`,
`prefer_a`, `prefer_b`, `rating`, `correction`, `report_error`,
`report_unsafe`), an optional 1–5 rating, an optional correction, a reason
category, a confidence, provenance refs and a status. **There is no field for
reasoning, because none is collected** — a correction is text and is redacted
before it is stored, so a person cannot hand a credential to the training loop
by accident.

`FeedbackQualityFilter` classifies every row ACCEPTED / REJECTED /
NEEDS_REVIEW with named reasons: `unknown_feedback_type`,
`missing_feedback_target`, `invalid_rating`, `missing_rating`,
`low_confidence`, `duplicate_feedback`, `contradictory_feedback`,
`feedback_on_incomplete_task`, `sensitive_content`, `impossible_values`,
`invalid_candidate_reference`. **A rejected row is never deleted** — it keeps
its identity and its status, because "this feedback was not usable" is itself
worth being able to audit. A held row can be settled by a person
(`decide_feedback`: `accept` keeps it usable, `reject` does not; both record
who decided and why). A feedback row that is stored but refused still reports
`ok: false` honestly rather than pretending it landed.

## 18.5 AI ratings and the replaceable evaluator

An `AIRating` is RLAIF's unit of feedback: criterion scores, each with a reason
naming what was observed, plus an overall score, a confidence, evidence strings,
the evaluator's identity and version, and the candidate it judged. Ratings are
stored in their own repository, listed with a per-source breakdown, and the
rating's value comes from itself — the `rating` feedback type maps to `0` in the
feedback table by design.

`Evaluator` is an interface with five implementations and one refusal:

| Name | Behaviour |
|------|-----------|
| `auto` | resolves to the deterministic rule evaluator |
| `rule` | scores what can be objectively checked; says so neutrally where it cannot |
| `local` | a local judge, if one is wired |
| `external` | an injected judge, if one is available |
| `human` | a person's own feedback, if one exists |
| `none` / `off` | refuses to rate — never a silent zero |

Hidden reasoning is not a rating input: `HIDDEN_REASONING_KEYS`
(`chain_of_thought`, `cot`, `internal_monologue`, `reasoning`, `scratchpad`,
`thinking`) are discarded on the way in and recorded as discarded evidence. An
evaluator that cannot answer raises `EvaluatorUnavailable` and **no rating is
stored**; the evaluator is replaceable without changing the pipeline around it.

## 18.6 Human is not AI

Every reward, every dataset row and every rating records its source
(`human`, `ai`, `rule`, `verifier`, `composite`). A composite total carries its
per-source weights, and a named provider wires only its own source — so "the
human said yes" and "the evaluator scored 0.9" stay different facts.

`FeedbackDisagreementDetector` compares a person's verdict with an evaluator's
reading of the same trajectory and records `human_vs_ai` disagreements with a
`recommended: review` — it **never picks a winner** and never silently averages
the two. Disagreements are stored, listed, counted by kind, and re-detecting is
idempotent. Agreement is not a disagreement, and two AI sources disagreeing is
tracked the same way rather than resolved.

At dataset level the same rule shows up twice: a `mixed` dataset keeps human-only
and AI-only rows side by side (each row's mode is recorded), while an `rlhf`
dataset refuses a row whose only support is an evaluator endorsement
(`human_feedback_required` + `mode_mismatch`) and an `rlaif` dataset refuses a
row that also carries human feedback.

## 18.7 Reward datasets

`RewardDatasetBuilder` turns stored trajectories, evaluations, rewards, feedback
and ratings into an immutable `name@version` collection. `RewardExample` carries
the reward, its source, its integrity status, its evidence and the trajectory it
came from; `RewardDatasetVersion` carries the mode, the selection rules and their
counts, per-source statistics, the split map, a content `fingerprint()`, the
reward version and policy, and its own provenance. Rebuilding with the same
inputs produces the same content; a version is written once.

`RewardDatasetRules` is the one table of decisions:

| Rule | Default | Meaning |
|------|---------|---------|
| `mode` | `mixed` | `rlhf`, `rlaif` or both kinds of row |
| `require_integrity` | true | an INVALID reward cannot be taught |
| `hold_suspicious` | true | a SUSPICIOUS reward is held, not deleted |
| `require_evidence` | true | a reward without observable evidence is refused |
| `require_verified` | false | demand a verification pass |
| `require_human` / `require_ai` | false | demand that source specifically |
| `require_reward` | true | a row without a reward is not a reward row |
| `deduplicate` | true | identical rows collapse, with the count kept |
| `include_rejected` | true | rejected rows stay visible in the audit |
| `group_by` | `task` | splits keep a whole task together |

Every refusal has a name (`no_reward`, `reward_source_not_allowed`,
`reward_out_of_range`, `reward_integrity_invalid`, `reward_integrity_flagged`,
`reward_without_evidence`, `verification_required`, `human_feedback_required`,
`ai_feedback_required`, `mode_mismatch`, `duplicate_reward_example`,
`contradictory_rewards`, `malformed_example`, `missing_trajectory`) and the
counts stay on the version, so a small dataset explains itself. Splits reuse
Phase 16's deterministic, leak-free walk (`SplitConfig`, 80/10/10, seed 42);
`validate()` names every reason a version must not train on, and `held()` lists
exactly which rows were excluded and why. `max_examples` bounds a build for a
quick look; it never silently drops rows without recording that it did.

## 18.8 Configuration and the optimizer boundary

`RLTrainingConfig` is one validated place for a run's parameters: mode,
algorithm, models, dataset version, reward provider and policy, evaluator,
environment, rollouts (`rollout_count`, `max_steps`, `gamma`, `kl_coefficient`,
`clip_range`, learning rate, batch/gradient settings), checkpoints, LoRA and the
shared Phase 16 fields (hardware policy, precision, seed, `dry_run`). Invalid
values are errors, not clamps; a typo keeps the default. `dry_run` defaults to
`True`, `algorithm` defaults to `mock_policy`, and `needs_reference_model` is
true only when `kl_coefficient > 0`, so the reference model is counted in the
estimate only when it would exist.

The optimizer boundary is explicit about what is real:

| Algorithm | Implemented | Learns | Behaviour |
|-----------|-------------|--------|-----------|
| `mock_policy` | yes | no | a deterministic walk; every figure labelled simulated |
| `ppo` | no | yes | refused by name — "not implemented" |
| `grpo` | no | yes | refused by name — "not implemented" |

`PolicyOptimizer.describe()["simulated"]` is `not learns`, so a mock run cannot
report itself as a learned policy. A real run additionally needs a deployment
that permits it, an explicit confirmation, and a wired policy-optimizer runner;
without all three it stays a dry run.
## 18.9 Rollouts and the environment seam

A `Rollout` is a deterministic episode against an `Environment`:
`MockEnvironment` ships with the phase and needs no dependency, `ScriptedPolicy`
plays a fixed action sequence, and `RolloutRunner` records every `RolloutStep`
with its reward components. The mock's constants are fixed so two runs agree:
a successful step is worth `1.0`, a failure `-1.0`, merely stepping `0.1`, and
repeating an action past `2` costs `0.05` each time. A rollout is evidence about
the mock environment, and the phase never mistakes it for evidence about a real
one — the `environment` name is recorded, and `mock` is the only one shipped.

## 18.10 Resources — no CUDA required

`RLResourceEstimator` reuses Phase 16's component walk and adds the RL-specific
parts: the `reward_dataset`, the `experience_buffer`, and — only when
`kl_coefficient > 0` — the `reference_model` as a second copy of the weights, or
an honest statement that the size is unknown and the figure is a lower bound.
It prices against this machine's real memory reading, reports AUTO/CPU/GPU/NPU,
and returns SAFE / WARNING / UNSAFE with its reasons. An UNSAFE verdict is a
**refusal**, not a warning to click past; overriding it needs a deployment that
allows it. On a bare machine with the default configuration the verdict is
`warning` with backend `dry_run` — the point is that a 16 GB Windows desktop with
an Intel iGPU and an NPU can plan, price and simulate the whole pipeline.

## 18.11 Runs and checkpoints

`RLHFManager` extends the Phase 16 `TrainingManager`, so a run is the same
`TrainingRun` with `algorithm` and `rl_metrics`, the same checkpoints, the same
pause / resume / cancel and the same worker-thread start. `create_run` validates
an RL configuration **and audits the dataset**: an RLHF run needs human-backed
rows and an RLAIF run needs rating-backed rows, and the refusal lists exactly
which check failed — a run is never created against a dataset it cannot learn
from. A dry run walks a real step schedule and labels every figure `simulated`;
nothing about a scheduled number is allowed to read as a measurement. Run status
is Phase 16's own (`created` → `validating` → `preparing` → `running` →
`evaluating` → `completed`, plus paused/failed/cancelled), and the checkpoint
manager applies the configured retention.

## 18.12 Evaluation — a higher reward is not proof

`RLModelEvaluator.compare` compares up to four models — base, RL candidate, SFT
and preference-optimized — on held-out examples from a supervised dataset
(Phase 16's store, read not duplicated). The verdict is taken from the **worst**
comparison, with per-metric deltas, noise floors and blocking regression areas,
and the payload states `reward_metrics_consulted: false`: the reward curve the
run optimized is not evidence about the model. Without two predictors nothing
was measured, the comparison is `skipped`/`inconclusive`, and approval stays
impossible.

Approval, promotion and rollback are the Phase 16 registry's own explicit
transitions. Nothing in Phase 18 auto-promotes a model, and a higher training
reward never moves a registry row by itself.

## 18.13 API, CLI, settings, events and diagnostics

* **31 `/rlhf/*` routes** — status, summary, algorithms, estimate, dry-run,
pipeline, feedback (submit / list / decide), rate, ratings, disagreements,
datasets (build / list / read / validate / held), runs (create / list / read /
start / pause / resume / cancel / re-estimate / evaluate / checkpoints),
compare, evaluations, models. Every route answers with the application's own
method's shape, including refusals, which are HTTP errors with the reason.
* **A 30-action `novacontrol rlhf` CLI** — `rlhf status` is a safe first look,
and every action dispatches to the same application method the route calls, so
the CLI can do nothing the API could not. `rlhf dry-run --mode rlaif` plans
without starting anything.
* **Settings** — one `rlhf:` config section (enabled, dry_run, allow_unsafe,
hardware_policy, max_checkpoints, max_records, retention_days),
`NOVACONTROL_RLHF_*` environment overrides, and five user settings
(`rlhf_enabled`, `rlhf_dry_run`, `rlhf_max_checkpoints`, `rlhf_retention_days`,
`rlhf_max_records`) that round-trip through `POST /settings`.
* **Seven events** on the existing bus: `rlhf.feedback_received`,
`rlhf.feedback_decided`, `rlhf.rating_created`,
`rlhf.disagreement_detected`, `rlhf.dataset_built`, `rlhf.dry_run_completed`,
`rlhf.comparison_completed` — published through the failure-isolated publisher,
so a broken subscriber cannot break a run.
* **One runtime module** answering status/feedback/ratings/disagreements/
datasets/algorithms/estimate/pipeline requests and starting nothing.
* **The 24th diagnostics row** — `RLHF / RLAIF`, reported as SKIPPED when the
subsystem is switched off.

The subsystem has its own switch: with `rlhf_enabled` false, changing actions
answer `ok: false` with the reason ("switched off") rather than pretending, and
read-only surfaces keep answering their stored state.

## 18.14 Safety, privacy and what is absent

* **No chain-of-thought, ever.** There is no field for hidden reasoning in
feedback, ratings, rewards, dataset rows or runs; the phase's reasoning-key list
is discarded on the way in, and corrections are redacted before storage.
* **No secrets.** Residual credentials are refused or redacted by the same audit
redactor Phase 15 uses; a `REDACTED` value replaces them.
* **Nothing starts by itself.** `dry_run` is the default, `create_run` has no
side effects, and a real run needs the deployment's permission, an explicit
confirmation, and a wired optimizer runner.
* **No model is loaded or downloaded by the phase**, and no CUDA or NVIDIA GPU
is required — the tests prove the whole subsystem on mocks, deterministic
environments, synthetic rewards and dry runs.
* **Absent on purpose:** RLVR, critique-based learning, RLCD-style training,
agentic RL, game agents, distributed training, and the concrete
Transformers/PEFT loop. The optimizer interface is where the latter lands.

## Tests

`tests/test_rlhf.py` runs entirely on deterministic fixtures, mocks, synthetic
rewards and dry-run mode. It covers the schema and the vocabulary guard,
provider composition and per-source weights, normalisation/clipping with the
safety floor kept separate, every integrity finding and status, the quality
filter and redaction (nothing is deleted), all eight feedback types, the
evaluator set and `EvaluatorUnavailable`, hidden-reasoning refusal, the
disagreement detector (never a winner), reward datasets (rules, reason codes,
splits, leakage, held rows, immutability, fingerprints), rollouts and the mock
environment, configuration validation and the mock/ppo/grpo boundary, resource
estimates (reference model only with KL > 0, CPU-only machines), the pipeline's
ten stages and refusals, the manager end to end (datasets, runs, checkpoints,
pause/resume/cancel, evaluation, registry, persistence across a restart), the
bus module, the live application (dry runs, the four-model comparison, the
off switch, the diagnostics roster, the settings round-trip), the HTTP surface
over an isolated application, and the CLI parser and every dispatch.

## Gates

`python -m pytest tests/ -q`, `python scripts/generate_api_reference.py --check`
(docs in sync), `python -m mypy src` and `python -m mypy src --platform win32`.
Ruff is not a gate in this repository (the E501 baseline is older than it), but
`rlhf/` and `tests/test_rlhf.py` are ruff-clean apart from that baseline.

