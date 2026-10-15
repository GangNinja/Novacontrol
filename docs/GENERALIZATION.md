# Generalization, ARC-style reasoning and intelligence evaluation (Phase 26)

`src/novacontrol/generalization/` measures whether a rule **inferred from examples
still works on an example that was never shown** — and refuses to call anything else
generalization. It is the last phase of the intelligence roadmap, and it is built on
Phases 8, 9, 12, 14, 15, 22, 24 and 25 rather than beside them.

One sentence for what it ships: a **deterministic, reproducible benchmark with a
hand-written held-out answer key**, a named operator library that a bounded search
may compose, six task families with their own success conditions and verifiers, three
baselines, a leakage guard that refuses a dirty split, failure classification with
non-executing recommendations, and a per-family report that deliberately has **no
single intelligence score**.

## The cycle

Every arrow the phase names is a record, not a slogan:

    define task            EvaluationTask — request, success condition, verifier, examples
    establish baseline     baseline_id on the run (identity / random_rule / no_answer)
    present unseen example test_input, whose expected_output the SEARCH never sees
    observe input          grid parsing / sequence reading / scene parsing
    identify structure     GridObject, BBox, per-field tables, token weights
    form hypothesis        CandidateRule — operators composed to depth ≤ 2
    generate candidate     the rule's prediction for the unseen input
    verify solution        the task's own verifier (7 of them, see below)
    measure performance    FamilyMetrics per family: solve rate, held-out rate, cell accuracy
    compare against base   compare_results over the two stored runs, task by task
    classify failure       FailureCategory — 12 named causes, never one bucket
    record evidence        the run, its fingerprint and the export payload
    re-evaluate            a later run over the same task ids, compared with the first

A rule is believed only after reproducing **every** example exactly. Two rules that
both fit and disagree produce `ambiguous`, never a guess. Anything the library cannot
express produces `unsupported`, never an answer that looks like one.

## The corpus

39 hand-written tasks with hand-written answer keys (`CORPUS_VERSION =
phase26.corpus.1`): **19 fitting** (train) and **20 held-out**. A task carries a
request, a success condition, its examples, the unseen input, the expected output, a
split, a difficulty and a rule family. `validate_task` refuses a task with no id, no
request, no success condition, no unseen input, no answer key, an unknown verifier or
an empty example set — and the refusal names which rule refused it.

| family | what it measures | verifier |
| --- | --- | --- |
| `grid_transform` | ARC-style grids: infer a transformation from 2–3 examples and apply it | `exact_grid_match` |
| `spatial_relation` | geometry over a parsed scene: left of, above, touching, inside, larger than | `relation_holds` |
| `state_transition` | predict the next structured state, one inferred rule per field | `state_equals` |
| `small_planning` | bounded route search on a grid, or a proof that no route exists | `plan_reaches_goal` |
| `tool_selection` | choose a tool for a request, learned from labelled examples | `tool_matches` |
| `multi_step_reasoning` | continue a sequence whose rule is only shown, never stated | `chain_matches` |

## The operator library and the search

22 pure operators (`identity`, `rotate`, `flip`, `transpose`, `anti_transpose`,
`translate`, `recolor_map`, `recolor_all`, `keep_largest_object`,
`keep_smallest_object`, `crop_to_content`, `extract_color`, `select_color_subgrid`,
`tile`, `mirror_tile`, `gravity`, `move_object`, `count_objects`, `count_colors`,
`count_non_background`, `fill_holes`, `outline_objects`) composed to a depth ceiling
of 2. An operator is a function of the input grid and its parameters: **no task id, no
answer, no corpus knowledge lives in one**. Parameters are proposed from the examples
(colours present, the sizes that fit, offsets that work) and every candidate is checked
against every example before it is allowed to predict the unseen input.

Ceilings are clamped, never raised: `max_candidates` 4,000, `max_depth` 2,
`max_seconds` 300, `max_tasks_per_run` 200, `max_results_kept` 400, `keep_runs` 50
(`HARD_MAX_GENERALIZATION_*`), and a configuration may ask for less. A search that runs
out of budget reports `budget_exhausted` for that task rather than a slow answer.

## Baselines, comparison and reproducibility

Three baselines ship: `identity` (answer the input), `random_rule` (a seeded rule) and
`no_answer`. Candidate and baseline run over the **same** task ids in the same run, so
a comparison is like-for-like. `compare` reads two stored runs and reports `improved`,
`regressed`, `appeared`, `disappeared`, `unchanged` — a task only one run evaluated is
reported as appearing, never as a movement, differing seeds are called out, and the
baseline record is never rewritten.

