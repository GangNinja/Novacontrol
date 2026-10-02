# Preference Optimization (Phase 17)

Phase 17 in one line: it turns the pairs the system has already
**observed** — what worked and what did not, what a person chose, where an
evaluation saw a real margin, where a run disagreed with a golden fixture —
into versioned preference datasets, runs DPO or ORPO against them (a dry run by
default), and lets a model improve only where a **measured behaviour comparison**
says it improved.

It is built on Phase 15 (trajectories, evaluations, feedback, the reward engine,
the golden dataset) and Phase 16 (the splitter, the run lifecycle, checkpoints,
the evaluator and the one model registry). It adds no second store, no second
registry, no second trainer interface and no reinforcement learning.

## The shape of it

```
Phase 15 record ──> PreferenceDatasetBuilder ──> PreferenceDatasetVersion ──> PreferenceTrainer
 (trajectories,      candidate_for() per          name@version, immutable,      dry_run | DPO | ORPO
  evaluations,        family; verified             group-safe splits,           (PEFT/LoRA preferred)
  feedback,           outcomes, corrections,       quality verdicts,
  benchmarks)         evaluation margins,          leak-free by construction
                      benchmark disagreement,
                      human review ──> PreferenceReviewQueue
                                            (choose_a / choose_b / tie / reject)
                                              │
                     PreferenceEvaluator ─────┘
                     base vs SFT vs candidate on held-out pairs
                     preference_accuracy + Phase 16 metrics + regression areas
                                              │
                     SFTModelRegistry (the Phase 16 one) ──> explicit approval only
```

## 17.1 Six families, one pair schema

`PreferenceDatasetType` names the six output shapes a preference pair is built
from: `nlu`, `decision`, `tool_selection`, `planning`, `recovery`, `response`.
Each family has one builder path (`candidate_for`) that returns **the
observable output of one run for that family** — an intent, a decision, a tool
call with arguments, a plan, a recovery decision, a response. A trajectory that
said nothing of that shape yields `{}` and cannot be squeezed into a family it
never produced.

`PreferenceExample` is the pair:

- `prompt` / `context` — the request the two candidates answer (never a thought);
- `chosen` / `rejected` — two structured mappings that both actually happened
  (or, for a benchmark expectation, a fixture that outranks the run);
- `chosen_outcome` / `rejected_outcome` and `chosen_evaluation` /
  `rejected_evaluation` — the recorded evidence behind each side;
- `preference_source` (`PreferenceSource`), `confidence`, `strength`,
  `source_trajectory_ids`, `source_evaluation_ids`, `difficulty`, `tags`;
- `quality` (the filter's verdict and named reasons) and `review` (a person's
  decision, when one was given);
- `dataset_version` (the version a pair was built into).

`PreferenceStrength` keeps three axes apart on purpose — `confidence` (how sure
the extractor is), `evidence_quality` (how strong the evidence itself is) and
`verification_strength` (whether the preferred side has an OBSERVED, verified
outcome) — with `overall` a documented weighted mean
(`0.4 / 0.35 / 0.25`) kept only for ranking and reporting. Each pair also
carries a `PreferenceEvidence` list, so "who decided, and was it observed?" is
stored with the pair rather than re-derived.

`fingerprint()` is a **content hash** of the two candidates (order-insensitive
in the two sides, ids and timestamps excluded), which is what makes a rebuild of
the same data recognisable and a different one refusable.

## 17.2 Where a pair may come from

`PreferenceDatasetBuilder` builds pairs from what was recorded, never from
prose:

- **Verified outcomes** — one run succeeded with verification, the other did
  not. Two successes are not a preference; a run that never recovered is a
  rejected behaviour for the `recovery` family.
- **Explicit feedback** — a person labelled one candidate accepted and another
  rejected. A structured `corrected_output` (a mapping, never prose) becomes
  the chosen side and the run's own output the rejected side.
- **Evaluation margins** — where the evaluation scores differ beyond
  `min_score_margin`, the higher-scoring side wins; a narrow margin produces
  nothing.
- **Benchmark expectations** — a golden fixture's expectation outranks the run
  **where the run disagreed with it**. The fixture is a partial statement, so
  agreement is judged on the fields the fixture actually declares: a run that
  did exactly what was expected has no pair in it.
