# NovaForge NF-13A … NF-22 — Consolidated Remediation Register

**Scope:** every phase from NF-13A (embedded game host) through NF-22 (first autonomous
gameplay), their dependencies (NF-01 … NF-12 as they are consumed), and the cross-phase
integration between them.
**Method:** read each phase report against the code it describes, re-run that phase's own
checks, then drive the behaviour the report claims. A finding is closed only when the
behaviour is exercised — not because a report says it was fixed and not because a test
that never reaches the behaviour happens to be green.
**Verdict:** **PASS WITH LIMITATIONS** for the ten phases. Two defects fixed in this pass
(R-01, R-02), two fixed in the preceding audit pass on the same tree (R-03, R-04), every
remaining item a declared limitation of the phase's defined scope, and five environmental
blockers that no code change can clear on this machine.

---

## 1. Repository and working-tree state

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `8d10852 feat: give NovaForge a guarded action path and an objective verifier` |
| Working tree | **dirty by design** — the NF-01 … NF-22 work is uncommitted in this checkout (≈137 entries). Nothing was reset, stashed, discarded, committed or pushed during this remediation. |
| Runtime | `.venv` with an editable install; `PYTHONPATH=src` for tests, scripts and mypy |
| Host | Windows 11, Python 3.13, 16 GB RAM (no game launched, no input injected into any real application) |

Every edit in this pass is additive or a targeted correction; unrelated changes from
earlier phases were left exactly as found (verified with `git status` before and after, and
with byte-identical restores for every mutation test).

## 2. Disposition key

| Code | Meaning |
|---|---|
| **FIXED** | A real defect; implemented, pinned by a test, and re-verified in this pass. |
| **FIXED (earlier pass)** | A real defect corrected by the preceding audit pass on this tree; re-verified here. |
| **RETAINED** | A declared limitation of the phase's defined scope. Not a defect; the phase reports it honestly and the code refuses rather than guesses. |
| **BLOCKED** | Needs hardware, a target, a model or a platform this machine does not have. |
| **OUT OF SCOPE** | Deliberately excluded by the phase text. |

---

## 3. Findings

### 3.1 Defects

| ID | Phase | Finding | Class | Disposition |
|---|---|---|---|---|
| **R-01** | App UI (NF-19/20/22 approval surface) | A decorative 3D card tilt retargets pointer input, so **the JARVIS panel's Approve And Run stops receiving clicks after its first run**. | defect (functional, user-facing) | **FIXED** (§4.1) |
| **R-02** | NF-20 | **Plan approval was a request flag, not a token.** `/plan/execute` accepted `approved: true`, which an authenticated caller can set for itself; the panel already *sent* an `approval_token` the backend never minted or read. | defect (authorization gap), NF-20 limitation 5 | **FIXED** (§4.2) |
| **R-03** | NF-13A … NF-22 routes | **Six served routes were declared nowhere** (`POST /build/project`, `GET /build/artifacts`, `POST /build/run`, `GET /models/status`, `POST /models/load`, `POST /models/unload`), so they were invisible to `ApiSurface`, the frontend contract and every existing drift check. | defect (contract/documentation), pre-existing | **FIXED (earlier pass)** — rows added to `api/models.py` + `api/route_consumers.py`; `tests/test_api_reference_sync.py::test_every_route_the_app_serves_is_declared` walks `@app.<verb>(…)` decorators and fails on any served-but-undeclared route (mutation-tested). NovaForge now reads **101 live = 101 declared = 101 consumers**. |
| **R-04** | CI | **The two type-check environments disagreed**: removing the Qt `# type: ignore[misc]` comments left `mypy src` clean where PySide6 is installed but red where it is absent (`Class cannot subclass "QWidget" (has type "Any")`). | defect (CI configuration) | **FIXED (earlier pass)** — module-scoped `disallow_subclassing_any = false` in `pyproject.toml`; verified by hiding PySide6 and running the full tree. Re-verified this pass: both views clean. |

