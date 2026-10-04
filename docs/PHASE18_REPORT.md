# Phase 18 Final Report — RLHF + RLAIF

Scope: implement the RLHF/RLAIF subsystem on top of Phases 15–17, validate it,
and stop before Phase 19. This report records what was added, what changed, what
was verified, what was found and fixed, and what is deliberately deferred.

**Result: complete and validated.** All four gates pass on the frozen tree, no
real training starts by itself, no model is loaded, and no CUDA or NVIDIA GPU is
required.

## Files added

| Path | What it is |
|------|------------|
| `src/novacontrol/rlhf/models.py` | Schemas: modes, algorithms, sources, feedback, ratings, rewards, integrity, rollouts, dataset versions |
| `src/novacontrol/rlhf/config.py` | `RLTrainingConfig`, `RewardPolicyConfig`, `REWARD_PROVIDERS`, `EVALUATORS` |
| `src/novacontrol/rlhf/feedback.py` | `FeedbackQualityFilter` and the named reasons |
| `src/novacontrol/rlhf/evaluators.py` | `Evaluator` interface, rule/local/external/human/none implementations, `evaluator_for` |
| `src/novacontrol/rlhf/rewards.py` | The four reward providers, normalisation, clipping, composite weighting |
| `src/novacontrol/rlhf/integrity.py` | `RewardIntegrityChecker` and the named findings |
| `src/novacontrol/rlhf/rollout.py` | `Rollout`, `RolloutRunner`, `MockEnvironment`, `ScriptedPolicy` |
| `src/novacontrol/rlhf/datasets.py` | `RewardDatasetBuilder`, `RewardDatasetRules`, reason codes, splits |
| `src/novacontrol/rlhf/resources.py` | `RLResourceEstimator` |
| `src/novacontrol/rlhf/storage.py` | The JSONL repositories (feedback, ratings, rewards, rollouts, datasets, runs, evaluations, models) |
| `src/novacontrol/rlhf/backends.py` | `PolicyOptimizer` boundary, `MockPolicyOptimizer`, `RLTrainerFactory` |
| `src/novacontrol/rlhf/evaluation.py` | `RLModelEvaluator` — the four-model measured comparison |
| `src/novacontrol/rlhf/pipeline.py` | `RLPipeline`, the ten stages, `RLPipelinePlan` |
| `src/novacontrol/rlhf/manager.py` | `RLHFManager`, the bus events, the disagreement detector wiring |
| `src/novacontrol/rlhf/runtime.py` | The runtime module (answers requests, starts nothing) |
| `src/novacontrol/rlhf/__init__.py` | 133 exports |
| `tests/test_rlhf.py` | 165 tests, 2807 lines |
| `docs/RLHF.md` | The phase's documentation |
| `docs/PHASE18_REPORT.md` | This report |

## Files modified

| Path | Change |
|------|--------|
| `src/novacontrol/application.py` | `_build_rlhf_manager`, `apply_rlhf_settings`, ~30 `rlhf_*` methods |
| `src/novacontrol/api/app.py` | 31 `/rlhf/*` routes, settings round-trip |
| `src/novacontrol/api/models.py` | 31 `ApiRoute` rows |
| `src/novacontrol/api/route_consumers.py` | 31 no-render rows |
| `src/novacontrol/cli/parser.py` | The 30-action `rlhf` subparser |
| `src/novacontrol/cli/commands.py` | `run_rlhf`, `_rlhf_action` and helpers |
| `src/novacontrol/__main__.py` | Dispatch |
| `src/novacontrol/core/config.py` | `RLHFSettings` plus `NOVACONTROL_RLHF_*` overrides |
| `src/novacontrol/settings/models.py`, `settings/manager.py` | Five `rlhf_*` user settings |
| `src/novacontrol/evaluation/models.py`, `evaluation/reward.py`, `training/models.py` | Small additive changes so the RL layer can carry provenance on existing rows |
| `tests/test_optimization.py`, `tests/test_web_api.py` | The diagnostics roster pins moved 23 → 24 and name the new row |
| `README.md`, `docs/STATUS.md`, `docs/TRAINING.md`, `AGENTS.md` | Phase 18 rows, the new link, and the phase's rules |
| `docs/DEVELOPMENT_LOG.md` | §44 (what was built) and §45 (re-verification and repairs) |
| `start.bat` | Fixed the launcher (see below) |

