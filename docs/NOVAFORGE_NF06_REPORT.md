# NovaForge NF-06 — Forge Coach and Controlled Experiments

**Verdict: PASS WITH LIMITATIONS.**

NF-06 adds the layer that reads what NF-01…NF-05 recorded and turns it into three honest answers:
**what happened** (findings with provenance), **what the earliest supported failure is** (a candidate in
sequence order, never a cause), and **what is worth trying next** (a ranked recommendation that can become
a controlled experiment). An experiment runs both arms through the existing NF-05 evaluation lab, one
variable at a time, with the approval bound to the exact configuration it was given.

> **UI correction, 2026-10-20.** The sidebar `Forge Coach NF-06` destination this phase added has been
> **removed**: it opened the same `novaforgePanel` as the `NovaForge` entry, so the Coach lives only in its
> **Forge Coach** tab. The tab strip was also made to wrap, because with the sidebar entry gone an
> off-screen tab would have been the only route to it. See
> [NOVAFORGE_VERIFICATION.md](NOVAFORGE_VERIFICATION.md) §8.3.

The limitations are structural and are stated everywhere they matter: **no environment is controllable and
no objective verifier exists**, so no analysis, recommendation, experiment or verdict in this build claims a
gameplay improvement, a score, or a completed objective. NF-06 invokes no model, trains nothing, changes no
weights, applies no configuration on its own, and sends no input. Nothing here was measured against a game:
the live verification observed the operator's own Notepad window, exactly as NF-05 did.

> Report path: `docs/NOVAFORGE_NF06_REPORT.md`

---

## 1. What NF-06 adds, and what it refuses

| Surface | What it does | What it refuses |
|---|---|---|
| **Analysis** (`novaforge/coach.py`) | Reads the recorded session, telemetry, timeline, benchmark run and evaluation; recomputes findings on every read | Caching a diagnosis, observing a live session, modifying any record, claiming an objective |
| **Finding** | Names severity *with its basis*, evidence strength *with its basis*, the classification (observed / derived / hypothesis / unknown) and the evidence refs | A finding with no evidence, a severity with no basis, a hypothesis dressed as a fact |
| **Earliest recoverable error** | The first recorded failure signature in `seq` order, with later symptoms, counter-evidence and alternatives | Calling it the cause: `causation_claimed` is always `false` and the reason says order is not causation |
| **Recommendation** | One row per finding category, ranked by severity then evidence strength, naming the metric to observe and the criterion that would count | A recommendation whose metric this build cannot measure (the planner refuses it), or one that promises a score |
| **Experiment** | `repeat` or single-variable `configuration_change`, both arms ordinary NF-05 evaluations, approval bound to the spec digest | Running without approval, running a changed configuration, two experiments at once, a destructive or unbounded run |
| **Verdict** | `improved` / `regressed` / `inconclusive` / `not_comparable`, with the sample, the guardrails and the reasons | An improvement claim without a compatible comparison, ≥2 eligible runs per side, a delta beyond the threshold and no breach |

A session with nothing recorded returns `insufficient_evidence` **with what would help**, rather than a
diagnosis built from nothing. That is an answer, not an error.

## 2. Architecture

NF-06 adds two modules and composes everything else:

```
session / evaluation record ─┐
NF-02 telemetry snapshot ────┤
NF-03 evidence timeline ─────┼──►  coach.analyse()  ──►  findings ──►  recommendations
NF-04 benchmark run ─────────┤        (read-only)                        │
NF-05 evaluation record ─────┘                                           ▼
                                                        experiments.propose()   (spec + digest)
                                                        experiments.approve()   (digest bound)
                                                        experiments.run()       (approval revalidated)
                                                                 │
                                            baseline arm ──► NF-05 evaluation ──┐
                                            candidate arm ─► NF-05 evaluation ──┴──► NF-04 compare
                                                                                     │
                                                                        results / verdict / limitations
```

No second session manager, benchmark lab, evaluation lab, verifier or retry loop was introduced. The Coach
holds **no** execution path: `coach.py` imports neither the manager's lifecycle nor the lab's `start`.

## 3. Analysis (`novaforge/coach.py`, 1 975 lines)

