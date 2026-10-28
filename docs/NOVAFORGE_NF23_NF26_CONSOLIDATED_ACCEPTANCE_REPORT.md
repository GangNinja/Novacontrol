# NovaForge NF-23 … NF-26 — Consolidated Remediation and Acceptance Report

Scope: **NF-23 (real-game compatibility and observation), NF-24 (real-game state understanding and
objective recognition), NF-25 (objective execution and progress validation), NF-26 (adaptive
gameplay, learning and performance optimisation)**, including their integration with NF-13A…NF-22.

Companion register (every finding with severity, disposition and next action):
`docs/NOVAFORGE_NF23_NF26_REMEDIATION_REGISTER.md`. Phase histories remain in
`docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md`, `…NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md`,
`…NF25_REAL_GAME_OBJECTIVE_EXECUTION_REPORT.md` and `…NF26_ADAPTIVE_GAMEPLAY_REPORT.md`; each now
carries a dated section pointing here. Milestone evidence across NF-22…NF-26:
`docs/NOVAFORGE_VERIFICATION.md` Appendix E.

---

## 1. Executive verdict

| Phase | Implementation | Automated verification | Live-game verification | Verdict |
|---|---|---|---|---|
| NF-23 | complete and reachable in the shipped app | **PASS** (full suite + live-window probe) | **BLOCKED** (B-01) | **PARTIAL PASS** |
| NF-24 | complete for the profiles it declares | **PASS** | **BLOCKED** (no commercial profile, B-01) | **PARTIAL PASS** |
| NF-25 | complete | **PASS** | **BLOCKED** (no approved live run, B-02) | **PARTIAL PASS** |
| NF-26 | complete and bounded | **PASS** (74 tests + evaluation) | **BLOCKED** (no live adaptation trials) | **PARTIAL PASS** |

**Why PARTIAL and not PASS WITH LIMITATIONS.** Each phase's declared scope *includes* live-game
behaviour (NF-23 Objective H, NF-24 commercial-game interpretation, NF-25 an approved live task,
NF-26 real-game adaptation). Under the rules this pass applies, a declared requirement that is not
met keeps the phase at PARTIAL, and the same label is used by all four phase reports and by
`docs/NOVAFORGE_VERIFICATION.md` Appendix D. Calling these "limitations" instead would soften a
missing in-scope requirement.

No phase receives FULL PASS. No phase is FAIL: no safety property is violated and no critical path
is broken — the unexercised half is blocked by a title that will not start (B-01) and by the absence
of operator approval for live input (B-02), both external to the code.

**This pass found and fixed six findings** (two high, two medium, two low verification gaps). The most
consequential is R-01: a session-start field the build does not read used to be **silently
dropped**, so an operator who wrote `max_seconds` instead of `time_limit_seconds` got a session
with **no time limit** and no warning. It is now refused by name, and the shipped application
demonstrably caught the audit's own stale payload that way.

---

## 2. Initial repository state

| Item | Value |
|---|---|
| Branch / HEAD | `main` / `8d10852` |
| Pre-existing modifications | 59 tracked files modified, 104 untracked — the NF-01…NF-26 work is uncommitted in this checkout |
| This pass's changes | 3 product/test files edited (`manager.py`, `autonomy.py`, `learning.py` — the latter two in the preceding audit turn), 3 test files extended, 4 documents written/updated, 4 scratch probes |
| Repository integrity | nothing reset, stashed, discarded, reverted or committed; no unrelated file reformatted |
| Runtime | Windows 11, Python 3.13, `.venv` editable install, `PYTHONPATH=src` |
| Safety during the pass | **no game launched**, **no gameplay input sent**, no display-mode change, no process force-killed, the disconnected external drive untouched |

---

## 3. Scope and acceptance criteria

The audit covered, for each phase: implementation presence and reachability, automated tests,
integration with the phases below it, type checks, API-documentation synchronisation, performance
figures, and the honest labelling of anything unverifiable. The acceptance rules applied are the
phase text's own:

