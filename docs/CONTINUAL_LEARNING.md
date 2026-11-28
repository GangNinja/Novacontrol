# Continual Learning & Self-Improvement (Phase 27)

NovaControl's continual learning is the layer that turns **executed evidence** into
better later attempts. It is deliberately small: one package, one engine, and a
set of records. It does not plan, execute, verify or remember conversations — it
observes what those existing layers already did, judges the result against the
verification they supplied, and answers one question on a later request:

> What does previous evidence suggest for this task, and how strong is that
> evidence?

Everything it can change is **strategy data** (a step template, a tool order, a
retry budget). It cannot edit source code, permissions or safety rules, and a
recommendation is advisory: the existing planner, tool selection and executor
still make and run the plan.

## Where it sits

```
request/execution (existing layers)
        │  steps, verification, retries, latency, feedback
        ▼
continual.engine ──► evaluate.OutcomeEvaluator   (success/partial/failure/recovered/corrected/unverified)
        │        ──► signals.SignalExtractor      (tool, plan, correction, recovery, latency, repeat)
        │        ──► strategies.StrategyBook      (propose → validate → promote → rollback)
        │        ──► retrieval.ExperienceRetriever (lexical relevance + hard context gate)
        ▼
store.JsonContinualLearningStore ──► persistence.JsonStateStore (namespace `continual_learning`)
```

Integration is intentionally one-directional and optional:

