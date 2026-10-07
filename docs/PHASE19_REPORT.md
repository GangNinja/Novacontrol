# Phase 19 Report — RLVR + Critique-Based Learning

## Summary

Phase 19 adds **RLVR — Reinforcement Learning with Verifiable Rewards** and the
critique-based learning that turns a verifier's finding into a corrected example,
built **directly on top of** the systems Phase 15–18 already own. It is optional
and experimental: the RLVR switch defaults off, `dry_run` defaults true, no model
is loaded or downloaded, no CUDA/NVIDIA GPU is required, and nothing starts
training by itself.

The phase ships **188 tests** (`tests/test_rlvr.py`), all running on
deterministic mocks, synthetic trajectories and dry-run mode — no model loaded,
no GPU required.

---

## 1. Files Added

### New package: `src/novacontrol/rlvr/` (15 modules)

| Module | Responsibility |
|--------|---------------|
| `__init__.py` | Package init. |
| `models.py` | `VerificationResult`, `CritiqueResult`, `CorrectionProposal`, `CorrectedExample`, `CritiqueDatasetVersion`, `RLVREvaluation`, and supporting enums/fingerprints. |
| `config.py` | `RLVRTrainingConfig` (nests `RLTrainingConfig` + `VerifierPolicyConfig` + `VerifiableRewardConfig` + `CritiqueConfig`), resolver helpers, `_RLVR_KEYS`/`_RL_KEYS` routing. |
| `verifiers.py` | Re-runnable verifiers: `FileVerifier`, `ProcessVerifier`, `TestVerifier`, `HTTPVerifier`, `DatabaseVerifier`, `GitVerifier`, `OutputVerifier`, `SchemaVerifier`, `CustomVerifier`. Includes the widened `_is_safety_failure` (`error_category`). |
| `registry.py` | `VerifierRegistry` — registration idempotency, disable/enable, deterministic/dry-run flags, fingerprinting. |
| `rewards.py` | `VerifiableRewardProvider` (`reward_source="verifier"`), `RewardValidator` findings, `VerifiableRewardConfig` with source weights. |
| `critique.py` | `CritiqueEngine`, `CritiqueCorrector`, `CritiqueConfig`. |
| `datasets.py` | `CorrectedExampleBuilder`, `CritiqueDatasetBuilder`, `CritiqueDatasetVersion` with `accepted_examples()`/`held()`/`validate()`. |
| `security.py` | `SecurityRecord`, the five security codes, `HIDDEN_REASONING_KEYS`. |
| `evaluation.py` | `RLVREvaluation`, `RewardResult` from evaluation.reward. |
| `storage.py` | `build_rlvr_repositories` — JSONL repositories for critiques, corrections, datasets. |
| `trainer.py` | Trainer boundary (`mock_policy` only; `ppo`/`grpo` refused). |
| `pipeline.py` | Ten-stage pipeline: `build` + `build_error`, stage descriptors, plan/simulate. |
| `runtime.py` | `RLVRModule` — bus integration with seven request/reply event pairs. |
| `manager.py` | `RLVRManager` — extends `RLHFManager`; `rlvr_status`, `verifiers`, `critiques_*`, `corrections_*`, `critique_datasets_list`, `pipeline_plan`, `dry_run`, `create_run`, `evaluate`. |

### New test file

| File | Tests |
|------|-------|
| `tests/test_rlvr.py` | 188 tests, deterministic mocks/synthetic/dry-run. |

### New documentation

| File |
|------|
| `docs/RLVR.md` |
| `docs/PHASE19_REPORT.md` (this file) |

### New directory

| Path | Contents |
|------|----------|
| `training_output/` | RLVR dry-run artefact output (created at runtime, gitignored). |

---

## 2. Files Modified

### Source code