* **FULL PASS** only if every declared requirement is implemented, integrated and supported by the
  required evidence, with no unresolved in-scope defect.
* **PASS WITH LIMITATIONS** when the in-scope implementation and verification pass but a declared
  capability or environmental dependency remains unverified.
* **PARTIAL PASS** for an incomplete in-scope requirement or missing integration.
* **FAIL** for a violated safety property or a broken critical path.
* **BLOCKED** for a specific test that genuinely cannot run here.

Live-game evidence cannot be replaced by fixtures, and a simulated run is never reported as live.

---

## 4. Findings, phase by phase

### 4.1 NF-23 — real-game compatibility and observation

**Verified in this pass.** The shipped `NovaControlApplication` (real `LocalDesktopRunner`, temp data
dir) reports `capture_supported: true`, enumerates **5 open windows**, and advertises both capture
backends as available (`printwindow` for the window surface, `gdi_display` for the foreground
display). A session started against the project's own disposable harness window acquired **3
frames** at 1040×440 from `printwindow`, all `fresh`, content `valid`/`repeated`, and produced 3
observation records (`desktop_capture`, `evidence: true`, 34.4 ms for the last). `target_validated`
became true, which is the flag the input boundary requires.

**What the tests pin.** Frame validation is a conjunction of separate facts
(`capture_ok`/`target_visible`/`fresh`/`content_ok` → `livable`); an invalid, stale, wrong-target or
unreadable frame is refused, and a refusal carries `capture_ok: false` + `content: unverified`
rather than an identity claim. The eight observation states map from those facts, and the pause
path reports `observation_paused`/`observation_inactive`.

**Limitation retained.** No live-game frame: the installed candidate's `SteamAPI_Init` fails on
this host (B-01). The capture path itself is proven against a real window — the failure is the
title, not the observation stack.

### 4.2 NF-24 — real-game state understanding and objective recognition

**Verified in this pass.** With a live session over the harness window, the objective subsystem
attached a `WindowTitleVerifier` (`supported: true`) and, on demand, returned
`outcome: verified_failure`, `achieved: false`, `decided: true`, with the reason quoting the
window's own title and the handle/pid it read. That is the phase's core rule working on a live
surface: the objective was **not** achieved, and the system said so — it did not infer success from
the absence of a failure signal, and it did not mistake `verified: true` (the verdict is decided)
for success.

**Semantics checked because they are a trap.** The objective verdict carries `verified: true` even
when the outcome is `verified_failure`. Every NovaForge consumer tested uses the explicit
`status`/`outcome` (`verified_success`, `verified_failure`, `partial_success`, …) rather than the
boolean: `tasks.py` compares `status`, `autonomy.py` selects on `verified_success`,
`verification.py` exposes `OutcomeStatus.VERIFIED_SUCCESS`. `StandardResult.verified` in
`intelligence/results.py` is the one boolean-shaped surface, and it is fed only from a payload's own
`verified` key (no NovaForge objective verdict is adapted into it). Recorded as an observation, not
a defect.

**Limitations retained.** No commercial-game profile; text-only HUD reading; geometry-only entities;
one resolution per profile with a refusal for a new one. Each is the honest answer this build can
give without a labelled per-title dataset.

### 4.3 NF-25 — objective execution and progress validation

**Verified in this pass.** Through the real manager against a live session, an **unapproved** task
was refused with `status: refused`, `code: approval_required`, "A task may send bounded input to
the game, so it needs the operator's explicit approval. Nothing was started." Two earlier attempts
in the same probe were refused earlier still (`invalid_task`) for a missing action budget and for
missing required evidence, which shows field validation precedes approval and nothing is started on
a malformed spec.

**What the tests pin.** The judge's seven fixtures (verified completion with evidence, missing tag,
dispatch-only, progress-without-completion, stale evidence, objective failed, action-policy
exceeded) — 7/7 as expected, **0 false completions**. Preflight refusals for an unapproved task, an
unknown session, an unpinned target, a different window, an unperformable action, an unavailable
control layer and stale or unreadable state. Held input is measured after a run and after a cancel,
and an unreadable control layer reports "unknown", never a fabricated zero.

