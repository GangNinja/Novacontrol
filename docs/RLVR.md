# Reinforcement Learning with Verifiable Rewards (Phase 19)

Phase 19 adds **RLVR — Reinforcement Learning with Verifiable Rewards**, and the
critique-based learning that turns a verifier's finding into a corrected
example, without touching the architectures Phase 15–18 already own. It is built
directly on Phase 15's trajectories/evaluations/rewards, Phase 16's datasets,
runs, checkpoints, resource verdict, evaluation gate and model registry,
Phase 17's deterministic splits and dry-run discipline, and **Phase 18's reward
provider / integrity checker** — RLVR's reward provider *is* a `RewardProvider`
voice, not a second reward engine. It adds no second store, no second registry,
no second trajectory format and no second event bus.

> **Phase 19 is optional and experimental, and it ships one optimizer:
> `mock_policy`.** The verifiers run on observations, not opinions. Normal
> NovaControl operation works with every RLVR dependency absent: nothing in
> `rlvr/` imports `torch`, `transformers` or `peft` at module level, nothing
> downloads a model, no hidden chain-of-thought is stored, and no CUDA or NVIDIA
> GPU is required or assumed. `dry_run` is `True` by default, the RLVR switch is
> off by default, and **no run starts by itself**.
>
> **Not implemented, on purpose:** agentic RL, game agents, distributed training,
> and the concrete Transformers/PEFT optimisation loop (that is the optimizer
> boundary Phase 18 named — RLVR only fills it). This phase is verifiable-reward
> training only.

## The shape of it

Phase 15 records what happens, Phase 16 supervises it, Phase 17 prefers one
observed outcome over another, Phase 18 asks *"how much is that worth and can
the reward be trusted?"*, and Phase 19 asks a sharper question: **"worth is
only what can be checked, and the check comes first."**

RLVR replaces "a human or an evaluator told me this was good" with **"a verifier
I can re-run observed a concrete outcome."** The reward is still a claim with
provenance — and now that provenance is a verification the machine can reproduce
blindly, not an opinion the machine has to trust.

* **A reward is evidence, not opinion.** A `VerifiableRewardProvider` scores a
  trajectory by replaying it through registered verifiers and summing the
  verified checks; each check contributes a signed contribution with an explicit
  source (`verifier`), an evidence trail, and a fingerprint of the verifier
  policy that produced it. A human or AI source can still compose in, but it
  rides beside the verifiable signal rather than standing on its own.
* **A reward is audited before it is taught.** The Phase 18
  `RewardIntegrityChecker` runs unchanged on every row; RLVR widens what counts
  as evidence and adds `error_category` to `VerificationEvidence` so "the verifier
  itself mis-fired" is a distinct finding from "the run was unsafe."
* **Critique earns correction earns examples.** A verifier failure is not the end
  of the signal — `CritiqueEngine` turns structured findings into a
  `CritiqueResult`, a corrector turns that into a `CorrectionProposal`, and the
  `CorrectedExampleBuilder` turns a verified correction into a training row for
  the next SFT pass. The correction is verified before it is accepted, and the
  same integrity rails that guard a reward guard a corrected example.
* **A verifier can be disabled but never edited into passing.** Disabling a
  verifier removes it from the score; it never silently flips a failure to a
  pass. The verifier fingerprint is recorded on the reward so a result is not
  re-used after the policy changed without review.

## 19.1 The ten-stage pipeline

Both a plan and a dry run walk the same ten stages, in order. `plan()` builds
the stage-by-stage plan and **never raises** — it answers `ok` and names the
blocked stages instead. `rlvr dry-run` validates, prices, plans and simulates
all ten, and records `started: false` so nothing that reads it can mistake it
for a run.

