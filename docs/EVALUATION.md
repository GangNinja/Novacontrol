# Data Collection, Evaluation and Reward Foundation (Phase 15)

Phase 15 records what NovaControl **did**, scores it across nine dimensions, and
turns those scores into a weighted, fully explained reward. It is the dataset
foundation a later phase would learn from — and it is deliberately *only* that.

> **Phase 15 does not train anything.** There is no fine-tuning, no preference
> optimisation and no reinforcement learning anywhere in this phase: no SFT, no
> DPO/ORPO, no RLHF/RLAIF, no RLVR, and no agentic RL. What this phase produces
> is the evidence and the rules by which such a phase *would* be judged. Nothing
> here changes a model's weights, prompts or behaviour. (Phase 16 added
> supervised fine-tuning on this record — [docs/TRAINING.md](TRAINING.md) — and
> Phase 17 added DPO/ORPO on its verified outcomes and feedback —
> [docs/PREFERENCE.md](PREFERENCE.md).)

It also changes no existing behaviour. The recorder is an **observer** of the
lifecycle the application already publishes — the same `EventBus`, the same typed
vocabulary, the same correlation id a request already threads — and every step of
it is failure-isolated, so the layer that learns from work cannot break the work.

## The shape of it

```
User request → NLU → Context → Decision → Plan → Execution → Tool calls
             → Observations → Verification → Recovery if necessary → Final result
             → Trajectory + Evaluation + Reward → Dataset / benchmark storage
```

The pipeline above is the one NovaControl already runs. A trajectory is a
**projection** of that path, not a second execution path: nothing in
`evaluation/` decides, plans, calls a tool or answers a request.

| Module | What it is |
| --- | --- |
| `evaluation/models.py` | The schema: trajectory, sub-records, dimensions, statuses, versions |
| `evaluation/recorder.py` | `TrajectoryRecorder` — an EventBus observer that builds trajectories |
| `evaluation/quality.py` | `DataQualityFilter` — ACCEPTED / REJECTED / NEEDS_REVIEW with reasons |
| `evaluation/evaluator.py` | `EvaluationEngine` — nine dimensions, scored separately |
| `evaluation/reward.py` | `RewardEngine` + `RewardConfig` + `RewardResult` |
| `evaluation/datasets.py` | `GoldenDataset` / `GoldenExample` + the built-in set |
| `evaluation/storage.py` | JSONL repositories (trajectories, evaluations, rewards, datasets) |
| `evaluation/metrics.py` | `MetricsCalculator` — the aggregate figures |
| `evaluation/service.py` | `EvaluationService` — the order the pieces run in |
| `evaluation/runtime.py` | `EvaluationModule` — the bus seam that attaches the recorder |

## 15.1 Trajectory schema — `evaluation/models.py`

One `AgentTrajectory` per piece of work, with the fields the phase names:

| Field | Meaning |
| --- | --- |
| `trajectory_id`, `task_id`, `parent_task_id` | identity (the correlation id a request already uses) |
| `timestamp`, `schema_version`, `source` | when, which schema, which layer recorded it |
| `user_request` | the request, **redacted** on the way in |
| `context_summary`, `structured_intent`, `decision`, `plan` | what the pipeline understood and chose |
| `execution_steps`, `tool_calls` | what ran, and what came back (status, duration, error) |
| `observations`, `verification_results`, `recovery_events` | what was seen, checked, and repaired |
| `final_result`, `status`, `success`, `failure_reason` | how it ended |
| `model_information`, `latency_metrics`, `resource_usage` | measured facts — `None` when nobody measured them |
| `user_feedback`, `reward`, `quality`, `evaluation_id` | the person's word, the reward, the verdict |
| `event_count`, `redactions`, `redaction_kinds`, `metadata` | capture bookkeeping |

Rules that shaped it:

* **Structured only.** There is no field for a model's hidden reasoning, and no
  code path that could fill one. What is stored is what the layers *did*.