Phase-local defects (49 across NF-13A … NF-22 — e.g. NF-16's line-0 OCR grouping in the
shared perception engine, NF-17's objective registry being silently replaced, NF-20's plan
events never reaching the timeline, NF-22's seven reporting/shutdown defects) are recorded
in each phase's own report under its "defects found and fixed during verification"
section, each with the pin that holds it. They are **not** re-listed here; they were
re-verified this pass by the phase suites passing in full (§5).

### 3.2 Retained limitations, by phase

Each is a scoped, declared limit; the code refuses or reports honestly rather than
fabricating the capability.

| ID | Phase | Limitation | Why it is correct to retain |
|---|---|---|---|
| L-01 | NF-13A | Native embedding needs a **windowed** (non-borderless) target; the installed title examined is borderless and is refused by design. | Forcing a display mode or repeatedly reparenting an incompatible window is explicitly forbidden by the phase and unsafe. |
| L-02 | NF-14 | No commercial-game compatibility profile; unsupported renderers are rejected, never faked. | A profile without provenance would be a guess, and a fake compatibility result is the one thing the phase forbids. |
| L-03 | NF-15 | A desktop screenshot is never presented as a validated game frame; unvalidated capture is reported as unavailable. | Truthfulness of evidence is the phase's whole point. |
| L-04 | NF-16 | **Line-only OCR readers** lose a value inside a label+value line; the field is `absent` with a reason. | The status is honest. A richer status would be an enhancement, and the shipped reader's word-level path already avoids the case. |
| L-05 | NF-16 | HUD reading is **text, not graphics**: bars, meters, colour and minimap are `unsupported` by design. | Measuring fill would require a calibrated, labelled per-title dataset this build does not have. |
| L-06 | NF-16 | Entities are **geometry only** (`kind` always `unknown`, `confidence: None`). | Naming an "enemy" from a connected component would be a fabricated classification. |
| L-07 | NF-16/17 | No commercial-game profile exists; three harness/fixture profiles at one resolution each, and a new resolution is a refusal. | Profile drift is surfaced by the resolution refusal rather than silently mis-reading. |
| L-08 | NF-17 | One numeric counter per rule; bars, timers, checkpoints and quest stages are not measured; no calibrated confidence. | Nothing in the build measures a probability, so no number is reported. |
| L-09 | NF-17 | A missing objective is **not** a failure, and a retained counter is marked `progress last read`. | Both are the phase's explicit honesty rules, pinned by tests. |
| L-10 | NF-18 | Knowledge revalidation needs fresh evidence; stale records are not silently promoted to fact. | The alternative is a self-confirming store. |
| L-11 | NF-19 | Input is bounded to the declared key/mouse/wait kinds with the resource gate and a graceful stop; no controller/gamepad path. | An undeclared input surface would bypass the phase's own risk table. |
| L-12 | NF-20 | Plan records are process-lifetime; one plan executes globally; declared rules only (no inferred tactics). | Coarse but safe; the timeline persists plan status events. |
| L-13 | NF-21 | An unobservable expected effect is reported **unknown / not verifiable**, never "success". | This is the invariant the phase exists to enforce. |
| L-14 | NF-22 | **Live gameplay `NOT_TESTED`** — the loop is verified over a simulated fixture world, labelled `evidence_source: simulated` everywhere it appears. | Claiming autonomous gameplay from a simulation is exactly what the phase forbids; no permitted live target + HUD profile exists in this build. |

### 3.3 Blockers (no code change clears these here)

| ID | Blocker | Evidence |
|---|---|---|
| B-01 | **Live game acceptance** — needs an installed, windowed, offline title with a HUD profile whose completion is independently observable, and the resource gate met. | NF-13A/14/16/17/19/20/22 all report the live half `NOT_TESTED`, consistently and by name. |
| B-02 | **Model-backed reasoning** (NF-16 vision descriptions, NF-18 synthesis quality, NF-20 rationale) — no local model runtime was present (`Echo` provider). | `/status` reports `model_configured: false`; every such path reports its own reason instead of claiming a model read. |
| B-03 | **Non-Windows behaviour** of capture, window enumeration, embedding and the input guard. | Only the CI-parity type check is Linux-shaped; no Linux host was available. |
| B-04 | **NF-18 live corpus** — a real knowledge ingest from a game session needs B-01. | The phase's evaluation says `live evaluation: NOT_TESTED`. |

### 3.4 Out of scope

| ID | Item | Justification |
|---|---|---|
| O-01 | Online multiplayer, anti-cheat circumvention, memory injection, rendering hooks, display-mode forcing, unattended long-duration autonomy. | Forbidden by the phase texts and by the safety constraints of this work. |

---

## 4. Items resolved in this pass

### 4.1 R-01 — Approve And Run received no click after its first run

**Reproduction.** `python -m pytest tests/test_browser_token_reuse_e2e.py -q` failed on the
wait for the second plan+execute pair. Instrumenting the real page at the button's
coordinates showed:

```
click 1: mousedown/mouseup/click → jarvisRunButton     → /browser/plan, /browser/execute
click 2: mousedown → ARTICLE.command-console
         mouseup   → jarvisRunButton
         click     → ARTICLE.command-console             → no request at all
```

The button was enabled, visible, `pointer-events: auto`, its box covered the click point
(`[1071, 240, 150, 37]`), and `document.elementFromPoint` resolved to the button in the same
task — yet the dispatched `mousedown` targeted the card. A `click` is only sent to the
nearest common ancestor of `mousedown` and `mouseup`, so the button's listener never ran.

**Root cause.** `effects.js` tilts every card in
`.surface, .command-console, .info-card, .metric-card, .telemetry-card` on each `mousemove`
with `perspective(800px) rotateX(…) rotateY(…) translateZ(4px)`. Once the JARVIS console
(which contains the button) has carried that 3D transform, pointer input is hit-tested
through the transformed ancestor and retargets to the card. The first click works because no
run has re-rendered that subtree yet; every later click is swallowed, at any delay. Isolation
was by experiment, each variant served through route interception with the repository
untouched:

| Variant | `mousedown` target | Network |
|---|---|---|
| as shipped | `ARTICLE.command-console` | plan, execute (click lost) |
| `.command-console` removed from the tilt family | `jarvisRunButton` | plan, execute, plan, execute |
| `prefers-reduced-motion: reduce` (tilt detached) | `jarvisRunButton` | plan, execute, plan, execute |

A standalone page with the same card, button and real `effects.js` clicked correctly in every
variant, which is why the attribute is the app's nested, re-rendered card and not the
transform alone.

**Fix.** The tilt is refused for any card that contains an interactive control, cached per
card in a `WeakMap`:

```js
const INTERACTIVE_SELECTOR =
  "button, a[href], input, select, textarea, [role='button'], [contenteditable='true']";
// in onMouseMove:  if (!isTiltable(card)) return;
```

The rule is general on purpose: any card that re-renders controls into itself had the same
hazard (the NovaForge and Settings cards among them), so the fix is one invariant — *a
decorative transform must never be applied to an ancestor of a control* — rather than a list
of exemptions.

**Regression pins.**

* `tests/test_browser_token_reuse_e2e.py` — the spec that caught it; passes unmodified.
* `tests/test_reduced_motion_e2e.py` — the tilt's own probe still observes the transform.
* `tests/test_web_platform.py::test_the_card_tilt_never_transforms_an_ancestor_of_a_control`
  — static pin for CI (no browser there): the guard exists, is cached, and is consulted
  before the transform write. **Mutation-tested**: removing the guard fails the pin by name;
  the file was restored byte-identically.

**Live verification (app's own browser panel, 1280×900, fresh load).** Of 86 cards the 42
that hold controls are never transformed (the JARVIS console among them) while the 44
control-free cards still receive `perspective(800px) rotateX(…) …`; two real clicks on
Approve And Run produced `POST /browser/plan, /browser/execute, /browser/plan,
/browser/execute` with **zero console messages** and no failed requests.

### 4.2 R-02 — Plan approval was a flag, not a token

**Finding.** NF-20's limitation 5 recorded that `/plan/execute` gates on `approved: true`.
The panel already sent `approval_token: plan.approval.token`, but nothing minted a token for
a plan and nothing read one — the frontend believed in a gate the backend did not have.

**Fix (at the HTTP boundary, where a remote caller exists).**

* `NovaControlApplication.forge_plan_receipt(payload)` mints a token through the existing
  `_mint_approval` for any plan that is `required` **and** still `pending`, in both payload
  shapes the routes serve (`plan` for build/replan receipts, `current` for the status view).
  A plan that needs no approval gets no token.
* `NovaControlApplication.execute_forge_plan(session_id, payload)` requires and consumes it
  (`_consume_approval`, the same single-use, plan-bound, 5-minute store the desktop/browser/
  phone plans use) whenever the stored plan is `awaiting_approval` and the caller says
  `approved: true`. Missing, unknown, expired, replayed or differently-bound → `ValueError` →
  **403**, and nothing is dispatched.
* `GET /novaforge/sessions/{id}/plan`, `POST …/plan` and `POST …/plan/replan` serve receipts
  through `forge_plan_receipt`, so a reloaded panel holds a token for the plan it shows.
* `NovaForgeManager.plan_gate(plan_id)` is the read-only view the gate needs (status +
  approval), so the application never reaches into the planner.
* The panel sends `approval_token` and, on any refusal, repaints from a fresh preview — an
  expired token is one press from recovery, never a dead end.
* The plan service's in-process `execute(..., approved=True)` contract is **deliberately
  unchanged**: 100+ service-level tests drive the loop in process and the trust boundary is
  the wire, so the change tightens exactly where it matters and nothing inside the process
  had to be rewritten.

**Regression pins** (`tests/test_novaforge_planning.py::PlanningApiTests`, plus one panel
contract test):

| Behaviour | Assertion |
|---|---|
| a plan awaiting approval is served a token | `test_only_a_plan_awaiting_approval_is_served_a_token` (both payload shapes; `not_required`, `approved` and `None` plans untouched) |
| the flag alone runs nothing | `test_executing_an_awaiting_plan_without_its_token_is_refused` → 403, injector untouched |
| a token authorises exactly one attempt | `test_a_minted_token_executes_once_and_a_replay_is_refused` → 200 then 403 |
| a token is bound to its plan | `test_a_token_for_another_plan_does_not_authorize_this_one` → 403 "does not match" |
| expiry is enforced | `test_an_expired_token_is_refused` → 403 "expired" |
| policy-free plans still run | `test_a_plan_that_needs_no_approval_passes_the_gate_untouched` |
| a reloaded panel can approve what it sees | `test_the_status_route_serves_a_usable_token_for_a_pending_plan` |
| the panel sends it | `tests/test_novaforge_ui.py::NF20PlanningPanelTests::test_executing_a_plan_sends_the_server_minted_approval_token` |

All eight **mutation-tested** (guard removed, token never consumed, empty-token check
disabled, and the panel payload stripped — each mutation failed its own pins; every file was
restored byte-identically). API surface description updated and `docs/API.md` regenerated
(`--check` in sync, 372 routes).

---

## 5. Verification executed in this pass

All commands from the repository root with `PYTHONPATH=src` using `.venv/Scripts/python.exe`.
Test files were run in groups because the whole NovaForge set exceeds one command's budget;
each group's exit status is the tested command's own.

| # | Check | Command | Result |
|---|---|---|---|
| 1 | Phase suites (NF) | `pytest tests/test_novaforge.py tests/test_novaforge_api.py tests/test_novaforge_autonomy.py -q` | 220 passed, 21 subtests |
| 2 | Phase suites (NF) | `pytest tests/test_novaforge_benchmark.py …_benchmark_api.py …_coach.py …_control.py …_embedding.py -q` | 273 passed, 154 subtests |
| 3 | Phase suites (NF) | `pytest tests/test_novaforge_evaluation.py …_evaluation_api.py …_experiment_api.py …_gameplay.py …_gameplay_api.py -q` | 153 passed, 51 subtests |
| 4 | Phase suites (NF) | `pytest tests/test_novaforge_knowledge.py …_launcher.py …_library.py …_objectives.py …_observation.py -q` | 266 passed, 22 subtests |
| 5 | Phase suites (NF) | `pytest tests/test_novaforge_planning.py …_policy.py …_state.py …_timeline.py …_vision.py -q` | 239 passed, 35 subtests |
| 6 | Phase suites (NF) | `pytest tests/test_novaforge_ui.py tests/test_novaforge_verification.py -q` | 265 passed, 121 subtests |
| 7 | Dependencies | `pytest tests/test_capture_backends.py tests/test_perception.py tests/test_desktop.py tests/test_desktop_input.py tests/test_vision_model_config.py -q` | 325 passed, 46 subtests |
| 8 | Web/API contract | `pytest tests/test_web_api.py tests/test_web_platform.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | 145 passed, 949 subtests |
| 9 | App + GUI + E2E | `pytest tests/test_application.py tests/test_gui.py tests/test_focus_rings.py tests/test_guided_click_e2e.py -q` | 53 passed, 15 subtests |
| 10 | E2E | `pytest tests/test_telemetry_e2e.py tests/test_tab_focus_walk_e2e.py -q` | 5 passed |
| 11 | **R-01 pins** | `pytest tests/test_browser_token_reuse_e2e.py -q` / `pytest tests/test_reduced_motion_e2e.py -q` | **1 passed** / **2 passed** |
| 12 | **R-02 pins** | `pytest tests/test_novaforge_planning.py tests/test_novaforge_ui.py -q` | 219 passed, 120 subtests |
| 13 | Regression after R-02 | `pytest tests/test_novaforge_verification.py …_gameplay_api.py …_api.py test_api_reference_sync.py test_route_shape_contract.py -q` | 181 passed, 499 subtests |
| 14 | Acceptance scripts | `scripts/novaforge_nf16_state_eval.py`, `…_nf17_objective_eval.py`, `…_nf18_knowledge_eval.py`, `…_nf22_autonomy_acceptance.py` | all **exit 0**: 20/20 fixtures + 3/3 frames + 8/8 fields; 32/32 checks, 0 false completions; 51/51 checks, live `NOT_TESTED`; **16/16 steps, `failed_steps: []`, `live_gameplay: NOT_TESTED`** |
| 15 | Types, both CI views | `python -m mypy src` and `python -m mypy src --platform win32` | **Success: no issues found in 451 source files** (each) |
| 16 | API reference | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| 17 | Live UI | app on `127.0.0.1:8001`, real Chromium, 1280×900 | tilt guard measured (42/86 control-bearing cards skipped, 44 still tilt); two-click flow `plan, execute, plan, execute`; **zero console messages**; served stamp `UI v080447b1` equals the asset token; never-cache trio present |

Not re-run in this pass, and why: the eleven acceptance scripts that launch or probe a real
or disposable **window** (NF-13A/14 embedding, NF-15 observation, NF-19 control, policy,
gameplay, controlled-evaluation) — they need a desktop session and were executed in the
preceding audit pass (11/11 exit 0); nothing in this pass touched capture, window handling or
the input path they exercise. No game was launched and no input was injected into any real
application in either pass.

## 6. What was deliberately not changed

* The **in-process planner contract** (`execute(session_id, plan_id, approved=True)`), so the
  100+ service-level tests and the autonomy loop keep driving the same code path; only the
  wire gained the gate.
* **No test assertion was weakened, skipped or deselected** to reach a green result. The one
  spec that was failing passes unmodified; the only test edits are additions.
* **The tilt effect itself** — it still runs on the 44 control-free cards; the change removes
  the hazard, not the design.
* **No parallelism introduced**: the app keeps one plan at a time, one capture session, one
  input controller — the coarse safety this build is built around.

## 7. Sign-off

| Statement | Status |
|---|---|
| Every in-scope finding is fixed with a pin, or classified as a declared limitation / blocker above | yes |
| No known failing test in the run sets above | yes (the previously failing E2E passes) |
| Both supported type-check views clean | yes |
| API reference synchronised | yes |
| UI and API contracts consistent (panel sends what the route now requires) | yes |
| Cancellation and shutdown leave no service-owned input held | pinned by NF-19/NF-22 suites |
| No unrelated user change lost | yes (byte-identical restores verified after each mutation) |
| Live-game claims | **none made**; `live_gameplay: NOT_TESTED` |
