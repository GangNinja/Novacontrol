# NovaForge NF-23 … NF-26 — Consolidated Remediation Register

Companion to `docs/NOVAFORGE_NF23_NF26_CONSOLIDATED_ACCEPTANCE_REPORT.md`. This register is the
per-finding record: what was wrong, why, what changed, what pins it, and what is still open. It
supersedes nothing — the NF-23, NF-24, NF-25 and NF-26 phase reports keep their own history, and
their earlier findings (F-01…F-08, the NF-24/NF-25 defect lists, NF-26's fourteen) remain valid and
are marked here as re-verified rather than re-opened.

## 1. Repository and working-tree state

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `8d10852 feat: give NovaForge a guarded action path and an objective verifier` |
| Working tree | **Dirty by design** — 59 tracked modifications and 104 untracked files, all pre-existing except the audit probes named below. Nothing was reset, stashed, discarded, committed or pushed during this pass. |
| Runtime | `.venv` with an editable install; `PYTHONPATH=src` for tests, scripts and mypy |
| Host | Windows 11, Python 3.13, 16.5 GB RAM; no game launched, no input injected into any game |
| Files added by this pass (scratch, not product) | `.nc_probe_nf23_reachable.py`, `.nc_probe_nf23_frames.py`, `.nc_audit_workflows.py`, `.nc_audit_nf25_nf26.py` |

## 2. Disposition key

| Code | Meaning |
|---|---|
| **FIXED** | A real defect; implemented, pinned by a test, and re-verified in this pass. |
| **FIXED (earlier pass)** | A real defect corrected by an earlier phase audit on this tree; re-verified here through the suites that pin it. |
| **RETAINED** | A declared limitation of the phase's defined scope. Not a defect; the code refuses or reports `unknown` rather than guessing. |
| **BLOCKED** | Needs hardware, a target, a model or an operator approval this machine does not have. |
| **OUT OF SCOPE** | Deliberately excluded by the phase text. |

## 3. Findings

### 3.1 Defects

| ID | Phase | Finding | Severity | Disposition |
|---|---|---|---|---|
| **R-01** | NF-23/24/25 (session preflight) | **An unknown field in a session-start payload was silently ignored.** A caller spelling the time limit `max_seconds` (the NF-25 task layer's word) or configuring a checkable objective as `objective_mode` / `objective_parameter` received a session with **no time limit and no objective**, while believing both were set. `_autonomy_limits` already refuses unknown bounds by name; the session path did not. | High (a dropped bound is a safety control) | **FIXED** (§4.1) |
| **R-02** | NF-22 → NF-26 (provenance) | **An unlabelled autonomy run was reported as live evidence.** `AutonomousSessionController` defaulted `evidence_source` to `"live"`, and `_run_from_dict` restored a stored run without the field as `"live"`. An undeclared run could therefore enter NF-26's ledger as real-game evidence — the promotion NF-26 refuses inside its own module, performed one layer below it. | High | **FIXED** (§4.2) |
| **R-03** | NF-26 (record facts) | **A run payload that omitted its step lists was recorded as "zero submitted steps".** `from_task_report` summed `len(steps)` over the cycles it found; a cycle with no `execution` block contributed 0, so a report that said it dispatched inputs either failed validation (`dispatched > requested`) or claimed a measurement nobody took. | Medium | **FIXED** (§4.3) |
| **R-04** | NF-26 (manager hand-off) | **The manager recorded the game's NAME as the record's profile identity.** `run_task` passed `profile=payload["game"]`, claiming a profile nothing observed. | Medium | **FIXED** (§4.4) |
| **R-05** | NF-26 (coverage) | **The "learning never gates the task" invariant was stated but unpinned.** `run_task`'s docstring promises recording never changes the answer; no test broke the recorder while a task ran. | Low (verification gap) | **FIXED** (§4.5) |
| **R-06** | NF-26 (coverage) | **The zero-vs-unmeasured distinction had no pin in either direction** — the fix for R-03 could silently collapse into "always unknown" without a test noticing. | Low (verification gap) | **FIXED** (§4.3) |
| **R-07** | CI (type check, both phases) | **The Linux mypy view rejected five Windows-only `ctypes` attributes**, so the `typecheck` job would have failed on a tree whose Windows views were clean: `gui/theme.py` (`ctypes.windll`), `novaforge/embedding.py` (twice in `_require`, plus `ctypes.WINFUNCTYPE` in `find_window`) and `novaforge/launcher.py` (`ctypes.windll`). The guards were runtime checks (`os.name`, a `platform_supported` property) that a type checker cannot narrow, and the repo's own convention — documented in `telemetry/hardware.py` — is a literal `sys.platform` comparison for exactly this reason. | High (a red CI job on the pushed tree) | **FIXED** (§4.6) |
| **R-08** | CI (test portability, phase 4 planner) | **`test_an_approved_command_step_runs_and_is_verified` executed a bare `python`**, which a stock Ubuntu image does not provide (only `python3`), so the approved step failed and the test asserted `COMPLETED` against `FAILED`. The class tests that an approved command runs and its stdout is captured, not that the host has a `python` alias. | Medium (a failing test on the Linux matrix) | **FIXED** (§4.6) |

Earlier-phase defects, re-verified in this pass (all **FIXED (earlier pass)**, each still pinned by
the suite that this pass re-ran):

| Phase | Findings re-verified | Suite that pins them (this pass) |
|---|---|---|
| NF-23 | F-01 (side-car thumbnail), F-02 (plumbing/equivalence/orphan sweep), F-03/F-04 (ladder + eight states), F-06 (observation pause/inactive refusals), F-07 (launch working directory), F-08 (refusal envelope `capture_ok`/`content`) | `test_capture_backends.py`, `test_novaforge_nf23.py`, `test_novaforge_observation.py`, `test_desktop.py` — all green in the full-suite groups |
| NF-24 | State/objective evidence handling, profile-mismatch refusal, false-completion guards (report §11) | `test_novaforge_nf24.py`, `test_novaforge_state.py`, `test_novaforge_objectives.py` — green |
| NF-25 | Preflight refusals, judge fixtures, held-input measurement, wrong-task/wrong-run guards, cancellation honesty (report §12) | `test_novaforge_nf25.py` — green; eval 7/7 fixtures, 0 false completions |
| NF-26 | The fourteen defects of its two verification passes (report appendix) | `test_novaforge_nf26.py` — 74 passed |

### 3.2 Retained limitations

| ID | Phase | Limitation | Why it is correct to retain |
|---|---|---|---|
| L-01 | NF-23 | **No live-game frame has been acquired on this host.** The candidate title's Steam API initialisation fails (`SteamAPI_Init failed`, measured), so the game exits in ~0.02 s. | The blocker is the title/OS combination, not the capture path: the same ladder captured a real window on this host in this pass (§5.2). Faking a live result is the one thing the phase forbids. |
| L-02 | NF-23 | A **repeated** frame is reported `repeated`, not `blank` and not silently "fresh content". | A static window legitimately produces identical pixels; calling that a failure would refuse valid observation, and calling it a new state would invent change. |
| L-03 | NF-24 | No **commercial-game profile**; three harness/fixture profiles at one resolution each, and a new resolution is a refusal. | A profile without provenance is a guess. |
| L-04 | NF-24 | HUD reading is **text, not graphics**: bars, meters, colour and minimap are `unsupported`. | Measuring fill needs a calibrated, labelled per-title dataset this build does not have. |
| L-05 | NF-24 | Entities are **geometry only** (`kind` always `unknown`). | Naming an "enemy" from a connected component would be fabricated classification. |
| L-06 | NF-25 | The action policy is enforced **on the claim**, not as a pre-filter inside NF-20's planner. | Pre-filtering is a cross-phase change; the task reports a breach and refuses completion instead. |
| L-07 | NF-25 | `TaskRunner.finish` waits `max_seconds + 10`; a run that overruns reports `still_running`. | Honest non-terminal answer instead of blocking the caller. |
| L-08 | NF-25 | **No HTTP route** for the task layer; it is reachable through `NovaForgeManager.run_task`. | A documented route is a separate decision; the layer was in scope, the route was not. |
| L-09 | NF-26 | Records are versioned and **any other schema version is dropped and counted**, never migrated. | Migrating under today's field meanings is how stale data becomes a modern label. |
| L-10 | NF-26 | Comparisons refuse an arm that spans two games, two strategy versions or both evidence classes. | A pooled rate would describe neither profile; the caller scopes. |
| L-11 | NF-26 | A **time-windowed** retention policy is absent; growth is bounded by a record cap. | A cap is honest and testable; an age window was not in scope. |

### 3.3 Blockers

| ID | Blocker | Evidence |
|---|---|---|
| **B-01** | The installed candidate title cannot start on this host (`SteamAPI_Init failed`; Steam's own launch fails identically). | NF-23 report §9, re-measured in that phase's independent verification. Not re-attempted in this pass: no launch may be made automatically. |
| **B-02** | **No operator approval for live gameplay input** exists for this pass. | This pass sent no input to any game; the harness probe sent none either. |
| **B-03** | **No local model runtime is listening.** `http://127.0.0.1:11434/api/tags` returns nothing, `NOVACONTROL_OLLAMA_URL`/`NOVACONTROL_DISABLE_OLLAMA` are unset, and `build_llm_provider_from_environment()` returns `EchoLLMProvider` with `model_configured` unset. | measured this pass (§6) |
| **B-04** | **The cloud slot is configured but incomplete**: `data/cloud_llm.json` names `provider: gemini` and an empty `model`. Exercising it would spend the owner's credits and is out of scope without approval. | shape read this pass; key never printed (§6) |

## 4. Detail on the defects fixed in this pass

### 4.1 R-01 — A session-start field this build ignores is now refused by name

**Root cause.** `NovaForgeManager.validate` built a `MissionConfig` from the payload and validated
only the fields it knew; every other key was discarded by `MissionConfig.from_dict`. The autonomy
path had already learned this lesson (`_autonomy_limits` refuses unknown bounds), but the session
path had not.

**Change.** `src/novacontrol/novaforge/manager.py`:

* added `_START_PAYLOAD_KEYS` = the `MissionConfig` fields plus the two identity pins the UI sends
  (`expected_handle`, `expected_pid`);
* `validate()` now appends an error naming every key outside that set and listing the accepted
  fields, so `start()` refuses with a 422 instead of starting something weaker than asked for.

**Pin.** `tests/test_novaforge.py::MissionValidationTests::test_an_unknown_start_field_is_refused_by_name`
— the refusal names `max_seconds` and `objective_mode`, lists `time_limit_seconds` as accepted, and
`start()` raises `MissionValidationError`. The same test asserts a payload with the known fields
(including the identity pins) still validates.

**Live confirmation.** The audit's own capture probe still carried the old spelling
(`max_seconds`, `objective_mode`, `objective_parameter`); after the change the **shipped
application** refused it with:

```
Unknown session field(s) 'max_seconds', 'objective_mode', 'objective_parameter': this build would
ignore them. Accepted fields are expected_handle, expected_pid, frame_interval_seconds,
max_actions, name, objective, success_criteria, target_title, time_limit_seconds.
```

That is the defect being caught in the exact situation that used to be silent.

### 4.2 R-02 — Undeclared provenance is no longer reported as live

**Root cause.** Two independent defaults asserted the strongest label without evidence:
`AutonomousSessionController.__init__(evidence_source="live")` and
`_run_from_dict(... or "live")` for a stored row without the field.

**Change.** `src/novacontrol/novaforge/autonomy.py`: both defaults are now `"simulated"`. The
production manager still passes `"live"` explicitly (with its comment explaining that its evidence
comes from the real observation pipeline) and the acceptance harness passes `"simulated"`
explicitly, so no real run changed class — only the undeclared case did.

**Pin.** `tests/test_novaforge_autonomy.py::test_undeclared_provenance_is_never_reported_as_live`:
an unlabelled controller reports `simulated`, and a stored row that lost the field restores as
`simulated`. Two tests that had been relying on the old default now declare
`evidence_source="live"` themselves; no assertion was relaxed.

### 4.3 R-03 / R-06 — An omitted step list is unmeasured, an empty one is zero

**Root cause.** `ExperienceRecord.from_task_report` counted `len(steps)` for each cycle it found,
treating an absent `execution` block as "this cycle ran zero steps".

**Change.** `src/novacontrol/novaforge/learning.py`: the summed count is a **measurement** only if
at least one cycle carried an `execution` block; otherwise `actions_requested` stays `None`
(unmeasured). A genuine empty `steps` list inside a present block is still a measured `0`.

**Pins.** `tests/test_novaforge_nf26.py`:
`test_a_cycle_without_its_step_list_is_unmeasured_not_zero` and
`test_an_execution_block_with_no_steps_is_a_measured_zero` — both directions, so the fix cannot
degrade into "never measured".

### 4.4 R-04 — The record takes the state layer's profile, not the game's name

**Root cause.** `NovaForgeManager.run_task` passed `profile=str(payload.get("game"))` — a title is
not a profile identity.

**Change.** `src/novacontrol/novaforge/manager.py`: the argument is gone; the record derives the
profile from the run's own cycle state (and records none when the run named none).

**Pin.** `tests/test_novaforge_nf26.py::test_the_game_profile_is_the_state_layers_profile_not_the_game_name`
— a run whose cycle names `menu/profile-a` records that, a run that names none records `""`, and
both keep `game` as the game.

### 4.5 R-05 — A broken recorder cannot fail the task (now pinned)

**Root cause.** The invariant lived in `run_task`'s docstring and its `except` blocks, with no test
that exercised the failure.

**Change.** None to production code — the behaviour was already correct. Added
`tests/test_novaforge_nf26.py::ManagerLearningTests::test_a_broken_recorder_never_fails_the_task`:
both `active_profile()` and `record_task_report()` raise, and the task's own report comes back
unchanged.

### 4.6 R-07 / R-08 — the two defects only the Linux run could show

**Root cause (R-07).** Both platform views are *supposed* to be checked from one Linux runner
(`mypy src` and `mypy src --platform win32`), and both were clean **on Windows**, where typeshed
declares `ctypes.windll`/`WINFUNCTYPE`. On Linux the native view is the strict one, and the five
attribute reads above are declared Windows-only. The runtime guards did not help because
`os.name == "nt"` and a `platform_supported` property are values, not platform narrowing.

**Change (R-07).** `gui/theme.py` and `novaforge/launcher.py` now guard with a literal
`sys.platform != "win32"` early return — the idiom `telemetry/hardware.py` already documents for this

exact problem — so the checker narrows the branch in both views and no suppression was needed.
`Win32WindowApi` keeps its injectable `platform=` (tests pass an explicit platform), which
`sys.platform` cannot express, so its three Windows-only reads go through one documented
`_windll()` helper and a `getattr(ctypes, "WINFUNCTYPE")`; behaviour is unchanged on every host,
including the AttributeError the old code raised if it were ever reached off Windows.

**Root cause (R-08).** The command string named the interpreter by its alias. Nothing in the
product is wrong; the test was not hermetic. Its sibling refusal test used the same string, so both
now build the command from `sys.executable` via one `_print_42_command()` helper.

**Verification.** First Linux run: **1 failed, 5721 passed, 242 skipped** — the failure being R-08
against the tree that already carried the R-07 fix. After the test fix:
`pytest tests/test_planner_phase4.py -q` → **67 passed, 5 subtests** (Linux, 4.00 s; Windows,
49.97 s before the fix and green after), and the complete Linux suite → **5722 passed, 242 skipped,
3791 subtests, 0 failed** in 961.45 s. `mypy src` and `mypy src --platform win32` are both clean over
454 files from Linux. No assertion was weakened, no test skipped, no error downgraded.

## 5. Verification ledger

### 5.1 Commands and exact results (final tree)

| Command | Result | Duration |
|---|---|---|
| `python -m pytest tests/test_novaforge.py -q` | **78 passed** | 10.13 s |
| `python -m pytest tests/test_novaforge_nf23.py tests/test_novaforge_nf24.py tests/test_novaforge_nf25.py tests/test_novaforge_nf26.py tests/test_novaforge_gameplay_api.py tests/test_novaforge_autonomy.py tests/test_novaforge_api.py -q` | **429 passed, 31 subtests** | 53.40 s |
| `python -m pytest tests/test_novaforge_ui.py tests/test_web_api.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py tests/test_novaforge_library_ui.py -q` | **269 passed, 628 subtests** | 109.95 s |
| Full suite, files 1–30 | 860 passed, 1 skipped, 808 subtests | 229.92 s |
| Full suite, files 31–60 | 854 passed, 1144 subtests | 238.04 s |
| Full suite, files 61–90 | 1421 passed, 831 subtests | 473.02 s |
| Full suite, files 91–120 | 1695 passed, 203 subtests | 280.47 s |
| Full suite, files 121–148 | 1133 passed, 861 subtests | 393.01 s |
| **Complete suite (148 files)** | **5963 passed, 1 skipped, 3847 subtests, 0 failed** | **1614.46 s (26 m 54 s)** |
| `python -m mypy src` | **Success: no issues found in 454 source files** | warm cache |
| `python -m mypy src --platform win32` | **Success: no issues found in 454 source files** | warm cache |
| `python scripts/generate_api_reference.py --check` | **`docs/API.md`: in sync** | 2 s |
| `python scripts/novaforge_nf26_learning_eval.py` | 6/6 as expected; 0 false labels; 0 live-evidence claims; exit 0 | 2 s |
| `python scripts/novaforge_nf25_task_eval.py` | 7/7 fixtures; 0 false completions; exit 0 | 1 s |
| **CI verification, 2026-10-28** — `pytest tests/ -q` on Linux (Ubuntu, Python 3.12, editable `.[dev]` install, i.e. the `test` job's environment) | **5722 passed, 242 skipped, 3791 subtests, 0 failed** | 961.45 s (16 m 01 s) |
| `python -m mypy src` from Linux (the `typecheck` job's native view) | **Success: no issues found in 454 source files** — after R-07; it reported **5 errors in 3 files** before | — |
| `python -m mypy src --platform win32` from Linux | **Success: no issues found in 454 source files** | — |
| `python scripts/generate_api_reference.py --check` from Linux | **`docs/API.md`: in sync**, exit 0 | 2 s |

The two mypy rows above the CI block were produced on the **Windows** host, which is why R-07 (a
Linux-only view failure) survived them; the same commands were then run from Linux and both pass.
The 242 Linux skips are the Windows-only desktop/browser E2E modules and their subtests — the same
tree reports 1 skip on Windows, so no test is silently dropped on either platform.
| `python scripts/novaforge_nf22_autonomy_acceptance.py` | 16 steps, 0 failed; `live_gameplay: NOT_TESTED`; exit 0 | 4 s |

The single skip is `tests/test_database.py`, skipped at import because the optional `sqlalchemy`
extra is not installed (confirmed with `-rs`). No test was deselected, weakened or deleted.

### 5.2 Live probes (no game, no input)

| Probe | What it proved |
|---|---|
| `.nc_probe_nf23_reachable.py` — shipped `NovaControlApplication` + real `LocalDesktopRunner`, temp data dir | `windows.available: true`, `capture_supported: true`, **5 windows enumerated**, both capture backends `available: true`, `action_execution` and `game_control` available. NF-23 is wired in the product, not only in tests. |
| `.nc_probe_nf23_frames.py` — the project's own disposable harness window | **3 frames**, `printwindow` backend, 1040×440, `fresh: true`, content `valid` then `repeated`, all `livable`; **3 observation records** (latest: `desktop_capture`, `evidence: true`, 34.4 ms, frame_seq 3); `target_validated: true`; objective verifier supported (`WindowTitleVerifier`) |
| same probe, objective negative control | verdict `verified_failure`, `achieved: false`, `decided: true` — success is **not** claimed before the objective is met; the reason quotes the window's own title |
| same probe, unapproved task | NF-25 refusal `status: refused`, `code: approval_required`, "Nothing was started." (earlier runs also showed `invalid_task` for a missing budget and for missing required evidence, i.e. field validation precedes approval) |

### 5.3 Model and hardware readiness (measured this pass)

| Fact | Value |
|---|---|
| Local model endpoint | `http://127.0.0.1:11434/api/tags` → empty response (nothing listening) |
| Environment overrides | `NOVACONTROL_OLLAMA_URL` unset, `NOVACONTROL_DISABLE_OLLAMA` unset |
| Boot provider | `EchoLLMProvider`, `model_configured: None` |
| Cloud slot | `provider: gemini`, `model: ""` (key present, never printed); not exercised — that would spend the owner's credits |
| RAM | 16.50 GB total, **3.96 GB available**, 76.0 % used → the launch gate (≥2 GB free, <92 % used) would pass |
| Inference latency | **not measured** — no model is available to measure |

## 6. Next actions

| For | Action |
|---|---|
| B-01 | Re-attempt the candidate title's launch only with operator approval, on a machine where its Steam API initialisation succeeds (or with a different, verified offline title). |
| B-02 | Grant explicit approval for one bounded live run (the preflight in the consolidated report §14 is the checklist). |
| B-03 | Start a local model runtime (`ollama serve` with a small model) or configure a cloud model, then re-measure latency and structured-output rejection. |
| B-04 | Set a cloud model name in Settings before selecting the cloud mode; the empty `model` cannot answer. |
| L-01/L-03 | A commercial-game profile line-only reader for the chosen title, from labelled frames captured under approval. |
| NF-27 | Not started; this register and the consolidated report are the stop condition for the NF-23…NF-26 milestone. |