| File | Change |
|------|--------|
| `src/novacontrol/application.py` | `rlvr_*` methods: `rlvr_status()`, `rlvr_summary()`, `rlvr_verifiers()`, `rlvr_verify()`, `rlvr_reward()`, `rlvr_critiques()`, `rlvr_corrections()`, `rlvr_propose()`, `rlvr_build()`, `rlvr_validate()`, `rlvr_held()`, `rlvr_pairs()`, `rlvr_datasets()`, `rlvr_estimate()`, `rlvr_pipeline()`, `rlvr_dry_run()`, `rlvr_evaluate()`, `rlvr_runs()`, `rlvr_run_start()`, `rlvr_run_pause()`, `rlvr_run_cancel()`, `rlvr_run_resume()`, `rlvr_run_re_estimate()`, `rlvr_run_checkpoints()`. Wired into `build()`/`apply()`/`diagnostics()`. |
| `src/novacontrol/api/app.py` | 31 `/rlvr/*` routes registered on the FastAPI application. |
| `src/novacontrol/api/models.py` | 31 RLVR route payloads/models (confirmed by grep). |
| `src/novacontrol/api/route_consumers.py` | RLVR route handlers dispatch to application's own `rlvr_*` methods. |
| `src/novacontrol/core/config.py` | `RLVRSettings` config section. |
| `src/novacontrol/settings/models.py` | `rlvr_*` user settings. |
| `src/novacontrol/settings/manager.py` | `rlvr_*` settings application and overrides. |
| `src/novacontrol/cli/commands.py` | `rlvr` CLI commands: `status`, `verify`, `reward`, `critique`, `corrections`, `propose`, `build`, `validate`, `held`, `pairs`, `datasets`, `estimate`, `pipeline`, `dry-run`, `create`, `runs`, `summary`. |
| `src/novacontrol/cli/parser.py` | `rlvr` subparser with `_rlvr_config_args`. |
| `src/novacontrol/__main__.py` | RLVR entry point wiring. |
| `src/novacontrol/rlhf/integrity.py` | Widened `RewardValidator` safety-failure detection — `_is_safety_failure` now matches `safety` and `unsafe` in detail/evidence and honours `error_category`. |

### Infrastructure / wiring

| File | Change |
|------|--------|
| `src/novacontrol/core/config.py` | `RLVRSettings` with `enabled`, `dry_run`, `base_model`, `algorithm`, `verifiers`, `reward`, `critique`, `verifier_policy`, `reward_config`, `dataset_version`. |

### Tests

| File | Change |
|------|--------|
| `tests/test_optimization.py` | Diagnostics roster pin: 25 components, `RLVR` added as 25th. |
| `tests/test_web_api.py` | HTTP diagnostics route pin: 25 components, `RLVR` in the set. |

---

## 3. Verifier Architecture

### What a verifier is

