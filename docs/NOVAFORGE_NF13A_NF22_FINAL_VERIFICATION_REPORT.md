# NovaForge NF-13A … NF-22 — Final Verification Report

**Report:** cross-phase remediation and verification, NF-13A (embedded game host) → NF-22
(first autonomous gameplay).
**Companion documents:** `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md` (every finding
and its disposition), `docs/NOVAFORGE_VERIFICATION.md` (the preceding milestone audit and its
appendix, updated in place), and the ten phase reports under `docs/NOVAFORGE_NF*_REPORT.md`.

---

## 1. Executive summary

The ten phases were read against their own claims, their checks were re-run, and the
behaviour those checks describe was driven. **Two defects were found and fixed in this
pass**, both with regression pins that were mutation-tested; **two more had been fixed in the
preceding audit pass on this tree** and were re-verified. Every remaining item is a declared
limitation of a phase's defined scope or an environmental blocker — none is a fabricated
capability and none is a silently-skipped check.

* **R-01 (fixed).** The JARVIS panel's **Approve And Run stopped receiving clicks after its
  first run** — the suite's only failure. Root cause: the decorative 3D card tilt in
  `effects.js` puts `perspective(...)/translateZ(...)` on the card that contains the button,
  and once it has, the browser retargets pointer input to the card, so the `click` never
  reaches the button. The tilt is now refused for cards containing interactive controls. The
  failing Playwright spec passes **unmodified**; measured live, two clicks now produce
  `plan, execute, plan, execute` with zero console messages.
* **R-02 (fixed).** **NF-20's approval limitation:** `/plan/execute` accepted `approved: true`,
  a flag an authenticated caller can set for itself, while the panel sent an `approval_token`
  nothing minted or read. Plans now carry the same server-minted, single-use, 5-minute,
  plan-bound token the desktop/browser/phone commands carry, consumed before anything is
  dispatched (missing/unknown/expired/replayed/mis-bound → **403**, nothing sent). Eight new
  tests, each mutation-checked; the in-process planner contract was deliberately left intact.
* **R-03 (fixed earlier, re-verified).** Six served routes were declared nowhere; a generated
  test now walks the FastAPI decorators so a served-but-undeclared route fails CI.
* **R-04 (fixed earlier, re-verified).** The two type-check environments disagreed about the
  Qt subclasses; the CI view is now clean through a module-scoped mypy relaxation.

**Honest bottom line:** the phases are internally consistent, their refusals are real, and the
integrated loop works end to end **in a simulated fixture world**. **No live game was launched
and no input was injected into any real application**, so **live gameplay is `NOT_TESTED`** —
and the reports say so wherever it matters. NF-13A's embedding half stays blocked on a
windowed target, and model-backed reasoning is unavailable on this machine.

## 2. Exact repository and working-tree state

| Item | Value |
|---|---|
| Branch / HEAD | `main` at `8d10852` |
| Working tree | **dirty by design** — all NF-01 … NF-22 and NovaForge work is uncommitted (≈141 `git status` entries). **Nothing was committed, pushed, reset, stashed, discarded or reverted in either pass.** |
| Python / env | Python 3.13 in `.venv` (editable install); `PYTHONPATH=src` for tests, scripts and mypy |
| Host | Windows 11, Intel Core Ultra 5 125H, 16 GB RAM. No game launched; no synthesis into any real application; the disconnected external drive was not touched. |
| New documents | `docs/NOVAFORGE_NF13A_NF22_REMEDIATION_REGISTER.md`, this report |
| Phase reports updated in place | `NOVAFORGE_NF20_GAME_PLANNING_REPORT.md` (limitation 5 → resolved, plus its unverified-requirements row), `NOVAFORGE_NF22_FIRST_AUTONOMOUS_GAMEPLAY_REPORT.md` (the E2E failure → resolved), `NOVAFORGE_VERIFICATION.md` (§A.3.3 → resolved, summary pointer, outstanding list) |