* **Vocabulary, declared once**: `FINDING_KINDS` = observed / derived / hypothesis / unknown,
  `EVIDENCE_STRENGTHS` = strong / moderate / weak / insufficient, `SEVERITIES` = critical / high / medium /
  low / info. `Finding.validate()` refuses a finding with no title, no explanation, no severity or
  confidence *basis*, an unknown band, or an observed finding with no evidence reference.
* **Failure signatures**: eight recorded signatures (`capture.failed`, `target.lost`, `target.changed`,
  `target.unreadable`, `session.failed`, `timeline.persistence_failed`, `error.recorded`,
  `frame.rejected`) each declare their category, severity and kind in one table.
* **Configured bounds are not faults**: NF-01 writes the time-limit notice into the record's error list on
  purpose, and NF-03 records it as `error.recorded`. The Coach filters exactly `tellocation == "limits"`
  (`_BENIGN_ERROR_WHERE`) out of its failure analysis, because retelling an intended ending as a fault would
  invent a bottleneck. The time limit still produces a *derived* finding of its own.
* **Evidence is references, never payloads**: an event reference carries `seq`/`event_id`/`type`/`summary`;
  a metric reference carries the key and value; a record reference is a path. A frame is cited by sequence
  number and resolved on demand — never embedded.
* **Missing is `None`, never zero**: every metric the analysis reports is either measured or absent with a
  reason (`latency_median_ms` with fewer than the scenario's samples is unavailable, not 0 ms).
* **The subject**: session id, evaluation id, environment, scenario, the **pinned target window** (title,
  handle, pid, geometry), state, end reason, mission, created-at — plus the evidence counts, the
  classification and the limitations that apply to *this* subject.
* **Determinism**: nothing derived is cached, and the finding ids are digests over the subject and the
  category, so two analyses of one unchanged record agree — verified (benchmark script reports
  `deterministic_findings: true`).

## 4. Recommendations (`RECOMMENDATION_RULES`)

One row per category — **14 rows** (a count this audit corrected; the report had said 16 while its own list
below named 14 rule categories, and `RECOMMENDATION_RULES` declares exactly 14 `_Rule` entries) — each
declaring the action, the expected measurable effect, the NF-04 **metric** to observe, the success criterion,
the risk, the effort and the experiment shape:

`capture_failure`, `stale_observation`, `observation_discontinuity`, `capture_latency`, `target_loss`,
`target_changed`, `persistence_failure`, `unexpected_exception`, `error_recorded`, `time_limit_reached`,
`guardian_cancellation`, `insufficient_frames`, `incompatible_baseline`, `single_sample`, plus the
structural `objective_verifier_missing` (no rule — a missing capability is not a testable next step).

* Ranking is `(severity weight, evidence strength weight, category)` — deterministic, never random.
* A recommendation id is a **digest** (`rec-` + 12 hex characters), so the operator's decision survives a
  reload and a restart; `set_recommendation_status` accepts only that shape and stores **status only**.
  Recommendation text is regenerated by each analysis, so a stale diagnosis cannot outlive its evidence.
* `guardian_cancellation` deliberately observes `frames`, not `interventions`: `interventions` exists in the
  NF-04 catalog but no scenario collects it, so it cannot decide an experiment.
* A recommendation whose metric no runnable scenario measures is refused by the planner instead of being
  proposed with an unmeasurable criterion.

## 5. Controlled experiments (`novaforge/experiments.py`, 1 999 lines)

* **Kinds**: `repeat` (the same configuration; `change` must be empty) and `configuration_change` (exactly
  one declared variable, and the `change` must set exactly that variable).
* **Approval is bound to the configuration**: `approve` records the spec's digest; `run` recomputes it and
  refuses when the configuration changed after approval. Editing the proposal means approving again.
* **Revalidation is a refusal, not a warning**: `run` asks the evaluation lab's preflight again (target
  identity and uniqueness, scenario, budget, guardian controls, permission layer, the one-session slot) and
  moves the record to `blocked` with the failing checks and their reasons when the world changed. A refusal
  does **not** revoke the approval — the configuration never changed — so the same approved experiment can
  run again once the window is back or the conflicting session ends; the panel shows the refusal reasons and
  keeps Run available for exactly that retry. A proposal blocked for its own validation errors was never
  approved and can never run.