**Limitations retained.** The action policy is enforced on the claim rather than pre-filtered inside
NF-20's planner; `finish` reports `still_running` beyond `max_seconds + 10`; the task layer has no
HTTP route. Live objective execution remains blocked (B-01/B-02).

### 4.4 NF-26 — adaptive gameplay and learning

**Verified in this pass.** 74 unit tests over the real learning module and planner; the evaluation
script's six honesty controls (improvement, no-difference, tuning-only advantage, safety regression,
insufficient sample, and the six label cases) all behave as declared; a **real** NF-25 `TaskRunner`
report fed into `LearningService.record_task_report` produced a `verified_success`, `valid` record
with 2 dispatched / 1 responded and 7 evidence references, proving the fixtures match the product's
field names.

**Defects fixed (this pass).** An omitted step list was recorded as "zero submitted steps"; the
manager recorded the game's name as the profile; the "learning never gates the task" invariant and
the zero-vs-unmeasured distinction were unpinned; and, one layer below, **an unlabelled autonomy run
was reported as live evidence** (R-02), which would have let simulated evidence be counted as
real-game evidence by the comparator.

**Limitations retained.** No live adaptation trials; comparisons deliberately refuse cross-game,
cross-version and mixed-evidence arms; retention is a cap, not an age window.

---

## 5. Every defect discovered in this pass

| ID | Phase | One-line description | Severity |
|---|---|---|---|
| R-01 | NF-23/24/25 preflight | unknown session-start fields silently ignored (a dropped time limit) → refused by name | High |
| R-02 | NF-22 → NF-26 | undeclared `evidence_source` defaulted to `live` | High |
| R-03 | NF-26 | omitted step lists recorded as "zero submitted steps" | Medium |
| R-04 | NF-26 | game name recorded as the record's profile identity | Medium |
| R-05 | NF-26 | "learning never gates the task" unpinned | Low |
| R-06 | NF-26 | zero-vs-unmeasured distinction unpinned in both directions | Low |