Earlier findings were **not rewritten**: each update keeps the recorded limitation, its
measured evidence and its original conclusion, and marks the item resolved with the new
mechanism, fix, pins and evidence.

## 3. Scope and phase-by-phase audit

| Phase | What it claims | Audit finding |
|---|---|---|
| **NF-13A** embedded game host | separate launch / discovery / embed / capture / control stages; a launched window is not an embedded one; incompatible targets refused | Present and honest. The **windowed** game this host can embed does not exist among the installed titles, so the embed half is `BLOCKED` (L-01/B-01), reported as such. |
| **NF-14** embedding compatibility | profile per renderer, recovery from missing/hidden/minimised/destroyed windows, no fake compatibility result | Present; unsupported renderers are refused, not faked. No commercial profile (L-02). |
| **NF-15** unified observation | validated, target-bound, fresh frames; desktop screenshots never presented as game frames | Present; unvalidated capture is reported unavailable (L-03). |
| **NF-16** game-state understanding | typed state, five field statuses, change tracking, profiles with resolution refusal | Present; **its evaluation found and fixed a real defect in the shared perception engine** (`perception/engine.py` merged line 0), plus three profile/region defects. Retained limits L-04/L-05/L-06/L-07. |
| **NF-17** objective & progress | typed objectives, conservative completion/failure, supersession, bounded persisted history | Present; a missing objective is not a failure and a retained counter is marked `progress last read` (L-08/L-09). |
| **NF-18** knowledge & memory | per-game isolation, provenance, stale revalidation, bounded history | Present (L-10). |
| **NF-19** control interface | bounded, target-bound input, cancellation, release of held inputs, auditability | Present (L-11). |
| **NF-20** planning | candidate generation from declared rules, approval, budgets, replanning | Present; **approval was a flag — R-02, now fixed**; plan records are process-lifetime (L-12). |
| **NF-21** outcome verification & recovery | verdicts from fresh observation only; unknown when unobservable; bounded recovery | Present (L-13); the pipeline never reports COMPLETED without a PASS. |
| **NF-22** first autonomous gameplay | one bounded session composing NF-15…NF-21, budgets, approval, pause/cancel, honest trace | Present; **its acceptance run is 16/16 with `live_gameplay: NOT_TESTED`** (L-14), and seven reporting/shutdown defects were fixed inside the phase. |

## 4. Original issues and final dispositions

Full detail in the register (§3). Summary:

| ID | Issue | Class | Disposition |
|---|---|---|---|
| R-01 | Approve And Run got no click after its first run | defect | **FIXED** — guard + 3 pins, mutation-tested, live-verified |
| R-02 | Plan approval was a request flag, not a token | defect (authorization) | **FIXED** — gate + 8 pins, mutation-tested, API docs regenerated |
| R-03 | Six served routes undeclared | defect (contract) | **FIXED (earlier pass)** — rows + generated drift test |
| R-04 | Type-check environments disagreed | defect (CI) | **FIXED (earlier pass)** — pyproject override, both views clean |
| L-01 … L-14 | Declared phase limitations | limitation | **RETAINED**, each with the reason it is correct to retain |
| B-01 … B-04 | Live game, model runtime, non-Windows, live corpus | blocker | **BLOCKED** — environmental |
| O-01 | Online play, anti-cheat circumvention, memory injection, unattended autonomy | — | **OUT OF SCOPE** by the phase texts |

## 5. Code changes grouped by issue and phase

**R-01 (app UI / the NF-19-20 approval surface)**

| File | Change |
|---|---|
| `src/novacontrol/web/static/js/effects.js` | `INTERACTIVE_SELECTOR` + `isTiltable()` (per-card `WeakMap` cache); `onMouseMove` consults it before writing any transform |
| `tests/test_web_platform.py` | `test_the_card_tilt_never_transforms_an_ancestor_of_a_control` (+ ~40 lines of docstring/assertions) |

**R-02 (NF-20)**

