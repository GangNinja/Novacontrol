# NovaForge NF-10 — adaptive gameplay policy and evidence-based evaluation

**Date:** 2026-10-22
**Verdicts:**

| Area | Verdict |
| --- | --- |
| State-dependent decision-making (deterministic policy) | **PASS** |
| Objective verification (independent, from the target) | **PASS** |
| Safety and guarded execution (NF-07/NF-08 preserved) | **PASS** |
| Evaluation quality (baseline comparison, trial record) | **PASS WITH LIMITATIONS — 3 trials, deterministic task, no significance claim** |
| Live model-backed policy availability | **FAIL — no model chose an action; the deterministic policy decided every live trial** |
| Recovery after a controlled failure | **PASS** |

NF-10 asked for something narrower than "game intelligence": a policy that chooses
different actions for different observed states, a baseline to compare it against,
and honest measurements. All three exist and were exercised on the disposable
harness window with real injected keys and the real verifier. **No general gameplay
intelligence, no autonomous real-game capability and no model-backed decision is
claimed** — the deterministic policy is a rule over measured evidence, and it is
labelled as such everywhere it is reported.

---

## 1. Initial repository state

| Fact | Value |
| --- | --- |
| Branch | `main` |
| HEAD | `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`) |
| Working tree | dirty before this pass (uncommitted NF-07/NF-08/NF-09 work and user edits, 60 entries); NF-10 adds to it. No reset, stash, clean, commit or push. |
| Python | 3.13.0 (`.venv`, editable install) |
| Node | v24.19.0 |
| OS | Windows; the disposable WinForms harness is available; no game was launched |
| Local model | Ollama present, but NF-09's live inference on this CPU **failed** (empty answer at 194.9 s; timeout at 295.0 s) |
| Test runner | `pytest` with `PYTHONPATH=src` |

Read before changing anything: `docs/NOVAFORGE_NF08_REPORT.md`,
`docs/NOVAFORGE_NF09_REPORT.md`, the NF-07 verification report and gap audit,
`docs/STATUS.md`, `docs/API.md`, and the implementations of
`novaforge/observation.py`, `policy.py`, `closed_loop.py`, `objective.py`,
`manager.py`, `visual.py`, the application's loop wiring, the Forge panel and the
existing benchmark/evaluation/timeline modules.

## 2. Reuse / gap matrix

| Need | Existing facility | Gap | Decision |
| --- | --- | --- | --- |
| Observe the state | `ObservationBuilder` (title, OCR, frame freshness, NF-09 vision) | none | reuse unchanged; the loop still observes measured evidence only |
| Decide | `DecisionProvider` + `DecisionProposal` (NF-08) | one single-action rule | **extend** with a staged policy and a fixed-action baseline; the proposal gains version, `model_backed`, uncertainty and per-decision measurements |
| Execute | session `plan_action`/`execute_action`, digest, Guardian, input guard, budgets | none | reuse; the policy never executes |
| Verify | `WindowTitleVerifier` | none | reuse; the goal is `window_title contains COMPLETE` |
| Bound the run | `LoopLimits`, cancellation, deadlines | none | reuse |
| Supervise | `ClosedLoopRunner` | `verified_failure` after an action ended multi-step runs; policy stop with a failing verdict was reported as `stopped` | two scoped fixes (below), NF-08 behaviour preserved by default |
| Measure | `LoopResult` | no per-decision age/latency/evidence fields | add three measured fields |
| Aggregate trials | nothing existed for policy runs | — | add `policy_eval.py`: trial record + aggregates over `LoopResult`, no runner and no verdict of its own |
| Task | `scripts/forge_harness_window.ps1` (single READY state) | one fixed action always correct | **extend** with `-Task stages`; the NF-08 `ready` task is untouched |
| UI | Forge closed-loop card | no policy choice, no provenance | add a policy selector and report the policy, its version and whether any decision was model-backed |

There is still ONE loop and ONE guarded action path. `policy_eval.py` reads
`LoopResult`; it never runs anything.