* **One at a time**: the lab holds the same single session slot the rest of NovaForge uses; a second start is
  a conflict (409 over HTTP).
* **Bounded**: per-arm ceiling derived from the budget (`ARM_SLACK_SECONDS`), per-experiment ceiling
  (`MAX_TOTAL_SECONDS = 900`), repeats per arm `1…MAX_REPEATS`, and the whole thing cancellable through the
  arm's own evaluation stop path.
* **Values come from records, not imagination**: a `configuration_change` derived from the
  `incompatible_baseline` recommendation takes **both** numbers from the two recorded runs — the subject's
  own configuration and the historical baseline NF-05 kept for it. When the two do not differ on the
  declared variable, the proposal is retained **blocked** with that reason rather than being built around an
  invented value.
* **Assessment**: `assess()` compares the arms through NF-04's own rules and returns `improved` / `regressed`
  / `inconclusive` / `not_comparable`, with the sample (runs and eligible runs per side), the primary and
  secondary metric rows, the guardrails, the comparison reasons, the failed/aborted/invalid runs and the
  limitations. `improved` requires a compatible comparison, ≥2 **eligible** runs per side (a failed or
  invalidated attempt is one measurement, not two), a delta beyond the declared threshold in the declared
  direction and **no** guardrail breach.
* **A refused comparison is labelled as one**: when NF-04 returns a row with no delta, the metric's
  `source` is `per_arm_description` and the reason says the figures are each arm's own description, *not an
  effect* — `certified` is false. (This was a real defect; see §11.)
* `gameplay_claimed`, `objective_verified` and `improvement_claimed` are `false` on every verdict this build
  can produce, and a repeat of one configuration reports `limited_to_single_run`.

## 6. Persistence

* Namespaces: `novaforge_coach_recommendations` (statuses only, capped) and `novaforge_experiments`
  (proposals, approvals, arms, results, history; 60 kept).
* Every write is read back and reported: `persistence: {state: verified | failed | not_persisted, reason}`.
  A store that refuses the write is reported, never hidden — and a recommendation decision still returns to
  the caller with that state.
* Restart: a new Coach reads the stored statuses, and a new experiment lab reads its experiments; a record
  left `running` is **not** resurrected (the lab has no live task after a restart). Verified by the
  benchmark script's restart pass (`restart_experiments: 2`, one approved status persisted) and by tests.

## 7. Safety and permission

* `novaforge.experiment` is declared to the one `PermissionManager`; the lab asks it again at the moment of
  the run, so a stored approval cannot outlive a changed decision. A refused permission blocks the run and
  is recorded.
* `run` never bypasses the evaluation lab, so Guardian limits, the emergency stop, the input boundary and
  input cleanup are the session manager's own — NF-06 adds no way around them.
* NF-06 invokes no model and trains nothing: the Coach's capability card states it, the panel repeats it, and
  no code path in either module constructs an LLM provider or writes a weight.

## 8. API

Twelve routes, registered in all five places (handler, `ApiSurface`, route consumers, `docs/API.md`, tests):

```
GET  /novaforge/coach
POST /novaforge/coach/analyse
GET  /novaforge/coach/recommendations
POST /novaforge/coach/recommendations/{recommendation_id}
GET  /novaforge/experiments
POST /novaforge/experiments
GET  /novaforge/experiments/{experiment_id}
POST /novaforge/experiments/{experiment_id}/approve
POST /novaforge/experiments/{experiment_id}/reject
POST /novaforge/experiments/{experiment_id}/run
POST /novaforge/experiments/{experiment_id}/cancel
GET  /novaforge/experiments/{experiment_id}/results
```

Errors are specific: unknown session/evaluation/recommendation → 404; an analysis with no subject, an
unknown status, or a blocked approval → 422; a second concurrent run → 409. `python
scripts/generate_api_reference.py --check` reports **docs/API.md in sync**.

## 9. Panel

A new **Forge Coach** tab (and an enabled `Forge Coach NF-06` sidebar destination):

