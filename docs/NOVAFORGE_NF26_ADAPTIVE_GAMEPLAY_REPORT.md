# NovaForge NF-26 — Adaptive Gameplay, Learning, and Performance Optimization

**Status:** implemented and verified on deterministic fixtures; the live-game half is **NOT RUN** (no approval, no
launchable safe offline title — the NF-23/24/25 blocker stands). No claim of improved real-game adaptation is made.

**Rule this phase exists to enforce:** a strategy is not better because it was selected more often, sent more
actions, or scored well internally. It is better only when *verified outcomes*, measured under comparable
conditions, say so — with the sample size and the uncertainty stated.

---

## 1. Executive summary

NF-26 adds **one** new module, `src/novacontrol/novaforge/learning.py`, and two seams into existing ones. It
turns NF-25's judged task reports into versioned **experience records**, measures each strategy from those
records, **refuses** to call a difference an improvement without enough decided trials and detached intervals,
changes the planner only through a bounded **strategy hint** on the existing `PlanningContext`, groups recurring
failures with their counterevidence, and keeps every change traceable and reversible.

What it does **not** do: capture a frame, interpret a state, verify an outcome, send input, or run a second
planner. Its whole input is NF-25's report; its whole output is a hint the NF-20 planner may ignore.

Verification: **70 new tests pass**, the dependency suites (**161 / 170 / 363 / 58** tests) pass, `mypy` is clean
in both platform views (454 files), the API reference is in sync, and the evaluation script's six cases match
their expected verdicts with **0 false labels** and **0 live-evidence claims**. The baseline-versus-adapted
demonstration is **fixture-based (simulated)**; a real-game improvement claim needs approved live trials and is
explicitly not made.

Defects found and fixed in this phase are listed in the two verification-pass appendices at the end — including
two that would have made the feature silently useless in production (an unapproved apply permanently poisoning a
valid change, and an applied change never reaching the slot the planner reads) and three that let a comparison
report a rate pooled across evidence classes, games or strategy versions.

## 2. Initial findings and upstream dependency status

Reconnaissance read the NF-13A … NF-25 reports, `docs/NOVAFORGE_VERIFICATION.md` (Appendices A–C), the NF-23/24/25
implementation plans, and the code for observation (NF-15), state understanding (NF-16/24), objectives (NF-17),
knowledge (NF-18), bounded input (NF-19), planning (NF-20), verification/recovery (NF-21), orchestration (NF-22)
and the judged task layer (NF-25).

| Capability NF-26 relies on | Established by | State as NF-26 finds it |
|---|---|---|
| Judged task report with verdict, evidence and provenance | NF-25 | verified on fixtures (91 tests); live execution blocked |
| Bounded planning with an explicit evidence context | NF-20 | verified; `PlanningContext` gained one optional field |
| Bounded orchestration / input / verification | NF-22 / NF-19 / NF-21 | verified, untouched |
| Objectives and progress | NF-17 | verified (32/32 eval, 0 false completions) |
| Knowledge memory with provenance | NF-18 | verified (51/51 eval) |
| Structured state understanding | NF-16 / NF-24 | verified on fixtures and recorded frames; live interpretation **blocked** |

**No upstream defect blocked NF-26.** The consequence of the blocked live half is stated once and carried
everywhere: NF-26 may be built and verified on deterministic fixtures, and every artefact it produces says
`simulated`.

## 3. Files and components changed