Earlier-phase defects (NF-23 F-01…F-08, NF-24, NF-25, NF-26's fourteen) were **not** re-opened: the
suites that pin them ran green in the full-suite groups. See the register §3.1.

---

## 6. Root cause of each defect

| ID | Root cause |
|---|---|
| R-01 | `validate()` validated only the fields it knew, and `MissionConfig.from_dict` discarded the rest; the autonomy path already refused unknown bounds, the session path did not. |
| R-02 | Two independent defaults asserted the strongest provenance label (`"live"`) for a value nobody had declared, at construction and at restore. |
| R-03 | A cycle without an `execution` block contributed `len([]) = 0`, conflating "did not observe" with "observed zero". |
| R-04 | `run_task` reused the game title as the record's profile argument. |
| R-05 / R-06 | Behaviour was correct but only stated; no test exercised the failure path or the two sides of the measurement rule. |

---

## 7. Source files changed and rationale

| File | Change | Why |
|---|---|---|
| `src/novacontrol/novaforge/manager.py` | `_START_PAYLOAD_KEYS` + rejection of unknown session fields in `validate()`; removed the `profile=<game name>` argument from the NF-26 hand-off | R-01, R-04 |
| `src/novacontrol/novaforge/autonomy.py` | `evidence_source` defaults `"live"` → `"simulated"` at construction and at restore | R-02 |
| `src/novacontrol/novaforge/learning.py` | `actions_requested` is a measurement only when a cycle carried its execution block; `_same_experience` ignores recording time; profile derived from the run | R-03 and the preceding audit's duplicate/profile fixes |
| `tests/test_novaforge.py` | +1 test: unknown start field refused by name | R-01 |
| `tests/test_novaforge_autonomy.py` | +1 test: undeclared provenance is never live; two tests now declare `evidence_source="live"` | R-02 |
| `tests/test_novaforge_nf26.py` | +3 tests: step-list measurement both ways; broken recorder never fails the task; profile is not the game name | R-03, R-05, R-06, R-04 |

No parallel subsystem, launcher, observer, planner or store was introduced; every change is inside
the existing modules that already own the behaviour.

---

## 8. Regression tests added

| Test | Pins |
|---|---|
| `tests/test_novaforge.py::MissionValidationTests::test_an_unknown_start_field_is_refused_by_name` | an unread field is refused by name, accepted fields are listed, known payloads (incl. identity pins) still validate |
| `tests/test_novaforge_autonomy.py::PersistenceAndObservabilityTests::test_undeclared_provenance_is_never_reported_as_live` | controller default and stored-row restore are `simulated` |
| `tests/test_novaforge_nf26.py::ExperienceRecordTests::test_a_cycle_without_its_step_list_is_unmeasured_not_zero` | an omission is `None`, not `0` |
| `tests/test_novaforge_nf26.py::ExperienceRecordTests::test_an_execution_block_with_no_steps_is_a_measured_zero` | a real empty execution is a measured `0` |
| `tests/test_novaforge_nf26.py::ExperienceRecordTests::test_the_game_profile_is_the_state_layers_profile_not_the_game_name` | profile identity comes from the state layer |
| `tests/test_novaforge_nf26.py::ManagerLearningTests::test_a_broken_recorder_never_fails_the_task` | NF-26 observes NF-25, never gates it |

---

## 9. Test commands and exact results

### 9.1 Affected suites (final tree, after the last edit)

| Command | Result | Duration |
|---|---|---|
| `python -m pytest tests/test_novaforge.py -q` | **78 passed** | 10.13 s |
| `python -m pytest` NF-23/24/25/26 + gameplay API + autonomy + API `-q` | **429 passed, 31 subtests** | 53.40 s |
| `python -m pytest` NF UI + web API + route shape + API sync + library UI `-q` | **269 passed, 628 subtests** | 109.95 s |
| `python scripts/novaforge_nf26_learning_eval.py` | 6/6 as expected; 0 false labels; 0 live-evidence claims | 2 s |
| `python scripts/novaforge_nf25_task_eval.py` | 7/7 fixtures; 0 false completions | 1 s |
| `python scripts/novaforge_nf22_autonomy_acceptance.py` | 16 steps, 0 failed; `live_gameplay: NOT_TESTED` | 4 s |

### 9.2 Complete suite

| Group | Result | Duration |
|---|---|---|
| files 1–30 | 860 passed, 1 skipped, 808 subtests | 229.92 s |
| files 31–60 | 854 passed, 1144 subtests | 238.04 s |
| files 61–90 | 1421 passed, 831 subtests | 473.02 s |
| files 91–120 | 1695 passed, 203 subtests | 280.47 s |
| files 121–148 | 1133 passed, 861 subtests | 393.01 s |
| **all 148 files** | **5963 passed, 1 skipped, 3847 subtests, 0 failed** | **1614.46 s (26 m 54 s)** |

The single skip is `tests/test_database.py`, skipped at import because the optional `sqlalchemy`
extra is absent. No test was skipped by choice, deselected, weakened or deleted.

---

## 10. Cross-phase integration evidence

Chain audited: **window identity → validated frame → state observation → objective → approval →
bounded action → fresh observation → outcome verification → experience record → comparison →
adaptation → planner hint.**

| Boundary | Evidence in this pass | Result |
|---|---|---|
| App → NF-23 capture | real `LocalDesktopRunner`, `capture_supported: true`, 5 windows, both backends available | **wired** |
| NF-23 frame → NF-24 observation | 3 frames → 3 observation records, `evidence: true`, 34.4 ms | **wired** |
| NF-24 → NF-21 objective verdict | `WindowTitleVerifier` supported; live verdict `verified_failure`, `achieved: false`, `decided: true` | **wired, honest** |
| NF-25 approval gate | unapproved task refused `approval_required`, nothing started | **enforced** |
| NF-25 preflight ordering | missing budget / missing evidence refused `invalid_task` before approval | **enforced** |
| NF-25 report → NF-26 record | real `TaskRunner` payload → record `verified_success`, `valid`, 2 dispatched / 1 responded, 7 evidence refs | **wired** |
| NF-26 → NF-20 planner | hint moves an avoided kind behind a cheaper one; a 600 s hint clamps to the policy's 10 s | **wired** |
| NF-26 never gates NF-25 | broken recorder: task report unchanged | **pinned** |
| Every route declared and documented | `generate_api_reference.py --check` → in sync; route-shape contract green | **consistent** |
| UI/API payload agreement | 42-step operator workflow (page, assets, chat, command plan/approve/execute, device plans, 19 read surfaces, refusal paths) | **all pass** |

Failure paths were exercised too: unknown session 404, autonomy start 422, phone plan 200 with
pairing guidance (never a 403), replayed approval token 403, and no 5xx anywhere on a refusal.

---

## 11. Type-check and API synchronisation

| Check | Result |
|---|---|
| `python -m mypy src` | **Success: no issues found in 454 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 454 source files** |
| `python scripts/generate_api_reference.py --check` | **`docs/API.md`: in sync** |

Both platform views are clean, so the change does not depend on which view CI runs, and no route was
added or renamed (the R-01 refusal reuses the existing 422 path).

---

## 12. Performance and resource measurements

| Measurement | Value | Source |
|---|---|---|
| Frame capture (printwindow, 1040×440) | ~31 ms per frame | live probe, frame record |
| Observation (capture + record) on the final frame | 34.4 ms | live probe, observation record |
| Experience record | 0.0149 ms | NF-26 eval |
| Metrics computation | 0.0148 ms | NF-26 eval |
| Strategy comparison | 0.0452 ms | NF-26 eval |
| Hint build | 0.0187 ms | NF-26 eval |
| Planning hint delta | 0.00129 ms | NF-26 eval |
| Task judge | 0.0302 ms over 500 iterations; runner end-to-end 0.0774 ms | NF-25 eval |
| Record store | 24/24 rows exact round trip, 1412.7 bytes/record | NF-26 eval |
| Memory at the 200-record cap | 322,534 bytes retained (1612.7 B/record), peak 326,718 | NF-26 eval |
| RAM on this host | 16.50 GB total, **3.96 GB available**, 76.0 % used | `HardwareTelemetry.snapshot()` |
| Launch gate | would **pass** (needs ≥2 GB free and <92 % used) | same snapshot |

No speedup is claimed anywhere: these are measurements of the current build, not comparisons.

---

## 13. Model-readiness findings

* **No local runtime is listening.** `GET http://127.0.0.1:11434/api/tags` returns nothing, so the
  Ollama probe in `build_llm_provider_from_environment()` misses and the build falls back to
  `EchoLLMProvider` (`model_configured: None`).
* **Environment overrides are unset** (`NOVACONTROL_OLLAMA_URL`, `NOVACONTROL_DISABLE_OLLAMA`), so
  the miss is genuine, not a disabled probe.
* **The cloud slot is half-configured**: `data/cloud_llm.json` names `provider: gemini` with an
  **empty `model`**. A cloud call would fail or be unusable, and exercising it would spend the
  owner's credits; it was not called. The key was never printed.
* **Consequence, stated honestly:** model-backed reasoning is **unavailable today**, so every
  NovaForge path that can run deterministically did, and any claim that a model chose a decision
  would be false. Inference latency is reported as **not measured** rather than invented.
* **Minimum next step:** `ollama serve` with one small model (or set a cloud model in Settings),
  then re-measure latency and confirm malformed/unsafe suggestions are rejected before any action.

---

## 14. Live-game preflight status

Prepared, **not executed**. Nothing below permits a launch on its own; each item is a gate that must
be satisfied *and* approved.

| # | Check | Status now |
|---|---|---|
| 1 | Candidate discovered from the internal inventory (`GameCatalog` over the Steam library index / shortcuts / processes) | last measured in NF-23: `Freedom Fighters`, `steam:1347780`, `INSTALLED_AND_LAUNCHABLE`; **not re-scanned this pass** |
| 2 | Installed and launchable | true in NF-23; the title's `SteamAPI_Init` still fails on this host (**B-01**) |
| 3 | Windowed, no forced display-mode change | the title's current config was not changed; a borderless/exclusive target is refused by design |
| 4 | Offline, single-player, no anti-cheat bypass | accepted in NF-23 |
| 5 | Resource gate | **passes now**: 3.96 GB free (gate ≥2 GB), 76 % used (gate <92 %) |
| 6 | Capture backend and target identity | **verified this pass** on a real window (printwindow, identity validated) |
| 7 | Game-specific observation profile | **absent** — the blocker for NF-24's live reading |
| 8 | Small reversible objective with a checkable criterion | designed in the NF-25 plan; verifier path proven this pass with `WindowTitleVerifier` |
| 9 | Negative control (success not detected before achievement) | **performed this pass** on the harness: `verified_failure`, `achieved: false` |
| 10 | Positive verification condition | proven in fixtures; live positive control requires input (**needs approval**) |
| 11 | Bounded allowed input | declared per task (`permitted_actions`, `max_actions`, `max_seconds`) |
| 12 | Stop/cancel procedure | `stop`/`cancel` paths tested; `stop` releases held input |
| 13 | Graceful close and post-test process verification | implemented (`STOP_APP` via WM_CLOSE, process-identity probe); not exercised on the title |
| 14 | Evidence collection and report generation | `data/novaforge/frames/…`, session history, audit trail |

---

## 15. Remaining blockers and limitations

| ID | Item | Why it remains |
|---|---|---|
| B-01 | Candidate title cannot start (`SteamAPI_Init failed`) | host/title combination; not re-attempted (no automatic launch) |
| B-02 | No operator approval for live gameplay input | not granted for this pass |
| B-03 | No local model runtime | nothing listening on 11434 |
| B-04 | Cloud model unset | configuration incomplete; credits not spent |
| L-01…L-11 | The retained limitations listed in the register §3.2 (no commercial profile, text-only HUD, geometry-only entities, policy-on-claim, no task route, schema-drop, scoped comparisons, cap-not-window retention) | each is a declared, honest refusal rather than a defect |

---

## 16. Claims rejected for insufficient evidence

The following are **not** claimed anywhere in this report or the phase reports:

1. **Autonomous gameplay in a real game.** The loop is verified over fixtures and a disposable
   window; no game was played.
2. **Real-game adaptation improvement.** The demonstration is simulation-based by construction.
3. **Universal game compatibility.** Two capture backends and three fixture profiles are not
   universality; fullscreen-exclusive, minimized and occluded titles are refused or classified.
4. **Commercial-game state or objective recognition accuracy.** No labelled commercial HUD data
   exists here.
5. **Model-backed NovaForge decisions.** No model is available (B-03/B-04).
6. **Human-level or unrestricted autonomy.** Not implemented, not tested, not implied.
7. **Cross-game or cross-version learning.** Deliberately refused by the comparator.

---

## 17. Reproduction commands

```bash
export PYTHONPATH=src

# affected phase suites
python -m pytest tests/test_novaforge.py -q
python -m pytest tests/test_novaforge_nf23.py tests/test_novaforge_nf24.py \
    tests/test_novaforge_nf25.py tests/test_novaforge_nf26.py \
    tests/test_novaforge_gameplay_api.py tests/test_novaforge_autonomy.py \
    tests/test_novaforge_api.py -q
python -m pytest tests/test_novaforge_ui.py tests/test_web_api.py \
    tests/test_route_shape_contract.py tests/test_api_reference_sync.py \
    tests/test_novaforge_library_ui.py -q

# complete suite, in file groups (no single run fits a short timeout)
python -m pytest $(ls tests/test_*.py | sed -n '1,30p')   -q
python -m pytest $(ls tests/test_*.py | sed -n '31,60p')  -q
python -m pytest $(ls tests/test_*.py | sed -n '61,90p')  -q
python -m pytest $(ls tests/test_*.py | sed -n '91,120p') -q
python -m pytest $(ls tests/test_*.py | sed -n '121,148p') -q

# static and documentation checks
python -m mypy src
python -m mypy src --platform win32
python scripts/generate_api_reference.py --check

# evidence scripts
python scripts/novaforge_nf26_learning_eval.py
python scripts/novaforge_nf25_task_eval.py
python scripts/novaforge_nf22_autonomy_acceptance.py

# live probes used in this pass (no game, no input; the harness window is disposable)
python .nc_probe_nf23_reachable.py
python .nc_probe_nf23_frames.py
python .nc_audit_workflows.py
```

---

## 18. Final acceptance matrix

| Phase | Requirement group | Implementation | Tests | Live evidence | Status |
|---|---|---|---|---|---|
| NF-23 | Target identity and session association | complete | pass | real window identity validated | **PASS** |
| NF-23 | Capture ladder, frame validity, freshness, content | complete | pass | 3 live frames, content validated, repeated labelled | **PASS** |
| NF-23 | Invalid/stale/wrong-target frames never live | complete | pass | n/a (fixtures) | **PASS** |
| NF-23 | Lifecycle, failure propagation, no second capture stack | complete | pass | pause/stop exercised | **PASS** |
| NF-23 | Live-game capture | n/a | n/a | **none** (B-01) | **BLOCKED** |
| NF-24 | Evidence-grounded state, unknown handling | complete | pass | observation records with evidence | **PASS** |
| NF-24 | Objective recognition without false completion | complete | pass | `verified_failure`, `achieved: false` | **PASS** |
| NF-24 | Commercial-game profile | absent by design | refusal pinned | **none** | **BLOCKED** (in-scope requirement → PARTIAL) |
| NF-25 | Approval, budgets, cancellation, input release | complete | pass | unapproved task refused | **PASS** |
| NF-25 | Judgement and progress from fresh evidence | complete | pass (7/7, 0 false completions) | fixture verdicts | **PASS** |
| NF-25 | Live objective execution | n/a | n/a | **none** (B-01/B-02) | **BLOCKED** |
| NF-26 | Provenance, labels, comparability, reversal | complete | pass (74) | simulation only | **PASS** |
| NF-26 | Baseline-vs-adapted improvement under stated conditions | complete | pass (eval set B) | simulation | **PASS (simulation)** |
| NF-26 | Real-game adaptation | n/a | n/a | **none** | **BLOCKED** |

**Overall: PARTIAL PASS for NF-23, NF-24, NF-25 and NF-26** — PASS on every in-scope requirement
this host can demonstrate, BLOCKED on the live-game half. Six findings were fixed and pinned in this
pass; no in-scope defect is known to remain. The unmet requirements are the live-game ones, and they
are unmet for two stated, external reasons (B-01, B-02) that no code change here can clear.

**NF-27 was not started.** This report and the register are the stop condition for the
NF-23…NF-26 milestone.

---

## 19. CI verification and repository hand-over (2026-10-28, after §18)

This section is dated and append-only: §1–§18 describe the audit as it stood on 2026-10-27. What
follows records the packaging step — updating the documentation, proving the CI jobs on the platform
they actually run on, and committing the work — and the two findings **that step alone produced**.

### 19.1 Documentation brought back in line with the tree

The reports had run well ahead of the front matter. Fixed here: the README's NovaForge table stopped
at **NF-09** while the tree ships **NF-26** (17 rows added — NF-10 … NF-26 plus this acceptance pass —
each carrying its own honest verdict, including NF-13A's *partially verified* and NF-23…NF-26's
*PARTIAL PASS*); the headline suite figure was two audits stale (**3,325 passed / 12 skipped /
2,102 subtests / 108 files** → **5,963 / 1 / 3,847 / 148** on Windows, with the Linux figure now stated
beside it); `docs/STATUS.md` stopped at NF-15 (sections for NF-16 … NF-26 and this pass added, and its
"Next Work" paragraph rewritten — it still described the decision pipeline as missing); and the
limitations list gained the NF-13A…NF-26 facts that a reader of the old text would have missed
(**no real game has been played**, embedding is refused rather than forced, graphical HUD features are
unsupported by construction, no model-backed reasoning was available, capture is Windows-only and
unverified on Linux/macOS).

`docs/NOVAFORGE_VERIFICATION.md` was **not** rewritten: it is the historical NF-01…NF-06 audit and
stays as written, with NF-22…NF-26 evidence in its Appendices A–E.

### 19.2 R-07 — the Linux type-check view rejected five Windows-only attributes

The `typecheck` job runs `mypy src` (Linux view) **and** `mypy src --platform win32`. Both had been
reported clean, because both had been run on **Windows**, where typeshed declares `ctypes.windll` and
`ctypes.WINFUNCTYPE`. From Linux, the native view reported **5 errors in 3 files**:

| File | Reads | Guard that the checker cannot narrow |
|---|---|---|
| `src/novacontrol/gui/theme.py` | `ctypes.windll.user32.SystemParametersInfoW` | `try/except` (the attribute is simply absent) |
| `src/novacontrol/novaforge/embedding.py` | `ctypes.windll.user32`, `ctypes.windll.kernel32`, `ctypes.WINFUNCTYPE` | the `platform_supported` property |
| `src/novacontrol/novaforge/launcher.py` | `ctypes.windll.kernel32.GetDriveTypeW` | `if not os.name == "nt"` |

**Fix, by the repo's own rule.** `telemetry/hardware.py` documents the idiom: guard with a literal
`sys.platform` comparison so the checker narrows the branch. `theme.py` and `launcher.py` now do
exactly that (an early return each; behaviour identical — off Windows the old `theme.py` path already
fell into its `except` and returned `True`, and `launcher.py` already returned `True`).
`Win32WindowApi` deliberately keeps its injectable `platform=` (tests pass an explicit platform), which
`sys.platform` cannot express, so its three reads go through one documented `_windll()` helper plus
`getattr(ctypes, "WINFUNCTYPE")`. **No `# type: ignore` was added** and no mypy override was widened.

### 19.3 R-08 — a planner test executed a bare `python`

The first complete Linux run produced exactly **one** failure:
`tests/test_planner_phase4.py::ApplicationPlannerTests::test_an_approved_command_step_runs_and_is_verified`
asserted `COMPLETED` and got `FAILED`. The step ran `python -c print(42)`, and a stock Ubuntu image has
**no `python`** — only `python3` — so the approved step failed for a reason that has nothing to do with
planner approval or verification. Both that test and its refusal sibling now build the command from
`sys.executable` (`_print_42_command()`), which is what the class is actually about: an approved step
runs and its stdout is captured. Nothing was weakened — the assertions are unchanged.

### 19.4 What the CI environment reported

| Job | Command | Result |
|---|---|---|
| `test` (Ubuntu, Python 3.12, `pip install -e ".[dev]"`) | `python -m pytest tests/ -q` | **5722 passed, 242 skipped, 3791 subtests, 0 failed** — 961.45 s (16 m 01 s) |
| `docs-sync` | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync`, exit 0 |
| `typecheck` (linux view) | `python -m mypy src` | **Success: no issues found in 454 source files** |
| `typecheck` (windows view) | `python -m mypy src --platform win32` | **Success: no issues found in 454 source files** |

The 242 Linux skips are the Windows-only desktop/browser E2E modules (the same tree reports 1 skip on
Windows), and the earlier first run in that environment was **1 failed, 5721 passed, 242 skipped** —
the R-08 failure, against a tree that already carried the R-07 fix. Nothing else differed between the
two platforms.

**Verdicts in §1 and §18 are unchanged**: still **PARTIAL PASS** for all four phases, still BLOCKED on
B-01 and B-02. Packaging the work, and making it pass on Linux, did not touch — and could not have
touched — a single live-game requirement. **No game was launched and no gameplay input was sent** during
this step either.