A `Verifier` is one checkable question with a deterministic answer when it has one.
Every verifier takes a `VerificationRequest` (subject key, scope, expectation,
evidence token set, the machine's real workspace root) and returns a
`VerificationResult` with:

- `subject_key()` — so the same verifier checking a step and then the task is
  multi-step verification, not a duplicate.
- `status` — `PASS` / `FAIL` / `SKIPPED` / `UNKNOWN`.
- `detail`, `attempts`, `duration_ms`, `timestamp`.
- evidence tokens the integrity check can cite.
- `error_category` — so "the verifier itself mis-fired" is distinct from "the run
  was unsafe".

### Verifiers

| Verifier | Checks |
|----------|--------|
| `FileVerifier` | existence, content substring/hash, size, structure, JSON body. |
| `ProcessVerifier` | running/exited, exit code, captured output. |
| `TestVerifier` | tests passed, and how many ran/failed/skipped. |
| `HTTPVerifier` | recorded HTTP response: status, schema, field presence. |
| `DatabaseVerifier` | expected records, values, state changes. |
| `GitVerifier` | branch, head, cleanliness, recorded changes. |
| `OutputVerifier` | raw or structured output: exact, normalised, field-by-field. |
| `SchemaVerifier` | a value against a declarative schema: fields, types, constraints. |
| `CustomVerifier` | a domain check a plugin registers through the plugin SDK. |

### Registry

`VerifierRegistry` at `self.verifiers_registry` (ctor param `verifier_registry`),
**never** shadowing the inherited `self.registry` (the `SFTModelRegistry`).
Registering the identical verifier again is a no-op. `deterministic` and
`dry_run` are declared up front so a dry run reports the count honestly.

### Scoping and subjects

Every result carries `subject_key()`. A step that ended by policy exhaustion
(no terminal message) falls back to the last observation so the verifier always
has something concrete to read. The integrity check widens what looks like a safety
failure: `safety`/`unsafe` in detail, evidence tokens, and `error_category`.
A missing file is an `execution_error`, never a silent pass.

---

## 4. RLVR Status

### Design principles

- **Optional / experimental.** The `rlvr:` config section defaults `enabled`
  to `false`; with it off, mutating actions answer `ok: false` with the reason
  ("switched off").
- **Dry-run by default.** `dry_run` is `True`; `create`/`create_run` have no side
  effects beyond a `created` run row.
- **No model loading.** No `torch`, `transformers`, `peft` import at module level;
  no download; no CUDA/NVIDIA GPU required or assumed.
- **No auto-training.** A real optimiser step additionally needs a deployment
  that permits it and explicit confirmation.
- **No hidden chain-of-thought.** `HIDDEN_REASONING_KEYS` are discarded on intake;
  no field for hidden reasoning exists in trajectories, rewards, critiques,
  corrected examples or runs.

### Optimiser boundary (identical to Phase 18's)

| Algorithm | Implemented | Learns | Behaviour |
|-----------|-------------|--------|-----------|
| `mock_policy` | yes | no | deterministic walk; every figure labelled simulated |
| `ppo` / `grpo` | no | yes | refused by name — "not implemented" |

`PolicyOptimizer.describe()["simulated"]` is `not learns`, so a mock run cannot
report itself as a learned policy.

---

## 5. Reward Validation

### VerifiableRewardProvider

`VerifiableRewardProvider` is a `RewardProvider` voice
(`reward_source = "verifier"`). A `RewardRequest` is judged by replaying it
through the active verifier policy and summing the verified checks:

- A passing verification of a required fact → positive term.
- A failing verification of a required fact → negative term.
- An un-checked claim → contributes nothing (does not score positively).
- Each term carries its evidence, its `verifier_id`, and the
  `verifier_policy_version` so the reward cannot be replayed after the policy
  changed.

### RewardValidator (Phase 18 integrity checker, widened)

`RewardValidator` (`novacontrol.rlvr.rewards`, reusing the Phase 18
`RewardIntegrityChecker` shape) reports findings:

- `unsafe_action_with_positive_reward`
- `task_failed_but_reward_high`
- `reward_without_verification`
- missing evidence
- source disagreement when a human/AI component fights the verifier total

A reward whose sign fights the task verdict is an error; a safety penalty stays
its own component with a floor of `-1.0` exactly as Phase 18 specifies.

### Refusals

- A reward whose sign fights the task verdict is reported as an error.
- A safety penalty is its own component with a floor of `-1.0`.
- A reward re-used after `verifiers_changed` or `reward_config_changed` is
  refused rather than silently re-scored.
- A `protected_target` is refused (a verifier/policy/expected result cannot be
  rewritten by the thing it is meant to check).

---

## 6. Critique Engine

### Flow: critique → correction → dataset

1. A verifier flags a failing expectation during a rollout.
2. `CritiqueEngine` emits a `CritiqueResult` (critique id, trajectory id,
   verifier id, severity, category, the failing expectation, the observed
   behaviour, the explanation, the source). Malformed output surfaces as
   `output_format_error` — the dry run's honest readout when the scripted task
   failed.
3. `CritiqueCorrector` emits a `CorrectionProposal`.
4. `CorrectedExampleBuilder.build(...)` produces a `CorrectedExample`,
   verified before acceptance. A hidden-reasoning correction is rejected
   outright before it becomes an example.
5. `CritiqueDatasetBuilder` writes an immutable `name@version` with splits,
   statistics, fingerprints, and `accepted_examples()` / `held()` / `validate()`.

### Dataset invariants

- `CritiqueDatasetVersion` is immutable, content-fingerprinted, and rebuilt with
  the same inputs producing the same content.
- `accepted_examples()` exposes the trainable slice; `held()` the rest.
- A dataset with no accepted example is **refused** for training (`create` is
  refused for an untrainable dataset) — the loop never starts from unchecked
  rows.

---

## 7. Corrected-Example Pipeline (10-stage)

Both `plan()` and a dry run walk the same ten stages, in order. `plan()` builds
the stage-by-stage plan and **never raises** — it answers `ok` and names the
blocked stages instead. `rlvr dry-run` validates, prices, plans and simulates
all ten, and records `started: false` so nothing can mistake it for a run.

| # | Stage | What it answers |
|---|-------|-----------------|
| 1 | `environment` | A deterministic episode is available (mock environment, scripted policy). |
| 2 | `model_action` | The policy's actions are reproducible on this machine. |
| 3 | `verifier_selection` | The active verifier policy is consistent with the reward config. |
| 4 | `verification` | Each claim was checked by something re-runnable. |
| 5 | `reward_calculation` | A signed reward with provenance was produced. |
| 6 | `reward_validation` | The reward survives the Phase 18 integrity check. |
| 7 | `critique_generation` | The verifier's findings surfaced as a critique. |
| 8 | `dataset_generation` | Verified failures became corrected training examples. |
| 9 | `training_configuration` | The configuration is valid and complete. |
| 10 | `evaluation` | How the corrected dataset grades the policy. |

---

## 8. Preference / Reward Integration

RLVR does **not** add a second reward engine. `VerifiableRewardProvider` is a
`RewardProvider` voice alongside the four Phase 18 voices (`human`, `ai`,
`rule`/`verifier`, composite). A `RewardRequest` that names the verifier source
is replayed through the verifier policy; a request that names a source the run
has none of gets a refusal, never a guessed number.

The Phase 18 `RewardIntegrityChecker` runs unchanged on every row. RLVR widens
what counts as a safety failure (adding `error_category`) and adds the
policy-fingerprint reuse guard (`verifiers_changed` / `reward_config_changed`).

A human or AI source can still compose in, but it rides beside the verifiable
signal rather than standing on its own — a reward sign that fights the
verifier total is a `source_disagreement` finding, not a pass.

---

## 9. Security

`novacontrol.rlvr.security` owns the protections. A security decision is a
`(code, principal, context, decision)` record on a `SecurityRecord` with a
timestamp.

| Code | Meaning |
|------|---------|
| `protected_target` | A verifier/policy/expected result is read-only / cannot be rewritten by the thing it is meant to check. |
| `verification_control` | A check was bypassed, suppressed or short-circuited. |
| `expected_result_changed` | A frozen expectation moved after it was frozen. |
| `reward_config_changed` | The verifier/reward policy changed underneath a score being reused. |
| `verifiers_changed` | The verifier set changed underneath a score being reused. |
| `confirmation_required` | A real (non-dry-run) step was attempted without explicit confirmation. |

### Headline protections

- **No self-reward.** The reward provider is itself governed by these checks;
  a result that would rewrite its own expectation is refused with
  `expected_result_changed` rather than scored up.
- **No editing a verifier into passing.** A verifier can be disabled — removing
  it from the score — but never silently flipped to pass. The
  `verification_control` code is raised when a check is bypassed.
- **Policy-locked rewards.** A reward carries the verifier policy fingerprint
  that produced it; reuse after that policy changes is refused
  (`verifiers_changed` / `reward_config_changed`) rather than silently re-scored.
- **No hidden chain-of-thought.** `HIDDEN_REASONING_KEYS` are discarded on
  intake; no field for hidden reasoning in any Phase 19 structure.
- **No secrets.** Residual credentials refused or redacted by Phase 15's
  `Redactor`; a `REDACTED` value replaces them.
- **Nothing starts by itself.** `dry_run` is the default, `create` has no side
  effects, and a real optimiser step needs the deployment's permission, an
  explicit confirmation, and a wired policy runner.

---

## 10. Configuration

`RLVRTrainingConfig` is the single validated place for an RLVR run. It nests:

- The shared Phase 18 RL schedule (`rl = RLTrainingConfig`).
- The `verifiers` policy (`VerifierPolicyConfig`).
- The `reward` policy (`VerifiableRewardConfig`).
- The `critique` rules (`CritiqueConfig`).
- The `critique_dataset_version`.

`RLTrainingConfig` rejects a `critique_dataset_version` (it has no home there);
that field lives only on `RLVRTrainingConfig`. CLI `--set` keys are routed into
the right block by `_RLVR_KEYS` / `_RL_KEYS`. Invalid values are errors, not
clamps; a typo keeps the default.

`resolve_rlvr_config` routes the Phase 16 flat CLI keys
(`--set base_model=...`) into the nested `rl` block.

---

## 11. Naming and Collision Avoidance

| Concern | Resolution |
|---------|------------|
| `application.py` imports agentcore `Verifier` | RLVR's aliases as `RLVRVerifier` to avoid the name collision. |
| `RLVRManager` inherits `self.registry` (SFTModelRegistry) | The verifier registry is at `self.verifiers_registry` (ctor param `verifier_registry`). |
| `RLVRManager` inherits `self.pipeline` (Phase 18 RLPipeline) | RLVR's pipeline is at `self.rlvr_pipeline` (ctor param `pipeline`). |
| `RLVRManager` extends `RLHFManager` extends `TrainingManager` | One class, one set of routes, one CLI namespace — no duplication. |

---

## 12. Dry-Run Results

A deterministic dry run
(`--model nova-mock --tasks ... --dataset-version smoke-demo@1.0.0 --labels ...`)
walks all ten stages as `done`:

| Metric | Value |
|--------|-------|
| Stages | 10 (all `done`) |
| Tasks | 2 |
| t-ok reward | `1.0` |
| t-fail reward | `-1.0` |
| reward total | `0.0` |
| reward validation findings | `[]` |
| integrity | `{valid: 2}` |
| verification accuracy | `1.0` |
| false positives | `0` |
| false negatives | `0` |
| critiques | 2 `output_format_error` findings from the failing task |
| corrections | became corrected examples, held back from the dataset |

`started: false` is recorded so no consumer can mistake the dry run for a run.

---

## 13. Tests

`tests/test_rlvr.py` — **188 tests**, all deterministic, no model loaded, no GPU:

- Verifier schema and registry (registration idempotency, disable/enable,
  deterministic/dry-run flags, fingerprint).
- Every verifier's result shape and `error_category`.
- Widened `_is_safety_failure` detection (`safety`/`unsafe` in detail, evidence
  tokens, `error_category`).
- `VerifiableRewardProvider` (signed contributions, evidence, policy
  fingerprint, source weights).
- `RewardValidator` findings (reward-sign-vs-verdict, missing evidence,
  unsafe-positive).
- `CritiqueEngine` and `CorrectedExampleBuilder` (hidden-reasoning refusal,
  verification gate, held examples).
- Critique datasets (rules, reason codes, splits, leakage, held rows,
  `validate()` refusing a dataset with no accepted example, immutability,
  fingerprints).
- The ten-stage pipeline and its refusals.
- `RLVRManager` end to end (datasets, runs, create-run audits, the off switch,
  the diagnostics roster).
- Security codes (`protected_target` / `verification_control` /
  `expected_result_changed` / `reward_config_changed` / `verifiers_changed` /
  `confirmation_required`; no self-reward; no editing a verifier into passing).
- Config routing and the CLI parser and dispatches.

Diagnostics roster pins (also updated by Phase 19):

| File | Pin |
|------|-----|
| `tests/test_optimization.py` L963 | `assertEqual(len(report["components"]), 25)`; line 960 `"RLVR"` in roster. |
| `tests/test_web_api.py` L1171 | `assertEqual(len(payload["components"]), 25)`; line 1173 `"RLVR"` in set. |

---

## 14. Gates — Verification Results

| Gate | Command | Result |
|------|---------|--------|
| RLVR unit tests | `pytest tests/test_rlvr.py -q` | **188 passed**, 39 warnings, 179s. |
| Diagnostics roster (local) | `pytest tests/test_optimization.py -k diagnostic_roster` | **1 passed** (25 components, RLVR is 25th). |
| Diagnostics route (HTTP) | `pytest tests/test_web_api.py -k diagnostics_route` | **1 passed** (25 components, RLVR in set). |
| mypy (default) | `mypy src` | **Success: no issues found in 320 source files.** |
| mypy (win32) | `mypy src --platform win32` | **Success: no issues found in 320 source files.** |
| API reference sync | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (207 routes, 31 `/rlvr/*`). |
| Route count | `grep -c '/rlvr' docs/API.md`; total routes | 31 `/rlvr`; 207 total. |

> Note: the full `test_optimization.py` and `test_web_api.py` suites were not
> re-run in full during this turn (they exceed the per-command timeout), but the
> specific diagnostics-roster tests that pin the Phase 19 changes were verified
> green. The broader suite gate was confirmed green in the interrupted session.

### 14.1 Repairs after the phase closed

One of this phase's regression checks could not hold on a machine that has
`psutil` installed, and the whole suite was later re-run to completion:

- **`NoReasoningRegressionTests::test_the_package_imports_no_optional_heavy_dependency`
  now runs its property in a fresh interpreter.** It asserted `psutil` was absent
  from `sys.modules`, but the same test module imports `novacontrol.api.app`,
  whose module-level `app = create_app()` builds the application and reaches the
  telemetry layer's optional psutil probe — so the assertion could only hold on a
  machine WITHOUT psutil (which is why CI was green) and always failed on one with
  it, although the property it names was true in both cases. It now runs
  `import novacontrol.rlvr` in a subprocess and requires the child's `sys.modules`
  to contain none of `torch`, `transformers` and `psutil`.
- **The full phase suite and both gates were re-run in the Phases 15–20
  verification pass** (§49 of [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md)):
  `tests/test_rlvr.py` green in the whole-tree run of **3,325 passed / 12 skipped,
  2,099 subtests, 0 failed**, and mypy clean in both platform views (333 files).

---

## 15. Deferred Items

| Item | Why deferred |
|------|-------------|
| Concrete Transformers/PEFT optimisation loop | This is the optimizer boundary Phase 18 named; RLVR only fills the `mock_policy` side of it. |
| Agentic RL, game agents | Explicitly Phase 20. |
| Distributed training | Explicitly out of scope for this phase. |
| Full cross-phase test gate subset re-run | The full `test_optimization.py` + `test_web_api.py` + `test_preference.py` subset (495 tests, ~1925s) was verified green in the interrupted session; only the specific diagnostics-pins were re-verified in this turn to stay within command timeouts. |
| CLI smoke test re-run | Verified green in the interrupted session; not re-run in this turn due to timeout. |

---

## 16. Compatibility

- **Backward compatible.** All existing Phase 15–18 behaviour is unchanged; RLVR
  is an additive package (`novacontrol.rlvr`) and a 25th diagnostics row.
- **No duplicate systems.** RLVR reuses `Trajectory`, `EvaluationEngine`,
  `RewardEngine`, `RLTrainer`, `RewardProvider`, `Environment`, `ModelRegistry`,
  `ResourceGovernor`, `PermissionRisk`, `EventBus`, and telemetry infrastructure
  — no second store, no second registry, no second trajectory format, no second
  event bus.
- **No forced dependencies.** Nothing in `rlvr/` imports `torch`,
  `transformers` or `peft` at module level; the optional training dependencies
  are detected at runtime.
- **Off by default.** `rlvr_enabled` defaults to `false`; with it off, mutating
  actions answer `ok: false` with the reason.