| File | Change |
|---|---|
| `src/novacontrol/novaforge/learning.py` | **new** (≈2,200 lines): records, labels, metrics, comparator, profiles, changes, adaptation engine, failure analysis, store, experiment, service |
| `src/novacontrol/novaforge/planner.py` | `PlanningContext.strategy: StrategyHint \| None`; the hint's three bounded effects (`_active_hint`, `_state_age_limit`, the `strategy` scoring term, the plan-length floor) and `plan["strategy"]` traceability |
| `src/novacontrol/novaforge/manager.py` | `learning()` (one service over the manager's own store), the hint in `_planning_context`, the `run_task` hand-off (records the judged report against the strategy that was in force), `run_experiment` |
| `src/novacontrol/novaforge/__init__.py` | exports for the learning surface |
| `tests/test_novaforge_nf26.py` | **new**: 70 tests |
| `scripts/novaforge_nf26_learning_eval.py` | **new**: the evaluation (6 cases, latency, storage) |
| `docs/NOVAFORGE_NF26_IMPLEMENTATION_PLAN.md` | the plan, kept as built |
| `docs/NOVAFORGE_NF26_ADAPTIVE_GAMEPLAY_REPORT.md` | this report |
| `docs/NOVAFORGE_VERIFICATION.md` | Appendix D (current results; A–C untouched) |

No API route was added or changed, so `docs/API.md` is untouched and `scripts/generate_api_reference.py --check`
reports **in sync**.

## 4. Experience-record schema

`ExperienceRecord` (`EXPERIENCE_SCHEMA_VERSION = "nf26.1"`) keeps **facts and interpretations in separate
fields**:

* **Identity** — `experience_id` (`exp:<task_id>:<run_id>`), `task_id`, `session_id`, `objective`, `game`,
  `profile`, `strategy_id`, `strategy_version`.
* **Facts** — `label`, `verdict`, `code`, `initial` (observation id, state id, scene, age), `transitions`
  (per-cycle state/scene rows), `actions_requested` / `actions_dispatched` / `actions_responded`,
  `verified_progress`, `recoveries`, `failure_codes`, `resource` (gate, limits, held inputs after the run),
  `timing` (`started_at`, `ended_at`, `seconds`), `evidence_refs`, `evidence_source`, `simulated`,
  `recorded_at`.
* **Interpretation** — `interpretation`, a free mapping that **may not carry a fact key** (`errors()` refuses
  it). `confidence` is a measured mean or `None`.

A figure nobody measured is `None`, never `0`: `seconds`, `confidence`, `actions_requested` and
`verified_progress_per_minute` all report "not measured" as `None`.

`from_task_report(report, run=…)` returns `None` for a report that never ran (a refused spec, a missing
approval): those are preflight refusals, not trials of a strategy. An interrupted run **is** recorded and
labelled `uncertain`.

**Uncertainty policy:** only a judged `verified_completion` is `verified_success`, only `objective_failed` is
`verified_failure`; every other ending (dispatch-only, progress-only, cancelled, safety stop, resource refusal,
outcome unknown) is `uncertain` and stays out of both success and failure rates. A dispatch success can never
become a gameplay success.

## 5. Strategy-evaluation method

`metrics_for(records, …)` measures one arm per `(strategy_id, version, game)`: `tasks`, `verified_successes`,
`verified_failures`, `uncertain`, `decided`, `completion_rate`, `uncertainty_rate`,
`verified_progress_per_minute` (over records whose **time** was measured), `actions_dispatched/responded`,
`unnecessary_actions`, `failed_action_rate`, `recoveries`, `recovery_rate`, `safety_stops`, `resource_refusals`,
`policy_breaches`, `mean_task_seconds`, `measured_seconds`, live/simulated record counts, and a Wilson 95 %
interval for the completion rate. **There is no universal score** — each metric keeps its own name and
direction, and the comparator names its primary metric (default `completion_rate`).

`compare_strategies(baseline, candidate, …)`:

1. refuses non-comparable arms by name: different games or objectives, an arm that **mixes** live and
   simulated records, and an arm that spans more than one **game** or more than one **strategy version**. A rate
   therefore describes one class of evidence, one title and one profile — the caller scopes the records, rather
   than the comparator quietly aggregating across them;
2. needs `MIN_TRIALS` = 5 **decided** trials per arm, else `insufficient_evidence`;
3. reports per-metric deltas and the sample sizes with every verdict;
4. requires `improvement` to beat the baseline on the primary metric **and** (for the completion rate) to have
   non-overlapping intervals **and** to worsen **no** safety metric (`safety_stops`, `resource_refusals`,
   `policy_breaches`, `uncertainty_rate`);
5. reports `regression` when the primary metric worsens, or when it improves while a safety metric worsens —
   a safety metric cannot be traded for a better primary metric;
6. carries `live_evidence=False` for simulated arms and lists the `evidence_ids` it used.

## 6. Adaptation and rollback behaviour

* **The whole surface a strategy may change** (`StrategyProfile`): `preferred_kinds` / `avoided_kinds`
  (validated against `CONTROL_KINDS`; a kind cannot be both), `preference_weight` (0–1),
  `max_actions_per_plan` (**lower only**), `max_state_age_seconds` (**tighter only**), plus identity, evidence,
  rationale, `evaluated_by` and `supersedes`. Anything else is refused by `errors()`.
* **Every change is a record** (`StrategyChange`): previous profile, proposed profile, the comparison and the
  experience ids that motivated it, expected benefit, risks, applicability conditions, evaluation method,
  `applied`/`approved`, and a rollback with its reason.
* **`AdaptationEngine.propose`** refuses a change whose comparison verdict is not `improvement`
  (`evidence_insufficient`) or that cites no evidence (`no_evidence`). **One unfavourable record never changes
  the planner**, because a record cannot produce an `improvement` verdict.
* **`apply(change_id, approved=…)`** requires the operator's approval when the planner's own policy requires it.
  A missing approval is a **refusal**, not a rejection: the proposal stays appliable. An applied change is
  installed in **both** its own strategy slot and the **active** slot planning reads. A change that was rolled
  back can be applied again — an undo is not a verdict that the evidence got worse — and re-applying clears the
  rollback fields so no record says both "applied" and "rolled back".
* **`rollback(strategy_id, reason=…)`** restores the profile the last applied change replaced and records when
  and why. The reference profile is never lost: every change carries the profile it replaced, `rollback`
  restores it to the change's own slot **and** to the active slot planning reads, and the reference is the
  default rollback's result however many changes were applied.
* **The planning channel is one optional field.** `StrategyHint` may do exactly three things, each of which only
  *narrows* what the planner would do anyway: reorder already-feasible candidates (a bounded ±`preference_weight`
  scoring term), shorten the plan (`min(policy cap, hint cap)`), and demand fresher evidence
  (`min(policy limit, hint limit)`). An invalid hint is ignored whole, never half-applied, and the applied hint's
  identity, evidence and effect travel on the plan (`plan["strategy"]`).

## 7. Failure-analysis approach

`analyse_failures(records)` groups records by a signature (ending + failure codes + scene + strategy) and emits
`FailurePattern` rows carrying occurrences, the evidence references that support the pattern, **counterevidence**
(records for the same game and strategy that succeeded, i.e. the setting where the signature did not recur), a
measured confidence with its own `None` when there is no
counterevidence to weigh, a `FailureBlame` classification (invalid observation, state interpretation, objective
definition, plan selection, input dispatch, no observable progress, unexpected transition, ineffective recovery,
resource exhaustion, unverifiable outcome, unknown), affected games, a suggested remediation, and
`verified: bool`.

`verified` is true only when the record's own evidence names the cause; everything else is kept as a
**hypothesis** with a note that correlation is not causation. Successes never enter a failure pattern, and an
uncertain ending is analysed without being counted as a failure. One failure never alters the planner — the
comparison's `improvement` verdict is the only door.

## 8. Automated tests and exact results

| Command | Result |
|---|---|
| `PYTHONPATH=src python -m pytest tests/test_novaforge_nf26.py -q` | **70 passed** |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_nf26.py tests/test_novaforge_nf25.py -q` | **161 passed** |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_planning.py tests/test_novaforge.py tests/test_novaforge_api.py -q` | **170 passed, 26 subtests** |
| `PYTHONPATH=src python -m pytest tests/test_novaforge_autonomy.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_verification.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py -q` | **363 passed, 58 subtests** |
| `PYTHONPATH=src python -m pytest tests/test_api_reference_sync.py tests/test_route_shape_contract.py tests/test_application.py -q` | **58 passed, 477 subtests** |
| `PYTHONPATH=src python -m mypy src` | **Success: no issues found in 454 source files** |
| `PYTHONPATH=src python -m mypy src --platform win32` | **Success: no issues found in 454 source files** |
| `PYTHONPATH=src python scripts/generate_api_reference.py --check` | **docs/API.md: in sync** |
| `PYTHONPATH=src python scripts/novaforge_nf26_learning_eval.py` | **6/6 cases as expected; false labels 0; live-evidence claims 0** (exit 0) |

The NF-26 suite (`tests/test_novaforge_nf26.py`) covers, with deterministic fixtures: record validation and
provenance, a report that never ran, dispatch-only and progress-only runs, an interpretation carrying a fact key
(refused), counts validated against each other, command-shaped screen text staying inert evidence (no strategy
change, no hint, no approval prompt), corrupt rows and a foreign schema version (dropped and counted),
rates with no denominator (`None`) and a rate whose denominator was measured, Wilson bounds, small-sample
uncertainty, interval overlap, regression, safety non-inferiority, non-comparable arms, live/simulated
separation, profile validation, the evidence gate, approval, rollback (including the no-argument undo),
baseline preservation, duplicate detection, re-judged replacement, unreadable/unwritable stores, growth bounds,
failure classification with counterevidence, experiment approval/preflight/safety-stop/confidence-stop/arm
alternation, the hint's three bounded effects **through the real planner** (both ordering directions — a preferred
kind moving ahead of a cheaper one, an avoided kind moving behind one — the plan-length floor and the freshness
ceiling), the plan saying truthfully which effect it applied, an invalid hint ignored entirely, and the manager
hand-off (including a fresh manager reading the recorded experience back from the same data directory), the
comparator's refusals (a mixed-evidence arm, an arm spanning a game, an arm spanning a version), a rolled-back
change being appliable again, and a record whose `simulated` flag contradicts its own `evidence_source`.