## 3. The harness task

Extending the existing disposable window was practical, and it keeps the task on
the same observation/policy/action/verification interfaces as NF-08:

```text
NC FORGE HARNESS - STAGE IDLE     --A-->  STAGE ARMED
STAGE ARMED                       --B-->  STAGE COMPLETE     (goal)
STAGE FAULT                       --R-->  STAGE IDLE         (recovery)
any other gameplay key from IDLE/ARMED --> STAGE FAULT
```

| Property | Value |
| --- | --- |
| Initial state | `STAGE IDLE` (a freshly launched window, so every trial starts equally) |
| Permitted actions | gameplay key taps only (`a`, `b`, `r`, `space` for the baseline); the action policy still allows only its declared set |
| Transitions | the table above, entirely visible in the window title |
| Goal | window title contains `COMPLETE` |
| Timeout | session 120 s; loop deadline 60 s; observation bound 15 s |
| Failure conditions | wrong key → `FAULT`; `COMPLETE` is terminal |
| Recovery | `FAULT --R--> IDLE`, so the same run can recover instead of failing |

The state lives **only** in the window title, which the OS reports and the
verifier reads. The policy is not following a hidden script that the verifier is
unaware of: change the title out of band and the next decision changes with it.

Live acceptance inserts a controlled failure before the recovery trial: one
`space` tap through the same guarded plan/execute path puts the window in
`FAULT`, then the policy must recover and finish.

## 4. Policy design and provenance

`HarnessStagesPolicy` (v1, `policy.py`):

* Parses `STAGE <name>` from the measured title; a bare word elsewhere in a title
  is not a state.
* Chooses `a` in IDLE, `b` in ARMED, `r` in FAULT; returns **None** at COMPLETE
  because success is the verifier's verdict, not an action.