A run carries an **outcome fingerprint**: a hash of what decides its numbers
(task id, status, candidate, source, prediction, exact match) and of nothing else —
durations, timestamps and result order are excluded on purpose, so two identical
evaluations reproduce the same fingerprint with different run ids.

## The leakage guard

A held-out task is only held out if its answer was never available while fitting. Three
checks run before every evaluation and are reported separately:

1. `overlapping_ids` — the same task id, or the same question and answer, in both splits;
2. `shared_answers` — a held-out (input, answer) pair already present among the fitting tasks;
3. `used_answers` — a held-out example flagged `seen`, the solver's own record that its
   answer was used while tuning.

`clean` is `None` only when the check could not run (no held-out task at all), never when
it found nothing. With `require_clean_splits` (the default), a run over a dirty split is
**refused with 422** — a number measured over leaked examples is not a generalization
measurement.

## Failure classification

Twelve named causes, because a solver's ending cannot say which one happened:
`input_parsing_failure`, `invalid_task_definition`, `no_candidate_rule_found`,
`multiple_plausible_rules`, `rule_fits_train_fails_test`, `transformation_defect`,
`verification_failure`, `resource_budget_exhausted`, `provider_unavailable`, `timeout`,
`data_leakage`, `evaluation_infrastructure`. Ambiguity is not failure, a defective
transformation is not a wrong answer, and a broken evaluation SETUP is reported as
itself rather than counted as a model miss.

`recommendations` turns recurring categories into advice that names the tasks, the
families and the evidence behind it — and **executes nothing**: no training run, no
dataset, no model, no promotion.

## Persistence and restart

Runs persist through the application's `JsonStateStore` under one key
(`generalization_runs`), bounded by `keep_runs`, and a boot restore reports how many came
back and how many were skipped. **Registered tasks do not persist**: a registration is a
definition for the life of the process, and the corpus a number was measured over is the
built-in one the version names.

## API

17 routes, all `no-render` in the consumer contract (there is no panel yet): `GET
/generalization/{status,overview,families,operators,tasks,tasks/{id},splits,runs,runs/{id},
runs/{id}/export,compare,failures,recommendations,game-eval/contracts,game-eval}` and
`POST /generalization/{tasks,evaluate}`. The task LIST withholds the answer key; the
single-task view is where a key lives; a run list carries trimmed rows; the run view and
the export carry the per-task results.

## What this phase does NOT do

- **No general ARC solver.** The grid capability is a named, enumerated library; a task
  outside it is reported `unsupported`. `generalization.arc_general_solver` is
  `partially_implemented` and says so.
- **No single intelligence score.** The families are measured in different units, so an
  aggregate would hide a family failing behind a mean. `aggregate_score` is `None` and
  `single_intelligence_score` is `unavailable`.
- **No model takes part by default.** No rule is proposed by an LLM
  (`generalization.model_proposed_rules` is `provider_dependent`), nothing trains, no
  weight changes, nothing is downloaded, and no candidate is promoted from a finding.
- **No synthetic or mock result is labelled real** (`generalization.synthetic_results` is
  `unavailable`).
- **No gameplay is claimed.** The nine game-evaluation CONTRACTS ship as definitions and
  an adapter over Phase 25's session record; running them is the post-Phase-26 milestone.
  `python scripts/benchmark_generalization.py` is the benchmark — not a game.

## Measured on this machine

Corpus 19 fitting / 20 held-out, leakage clean. Candidate `infer` over the held-out split:
**16/20 solved, 2 ambiguous, 2 unsupported, 0 failed, 22.5 ms**, fingerprint
`5765cdb7…`. Baseline `identity`: **1 solved, 10 unsupported, 9 failed**. `compare`:
16 improved, 1 regressed (`ho_ambiguous_double_reading`, where the identity baseline
trivially echoes the input the answer happens to equal — reported, not hidden), 3
unchanged, `baseline_preserved: true`. Grid family: 8/10 solved at 0.89 cell accuracy
after examining 1,018 candidates. Boot 2 restored 4 runs, 0 skipped.

## Running it

    # the whole suite
    PYTHONPATH=src python -m pytest tests/ -q

    # the phase's own suite
    PYTHONPATH=src python -m pytest tests/test_generalization.py -q

    # the benchmark: 10 self-checked scenarios over the real HTTP API
    PYTHONPATH=src python scripts/benchmark_generalization.py          # human summary
    PYTHONPATH=src python scripts/benchmark_generalization.py --json   # machine-readable