Everything fixture-based is labelled `simulated`. **No test launches a game, reads a window or sends input.**

## 9. Type-check and documentation validation

* `mypy src` and `mypy src --platform win32`: both clean (454 files) — the two views CI checks.
* `python scripts/generate_api_reference.py --check`: in sync (no route added by this phase).
* `docs/NOVAFORGE_VERIFICATION.md` updated as **Appendix D**, preserving Appendices A–C and the phase reports
  above them verbatim.

## 10. Baseline and adapted-strategy comparison

`scripts/novaforge_nf26_learning_eval.py` builds NF-25-shaped judged reports and compares arms over **disjoint**
run ids: **set A** is the tuning data a candidate profile would be chosen from, **set B** is the evaluation data
the verdict is drawn from.

| Case | Baseline | Candidate | Verdict | Expected |
|---|---|---|---|---|
| tuning set A | 0/6 completions | 6/6 | `improvement` | `improvement` |
| **evaluation set B** | 0/6 completions | 6/6 | **`improvement`** | `improvement` |
| a busier arm that fails more (6 dispatched / 0 responded per trial) | 0/6 | 0/6 | `no_difference` | `no_difference` |
| an advantage that exists only in the tuning set | 3/6 | 3/6 | `no_difference` | `no_difference` |
| more completions, one safety stop | 1/6 | 6/6 + safety stop | `regression` | `regression` |
| a two-trial arm | 0/2 | 2/2 | `insufficient_evidence` | `insufficient_evidence` |