| Existing component | How Phase 27 uses it |
| --- | --- |
| `NovaControlApplication` | captures one evidence record after a chat response is shaped, after every executed device workflow (desktop/browser/phone), after an agentic task run (from `AgentTaskState`'s per-step/final verifier results), and after a game launch only when the launcher verifies a new process and window; appends an advisory `continual_learning` block only when a suggestion exists |
| `EventBus` | publishes `learning.experience_recorded`, `learning.strategy_proposed`, `learning.strategy_validated`, `learning.strategy_promoted`, `learning.strategy_feedback`, `learning.strategy_rolled_back` |
| `JsonStateStore` | persists the bounded corpus; a failed read/write degrades to empty/unsaved rather than raising |
| planner / executor / tool registry | untouched; a strategy's steps are data the planner may consult, and targets are always bound by the caller (`adapt_targets`) |
| reliability / permission layers | untouched; nothing here executes, approves or elevates anything |

There is no second decision engine, planner, memory system or task executor. No
model is called on any path (retrieval is lexical and deterministic).

## Evidence and outcomes

`ExecutionEvidence` records the original goal, kind, route/intent, context
(device/app), ordered steps with status and attempts, tools, errors, recoveries,
verification, user feedback, latency, retries and correlation id. Its outcome is
**not** a field a caller may set: `OutcomeEvaluator.apply` fills it from the
evidence, and `assessed` says whether that happened.

| Outcome | Decided when |
| --- | --- |
| `success` | verification passed, or an independent `verify`/`check` step confirmed the result |
| `partial` | some steps completed but nothing independent confirmed the whole task, or verification passed beside a failed step without a retry |
| `failure` | verification failed, or no step completed |
| `recovered` | a step failed and the run still ended verified after retries/recoveries |
| `user_corrected` | the person said the result was wrong, regardless of any verifier |
| `unverified` | work happened with no verification evidence at all |

Two rules are load-bearing:

- **A step confirming its own write is not verification.** Only a step whose job
  is checking (`verify`/`check`/`validation`) or an explicit verification mapping
  can move a run to `success`.
- **A note is not a correction.** A positive verdict with an explanation stays a
  success; only a negative verdict reclassifies the run.

## Learning from repeats

`ExperienceRetriever` scores previous executions lexically (identical normalized
goal, or Jaccard token overlap) with a same-task-kind bonus. Device and app are a
**hard compatibility gate**, not a weight: a phone execution never informs a
desktop task, even with identical words. Below the relevance floor, nothing is
returned — evidence is excluded rather than padded.

`engine.recommend(goal, task_kind, context)` returns a `Recommendation` with
`source`:

- `strategy` — only a validated, ACTIVE version, and only after its verified
  successes clear the policy floor;
- `evidence` — a single verified execution with a near-identical goal, at lower
  confidence, when no strategy is active yet;
- `none` — no comparable evidence, or none verified. Callers must treat this as
  "no suggestion".

Every non-empty recommendation carries its `evidence_ids`, confidence and a
human-readable reason. `adapt_targets` is always true for step templates: the
template's literal target is an example, and the caller binds the current task's.
Device plans (`/desktop/plan`, `/browser/plan`, `/phone/plan`) carry the
suggestion under `plan["continual"]` when it is compatible; the planned workflow
itself is never rewritten, because the existing planner still owns the plan.

## Strategy lifecycle

```
propose (≥ min_support distinct verified successes, same structural shape)
   → validate (executed outcomes: successes vs partial/failure, promotion floor)
   → promote (next version ACTIVE; previous version SUPERSEDED, retained)
   → rollback (previous version ACTIVE; new version ROLLED_BACK)
```

The structural shape is `(step kind, tool)`, so a tool improvement is not a
different plan. A contradicting failure blocks validation; unverified trials can
never support it. Stats are **recomputed from evidence**, never accumulated:
deleting an execution updates the version's success rate. Up to four versions per
strategy are kept (the active version and its parent always survive trimming).
An automatic regression rollback fires on two corrections or a decided success
rate below the floor over at least four decided trials.

Proposals live in their own table and are never mixed with validated versions.
Promotion is refused unless the proposal is `VALIDATED`.

## Persistence, retention and safety

- One JSON namespace, `continual_learning`, through the existing
  `JsonStateStore` (atomic writes, tolerant reads).
- Every row is parsed by its model's `from_dict`; unreadable rows are **skipped
  and counted** in the restore report, never guessed at. A payload from another
  schema is refused as a whole (`skipped.schema = 1`).
- Retention: at most `max_experiences` records (default 500) and
  `retention_days` (default 180). Pruning always keeps evidence cited as strategy
  provenance.
- Duplicate experience ids are rejected; repeated executions of the same logical
  task remain distinct records with the same fingerprint.
- All engine operations take one re-entrant lock; saves are debounced and forced
  on explicit changes and shutdown.
- Capture and recommendation catch their own exceptions and report them in
  `status()["errors"]`. The application calls capture **after** shaping the
  response, so a learning defect costs learning, never the request.
- Boundaries are part of the read surface:
  `edits_source_code=false`, `changes_permissions=false`,
  `weakens_safety_controls=false`, `promotes_unverified_evidence=false`,
  `requires_execution_evidence=true`, `advisory_only=true`.

## Configuration

`NovaControlConfig.continual` (`ContinualLearningSettings`):

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | true | engine answers reads; false disables capture and recommendations |
| `persist` | true | write the corpus through the state store |
| `capture_requests` | true | live switch for recording request, execution, agentic and verified game-launch evidence |
| `auto_learn` | true | propose from verified successes without being asked |
| `auto_promote` | true | validate + promote a proposal automatically |
| `auto_rollback` | true | restore the previous version on a regression |
| `max_experiences` | 500 | hard corpus cap (minimum 16) |
| `min_support` | 2 | distinct verified successes a proposal/validation needs |
| `min_confidence` | 0.6 | floor for offering a strategy/tool signal |
| `retention_days` | 180 | age-based pruning |
| `benchmark_tasks` | 6 | default benchmark size (clamped 2–12) |

Environment overrides: `NOVACONTROL_CONTINUAL_ENABLED`, `_PERSIST`, `_CAPTURE`,
`_MAX_EXPERIENCES`, `_MIN_SUPPORT`.

## API

All routes are authenticated and appear in `docs/API.md`.

| Route | Purpose |
| --- | --- |
| `GET /continual/status` | counts, outcomes, active versions, policy, boundaries, restore report |
| `GET /continual/overview` | capability table, honest limits, live policy |
| `GET /continual/experiences` | recorded executions, newest first, filter by kind/outcome |
| `GET /continual/experiences/{id}` | one record with steps, verification and outcome reasons |
| `DELETE /continual/experiences/{id}` | forget one execution; signals and stats recomputed |
| `POST /continual/feedback` | positive/negative verdict on one execution (422 on a bad verdict) |
| `GET /continual/signals` | derived statements with support and provenance |
| `GET /continual/strategies` | every version, status, provenance and measured stats |
| `GET /continual/proposals` | proposals, separate from validated/promoted work |
| `POST /continual/proposals` | propose from distinct verified successes (never invents one) |
| `POST /continual/proposals/{id}/validate` | validate against executed evidence (409 if already promoted) |
| `POST /continual/proposals/{id}/promote` | promote a validated proposal (409 otherwise) |
| `POST /continual/strategies/{id}/rollback` | restore the previous version (409 when none) |
| `POST /continual/recommend` | advisory suggestion with evidence ids and confidence (422 on blank goal) |
| `POST /continual/benchmark` | isolated baseline → learn → compare run |

The benchmark route builds a **separate** engine over an in-memory store: a
measurement never becomes evidence for the strategies it measures.

## Benchmark methodology and measured results

`continual.benchmark.LearningBenchmark` runs real filesystem work in a temporary
directory through a declared tool table (`legacy_write`, `write_text`,
`read_text`, `list_dir`). It is synthetic by design and says so in
`report["method"]`; it does not claim to measure the whole product.

1. **Baseline** — the generic path uses a writer with a deterministic
   first-attempt failure and supplies no verification. It is recorded by the real
   engine (so it is `partial`: completed, not verified).
2. **Learning** — one baseline run is corrected by the person (`negative`), and
   two verified executions (`write_text` + read-back `verify`) establish the
   template. The harness writes no strategy itself.
3. **Learned** — new tasks use the engine's recommendation; the executor still
   runs every step and the read-back still verifies the file.
4. **Regression** — an unrelated `filesystem.inventory` family runs before and
   after; its completion rate must not move.

Measured on this machine (Windows, CPython 3.13, four-task family, two
independent runs; medians are wall-clock per task):

| Metric | Baseline | Learned |
| --- | --- | --- |
| Verified completion | 0 / 4 (0.0) | 4 / 4 (1.0) |
| Retries | 4 | 0 |
| Median latency | 0.685 ms / 0.702 ms | 2.391 ms / 3.193 ms |
| Unrelated family completion | 1.0 | 1.0 |

The deltas were identical across runs: `verified_completion_delta = 1.0`,
`retries_delta = -4`, `unrelated_completion 1.0 → 1.0`. Latency **increased** by
about 1.7–2.5 ms per task, and the report states that rather than claiming a
speedup: the learned path performs an extra read-back that the baseline never
did, which is exactly why its completion is verified instead of assumed.

## Known limits

- Evidence is only as good as the verification the executing layer supplies. A
  chat answer with no steps is honestly `unverified`; it cannot support promotion
  until a person confirms it.
- Retrieval is lexical: a paraphrase with no shared content tokens is not
  recognised as the same task.
- A strategy is a plan template, not code. It cannot add a capability the build
  does not already have.
- Agentic task runs are captured from `AgentTaskState`: a run whose actions
  returned but whose final verifier did not pass is PARTIAL, not SUCCESS.
- A game launch is captured only after the launcher confirms both a new process
  and its window. An unverified launch is not evidence and is not recorded as a
  completed execution.
- Research (`/explore`) remains uncaptured: a report has no executed workflow or
  action evidence, and inventing steps would make the corpus claim work that
  never ran.
- No real-world side effect can be verified beyond the recorded verifier.
- The benchmark family is synthetic and small; it demonstrates the loop, not a
  product-wide percentage.

## Tests

`tests/test_continual_learning.py` (50 tests) covers outcome classification
(including the self-confirming step and the positive-note regression), duplicate
recording, fingerprints, retrieval compatibility, signals and their removal,
proposal/validation/promotion/rollback, statistics recomputation, persistence
across a restore and across `JsonStateStore` on disk, corrupt rows, foreign
schemas, retention pruning, concurrent recording, the measured benchmark, every
HTTP route's success/404/409/422 path, and benchmark isolation from the live
corpus.