* Refuses on stale/degraded/unavailable observations, on an unrecognised stage, and
  after `stall_limit` identical observations ("repeating the action is not a
  decision").
* Each proposal carries the action, the reason, the observation's evidence
  references, the expected next title, the policy id and version, `model_backed`,
  and any uncertainty.
* **Vision is a fallback, never an override.** A model description is consulted
  only when the title states no stage; it must be a structured `interpreted`
  reading with no recorded conflict, and the resulting proposal is marked
  `model_backed: true` with the uncertainty that it rests on a claim about pixels.
  A measured title always wins.
* `capability()` reports `model_backed: false`, the stage→action table and the
  reason it is a rule, not a learned policy.

`FixedKeyPolicy` (v1) is the baseline: one key on every observation, no state
reading, no model, no expected outcome. It exists to be compared, not shipped.

**Provenance in the record.** `LoopResult` now carries `policy_version` and
`model_backed` (true only when at least one decision used a model output).
`LoopStep` carries `observation_age_seconds`, `decision_latency_ms` and
`evidence_count`. A trial is built from that record; nothing is re-derived from
logs.

### 4.1 Multi-step verification semantics (a real defect found by the task)

The NF-08 loop treated `verified_failure` after any executed action as the end of
the attempt. That is correct for a one-action policy and wrong for a staged
objective: every honest intermediate observation would fail the run. The loop now
honours `fail_on_unmet_verification` on the policy — default **True** (NF-08 and
the fixed baseline unchanged), **False** for `HarnessStagesPolicy`, whose objective
is expected to be unmet until COMPLETE. When a policy stops while the verifier has
just reported `verified_failure`, the run is reported **failed**, not `stopped`.

## 5. Baseline / candidate protocol and raw results

Identical initial conditions for both policies: a freshly launched window per
trial, the same target, the same objective (`contains COMPLETE`), the same limits
(8 actions, 60 s), the same guarded path and the same verifier. Three trials per
policy, plus one recovery trial for the candidate. Every failed trial would be
kept; none occurred for the candidate.

Live run on this machine (2026-10-22), from
`python scripts/novaforge_policy_acceptance.py --trials 3 --output data/novaforge_nf10_trials.json`:

| Policy | Trial | Outcome | Actions | Observed transitions | Elapsed |
| --- | --- | --- | --- | --- | --- |
| HarnessStagesPolicy | 1 | verified_success | 2 | 2 | 1.965 s |
| HarnessStagesPolicy | 2 | verified_success | 2 | 2 | 2.276 s |
| HarnessStagesPolicy | 3 | verified_success | 2 | 2 | 2.366 s |
| HarnessStagesPolicy (recovery) | 1 | verified_success | 3 | 3 | 2.695 s |
| FixedKeyPolicy | 1 | verified_failure | 1 | 1 | 1.529 s |
| FixedKeyPolicy | 2 | verified_failure | 1 | 1 | 1.589 s |
| FixedKeyPolicy | 3 | verified_failure | 1 | 1 | 1.096 s |

Aggregates (raw rows are in `data/novaforge_nf10_trials.json`, which is
runtime data and not committed):

| Metric | Candidate | Baseline | Delta |
| --- | --- | --- | --- |
| Trials | 3 | 3 | — |
| `verified_success` | 3 | 0 | +3 |
| `verified_failure` | 0 | 3 | −3 |
| Success rate | 1.0 | 0.0 | +1.0 |
| Actions executed (mean) | 2.0 | 1.0 | +1.0 |
| Actions refused (total) | 0 | 0 | 0 |
| Trials that DECIDED DIFFERENTLY after a transition | 3/3 | 0/3 | +3 |
| Trials with any observed state change | 3/3 | 3/3 | 0 |
| Elapsed mean / median | 2.202 s / 2.276 s | 1.405 s / 1.529 s | +0.797 s |
| Observation age (mean) | 0.216 s | 0.314 s | −0.098 s |
| Decision latency (mean) | 0.102 ms | 0.110 ms | −0.008 ms |
| Evidence coverage (mean refs/decision) | 3.0 | 3.0 | 0 |
| Model-backed trials | 0 | 0 | 0 |
| Unavailable metrics | none | none | — |

Interpretation, stated carefully:

* The candidate **responded to two transitions** (IDLE→ARMED→COMPLETE); the
  baseline did not read the state, pressed a key that is wrong in the task, and was
  put in FAULT. This demonstrates *state dependence*, not superiority over a tuned
  policy: the baseline is deliberately naive.
* The candidate is **slower by 0.80 s** because it executes two actions instead of
  one. More actions, not fewer, are what the task requires.
* `N = 3` on a deterministic task. No statistical significance is claimed and none
  should be read into the rates; the raw rows are preserved so a larger run can be
  added without rewriting the record.
* The baseline DID observe a state change (IDLE→FAULT, caused by its own wrong key)
  and one key press later a new decision was impossible — it had already stopped.
  `responded_to_transition_trials` therefore distinguishes the two columns above:
  the candidate made a **different second decision after** the change, the baseline
  made no second decision at all.
* **Resource consumption is not reported**: these runs had no reliable per-trial
  sampler for CPU/GPU/memory, so the figure is listed as unmeasured rather than
  printed as zero. The panel's telemetry card is per-session measurement, not a
  per-trial attribution, and it was not used to invent one.

Recovery trial detail: one wrong key through the guarded path → `STAGE FAULT`;
the policy proposed `r` (IDLE), then `a` (ARMED), then `b` (COMPLETE); the verifier
read COMPLETE. 3 actions, 3 transitions, 2.695 s, verified_success.

## 6. Safety evidence

* Every action in every trial went through `plan_action` (ActionPolicy, digest
  binding) and `execute_action` (Guardian, foreground check, input guard, budget);
  the policy only ever returned a proposal.
* Action refusals are terminal in the run (`refused`) and are counted; a denied
  action is not retried. Tested.
* Cancellation is checked before every observation, before planning, and by the
  input guard during an injection. Tested at the loop level; NF-08's live
  cancel-during-hold acceptance still passes unchanged.
* Stale/degraded observations stop the run before a plan exists. Tested.
* The goal is the verifier's: a run that ends `completed` without
  `verified_success` is recorded as **inconclusive**, and a verified failure is a
  failure even if the policy stopped for its own reason. Tested.
* `COMPLETE` is never an action trigger; the policy refuses to act once it reads it.
* The model fallback cannot bypass any gate, cannot choose an action from prose or
  a conflicted interpretation, and labels its decision.

## 7. Model / runtime availability

| Question | Answer |
| --- | --- |
| Was a model used for any live decision? | **No.** `model_backed_trials: 0` for both policies. |
| Is a model required for the policy? | No. The stage rule is deterministic; OCR/vision are optional evidence. |
| Is the vision fallback wired? | Yes — it consumes `GameObservation.vision` from NF-09 and refuses anything short of a structured, conflict-free claim. Exercised by unit tests with typed fake interpretations. |
| Is live model-backed policy selection available? | **No.** NF-09's live inference failed on this CPU (`qwen3-vl:4b` empty answer at 194.9 s; timeout at 295 s). No live model decision was attempted. **Verdict: FAIL** for model-backed policy availability, by absence of a working local model — not by substituting a mock. |
| Cloud models? | Reachable through the same provider gate, not configured or used (no credentials, no paid calls). |

## 8. UI, evidence and reporting

The Forge panel's Guardian tab exposes the run and the record; it does not re-derive either.

| Required exposure | Where it is |
| --- | --- |
| Policy name and version | The policy badge plus `Policy: <name> v<n>` on the run card; both read from the run's own fields. |
| Rule-based or model-backed | `deterministic decisions only` / `at least one decision was model-backed`, from `result.model_backed`, and per step from `proposal.model_backed`. |
| Observed state and freshness | Each step renders `state: <window title>`, the OCR text when present, a non-observed status, and `frame age: <n> s`. |
| Decision reason and evidence refs | Each step renders `Reason: …`, `Evidence: <ref> · <ref>`, and the decision latency. |
| Selected action and expected outcome | The step's action label and `Expected: <the next state the policy predicted>`, with `expectation met` / `expectation missed` from the post-action observation. |
| Execution result and subsequent observation | `plan:` / `execute:` statuses, and the next observation row the loop took. |
| Objective verification | The verifier's badge AND its own quoted verdict line (`Verifier: verified_failure — <reason>`), never the loop's stop reason standing in for it. |
| Metrics and trial history | A **Trial history** card: per-policy aggregates (trials, verified successes/failures, success rate, mean actions, mean elapsed, how many trials decided differently after a transition, model-backed count) and the last five trial rows — including refused and inconclusive ones. |
| Refusal and uncertainty reasons | The policy's `Uncertainty: …` lines, the loop's refusal reasons, and the trial row's stored reason. |

New read-only route `GET /novaforge/trials` returns the rows and aggregates recorded by this
process, and `POST /novaforge/sessions/{id}/loop` now also returns `trial` and `policy_summaries`
for the run it just made. The aggregates are produced by `novaforge/policy_eval.py` — the same
module the acceptance script uses — so the panel and the report cannot disagree about a number.
Trial history is **process-local and explicitly labelled as such**: it is capped at 50 rows, the
response says `durable: false`, and the durable record remains the acceptance script's JSON.

Nothing decorative was added: there is no confidence dial, no invented score, and a metric with no
samples is omitted rather than printed as zero. The observation-only and controlled-evaluation
workflows are untouched.

## 9. Learning and adaptation boundaries

No online learning, weight update or automatic configuration change was added.
The policy's only "adaptation" is the loop's normal behaviour: re-observe after an
action and decide from the new state. Any future bounded adaptation must run
through verified outcomes, keep the previous configuration for rollback, and re-run
this acceptance protocol; inventing an unverified learner to justify the title was
explicitly not done.

## 10. Tests and commands

New `tests/test_novaforge_policy.py` (33 tests) covers:

1. each stage selects its own action;
2. a state transition changes the next decision;
3. stale/unreadable observations cannot trigger an action;
4. proposals pass through plan/execute (the stub gateway records the exact
   payloads and a refusal stops the run);
5. a failed action ends in a refusal, not a retry;
6. a completed run requires the verifier's `verified_success`;
7. cancellation stops before the next action, AND a policy that never decides is
   bounded by `decision_timeout_seconds` (a cancel during the wait is reported as
   `canceled`, not as a timeout);
8. a policy that raises, or returns a malformed proposal (no action, no kind, no
   reason, a bare dict), is reported as `error` and **nothing is planned**;
9. target loss after an action stops the loop before another decision;
10. a planner fault is recorded as an error, never raised out of `run()`;
11. baseline vs candidate aggregates and deltas, with failures preserved, and a
    state change without a changed decision is NOT counted as responding;
12. refused/inconclusive/verification-error runs remain visible;
13. model unavailability and model-claim provenance are reported honestly.

Also extended: `tests/test_novaforge_gameplay_api.py` (policy list capability,
unknown policy → 422 without running, default remains the NF-08 rule, a finished
run recorded as a trial with its aggregates, an inconclusive run kept, a refused
run kept and counted, `GET /novaforge/trials`), `tests/test_novaforge_ui.py`
(selector, request field, reported policy/version, the decision's state/reason/
expectation/evidence rendered, trial aggregates + a refusal rendered),
`tests/test_desktop_input.py` (all letters resolve), and the acceptance script.

| Command | Result |
| --- | --- |
| `pytest tests/test_novaforge_policy.py` | **33 passed**, 9 subtests |
| `pytest` policy + closed loop + gameplay + gameplay API + core + API | **253 passed**, 26 subtests |
| `pytest` desktop + desktop input + Forge UI + NF-09 vision + timeline | **340 passed**, 121 subtests |
| `pytest` route contract + API reference + benchmark/evaluation/experiment routes | **60 passed**, 413 subtests |
| `python -m mypy src` | clean, **433 files** |
| `python -m mypy src --platform win32` | clean, **433 files** |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (323 routes; `/novaforge/trials` added) |
| `node --check src/novacontrol/web/static/js/novaforge.js` | clean |
| `python scripts/novaforge_policy_acceptance.py --trials 3 --output data/novaforge_nf10_trials.json` | exit **0**; candidate 3/3 success, recovery 1/1, baseline 0/3 |

The full repository suite was **not** run in one pass.

### 10.1 Defects found and fixed during implementation

| # | Defect | Fix and pin |
| --- | --- | --- |
| 1 | **Letter keys were unreachable except a/s/d/w.** `_VK` was built with uppercase `"A".."Z"`, but `normalize_key` lowercases before lookup, so `b` and `r` were "not a key this build knows" — the live run could not press the second stage's key. | Lowercase the generated table; `test_every_letter_resolves_after_lowercasing` pins all 26 letters. |
| 2 | **Multi-step objectives could never continue past an intermediate verified failure** (NF-08's one-action rule applied to every policy). | `fail_on_unmet_verification`; default True preserves NF-08, False for the staged policy. Pinned by `test_two_transitions_complete_only_when_the_verifier_agrees`. |
| 3 | **A policy stop with a failing verdict was reported as `stopped`**, hiding the verdict. | The loop reports `failed` with the verifier's reason plus the policy's stop reason; pinned. |
| 4 | **Transition counting read only a nested `target` shape**, so real runs reported 0 transitions and the acceptance verdict said the policy never responded to a change. | Read `observation["title"]` (the real `summarise` shape) with the nested form as fallback; pinned by the live rows now showing 2–3 transitions. |
| 5 | **A module export collision**: `policy_eval.summarise` shadowed the observation module's `summarise`, breaking `test_novaforge_closed_loop` (caught before completion). | Renamed to `summarise_trials`; the observation export is restored and the regression group passes. |
| 6 | **`responded_to_transition_trials` credited the BASELINE with adaptation.** It counted any observed state change (3/3 for `FixedKeyPolicy` too), but the baseline's change was its own wrong key moving IDLE→FAULT and it never made a second decision — the opposite of responding. | `responded_to_transition()` now requires two decisions, a stage change between them, and a second action that is not the first repeated; the stored rows carry `responded_to_transition` per trial. Pinned by `test_a_state_change_without_a_changed_decision_is_not_a_response`, and the re-run reports **3/3 candidate vs 0/3 baseline**. |
| 7 | **A hung policy could hold the loop, and the operator's Stop, for ever.** `policy.propose()` was awaited with no bound, although the observation and verifier both had one. | `LoopLimits.decision_timeout_seconds` (default 10 s, validated) bounds the decision; a spent bound stops the run, and a cancel that arrives during the wait is reported as `canceled`. Pinned by `test_a_policy_that_never_decides_stops_at_its_bound` and `test_a_cancel_during_a_hung_decision_is_reported_as_cancelled`. |
| 8 | **A policy fault escaped the loop.** Only `PolicyUnavailable` was caught, so a raising policy — or a malformed answer that later hit `proposal.to_dict()` — propagated out of `run()`, and a gateway `plan_action`/`execute_action` raise did too. | The answer is validated (`_proposal_problem`: action mapping, kind, reason, evidence, expectations) before planning; a raise from the policy, planner or executor is recorded as `error` with the fault named and **nothing executed**. Pinned by the malformed/raising/faulty-planner tests. |
| 9 | **The required decision evidence and trial history were not exposed (§8).** The panel showed plan/execute statuses only — no observed state, reason, evidence refs, expected outcome, uncertainty, verifier reason, metrics or history — and the report had no UI section at all. | Each step now renders the state, reason, expectation, uncertainty, evidence refs, frame age and latency; the verifier's own verdict is quoted; a Trial history card shows per-policy aggregates and the last five rows including refusals, from `GET /novaforge/trials` (records capped at 50, labelled process-local). Pinned by the API tests and the DOM driver. |

## 11. Remaining gaps and unverified requirements

1. **No live model-backed decision.** The fallback interface exists and is unit
   tested, but a real model chose nothing; NF-09's hardware limitation is unchanged.
2. **No mid-run policy mistake recovery from a wrong POLICY action.** The recovery
   trial injects the wrong key as an operator action before the run; the loop
   itself never chose a wrong stage action. Recovery from a policy error inside the
   run is covered by the FAULT stage only if the environment enters it.
3. **Small sample.** 3 trials per policy on a deterministic task; no significance
   and no variance analysis.
4. **The baseline is deliberately naive.** It demonstrates state dependence, not
   optimality; a tuned baseline would be a different experiment.
5. **One task, one harness.** No second environment, no real game, no multi-step
   memory across sessions.
6. **The new UI was not clicked live.** The decision detail, the verifier line and
   the trial-history card are pinned by the DOM driver and by the API tests, and
   the backend loop path was exercised live through the acceptance script — but
   the browser itself was not driven against a live session.
7. **Trial history is process-local.** `GET /novaforge/trials` retains the last 50
   runs in memory and says `durable: false`; a restart clears it. The durable
   record is the acceptance script's JSON artifact.
8. **No NPC-style perception benchmarks or pixel-state policies.** State is the
   window title (the task's own honest state), not computer vision.
9. **Cloud providers not exercised** (no credentials, no paid request).

## 12. Verdict

**Overall: PASS WITH LIMITATIONS.**

The completion criterion is met on the disposable harness: a repeatable experiment
in which the policy chose a different action after each observed transition,
recovered from a controlled fault, executed only through the guarded path, and was
declared successful only by the independent verifier. The baseline failed the same
task under identical conditions. The honest limitations are the sample size, the
single deterministic environment, and the absence of a working local model for
model-backed decisions.

Evidence artifact: `data/novaforge_nf10_trials.json` (runtime data, not committed).
Acceptance script: `scripts/novaforge_policy_acceptance.py`.