Nothing was removed and no existing test was weakened; the only pre-existing
tests changed are the two roster pins.

## Reward architecture

`RewardProvider` (interface: `evaluate`, `validate`, `explain`, `confidence`)
with four implementations — human, AI, rule/verifier (Phase 15's engine re-used
untouched), composite. Every result is a structured `RewardResult`: source,
confidence, evaluator identity and version, components, penalties, per-source
weights, evidence and a `reward_version`. A single versioned
`RewardPolicyConfig` holds every number (weights, feedback values, clip bounds,
integrity thresholds) — no reward weight is scattered. Normalisation keeps the
raw total and keeps a safety penalty its own component with a floor; a refusal
is not penalised. Default weights: human 1.0, verifier 0.8, rule 0.6, AI 0.4.

## Human feedback architecture

`HumanFeedback` with eight types (`accept`, `reject`, `prefer_a`, `prefer_b`,
`rating`, `correction`, `report_error`, `report_unsafe…ness; a held row can be
settled by a person with an explicit decision. There is no field for reasoning —
a correction is redacted on the way in.

## AI feedback architecture

`AIRating` with criterion scores (correctness, task completion, tool
correctness, plan validity, safety, efficiency, response quality), each with its
reason, plus overall score, confidence, evidence, evaluator identity/version and
the judged candidate. The `Evaluator` interface is replaceable (rule, local,
external, human, none) and `auto` prefers the deterministic rule evaluator
whenever a task can be verified objectively. An evaluator that cannot answer
refuses; nothing is guessed to fill the gap. Hidden-reasoning keys are discarded.

## RLHF status

Working end to end: human feedback → audited reward → reward dataset → plan /
dry-run → run → measured comparison → explicit approval. An `rlhf` dataset
refuses rows whose only support is an evaluator endorsement
(`human_feedback_required` + `mode_mismatch`).

## RLAIF status

Working end to end on the same pipeline: an evaluator rates an observable
candidate → audited reward → reward dataset → run. A `rlaif` dataset refuses
rows that also carry human feedback. Human and AI readings of the same
trajectory that disagree are recorded by `FeedbackDisagreementDetector` — with
`recommended: review`, never a winner.

## Rollout / environment status

`Rollout`, `RolloutStep`, `RolloutRunner`, `MockEnvironment` and `ScriptedPolicy`
ship and are deterministic; the environment name is recorded, and `mock` is the
only one shipped. Reward propagation through a rollout is covered by tests.

## Policy optimizer status

`mock_policy` is implemented and does not learn (`simulated: true`). `ppo` and
`grpo` are in the vocabulary and are refused with "not implemented". The
interface (`PolicyOptimizer`, `RLTrainerFactory`, `RLTrainer`) is where a real
optimizer plugs in; none is shipped, and a real run additionally needs a
deployment that permits it, an explicit confirmation and a wired runner.

## Resource governance

`RLResourceEstimator` reuses Phase 16's component walk, adds the reward dataset,
the experience buffer and — only when `kl_coefficient > 0` — the reference model
as a second copy of the weights (or an honest lower bound when the size is
unknown). It prices against the machine's real memory reading, reports
AUTO/CPU/GPU/NPU with no CUDA assumption, and an UNSAFE verdict is a refusal.
On a bare machine with the default config: `warning` / `dry_run`.

## Tests added

`tests/test_rlhf.py` — 165 tests across 14 classes: schema and vocabulary guard,
reward providers and composition, normalisation/clipping with the safety floor,
every integrity finding and status, the quality filter and redaction, all eight
feedback types, the evaluator set and `EvaluatorUnavailable`, hidden-reasoning
refusal, the disagreement detector, reward datasets (rules, reason codes, splits,
leakage, held rows, immutability, fingerprints, mixed/rlhf/rlaif modes), rollouts
and the mock environment, configuration validation and the mock/ppo/grpo
boundary, resource estimates, the ten-stage pipeline and its refusals, the
manager end to end (datasets, runs, checkpoints, pause/resume/cancel,
evaluation, registry, persistence across a restart), the bus module, the live
application (dry runs, the four-model comparison, the off switch, the
diagnostics roster, the settings round-trip), the HTTP surface over an isolated
application (12 tests), and the CLI parser and every dispatch (3 tests).

## Test results

| Gate | Command | Result |
|------|---------|--------|
| Tests (group 1) | `pytest` files 1–35 | **750 passed / 3 skipped, 641 subtests** |
| Tests (group 2) | `pytest` files 36–70 | **874 passed / 2 skipped, 761 subtests** |
| Tests (group 3) | `pytest` files 71–106 | **1329 passed / 7 skipped, 620 subtests** |
| Total | — | **2953 passed / 12 skipped** |
| API docs | `generate_api_reference.py --check` | in sync (176 routes, 31 `/rlhf/*`) |
| Types | `mypy src` | clean, 305 source files |
| Types (Windows) | `mypy src --platform win32` | clean, 305 source files |
| Ruff | `ruff check src/novacontrol/rlhf/ tests/test_rlhf.py` | clean |

Phase 18's own suite: `tests/test_rlhf.py` — **165 passed**.

## Defects found and repaired during validation

1. **A `mixed` dataset rejected every row and `rlaif` rejected human-backed
   rows.** The audit compared a row's signal mode with the dataset mode for
   literal equality, so rows carrying both signals were refused by both loops. A
   row's mode now *satisfies* the dataset's mode (either side may be `mixed`).
2. **`rl_metrics["simulated"]` was false for `mock_policy`.** It was derived
   from "is implemented", but the mock is implemented and does not learn. Now
   `dry_run or not learns`.
3. **A stored comparison could not be written.** `RLModelEvaluator.compare`
   built its `TrainingEvaluation` without an `evaluation_id`, which the
   repository refuses. It now derives the id from the run (or dataset).
4. **`GET /rlhf/feedback` defaulted to a status that matches nothing.**
   `status="pending"` is not a feedback status, so the default list silently
   returned zero rows. The default is now the empty string (every status).

## Deferred items (deliberately)

* RLVR, critique-based learning, RLCD-style training, agentic RL and game
  agents — not in this phase.
* `ppo` and `grpo` policy optimizers — named and refused; the interface is the
  seam they land on.
* The concrete Transformers/PEFT training loop — the optimizer runner boundary
  is where it lands.
* Distributed training — no second stack exists.
* Phase 19 — not started, as instructed.

## Compatibility issues

* **Diagnostics roster**: Phase 18 adds a 24th row (`RLHF / RLAIF`), so the two
  tests that pinned the roster's size now expect 24. This is the roster test
  doing its job; it is the only pre-existing test change.
* **`start.bat`**: the venv check used an unquoted path
  (`if not exist .venv\Scripts\python.exe`), so with the project under a path
  containing spaces the check ran against the wrong path and the launcher could
  proceed without a working environment; it also activated the venv but relied
  on the *system* `python` being on PATH. It now checks the quoted path, calls
  the venv's interpreter directly, accepts a port argument, and prints the exit
  code and a port hint on failure. Verified by launching it: HTTP 200.
* No other compatibility issues: no removed API, no changed default of an
  existing subsystem, and normal NovaControl operation works with every RL
  dependency absent.