Wilson 95 % intervals for set B: baseline `[0.000, 0.390]` (0/6), candidate `[0.610, 1.000]` (6/6) —
**disjoint**, with 6 decided trials per arm (above `MIN_TRIALS` = 5) and no safety metric worse. The
busy arm sends three times the actions and is still **not** better; the tuning-only advantage is **not**
promoted; the safety stop voids an otherwise complete improvement.

**This is a simulation-based demonstration of the evaluation method.** No game was launched, observed or driven:
the arms are fixtures with `evidence_source=simulated`, and `live_evidence` is `False` in every comparison
(section 12 keeps the two apart).

## 11. Sample sizes and uncertainty

Every comparison reports `baseline_decided`, `candidate_decided`, `baseline_tasks`, `candidate_tasks`, the
Wilson interval per arm, the per-metric deltas, `safety_ok` with its notes, and the full list of `evidence_ids`
it used. `MIN_TRIALS` = 5 decided trials per arm is enforced by the comparator, not by convention: below it the
verdict is `insufficient_evidence`, and a two-trial arm is pinned to fail that way. A `no_difference` verdict
explicitly says the difference was not established at this sample size rather than "probably better".

## 12. Simulation and live-game results, clearly separated

| | Result |
|---|---|
| **Simulation (fixtures)** | The evaluation method works: 6/6 expected verdicts, 0 false labels, 0 live-evidence claims; 24 records round-trip through the JSON store byte-exactly; the hint reaches the real planner and only narrows it |
| **Live game** | **NONE. NOT RUN.** No game was launched, no window observed, no input sent, no approval requested |

No live-game adaptation improvement is claimed. Objective J's "if only simulation is available, report the result
as simulation-based" branch is the one that applies.

## 13. Performance and resource measurements

Measured by the eval script (`data/novaforge_nf26_learning_eval.json`, 500 iterations per call, fixture-only).
The runs taken during verification (five, plus a final confirmation run after the last fix) are reported as a
range: these are sub-millisecond operations on a shared machine where scheduler jitter dominates, and a single
run is not a reliable cost figure.

| Measurement | Value |
|---|---|
| record build (`from_task_report`) | 0.0138–0.0148 ms |
| metrics for an arm | 0.0146–0.0172 ms |
| full comparison (baseline vs candidate) | 0.0429–0.0472 ms |
| profile + hint build + validation | 0.0157–0.0181 ms |
| planning context without a hint | ~0.00002 ms |
| planning context with a hint (validation + traceability) | 0.00134–0.00170 ms |
| **hint delta per planning decision** | **0.00129–0.00168 ms** |
| store write / read (24 records) | 1.1–1.9 ms / 17.5–19.1 ms |
| store size | 33,905 bytes total, **1,412.7 bytes per record** |
| memory: record set at the 200-record cap | 318,884–327,084 bytes retained, **1,594.4–1,635.4 bytes per record**, peak 323,068–331,268 |
| memory: one validated planning hint | **411 bytes** |
| growth bounds | `max_records` 200, `max_changes` 50, oldest dropped first |
| duplicate / corrupt handling | identical replay counted as a duplicate (no second row); a re-judged run replaces its row; corrupt and foreign-schema rows dropped and counted |

These runs were taken on the final code, after both verification passes' fixes (the first pass's ranges were the
same order of magnitude: 0.0136–0.0269 / 0.0153–0.0238 / 0.0399–0.0664 ms).

