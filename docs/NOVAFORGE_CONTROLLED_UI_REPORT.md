# NovaForge — Controlled-Run UI: implementation, verification and report (2026-10-21)

## 1. Scope

The controlled-evaluation API shipped in the NF-07 → NF-05 integration pass and was accepted five
times over HTTP. This milestone makes that workflow reachable from the NovaForge panel **without
changing what it is allowed to do**:

* the panel can now choose `observation_only` (default, unchanged) or `control`;
* in control mode the operator authors the objective and the action plan, validates readiness,
  approves **one digest** and starts that exact plan;
* every rendered figure — verdict, stages, metric, digest, refusal — comes from the backend
  record; nothing is inferred from "the run completed".

No planner was added, no game was launched or controlled, and no backend safety gate was
weakened. Input only ever reached the disposable `NC FORGE HARNESS` window.

## 2. What shipped

| Surface | Change |
| --- | --- |
| `web/static/index.html` | `#forgeEvaluationMode` selector, experimental banner, `#forgeEvaluationControlConfig` (objective kind/comparison/value/path/pointer/max-age, plan editor, plan problems, policy hint, action budget), `#forgeEvaluationApprove` + `#forgeEvaluationApprovalRow`, `#forgeEvaluationPlanProblems`. Observation copy retained and clarified. |
| `web/static/js/novaforge.js` | Mode switch and field sync; objective editor; operator-authored plan editor (key / hold / click / move) with in-place row summaries; UI-side plan validation mirroring the declared `ActionPolicy`; digest rendering; approval-gated start; start-recovery; verdict/stage/step/metric rendering for the active run and the report. |
| `web/static/nc-os.css` | Styles for the plan editor, digest block, control headings and the verdict line (tones `ok` / `bad` / `warn` / muted). |
| `novaforge/evaluation.py` | One reporting fix: a completed plan with a decided verdict closes the `refusal` stage as `not_applicable` instead of leaving it at the initial `pending` (see §5, D3). |
| `tests/test_novaforge_ui.py` | 16 new pins: 10 behavioural (guard, observation consent, digest echo, recovery, verdict translation, plan/objective validation, summaries, markup, no client-side digest minting) and 6 DOM-rendered (plan editor, preflight digest, verified success, six stages, inconclusive, report). |
| `tests/test_novaforge_evaluation.py` | The control run now also asserts `states["refusal"] == "not_applicable"`. |

## 3. The gate, in order

Start is disabled until **all** of these hold, and the tooltip names the first that does not:

1. a preflight exists and its `ready` is `true`;
2. no evaluation is running and no start is in flight;
3. the configuration signature (`evaluationSignature()`) still matches the validated one;
4. consent: for a controlled run, `#forgeEvaluationApprove` ticked; for an observation run, the
   existing "only observes it" confirmation ticked. Neither is implied by readiness;
5. a controlled run additionally needs the preflight's own `approval.digest`.

The start payload echoes `approval_digest` **from the preflight response**, so the UI cannot mint
or guess one (pinned by `test_start_echoes_the_preflights_digest_and_nothing_else`, which also
forbids `sha256` / `crypto.subtle` in the panel). If a start fails ambiguously, the panel never
re-posts the plan: `recoverEvaluationStart` reads the history and adopts a `running` record whose
`plan_digest` matches, then polls it. A failed controlled start drops the approval and the
preflight, because an approval is single-use.

## 4. What the reader sees

* **Active evaluation**: mode, plan digest (full), control state, objective verdict with its tone
  and the verifier's reason, the six control stages separately (`proposal`, `approval`,
  `execution`, `verification`, `refusal`, `cleanup`), each approved action with its status and
  reason, and the measured-metric table where an unmeasured row prints `Unavailable — reason`
  rather than `0`.
* **Report**: the same verdict plus objective summary, digest, approval state, actions, stages,
  metrics and verifier evidence, with the record's own note that a run ending without a decided
  verdict is inconclusive.
* **Status text**: a controlled run reads `completed · objective verified` only when
  `objective.decided` (full record) or `objective_verified` (history row) is true; otherwise
  `completed · objective inconclusive`. An observation run still reads `data complete`.

## 5. Defects found while verifying, and their fixes

**D1 — an edited plan row kept its old summary.** The row head showed `key ? · 60 ms` after the
operator chose `space`; only the aggregate line refreshed. The row summary is now updated in place
on edit (`onEdit`), so a full re-render — which would steal focus mid-edit — is unnecessary.
Found by driving the real form in a browser.

**D2 — the observation-mode consent check was dropped by the refactor.** Routing every start
through the new `controlStartGuard` lost the old "confirmed" requirement for observation runs.
`tests/test_novaforge_ui.py::test_start_requires_readiness_and_an_explicit_confirmation_for_this_exact_config`
caught it; the guard now requires consent in **both** modes, with different wording. This is the
one behavioural regression the tests caught before any live run.