- **Human review** — a pair a person submits or settles. That is the strongest
  source there is, and it is recorded as evidence.
- **Teacher / synthetic** — allowed, marked as such, and never treated as
  observed: `PreferenceSource.verified` is False for both.

Duplicate orientations are counted and dropped; the same two candidates the
other way round are a **contradiction** and neither orientation trains. A row
carrying hidden chain-of-thought is refused **before a pair exists** — a
`chain_of_thought`/`reasoning` key anywhere in the record is counted as
`hidden_reasoning`, not trimmed.

## 17.3 Quality: ACCEPTED / REJECTED / NEEDS_REVIEW

`PreferenceQualityFilter` classifies every pair with named, structured reasons
(`PreferenceRuleCode`) and records what it checked. One severity table decides:
an `ERROR` (identical candidates, a candidate that contradicts its outcome,
residual sensitive text under the default policy) **rejects**; a `WARN`
(weak evidence, low confidence, unverified source, missing provenance) **holds
for review** — unless a person has already answered that question by settling
the pair. A pair held for review is stored and counted, and never becomes
training data while it waits.

Pairs are redacted on the way in (both candidates and their outcomes), so a
secret in the rejected side cannot survive because only the chosen side was
read. Under the default `residual_sensitive_rejects=True`, any value the
redactor had to remove rejects the pair outright.

## 17.4 Versions, splits and leakage

`PreferenceDatasetVersion` is `name@version` (monotonic:
`1.0.0 → 1.0.1`), immutable, and carries the pairs, the splits, the rules that
selected them, the statistics, the sources, and a preprocessing stamp
(`phase17.1`). `validate()` returns **every** reason a version must not be
trained on: unknown family, a pair in two splits or none, duplicate ids, a type
mismatch, identical candidates, hidden reasoning, missing evidence or
provenance, group leakage, an accepted pair missing from the splits.