* a read-only banner that states the Coach changes nothing and invokes no model;
* subject pickers (evaluation preferred — it carries its benchmark run — then session), **Analyse Evidence**;
* the subject card, the earliest-recoverable-error card, the findings list (expandable, severity /
  classification / evidence-strength chips, evidence links that open the event in the Evidence Timeline) and
  the ranked recommendations with **Propose Experiment** plus operator-decision buttons;
* the experiment card: the full spec (kind, variable, both arm budgets, repeats, metric and direction,
  threshold, baseline runs, parent finding and recommendation, guardrails, abort conditions, approval), the
  arms table, and the results card (verdict banner, sample, primary row, secondary metrics, guardrails,
  comparison reasons, failed runs, limitations) with JSON export;
* the experiment history (every proposal, approval, rejection, run, cancellation and result) with
  click-to-inspect;
* **Propose is disabled** for a recommendation whose experiment this build cannot run, with the reason on the
  button and under it.

## 10. Live verification (this machine, 2026-10-18)

Server: `python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001`
(restarted twice so the reloaded code and assets were the ones under test). Nothing was launched, no game
was started, no gameplay input was sent, and no model was invoked.

| Step | Observed |
|---|---|
| `GET /novaforge/coach` | capability card: read-only analysis, the four finding kinds, the seven statuses, four limitations |
| Analyse a recorded evaluation | `4 finding(s)` — frame gaps (derived, weak), time limit (derived, moderate), refused baseline comparison (observed, strong), no objective verified (observed, strong); `causation_claimed: false` |
| Subject card | session `5bf5fed3e4dc…`, evaluation `8338f6c6ad1c…`, `Disposable Test Window`, `observation-baseline@1`, **target `start.ps1 - Notepad` handle `1049688` pid `32468`**, `STOPPED · time_limit`, 15 recorded events, 6 frames, `classified · time_limit` |
| Recommendations | 3, ranked 1–3, each naming its metric, criterion, risk, effort and preconditions |
| Propose (before the fix) | **blocked**: "The change must set exactly the declared variable ('duration_seconds'); got []" — a real defect (§11) |
| Propose (after the fix) | `proposed · awaiting approval`; baseline arm **3 s** (the recorded baseline's basis), candidate arm **5 s** (the subject's), baseline run `5b420b7c9f30…`, threshold 0, abort 33 s/arm |
| Approve | `approved · not started`, "bound to configuration `7806e033fe93a8ff`" |
| Run | `running` → `completed`; both arms `completed` as real NF-05 evaluations of the open Notepad window |
| Verdict | `NOT_COMPARABLE` — "The runs used a different execution budget (3 vs 5); their measurements are not on the same basis." `improvement_claimed: false`, `gameplay_claimed: false`, `objective_verified: false`; primary row `frame_yield` 0.571 → 0.667 labelled *per-arm description (not an effect)* |
| Console | no errors |

The live pass is what found three of the six defects below; none of them was visible from the tests alone.

## 11. Defects found during verification, and the fix for each

1. **A refused comparison was labelled as an NF-04 comparison.** `_metric_row` set
   `source: "nf04_comparison"` whenever NF-04 returned a row, including rows where it recorded **no delta**
   ("the runs are not compatible"). The UI then printed "NF-04 certified comparison" for figures NF-04 had
   explicitly refused to certify. Fixed: a row with no delta is `per_arm_description` and its reason now says
   the figures are each arm's own description, *not an effect*. Pinned by
   `test_a_configuration_change_reports_not_comparable_with_nf04_reasons` and by the live run above.
2. **The subject's target identity was dropped.** `SessionRecord.target` is a `TargetWindow` dataclass, not a
   mapping, so reading it with `_mapping()` produced `{}` and the panel said **"Target not recorded"** about
   an identity the record held. Fixed with `_target_identity()` (mapping → `to_dict()` → `{}`), pinned by
   `test_the_subject_reports_the_pinned_target_identity` and by the live subject card.
3. **The analyse badge claimed approval was required.** "ready · approval required" sat under *Analyse a
   Recorded Run*, but analysing is read-only — approval belongs to running an experiment. Fixed to
   "ready · read-only".