**D3 — a finished run still showed `refusal: pending`.** After a `verified_success` the refusal
stage was never touched, so a completed evaluation invited the question "what is still pending?".
`_run_control` now closes that stage as `not_applicable` with a stated reason, and the control-run
test pins it. Verified live: the second UI run renders `refusal  not applicable  Nothing was
refused; the plan ran to a decided verdict.`

## 6. Checks

| Check | Result |
| --- | --- |
| `node --check static/js/novaforge.js` | pass |
| `pytest tests/test_novaforge_ui.py` | **104 passed**, 61 subtests (was 88) |
| `pytest tests/test_novaforge_evaluation.py -k ControlEvaluationTests` | 5 passed |
| `pytest tests/test_novaforge_evaluation_api.py` | 13 passed (186.6 s) |
| `pytest tests/test_novaforge_benchmark_api.py` | 22 passed |
| `python -m mypy src` | clean, 428 files |
| `python -m mypy src --platform win32` | clean, 428 files |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (no route changed) |

The DOM harness was mutation-checked: removing the `in_progress` verdict branch from the real
renderer made the inconclusive copy disappear from the captured output, confirming the tests
drive the shipped code rather than a copy of it.

## 7. Live acceptance through the served UI

Server: `uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001` from a
temporary working directory (the user's `data/` untouched). Target: `scripts/forge_harness_window.ps1`
(`NC FORGE HARNESS - idle`, which retitles to `READY` only when it receives a key press). The page
was driven in the browser panel.

Served-asset checks: `/` carries `Cache-Control: no-cache, no-store, max-age=0, must-revalidate`
plus `Pragma: no-cache` and `Expires: 0`, injects `novaforge.js?v=566116fabced`, and contains every
new control id.

Two complete controlled runs, each from the form:

| Step | Run 1 | Run 2 |
| --- | --- | --- |
| Mode switched | control; banner shown, config shown, observe-only confirm hidden | same |
| Environment | `artifact-harness@1` → `controllable on this host` | same |
| Target | `NC FORGE HARNESS - idle` | `NC FORGE HARNESS - idle` |
| Plan | 1 × `key space · 60 ms` | 1 × `key space · 60 ms` |
| Readiness | passed (objective verifier, input capability, objective definition, plan, budget all `pass`) | passed |
| Digest | `3a03631f…0402053` | `7f7de870…735bf9e` |
| Start before approval | disabled — "Approve the exact plan before starting it." | same |
| Result | `completed · objective verified` | `completed · objective verified` |
| Verdict | `verified success — Read 'NC FORGE HARNESS - READY' from window … satisfied because the title contains 'READY'.` | same (different handle/pid) |
| Stages | proposal recorded · approval approved · execution completed · verification verified success · refusal **not applicable** · cleanup completed | same |
| Metrics | `objective_success achieved`, `completion_time_seconds 0.832 s`, `actions_executed 1` | `objective_success achieved`, `actions_executed 1` |
| Report | verdict, digest, approved actions and verifier evidence all render | — |

Observation-only was then re-selected in the same session: the control config hides, the
"only observes it" confirmation returns, the button relabels to `Start Evaluation`, and it is
disabled until re-validated. The existing workflow is intact.

![Controlled evaluation verdict](screenshots/novaforge-controlled-evaluation-verdict.png)

## 8. Remaining blockers to a first controlled *game* evaluation

The UI is no longer one of them. What still stands between this panel and a real game:

1. **No decision pipeline.** The plan is authored by a person. Nothing in this build chooses an
   action from what was observed, so a "game evaluation" would measure the operator's script, not
   the system's play.
2. **Only two objective kinds, and a real game announces neither.** `window_title` and a declared
   `game_state_file` are checkable; a pixel region or OCR reading is not.
3. **One controllable environment.** `artifact-harness` is the only record that declares `control`
   and is live-capable on this host. No commercial game is registered as controllable, and
   registering one would not make it true.
4. **Anti-cheat and per-game policy are unproven.** The offline/single-player declaration is the
   operator's, not a verified property of a game.
5. **The frame ceiling is unchanged** (~8 fps from the readiness audit), which suits turn-based
   play and not real-time play.

## 9. Verdict

**PASS WITH LIMITATIONS.** The controlled workflow is reachable, gated on a single-use digest,
recoverable after an ambiguous start, and reports execution separately from verification in both
the live card and the report — while observation-only remains the default and behaves as before.
The limitation is the honest one: the panel can now run an approved plan against the disposable
harness and report what the target said, and it still cannot play a game.