| # | Stage | What it answers |
|---|-------|-----------------|
| 1 | `environment` | a deterministic episode is available (mock environment, scripted policy) |
| 2 | `model_action` | the policy's actions are reproducible on this machine |
| 3 | `verifier_selection` | the active verifier policy is consistent with the reward config |
| 4 | `verification` | each claim was checked by something re-runnable |
| 5 | `reward_calculation` | a signed reward with provenance was produced |
| 6 | `reward_validation` | the reward survives the Phase 18 integrity check |
| 7 | `critique_generation` | the verifier's findings surfaced as a critique |
| 8 | `dataset_generation` | verified failures became corrected training examples |
| 9 | `training_configuration` | the configuration is valid and complete |
| 10 | `evaluation` | how the corrected dataset grades the policy |

`pipeline.plan()` and a dry run return stage descriptors (`name`/status/`detail`/
`meta`) plus `blocked`/`skipped`/`done` so a gate can read readiness without
running anything.

## 19.2 Verifiers — checks that can be re-run

A `Verifier` is one checkable question, with a deterministic answer when it
has one. Every verifier takes a `VerificationRequest` (subject key, scope,
expectation, evidence token set, the machine's real workspace root) and returns a
`VerificationResult` with a `subject_key()`, a `status`, `detail`, `attempts`,
`duration_ms`, a `timestamp`, and evidence tokens the integrity check can cite.

| Verifier | Checks |
|----------|--------|
| `FileVerifier` | existence, content substring/hash, size, structure, JSON body |
| `ProcessVerifier` | running/exited, exit code, captured output |
| `TestVerifier` | that tests passed, and how many ran/failed/skipped |
| `HTTPVerifier` | a recorded HTTP response: status, schema, field presence |
| `DatabaseVerifier` | expected records, values and state changes |
| `GitVerifier` | branch, head, cleanliness, recorded changes |
| `OutputVerifier` | raw or structured output: exact, normalised, field-by-field |
| `SchemaVerifier` | a value against a declarative schema: fields, types, constraints |
| `CustomVerifier` | a domain check a plugin registers through the plugin SDK |

`CustomVerifier` is the seam — exactly how Phase 16's plugin SDK is the seam for
datasets. A verifier is registered once with `RLVRManager.register_verifier`;
registering the identical verifier again is a no-op, and `deterministic`/
`dry_run` are declared up front so a dry run can report the count honestly.

**Scoping and subjects.** Every result carries `subject_key()` because the same
verifier checking a step and then the task is multi-step verification, not a
duplicate. A step that ended by policy exhaustion (no terminal message) falls
back to the last observation so the verifier still has something concrete to
read. The integrity checker widens what looks like a safety failure
(`safety`/`unsafe` in detail, evidence tokens, `error_category`), and a missing
file is an `execution_error`, never a silent pass.

## 19.3 VerifiableRewardProvider — the reward voice

`VerifiableRewardProvider` is a `RewardProvider` voice (`reward_source =
"verifier"`). A `RewardRequest` is judged by replaying it through the active
verifier policy and summing the verified checks:

* a passing verification of a required fact contributes a positive term
* a failing verification of a required fact contributes a negative term
* an un-checked claim contributes nothing (it does not score positively)
* each term carries its evidence, its `verifier_id`, and the
  `verifier_policy_version` so the reward cannot be replayed after the policy
  changed

The Phase 18 `RewardValidator` (`novacontrol.rlvr.rewards`) consumes those terms
and reports findings: `unsafe_action_with_positive_reward`,
`task_failed_but_reward_high`, `reward_without_verification`, missing evidence,
and source disagreement when a human/AI component fights the verifier total.
A reward whose sign fights the task verdict is reported as an error; a safety
penalty stays its own component with a floor of `-1.0` exactly as Phase 18
specifies. A verifier can be disabled but never edited into passing, and a
`protected_target` / `expected_result_changed` / `verifiers_changed` /
`reward_config_changed` / `confirmation_required` code is raised when a result
is re-used after the policy it depended on moved.

## 19.4 Security — the hacking rails

`novacontrol.rlvr.security` owns the protections, and they are deliberately
narrow and named. A security decision is a `(code, principal, context, decision)`
record on a `SecurityRecord` with a timestamp.

| Code | Meaning |
|------|---------|
| `protected_target` | a verifier/policy/expected result is read-only / cannot be rewritten by the thing it is meant to check |
| `verification_control` | a check was bypassed, suppressed or short-circuited |
| `expected_result_changed` | a frozen expectation moved after it was frozen |
| `reward_config_changed` | the verifier/reward policy changed underneath a score being reused |
| `verifiers_changed` | the verifier set changed underneath a score being reused |
| `confirmation_required` | a real (non-dry-run) step was attempted without explicit confirmation |

The headline protections a reader can audit:

* **No self-reward.** The reward provider is itself governed by these checks; a
  result that would rewrite its own expectation is refused with
  `expected_result_changed` rather than scored up.
* **No editing a verifier into passing.** A verifier can be disabled — removing
  it from the score — but never silently flipped to pass. The
  `verification_control` code is raised when a check is bypassed.
* **Policy-locked rewards.** A reward carries the verifier policy fingerprint
  that produced it; reuse after that policy changes is refused
  (`verifiers_changed` / `reward_config_changed`) rather than silently re-scored.
* **No hidden chain-of-thought.** There is no field for hidden reasoning in
  trajectories, rewards, critiques, corrected examples or runs; the phase's
  reasoning-key list (`HIDDEN_REASONING_KEYS`) is discarded on the way in.
* **No secrets.** Residual credentials are refused or redacted by Phase 15's
  `Redactor`; a `REDACTED` value replaces them.
* **Nothing starts by itself.** `dry_run` is the default, `create` has no side
  effects, and a real optimiser step needs a deployment that permits it and an
  explicit confirmation (`confirmation_required`).

## 19.5 Critique and correction

`CritiqueEngine` turns structured verifier findings into a `CritiqueResult`
(critique id, trajectory id, verifier id, severity, category, the failing
expectation, the observed behaviour, the explanation, the source). A corrector
(the `CritiqueCorrector`) proposes a `CorrectionProposal`; the
`CorrectedExampleBuilder` turns an accepted, verified correction into a
`CorrectedExample` carrying the original input/output, the correction, the
verification it earned, and the trajectory id — the input to a later SFT pass.

* A hidden-reasoning correction is rejected outright before it becomes an
  example.
* A correction is verified before it is accepted; the builder's policy decides
  accept / hold, and a held example never trains.
* `CritiqueDatasetVersion` is immutable, content-fingerprinted, and rebuilt with
  the same inputs producing the same content; `accepted_examples()` exposes the
  trainable slice and `held()` the rest.
* A dataset with no accepted example refuses to train (`create` is refused for an
  untrainable dataset), so the loop never starts from a handful of unchecked
  rows.

## 19.6 Corrected-example pipeline

critique → correction → dataset, dry-run verifiable end to end:

1. a verifier flags a failing expectation during a rollout;
2. `CritiqueEngine` emits a `CritiqueResult` (with `output_format_error` for
   malformed output, the dry-run's honest readout when the scripted task
   failed);
3. the corrector emits a `CorrectionProposal`;
4. `CorrectedExampleBuilder.build(...)` produces a `CorrectedExample`, verified
   before acceptance;
5. `CritiqueDatasetBuilder` writes an immutable `name@version` with splits,
   statistics, fingerprints and `accepted_examples()`/`held()`/`validate()`.

## 19.7 Configuration and the optimizer boundary

`RLVRTrainingConfig` is the single validated place for an RLVR run. It nests the
shared Phase 18 RL schedule (`rl = RLTrainingConfig`), the `verifiers` policy
(`VerifierPolicyConfig`) and the `reward` policy (`VerifiableRewardConfig`),
plus the `critique` rules (`CritiqueConfig`) and the
`critique_dataset_version`. `RLTrainingConfig` rejects a
`critique_dataset_version` (it has no home there); that field lives only on
`RLVRTrainingConfig`, and CLI `--set` keys are routed into the right block by
`_RLVR_KEYS` / `_RL_KEYS`. Invalid values are errors, not clamps; a typo keeps
the default.

The optimizer boundary is identical to Phase 18's:

| Algorithm | Implemented | Learns | Behaviour |
|-----------|-------------|--------|-----------|
| `mock_policy` | yes | no | a deterministic walk; every figure labelled simulated |
| `ppo` / `grpo` | no | yes | refused by name — "not implemented" |

`PolicyOptimizer.describe()["simulated"]` is `not learns`, so a mock run cannot
report itself as a learned policy. A real optimiser step additionally needs a
deployment that permits it, an explicit confirmation, and a wired policy-runner;
without all three it stays a dry run.

## 19.8 The model, environment and rollout seam (reused)

A rollout is a deterministic episode against an `Environment`: `MockEnvironment`
ships with Phase 18 and needs no dependency, `ScriptedPolicy` plays a fixed
action sequence, and `RolloutRunner` records every step with its reward
components. RLVR's verifiers read the episode's observations (a step, a task, a
file, an HTTP response), so they are testable on synthetic trajectories without
any model. The `environment` name is recorded on the episode, and `mock` is the
only one shipped.

## 19.9 Runs and checkpoints (reused)

`RLVRManager` extends the Phase 16 `TrainingManager`, so a run is the same
`TrainingRun` (same checkpoints, same pause/resume/cancel and worker-thread
start) with `algorithm = "rlvr"` and `rl_metrics` carrying the verifier
snapshot, reward config version and critique audit issues. `create_run`
validates the configuration **and** audits the critique dataset: a dataset with
no accepted example is refused, and the refusal lists exactly which check failed.
`RLModelEvaluator` (Phase 16) grades the corrected dataset; a dry run labels
every figure `simulated` and `started: false`. Approval and promotion stay the
Phase 16 registry's own explicit transitions.

## 19.10 API, CLI, settings, events and diagnostics

* **31 `/rlvr/*` routes** — `status`, `summary`, `verifiers`,
  `verifiers/disable`, `verifiers/enable`, `verify`, `reward`, `critiques`,
  `corrections`, `datasets`, `datasets/{id}/validate`, `held`, `pairs`, `{id}`,
  `estimate`, `pipeline`, `dry-run`, `evaluate`, `runs`, `runs/start`, `pause`,
  `cancel`, `resume`, `re-estimate`, `evaluate/{id}/checkpoints`, `runs/{id}`.
  Every route dispatches to the application's own `rlvr_*` method, which refuses
  cleanly when the subsystem is switched off (HTTP error with the reason).
* **A `novacontrol rlvr` CLI** — `status` is a safe first look (8 verifiers, 8
  deterministic, dry-run true); `verify`, `reward`, `critique`,
  `corrections`, `propose`, `build`, `validate`, `held`, `pairs`, `datasets`,
  `estimate`, `pipeline`, `dry-run`, `create`, `runs`, `summary`. `rlvr dry-run
  --model ... --tasks ... --dataset-version ...` plans without starting anything.
* **Settings** — a single `rlvr:` config section (enabled, dry_run, base model,
  algorithm, verifiers, reward, critique) plus `NOVACONTROL_RLVR_*` overrides.
  The RLVR switch is **off by default**; with `rlvr_enabled` false, mutating
  actions answer `ok: false` with the reason ("switched off").
* **Events** — on the existing bus, published and failure-isolated, with
  seven request/reply pairs defined in `rlvr/runtime.py`:
  `rlvr.status_requested` / `rlvr.status_completed`,
  `rlvr.verifiers_requested` / `rlvr.verifiers_completed`,
  `rlvr.critiques_requested` / `rlvr.critiques_completed`,
  `rlvr.corrections_requested` / `rlvr.corrections_completed`,
  `rlvr.datasets_requested` / `rlvr.datasets_completed`,
  `rlvr.pipeline_requested` / `rlvr.pipeline_completed`,
  `rlvr.dry_run_requested` / `rlvr.dry_run_completed`.
* **The 25th diagnostics row** — `RLVR`, reported as SKIPPED when the subsystem
  is switched off, DEGRADED when the optional training dependencies are absent
  (verification, critiques, datasets, planning and dry runs all still work),
  OK when the dependencies are installed.

## 19.11 Surfaces

`RLVRManager` extends `RLHFManager` (which extends the Phase 16
`TrainingManager`): one class, one set of routes, one CLI namespace. `resolve_rlvr_config`
routes the Phase 16 flat CLI keys (`--set base_model=...`) into the nested `rl`
block, and `RLTrainingConfig.from_training_config` projects a Phase 16 caller's
configuration onto the RL schedule. `RLVREvaluation` carries the run-scoped
verdict (verification accuracy, false positive/negative, verifier agreement,
critique accuracy, correction success, reward integrity, metrics, evidence).

## 19.12 Safety, privacy and what is absent

* **No chain-of-thought, ever.** No field for hidden reasoning in verifiers,
  rewards, critiques, corrected examples or runs; reasoning keys are discarded
  on the way in; corrections are redacted before storage.
* **No secrets.** Residual credentials refused or redacted by Phase 15's
  redactor.
* **Nothing starts by itself.** `dry_run` is the default; `rlvr create` has no
  side effects beyond a `created` run row; a real optimiser step needs the
  deployment's permission, an explicit confirmation, and a wired runner.
* **No model is loaded or downloaded by the phase**, and no CUDA or NVIDIA GPU is
  required — the tests prove the whole subsystem on mocks, deterministic
  environments, synthetic trajectories and dry runs.
* **Absent on purpose:** agentic RL, game agents, distributed training, and the
  concrete Transformers/PEFT loop (the optimizer boundary Phase 18 named — RLVR
  only fills it). It also stops here, before Phase 20.

## Dry-run status

A deterministic dry run (`--model nova-mock --tasks ... --dataset-version
smoke-demo@1.0.0 --labels ...`) walks all ten stages as `done`: 2 tasks,
t-ok reward `1.0`, t-fail reward `-1.0`, reward total `0.0`, reward validation
findings `[]`, integrity `{valid: 2}`, verification accuracy `1.0`, fp/fn `0`.
Critiques surfaced two honest `output_format_error` findings from the failing
task, which became corrected examples and were held back from the dataset.

## Tests

`tests/test_rlvr.py` runs entirely on deterministic fixtures, mocks, synthetic
trajectories and dry-run mode — 188 tests, no model loaded, no GPU required. It
covers the verifier schema and registry (registration idempotency, disable/enable,
deterministic/dry-run flags, fingerprint), every verifier's result shape and
`error_category`, the widened safety-failure detection, the
`VerifiableRewardProvider` (signed contributions, evidence, policy fingerprint,
source weights), `RewardValidator` findings (reward-sign-vs-verdict, missing
evidence, unsafe-positive), `CritiqueEngine` and `CorrectedExampleBuilder`
(hidden-reasoning refusal, verification gate, held examples), critique datasets
(rules, reason codes, splits, leakage, held rows, `validate()` refusing a
dataset with no accepted example, immutability, fingerprints), the ten-stage
pipeline and its refusals, `RLVRManager` end to end (datasets, runs, create-run
audits, the off switch, the diagnostics roster), security codes
(`protected_target`/`verification_control`/`expected_result_changed`/
`reward_config_changed`/`verifiers_changed`/`confirmation_required`,
no self-reward, no editing a verifier into passing), config routing and the
CLI parser and dispatches. The Phase 19 additions also pin the 25-row
diagnostics roster (`tests/test_optimization.py`, `tests/test_web_api.py`).

## Gates

`python -m pytest tests/ -q`, `python scripts/generate_api_reference.py --check`
(docs in sync), `python -m mypy src` and `python -m mypy src --platform win32`.
`ruff` is local only and not a CI gate in this repository; `rlvr/` is ruff-clean
apart from the documented `E501` baseline.