4. **The `incompatible_baseline` recommendation produced a blocked, un-approvable proposal.** A
   `configuration_change` requires a value, and the recommendation named only the variable, so every Propose
   from that card created a `blocked` record (live: experiment `d662fa6b45f8`). Fixed twice over: the Coach
   now declares `change_from: recorded_baseline` for that category, and the planner derives **both** values
   from the two recorded runs — the subject's own configuration and the recorded baseline NF-05 kept — with
   the baseline run id recorded as the historical reference. When no recorded difference exists, the
   proposal is blocked *with that reason* instead of the bare validation error. Pinned by
   `test_an_incompatible_baseline_becomes_a_change_taken_from_recorded_runs` and
   `test_a_recorded_change_with_no_measured_value_is_refused_with_a_reason`, and verified end to end live.
5. **The rule's success criterion was unreachable.** `incompatible_baseline` promised "the reason list is
   empty", but NF-04 never certifies a delta across a budget or interval change — so the criterion could
   never be met by the experiment the rule proposes. Rewritten to the achievable, honest outcome: both arms
   complete, each arm's own value is reported, and the basis difference is stated rather than pooled.
6. **Three mypy errors** in the new modules (`no-redef` in `coach._persist`, `float(Any | None)` and a
   possibly-`None` benchmark in `experiments.py`), plus three stale UI pins that still asserted NF-06 was
   unimplemented (the roadmap card, the count of planned nav entries, and the sidebar expectations). All
   fixed; both mypy views are clean and the UI suite reflects the implemented reality.

A seventh, minor finding is in the benchmark script rather than the product: it proposed experiments without
declaring an arm budget, so the spec's 10 s default applied and ~20 s of real evaluation was measured as
"overhead". The script now declares the budget it measures.

Found in the acceptance pass of 2026-10-18 (same day, after the live run):

8. **The “≥2 eligible runs per side” gate counted attempts, not eligible runs.** `assess` reported
   `eligible_baseline`/`eligible_candidate`, but `_verdict` read the total attempt counts, so an arm that
   tried twice with one run invalidated (a capture failure) still satisfied the gate and could claim
   `improved` from a single value. Fixed: the gate and the `improved`/`regressed` reason text use the
   eligible counts, and the limitation line says “fewer than two eligible runs”. Pinned by
   `test_an_invalidated_attempt_is_not_a_second_measurement` (an all-eligible sample still reaches the
   threshold gate, so the fix cannot silence real verdicts).
9. **A readiness refusal was a dead end.** `run` moved the record to `blocked`, `approve` refused every
   blocked record and `run` required `approved` — so after reopening the window the operator could not
   retry the approved configuration and had to propose a duplicate. Fixed: a blocked record with **no**
   proposal errors is a run-time refusal, its approval still binds the digest, and `run` accepts it again;
   a proposal blocked for its own errors is refused with those reasons. Pinned by
   `test_a_readiness_refusal_does_not_revoke_the_approval` and
   `test_a_proposal_blocked_for_its_own_errors_is_never_retried`.
10. **The run-time refusal reasons were invisible after a reload.** The API kept `preflight.reasons`, but the
    panel rendered only `record.errors` (proposal validation) and the badge; the reason existed only in the
    transient toast. Fixed: `renderExperimentSpec` renders “Why the run was refused” for a blocked record,
    the badge reads “blocked · the refusal reasons are below”, and `experimentCanRun` allows the retry while
    `experimentCanApprove` stays disabled. Pinned by the Node DOM pin
    `test_a_run_time_refusal_keeps_its_reasons_visible_after_a_reload` and by the decision pin
    `test_a_run_time_refusal_keeps_the_approval_but_a_proposal_block_does_not`.
11. **A machine-speed-dependent test pinned a 2 s budget** against `capture-latency`'s 20-frame validity
    floor; on this host it recorded 17 frames and invalidated the arm, so a passing assertion depended on
    the machine being fast. The fixture now uses a 4 s budget (the comparison it asserts is still
    `not_comparable` for the observation interval, not for a short run). Test-only fix; no product change.

## 12. Performance (`scripts/benchmark_novaforge_coach.py`)