Unbounded work is structurally impossible: the record set is capped, the change history is capped, the
experiment has `max_trials_per_arm` and inherits the task's own action/time/cycle budgets, and the hint can only
*shorten* what the planner would do. Nothing in this layer loads a model, captures a frame or reads a file
per plan — the hint the planner reads is a 411-byte record, and even the record set at its full 200-record cap is
~0.33 MB of Python objects in this fixture (two measured runs). Learning retrieval therefore adds no material
memory pressure to a planning decision; the numbers are fixture-only and exclude game or model memory, which
this layer never holds.

## 14. Known limitations and blockers

1. **Live-game adaptation is unverified.** No safe installed offline title can start on this host and no operator
   approval for live input exists (NF-23/24/25 blocker, unchanged).
2. **The demonstration is simulation-based.** It shows the *method* (a candidate that genuinely completes more,
   measured on disjoint evaluation data, with intervals, sample sizes and a safety gate) — not real-game
   improvement.
3. **The adapted strategy's real effect is untested.** The hint's three effects are verified through the real
   planner on fixtures; whether reordering actions helps on a real title is an open, live-game question.
4. **No operator-facing route.** The learning surface is reachable through `NovaForgeManager.learning()` and the
   plan record (`plan["strategy"]`); adding a route means the five-place route procedure and was not part of the
   plan's scope.
5. **`MAX_RECORDS` is a fixed bound**, not a retention policy with a time window; the store keeps the newest 200
   records, which is the intended compact-evidence rule.
6. **The experiment's comparability depends on the operator** giving both arms comparable starting conditions;
   the experiment records its conditions and refuses to run without an approved task, but it cannot detect an
   unfair start by itself.
7. **No schema migration yet.** Records are versioned (`nf26.1`) and a row from any other schema version is
   **dropped and counted**, never migrated. That is safe (stale data cannot be imported under today's field
   meanings), but a future schema bump must add a migration or the ledger's history is lost. `nf26.1` is the
   only version ever written, so there is nothing to migrate today — which is also why no migration is claimed
   to have been exercised.
8. **Comparability is caller-scoped.** The comparator refuses a mixed-evidence arm, an arm spanning more than
   one game, and an arm spanning more than one strategy version; the caller scopes the records (one game, one
   version) rather than the comparator aggregating across them. The refusal names what to scope, but a caller
   that ignores it gets `insufficient_evidence` rather than a number.

## 15. Reproduction commands

```bash
export PYTHONPATH=src
python -m pytest tests/test_novaforge_nf26.py -q
python scripts/novaforge_nf26_learning_eval.py          # writes data/novaforge_nf26_learning_eval.json
python -m mypy src && python -m mypy src --platform win32
python scripts/generate_api_reference.py --check
```

Live procedure (for a machine with a launchable safe offline title and an explicit approval — **not performed**):
identify the process/window, check the resource gate (≥2 GB free RAM, memory below 92%), confirm the game is not
already running, present the exact task spec (objective, permitted inputs, action/time budgets, expected evidence,
stop conditions), then run the baseline arm and the candidate arm on comparable starting states, in an offline
single-player save.

## 16. Final acceptance checklist and verdict