* **Optional means optional.** Every field has a usable default, so an existing
  execution path hands over as much or as little as it has. `success` is
  `bool | None`, and `None` means "the run ended without anything deciding
  whether it worked" — a real outcome, never stored as a failure.
* **A figure is measured or absent.** `duration_ms` and the memory readings are
  `None` when unmeasured, never a `0.0` that would read as free.
* **Versioned.** `TRAJECTORY_SCHEMA_VERSION` travels on every row, and a row
  written by a later schema still loads (unknown keys are ignored, unknown
  statuses read as in-progress).

## 15.2 The recorder — `evaluation/recorder.py`

`TrajectoryRecorder` subscribes to the events the application already publishes —
`intent.detected`, `context.resolved`, `decision.created`, `plan.created`,
`task.started`/`paused`/`resumed`/`cancelled`/`completed`/`failed`,
`tool.selected`/`started`/`completed`/`failed`, `verification.*`, `recovery.*` —
groups them by correlation id, and writes one trajectory when the task ends.

* **Lightweight and bounded.** In-flight drafts are capped (the oldest is written
  out as an unfinished capture rather than kept), and finished rows live in a
  small ring that exists so a late annotation can still find one.
* **Failure-isolated.** The handler body runs inside a catch-all; a failure is
  counted, logged, and the request moves on. The bus's own isolation is not
  relied upon — this promise belongs to the recorder.
* **Disableable.** With recording off the handler returns before it reads the
  payload; nothing is created, and switching off writes out whatever is in
  flight instead of dropping it.
* **Annotatable.** The end-to-end latency and the machine's memory readings are
  measured outside the event vocabulary, so the layer that measures them hands
  them over (`annotate(...)`) rather than having the recorder guess. An
  annotation on an already-written row re-delivers it, and the service upserts.

## 15.3 Data quality — `evaluation/quality.py`

`DataQualityFilter` classifies every row: **ACCEPTED**, **REJECTED**, or
**NEEDS_REVIEW**, with a structured reason for each finding:

```json
{"code": "failed_verification", "severity": "warn",
 "detail": "the run reports success while verification failed on s1"}
```

The checks, in order: presence, outcome (terminal? decided?), serialisability,
schema version, execution (a success claim with nothing behind it), consistency
(contradictions between the outcome and what ran), tools (malformed calls),
safety (a refusal — held as evidence; an action that ran after its confirmation
was refused — rejected), privacy (residual sensitive text), noise (too many
steps/events), and duplicates (the same request, route, tools and outcome under
a different id).

**Nothing is deleted silently.** The filter classifies; it never removes a row.
Severity decides the verdict — `ERROR` rejects, `WARN` holds for review, `INFO`
attaches a note to an accepted row — and every threshold lives in
`QualityConfig`.

## 15.4 The evaluation engine — `evaluation/evaluator.py`

Nine dimensions, scored **separately** — a single opaque number is exactly what
this phase was told not to build:

| Dimension | What it reads |
| --- | --- |
| NLU | the intent, its confidence, and whether confidence and outcome agree |
| Decision | the route, whether an escalation was warranted, whether confirmation was declared |
| Tool selection | which tool ran, whether it succeeded, repeated calls |
| Planning | step validity, resolvable dependencies, steps never attempted |
| Execution | task completion, tool success ratio, retries |
| Verification | pass ratio, **false success** (success beside a failed verification), false failure |
| Recovery | what recovered, how many attempts it took |
| Safety | refusals respected, confirmations answered, blocked unsafe actions |
| Efficiency | latency against budget, tool-call count, retries, memory delta |

Every score carries its findings and the figures behind it, and a dimension with
no evidence is `unknown` — never a guessed number. Give the engine a
`GoldenExample` and it also checks the run against what the dataset expected
(intent, route, tool, verification, outcome, plan size) and folds those checks
into the matching dimensions.

`EvaluationResult` keeps all of it: `overall_status`, `task_success`, the nine
named scores *and* the `scores` mapping, the dimension detail, `latency`,
`resource_metrics`, `issues`, `evidence`, `evaluator_version`, `timestamp`.