Deterministic 320×200 PNG capture seam, no game, `--rounds 5 --duration 1`; medians shown.

| Path | Measured |
|---|---|
| Coach capability card | **0.008 ms** |
| `analyse` (full, on a completed evaluation: 15 events, 6 frames, telemetry, run, model record) | **5.04 ms** median (4.28–8.62) |
| Recommendation status write (including the verified store write) | **17.7 ms** |
| `recommendations(limit=50)` | **0.022 ms** |
| `propose` (spec + digest + persist + read-back) | **22.2 ms** |
| `approve` (digest bind + persist + read-back) | **15.6 ms** |
| Experiment wall time, 2 arms × 1 s budget | **2 478 ms** (≈239 ms over the two budgets: preflight, session teardown and persist) |
| `get` / `history` / `results` | **0.003 / 0.075 / 0.001 ms** |
| Restart restore (new Coach + lab) | **1.82 ms** |
| Snapshot size | recommendations **721 B** (1 status), experiments **37.7 KB** (2 experiments ≈ 19 KB each; ≈1.1 MB at the 60 cap) |

Analysis is linear in what the record holds and comfortably under the panel's interaction budget; the
persist-bearing writes (17–22 ms) are the ones a user waits on, and they are admitted by the panel's
disabled-while-busy buttons.

## 13. Tests and checks (exact results)

| Check | Command | Result |
|---|---|---|
| Coach unit + experiment lifecycle | `pytest tests/test_novaforge_coach.py -q` | **47 passed, 19 subtests** (3 min 45 s) |
| NF-06 HTTP surface | `pytest tests/test_novaforge_experiment_api.py -q` | **9 passed** |
| Panel contract, decisions, renderers | `pytest tests/test_novaforge_ui.py -q` | **86 passed, 41 subtests** (19 of them NF-06) |
| NF-01/NF-03 regression | `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_timeline.py -q` | **147 passed** |
| NF-04 regression | `pytest tests/test_novaforge_benchmark.py tests/test_novaforge_benchmark_api.py -q` | **71 passed, 97 subtests** |
| NF-05 regression | `pytest tests/test_novaforge_evaluation.py tests/test_novaforge_evaluation_api.py -q` | **65 passed, 26 subtests** |
| Route contract + web platform | `pytest tests/test_route_shape_contract.py tests/test_web_platform.py -q` | **63 passed, 446 subtests** |
| API reference | `python scripts/generate_api_reference.py --check` | **in sync** |
| Types | `python -m mypy src` and `--platform win32` | **no issues, 426 files** each |
| NovaForge tests collected | `pytest <ten novaforge files> --collect-only -q` | **420 tests** |

The renderer pins are behavioural, not textual: `renderCoachAnalysis`, `renderExperiment`,
`renderExperimentResults` and the pure decision helpers are evaluated in Node against a minimal DOM shim, so
"insufficient evidence is stated", "a candidate is never called a cause", "a refused comparison is a
description" and "Run is disabled without a matching approval digest" are assertions about the nodes built,
not about source text.

## 13a. Acceptance pass, 2026-10-18 (after the live run)

Run scope: the NF-06 files first, then only the dependency/integration files its code or panel touches.

| Check | Result |
|---|---|
| `pytest tests/test_novaforge_coach.py tests/test_novaforge_ui.py -q` | **133 passed, 60 subtests** in 224 s (47 coach + 86 panel, 19 NF-06 panel pins; includes the 3 new experiment pins and 2 new panel pins). Coach alone re-measured after the final edit: **47 passed, 19 subtests** in 225 s |
| `pytest tests/test_novaforge_experiment_api.py -q` | **9 passed** in 152 s |
| `pytest tests/test_novaforge_api.py -q` | **31 passed** in 394 s (NF-01 regression, run separately after the combined command hit the tool ceiling) |
| `pytest tests/test_route_shape_contract.py -q` | 13 of 18 passed inside the tool window; the file is slow, not stuck — see below |
| `python -m mypy src` | **Success: no issues found in 426 source files** |
| Served panel after the edit (`/static/js/novaforge.js` on the live preview) | the new branch, badge text and refusal block are served; page loads with **no console errors**; a synthetic readiness-blocked record renders **Run enabled, Approve disabled** and the reason text |

