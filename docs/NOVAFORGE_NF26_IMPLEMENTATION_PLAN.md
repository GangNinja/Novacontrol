# NovaForge NF-26 — Adaptive Gameplay, Learning, and Performance Optimization

**Status:** implemented and verified on deterministic fixtures (2026-10-28); the live-game half is NOT RUN.
Reconnaissance complete; this plan is written before the code and is kept as built. Results:
`docs/NOVAFORGE_NF26_ADAPTIVE_GAMEPLAY_REPORT.md` (with its two verification-pass appendices) and
`docs/NOVAFORGE_VERIFICATION.md` Appendix D. The fourteen defects the two verification passes found and fixed are
listed in those appendices; where this plan named a metric, class or parameter that the build does not have, it
has been corrected to the as-built name (the plan's decisions are unchanged).

**Rule this phase exists to enforce:** a strategy is not better because it was selected more often, sent more
actions, or scored well internally. It is better only when verified outcomes, measured under comparable
conditions, say so — with the sample size and the uncertainty stated.

## 1. Upstream dependency status (measured, not assumed)

| Capability | Established by | State NF-26 may rely on |
|---|---|---|
| Observation + validation | NF-15 | verified (fixtures + recorded frames) |
| Structured state understanding (scene) | NF-16 / NF-24 | verified on fixtures and recorded frames; **live-game interpretation BLOCKED** (NF-23/24) |
| Objectives and progress | NF-17 | verified (32/32 eval, 0 false completions) |
| Knowledge memory with confidence and freshness | NF-18 | verified (51/51 eval) |
| Bounded input | NF-19 | verified (717 control tests) |
| Evidence-grounded planning | NF-20 | verified; `PlanningContext` is the planner's whole evidence, `PlanPolicy` its bounds |
| Outcome verification + recovery | NF-21 | verified; `not_verifiable` is a real answer |
| Bounded autonomous orchestration | NF-22 | verified on fixtures/simulation |
| Bounded real-game task + evidence judge | NF-25 | verified on fixtures (91 tests); live execution **BLOCKED / NOT APPROVED** |

**Consequence for NF-26:** the learning layer may be built and verified on deterministic fixtures and on records
produced by NF-25's judge. **Live-game adaptation remains explicitly unverified**, because no safe installed
offline title can start on this host and no operator approval for a live input test exists.

**No upstream defect was found to block NF-26;** nothing in this plan assumes a capability NF-13A–NF-25 did not
demonstrate.

## 2. What already exists (reused, never duplicated)

| Need | Reused component |
|---|---|
| Experience source | `novaforge.tasks.TaskReport` (`run`, `progress`, `stages`, `verification`, `evidence_source`, `simulated`) — NF-25's judge is the only thing that decides a verdict |
| Run facts (cycles, dispatch/response, recoveries, evidence refs) | `novaforge.autonomy.AutonomyRun.to_dict()` as carried inside the task report |
| Knowledge | `novaforge.knowledge_service.KnowledgeService` (provenance of `knowledge_refs` on plans) |
| Planning channel | `planner.PlanningContext` + `planner.PlanPolicy` (a new optional `strategy` field, §5.3) |
| Orchestration / input / verification | NF-22 → NF-20 → NF-19 → NF-21, unchanged |
| Persistence | `persistence.json_store.JsonStateStore` (`read`/`write`, forgiving reads), the store the manager already owns |
| Task definition for bounded exploration | `novaforge.tasks.TaskSpec` (budgets, permitted actions, required evidence) |

No new observation code, no new planner, no new input path, no second verifier.

## 3. Objective A — Evidence-based experience records

New module `src/novacontrol/novaforge/learning.py`.

* `ExperienceRecord` — one task attempt, **facts and interpretations in separate fields**:
  task/objective/game/profile/strategy identity; `initial` reading (observation id, state id, scene, age);
  observed transitions; actions requested vs dispatched vs responded; verified progress; final outcome and
  verification status with evidence refs; recovery attempts; failure codes; resource + timing rows (a figure
  that was not measured stays `None`); `confidence` (measured mean or `None`); `evidence_source` and
  `simulated`; `interpretation` (hypotheses, never merged into facts); `recorded_at`.
* Builders: `ExperienceRecord.from_task_report(report, run=…, strategy_id=…, strategy_version=…)` and
  `ExperienceRecord.from_dict` (returns
  `None` for an unknown schema version or a malformed row — a corrupt record is dropped with a counter, never
  guessed at).
* **Explicit uncertainty policy** (`ExperienceLabel`): only a judged `verified_completion` is
  `verified_success`; `objective_failed` is `verified_failure`; every other verdict — cancelled, safety stop,
  resource refusal, dependency failure, interrupted, progress-only, outcome unknown — is `uncertain` and is
  **excluded from success and failure rates** unless the caller asks for the uncertainty rate. An executed
  action is never a success label; a dispatch-only run can never become evidence of progress.
* Validation: `errors()` refuses a record with no task id, no objective, an unknown action kind, a negative
  count, a non-boolean `simulated`, an unreadable timestamp, or an interpretation key that contradicts a fact
  field. Invalid records are refused, never silently repaired.

## 4. Objective B — Strategy evaluation

* `StrategyMetrics` computed per `(game, strategy_id, version)` over a set of records: `tasks`,
  `verified_successes`, `verified_failures`, `uncertain`, `decided`, `completion_rate` (successes ÷
  succeeded+failed), `uncertainty_rate`, `verified_progress_per_minute` (only over records whose duration was
  measured, else `None`), `actions_dispatched`/`actions_responded`, `unnecessary_actions` (dispatched −
  responded, floored at 0), `failed_action_rate`, `recoveries`, `recovery_rate`, `safety_stops`,
  `resource_refusals`, `policy_breaches`, `mean_task_seconds`, `measured_seconds`, and `live_records` /
  `simulated_records`.
* No universal score. The comparison names its **primary metric** (default `completion_rate`) and reports
  every metric separately, each with its own direction.
* `StrategyComparison` compares a baseline arm and a candidate arm: sample sizes, Wilson 95 % interval for the
  completion rate, per-metric deltas, `insufficient_evidence` when either arm is below `MIN_TRIALS`
  (default 5) or the primary metric is unmeasurable, `improvement`, `regression`, or `no_difference`
  (non-overlapping intervals decide; overlapping intervals with a large sample say `no_difference`, never
  "probably better").
* Safety is a non-inferiority gate, not a metric to trade: a candidate whose `safety_stops`,
  `resource_refusals`, `policy_breaches` or `uncertainty_rate` is worse than the baseline cannot be an
  `improvement` whatever the primary metric says.
* Simulated and live evidence are never pooled: a comparison over simulated records says so, and
  `live_evidence=False` travels on the result. An arm that mixes the two classes, or that spans more than one
  game or more than one strategy version, is refused by name — a rate describes one class, one title and one
  profile, and the caller scopes the records.

## 5. Objective C — Bounded strategy adaptation

### 5.1 `StrategyProfile` — the whole surface a strategy may change

`strategy_id`, `version`, `max_actions_per_plan` (may only be **lower** than the policy), `max_state_age_seconds`
(may only be **tighter**), `preferred_kinds`, `avoided_kinds` (validated against `CONTROL_KINDS`; a kind cannot be
both), `preference_weight` (0–1), `evidence`, `rationale`, `evaluated_by`, `created_at`, `supersedes`,
`active`. `errors()` refuses anything outside those bounds: a profile can make the planner more careful or
reorder candidates, it can never widen a budget, invent an action, or change a rule.

### 5.2 `StrategyChange` — every adaptation is a record, not a mutation

Previous profile, proposed profile, the metric comparison and the experience ids that motivated it, the
expected benefit, the risks, the applicability conditions (game / objective kind / profile), the evaluation
method, the comparison it was based on, and `rolled_back` with its reason. Applied only through
`AdaptationEngine.apply(change, approved=…)`; a change is never applied from a single unfavourable record
(the evidence gate is the comparison's own `improvement` verdict), and any change can be rolled back to the
previous profile, which is kept.

### 5.3 The planning channel (one new optional field, no second planner)

`PlanningContext.strategy: StrategyHint | None`. The planner (NF-20) may use it in exactly three documented
ways, all of which only *narrow* what it would otherwise do:

1. **Ordering** — a bounded scoring term (`parts["strategy"] = ±weight`) that moves preferred kinds earlier and
   avoided kinds later among the candidates that already exist. It never adds or removes a candidate and never
   makes an infeasible action feasible.
2. **Plan length** — `min(policy.max_actions_per_plan, hint.max_actions_per_plan)`.
3. **Freshness** — `min(policy.max_state_age_seconds, hint.max_state_age_seconds)`: a strategy may demand
   *fresher* evidence, never staler.

The applied hint's identity is recorded on the plan (`plan["strategy"]`), so every plan says which strategy
produced it. A hint that fails validation is ignored with a recorded reason — never partially applied.

## 6. Objective D — Failure analysis

`analyse_failures(records)` groups records by a **signature** (final outcome + failure codes + scene + strategy)
and emits `FailurePattern` rows with: occurrences, the evidence references that support the pattern,
**counterevidence** (records for the same game and strategy that succeeded), a measured confidence
(`occurrences ÷ (occurrences + counterevidence)`) and its own `None` when there is no counterevidence to weigh,
a `FailureBlame` classification (invalid observation, state interpretation, objective definition, plan
selection, input dispatch, no observable progress, unexpected transition, ineffective recovery, resource
exhaustion, unverifiable outcome, unknown), affected games/objectives, a suggested remediation, and
`verified: bool` — true only when the record's own evidence names the cause (e.g. a `dependency_unavailable`
outcome with a recorded refusal code), otherwise the row is kept as a **hypothesis** with a note that
correlation is not causation. One failure never changes the planner.

## 7. Objective E — Persistent learning with provenance

`ExperienceStore` over the manager's `JsonStateStore`, one namespace (`novaforge_learning`), one versioned
envelope: `{version, schema: "nf26.1", records, changes, profiles}`.

* Bounded: `max_records` (default 200) and `max_changes` (default 50), oldest dropped first, drop counted.
* Duplicate handling by `experience_id`: an identical duplicate is counted and dropped, a changed row with the
  same id replaces the stored one (a re-judged run wins) — pinned either way.
* Corrupt or schema-mismatched rows are dropped with a counter; an unreadable store is `{}`, as the store
  itself promises. Nothing raises into the work that recorded it.
* Compact evidence only: ids, refs, counts, metric rows — **no frames, no screenshots, no OCR dumps**. Observed
  text is evidence, never an instruction: nothing read from a record is executed, and a record whose text reads
  like a command has no effect on any strategy (pinned).
* `provenance(experience_id)` returns the refs that record does carry, so "which evidence influenced this
  strategy" is answerable from the store alone.

## 8. Objective F — Controlled exploration

`StrategyExperiment` compares a **known-safe baseline** and one candidate under an approved NF-25 `TaskSpec`:
deterministic arm assignment (alternating, so a run is reproducible without randomness), per-arm budgets
inherited from the task spec (`max_actions`, `max_seconds`, `max_cycles`) and its own `max_trials_per_arm`,
comparability conditions recorded (game, profile, objective, session, budgets), a stop when state confidence
falls below `min_state_confidence` or when a safety stop/resource refusal occurs, and a refusal to start for an
unapproved or unrunnable task. Destructive actions are impossible by construction: the arms only ever receive
the kinds the task spec permits. Live exploration is **not run** in this pass and
would need explicit approval (the plan §11 states the procedure).

## 9. Objective G — Regression prevention

* The reference profile (`version 0`, no preference, policy caps) is never lost: every change keeps the
  profile it replaced, and `rollback` restores it to both the change's own slot and the active slot planning
  reads — so the known-safe strategy is the default undo however many changes were applied.
* Every applied change is traceable: `StrategyChange` carries its evidence and evaluation method, and
  `LearningService.history()` returns them in order.
* Rollback (`rollback(strategy_id, reason=…)`) restores the superseded profile and records the rollback; the
  baseline remains reachable after any number of changes.
* Regression detection is the comparator's job: a candidate that lowers the primary metric, or worsens a safety
  metric, is reported as `regression` / `not an improvement` — pinned with fixtures where the better-scoring
  arm is the worse one in verified outcomes.
* Safety budgets stay in force after adaptation: the hint only narrows; the tests assert the planner's own
  policy still bounds a plan built under an aggressive-looking profile.

## 10. Objective H — Integration with NF-23 … NF-25

* The learning layer **consumes** `TaskReport` only — it never captures a frame, interprets a state, verifies
  an outcome or sends input.
* Recommendations return through NF-20's existing `PlanningContext` (§5.3); NF-22's loop and NF-19's control
  path are untouched.
* Simulation results stay simulated: `evidence_source`/`simulated` travel from the report through the record
  into the comparison and onto every report line.
* Wiring: `NovaForgeManager.learning()` builds ONE `LearningService` over this manager's own store (no
  knowledge collaborator: the layer's whole input is the judged report); `NovaForgeManager.run_task` records the
  judged report as an experience; `_planning_context` passes the active profile's hint to the planner.

## 11. Objective I — Testing, performance, and the live-test rule

Tests (`tests/test_novaforge_nf26.py`) cover the brief's list: record validation and provenance, unknown and
contradictory outcomes, strategy comparison, small-sample uncertainty, safety non-inferiority, failure
classification with counterevidence, change explanations, persistence and schema handling, duplicates, corrupt
records, baseline preservation, rollback, budget enforcement under a hint, simulation/live separation,
adaptation refused from invalid evidence, and regression detection — plus the planner-side pins for the three
documented hint effects. Deterministic fixtures only; no window, no input, no model.

Measurements (`scripts/novaforge_nf26_learning_eval.py`): record build, evaluation, comparison, hint build and
persistence latency; store size; memory held at the record cap (`tracemalloc`) and by one planning hint; and the
planning-hint effect on planning latency (measured against the same fixture context with and without a hint).

**Live-test rule:** no live gameplay test in this pass. The procedure (for a machine with a launchable safe
offline title) is: identify the process/window, check the resource gate, confirm the game is not already
running, present the exact task spec (objective, permitted inputs, action/time budgets, expected evidence, stop
conditions), and only then, with the operator's explicit approval, run the baseline arm followed by the
candidate arm on comparable starts.

## 12. Objective J — Demonstration of measurable improvement

The evaluation script separates **tuning data** (set A, used to choose the candidate profile) from
**evaluation data** (set B, disjoint, used only to measure), runs both arms over each set with deterministic
fixtures that mimic the failure modes the phase cares about (forced failures, unverifiable outcomes, action
waste), and reports: completion rate per arm with Wilson intervals, sample sizes, the per-metric deltas, the
safety metrics, and the verdict. It also runs the two honesty controls: an arm that merely *sends more actions*
must not be judged better, and a candidate whose advantage exists only in the tuning set must be reported as
`insufficient_evidence`/`no_difference` on the evaluation set.

## 13. Safety boundaries

Unchanged and enforced where they already are: no GTA V, no external drive, no force-kill, no duplicate
instance, no injection/memory/rendering hook, no display-mode change, no online multiplayer, no input to an
unvalidated window, resource gate before any launch, graceful shutdown only, no live test without explicit
approval. NF-26 adds no input path: every action still travels NF-22 → NF-20 → NF-19 → the Guardian, and the
learning layer cannot be reached from an action at all.

## 14. Measurable acceptance criteria

| Criterion | Target | Basis |
|---|---|---|
| record validation | 100 % of records missing a fact or carrying an unknown kind/scene are refused | `ExperienceRecord.errors()` |
| invalid labels | 0 records labelled success/failure from a dispatch-only, cancelled, timed-out or unknown run | `ExperienceLabel` policy + tests |
| small samples | < 5 trials per arm ⇒ `insufficient_evidence`, never a superiority claim | `StrategyComparator` |
| traceability | every applied change lists its evidence ids, its evaluation method and its rollback | `StrategyChange` |
| rollback | restoring the superseded profile leaves the baseline reachable | `AdaptationEngine.rollback` |
| bounds | a hint never raises `max_actions_per_plan` or relaxes `max_state_age_seconds`; an invalid hint is ignored with a reason | planner pins |
| persistence | corrupt rows dropped and counted; duplicates handled; growth bounded | `ExperienceStore` tests |
| honest verdict | an improving arm must beat the baseline on the primary metric **and** not worsen any safety metric | `StrategyComparison` |
| demonstration | the evaluation set shows a reproducible improvement (or the run reports `no_difference`) | `novaforge_nf26_learning_eval.py` |

## 15. Implementation steps

1. `novaforge/learning.py`: records, labels, store, metrics, comparator, profiles, changes, adaptation engine,
   failure analyser, experiment.
2. `planner.py`: optional `PlanningContext.strategy` + the three bounded effects + plan-level traceability.
3. `manager.py`: `learning()`, the strategy hint in `_planning_context`, and the `run_task` hand-off (which
   calls `record_task_report`); `__init__.py` exports.
4. `tests/test_novaforge_nf26.py`; `scripts/novaforge_nf26_learning_eval.py`.
5. `docs/NOVAFORGE_NF26_ADAPTIVE_GAMEPLAY_REPORT.md` + `docs/NOVAFORGE_VERIFICATION.md` Appendix D.

## 16. Open risk and its control

* **Learning from noise** — controlled by the uncertainty policy, the trial floor and the comparator's
  interval overlap rule.
* **A strategy that only looks faster because it fails faster** — controlled by the safety non-inferiority gate
  and by counting `unnecessary_actions` and `recovery_rate` separately from completion.
* **Simulation mistaken for real-game improvement** — every artefact carries `evidence_source`/`simulated`, the
  comparison reports `live_evidence`, and the verdict section of the report keeps the two apart.