## 15.5 Reward — `evaluation/reward.py`

A weighted reading of observable outcomes, with its reasons attached:

```
reward = task_success + verification_success + tool_correctness
       + plan_efficiency + safety + latency + resource_efficiency
       − unnecessary_actions − failed_execution − failed_verification
       − unsafe_actions − excessive_retry
```

* **Weights are configuration**, in exactly one place (`RewardConfig`), readable
  from the config file's `evaluation:` section. No weight is hard-coded anywhere
  else, and a stored result records the version and the weights that produced it.
* **A refusal is not a penalty.** `unsafe_action` is for an action that *bypassed*
  a gate — it ran although its confirmation was refused, or with no recorded
  answer. Stopping an action is what `safety` is *for*; penalising it would teach
  a future phase to avoid the safety layer.
* **`RewardResult`, not a number.** `total_reward`, `component_rewards`,
  `penalties`, `normalized_reward`, `reward_version`, a per-factor breakdown with
  raw value + weight + contribution + reason, `explanation_summary`, and
  `evidence` references. The explanation describes observable factors only.

## 15.6 Golden dataset — `evaluation/datasets.py`

A `GoldenDataset` is a versioned set of `GoldenExample`s: a request plus the
behaviour that should follow it — expected intent, decision, tool, arguments,
plan properties, verification outcome, safety behaviour and final outcome.
Versioning is part of the shape (`version`, `created_at`, schema version), and
the store keeps every version so an old measurement still resolves.

The built-in set (`novacontrol-core` 1.0.0) is small and deterministic by
design: six examples covering the system-tools route, a language-only answer, a
destructive request that must be refused, a taught fact, a scheduled task and a
direct filesystem read. Every expectation names something this installation
really has (an `IntentName`, a `DecisionRoute`, a registered tool), and the set's
timestamps are fixed so two loads are byte-identical.

## 15.7 Storage — `evaluation/storage.py`

Storage **extends the persistence layer that already exists**: JSONL files in the
application's data directory, written through a temporary file and an atomic
replace, exactly like the audit trail and the benchmark store. No database, no
server, no migration — `data/evaluation/{trajectories,evaluations,rewards,datasets}.jsonl`.

* **Replaceable.** A repository takes a store, and a store is a three-method
  protocol (`append`/`read`/`replace`) with an in-memory implementation for tests.
* **Upsert by id.** Evaluation and reward ids are derived from the trajectory id,
  so a late annotation *replaces* its rows instead of leaving two that disagree.
* **Filterable.** Trajectories by task, model, status, success and date range;
  evaluations by trajectory, task, status, dimension score; rewards by trajectory
  and total; datasets by id and version.
* **Bounded and prunable.** A configured cap per file, plus retention by age.

## 15.8 Metrics — `evaluation/metrics.py`

Computed from rows that were already written — nothing sampled, nothing
instrumented: task success rate, failure rate, verification success rate, tool
selection accuracy, tool-call success rate, planning success rate, recovery
success rate, safety intervention rate, retry rate, average reward, average
latency with **p50/p95**, fast-path percentage, LLM escalation percentage, the
per-route counts, average memory delta, the quality distribution, the nine
dimension means, and the overall-status distribution.

Two definitions are written down rather than guessed: **fast path** is a route
that reached a direct local path (a tool, the machine's own readings, a subsystem
handler, the vision pipeline) with no model; **LLM escalation** is a route that
needed a language model or the plan/agent loop. Both are counted **per run** — ten
runs through `local_llm` beside one through `direct_tool` are a 10% fast path and
a 90% escalation, not the 50/50 that counting the two route names would give. A
route outside both counts as neither, so the two percentages do not silently
claim to sum to one hundred. An unmeasured run is absent from a latency sample
rather than present as a zero.

## 15.9 API surface — read-only by construction