Defects fixed in this pass are §11 items 8–11. No behaviour was weakened to make a check pass: the two
new decision pins assert both sides (a readiness block still runs, a proposal block never does), and the
eligible-count pin also proves an all-eligible sample still reaches the verdict gate.

Checks that could not be completed in this pass, reported as limitations rather than passes:

* `tests/test_route_shape_contract.py` did not finish inside the tool window. That was a measurement
  artefact, corrected in the NF-01–NF-06 audit: run alone, `test_render_explore_fields_in_post_explore`
  passes in **46.1 s** (**34.2 s** on a first measurement) and
  `test_render_vision_click_fields_via_noop_runner` passes in **28.0 s** (**27.0 s** first time) (both
  boot the whole application and probe the live network/desktop, so each takes ~30 s). The file is slow,
  not hanging; its NF-06-relevant renderers passed. See
  `docs/NOVAFORGE_VERIFICATION.md` for the full-suite numbers.
* The combined `route_shape_contract + novaforge_api` command hit the tool's 600 s ceiling before either
  file finished; each was then run alone (NF-01 API: 31 passed; the route contract's stall is above).
* The live pass covers the panel rendering the refusal from a synthetic record; a live run-time refusal was
  not re-produced, because it needs a real window disappearing mid-approval (covered by the coach tests
  with the deterministic desktop double instead).

## 14. Failing / skipped / unverified requirements

* **No game, no gameplay input, no objective verification.** There is no controllable environment and no
  objective verifier in this build, so nothing NF-06 produced can claim a gameplay outcome. This is the
  phase's largest limitation and it is reported in every artifact.
* **A cross-basis experiment cannot be certified.** NF-04 refuses to pool runs recorded on a different budget
  or observation interval, so a `configuration_change` on those variables always ends `not_comparable` with
  per-arm figures. That is now stated in the recommendation's own success criterion — but it means NF-06
  cannot, in this build, produce a certified improvement.
* **Repeats beyond one per arm were not exercised live.** `repeat_count: 2` and a guardrail breach are
  covered by unit tests (`assess`), not by a live multi-run experiment; the live pass ran one run per arm.
* **The experiment cap and eviction were not exercised live** (60 experiments); the retention path is
  covered by unit tests.
* **Windows only, in practice.** The single-session slot, the stop path and the capture seam are the ones
  NF-01/NF-04/NF-05 already verify; NF-06 was verified on this Windows machine, and its own logic is
  platform-independent.
* **The panel cannot author an explicit spec.** A configuration change whose value the record cannot supply
  is blocked with a reason and must be proposed through the API; there is no value-picker in the UI yet.
* **No model was invoked and none is available**, so the analysis is comparison-and-arithmetic only; a
  language-model explanation layer is explicitly not part of this phase.

## 15. Known limitations

* Findings describe recorded evidence; they cannot observe a live session, and a hypothesis is never
  promoted to a cause by repetition of the wording.
* The analysis is bounded by design (`MAX_FINDINGS = 24`, `MAX_RECOMMENDATIONS = 12`,
  `MAX_LATER_SYMPTOMS = 8`) and the newest evidence is kept, so a very long session's earliest events can be
  outside NF-03's retention; the evidence counts say so.
* `insufficient_evidence` is an answer, not a failure — but it also means a thin session produces no
  diagnosis at all, which is the intended trade.
* Guardrails are defaulted per category and dropped when the chosen scenario cannot measure them; an
  explicitly supplied guardrail is kept so validation can refuse it with a reason.

## 16. Confirmation

* **No game was launched, prepared, driven or evaluated.** The only target observed was the operator's own
  Notepad window, through NF-05's existing evaluation path.
* **No gameplay input was sent** (no runnable scenario sends input; `input.available` is false everywhere).
* **No model was invoked, no weights were trained, and no configuration was applied** by NF-06.
* **Nothing was claimed that was not measured**: every figure in this report came from a command whose output
  is quoted above, and every unmeasured figure is reported as absent with its reason.
* Phase report path: `docs/NOVAFORGE_NF06_REPORT.md`.