| Acceptance criterion | State | Evidence |
|---|---|---|
| Experience records preserve trustworthy provenance | **PASS** | facts/interpretation split, evidence refs, `evidence_source`/`simulated`, `provenance(id)` |
| Invalid and uncertain outcomes cannot silently become training labels | **PASS** | only `verified_completion` is a success; every other ending is `uncertain`; 0 false labels in the eval |
| Strategy comparisons use defined, reproducible metrics | **PASS** | named metrics, named primary metric, Wilson intervals, fixed `MIN_TRIALS`, evidence ids listed |
| Adaptation changes are traceable and reversible | **PASS** | `StrategyChange` records + `history()` + `rollback(reason=…)`; baseline never overwritten |
| Failure analysis distinguishes hypotheses from verified causes | **PASS** | `verified` only from the record's own evidence; counterevidence and confidence reported |
| Safety boundaries remain enforced | **PASS** | hint narrows only; the planner's own policy is the ceiling; no new input path |
| Automated regression and integration tests pass | **PASS** | **74** new (third-pass pins included) + 161/170/363/58 dependency and integration tests; the complete 148-file suite is green (Appendix E) |
| Relevant type checks and documentation validation pass | **PASS** | mypy clean (both views, 454 files); API reference in sync |
| A baseline-versus-adapted evaluation demonstrates a reproducible improvement under its stated conditions | **PASS (simulation)** | evaluation set B: 0/6 vs 6/6, disjoint Wilson intervals, 6 decided trials per arm, no safety metric worse, 3 honesty controls hold |
| No known, fixable, in-scope defect remains | **PASS** | fourteen defects or stated-but-missing pins found and fixed (both verification passes in this document's appendix) |
| Approved live-game trials show the adapted strategy performing better | **NOT PERFORMED** | no approval, no launchable safe title |

**Verdict: PASS on the implementation and its fixture-based verification. NOT PERFORMED on live-game
adaptation.** The learning layer is evidence-grounded, bounded, reversible and honest about what it does not
know; no capability is claimed that was not demonstrated. Real-game adaptation improvement remains unverified
until an approved live test runs on a machine with a safe offline title.

No universal game compatibility, human-level gameplay or unrestricted autonomy is claimed.

---

## Appendix — verification pass (2026-10-28, same phase)

Run after the implementation was first reported, per the phase's "verify against every objective" instruction.
Test-first, then fixes, then the same suites again.

**Defects found and fixed (each now pinned):**

1. **A refused approval poisoned a valid change.** `AdaptationEngine.apply` wrote `approval_required` into the
   stored `StrategyChange`; because `apply` returns early for a change carrying a `rejected_reason`, once an
   unapproved call happened the change could never be applied, even with approval. A missing approval is now a
   non-destructive refusal (the record is untouched), so the operator can still approve it.
   *Pinned by* `test_an_improvement_applies_only_with_approval`.
2. **An applied strategy never reached planning.** The change was installed under its own `strategy_id`, while
   `LearningService.hint()` reads the **active** slot: an "adapted" strategy could be applied successfully and
   still influence nothing. `apply` now installs into both slots, and `rollback` restores the active slot only
   when that slot actually holds what the change installed (so a rollback of one change cannot clobber another).
   *Pinned by* `test_a_hint_is_none_until_a_change_was_applied` and
   `test_an_applied_change_reaches_the_planning_context_and_rolls_back`.
3. **Identical replayed reports were treated as updates.** Duplicate detection compared `recorded_at` (the time
   the layer heard about the run), so the same report replayed replaced its own row and `duplicates` never
   counted. The comparison now ignores the recording time (`_same_experience`).
   *Pinned by* `test_an_identical_duplicate_is_counted_and_not_doubled` (the fixture uses a fixed clock so a
   replay really is identical) and `test_a_re_judged_run_replaces_the_stored_row` (a re-judged run still wins).
4. **`LearningService.knowledge` was a dead collaborator.** The plan wired in the manager's knowledge service,
   but nothing in the module read it — an unused seam that looks like evidence integration. Removed, with the
   manager's argument: the layer's whole input is the judged report.
5. **The first draft of the planner pins tested a private helper instead of the planner.** A probe that
   re-implements `min(policy, hint)` proves nothing about the planner and broke on the real API
   (`PlanPolicy.control_policy` does not exist; `PlanningContext._replace` does not exist on a frozen slotted
   dataclass). Replaced with real `GamePlanService` runs over the NF-20 fixtures: the avoided kind moves behind a
   cheaper one, a hint can only shorten the plan, and a hint demanding 600 s is clamped to the policy's 10 s —
   each asserted from the plan the planner actually returned (including the refusal reason's "limit 2.0s" /
   "limit 10.0s").
6. **A rate was asserted without its denominator.** `verified_progress_per_minute` was expected to be `None` for
   an arm with no *decided* outcome; the metric is progress over measured **time**, so 3 progressing trials in
   30 s really is 6.0/min. The metric is unchanged and now pinned both ways: a measured denominator yields a
   number, an unmeasured one (`seconds=None`) yields `None`
   (`test_a_rate_whose_denominator_was_never_measured_is_none`). The eval script also had an internally
   contradictory dispatch-only fixture (a report claiming two dispatches while its run payload showed no
   submitted steps) and a non-ASCII print that crashed the Windows console — both fixed; the record refusal that
   exposed the contradiction is correct behavior and stayed.
7. **The operator's simplest undo could miss the live adaptation.** A change proposed for its own strategy id
   installs itself as the **active** profile (defect 2's fix), but `rollback()` with no argument looked only in
   that same id's history: the default undo returned successfully and left the adapted profile in force. A
   default rollback now also matches the applied change whose proposal is the active profile, restores both
   slots, and returns the reference profile. *Pinned by* `test_a_default_rollback_undoes_the_active_adaptation`
   (the no-argument undo removes the preference from the active slot **and** the change's own slot).
   The eval script's small-sample control was also renamed: it said `a five-trial arm is not evidence` while the
   fixture has two decided trials per arm — the label now matches the fixture and the report's table (no
   behaviour changed; the verdict was already `insufficient_evidence`).
8. **The plan claimed a pin that did not exist.** Plan §7 states that command-shaped text in a record has no
   effect on any strategy and is "pinned"; no such test existed. Added
   `test_command_shaped_text_in_a_record_is_evidence_and_never_an_instruction`: a record quoting
   "run rm -rf data/" is accepted, kept verbatim as evidence, and yields no proposal, no hint and no approval
   prompt. Nothing in the layer executes record text — now demonstrated rather than asserted.
9. **A plan claimed an effect the hint never asked for.** The plan's strategy row wrote
   `preferred_first: True` for *every* hint — an avoided-kinds-only hint or a caps-only hint has no preferred
   kinds at all, so the traceability record described a reordering that never happened (the avoided-kind test
   even asserted the false value). The row now reports `preferred_first` and `avoided_last` from the hint's
   actual lists, and **both** ordering directions are pinned through the real planner: a preferred kind moves
   ahead of a cheaper one, an avoided kind moves behind one, and a caps-only hint reports neither effect.
   *Pinned by* `test_a_preferred_kind_moves_ahead_of_a_cheaper_one` and
   `test_an_avoided_kind_moves_behind_a_cheaper_one`.

Objective I's memory figure was also missing: the eval now measures the record set at its 200-record cap
(325,584 bytes retained, 1,627.9 bytes/record, peak 329,768) and one validated planning hint (411 bytes), and
counts its reference-evaluation latency over five runs rather than one.

**Suites re-run after the fixes (all green):** every pytest invocation in §8, both `mypy` views, the
API-reference check, and the eval script (exit 0). No test was skipped, deselected, weakened or deleted; no assertion was relaxed.

**Still not verified:** everything in §14, above all live-game adaptation. The live half remains blocked by the
same two facts it was blocked by before this phase: no launchable safe offline title on this host, and no
operator approval for live input.

### Second verification pass — every objective re-checked against the code (same day)

The phase was then re-verified end to end rather than trusting the first pass's report: the NF-26 suite first,
then the dependency and integration suites, both `mypy` views, the API-reference check and five eval runs. The
audit found **five more defects**, all in the comparator's honesty guarantees or the engine's operator surface;
each is fixed and pinned.

10. **Two arms that both mixed live and simulated records were comparable.** The class check compared
    "any live" on each arm, so a baseline of simulated trials plus one live run and a candidate of the same
    shape passed, and their completion rates pooled the two classes — precisely the promotion this phase
    forbids. An arm carrying both classes is now not comparable, refused by name.
    *Pinned by* `test_an_arm_that_mixes_both_classes_of_evidence_is_not_comparable`.
11. **An arm spanning two strategy versions was measured as one rate.** After an adaptation was applied, the
    candidate's pre-change and post-change records pooled, so the "candidate" rate described neither profile
    (`compare_arms` filtered by id but not by version). The comparator now refuses a multi-version arm unless
    the caller pins `version`, and `LearningService.compare_arms` accepts that `version`.
    *Pinned by* `test_an_arm_spanning_two_versions_is_refused_until_one_is_pinned`.
12. **An arm spanning two games was measured as one rate.** Comparability compared the *sets* of games, so two
    arms both covering the same two titles passed while pooling rates across them — a per-game regression on
    one title could be hidden by a gain on the other. One game per comparison; the caller scopes.
    *Pinned by* `test_an_arm_spanning_two_games_is_refused_and_scoped_by_the_caller`.
13. **A rolled-back change could never be applied again.** `apply` returned early on `applied_at`, which a
    rollback leaves in place, so re-applying a change the operator had just undone was a silent no-op. A rolled
    back change is appliable again, and re-applying clears the rollback fields (a record never says both
    "applied" and "rolled back").
    *Pinned by* `test_a_rolled_back_change_can_be_applied_again`.
14. **A record could claim live evidence while its own source said simulated.** `errors()` checked that
    `simulated` was a boolean but never that it agreed with `evidence_source`, so a forged or mis-built row
    could enter the ledger as live evidence and make a comparison report `live_evidence=True`. The contradiction
    is now a validation error, refused at the record and not only at the comparator.
    *Pinned by* `test_a_record_cannot_claim_live_evidence_from_a_simulated_source`.

Text corrections in this pass: §13's figures were re-measured over five runs on the final code; §5, §6 and §7
wording was brought back in line with what the code does (arm comparability, the reference profile being the
rollback target rather than an untouched slot, and what failure counterevidence counts); the implementation
plan's §4/§6/§7/§9/§10/§15 were corrected where they named metrics, a class or a parameter the build does not
have.

**Re-run after the fixes (all green):** `tests/test_novaforge_nf26.py` **70 passed**; with NF-25 **161
passed**; planning + `test_novaforge.py` + API **170 passed, 26 subtests**; autonomy + gameplay API +
verification + objectives + knowledge **363 passed, 58 subtests**; API-reference sync + route shape +
application **58 passed, 477 subtests**; both `mypy` views clean (454 files); API reference in sync; the eval
script **6/6 as expected, 0 false labels, 0 live-evidence claims**, exit 0. No test was skipped, deselected,
weakened or deleted.

### Third pass — cross-phase audit (NF-22 … NF-26, 2026-10-28)

This pass audited the whole NF-22…NF-26 stack together rather than NF-26 alone, because the learning layer's
honesty depends on labels produced by the layers below it. Full command list, durations and per-phase
verdicts are in `docs/NOVAFORGE_VERIFICATION.md` Appendix E; the NF-26-specific findings are these.

**Defects found and fixed in this pass (each pinned):**

15. **An unlabelled autonomy run was reported as live evidence.** `AutonomousSessionController`'s constructor
defaulted `evidence_source` to `"live"`, and a stored run that lacked the field was restored as `"live"`.
Neither site had *declared* anything, so an unproven run could enter the ledger as real-game evidence and be
counted by the comparator as live — the exact promotion this phase forbids, performed one layer below it.
Both defaults are now `"simulated"`; production still passes `"live"` explicitly (the manager's own
controller) and the acceptance harness passes `"simulated"` explicitly, so no real run changed class.
*Pinned by* `test_undeclared_provenance_is_never_reported_as_live` (a controller with no declaration reports
`simulated`, and a restored row without the field restores `simulated`).
16. **A run payload that omitted its step lists was recorded as "zero submitted steps".** `from_task_report`
counted `len(steps)` over the cycles it found; a cycle with no `execution` block contributed 0, and a report
whose progress said it dispatched inputs then failed validation (dispatched > requested) — or, with zeros on
both sides, would have claimed a measurement nobody took. An omission is now `actions_requested=None`
(unmeasured), while an execution block that really carried no steps is still a measured 0.
*Pinned by* `test_a_cycle_without_its_step_list_is_unmeasured_not_zero` and
`test_an_execution_block_with_no_steps_is_a_measured_zero`.
17. **The manager recorded the game's NAME as the record's profile identity.** `run_task` passed
`profile=str(payload.get("game"))`, which would claim the run was read through a profile named after the title.
The record now takes the state layer's own profile id from the run (and records none when the run named none).
*Pinned by* `test_the_game_profile_is_the_state_layers_profile_not_the_game_name`.
18. **Nothing pinned that a broken recorder cannot fail the task.** The invariant is stated in `run_task`'s
docstring ("recording never changes the answer") but no test broke the recorder while a task ran. Added
`test_a_broken_recorder_never_fails_the_task`: both `active_profile()` and `record_task_report()` raise, and the
task's own report comes back unchanged.

**Cross-phase seam checked directly (not only through the hand-built fixtures):** a REAL `TaskRunner` report
(the NF-25 gateway, one completed cycle) was fed to `LearningService.record_task_report`. The record parsed
`progress`/`evidence_found`/`limits`/`resource_gate`/`initial` from the genuine payload, came out
`verified_success`, `valid`, with 2 dispatched / 1 responded submitted-step facts and 7 evidence references —
so the field names the fixtures use are the field names the product emits.

**Suites re-run after these fixes:** `tests/test_novaforge_nf26.py` **74 passed**; `tests/test_novaforge_autonomy.py`
**114 passed, 21 subtests**; NF-25 + NF-26 + gameplay API **179 passed, 10 subtests**; the NF-22 acceptance
script 16 steps / 0 failed; the NF-26 eval script 6/6 as expected, 0 false labels, 0 live-evidence claims. No
test was skipped, deselected, weakened or deleted; the two autonomy tests that had asserted `"live"` from an
undeclared controller now declare `evidence_source="live"` explicitly instead of relying on the removed
default.

### Consolidated NF-23…NF-26 acceptance (2026-10-28)

This phase was then audited together with NF-23…NF-25 as one chain. Full evidence:
`docs/NOVAFORGE_NF23_NF26_CONSOLIDATED_ACCEPTANCE_REPORT.md`; findings:
`docs/NOVAFORGE_NF23_NF26_REMEDIATION_REGISTER.md`.

**Verdict: PARTIAL** — PASS on the learning layer, its evidence discipline and its fixture-based
evaluation; BLOCKED on live-game adaptation (no approval, no launchable title). The same label is used by
`docs/NOVAFORGE_VERIFICATION.md` Appendix D, and this pass made the two documents agree.

**Findings in and below this layer (all fixed and pinned):** an unlabelled autonomy run was reported as
live evidence (R-02, `autonomy.py`); an omitted step list was recorded as "zero submitted steps" (R-03);
the record took the game's name as the run's profile identity (R-04); the "learning never gates the task"
invariant and the zero-vs-unmeasured rule were unpinned (R-05/R-06).

**Final numbers on this tree:** `tests/test_novaforge_nf26.py` **74 passed**; the complete 148-file suite
**5963 passed, 1 skipped, 3847 subtests, 0 failed** (1614 s); both `mypy` views clean over 454 files;
`docs/API.md` in sync; the eval script 6/6 as expected with 0 false labels and 0 live-evidence claims. No
test was skipped, weakened or deleted.
