# NovaForge — Capability Acceptance Review (post NF-09 / NF-10)

**Date:** 2026-10-22
**Type:** read-only acceptance review. No source file, existing report, commit or remote branch was changed by this review. One new file was created: this report.
**Tree examined:** `main` at `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`), working tree **dirty** (44 modified tracked files + 17 untracked files = 61 entries, no staged changes). The NF-07…NF-10 work is uncommitted and was left untouched.
**Host:** Windows 11, Python 3.13.0, Node v24.19.0. No Ollama server was listening at review time (`127.0.0.1:11434` → connection refused).
**No game was launched or controlled.** No server and no browser preview were started for this review.

---

## 1. Executive verdict

**NO-GO** for the first controlled game evaluation as a measurement of NovaForge's play.

The guarded execution path, the evidence verifiers and the bounded decision loop are real and were
re-verified: actions cannot reach the desktop without plan/digest/Guardian gates, success cannot be
claimed without an independent objective verdict, and a state-dependent rule completed the staged
harness 3/3 while the fixed baseline failed 3/3. What does **not** exist is anything that can make a
game decision: the only policies read the disposable harness's `READY` marker or its `STAGE <name>`
title convention, no real game is registered as controllable, and the model-backed decision path is
both unavailable (NF-09's live-inference FAIL still stands) and unreachable in the shipped loop. A "game evaluation" today would measure an operator's script and NovaForge's input and
verification plumbing — not NovaForge's play.

**Permitted, narrower experiment:** an operator-scripted, supervised, offline, turn-based drill in one
cooperative game whose success is readable by the `ocr_text` or `game_state_file` verifier. That is a
workflow validation, not a gameplay evaluation; §8 states the minimum conditions and §7 the claims it
may and may not support.

---

## 2. Evidence and report consistency

The phase record was cross-checked against the current code. Where an older report is superseded, that
is stated rather than silently corrected (existing reports were not edited).

| Report | Its verdict | Status against the current tree |
| --- | --- | --- |
| `docs/NOVAFORGE_NF07_REPORT.md` (acceptance 2026-10-20/21) | Guarded action path PASS; live 15/15; 253.7 ms abort of a 1.5 s hold | **Consistent.** The code path at `manager.py:1523` / `:1596` matches the reported behaviour; tests still pin digest mismatch, budget and foreground refusal. |
| `docs/NOVAFORGE_NF07_INTEGRATION_GAP_AUDIT.md` (2026-10-20 + 2026-10-21 addendum) | `NO-GO` for real-game evaluation; GO WITH LIMITATIONS for the disposable harness | **Consistent in conclusion, superseded in two premises.** The addendum's "no planner" and "verifier limited to a window title or a game-state artifact" statements predate NF-08 (OCR verifier + closed loop) and NF-10 (staged policy). The current tree has three verifier kinds and a bounded loop; it still has no game-capable policy, so the NO-GO conclusion survives for a different reason. |
| `docs/NOVAFORGE_NF08_REPORT.md` (2026-10-21) | PASS WITH LIMITATIONS; OCR objective; loop 1 action, 5.962 s | **Consistent.** `observation.py`, `objective.py` `OcrTextVerifier` and `closed_loop.py` implement what the report describes; OCR freshness is 5 s and a below-floor/empty reading is `unknown`, never success (`objective.py:112-115`, `:1044`). |
| `docs/NOVAFORGE_NF09_REPORT.md` (2026-10-22) | Integration PASS; **live model inference FAIL** | **FAIL stands.** No new live inference was attempted (no server, and the audit does not re-run a 5-minute CPU inference). The report's failure evidence (empty answer at 194.9 s; timeout at 295.0 s; direct probe 54.2 s) is the latest live evidence and is not superseded. |
| `docs/NOVAFORGE_NF10_REPORT.md` (2026-10-22) | PASS WITH LIMITATIONS; 3/3 candidate, 0/3 baseline | **Experiment consistent; two claims overstated.** The raw artifact `data/novaforge_nf10_trials.json` matches the reported rows and aggregates. However §7's "the vision fallback is wired — it consumes `GameObservation.vision` from NF-09" is not true of the live loop (finding S2), and the trial record can overwrite a refusal/abort with `verified_failure` (finding S3). |
| `docs/NOVAFORGE_CONTROLLED_UI_REPORT.md` (2026-10-21) | PASS WITH LIMITATIONS; controlled UI reachable | **Historical and partly stale.** §8's "No decision pipeline" and "a pixel region or OCR reading is not checkable" predate NF-08/NF-10 and no longer describe the tree. The controlled (`mode: "control"`) workflow was accepted in its own report; it was not re-run here. |
| `docs/GAME_CONTROL_READINESS_AUDIT.md` (2026-10-17 + remediations) | NOT READY for real-time; GO WITH LIMITATIONS for slow/turn-based | **Consistent.** The measured ceiling (~8.2 fps warm, ~120 ms/frame, ~1.35 s rich observation) was not re-measured in this review and remains the operative limit for real-time play. |
| `docs/STATUS.md` (NF-10 entry) | Candidate 3/3; baseline 0/3; no live model-backed decision | **Consistent** with the artifact and code, including "model-backed trials: 0". |

Two source comments are older than the behaviour they describe and were recorded as findings, not
edited: `guardian.py:89-96` still says "NF-02 has no gameplay input path … it also refuses because the
path itself does not exist", while the function now allows input when a session is `RUNNING` against a
validated target with a wired injector; `games.py:573-579` still says "The action plan is
operator-authored; no planner chooses actions", which stopped being the whole truth when NF-08/NF-10
added the closed loop.

---

## 3. Capability matrix

Legend: **READY** — exercised end to end and independently verified; **READY WITH CONSTRAINTS** —
works under stated bounds; **PARTIAL** — exists but a named gap limits the claim; **UNAVAILABLE** —
not present on this host/build; **UNVERIFIED** — no live evidence in this review.

| Capability | Status | Evidence | Limitations |
| --- | --- | --- | --- |
| Session foundation, state machine, persistence | READY | `novaforge/models.py` transitions; NF-01 tests; NF-07 D2/D3 fixes | Nine states; illegal transitions raise; 40-session cap (NF-01 report) |
| Window discovery and target pinning | READY | title + handle + pid + bounds; `manager.start` (`manager.py:850-865`) | Title collision refused, never guessed; primary-display capture |
| Screen capture | READY WITH CONSTRAINTS | persistent worker, **8.2 fps** warm, ~120 ms median, p95 129 ms (2026-10-17 benchmark, not re-run) | No DXGI duplication; full-screen crop of the primary monitor; a window extending past it is clipped |
| Measured observation (title, frame, OCR, freshness) | READY | `observation.py:191-193` (`actionable` = `observed`), stale at `:461`, degraded at `:504-527`, 2 s frame bound (`:41`) | Title alone can make an observation `observed`; an overlapping window is inside the captured region |
| OCR text objective | READY WITH CONSTRAINTS | `OcrTextVerifier` (`objective.py:1044`); NF-08 live OCR run | Empty reading proves nothing (absence cannot be proven); no numeric comparison; Windows OCR reports no confidence (`confidence: null`) |
| Game-state artifact objective | READY WITH CONSTRAINTS | `GameStateFileVerifier` (`objective.py:593`), declared roots, JSON pointer, mtime freshness | Default max age 300 s (`:615`); default roots include the OS temp dir (`application.py:1812-1820`); a pre-existing artifact can satisfy it unless the criterion changes per run |
| Window-title objective | READY | `WindowTitleVerifier` (`objective.py:467`); NF-07/NF-08/NF-10 live runs | A real game rarely announces a win in its title |
| Vision model **contract** (typed claim, provenance, no action) | READY | `visual.py`; NF-09's 52 tests; audit re-ran them (52 passed) | A model's answer is a claim; it is never a verdict |
| Live vision inference | UNAVAILABLE | NF-09: empty answer at 194.9 s, timeout at 295 s; audit probe: no listener (`HTTP 000`) | CPU-only `qwen3-vl:4b` does not fit the production prompt/budget |
| Model-backed policy decision | UNAVAILABLE (and unreachable) | `application.py:4310-4315` builds the loop with `describe=False`; `policy.py:241-260` needs `observation.vision` | Even with a working model, the loop never attaches a description (finding S2) |
| Bounded decision loop (observe → decide → plan → execute → verify) | READY WITH CONSTRAINTS | `closed_loop.py:355-660`; NF-10 live 3/3 + recovery 1/1; audit re-ran policy tests | One policy family, one harness; 8 actions / 60 s defaults; no retry |
| State-dependent game policy | UNAVAILABLE | `HarnessReadyPolicy` (READY marker), `HarnessStagesPolicy` (`STAGE x` title), `FixedKeyPolicy` | All three are harness rules; none reads a game's state space |
| Baseline / candidate comparison | READY WITH CONSTRAINTS | `policy_eval.py`; `data/novaforge_nf10_trials.json` (3 vs 3, delta +1.0) | N=3, deterministic task, deliberately naive baseline; no significance |
| Trial records and history | PARTIAL | `policy_eval.py`; `GET /novaforge/trials`; UI card | Refusal/abort can be misclassified after a prior verified failure (S3); history is process-local and pooled across configurations |
| Guarded action path (plan → digest → execute) | READY | `manager.py:1523`/`:1596`; `test_novaforge_gameplay.py` digest/budget/foreground tests | The loop supplies the digest itself; there is no per-action human approval on this path (S4) |
| Guardian, action budget, foreground and cleanup | READY | `guardian.py:89-176`; `actions.py:114-163`; NF-07 253.7 ms abort, `presses == releases` | Keys are not window-validated beyond foreground; no drag/chord; `esc` is in the default allowed set |
| Cancellation / emergency stop | READY | `app.py:1057-1106` cancels a running loop on pause/stop/cancel/emergency; `closed_loop.py:416-424` | `/loop/cancel` stops before the next action; only the Guardian emergency stop aborts mid-injection |
| Operator UI (policy, state, reason, evidence, expectation, verifier, trials) | READY (DOM/API) | `index.html:876-920`; `novaforge.js:1024-1100`, `:1190-1300`; audit re-ran UI tests (121 passed) | **Not clicked live in a browser** (NF-10 discloses this; this review did not start one); per-row model provenance is absent |
| Environment registry | PARTIAL | `games.py`: `disposable-window`, `offline-game-window`, `gameplay-objective`, `artifact-harness` | Only `artifact-harness` is control-capable; no commercial game is registered; declared text is stale (S8) |
| Real-time gameplay | UNAVAILABLE | readiness audit §10.2/§10.6 | 8.2 fps, no frame scheduler, no DXGI, primary display only |

---

## 4. Safety-invariant checklist

| # | Invariant | Enforced at | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| 1 | A proposed action cannot bypass the guarded plan/execute path | Every loop action calls `gateway.plan_action` then `execute_action` (`closed_loop.py:577-610`); the manager is the only executor | `test_a_proposal_sends_nothing_and_a_matching_digest_executes`, `test_the_loop_completes_only_when_the_window_confirms_it` | **PASS** |
| 2 | An execution is bound to the exact proposed action and target | Digest covers session + action + target (`manager.py:1617`); id and digest both required (`:1618`); target identity re-probed (`:1658-1676`) | `test_a_mismatched_digest_is_denied_and_sends_nothing`, `test_a_changed_handle_pauses_and_is_refused` | **PASS** |
| 3 | Input reaches only the validated foreground target | `input_safety_decision` requires `RUNNING`, no pause reason, `target_validated`, wired injector (`guardian.py:97-176`); foreground check (`manager.py:1678`) | `test_an_unfocused_target_is_refused`, `test_a_lost_target_pauses_the_session_and_refuses_the_action` | **PASS** (foreground is checked before, not during, an injection) |
| 4 | Action budget and policy bounds are enforced before injection | `input_safety_decision` budget gates (`guardian.py:137-170`); `action.validate` (`actions.py:234-271`); `MAX_HOLD_MS = 10 s` | `test_the_budget_is_enforced_at_execution`, `test_unknown_kind_and_missing_coordinates_are_refused` | **PASS** |
| 5 | An untrusted observation cannot authorize an action | `actionable` only for `observed` (`observation.py:191`); stale (`:461`) and degraded (`:504`) are not actionable; the loop stops before planning (`closed_loop.py:454-467`) | `test_a_stale_observation_cannot_trigger_an_action`, `test_a_stale_first_observation_stops_before_any_plan` | **PASS** |
| 6 | A hung decision, planner, executor or verifier cannot hang the loop | `decision_timeout_seconds` (default 10 s) at `closed_loop.py:93` and `:501-521`; observation 15 s; verification 20 s; faults recorded at `:522-560`, `:576-610`, `:687-718` | `test_a_policy_that_never_decides_stops_at_its_bound`, `test_a_policy_that_raises_is_reported_and_never_executed`, `test_a_planner_fault_is_recorded_not_raised` | **PASS** |
| 7 | Cancellation and emergency stop are honest and bounded | `app.py:1057-1106` cancels the loop on lifecycle controls; `closed_loop.py:416-424`, `:569-575`; Guardian abort measured at 253.7 ms of a 1500 ms hold (NF-07) | `test_cancellation_stops_before_the_next_action`, `test_a_guardian_cancel_interrupts_an_in_flight_hold` | **PASS** (the loop's own cancel is before the next action by design) |
| 8 | Success requires the independent objective verifier | `completed` is set only on `verified_success` (`closed_loop.py:476-482`); a verifier fault is `verification_error`; a trial is `verified_success` only when the verdict says so (`policy_eval.py:124-147`) | `test_two_transitions_complete_only_when_the_verifier_agrees`, `test_a_completed_run_without_a_verifier_success_is_inconclusive` | **PASS** |
| 9 | A refusal is not a failure and is never retried | Loop stops on `plan_status != proposed` or non-executed action (`closed_loop.py:586-610`); no retry exists | `test_a_refused_plan_stops_and_is_counted`, `test_a_denied_action_is_a_refusal_not_a_retry` | **PASS at the loop; PARTIAL at the trial record** (S3) |
| 10 | A model interpretation cannot act, approve or verify | Vision route is read-only (`app.py:1224-1247`); `visual.py` has no execution path; policy labels model use and refuses prose/conflicts (`policy.py:241-260`) | NF-09 safety suite; `test_the_model_never_stands_in_for_measured_evidence`, `test_a_model_conflict_or_prose_cannot_choose_an_action` | **PASS** |
| 11 | Model-backed policy decisions are reachable and reported honestly | `model_backed` flags exist in `LoopStep`/`LoopResult`; the loop's observation builder is `describe=False` | Code inspection `application.py:4193-4229`, `:4310-4315`; `test_capability_is_honest_about_model_backing` | **FAIL as an integration claim** — the fallback cannot fire in the live loop (S2) |
| 12 | Autonomous input is authorized through the permission layer | Session start asks `PermissionManager` once (`manager.py:860`, `:2423-2433`), declaring `novaforge.session` MEDIUM with no confirmation and the comment "sends no input" (`application.py:1771-1779`) | Audit code trace | **PARTIAL** — no action-level permission declaration or autonomous-loop opt-in (S4) |

---

## 5. Evidence table

### 5.1 Automated tests

These were run **in this review** against the current working tree (`PYTHONPATH=src`), not copied from a report:

| Command | Result |
| --- | --- |
| `pytest tests/test_novaforge_policy.py -q` | **33 passed**, 9 subtests |
| `pytest tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py -q` | **68 passed**, 17 subtests |
| `pytest tests/test_novaforge_ui.py -q` | **121 passed**, 75 subtests |
| `pytest tests/test_novaforge_vision.py -q` | **52 passed** |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (exit 0) |
| `node --check src/novacontrol/web/static/js/novaforge.js` | clean |

Total: **274 tests + 101 subtests passed** across the four scoped files. The larger groups cited by the
phase reports (e.g. 253 + 340 + 60) were **not** re-run, and the full repository suite was **not** run
in this review.

Prior automated evidence (as reported, not re-run): NF-10 policy 33, policy/loop/gameplay/API 253/26,
desktop/UI/vision/timeline 340/121, route/API/benchmark 60/413; NF-09 383/88 and 292/85. Those are
credible for the tree they describe but are not re-verified here.

### 5.2 Live harness results

| Run | Date | Result |
| --- | --- | --- |
| NF-07 disposable-window acceptance | 2026-10-20 | 15/15 steps; digest refusal; 1 press/1 release/0 held; verifier read `NC FORGE HARNESS - READY`; Guardian cancel 253.7 ms into a 1500 ms hold; cleanup 3/3 |
| NF-07 HTTP workflow | 2026-10-21 | 10/10 steps over `:8001`; approval-bound execution and verification |
| NF-08 closed loop | 2026-10-21 | 1 action, 1 decision, 5.962 s; `verified_success` from OCR reading `RECEIVED 1 KEY`; session `COMPLETED` |
| NF-10 policy acceptance | 2026-10-22 | candidate 3/3 `verified_success` (2 actions, 2 transitions, 1.965/2.276/2.366 s); recovery 1/1 (3 actions, 2.695 s); baseline 0/3 (1 action each, `STAGE FAULT`) |
| NF-10 raw artifact | 2026-10-22 | `data/novaforge_nf10_trials.json` parsed in this review: candidate/baseline/recovery aggregates and raw steps match the report; `model_backed_trials: 0` |

**Not done in this review:** the acceptance script was not re-run (it launches a window and injects
keys; this is a read-only audit), and no game was launched.

### 5.3 Live model results

| Evidence | Result |
| --- | --- |
| NF-09 production endpoint, 2 attempts | `unavailable` — empty answer at 194.9 s; timeout at 295.0 s |
| NF-09 direct probe (short prompt, 512 tokens) | Returned in 54.2 s with a JSON description, including a visible misspelling; proves the model can see, not that the pipeline produces a usable answer |
| This review's Ollama probe | `127.0.0.1:11434` refused the connection (`HTTP 000`) — no live inference was possible, and none is claimed |

### 5.4 Live browser results

| Evidence | Result |
| --- | --- |
| NF-07 Guardian/action panel | Driven in a browser to a `VERIFIED SUCCESS` badge; screenshot exists (`screenshots/novaforge-action-verified-live.png`) |
| NF-08 panel | Verified by **textual DOM reads** of the served page; the screenshot tool reported the preview webview was not being composited, so no image evidence exists |
| NF-10 trial-history UI | **Not clicked in a live browser** (report §11.6); pinned by `ClosedLoopPanelTests` (a Node driver against the real panel module) and API tests only |
| This review | **No browser was opened**, so the NF-10 UI remains **unverified live**; its DOM/API coverage stands |

---

## 6. Unresolved blockers, ranked

| Rank | Blocker | Severity / impact | Evidence | Remediation (not performed) |
| --- | --- | --- | --- | --- |
| **S1** | **No game-capable decision policy.** The only policies read the harness's `READY` marker or its `STAGE <name>` title. No policy models a real game's state→action mapping. | **Critical** — a game evaluation would measure the operator's script, not the system's play; it cannot support a "gameplay" claim. | `policy.py:101-224`, `:265-398`; `application.py:4279-4283`; `games.py:520-580`; NF-07 §9 | Implement a game-specific policy (state encoding → action), or scope the first trial explicitly as an operator-scripted workflow validation and say so in the report title/claim. |
| **S2** | **Model-backed decisions are unavailable and unreachable in the live loop.** `describe=False` means `observation.vision` is always `None`; even with `describe=True`, a vision-only read is `degraded` and not actionable. | **High** — NF-10's "vision fallback is wired" is an overstatement; `model_backed` can never be true in a real run, and the model-backed-policy claim has no path even if a model is installed. | `application.py:4193-4229`, `:4310-4315`; `observation.py:191-193`, `:504-527`; `policy.py:241-260`; NF-10 §7 | Either wire a bounded describe call with an explicit latency budget (and prove it leaves the loop's timing honest), or state that model-backed policy decisions are unsupported in the loop and remove the fallback claim from the capability text. |
| **S3** | **A refusal, abort, stop or error can be recorded as `verified_failure`.** `outcome_of` checks the last verdict before the run's status. | **Medium** — violates "refusals and aborts are preserved"; an operator's cancel after an intermediate "not yet" can look like a failure in the trial history and aggregates. | `policy_eval.py:124-147`; direct probe (this review): `refused`, `canceled`, `stopped`, `error` + `verified_failure` → all `verified_failure`. The existing test only covers those statuses with `unknown` (`test_failures_and_refusals_are_kept_and_counted`). | Make the run's stopping status take precedence for `refused`/`canceled`/`error`/`unavailable`, and add the missing regression rows. |
| **S4** | **The permission layer's declaration predates input and is asked only at session start.** `novaforge.session` is declared MEDIUM/no-confirmation; the declaration's own rationale still says a session "observes a window and sends no input" (`manager.py:2423-2429`), and `execute_action` asks no permission layer. | **Medium** — autonomous key injection is gated only by ActionPolicy/Guardian/foreground/budget; no declared risk for the action that actually has side effects, and no explicit autonomous-loop opt-in. | `application.py:1765-1779`; `manager.py:860`, `:1596-1710`, `:2423-2433`; `guardian.py:89-176` | Declare a gameplay-input capability with its risk and require an explicit session-level opt-in for the loop; keep the Guardian as the execution gate. |
| **S5** | **Perception and input-targeting limits for a real game.** ~8.2 fps, ~120 ms/frame, ~1.35 s rich observation; primary display only; full-screen crop; keys validated only by foreground. | **Medium** — turn-based only; overlays can enter the captured region; focus loss mid-injection sends the release elsewhere (the release is guaranteed, the destination is not). | readiness audit §10.2, §10.6; `observation.py:41`; NF-08 §4 | Keep the target on the primary display, unobstructed and focused; no real-time claim; consider a per-window capture path before any faster game. |
| **S6** | **An artifact objective can be satisfied by stale or pre-existing evidence.** Default freshness is 300 s; default roots include the temp directory. | **Medium** — a "win" file from an earlier run could pass unless the game rewrites it or the objective requires a per-run value. | `objective.py:615`, `:823-951`; `application.py:1812-1820` | Per-run artifact reset (delete/rename before the run) or a run-unique token in the criterion; tighten `max_age_seconds` for the trial. |
| **S7** | **Trial-history observability is thinner than the rest of the panel.** Rows omit per-trial `model_backed` and `policy_version`; a long run shows only its last 8 steps; history pools runs with different limits and is process-local. | **Low** — an operator can see aggregates and outcomes but cannot attribute model use to a row, and pooled aggregates are not a controlled result. | `policy_eval.py:391-408`; `novaforge.js:1100-1110`, `:1221`; `application.py:4468-4480` | Add provenance fields to trial rows; label the history as a per-run log rather than an experiment summary. |
| **S8** | **Stale capability text in three places.** `guardian.py` says the input path does not exist; `games.py` says no planner chooses actions and declares only `window_title`/`game_state_file`; `CONTROLLED_UI_REPORT` §8 says there is no decision pipeline and no OCR verifier. | **Low** — a reader can be misled about what the build can do; the runtime overlay corrects the registry kinds, but the prose does not. | `guardian.py:89-96`; `games.py:396`, `:573-579`; `docs/NOVAFORGE_CONTROLLED_UI_REPORT.md` §8 | Update the text in a remediation pass; do not let it stand in a report a reviewer will read as current. |

---

## 7. Readiness tiers

### Ready now (evidence exists; claims may be made)

* **Guarded action execution** on a validated, focused window: plan → digest → Guardian → input guard, with budget, target revalidation and cleanup (NF-07 15/15, 253.7 ms abort).
* **Objectively verified outcomes** through three verifier kinds — `window_title`, `game_state_file`, `ocr_text` — with unknown/stale never becoming success.
* **A bounded, state-dependent loop on the disposable harness**, including one controlled-fault recovery (NF-10 3/3 + 1/1 recovery vs baseline 0/3), with every action through the same gates.
* **Honest capability reporting** (no model, no capture, no input path, no verifier each named with a reason) and **operator observability** of state, reason, evidence, expectation, verifier verdict and trial aggregates — the latter DOM/API tested, not live-clicked.
* **A durable experiment record** (`data/novaforge_nf10_trials.json`, runtime data and not committed) whose rows and aggregates match the report.

### Ready only for a constrained experiment (state the restriction in the report)

* One **offline, single-player, windowed, fixed-resolution** game with **no anti-cheat**, fully visible on the **primary display**, operated under supervision.
* A **human-authored action plan** (NF-05 `mode: "control"`) or the deterministic harness policies — not a claim of gameplay intelligence.
* An objective readable by `ocr_text` on a declared region or by a per-run `game_state_file`; a title objective only if the game announces it.
* Actions limited to taps/clicks/moves/holds within `ActionPolicy`; no text entry, chords or `esc` unless the trial explicitly declares it.
* A small, supervised N (≥10 per arm recommended); a negative control; raw rows preserved.

### Not ready (do not claim, do not plan around)

* Autonomous play of a real game (no game policy; S1).
* Model-backed decisions or visual state interpretation in a live run (S2; NF-09 FAIL).
* Real-time gameplay (8.2 fps; no scheduler/DXGI).
* Online, multiplayer, anti-cheat-protected, fullscreen-exclusive or second-monitor-only games.
* Statistical significance, generalization across games, or "gameplay intelligence" from N=3 on a deterministic harness.
* Unattended runs (the operator is the stop mechanism of record).

---

## 8. Minimum prerequisites for the first controlled game evaluation

1. **Choose and declare one target.** Offline, single-player, windowed, fixed resolution, no anti-cheat, fully on the primary display; record the exact build/version and how it is launched and closed.
2. **Define the objective before playing.** Prefer an `ocr_text` criterion on a declared screen region (with the OCR confidence floor and 5 s freshness) or a `game_state_file` criterion with a **per-run unique marker** and a tightened `max_age_seconds`. Prove a **negative** read before any action and a **positive** read after, using a fresh artifact/save.
3. **Declare the action set.** Only the game's required keys/buttons; verify every planned action against `ActionPolicy`; exclude `esc` and system keys unless the trial intentionally uses them; no text entry or chords.
4. **Make initial conditions equal.** New game/save, same seed and settings, same window position/size, fresh process per trial; reset the objective artifact before each run.
5. **Decide what is being measured.** If the plan is operator-authored, the report must say it measures the guarded pipeline and verification, not policy skill. If a policy is to be judged, implement a game-specific policy and show it changes its decision after observed state changes; the harness rules are not evidence for it.
6. **Keep the safety chain intact.** Session `RUNNING` against a pinned handle/pid, target focused and unobstructed, budget set, operator present with the session emergency stop; no unattended run.
7. **Record enough to falsify the claim.** Per trial: observation age, decision latency, action/refusal counts, verifier outcome and reason, raw evidence reference, and whether the run was refused/aborted. Fix S3 first so a cancel is not filed as a failure.
8. **Freeze and cross-check evidence.** Save the JSON artifact, compare panel aggregates with it, and state every unmeasured value as unavailable — never zero.
9. **Write the limitation in the result.** One game, one task, small N, deterministic or operator-scripted baseline; no significance and no general gameplay claim.

---

## 9. Recommended next task

### NF-10.1 — Correctness and honesty remediation (do this before any game trial)

**Entry criteria**

* NF-10 accepted as PASS WITH LIMITATIONS; no game trial scheduled; working tree preserved.
* The four findings S2–S4 and S8 reproduced from this report (S3 has a direct probe; S2 is a code trace; S4 is a declaration trace).

**Exit criteria**

* S3 fixed and pinned: `refused`/`canceled`/`error`/`unavailable` after a prior `verified_failure` are classified by the run's own ending, with regression rows for each; `test_failures_and_refusals_are_kept_and_counted` extended accordingly.
* S4 addressed: a declared gameplay-input capability with risk, and an explicit session opt-in for autonomous input; the Guardian remains the execution gate; the stale "sends no input" comment corrected.
* S2 resolved one way or the other: either `describe=True` with a bounded, measured latency path and a real test that the loop can label a model-backed decision, or the capability/report text states model-backed policy decisions are unsupported in the loop (and the fallback claim is removed).
* S8 corrected in `guardian.py`, `games.py` and the controlled-UI report's superseded section (as a dated note, not a rewrite).
* Scoped tests green, `mypy` both views clean, `generate_api_reference.py --check` in sync, no unrelated changes, nothing committed unless the user asks.

### NF-11 — First controlled game trial (after NF-10.1)

**Entry criteria**

* All NF-10.1 exit criteria met.
* One target from §8.1 declared, with its objective and action set validated on the harness first (negative then positive read).
* A written protocol: N, initial conditions, artifact reset, stop procedure, what is and is not being measured.

**Exit criteria**

* ≥10 supervised trials on the declared target, every outcome decided by the objective verifier, no unintended input and no run left with a held key.
* At least one negative-control run recorded as `verified_failure` and one refused/canceled run classified correctly.
* Panel aggregates match the frozen artifact; the report states the experiment measures the operator's script (or the declared game-specific policy) and claims nothing beyond it.
* If policy-driven: the policy changes its decision after an observed state change in ≥N trials, with the baseline recorded under identical conditions; no model-backed claim unless live inference succeeds.

---

## 10. Checks run during this review

| Check | Result |
| --- | --- |
| `git status --porcelain`, `git log -1` | 61 entries (44 modified + 17 untracked), no staged files, HEAD `8d10852`; recorded before and after the report |
| Read current code: `closed_loop.py`, `policy.py`, `policy_eval.py`, `observation.py`, `objective.py`, `actions.py`, `guardian.py`, `manager.py`, `application.py`, `api/app.py`, `games.py`, `visual.py`, `novaforge.js`, `index.html` | Findings S1–S8 traced to lines cited above |
| `pytest tests/test_novaforge_policy.py -q` | 33 passed, 9 subtests |
| `pytest tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py -q` | 68 passed, 17 subtests |
| `pytest tests/test_novaforge_ui.py -q` | 121 passed, 75 subtests |
| `pytest tests/test_novaforge_vision.py -q` | 52 passed |
| `python scripts/generate_api_reference.py --check` | in sync, exit 0 |
| `node --check .../novaforge.js` | clean |
| `data/novaforge_nf10_trials.json` parse and cross-check | candidate 3/3, recovery 1/1, baseline 0/3; `model_backed_trials: 0`; rows match the NF-10 report |
| `outcome_of` direct probe (constructing `LoopResult`s) | refused/canceled/stopped/error + `verified_failure` → all `verified_failure` (S3 confirmed) |
| Ollama probe `127.0.0.1:11434/api/tags` (3 s timeout) | connection refused (`HTTP 000`) — no live model |

**Checks deliberately not run**

* Full repository test suite (prohibited by the review scope; a single run can exceed the tool timeout).
* `mypy src` / `mypy src --platform win32` (reports claim clean over 433 files; not re-run here).
* `scripts/novaforge_policy_acceptance.py` and any harness launch (would open a window and inject input; this is a read-only review).
* Live model inference (no server; NF-09's bounded attempts already failed).
* Browser/UI interaction and screenshot (no server started; NF-10's UI is DOM/API-pinned only).
* Any real game launch, capture or control (prohibited).

---

## 11. Repository preservation

* **No source file, test, existing report or script was modified by this review.**
* **No commit, push, reset, clean, stash, checkout or remote-branch change was made.**
* The working tree was dirty before the review (61 status entries) and was left dirty; this report is the only new file. `git status` after writing shows the same 61 entries plus `?? docs/NOVAFORGE_CAPABILITY_ACCEPTANCE_REVIEW.md`; tracked-file diffs are unchanged.
* No game was launched or controlled; no server or browser preview was started; no session or artifact was created outside this report.

---

## 12. Decision

NovaForge can currently demonstrate a **bounded, guarded, evidence-verified control loop on a cooperative
harness**: it observes a real window, decides from measured state, sends only policy-bounded input
through a digest-bound Guardian path, and completes only when an independent verifier reads success
from the target. It can do this repeatably on the disposable staged task (3/3, with a recovery run),
against a deliberately naive baseline (0/3), with failures and inconclusive runs preserved by design
(though S3 shows a refused or aborted run can still be mis-filed).

It **cannot** yet demonstrate gameplay: there is no policy that understands a real game, no working
model to interpret a frame, and no wired path by which a model's claim could choose an action inside
the loop. It also cannot yet be trusted to report every refusal and abort correctly, cannot run
unattended, cannot capture beyond the primary display, and cannot support a real-time or anti-cheat
target. The first controlled game evaluation is therefore **NO-GO** as a measure of NovaForge's play.
The smallest honest next step is the remediation above, followed by one supervised, operator-scripted
drill in a cooperative turn-based game with an OCR or per-run artifact objective — reported for what
it is: a test of the pipeline, not of the player.