| File | Change |
|---|---|
| `src/novacontrol/novaforge/manager.py` | `plan_gate(plan_id)` — read-only status+approval view of a stored plan |
| `src/novacontrol/application.py` | `FORGE_PLAN_KEYS`, `forge_plan_receipt(payload)` (mints for a `required`+`pending` plan in both payload shapes), `execute_forge_plan(session_id, payload)` (requires + consumes the token) |
| `src/novacontrol/api/app.py` | plan status / build / replan routes serve receipts through `forge_plan_receipt`; the execute route goes through `execute_forge_plan` and maps `ValueError` → **403** |
| `src/novacontrol/api/models.py` | the execute route's declared description now names `approval_token` and the 403 |
| `docs/API.md` | regenerated (372 routes, `--check` in sync) |
| `src/novacontrol/web/static/js/novaforge.js` | `executePlan` sends the token from the preview and repaints from a fresh preview on refusal |
| `tests/test_novaforge_planning.py` | `_patch_manager` helper + 7 route tests over the REAL receipt path and the REAL route |
| `tests/test_novaforge_ui.py` | `NF20PlanningPanelTests::test_executing_a_plan_sends_the_server_minted_approval_token` |

**R-03 / R-04 (earlier pass, unchanged here):** `api/models.py`, `api/route_consumers.py`,
`tests/test_api_reference_sync.py`; `pyproject.toml`, `gui/app.py`, `gui/models.py`,
`gui/theme.py`, `gui/viewmodel.py`.

No other production file was modified in this pass.

## 6. Tests executed, with exact commands and results

From the repository root, `PYTHONPATH=src`, `.venv/Scripts/python.exe`. Groups were used
because the NovaForge set exceeds a single command's budget; each result is the tested
command's own exit status.