Splits are Phase 16's splitter, applied to pairs: each pair is projected to the
supervised shape for the walk (one implementation of "group-safe and
deterministic" in this codebase), and the ids are translated back. The default
is 80/10/10 with `seed=42` and `group_by="task"`; a whole group goes to one
split. `split(name)` returns only ACCEPTED pairs by default —
`accepted_only=False` is for a report that wants to show what is waiting.

## 17.5 `PreferenceTrainingConfig`

One validated place for every trainer parameter, exactly like Phase 16's
`TrainingConfig` (which it extends and can be seeded from):

- `algorithm` — `dpo` or `orpo`, and nothing else. The vocabulary contains no
  RLHF/RLAIF/RLVR, PPO, GRPO or critique learning.
- `beta` — DPO's KL-penalty strength, in `[0.01, 1.0]` (default `0.1`);
  out-of-range is an error, not a clamp.
- `reference_model` — DPO keeps one; ORPO keeps none. The configuration says so
  (`needs_reference_model`), the resource estimate counts it, and a validator
  warning notes a reference model named for an algorithm that does not use one.
- `dry_run` defaults to **True**: nothing starts training because a
  configuration exists. A real (non-dry-run) run is a warning in `validate()`
  and needs the deployment's permission plus an explicit confirm.
- `use_lora`/`training_method` — PEFT/LoRA is the preferred method, with
  `qlora` asking for the quantisation extra.

`PreferenceTrainingConfig` projects onto `TrainingConfig`
(`as_training_config()`), so the inherited lifecycle, validation, checkpoint
manager and evaluator read the fields they always did.

## 17.6 Backends — one pipeline, two objectives

`PreferenceTrainer` extends the Phase 16 trainer interface. `DPOTrainer` and
`ORPOTrainer` share **every** step of the pipeline; only the objective (and
therefore whether a reference model is counted) differs, which is what keeps two
algorithms from becoming two training stacks. `objective()` documents what each
optimises, its beta, whether a reference model is needed and that a dry run is
simulated.

- `DryRunPreferenceTrainer` walks a real step schedule and labels every figure
  `simulated: true` — a dry run must never produce a number anybody could
  mistake for a measurement.
- A real backend with the optional stack installed but no runner wired says
  exactly that; a backend without the stack names the missing packages and
  points at dry-run, dataset validation and resource estimation, which work
  without them.
- A supervised dataset handed to a preference backend is refused by name: a
  preference objective needs the pair, and quietly treating one side as a target
  would produce a supervised run wearing a preference algorithm's name.

## 17.7 Resources — the reference model is counted

`PreferenceResourceEstimator` reuses Phase 16's walk and adds what this phase
costs: `preference_pairs` prices the pairs at twice a supervised example's bytes
per token (both sides are carried), and for DPO a **second copy of the weights**
is counted as `reference_model` (or the estimate says plainly that the model's
size is unknown, so the figure is a lower bound). ORPO counts neither.

Hardware policy is AUTO / CPU / GPU / NPU with no CUDA assumption anywhere: a
CPU-only machine is a normal machine here, and every estimate still ends in
SAFE / WARNING / UNSAFE with its reasons — and an UNSAFE verdict is a refusal,
not a warning to click past.

## 17.8 Runs and checkpoints

A preference run **is** a Phase 16 `TrainingRun`: same lifecycle and statuses,
same `CheckpointManager`, same run store. It adds `algorithm` and
`preference_metrics` (the objective, and for a dry run the simulated margin,
explicitly labelled), and its stored configuration carries both phases' fields
so the inherited lifecycle can read it back while the preference fields survive
a restart. `create_run` validates, prices and stores a CREATED run; `start`
runs it in a worker thread at the application layer; pause, resume and cancel
are Phase 16's, unchanged. A run resumed from a Phase 16 checkpoint is the same
mechanism — one checkpoint manager, two objectives.

## 17.9 Evaluation — and why a preference loss is not one

`PreferenceEvaluator` compares **base vs SFT vs preference-optimized** on the
same held-out pairs:

- `preference_accuracy` — the share of pairs where the model's answer sits
  closer to the chosen output than to the rejected one; a model that answers
  nothing scores zero, not a half.
- `chosen_match_rate` / `rejected_match_rate` / `preference_gap` — the two sides
  of that, so a reader can see whether a candidate prefers the right thing or
  merely distances itself from the wrong one.
- Phase 16's per-metric comparison over the supervised projection, with
  `LOWER_IS_BETTER` latency/memory, its noise floors, and the same nine
  regression areas — the blocking ones (task success, verification, safety,
  structured output) can stop an approval on their own. A preference gain does
  **not** excuse a required capability regressing.

No loss figure is consulted anywhere; every comparison records
`loss_consulted: false` and `verdict_source: "measured behaviour on the held-out
pairs"`. Without two predictors there is nothing honest to measure: the run is
recorded as `skipped` and approval stays impossible. A comparison requested over
HTTP with payloads that are not predictors is reported as
`inconclusive` — nothing was measured — never as a pass.

## 17.10 The registry — the same one, explicit promotion only

Preference models are Phase 16 `TrainingModelRecord`s in the **same**
`SFTModelRegistry`: `experimental → evaluating → approved → production →
deprecated`, with the base model and the adapter as separate fields, an
`algorithm` that says which objective produced the adapter, `approve` requiring
a recorded passing evaluation (never a preference loss), promotion demoting the
previous production model and keeping a rollback point, and a regression
auto-rejecting the candidate. `GET /preference/models` is a VIEW over that
registry, filtered to `dpo`/`orpo` — not a second registry.

## 17.11 Human review

`PreferenceReviewQueue` is the minimal interface a person works through:

- `submit` records a preference a person is asserting — built exactly like an
  extracted pair (same sanitising, same quality pass, same evidence), so it
  cannot arrive with fewer checks than a derived one;
- `enqueue` puts a pair in front of a person; a pair carrying hidden reasoning
  is refused rather than queued;
- `decide` answers `choose_a` / `choose_b` / `tie` / `reject`: choosing B
  **swaps** the pair's sides rather than forking it, a tie settles it as
  rejected for every later build, and both `tie` and `reject` require a reason
  (that reason is what makes the refusal auditable);
- `stats` reports what is waiting, what has been decided and who decided it;
- only **settled** pairs are ever pulled into a later build (`resolved_pairs`).

A reviewer's decision is stored as human-review evidence on the pair itself, so
the resulting preference is as well-sourced as any other and the reviewer's name
travels with it.

## 17.12 API, CLI, settings, events and diagnostics

- **HTTP** — 29 `/preference/*` routes: status, summary, algorithms, estimate,
  dry-run, datasets (build/list/validate/one/pair), reviews
  (queue/one/submit/decide), runs (create/list/one/checkpoints/start/pause/
  cancel/resume/re-estimate/evaluate), compare, evaluations and the model view.
  A real run is never started by a read, and a refusal is its own status, not a
  200 with `ok=false`. The minimal review interface is API-first: the queue and
  the decision are ordinary routes.
- **CLI** — `novacontrol preference` with 26 actions shaped like `training`:
  `status`, `summary`, `algorithms`, `datasets`, `dataset`, `build`, `validate`,
  `pair`, `estimate`, `dry-run`, `create`, `runs`, `run`, `checkpoints`,
  `start`, `pause`, `resume`, `cancel`, `evaluate`, `evaluations`, `reviews`,
  `review`, `submit`, `decide`, `models`, `model`. Every action dispatches to an
  application method, so the CLI can do nothing the API could not, and a
  refusal exits non-zero.
- **Settings** — a `preference:` config section (the same shape as `training:`,
  separate because an installation may answer the two differently), five user
  settings (`preference_enabled`, `preference_dry_run`,
  `preference_max_checkpoints`, `preference_retention_days`,
  `preference_max_records`) and `NOVACONTROL_PREFERENCE_*` environment
  overrides. `dry_run` is the OR of the two switches — the direction a safety
  switch fails in.
- **Events** — `preference.dataset_built`, `preference.review_queued`,
  `preference.review_decided`, `preference.dry_run`,
  `preference.comparison_completed`, published on the existing bus through the
  failure-isolated publisher. The runtime module answers status, datasets,
  reviews, algorithms and estimate requests, and starts nothing there.
- **Diagnostics** — a 23rd roster row, `Preference optimization`, DEGRADED
  without the training libraries (the dry run still works) and SKIPPED when the
  subsystem is switched off.

## 17.13 Safety, privacy and what is absent

- **A pair is never invented.** Both sides must be observable behaviour (or a
  named fixture that outranks it), and a pair whose orientation the record does
  not support is not produced at all.
- **Nothing trains by itself.** `dry_run` defaults to true, `create_run` and
  `submit` have no model side effects, and a real run needs both the deployment
  and a confirmation.
- **No hidden chain-of-thought, ever**, in the dataset or in the review queue.
- **A model is never approved from a preference loss** — only from measured
  behaviour on held-out pairs with the regression areas checked, through the one
  registry, one explicit step at a time.
- **Not implemented**: RLHF/RLAIF, RLVR, critique learning, agentic RL,
  distributed training, and the concrete Transformers/PEFT loop (the adapter
  boundary is where it lands). Tests use mocks, synthetic rows and dry runs —
  no model is downloaded and no GPU is required.

## Tests

`tests/test_preference.py` runs entirely on deterministic fixtures, mocks and
dry-run mode. It covers the schema and the fingerprint, the vocabulary guard
(no reinforcement-learning names), configuration validation, the quality
filter and redaction, all six builders, every skip reason, benchmark agreement,
evaluation margins, hidden-reasoning refusal, duplicates and contradictions,
splits and leakage, versioning and immutability, the review queue and every
decision (including the refusals), the DPO/ORPO objectives, the dry-run
schedule, the missing-dependency and no-runner boundaries, an injected runner,
resource estimates (reference model counted for DPO, not for ORPO, CPU-only
machines), the evaluator (pass/regress/inconclusive, silent models, SFT reading,
blocking regression areas), the manager end to end (datasets, runs,
checkpoints, reviews, evaluation, approval/promotion/rollback, persistence
across a restart), the bus module, the live application (including the
off switch and the diagnostics row), the HTTP surface, the settings (config
section, environment, user settings) and the CLI.

## Gates

`python -m pytest tests/ -q`, `python scripts/generate_api_reference.py --check`
(docs in sync), `python -m mypy src` and `python -m mypy src --platform win32`.
Ruff is not a gate in this repository (the E501 baseline is older than it), but
`preference/` and `tests/test_preference.py` are ruff-clean apart from that
baseline.
