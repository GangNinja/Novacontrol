# Supervised Fine-Tuning (Phase 16)

Phase 16 turns the Phase 15 record of *what NovaControl did* into supervised
fine-tuning datasets, runs a trainer against them, checkpoints as it goes,
evaluates the result against the base model, and registers the candidate as an
`EXPERIMENTAL` model that only an explicit, evidence-backed approval can promote.

> **Phase 16 implements no preference optimisation and no reinforcement
> learning.** There is no DPO, no ORPO, no RLHF/RLAIF, no RLVR and no agentic RL
> anywhere in this phase, and nothing here is a step toward *training* on a
> preference signal. It is supervised fine-tuning infrastructure — a dataset
> builder, a trainer interface, a resource estimator, checkpoints, an evaluation
> gate and a model registry — and the last section says exactly how a later
> phase would consume it. (Phase 17 did: see
> [docs/PREFERENCE.md](PREFERENCE.md) for the DPO/ORPO preferences built on this
> interface. Phase 18 did too: [docs/RLHF.md](RLHF.md) for the RLHF/RLAIF
> subsystem built on the same datasets, trainer boundary, resource verdict,
> evaluation gate and registry.)

Three more things it deliberately does not do:

* **It trains nothing automatically.** `TrainingConfig.dry_run` defaults to
  `True`. Creating a run has no side effects at all (no process, no checkpoint,
  no model); *starting* one in real mode needs the deployment to permit it **and**
  an explicit `confirm=True`.
* **It requires no CUDA device and no training library.** The default backend is
  a dry-run trainer that walks a real step schedule and writes real checkpoint
  files. Nothing in `training/` imports `torch`, `transformers` or `peft` at
  module level, so the package installs and the tests pass on the target machine
  (16 GB RAM, Intel iGPU + NPU).
* **It never trains on hidden chain-of-thought.** A training example is a
  structured input and a structured target. A source row or an example carrying a
  reasoning key is refused and counted, not trimmed.

It changes no existing behaviour: the subsystem is optional (one switch), every
entry point returns a structured result instead of raising, and the bus module is
read-only.

## The shape of it

```
Phase 15 rows (trajectories + evaluations)
    → SFTDatasetBuilder      accepted rows → versioned, split, redacted examples
    → SplitConfig            deterministic, group-safe train / validation / test
    → ResourceEstimator      what this configuration would cost on THIS machine
    → TrainingConfig         the parameters a trainer obeys (validated once)
    → SFTTrainer             dry-run backend, or the PEFT/LoRA adapter boundary
    → CheckpointManager      atomic checkpoints, validation, resume, retention
    → TrainingEvaluator      base vs candidate, on the held-out split
    → SFTModelRegistry       experimental → evaluating → approved → production
    → TrainingRun            the lifecycle those steps move a run through
```