| Command | Result |
|---|---|
| `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_autonomy.py -q` | 220 passed, 21 subtests, 92 s |
| `pytest tests/test_novaforge_benchmark.py tests/test_novaforge_benchmark_api.py tests/test_novaforge_coach.py tests/test_novaforge_control.py tests/test_novaforge_embedding.py -q` | 273 passed, 154 subtests, 288 s |
| `pytest tests/test_novaforge_evaluation.py tests/test_novaforge_evaluation_api.py tests/test_novaforge_experiment_api.py tests/test_novaforge_gameplay.py tests/test_novaforge_gameplay_api.py -q` | 153 passed, 51 subtests, 140 s |
| `pytest tests/test_novaforge_knowledge.py tests/test_novaforge_launcher.py tests/test_novaforge_library.py tests/test_novaforge_objectives.py tests/test_novaforge_observation.py -q` | 266 passed, 22 subtests, 87 s |
| `pytest tests/test_novaforge_planning.py tests/test_novaforge_policy.py tests/test_novaforge_state.py tests/test_novaforge_timeline.py tests/test_novaforge_vision.py -q` | 239 passed, 35 subtests, 35 s |
| `pytest tests/test_novaforge_ui.py tests/test_novaforge_verification.py -q` | 265 passed, 121 subtests, 33 s |
| `pytest tests/test_capture_backends.py tests/test_perception.py tests/test_desktop.py tests/test_desktop_input.py tests/test_vision_model_config.py -q` | 325 passed, 46 subtests, 79 s |
| `pytest tests/test_web_api.py tests/test_web_platform.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | 145 passed, 949 subtests, 259 s |
| `pytest tests/test_application.py tests/test_gui.py tests/test_focus_rings.py tests/test_guided_click_e2e.py -q` | 53 passed, 15 subtests, 262 s |
| `pytest tests/test_telemetry_e2e.py tests/test_tab_focus_walk_e2e.py -q` | 5 passed, 133 s |
| **`pytest tests/test_browser_token_reuse_e2e.py -q`** (R-01) | **1 passed, 14 s** — the spec that failed before the fix |
| **`pytest tests/test_reduced_motion_e2e.py -q`** (R-01 regression) | **2 passed, 27 s** |
| **`pytest tests/test_novaforge_planning.py tests/test_novaforge_ui.py -q`** (R-02) | **219 passed, 120 subtests, 22 s** |
| Regression after R-02: `pytest tests/test_novaforge_verification.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_api.py tests/test_api_reference_sync.py tests/test_route_shape_contract.py -q` | 181 passed, 499 subtests, 215 s |

**Acceptance and evaluation scripts** (this pass), each with its exit status captured:

| Script | Result |
|---|---|
| `scripts/novaforge_nf16_state_eval.py` | exit 0 — fixtures **20/20** (accuracy 1.0); recorded frames **3/3** analysed, latency first 1482.8 ms / steady 1468.0 ms; declared fields **8/8 recognized** |
| `scripts/novaforge_nf17_objective_eval.py` | exit 0 — **32/32** checks, accuracy 1.0, **false completions 0**; discovery precision/recall 1.0; latency avg 0.111 ms, max 0.271 ms; service 7/7 guarantees |
| `scripts/novaforge_nf18_knowledge_eval.py` | exit 0 — **51/51** checks; latency avg 16.0 ms, max 38.9 ms over 8 records; **live evaluation `NOT_TESTED`** |
| `scripts/novaforge_nf22_autonomy_acceptance.py` | exit 0 — **16/16 steps, `failed: 0`, `failed_steps: []`, verdict `PASS WITH LIMITATIONS`, `live_gameplay: NOT_TESTED`**, note: "real orchestration over simulated evidence. It is not live-game verification." |

**The whole suite was not re-run in this pass.** It was run in the preceding audit pass in
one process: `pytest tests/ -q` → **5 663 passed, 3 847 subtests passed, 1 skipped, 1 failed in
1 959.81 s** — that single failure being exactly the R-01 spec, which now passes. This pass
re-ran the phase suites, the dependency/integration suites, both E2E specs around the change,
both acceptance gates and both type-check views on the changed files; the static change to
`effects.js` cannot reach the Python-only suites beyond the ones listed, which include every
suite that reads the static tree (`test_web_platform`, `test_web_api`, `test_focus_rings`,
`test_reduced_motion_e2e`, `test_guided_click_e2e`, `test_telemetry_e2e`).

## 7. Type-check and API verification

| Check | Result |
|---|---|
| `python -m mypy src` | **Success: no issues found in 451 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 451 source files** |
| CI-parity view (third-party extras absent) | Clean since R-04: the module-scoped `disallow_subclassing_any = false` covers the Qt subclasses in both environments |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (372 routes) |
| Route contract | every served route is declared (`test_every_route_the_app_serves_is_declared`); NovaForge 101 live = 101 declared = 101 consumers |
| Frontend/backend contract | `BackendFrontendContractTests` green; the plan card sends the token the route now requires (§6, R-02 pins) |

## 8. Live-game test details

**None.** No game was launched, focused, driven or closed, and no input was synthesised into
any real application in this pass — or in the precedent audit pass. The live half is
`BLOCKED` on B-01 (a windowed, offline, lightweight target with a HUD profile whose
completion is independently observable, plus the resource gate). NF-13A's embedding half is
blocked on the same class of prerequisite. Nothing in this report describes a live game as
captured, embedded, controlled or completed.

## 9. Simulation results (clearly separated from live evidence)

Everything in this section is **simulated or fixture-based**, and the artefacts say so on the
wire (`evidence_source: "simulated"`, `simulated: true`, `live_gameplay: "NOT_TESTED"`,
`live evaluation: NOT_TESTED`).

* **NF-22 acceptance (16/16):** approval gate (no approval → nothing starts, 0 presses);
  a full bounded session reaching `COMPLETED` / `OBJECTIVE_COMPLETED` with **every dispatched
  action carrying a verified outcome and evidence refs**; the ordered phase trace
  `INITIALIZING → OBSERVING → UNDERSTANDING_STATE → IDENTIFYING_OBJECTIVE → PLANNING →
  EXECUTING → VERIFYING → COMPLETED`; session cleanup with input released; the action budget
  stopping a run after exactly its bound; a plan above the approved per-plan budget being
  **refused, not trimmed**; an unresponsive target stopping the run before any input; a
  refusing input backend never reported as progress; an unreadable counter never becoming a
  verified effect; a verified objective failure ending the run as failed; pause/resume; and
  cancellation that is never restarted.
* **NF-16/17/18 evaluations:** the fixture and recorded-frame numbers in §6 — real OCR over
  real recorded frames of disposable harness windows (NF-16) and generated states (NF-17/18).
* **Application-level flow verified this pass:** the JARVIS browser device planning and
  executing an approved command through the real routes, twice, with the token-reuse
  fallback (live browser, but a **local search**, not a game).
* **Not claimed:** general gameplay ability, play against humans, long unattended runs, or
  anything about a commercial title.

## 10. Performance and resource measurements

| Measurement | Value | Source |
|---|---|---|
| NF-16 state reading, recorded frames | first 1482.8 ms, steady 1468.0 ms, max 1482.8 ms per frame | `novaforge_nf16_state_eval.py` (this pass) |
| NF-17 objective interpretation | avg 0.111 ms, max 0.271 ms per state | `novaforge_nf17_objective_eval.py` (this pass) |
| NF-18 knowledge retrieval | avg 16.0 ms, max 38.9 ms over 8 records | `novaforge_nf18_knowledge_eval.py` (this pass) |
| NF-15/16 capture interval | 0.05 s in the fixtures; ~1.2 s/frame end-to-end perception — adequate for periodic readings, not a perception loop | phase reports |
| Whole-suite wall clock | 1 959.81 s (32:39) single process | preceding pass |
| Resource gate | no game launch attempted, so the ≥2 GB-free / ≤92 % rule was never the binding constraint; the acceptance script asserts the gate is consulted and the run refuses at CRITICAL | NF-22 acceptance, `AutonomyResourceGateWiringTests` |
| Live UI responsiveness | 1280×900, two full plan+execute round trips with zero console messages and no failed requests | this pass |

No performance claim is made that was not measured; where a figure was not measured it is
reported as such (the phases' own `None` convention).

## 11. Security and safety verification

* **Approval is now a token, not a flag (R-02).** A missing, unknown, expired, replayed or
  differently-bound token is refused with **403 and no dispatch**, measured directly against
  the real route. Tokens are single-use, bound to the plan id and pruned on expiry by the
  same store the desktop/browser/phone command plans use.
* **Risk is declared, never inferred** (NF-19/NF-22): an undeclared risk is HIGH and refused;
  the digest-bound action path revalidates target identity and policy before dispatch.
* **Refusals are results, not exceptions** where the UI needs to explain (e.g. an unknown
  plan id is a 200 with `unknown_plan`), while a missing approval token is a hard 403 — the
  two are deliberately different channels.
* **Nothing was dispatched to a real application** in this pass: the injectors are recording
  fakes; the one live flow executed a **local web search** through the app's own browser
  device.
* **Safety constraints honoured:** no game launched (including GTA V), no user process
  closed or force-killed, no duplicate instance, no display-mode forcing, no injection /
  hooks / anti-cheat circumvention, no multiplayer, and the disconnected external drive was
  never accessed.
* **Audit trails:** plan, control, verification and autonomy events all land on the session
  timeline and the shared audit log (NF-20's fix), with operational metadata only.

## 12. Regression results

* The **only** previously failing test in the suite (`test_browser_token_reuse_e2e.py`) now
  passes, and its assertions were never weakened.
* The tilt's own reduced-motion spec still passes — the effect is alive on the 44 control-free
  cards, measured live (42 of 86 cards are skipped because they hold controls).
* Both mutation tests restored their files **byte-identically** (`cmp` verified) and re-ran
  green; `git status` shows no unintended change.
* R-02 touched only the wire: the in-process planner suite (100+ tests) and the NF-21/NF-22
  suites that drive plans in process all pass unchanged (181 + 219 + 265 tests across the
  re-run groups).

## 13. Remaining blockers

| Blocker | What is needed to clear it |
|---|---|
| **B-01 live game acceptance** | an installed, windowed, offline, lightweight title; a HUD/objective profile for it; the resource gate met; and an approved, short, reversible objective. Then the ten-step live chain in the phase texts can be run. |
| **B-02 model-backed reasoning** | a local model runtime (or a configured provider) so NF-16 descriptions, NF-18 synthesis and NF-20 rationale can be exercised instead of reported as unavailable. |
| **B-03 non-Windows behaviour** | a Linux/macOS host for capture, window enumeration, embedding and the input guard. |
| **B-04 NF-18 live corpus** | depends on B-01: a real session to ingest knowledge from. |

None of these is a code defect; each is reported where it belongs and none is papered over.

## 14. Final acceptance matrix

| Phase / capability | Verdict | Evidence |
|---|---|---|
| NF-13A embedded game host | **PASS WITH LIMITATIONS** (embed half BLOCKED) | phase suite green; windowed-target blocker B-01 |
| NF-14 embedding compatibility & recovery | **PASS WITH LIMITATIONS** | phase suite green; unsupported renderers refused, never faked |
| NF-15 unified observation | **PASS WITH LIMITATIONS** | phase suite green; unvalidated capture reported unavailable |
| NF-16 game state understanding | **PASS WITH LIMITATIONS** | 20/20 fixtures, 3/3 frames, 8/8 fields; 4 defects fixed in-phase incl. the shared perception engine |
| NF-17 objective & progress | **PASS WITH LIMITATIONS** | 32/32 checks, 0 false completions; 11 in-phase fixes |
| NF-18 knowledge & memory | **PASS WITH LIMITATIONS** | 51/51 checks; live corpus BLOCKED |
| NF-19 control interface | **PASS WITH LIMITATIONS** | phase suite green; no service-owned input left held, pinned |
| NF-20 planning & action selection | **PASS WITH LIMITATIONS** | 219 tests incl. the 8 new approval-gate pins; **R-02 resolved** |
| NF-21 outcome verification & recovery | **PASS WITH LIMITATIONS** | phase suite green; unobservable effects reported unknown, never success |
| NF-22 first autonomous gameplay | **PASS WITH LIMITATIONS, `live_gameplay: NOT_TESTED`** | 16/16 steps, `failed_steps: []`, trace complete, budgets/refusals/pause/cancel honest |
| Cross-phase chain (catalog → launch → observation → state → objective → knowledge → planning → approval → input → verification → recovery → terminal state → trace) | **PASS in simulation; front-end links BLOCKED live** | NF-22 acceptance composes the real services; the launch/capture links need B-01 |
| App-wide UI regressions | **PASS** | R-01 fixed, live-verified; suite's only failure resolved |
| Types / API contract | **PASS** | both views clean; `docs/API.md` in sync; every served route declared |

## 15. Final verdict and evidence

**PASS WITH LIMITATIONS** for the defined acceptance scope of NF-13A … NF-22, with **no known
in-scope defect left unfixed** and **no live-game capability claimed**.

Why this is a PASS and not a conditional one: every requirement the phases declare for
themselves is implemented, reachable and exercised by something that would fail if it
regressed; the two real defects this pass found are fixed, pinned and mutation-tested; the
authorization gap NF-20 recorded in its own report is closed; and every limitation that
remains is a limit of the *scope* (no commercial-game profile, no graphical HUD measurement,
no calibrated confidence) or of the *machine* (no live target, no model runtime, not Linux) —
each stated in the same document that would otherwise be read as a capability claim.

The evidence is reproducible from this tree: the commands and results in §6–§7, the register's
per-finding detail, and the live assertions in §9–§12. Where evidence does not exist — a real
game, a model-backed answer, a Linux run — this report says so instead of implying otherwise.
