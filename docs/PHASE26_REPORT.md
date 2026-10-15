# Phase 26 — Generalization, ARC-style reasoning and intelligence evaluation

**Verdict: implemented, verified and documented — with its limits stated at the same
volume as its features.** NovaControl can now measure whether a rule it inferred from
examples still works on an example it was never shown, refuse a measurement taken over
leaked examples, classify why a task failed, compare two runs task by task without
rewriting either, and reproduce a run's outcome fingerprint — and it explicitly does
**not** claim a general ARC solver, a single intelligence score, or gameplay.

The last arrow of the phase is deliberately left unexecuted: the **post-Phase-26
game-playing evaluation** is not started here. This phase ships its *contracts* (nine
named evaluation definitions plus an adapter over Phase 25's session record) and no game.

---

## 1. Scope and implemented capabilities

| Requirement | Where it lives | State |
| --- | --- | --- |
| A task corpus with unseen examples and a hand-written answer key | `tasks.py` (39 tasks: 19 fitting, 20 held-out), `models.validate_task` | ✅ 39 tasks, `phase26.corpus.1` |
| Six task families, each with its own success condition and verifier | `manager.FAMILIES`, `models.VERIFIERS` (7 verifiers) | ✅ all six run |
| ARC-style grid inference from examples | `grids.py`, `transforms.py`, `rules.py`, `solvers.solve_grid` | ✅ 8/10 held-out grid tasks |
| A named, bounded transformation search | 22 operators, depth ceiling 2, `HARD_MAX_GENERALIZATION_*` ceilings | ✅ no general solver claimed |
| Ambiguity reported instead of guessed | `fit_rules` → `ambiguous` | ✅ 2 held-out tasks |
| Unsupported tasks reported instead of answered | `unsupported` outcome | ✅ 2 held-out tasks |
| Baselines for comparison | `identity`, `random_rule`, `no_answer` | ✅ same task set as the candidate |
| Held-out evaluation with a leakage guard | `manager.leakage_report`, `require_clean_splits` | ✅ 3 checks, refusal is 422 |
| Per-family metrics, never one blended score | `metrics.family_metrics` | ✅ `aggregate_score: None` |
| Failure classification | 12 named `FailureCategory` values, `failures.py` | ✅ grouped, with advice |
| Reproducibility | outcome fingerprint (durations/order excluded) | ✅ identical runs, different ids |
| Regression comparison | `metrics.compare_results` | ✅ records never rewritten |
| Evidence and persistence | run records, export payload, `JsonStateStore` key `generalization_runs` | ✅ restored across a restart |
| Capability table | 22 declared rows on the ONE registry | ✅ 16 implemented, 6 honestly classified |
| API surface | 17 routes, no-render in the consumer contract | ✅ `docs/API.md` in sync |

Honest non-claims, published by `overview()` as machine-readable flags:

`general_arc_solver` **false** · `hardcodes_benchmark_answers` **false** ·
`sees_held_out_answers_during_search` **false** · `leaks_held_out_answers_into_tuning`
**false** · `single_intelligence_score` **false** · `fabricates_benchmark_scores`
**false** · `labels_mock_results_as_real` **false** · `trains_anything` **false** ·
`downloads_models` **false** · `modifies_production_weights` **false** ·
`auto_starts_training` **false** · `claims_gameplay` **false** ·
`bypasses_permissions` **false**.

## 2. Existing architecture reused

Nothing was forked. The phase reads and writes only through doors that already existed:

| Existing layer | What Phase 26 uses it for |
| --- | --- |
| Phase 8 `DeterministicVerifier` | the shape of every verifier; nothing here executes a gated action (no command, no network, no write outside the state store), and every capability still **declares** what it may touch on its capability row |
| Phase 9 `EventBus` + `CapabilityRegistry` | 5 typed `generalization.*` events through an injected observer seam; the ONE capability table (22 rows) |
| Phase 12 pipeline vocabulary | a task without a success condition is refused at registration |
| Phase 14 honesty rule | an unmeasured figure is `None`, never `0.0` (`cell_accuracy` is `None` for non-grid families) |
| Phase 15 evidence discipline | reused for the shape of this phase's records (what a run was made with, and a `reproduce` block on every failure); **nothing here is recorded as an agent trajectory automatically** — the trajectory recorder observes the core task lifecycle — so the vocabulary is shared and no second recorder is built |
| Phase 22 world-state vocabulary | the `state_transition` family is expressed in it |
| Phase 24 planning contracts | the `small_planning` family is a bounded search with the same ceilings |
| Phase 25 embodied session record | the game-eval contracts read it; no game and no second environment |
| `persistence.JsonStateStore` | one key for the runs; boot restore reports what came back |

## 3. Modules created or modified

Created — `src/novacontrol/generalization/` (13 Python files including `__init__.py`, 7,670 lines in the current tree):

| Module | Lines | What it owns |
| --- | --- | --- |
| `models.py` | 1,032 | the vocabularies, `EvaluationTask.validate_task`, `EvaluationRun`, `LeakageReport` |
| `manager.py` | 1,068 | splits, leakage, evaluation, runs, comparison, export, persistence, counters |
| `tasks.py` | 990 | the 39-task hand-written corpus with its answer keys |
| `solvers.py` | 977 | the six family solvers, the bounded rule search, the baselines, `Budget` |
| `transforms.py` | 793 | the 22 operators and their parameter proposals |
| `grids.py` | 540 | grid parsing, objects, bounding boxes, connected components |
| `capabilities.py` | 424 | the 22-row capability table and its honest state classification |
| `evaluation.py` | 398 | `evaluate_task` / `verify` — the per-task cycle |
| `metrics.py` | 365 | per-family metrics, weighted aggregate (declared, not reported as "the" score), comparison |
| `__init__.py` | 346 | exports, `overview()`, `DEFERRED_PHASES`, lifecycle vocabularies |
| `rules.py` | 299 | `CandidateRule`, `fit_rules`, rule (de)serialisation |
| `game_eval.py` | 278 | the nine post-Phase-26 contracts and the session adapter |
| `failures.py` | 186 | 12 categories, `FAILURE_ADVICE`, grouping, verification records |

Also created: `tests/test_generalization.py` (1,344 lines, 82 tests in 17 classes),
`scripts/benchmark_generalization.py` (636 lines, 10 self-checking scenarios),
`docs/GENERALIZATION.md` (this phase's design doc) and this report.

Modified: `api/app.py` (17 routes), `api/models.py` (17 `ApiSurface` rows),
`api/route_consumers.py` (17 consumer declarations), `application.py`
(`_build_generalization`, `generalization_*` methods, `status()["app"]["generalization"]`,
boot restore, save/stop), `core/config.py` (`generalization:` section, 15 settings, every
number clamped), `core/events.py` (5 event types), `README.md`, `docs/STATUS.md`,
`docs/DEVELOPMENT_LOG.md`, `docs/API.md` (regenerated).

## 4. The task model and the split contract

A task is a request, a success condition, a verifier, 1–3 examples, an unseen input and
an answer key, split `train | dev | held_out` at difficulty `easy | medium | hard`.
`validate_task` refuses a task with no id, no request, no success condition, no unseen
input, no answer key, an unknown verifier or empty examples — each refusal names the
missing thing, because the reason is the whole value of a refusal.

Registration is **in-memory for the life of the process** (`POST /generalization/tasks`,
422 on a duplicate id or an unverifiable task); the RUNS are what persist. That is
deliberate: a measurement can only be cited against the corpus version it was taken over.
Verified live — after a restart the registered tasks are gone, the leakage verdict is
clean again, and the runs are still there.

## 5. The inference cycle, as code

`evaluate_task` runs the cycle per task: observe the input → identify structure (grids
parsed, objects found, sequences read, scenes measured, per-field tables built) → form a
hypothesis (candidate rules fitted to **every** example) → generate a candidate → verify
with the task's own verifier → produce a `TaskResult` whose `source` says where the answer
came from (`inferred`, `deterministic`, `reusable_rule`, `model_proposal`, `mock`, `none`).

The status vocabulary keeps the endings apart: `solved`, `failed`, `ambiguous`,
`unsupported`, `invalid`, `budget_exhausted`, `provider_unavailable`, `error`. Only
`solved` counts as solved; `ambiguous` and `unsupported` are separate endings that a
single "success rate" would have hidden.

## 6. The operator library and the bounded search

22 pure operators (rotations, reflections, translations, recolour maps, object selection,
cropping/extraction, tiling, gravity, movement, counting, hole filling, outlining) composed
to depth 2. Parameters are proposed from the examples and every candidate must reproduce
**every** example exactly before it may predict the unseen input; a library that cannot
express the task yields `unsupported`. Ceilings are clamped, never raised
(`max_candidates` 4,000, `max_seconds` 300, `max_tasks_per_run` 200, `keep_runs` 50), and a
run that hits a budget says `budget_exhausted` rather than returning a slow answer.

## 7. The six families

| Family | Solver | Verifier | Held-out result (this machine) |
| --- | --- | --- | --- |
| `grid_transform` | bounded operator search | `exact_grid_match` | 8/10 solved, 1 ambiguous, 1 unsupported, **cell accuracy 0.89**, 1,018 candidates examined |
| `spatial_relation` | connected components + geometry | `relation_holds` | 3/4 solved, 1 unsupported |
| `state_transition` | per-field transition inference | `state_equals` | 1/1 solved |
| `small_planning` | bounded breadth-first search | `plan_reaches_goal` | 2/2 solved |
| `tool_selection` | example-weighted token overlap | `tool_matches` | 1/1 solved |
| | | **total** | **16/20 solved, 2 ambiguous, 2 unsupported, 0 failed** |
| `multi_step_reasoning` | constant step or ratio | `chain_matches` | 1/2 solved, 1 genuinely ambiguous |

## 8. Baselines and candidate, measured

The same 20 held-out tasks, the same seed, the same frozen settings:

| Run | Solver | Solved | Ambiguous | Unsupported | Failed | Duration |
| --- | --- | --- | --- | --- | --- | --- |
| candidate | `infer` | **16/20** | 2 | 2 | 0 | 22.5 ms |
| baseline | `identity` | **1/20** | 0 | 10 | 9 | not measured (the evidence dump recorded the counts, not the clock) |

`compare` over the two stored runs: **16 improved**, 1 regressed, 0 appeared, 0
disappeared, 3 unchanged, `baseline_preserved: true`.

The single regression is reported rather than hidden: `ho_ambiguous_double_reading` is the
ambiguity probe whose example admits several transformations, and the identity baseline
happens to echo its input, which equals its answer key. The candidate correctly refuses to
guess and reports `ambiguous`; the comparison then says honestly that it did worse there.

Reproducibility: two runs of the same configuration with seed 4242 carry the same
fingerprint `5765cdb7…`/`e7f1871c…` (different seeds are *meant* to differ) with different
run ids, and the export's reproduce block (solver, baseline, seed, settings, task ids) is
identical.

## 9. Held-out evaluation and the leakage guard

Three checks run before every evaluation: **overlapping ids** (a task in both splits),
**shared answers** (a held-out input whose answer already exists among the fitting
examples) and **used answers** (an example flagged `seen`). `clean` is `None` only when
the check could not run. With `require_clean_splits` (default), a run over a dirty split is
**refused with 422** and the refusal names the shared pair — verified live: after
registering a held-out twin of a fitting task, `/generalization/splits` reports
`shared_answers: ["smoke_leak_heldout == smoke_leak_fitting (same question, same answer)"]`
and every further evaluation is refused until the process restarts. The benchmark's
leakage scenario pins the same behaviour through the API (`registered_status: 200`,
`duplicate_status: 422`, `evaluate_status: 422`).

## 10. Failure classification and recommendations

Twelve categories, because an ending cannot say which cause happened. A measured run's
failures grouped as:

    multiple_plausible_rules  → ho_ambiguous_double_reading, ho_mr_ambiguous_step
    no_candidate_rule_found   → ho_sp_unsupported_relation, ho_unsupported_row_order

`/generalization/recommendations` turns each recurring category into advice with the
evidence attached ("19 rules reproduce every example and give 13 different answers for the
unseen input: identity; rotate(1); rotate(2); rotate(3)") and **executes nothing** — no
training run, no dataset, no model, no promotion.

## 11. API, config and CLI

17 routes, all declared `no-render` in `api/route_consumers.py` because there is no panel
for this phase yet — the consumer contract says so instead of putting a control in the
contract the frontend does not have. `GET /generalization/tasks` withholds the answer key;
`GET /generalization/tasks/{id}` is where a key lives; `GET /generalization/runs` returns
trimmed rows; the run view and the export carry the per-task results and the fingerprint.
`docs/API.md` regenerated and in sync (`python scripts/generate_api_reference.py --check`).

`generalization:` config section (15 settings, each clamped): `enabled`, `persist`,
`default_family`, `default_solver`, `default_baseline`, `evaluate_held_out_only`,
`require_clean_splits`, `max_tasks_per_run`, `max_candidates`, `max_depth`, `max_seconds`,
`max_results_kept`, `max_model_calls`, `keep_runs`, `max_registered_tasks`. A configuration
may ask for less than the `HARD_MAX_GENERALIZATION_*` ceiling and never for more.

`status()["app"]["generalization"]` carries the live report plus the boot restore report.

No CLI command was added: the phase's surfaces are the API and the benchmark script. That
is reported as a limit, not a feature.

## 12. Automated test results

Commands, and what each returned on the tree this report describes:

| Command | Result |
| --- | --- |
| `PYTHONPATH=src python -m pytest tests/test_generalization.py -q` (first pass, before the audit) | **82 passed, 838 subtests passed**, exit 0 (4m42s) |
| `PYTHONPATH=src python -m pytest tests/test_generalization.py tests/test_route_shape_contract.py -q` (after the last source edit of the first pass) | **99 passed, 928 subtests passed**, exit 0 (9m21s on the contended host) |
| `PYTHONPATH=src python -m pytest tests/test_generalization.py -q` (**final re-run after the verification audit's fixes and its four new tests**) | **86 passed, 838 subtests passed**, exit 0 (4m49s) |
| audit probes: `probe_engine.py` (engine-level) / `probe_integration.py` (real app + HTTP + a real embodied session + restart) | **36 passed / 23 passed, 0 failures, exit 0 each** — the first pass of these probes found 8 failures, 4 of them real defects (§15) |
| `PYTHONPATH=src python -m pytest tests/test_activity_timeline.py tests/test_agentcore.py tests/test_agentic_rl.py tests/test_agents.py tests/test_api.py tests/test_api_reference_sync.py tests/test_audit.py -q` (per file, after the final edits) | all exit 0 — 12 + 29 + 175 (+6 subtests) + 5 + 3 + 5 (+265 subtests) + 25 (+4 subtests) passed |
| `PYTHONPATH=src python -m pytest <46 files, 1630 tests> -q` (same-day regression slice, before the final two edits — cited as the earlier green run, not as final verification) | 1,630 passed, 4 skipped, 838 subtests, exit 0 |
| `PYTHONPATH=src python -m pytest tests/test_web_api.py -q` (**final re-run** — the HTTP/app-wiring contract suite) | **65 passed, 34 subtests passed**, exit 0 (14m30s under contention) |
| `PYTHONPATH=src python -m mypy src` | **Success: no issues found in 412 source files** |
| `PYTHONPATH=src python -m mypy src --platform win32` | **Success: no issues found in 412 source files** |
| `PYTHONPATH=src python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| `PYTHONPATH=src python scripts/benchmark_generalization.py` | **10/10 scenarios matched**, exit 0 |
| live `uvicorn` + `client_smoke.py` | boot 1 **90/90**, boot 2 (restart) **7/7**, exit 0 |

**Everything except the two rows labelled as earlier-state runs was re-executed after the
verification audit's fixes**, and the phase suite again once its four new tests existed: the
live `uvicorn` smoke was re-run end to end on the final tree (90/90 boot 1, then a restart and
7/7), as were the phase suite (86/838), the web contract suite (65/34), both mypy views, the
reference check and the benchmark (10/10). No result in this section is carried over from an
earlier state of the tree.

**What is NOT verified, stated plainly.** The **whole suite in a single run was not
completed on the final tree.** This host is shared with other agents running their own
pytest against the same repository (observed batches `test_events.py test_evidence_ranking.py`,
`test_build_learn.py test_build_project.py`, and others), and under that contention several
single files that normally take seconds take minutes — `test_application.py` alone is 32
full-application-boot tests and exceeded the tool window twice; it first looked like a "hang"
at 8 tests, and re-running one of them alone took **23.6 s** on this host, so the file is
simply minutes long under contention rather than stuck. The phase's own suite, every
file touched by the final edits, both mypy views, the generated reference, the benchmark and
the live-server workflows were all run and pass on the final tree; the full-suite total is
left to CI (`.github/workflows/ci.yml`), which is also the one acceptance item this report
already records as local-only. Nothing in the results above was extrapolated from an earlier
run: each command listed was re-executed against the edited files.

## 13. Real user-level workflow results

A real `uvicorn` server (`127.0.0.1:8001`) was started from an isolated working directory
with its own `data/`, with Ollama and cloud LLM autodetection disabled, and driven by a
client that speaks HTTP exactly as a user or a CLI would. **Boot 1: 90/90 checks passed.
Boot 2 (after a restart): 7/7 passed.** The whole run was repeated on the final tree after
the audit's fixes — same counts, exit 0 — so these are not pre-fix numbers.

Boot 1 covered: the status/overview/families/operators surfaces; the task list withholding
its answer key while the single-task view carries it; registration accepted, a duplicate id
refused (422), a task with no success condition refused **and the refusal naming the field**,
an unknown verifier refused; a full evaluation with per-family metrics and no aggregate
score; the trimmed run list; the run view and the export agreeing on the fingerprint and
stating how to reproduce; a baseline run and a task-by-task comparison whose every
regression is a real movement; failures grouped by category; non-executing
recommendations; the nine game-eval contracts with `gameplay_claimed: false` and an honest
"no session has been run" report; two identical runs fingerprinting identically through the
export; and the leakage refusal described in §9.

Boot 2 covered: the runs survived the restart (`restored: 4, skipped: 0`), a restored run is
readable whole **with its fingerprint**, the status surface carries the restore report, the
registered tasks did **not** persist, and the leakage verdict is clean again.

The verification audit below added the leg the original smoke could not reach, because a
fresh install has no embodied session: a **REAL Phase 25 session** was created and run over
HTTP, and `/generalization/game-eval` then measured it (`sessions_measured >= 1`, rows that
name their environment, a measured figure on at least one contract, `gameplay_claimed:
false`). The contracts are therefore proven to read the session record rather than merely
declaring that they would.

## 14. Benchmark methodology and metrics

`scripts/benchmark_generalization.py` builds a REAL application over a temporary data
directory, serves it through `create_app()` and speaks to it over HTTP, then **checks its
own expectations**: a scenario that stops behaving fails the run and exits non-zero.
**10/10 scenarios matched**, in 550 ms of scenario time:

| Scenario | What it must show | Result |
| --- | --- | --- |
| A | a simple grid task inferred, verified and read back | ✅ `rotate(1)`, 6/6 cells, 25.5 ms |
| B | an unseen colour/dimension variant solved by the RULE, provenance `inferred` | ✅ 22.9 ms |
| C | ambiguous examples reported AMBIGUOUS, never scored as a match | ✅ 14.9 ms |
| D | a task outside the library reported UNSUPPORTED | ✅ 22 operators named, 31.8 ms |
| E | baseline and candidate over the same held-out set | ✅ 1/20 → 16/20, 16 improved, 1 regressed, 111.9 ms |
| F | two identical runs fingerprint identically, different ids | ✅ 145.7 ms |
| G | a tiny budget terminates inside its limit and says so | ✅ frozen to 1 candidate / 0.05 s → `unsupported`, 23.1 ms |
| H | a regression detected and the baseline record unchanged | ✅ 49.1 ms |
| I | reported counts equal the counts recomputed from the per-task results | ✅ 12 checks, 102.4 ms |
| leakage | a dirty held-out split refused with 422 | ✅ 23.0 ms |

The corpus verifies itself against its own machinery: every hand-written answer key is
recomputed from the operators, so an answer key that disagrees with the library is caught
before it can be scored — that check is what found the three corpus/operator defects in §15.
The leakage refusal is proven twice over, in the benchmark's `leakage` scenario (through the
API: `registered_status: 200`, `duplicate_status: 422`, `evaluate_status: 422`) and in the
live smoke, where the refusal names the shared pair. The fingerprint's exclusions
(durations, timestamps, result order) are pinned by
`test_a_different_solver_produces_a_different_fingerprint` and
`test_the_same_seed_and_settings_produce_the_same_fingerprint`, and rehearsed end to end by
scenario F and the live reproducibility leg.

## 15. Defects found and fixed

**During development (found by the phase's own corpus verification, which checks every
hand-written answer key against the operators):**

1. The **background heuristic** of `count_non_background`/`crop_to_content` miscounted on
   grids whose background colour is not the most frequent one — the answer keys disagreed
   with the operator, and the operator was wrong.
2. The **hole-filling operator** filled diagonally-adjacent regions as if enclosed; the
   enclosure test now requires the fill to reach the border to be a hole.
3. Two **mistyped literals** in the hand-written corpus (a wrong expected grid in a
   rotation task and a wrong next term in a sequence task) — caught because the corpus
   verification recomputes every answer key from the operators rather than trusting it.

**Found by the final verification pass (this report's own re-run):**

4. `GET /generalization/evaluate` and the `ApiSurface` row both promised the run view
   carries "a fingerprint over outcomes only", but the run payload had no such key — the
   fingerprint only existed on the export. Fixed by computing it on the run view itself
   (it is derived from the results already in hand, never stored), so the contract is true
   for both the single-run view and the export.
5. `scripts/benchmark_generalization.py --json` crashed with
   `AttributeError: 'Check' object has no attribute '__dict__'` — `Check` is a
   `slots=True` dataclass, so `check.__dict__` does not exist and the machine-readable
   form of the benchmark could not run at all. Fixed with an explicit `Check.to_dict()`;
   `--json` now emits 10 scenarios (15 KB) and exits 0.
6. The package docstring (and this report's first draft) claimed "the permission layer is
   asked before any task is registered" — no such call exists anywhere in the package,
   because nothing here executes a gated action. The claim was **removed** rather than
   satisfied with a token call: the module now states that a capability declares what it
   may touch and that no gated action is taken, which is what the code does.

**Found by the Phase 26 verification audit (the pass this section was re-run for):**

7. **The refusal never announced `generalization.leakage_detected`.** The event shipped in
   the vocabulary and fired only on the advisory path (`require_clean_splits: false`), so
   the one caller who most needed it — the person whose measurement was just refused — saw
   nothing on the bus. Fixed by announcing the refusal (with `refused: true` and the
   specific finding) before raising, and verified through the real application relay.
8. **A stored run's own `leakage` block always read `checked: false, clean: None`.**
   `_make_run` left the model default in place, so every run claimed the very check it was
   gated on had never happened — including runs that were refused *because* it failed.
   Fixed by carrying the computed verdict onto the run; a restored run now reports
   `checked: true, clean: true, fitting_tasks: 19, held_out_tasks: 20`.
9. **The frozen settings omitted `evaluate_held_out_only`** — the setting that decides
   WHICH tasks are measured. A run that evaluated 20 of 39 tasks could not say why, and
   could not be reproduced from its own record. Fixed; the policy, the effective split and
   the ceiling are all in the frozen block now.
10. **An integration claim was corrected rather than the code bent.** The package docstring
   (and this report) said the Phase 15 trajectory/recorder layer is where a run's evidence
   lands. The recorder subscribes to the core task lifecycle only, so a generalization
   evaluation is not recorded as a trajectory. Feeding them in would create partial
   trajectories with no terminal event — a second lifecycle wearing an observer's name — so
   the claim was rewritten to say what is true: the vocabulary and the evidence discipline
   are shared, and nothing here claims to feed the trajectory recorder.

**Found by driving a real server (the live smoke in §13):** the first smoke run reported 7
failing checks; six were the smoke's own wrong key names, and one — the single "regression"
against the identity baseline — turned out to be correct behaviour (an ambiguity probe whose
answer equals its input). Both are recorded here because a verification pass that only
counts passes is not a verification pass. One real ordering truth was learned and encoded:
registering a leaked held-out task makes **every** further evaluation in that process 422,
so the reproducibility legs run before the leakage leg.

## 16. Known limitations

- **Not a general ARC solver.** The grid capability is 22 named operators composed to
  depth 2. `generalization.arc_general_solver` is `partially_implemented`, and any task the
  library cannot express is reported `unsupported` (2 of 20 held-out tasks here).
- **The corpus is small and hand-written.** 39 tasks; four of the six families have three or
  fewer held-out tasks, so a per-family rate can move by 33 points on one task. §8's numbers
  describe this corpus, not "generalization ability".
- **No model takes part.** No LLM proposes a rule (`provider_dependent`, and no provider is
  wired by default), nothing trains, nothing is downloaded, no candidate is promoted.
- **There is no aggregate score**, by design. `weighted_aggregate` exists in `metrics.py`
  and is deliberately **not** published as "the" score; the families are not comparable.
- **`random_rule` is seeded, not random** — it is a deterministic baseline so a comparison
  is reproducible. It is named as a baseline, not as an independent measurement.
- **No panel and no CLI command** for the phase; the surfaces are HTTP and the benchmark
  script.
- **Registered tasks are not persisted** — a cited number can only be checked against the
  built-in corpus version named in the run.
- **`metrics.weighted_aggregate` ships and nothing in the shipping path calls it.** It is a
  documented utility for a caller who supplies explicit normalisation and weights, and the
  benchmark deliberately never uses it to produce a headline number (that is the phase's
  "no single score" rule). It is reported here rather than deleted because the capability
  table names it as available — but a reader should know it is not part of any reported
  figure.

### Remaining issues and unverified requirements (after the verification audit)

| Item | State |
| --- | --- |
| Whole suite in ONE run on the final tree | **Unverified here** — host shared with other agents' pytest; the phase suite, `test_web_api.py`, the touched files, both mypy views, the reference check, the benchmark and the live/probe workflows were all re-run individually and pass. CI remains the gate. |
| CI (ubuntu, py3.12/py3.13, both mypy views, reference check) | **Not run in this session** — the one acceptance item this build reports as local-only, as every phase before it did. |
| The trajectory recorder consuming generalization events | **Deliberately not wired** (defect 10): the recorder observes the core task lifecycle, and forcing these runs in would create partial trajectories. Documented as a boundary, not promised as a feature. |
| A general ARC solver, a single intelligence score, model-proposed rules by default | **Out of scope by design**, reported as such in the capability table and the honesty flags. |
| The post-Phase-26 game-playing evaluation | **Not started** (contracts only) — the phase's own instruction. |

## 17. Provider and resource limitations

- **No provider is required.** The whole benchmark is deterministic; the benchmark script
  sets `NOVACONTROL_DISABLE_OLLAMA` / `NOVACONTROL_DISABLE_CLOUD` so a local model cannot
  make a measurement non-reproducible. `ProviderPosture` has a `declined` value for the
  case where a caller allows a provider and the machine has none.
- **`generalization.model_proposed_rules`** is `provider_dependent`: the vocabulary can
  carry a model-proposed rule (`model_proposal` source), but nothing in this build asks a
  model for one.
- **Resource:** the whole evaluation is CPU-bound and fast — 20 tasks in 22.5 ms, the
  largest single task (grid with 1,018 candidates examined) under 10 ms. The ceilings exist
  for pathological inputs (a 4,000-candidate search, a 300 s wall clock), not for the
  shipped corpus.
- **Host caveat:** on this machine the full-suite slice run is slower than the phase it
  measures (several co-resident agents/worktrees share the host). Any check that depends on
  free memory or wall-clock pacing is reported as a host artifact rather than a phase
  result.

## 18. Compatibility with Phases 8–25

- **Phase 8** — every verifier follows the planner's deterministic-verifier shape. The
  phase executes nothing the permission layer gates, and says so instead of claiming a
  check it does not make: its capability rows declare their permissions and its honest
  flag `bypasses_permissions` is false because no gated action is ever taken.
- **Phase 9** — the phase publishes 5 typed events through an injected observer seam (a
  publisher that swallowed its payload validation made an event silently disappear in an
  earlier phase; the reserved-key rule is respected here) and registers its 22 capability
  rows on the ONE registry.
- **Phase 12** — the pipeline's rule that a step must state what success looks like is why
  a task without a success condition is refused at registration.
- **Phase 14** — an unmeasured figure is `None` (`cell_accuracy` for non-grid families,
  `aggregate_score` everywhere, `latency_ms.min` for a family with no runs).
- **Phase 15** — a run's evidence is structured the way that layer structures evidence
  (frozen settings, a `reproduce` block per failure, a fingerprint over outcomes), and no
  second recorder was built. The trajectory recorder observes the core task lifecycle, so
  this phase does **not** claim its runs appear as trajectories — stated here because the
  first draft of this report claimed they did.
- **Phase 22** — the `state_transition` family is expressed in the world-state vocabulary.
- **Phase 24** — the `small_planning` family is a bounded search with the same ceiling
  discipline.
- **Phase 25** — the game-eval contracts read the embodied session record through an
  adapter; no environment, session or loop was duplicated.

## 19. Explicitly deferred: the post-Phase-26 game-playing evaluation

`game_eval.py` ships nine named contracts — objective completion, action efficiency,
observation freshness, recovery success, constraint violations, time to completion,
initial-condition spread, reproducibility and resource use — each with its definition, the
Phase 25 field it reads, and its limitations. `GET /generalization/game-eval` summarises the
sessions this build actually ran, **per environment and never pooled**, with `null` for a
figure the records did not measure and an explicit note when there is nothing to measure
(verified live: `sessions_measured: 0`, "which is an empty report, not a zero score",
`gameplay_claimed: false`). Running that milestone — a real game, a real player, real
gameplay claims — is **not** started by this phase.

## 20. Final acceptance checklist and verdict

| Requirement | Evidence | State |
| --- | --- | --- |
| Task definition with unseen examples | `validate_task` refusals; 39-task corpus; §4 | ✅ |
| Baseline established and compared | §8: identity 1/20 vs infer 16/20, task-by-task compare | ✅ |
| Structure identified, hypothesis formed, candidate generated | §5, §6; provenance `inferred` on every solved answer | ✅ |
| Solution verified | 7 verifiers, one per family; `ambiguous`/`unsupported` never counted as solved | ✅ |
| Performance measured per family | §7, §8; `aggregate_score: None` | ✅ |
| Failures classified | 12 categories; §10 grouping; recommendations with evidence | ✅ |
| Evidence recorded | run records, fingerprint, export payload, persistence, boot restore (§13) | ✅ |
| Re-evaluated after improvement | two-run comparison that never rewrites the baseline; regression reported honestly (§8) | ✅ |
| Held-out evaluation actually held out | three-way leakage guard; 422 refusal verified live (§9) | ✅ |
| No unsupported intelligence claims | 13 honesty flags false; capability table classified (§1) | ✅ |
| Automated tests | §12 — **86 passed / 838 subtests** on the phase suite (after the audit's fixes and its four new tests), every file touched by the final edits re-run green, both mypy views clean, reference in sync; the one-run whole-suite total is left to CI | ✅ with the caveat §12 states |
| Independent verification pass | §12/§15 — 59 audit probes over the shipped API (engine + real app + HTTP + a real Phase 25 session + restart); the first pass found 4 real defects, all fixed and pinned by `VerificationAuditTests` | ✅ |
| Real user-level workflows | §13: 90/90 boot 1, 7/7 boot 2, benchmark 10/10; the audit added the game-eval-over-a-real-session leg the fresh-install smoke could not reach | ✅ |
| Documentation | `docs/GENERALIZATION.md`, this report, `docs/API.md`, README/STATUS/DEVELOPMENT_LOG | ✅ |
| Post-Phase-26 gameplay | deliberately **not** started; contracts only (§19) | — |

**Verdict.** Phase 26 is implemented as a small, genuine, reproducible evaluation system
rather than a collection of intelligence claims: six families measured in their own units,
a hand-written held-out corpus, a bounded named operator library, ambiguity and
unsupported-ness kept as distinct endings, a leakage guard that refuses a dirty
measurement, failure classification with non-executing advice, reproducibility by outcome
fingerprint, and a comparison that never rewrites what it compares. Its limits — no general
ARC solver, no model, no aggregate score, a 39-task corpus — are stated wherever its
results are. The dedicated post-Phase-26 game-playing evaluation is defined by contract and
not begun.