| Route | Returns |
| --- | --- |
| `GET /evaluation/summary` | what is recorded, how it was scored, what is held, retention, privacy |
| `GET /evaluation/trajectory/{trajectory_id}` | one run: trajectory + evaluation + reward breakdown |
| `GET /evaluation/metrics` | every aggregate figure |
| `GET /evaluation/rewards` | stored rewards, newest first, filterable by total |

Nothing here runs a task, calls a model, quotes a prompt, or writes a row. The
evaluation layer also answers bus requests (`evaluation.summary_requested`,
`evaluation.metrics_requested`, `evaluation.trajectory_requested`,
`evaluation.reward_requested`) and announces its own progress with custom event
types (`trajectory.recorded`, `evaluation.completed`, `reward.computed`,
`evaluation.retention_applied`) — no new event bus, and no lifecycle vocabulary
invented for it.

## 15.10 Privacy and data safety

* **Redaction happens on the way in**, through the audit trail's own `Redactor`
  (private keys, bearer tokens, JWTs, `sk-`/`ghp_`/`xox`/`AKIA` credentials, URL
  credentials, `key=value` assignments, secret-named mapping keys), and the count
  travels on the row (`redactions`, `redaction_kinds`).
* **The quality filter is the second line**: residual sensitive text rejects a row.
* **No hidden chain-of-thought**, anywhere. The schema has no field for it, and
  `GET /evaluation/summary` states `stores_chain_of_thought: false`.
* **Recording can be turned off** — from the config file, the environment, or
  `POST /settings` (`evaluation_enabled`), taking effect on the next request
  rather than the next restart.
* **Retention is configurable** (`evaluation.retention_days`,
  `evaluation.max_records`) and applied at boot; the rows live in a local file
  that can be deleted like any other.

## 15.11 How future SFT/DPO/RL phases would consume this

Nothing below is implemented in this phase; this is the interface those phases
would build against.

| Phase | What it takes from here |
| --- | --- |
| SFT / behaviour cloning | `ACCEPTED` trajectories: `user_request` → `structured_intent`/`decision`/`plan` → `execution_steps`/`tool_calls` as structured targets, with `success is True` and no unresolved failure. |
| DPO / ORPO | pairs of trajectories with the same request shape and different `RewardResult.normalized_reward`, or a `user_feedback` rating as the preference signal; the trajectory ids and reward breakdowns give the pairing. |
| RLHF / RLAIF | `RewardResult` as the reward model's target, with `components`/`penalty_breakdown` as the feature set, and `user_feedback` reserved as the human signal. |
| RLVR / verifiable-reward RL | the `verification` and `safety` dimensions plus `golden_matches`, which are binary, checkable outcomes rather than opinions. |
| Agentic RL | full trajectories as episodes: states (`decision`, `plan`, step statuses), actions (`tool_calls`), observations, and per-episode returns from `RewardResult`. |
| Evaluation / regression | the `GoldenDataset` version that was in force (recorded on every result) and the aggregate metrics as the trend line. |

Two properties make those phases possible without re-plumbing anything: every row
is **versioned** (schema, evaluator, reward, dataset), and the quality verdict
plus its reasons travel **with** the row, so "what is good enough to train on"
is answerable from the store rather than from a re-run.

## Tests

`tests/test_evaluation.py` — 108 tests / 6 subtests: trajectory creation and
serialisation, event→trajectory conversion, incomplete captures, quality
filtering (including duplicates, residue and malformed rows), the nine
dimensions (including false success, escalation and golden comparisons), reward
calculation and breakdown, configurable weights, safety and verification
penalties, the golden dataset, repository persistence and filtering, privacy and
redaction, recording disabled, recorder/sink failure isolation, the HTTP surface,
and a live application driven through `handle_request` with **no model loaded**.

## Gates

pytest in three file groups — **2,362 passed / 12 skipped** (1,846 subtests);
`docs/API.md` in sync (92 routes); mypy clean in both platform views (264 source
files); ruff clean on the new package and its tests.