| Module | What it is |
| --- | --- |
| `training/models.py` | The schema: dataset types, the example, splits, statistics, the run, the estimate, checkpoints, the model record, the evaluation, statuses and transitions |
| `training/config.py` | `TrainingConfig` + `TrainingConfigValidation` — every trainer parameter, in one validated place |
| `training/hardware.py` | `detect_hardware` → `HardwareCapabilities`, and `resolve_backend` for a policy |
| `training/resources.py` | `ResourceEstimator` → `ResourceEstimate` (SAFE / WARNING / UNSAFE) |
| `training/datasets.py` | `SFTDatasetBuilder` — seven builders, the refusal vocabulary, splitting, validation |
| `training/backends.py` | `SFTTrainer`, `DryRunTrainer`, `PeftLoraBackend`, `TrainingCallbacks` |
| `training/checkpoints.py` | `CheckpointManager` — write, validate, load, resume point, retention |
| `training/evaluation.py` | `TrainingEvaluator` — base vs candidate, regressions, noise floors |
| `training/registry.py` | `SFTModelRegistry` — the lifecycle, and the gates between statuses |
| `training/storage.py` | JSONL repositories (the audit trail's own pattern); replaceable |
| `training/manager.py` | `TrainingManager` — the order everything runs in, failure-isolated |
| `training/runtime.py` | `TrainingModule` — the read-only bus seam |
| `training/__init__.py` | The public surface |

## 16.1 Seven dataset types, one target schema each

A dataset names exactly **one** family, so its examples share one target shape.
The builder does not squeeze every trajectory into one format:

| `DatasetType` | Input | Target |
| --- | --- | --- |
| `nlu` | request (+ context summary) | `StructuredIntent` — intent, confidence, entities, strategy |
| `decision` | intent (+ capabilities) | decision — route, decision_type, capability, model |
| `tool_selection` | task, intent, available tools | tool + arguments + capability + status |
| `planning` | goal (+ capabilities) | plan — ordered steps with actions and dependencies |
| `recovery` | failed step, error, observations | strategy, outcome, attempts |
| `developer` | task (+ repository context) | actions taken and their result |
| `research` | request (+ available evidence) | result and the evidence used |

`SFTTrainingExample` is the record they all produce:

| Field | Meaning |
| --- | --- |
| `example_id`, `dataset_type`, `schema_version` | identity |
| `input`, `context` | what the model sees — mappings, never prose |
| `target` | what it should produce — a mapping, never prose |
| `metadata` | provenance and, deliberately, an open place for an importer's own fields |
| `source_trajectory_id`, `source_evaluation_id` | which Phase 15 rows it came from |
| `quality_status`, `difficulty`, `tags` | the verdict it was accepted under, how hard it is |
| `created_at`, `dataset_version` | when, and which version it landed in |
| `group_key` (property) | the unit that must never be split across train and test |
| `fingerprint()` (method) | a stable identity, so the same example cannot enter twice |
| `reasoning_violations()` | any hidden-reasoning key this example carries |

An example is extensible without a schema change: `metadata` is free-form and
`to_dict()`/`from_dict()` round-trips it exactly (pinned by a test), so an
importer can attach its own fields and `schema_version` says which shape it
followed.

## 16.2 What may become an example — `SelectionRules`

Eligibility is the operator's, not a constant. `SelectionRules` carries the lines:
which quality verdicts are acceptable (default: `accepted` only), whether success
is required, whether verification must exist and pass, a minimum reward or
evaluation score (optionally on one dimension), a model, task category, source,
tags, a date window, a row cap, and whether developer and research rows are in
scope.

Every refusal has a name, and the counts are kept on the dataset so a version
explains itself rather than just being small:

| Reason | Meaning |
| --- | --- |
| `quality_not_accepted` | the Phase 15 quality filter did not accept the row |
| `not_successful` | the run did not succeed |
| `verification_failed` / `verification_missing` | verification failed, or was required and absent |
| `below_min_reward` / `below_min_score` | under the configured floor |
| `model_filter` / `task_category_filter` / `source_filter` / `tag_filter` / `date_filter` | did not match the selection |
| `developer_excluded` / `research_excluded` | the type is out of scope for this dataset |
| `missing_structured_data` | the row has nothing of the shape this dataset type needs |
| `duplicate` | the same example (by fingerprint) is already in |
| `residual_sensitive_data` | redaction found something it had to remove |
| `hidden_reasoning` | the source row carries a chain-of-thought key |
| `malformed_example` | not serialisable as JSON |
| `no_examples_for_type` | nothing matched at all |

A refusal is recorded, never repaired silently. Secrets never reach an example:
a row whose request, result or notes contain a credential is rejected by the
Phase 15 quality gate, and anything that survives that is passed through the
audit trail's own `Redactor` on the way in.

## 16.3 Splits and versioning

`SplitConfig` defaults to **80 / 10 / 10** with `seed=42` and
`group_by="task"`: whole *groups* are assigned to one split, so a model cannot be
tested on a task whose other half it trained on. The group key is written at build
time (the task, or a session named in metadata) and falls back to the source
trajectory, then to the example itself, so an imported example is never split by
accident.

The assignment is a deterministic walk: groups are ordered by a hash of
`seed:group`, and each group goes to the split whose **relative** shortfall
against its own target is largest. Relative, not absolute — with absolute room a
80/10/10 split hands the first groups to `train` because its target is simply the
largest number, and four groups of ten become 30/10/0, leaving the test split the
evaluator reads by default empty. Measured relatively, the same four groups
become 20/10/10. Ties favour the small splits, so a handful of groups does not
starve validation or test.

Versions are monotonic and immutable: `next_version` bumps `1.0.0 → 1.0.1 →
1.0.2` for a repeated name, an id is `name@version`, and a stored version is
written once. `SFTDatasetBuilder.validate(dataset)` returns every reason the
dataset must not be trained on — an id that disagrees with `name@version`,
duplicates, an example whose type differs from the dataset's, a missing
structured target, a group crossing two splits, a reasoning key, statistics that
disagree with the rows — and returns an empty tuple only when it is clean.

`source_data_version()` records what the version was built from (trajectory
schema, row counts, the dataset type) and `preprocessing_version` records the
builder's own version (`phase16.1`), so a dataset can always be explained.

## 16.4 `TrainingConfig`

Every parameter a trainer obeys lives in one frozen dataclass: base model,
dataset, epochs, batch size, gradient accumulation, learning rate, warmup,
sequence length, evaluation and checkpoint frequency, seed, precision,
gradient checkpointing, checkpoint cap, resume point, LoRA rank/alpha/dropout and
target modules, hardware policy, dry run, and the training method.

`validate()` returns errors and warnings, and **an out-of-range value is an error,
not a clamp** — silently changing what someone asked for is worse than saying no.
A typo that cannot be read at all keeps the default instead (parsing is not
validation), so `epochs: "many"` becomes 1 rather than 0. `fp32` and `lora` are
the defaults; `training_method=full` with LoRA on is a *warning* (the LoRA
settings will be ignored), and `use_lora=false` with a LoRA method is an error
that names the fix.

The deployment's own defaults are merged in exactly one place,
`TrainingManager.resolve_config`: the config section's policy, plus the operator's
`training.defaults`, plus whatever the caller named. `None` means "not stated"
rather than "unset", so an unstated field does not clobber a real default.

## 16.5 Hardware, dependencies and the resource estimate

`detect_hardware()` reads this machine: total and available RAM (from the
application's monitor when given, otherwise psutil, otherwise unmeasured — a
figure is `None`, never a guess), disk space, the CPU count, and the optional
training dependencies. Runtime probing (`probe_runtime=True`) asks `torch` what
it has, **lazily and defensively**: a missing `torch` is a note, not an exception,
and a real training backend is reported as unavailable with the missing package
names rather than as a broken import somewhere else.

`resolve_backend(policy, capabilities)` maps a `HardwarePolicy` onto a device:

| Policy | Result on this machine |
| --- | --- |
| `local_cpu` | the CPU — available, always |
| `local_gpu` | **unavailable**, with the reason "no CUDA device was reported" |
| `local_npu` | **unavailable**, with the reason naming that no NPU was reported |
| `auto` | the best accelerator, then the CPU, with the reason it chose |

An explicit policy that names a device this machine does not have says so instead
of quietly becoming a CPU run: "train on the GPU" must not mean "train on the CPU
for six hours".

`ResourceEstimator.estimate(config, dataset=…)` returns a `ResourceEstimate` with
the verdict, the reasons, and the component breakdown:

| Component | LoRA | Full |
| --- | --- | --- |
| `weights` | the base model's footprint | the base model's footprint |
| `adapter` | the adapter's optimizer state (a small fraction of the weights) | — |
| `gradients` | — | trainable parameters × per-parameter bytes |
| `optimizer` | — | Adam moments for every trainable parameter |
| `activations` | micro-batch × sequence length, halved when gradient checkpointing is on | same |
| `dataset` | estimated tokens × bytes per token | same |
| `checkpoints` | one adapter's worth | one model's worth |

The verdict is `SAFE` when the total fits comfortably (below the 75% comfort
line), `WARNING` when it fits but is close, when the model's size is unknown, or
when free memory could not be measured at all — an unmeasured machine never gets
a clean bill of health — and `UNSAFE` when the total exceeds what is usable.
**An `UNSAFE` estimate is a refusal**: `TrainingManager.start` returns
`ok=false, refused=true` naming the reasons and does not move the run. Only an
explicit `override=True` proceeds, and only when the deployment sets
`NOVACONTROL_TRAINING_ALLOW_UNSAFE`; either way the override is recorded in the
run's notes.

## 16.6 Backends — the interface, the dry run, and the PEFT/LoRA boundary

`SFTTrainer` is a small abstract class: `validate_config`, `prepare_dataset`
(what will be fed to a model, as a summary), `estimate_resources`, `evaluate`
(backend-local checks), `save_checkpoint`, `finalize`, `cancel`, `start_training`
and `resume_training`. The **orchestrator** drives the loop — `start_training`
runs to a terminal status and returns the final run — so a backend cannot smuggle
a second scheduler, a second store or a second lifecycle into NovaControl.
`TrainingCallbacks` is how a trainer talks back: report a step, request a
checkpoint, ask whether cancellation or a pause was requested. The manager is the
only writer of a run.

`model_metadata()` is what a backend says it **produced**, in the shape the
registry stores, and the manager passes it to `registry.register` rather than
letting the registry infer it from the configuration. That is the difference
between a registry entry a deployment can load (`adapter.path` names the
directory a real runner wrote) and one that only looks complete; a dry run says
plainly that it wrote no adapter file.

`DryRunTrainer` needs nothing installed. It computes the step schedule from the
dataset's real size, walks it, produces a documentedly *simulated* loss curve, and
writes real checkpoint files with real metadata. It never claims a trained model,
and the evaluation treats a dry-run run as un-evaluated unless predictors are
explicitly supplied.

`PeftLoraBackend` is the isolated boundary a real run plugs into. It probes for
`torch`/`transformers`/`peft` **without importing them**, adds the LoRA settings
to the prepared summary, and refuses to run when the extras are missing or no
runner is wired — with the missing package names and the way to attach a runner in
the message. The concrete Transformers/PEFT training loop is deliberately **not**
shipped in Phase 16: it would be untestable on this machine, and untested code in
a training path is worse than a clear boundary. The adapter interface,
`PeftRunner = Callable[[TrainingRun, SFTDatasetVersion, TrainingCallbacks], TrainingRun]`,
is where it lands, and the LoRA-first defaults (`lora`, rank 8, alpha 16) are what
make a 16 GB machine viable.

## 16.7 The run lifecycle and its checkpoints

`TrainingRunStatus` has nine values: `created`, `validating`, `preparing`,
`running`, `paused`, `evaluating`, `completed`, `failed`, `cancelled` — the last
three are terminal. A run moves forward through the manager; every step summary
replaces the stored row, so a reader always sees the last written state and never
a half-updated one.

The guards, in the order they apply:

* `create_run` validates the configuration and estimates the cost, and refuses a
  dataset that does not exist or has no examples. It starts nothing.
* `start` refuses a terminal run, refuses a run that must be resumed instead,
  enforces the deployment's dry-run floor, re-estimates against the machine as it
  is *now*, and refuses an `UNSAFE` estimate unless the deployment allows an
  override **and** one was asked for. A real run additionally needs `confirm=True`.
* `cancel` asks a live run to stop at its next step (recorded, kept for
  inspection) and ends a stored one immediately. `pause` is the resumable
  version of the same idea — the difference matters: a paused run keeps its
  checkpoints and `resume` continues from one.
* Failures are **data**: a backend that raises fails the run with
  `Type: message` recorded on the row, announces `training.run_failed`, and
  increments the failure counter. A backend that cannot even be *constructed*
  (broken import, no runner) fails the run without inflating the count, because
  the diagnostics row already explains that condition.

`CheckpointManager` writes atomically: a checkpoint is written to a temporary
file and renamed, so a crash mid-write leaves the previous one intact. A record
carries its run, epoch, step, kind (`periodic`/`best`/`final`), metrics, path,
size, checksum and a status of `complete`, `incomplete` or `corrupt`, and
`validate()` re-reads the file to decide — a checkpoint is only reported as
loadable if it really is. `resume_point(run)` reports the newest *validated*
checkpoint and the reason when there is none, and `apply_retention` keeps the
newest `max_checkpoints` while always keeping the best one.

## 16.8 Evaluation — and why a loss curve is not one

`TrainingEvaluator.compare(dataset, base, candidate, split="test", …)` measures
both models on the **held-out** split and returns a `TrainingEvaluation` with a
verdict of `pass`, `regress` or `inconclusive`, the per-metric deltas
(candidate − base), the regressions found, coverage, and a `reason`.

* Twelve `TRACKED_METRICS` are measured, including `average_latency_ms` and
  `average_memory_bytes`, which are in `LOWER_IS_BETTER` — a candidate that is
  faster is better, and turning that around would make every real improvement
  read as a regression.
* A metric must move by more than its **noise floor** (1 ms for latency) before
  it counts as a regression, so scheduler jitter cannot fail a good candidate.
* A predictor that raises is counted as failed, not as a wrong answer, and if
  **nothing usable was measured, the verdict is `inconclusive` — never `pass`**.
  Believing a broken measurement is how a model gets approved for being absent.

`TrainingManager.evaluate_run` refuses a run that has not completed, refuses a run
with no registered model, and, when no base/candidate predictors were supplied,
records the run as `skipped` with the reason "the training loss is not an
evaluation" and leaves approval impossible. It will not invent a candidate.

## 16.9 The model registry

`ModelStatus` has six values — `experimental`, `evaluating`, `approved`,
`production`, `deprecated`, `rejected` — and `ALLOWED_STATUS_TRANSITIONS` defines
every move the registry will accept:

| From | To |
| --- | --- |
| `experimental` | `evaluating`, `rejected`, `deprecated` |
| `evaluating` | `approved`, `rejected`, `experimental` |
| `approved` | `production`, `rejected`, `deprecated` |
| `production` | `deprecated` |
| `deprecated` | — |
| `rejected` | `experimental` (the data changed; re-evaluate) |

Promotion is explicit by construction: a completed run registers as
`EXPERIMENTAL`, and nothing about training moves it further. `approve` requires a
**recorded passing evaluation**; `promote` requires `approved` and demotes the
previous production model while recording what it replaced, so `rollback` has
something to restore. A regression recorded by `record_evaluation` rejects the
candidate automatically — a model that failed its comparison is not left sitting
in an approvable state. Every transition appends
`{from, to, reason, at}` to a bounded `history` (20 entries) on the record, which
is the answer to "why is this model in production?".

The base model and the adapter are separate fields on `TrainingModelRecord`
because they are separate things: `base_model` is what was adapted,
`adapter` carries `kind`, `rank`, `alpha`, `dropout`, `target_modules`, the path
a runner wrote, and — when the model manager knows it — whether the base model
exists. `resource_requirements` keeps the estimate the run ran under, and
`training_run_id` keeps the lineage. A LoRA adapter is never confused with a full
model copy.

## 16.10 API, CLI, settings, events and diagnostics

**API** — 25 `/training/*` routes, following the existing surface exactly: reads
are `GET`, transitions are `POST` with a reason where one matters, and the
existing authentication/error conventions apply. A refusal is an HTTP status, not
a cheerful 200 with `ok=false`.

**CLI** — `novacontrol training <action>` with 24 actions covering the whole
lifecycle (`status`, `summary`, `datasets`, `dataset`, `build`, `validate`,
`estimate`, `create`, `runs`, `run`, `checkpoints`, `start`, `pause`, `resume`,
`cancel`, `evaluate`, `evaluations`, `models`, `model`, `approve`, `promote`,
`reject`, `deprecate`, `rollback`), `--set KEY=VALUE` for config overrides (typed
the same way the config reader types them), `--type` for the dataset family,
`--confirm` for a real run and `--override` for an unsafe estimate. Every action
dispatches to the application method that owns it, so the CLI cannot start,
approve or promote anything the API could not; a refusal prints the manager's
reason and exits non-zero. Nothing in the CLI constructs a real run by itself.

**Settings** — a `training:` config section (`enabled`, `dry_run`,
`allow_unsafe`, `hardware_policy`, `max_checkpoints`, `max_records`,
`retention_days`, `defaults`), the environment overrides
`NOVACONTROL_TRAINING_{ENABLED,DRY_RUN,ALLOW_UNSAFE,HARDWARE_POLICY,MAX_CHECKPOINTS,MAX_RECORDS,RETENTION_DAYS}`,
and five user settings (`training_enabled`, `training_dry_run`,
`training_max_checkpoints`, `training_retention_days`, `training_max_records`)
applied live from `POST /settings`. `SettingsManager.update` clamps the
checkpoint cap into range on the same rule the persisted copy is read with — it
is a ceiling, not a hint. An unrecognised boolean spelling keeps the current
value rather than reading as "off", because for a switch like `dry_run` the
default is the cautious answer and a typo must not be the thing that turns it
into the permissive one.

**Events** — the manager announces `training.dataset_built`, `run_created`,
`run_started`, `run_completed`, `run_failed`, `run_cancelled`,
`model_registered` and `evaluation_completed`; announcing is failure-isolated, so
a broken subscriber cannot fail a run.

**Diagnostics** — a `Training` row (category `systems`) reports whether the
subsystem is enabled, whether the deployment is a dry run, and which backends are
available, with the missing dependencies as the remediation. When the switch is
off the row is `SKIPPED`, and every entry point refuses with "supervised
fine-tuning is switched off in this installation
(`NOVACONTROL_TRAINING_ENABLED` / `training_enabled`)".

**Application** — `start`, `resume` and `evaluate` run in a worker thread
(`asyncio.to_thread`), because a real run is minutes to hours and pause/cancel
have to stay reachable while it trains.

## 16.11 Safety, privacy and what is absent

* **No hidden chain-of-thought.** A source row carrying a reasoning key is
  refused and counted (`hidden_reasoning`) before an example is built — the
  example builder copies a fixed set of provenance fields, so a
  `chain_of_thought` in a trajectory's metadata would otherwise never reach the
  example-level guard, and the row would train with its reasoning silently
  trimmed. Both guards exist; the source-level one is what makes the refusal
  visible.
* **Redaction on the way in**, using the audit trail's own `Redactor`; anything
  it had to change counts as `residual_sensitive_data` and the example is not
  trained on.
* **Bounded everything** — run record caps, `MAX_LOSS_POINTS` per run, a bounded
  model history, checkpoint retention, and dataset row caps.
* **No automatic side effects.** Nothing in this phase changes a prompt, a
  routing rule, a threshold or a stored model. `create*` never starts anything;
  `start` is the only action that trains, and it can only do so in dry-run mode
  unless the deployment and the caller both say otherwise.
* **Absent on purpose:** the concrete Transformers/PEFT training loop, DPO/ORPO,
  RLHF/RLAIF, RLVR, agentic RL, distributed training, and any evaluation that
  approves a model on its loss curve.

## 16.12 How a later phase would consume this

The seams a preference-optimisation or RL phase would use, without changing
anything here:

1. **Datasets** — a new `DatasetType` (or a second builder) produces the pairs a
   different objective needs; the split, versioning, redaction and validation
   rules are shared, so preference data is leak-free and versioned on day one.
2. **The trainer interface** — `SFTTrainer` is where a DPO/ORPO or policy-gradient
   objective plugs in. It receives the run, the dataset and the callbacks, and it
   returns the final run; the orchestration, checkpoints, pause/cancel and
   failure isolation are already there.
3. **Rewards** — Phase 15's `RewardEngine` is already weighted, explained and
   versioned, and it deliberately does not penalise a refusal. A preference phase
   consumes it as the signal; it does not need to re-derive one.
4. **Evaluation and the registry** — `TrainingEvaluator` is objective-agnostic:
   base vs candidate, on a held-out split, with regressions and a noise floor.
   A new objective gets the same gate, and `SFTModelRegistry` keeps promotion
   explicit, so a preference-tuned candidate cannot reach `production` without a
   recorded passing evaluation either.
5. **The resource estimator** — a new method adds its own components to
   `ResourceEstimate` and inherits the SAFE/WARNING/UNSAFE refusal, which is the
   guard that stops an expensive run on a 16 GB machine.

Phase 17 consumed these seams without reopening them: `preference/` sits beside
`training/` with a family-specific `PreferenceDatasetBuilder`, `DPOTrainer` /
`ORPOTrainer` subclasses of `SFTTrainer`, the splitter and the run lifecycle
unchanged, and the same registry and the same evaluation gate. See
[docs/PREFERENCE.md](PREFERENCE.md) for what it does and, just as importantly,
what it does not do.

Phase 18 consumed the same seams a third time without reopening them: `rlhf/`
builds reward datasets from the Phase 15 record plus the feedback and ratings,
re-uses `RewardEngine` unchanged as one of its reward providers, inherits
`TrainingRun`, the checkpoints, the splitter, the resource verdict, the
evaluation gate and the registry, and ships a single mock policy optimizer so the
pipeline is provable here. [docs/RLHF.md](RLHF.md) says what it does and what it
deliberately does not: no RLVR, critique learning, RLCD-style training, agentic
RL, distributed training or concrete Transformers/PEFT loop.

What a later phase must **not** do is what this one was careful not to: it must
not start large training by itself, must not require CUDA, must not train on
hidden reasoning, and must not promote a model on anything less than a recorded
comparison.

## Tests

`tests/test_training.py` runs entirely on deterministic fixtures, mocks and
dry-run mode: no model is downloaded, no GPU is required and nothing trains for
real. It covers the schema and the reasoning guard, the configuration validator,
hardware and the resource estimate, all seven dataset builders, selection and
refusal reasons, splits and leakage, dataset validation and versioning, the
trainer interface, the dry-run backend and its schedule, the PEFT boundary
(including an injected runner), checkpoints (write, corrupt, resume, retention),
the evaluator (pass/regress/inconclusive, predictors that fail, noise floors),
the registry and every legal and illegal transition, the manager end to end, the
bus module, the live application (including that a long start does not block the
event loop), the HTTP surface, the settings (config section, environment, user
settings) and the CLI.

## Gates

`python -m pytest tests/ -q`, `python scripts/generate_api_reference.py --check`
(docs in sync), `python -m mypy src` and `python -m mypy src --platform win32`.
Ruff is not a gate in this repository (the E501 baseline is older than it), but
`training/` and its tests are ruff-clean.
