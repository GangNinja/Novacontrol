# Group 1 — Phases 27–31: Audit, Remediation and Acceptance Report

**Date:** 2026-11-26
**Branch/HEAD:** `main` / `afa5ff6`, uncommitted worktree (all pre-existing work preserved)
**Commit/push/deploy:** none
**Group verdict:** **NOT CLOSED** — see §G (first pass), §H (second pass) and §I
(third pass). Three defects were found and remediated (D1; D10 and D11), and a
fourth, **D12** — the Phase 29 operator grounding a window-named objective on an
unrelated live application — is **remediated and CLOSED** in §J, and with it 29
#5. 29 #20's operator-loop half (DF-6) is **PARTIAL** as of the 2026-11-27
current-tree run (§N): the fail-closed behaviour passes, the two identity-containment
assertions do not, and the earlier 16/16 PASS is retracted. What remains outstanding
is environmental: DF-1…DF-5, DF-7, the DF-8 product decision and DF-6-L1; none is
converted into a PASS. A DF-6-L1 containment investigation (§O) finds no Windows mechanism
that can recall in-flight synthetic events: containment requires narrowing scope to UI
Automation patterns or an isolated session, and needs an operator decision. Phase 31 #6 (cross-environment transfer, browser half) is PASS.

This report is the group-level record. The per-phase reports
(`docs/PHASE27_REPORT.md` … `docs/PHASE31_REPORT.md`) remain the detailed
phase records and were updated with an audit addendum where this audit changed
their evidence.

---

## A. Phase 27–31 audit report

### A.1 Repository reconnaissance (baseline, before any modification)

| Item | Finding |
| --- | --- |
| Branch | `main`, tracking `origin/main`, HEAD `afa5ff6` |
| Working tree | Large pre-existing uncommitted change set (application, agents, browser, desktop, memory, api, docs, NovaForge, NovaMarket-adjacent files). **Preserved untouched** except the files this audit changed. |
| Phase tests present | `tests/test_continual_learning.py` (27), `tests/test_multi_agent_collaboration.py` (28), `tests/test_phase29_computer.py` (29), `tests/test_phase30_operations.py` (30), `tests/test_phase31_skills.py` (31) |
| Phase reports present | `docs/PHASE27_REPORT.md` … `docs/PHASE31_REPORT.md` |
| Baseline phase-suite run | **207 passed** in 55.90 s (the five suites above, before any change) |

### A.2 The recovered phase map (the brief's titles were partly wrong)

The request supplied assumed titles for Phases 28, 30 and 31. The repository
does not support those titles; the recovered map from the phase reports, the
packages and the tests is:

| Phase | Recovered title | Package | Report verdict at audit start |
| --- | --- | --- | --- |
| 27 | Continual Learning & Self-Improvement | `continual/` | PASS (with limitations) |
| 28 | **Multi-Agent Collaboration** (not "Skills") | `agents/collaboration*.py` | PASS (focused-verification closure) |
| 29 | Universal Computer & Device Operator | `computer/` + agentcore/desktop/browser | PARTIAL |
| 30 | **Persistent Autonomy & Personal Operations** | `operations/` | PARTIAL |
| 31 | **Transfer Learning & General-Purpose Skills** (skills live here, not in 28) | `skills/` | PARTIAL |

Cross-platform/environment transfer is **not** a separate phase: it is a
requirement inside Phase 29 (platform support) and Phase 31 (cross-environment
skill transfer). No phase title was invented and no roadmap file was edited.

**Documentation defect D2:** there is no canonical specification for Phases
27–31 in the repository. `ROADMAP.md` stops at Phase 20 and `docs/PHASES.md`
stops at Phase 15. Every phase report states this and uses the brief supplied
with its own request as the contract. This is a real specification gap, not
something this audit may paper over by writing a retrospective spec.

### A.3 Implementation assessment (code, not reports)

Read directly during this audit:

| Area | What exists | Assessment |
| --- | --- | --- |
| Phase 27 | `continual/` (9 modules), 15 `/continual/*` routes, capture from chat, desktop/browser commands, agentic runs, verified game launches and self-management recovery | Real; evidence-derived; advisory-only boundaries published |
| Phase 28 | `agents/collaboration.py` (2,779 lines), `collaboration_controls.py`, `analysis.py`, `verification.py`; 8 `/collaboration/*` routes | Real; reuses the Phase 12 specialist pipeline |
| Phase 29 | `computer/` (models, devices, context, history, operator), reuse of `agentcore` orchestrator/perception, desktop/browser controllers | Real; token-gated; live-verified on Windows |
| Phase 30 | `operations/` (models, manager, monitor, service, learning); 15 routes; dashboard | Real; durable objectives over the shared store |
| Phase 31 | `skills/` (models, catalog, acquisition, transfer, composition, service); 15 `/skills*` routes | Real; live-verified acquisition/transfer/composition |

**Defect found by inspection (D1):** `src/novacontrol/agents/` contained **zero**
references to skills (`grep -ri skill src/novacontrol/agents` → no matches).
Phase 31's report recorded this as limitation 3 ("collaboration pipelines have
no in-pipeline skill-selection tool; a future phase must add one"), while
Phase 28's report claimed PASS for integration. Both facts were true at the same
time, which is exactly the sort of gap a group audit exists to catch: skills
could be executed standalone and from Phase 30 objectives, but no specialist
could select one inside its own pipeline.

### A.4 Integration boundaries exercised

| Integration | Required verification | Result |
| --- | --- | --- |
| 27 ↔ 28 | Collaboration captures verified evidence, never upgrades unverified work | PASS — `_capture_learning` gates on the run's verification; 61 collaboration tests |
| 27 ↔ 29 | Computer-operation outcomes captured and evaluated | PASS — `application.py` captures agentic operator runs; observe-only runs are not invented as successes |
| 28 ↔ 29 | Skills/agents actually executed in computer workflows | PARTIAL — desktop/browser controllers are the executed capability; `ComputerOperator` is not a skill step type (unchanged, documented) |
| 27 ↔ 30 | Learning and recovery interaction | PASS (advisory) — `operations/learning.py` derives evidence; it does not auto-adjust schedules |
| 28 ↔ 30 | Skill/agent orchestration through objectives | PASS — `agent="skill:<id>"` dispatch with approval parking and checkpoint resume (live F) |
| 29 ↔ 30 | Computer-operation reliability through approved objectives | PASS — live Scenario G (real Calculator window verified) |
| 30 ↔ 31 | Environment and recovery integration | PASS — live F: objective restored across a fresh application instance, verified skill task |
| **28 ↔ 31** | **Skill selected and executed inside the specialist pipeline** | **Was FAIL (missing implementation) → fixed and tested in this audit (D1)** |
| 27–31 ↔ existing NovaControl | Planner, agents, memory, tools, runtime, events, permissions | PASS — no second planner/executor/registry introduced; `ToolRegistry`, `EventBus`, `JsonStateStore`, `PermissionManager`, `SpecialistPipeline` all reused |

Real application paths used for verification, not mocks: `NovaControlApplication`
with a real `JsonStateStore`, the real tool executor, the real desktop/browser
controllers, the real `AgenticOrchestrator`, and real Windows windows.

### A.5 Findings summary

| ID | Class | Phase | Severity | Status |
| --- | --- | --- | --- | --- |
| D1 | MISSING IMPLEMENTATION / INTEGRATION DEFECT | 28 ↔ 31 | High | **FIXED & VERIFIED** |
| D2 | DOCUMENTATION DEFECT | 27–31 | Medium | Documented (no canonical spec exists) |
| D3 | VERIFICATION GAP | 29 | Medium | **CLOSED** — real Win32 dialog induced |
| D4 | VERIFICATION GAP | 29 | Medium | Partly closed (crash after planning); mid-action crash open |
| D5 | VERIFICATION GAP | 30 | Medium | Partly closed (unclean exit); OS reboot BLOCKED |
| D6 | VERIFICATION GAP (environment) | 29 | Medium | BLOCKED — see §G |
| D7 | VERIFICATION GAP | 31 | Medium | Browser transfer not run; cross-OS not implemented |
| D8 | DOCUMENTATION DEFECT | 28 | Low | **FIXED** — Phase 28 report now states the skill-selection gap and its closure |
| D9 | TYPE DEFECT (minor, self-found) | — | Low | **FIXED** — the new provider adapter returned `Any`; now returns a validated mapping |

---

## B. Defect register

### D1 — No in-pipeline skill selection (Phase 28 ↔ 31)

| Field | Value |
| --- | --- |
| Class | MISSING IMPLEMENTATION → INTEGRATION DEFECT |
| Requirement | Phase 31 #12 "Phase 28 multi-agent integration"; Group brief §3 Phase 28 "actual in-pipeline skill selection rather than merely standalone skill execution" |
| Reproduction | `grep -ri skill src/novacontrol/agents` → 0 matches; with a validated skill in the library, a specialist given a goal it cannot plan reported "nothing to do" and never consulted the library |
| Root cause | The specialist pipeline (`agents/pipeline.py`) had no seam to the skill library. Skills could be listed, adapted and executed through `SkillService` and through Phase 30 objectives, but the pipeline's planner had only the agent's own `plan()` hook |
| Severity | High — the phase's central integration claim could not be true |
| Files | `src/novacontrol/agents/pipeline.py`, `src/novacontrol/application.py` |
| Fix | Added an optional `SkillProvider` protocol and a `skills=` seam to `SpecialistPipeline`. When an agent's own plan is **empty** (or a task explicitly names a skill), the pipeline asks the library for matches, refuses unvalidated/incompatible/under-specified ones, builds one `skill.execute` step, authorizes it through the existing `PermissionManager`, executes it through `SkillService`, and verifies it from the library's own verified verdict. The application wires its single `SkillService` in as the provider |
| Regression tests | `tests/test_phase31_skills.py::InPipelineSkillSelectionTests` (9) and `::InPipelineSkillApplicationTests` (2) |
| Verification | Phase 31 suite **42 passed** (was 31); app-level test runs a real validated skill (`machine_facts`) through the application's own pipeline and asserts the agent's own `execute` never ran |
| Final status | **FIXED — VERIFIED** |

Design constraints deliberately preserved: no second catalog, registry,
executor or permission table; a plan an agent already has is **never** replaced
(tested); an unvalidated version is not selected (tested); a skill waiting for
approval is reported as a refusal, never a success (tested).

### D2 — No canonical specification for Phases 27–31

| Field | Value |
| --- | --- |
| Class | DOCUMENTATION DEFECT / SPECIFICATION GAP |
| Requirement | Group brief §2.4 "Read the original specifications for Phases 27–31" |
| Reproduction | `ROADMAP.md` ends at Phase 20; `docs/PHASES.md` ends at Phase 15; no `docs/PHASE2x_SPEC*` files exist |
| Root cause | The phases were executed from per-request briefs; the repository never received the canonical text |
| Severity | Medium — acceptance cannot be judged against a document that does not exist |
| Fix | Not invented. The gap is recorded here and in each phase report; the per-request brief remains the only contract, and the recovered titles are documented in §A.2 |
| Final status | **OPEN (documentation), not closable from inside the repository** |

### D3 — Unexpected dialog never induced (Phase 29)

| Field | Value |
| --- | --- |
| Class | VERIFICATION GAP |
| Requirement | Phase 29 §15 "real application verification", §12 "unexpected dialog handling" |
| Reproduction | Phase 29 report row 25: "a genuinely unexpected dialog was not induced (the Save As dialog was expected by the workflow)" |
| Fix | `tmp/group1/verify_resilience.py` scenario 1 raises a real Win32 message box (`MessageBoxW`, class `#32770`) titled `NovaGroup1UnexpectedDialog` while unrelated applications are open, then observes through production `ComputerOperator.observe` |
| Evidence | `tmp/group1/resilience_evidence.json`: the dialog is in the observed window list, `has_dialog` is true, `dialogs` contains the title, and the observation still carries a screenshot hash. **4/4 checks passed** |
| Final status | **CLOSED (verified)** |

### D4 — Forced application crash during a workflow (Phase 29)

| Field | Value |
| --- | --- |
| Class | VERIFICATION GAP |
| Requirement | Phase 29 §12 "recovery: unresponsive application / forced crash" |
| Fix | Scenario 2 plans `close the NovaGroup1Probe window`, then **kills the target process** between planning and acting, then executes the approved plan |
| Evidence | `resilience_evidence.json`: the run reported `status: failed`, summary "1 action(s) executed, 1 recovery attempt(s). No observed evidence supports goal completion.", history `failed`; no success was fabricated against a dead window. **3/3 checks passed** |
| Residual gap | A crash **mid-action** (target dies while an input is being delivered) was not induced. The verified case is a crash inside the running workflow, at the plan→act boundary |
| Final status | **PARTIAL — crash-after-planning verified; mid-action crash unverified** |

### D5 — OS-level restart (Phase 30)

| Field | Value |
| --- | --- |
| Class | VERIFICATION GAP |
| Requirement | Phase 30 #3 "task state survives app and system restarts" |
| Fix attempted | Scenario 3 writes an objective and task in a child process that then calls `os._exit(9)` — no clean shutdown, no explicit flush — and reopens the store in the parent |
| Evidence | `resilience_evidence.json`: writer exit code 9; the objective **and its task** were restored by a fresh application instance. **4/4 checks passed** |
| Residual gap | An actual operating-system reboot was not performed. Rebooting the host is outside the authorized scope of a code audit, and no test can honestly call a process exit an OS restart |
| Final status | **PARTIAL — unclean process exit verified; OS reboot BLOCKED** |

### D6 — Environmental Phase 29 scenarios

| Field | Value |
| --- | --- |
| Class | VERIFICATION GAP (environment/hardware) |
| Items | A physical second monitor; a live display-resolution/scaling change; OS sleep/resume; a multi-hour (hour-scale) soak; a live development-environment workflow |
| Why blocked | The host has one display; changing resolution mid-audit risks the user's desktop; suspending the OS would interrupt the session; a multi-hour soak cannot be completed inside a bounded audit turn; a "development environment" workflow would require driving the user's editor and toolchain |
| Status | **BLOCKED** — each item is named with its reason, not marked PASS |

### D7 — Phase 31 residual gaps

| Field | Value |
| --- | --- |
| Class | VERIFICATION GAP / MISSING IMPLEMENTATION |
| Items | (a) Browser skill transfer was not live-run; (b) cross-OS transfer is not implemented (no macOS/Linux desktop adapter); (c) Phase 29 `ComputerOperator` is not a skill step kind; (d) no Skills UI (API only) |
| Status | **OPEN** — (a) is closable with a live browser run; (b) and (c) are scope, not defects; (d) is declared `no-render` by design |

### D8 — Phase 28 report claimed integration while Phase 31 recorded its absence

| Field | Value |
| --- | --- |
| Class | DOCUMENTATION DEFECT |
| Fix | `docs/PHASE28_REPORT.md` now states that in-pipeline skill selection did not exist at closure and records D1's remediation; `docs/PHASE31_REPORT.md` row 12 and limitation 3 were updated |
| Final status | **FIXED** |

### D9 — Provider adapter returned `Any`

| Field | Value |
| --- | --- |
| Class | TYPE DEFECT (introduced and caught during this audit) |
| Evidence | `mypy src/novacontrol/application.py` reported `Returning Any from function declared to return "Mapping[str, Any]"` at the adapter |
| Fix | The adapter now validates and copies the service result before returning it |
| Verification | App mypy now reports only the **pre-existing** `VisionProvider.see(..., structured=True)` error at `application.py:6259` |
| Final status | **FIXED — VERIFIED** |

---

## C. Remediation report

### C.1 Files changed by this audit

| File | Change |
| --- | --- |
| `src/novacontrol/agents/pipeline.py` | `SKILL_TOOL`, `SkillProvider` protocol, `skills=` seam, `_select_skill`, `_run_skill_step`, `_verify_skill_step`, skill tool declaration in `_declare`, availability in `_available_tools`, planner fallback, execution branch, verification branch |
| `src/novacontrol/application.py` | `_PipelineSkillProvider` adapter (select / explicit resolve / execute) and its wiring into `_build_specialist_pipeline` |
| `tests/test_phase31_skills.py` | 11 new regression tests (9 unit + 2 application-level) |
| `tmp/group1/verify_resilience.py` | New live resilience harness (dialog, forced crash, unclean exit) |
| `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` | This report |
| `docs/PHASE28_REPORT.md`, `docs/PHASE29_REPORT.md`, `docs/PHASE30_REPORT.md`, `docs/PHASE31_REPORT.md` | Audit addenda and corrected statuses |
| `docs/SKILLS.md`, `docs/MULTI_AGENT_COLLABORATION.md` | In-pipeline skill selection documented |
| `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md` | Group 1 entry |

No NovaForge, NovaMarket, `ROADMAP.md` or `docs/PHASES.md` file was touched.

### C.2 Architectural change (one, minimal, additive)

The specialist pipeline — the single loop every specialist runs — gained one
optional collaborator. It remains one loop:

```
NLU → Context → Decision → Planner → Tool Selection → Execution → Verification → Recovery
                                   │                                   │
                                   └── skill step (only when the agent  └── the library's own
                                       has no plan of its own)              verified verdict
```

* **Selection** happens in the planner stage, where a plan is decided.
* **Authorization** still happens in the tool-selection stage through the same
  `PermissionManager`; the skill tool is declared `LOW`, and the *skill's own*
  scopes are checked by `SkillService` before any step runs.
* **Execution** goes to `SkillService`, which routes each step through the
  application's existing tool/request/desktop/browser/agent paths.
* **Verification** uses the library's recorded verdict, not "the call returned".

Nothing in the agents package imports the skills package: the dependency points
from the composition root inward.

### C.3 Missing functionality implemented

In-pipeline skill selection (§B D1), including explicit selection by
`metadata={"skill": "<id or name>"}` and automatic selection when the agent has
no procedure. Unvalidated, incompatible or under-specified skills are refused
rather than substituted.

### C.4 Bugs fixed

D1 (missing implementation), D8 (contradictory documentation), D9 (type defect).

---

## D. Integration report

### D.1 Cross-phase results

| Boundary | Evidence | Verdict |
| --- | --- | --- |
| 27 → 28 | Collaboration `_capture_learning` records only verified work; 61 collaboration tests | PASS |
| 27 → 29 | Operator runs are captured from `AgentTaskState`; observe-only runs are not recorded as successes | PASS |
| 27 → 30 | `operations/learning.py` evidence + `status()["operations"]["learning"]` | PASS (advisory) |
| 27 → 31 | `SkillService.retrieve` calls `recommend`; prefer/avoid tools re-rank; validated-preference test | PASS |
| 28 → 29 | Desktop/browser controllers are the executed capability; `ComputerOperator` remains outside the skill step kinds | PARTIAL (by scope) |
| 28 → 30 | Objective tasks dispatch to specialists and to `skill:<id>` | PASS (live F) |
| 28 → 31 | **In-pipeline skill selection** (D1) | **PASS after remediation** |
| 29 → 30 | Approved desktop objective executes through the real controller and verifies the window | PASS (live G) |
| 30 → 31 | Objective + skill task restored after restart; checkpoint resume did not repeat a verified step | PASS (live F) |
| 29 → 31 | Desktop skill steps use the same plan/token path; real transfer between two applications | PASS |
| all → core | Same planner, tool registry, executor, event bus, store, permission layer | PASS (no duplicate subsystem) |

### D.2 Real application workflow evidence

| Harness | Result |
| --- | --- |
| `tmp/phase31/verify_phase31.py` (re-run on today's tree, fresh data dir) | **32 passed, 0 failed, 0 unverified** — including a real desktop transfer and a Phase 30 objective across a fresh application instance |
| `tmp/phase30/verify_phase30.py` (re-run) | **45 passed, 0 failed** — including the approved Calculator objective |
| `tmp/group1/verify_resilience.py` (new) | **14 passed, 0 failed** — unexpected Win32 dialog, forced crash between plan and act, unclean process exit |

The two phase harnesses were re-run rather than trusted: both recreated their
own data directories and reproduced their documented results on the current
tree, which is stronger evidence than the archived JSON alone.

### D.3 Compatibility with existing NovaControl

* No route was added, removed or changed: `python scripts/generate_api_reference.py --check` → `docs/API.md: in sync` (666 routes).
* `ToolRegistry` is unchanged; the skill tool is a pipeline capability, not a
  registered model-selectable tool.
* The pipeline change is inert without a provider (`SpecialistPipeline()` in the
  CLI demos behaves exactly as before — tested).

---

## E. Performance and reliability report

### E.1 Recorded measurements (from the phase harnesses, unchanged by this audit)

| Phase | Measurement | Value |
| --- | --- | --- |
| 27 | `capture` / `record` / `recommend` at 300 records | 0.026 ms / 2.724 ms mean / 2.074 ms mean |
| 27 | Memory / storage at 300 records | 132.8 KiB / 554.2 KiB |
| 28 | 20,000-row CSV collaboration, median wall (parallel) | 572.43 ms; measured parallelism 1.49; serial speedup 1.03× |
| 28 | Peak traced memory (one collaborative run) | 21,969 KiB |
| 29 | Capture / OCR / full observation median | 575.0 ms / 1,242.5 ms / 2,287.2 ms |
| 29 | 30-observation soak | 83.37 s total, median 2,268.7 ms, 0 failures |
| 29 | Soak RSS start → end / peak | 101.4 → 55.6 / 104.1 MB (no growth) |
| 29 | Calculator launch | 2,773.9 ms |
| 30 | 100 objectives / 400 tasks: create / restore / monitor scan | 14.59 ms / 8.10 ms / 0.65 ms |
| 30 | Snapshot size / process RSS | 354,406 bytes / 80.2 MB |
| 31 | Retrieval / version lookup / adaptation | 0.312 ms / 0.0015 ms / 14.983 ms |
| 31 | Stored state / process RSS | 60,170 bytes / 89.2 MB |

The new skill step adds one `SkillService.retrieve` plus one `execute` call per
run that has no plan of its own. No separate benchmark of that added latency was
run in this audit, and none is claimed.

### E.2 Stability and recovery evidence added by this audit

| Scenario | Result |
| --- | --- |
| Unexpected Win32 dialog during unrelated work | perceived: window list, `has_dialog: true`, `dialogs` contains the title, screenshot hash present |
| Target process killed between planning and acting | run `failed`; "No observed evidence supports goal completion."; history `failed`; no fabricated success; the next observation no longer lists the window |
| Unclean process exit (`os._exit(9)`) after an objective + task write | objective and its task restored by a fresh application instance |

A multi-hour soak, OS sleep/resume and a live resolution change remain
**unverified**; they are BLOCKED, not passed.

### E.3 Reliability properties re-confirmed

* A step that did not verify cannot complete a run (skill verification path).
* A skill awaiting approval is reported as a refusal, not a success.
* A run against a dead window fails and records the failure.
* A crash without a clean shutdown does not lose a durably written objective.

---

## F. Final acceptance matrix

Legend: **PASS** = implemented and supported by direct evidence; **PARTIAL** =
implemented or partly verified, with a named residual gap; **BLOCKED** = needs
hardware/environment/time this audit could not obtain; **FAIL** = not met.

### F.1 Phase 27 — Continual Learning & Self-Improvement (11 criteria)

| # | Criterion | Evidence | Status |
| --- | --- | --- | --- |
| 1 | Actual execution evidence captured | `engine.capture`, chat/device/agentic/game/recovery capture sites | PASS |
| 2 | Outcomes evaluated correctly | `OutcomeEvaluator` (11 tests) | PASS |
| 3 | Successes and failures produce signals | `SignalExtractor`; negative-verdict test | PASS |
| 4 | Repeated tasks retrieve relevant learning | `ExperienceRetriever` + `recommend` | PASS |
| 5 | Strategies improve through validated evidence | propose → validate → promote tests | PASS |
| 6 | Improvements persist across restarts | `JsonContinualLearningStore`; live restart | PASS |
| 7 | Changes auditable and reversible | versioned rows, rollback test, boundaries | PASS |
| 8 | Measured improvement via a reproducible benchmark | +1.0 verified completion, −4 retries, two identical runs | PASS |
| 9 | Existing capabilities remain functional | focused regression groups (group run in §E/§G) | PASS (focused policy; full suite not run) |
| 10 | Phase tests and regression checks pass | 48 phase tests at closure; suite passes today (207-test group) | PASS |
| 11 | Documentation and reports updated | `PHASE27_REPORT.md`, `CONTINUAL_LEARNING.md` | PASS |

### F.2 Phase 28 — Multi-Agent Collaboration (15 criteria)

| # | Criterion | Evidence | Status |
| --- | --- | --- | --- |
| 1 | Agents registered/discovered | registry, catalog, real specialists | PASS |
| 2 | Collaboration benefit determined | planner/decline tests | PASS |
| 3 | Decompose and delegate | research/coding/data plans, validated DAG | PASS |
| 4 | Independent concurrency | measured parallelism 1.49 | PASS |
| 5 | Dependent ordering | dependency context/order tests | PASS |
| 6 | Structured exchange | messages, declared context, provenance | PASS |
| 7 | Conflicts handled | unresolved + recomputation-resolved tests | PASS |
| 8 | Aggregate and verify | 4/4 real CSV verification; unverified output cannot complete | PASS |
| 9 | Failure preserves work | failure/timeout/retry/cancel tests | PASS |
| 10 | Restart/recovery | restore; running→interrupted; resume | PASS |
| 11 | Phase 27 learning | capture test | PASS |
| 12 | Permission and resource limits | approvals, token ledger, preflight, backoff, hardware cap | PASS |
| 13 | Real scenarios | live research A, real CSV C, disposable coding B, failure D, conflict E | PASS |
| 14 | Tests pass | 61 collaboration tests + relevant groups | PASS |
| 15 | Documentation and report | updated with D1/D8 addendum | PASS |

**In-pipeline skill selection (Phase 28 ↔ 31)** is now implemented and tested
(D1). It is additive: a specialist's own plan always wins.

### F.3 Phase 29 — Universal Computer & Device Operator (29 rows)

| # | Requirement | Status after this audit |
| --- | --- | --- |
| 1 | Perception: capture, windows, elements, OCR, coordinates, diffs, loading/error, context | PASS |
| 2 | Multiple resolutions and scaling configurations | **BLOCKED** (one display; no live resolution change induced) |
| 3 | Mouse: click, double/right click, drag, scroll, buttons | PASS |
| 4 | Keyboard: typing, shortcuts, selection, copy/paste, release | PASS |
| 5 | Focus, target verification, no wrong-app input, cancellation | PASS |
| 6 | Window management, dialogs, crash/unresponsiveness detection | PASS — unresponsive-window live; dialog live; forced crash live (D3/D4) |
| 7 | Browser navigation, forms, clicks, submission, scroll, inspection | PASS |
| 8 | Browser tabs/windows | PASS |
| 9 | Upload/download and session handling without bypassing auth | PASS |
| 10 | Closed observe→plan→act→observe→verify→adapt loop | PASS |
| 11 | File/document workflows | PASS |
| 12 | Development/system workflows | PARTIAL — Settings live-verified; a development-environment workflow was not run |
| 13 | Contextual references and ambiguity | PASS |
| 14 | Integration with agent/verification/recovery/learning architecture | PASS |
| 15 | Device abstraction and capability registry | PASS |
| 16 | Multiple monitors | **BLOCKED** (one physical display) |
| 17 | Permissions, risk classes, approvals, audit, emergency stop | PASS |
| 18 | Sensitive/destructive safeguards, credential handling | PASS |
| 19 | Recovery: dialogs, focus, delay, OCR/ambiguity, interruption | PASS |
| 20 | Recovery: unresponsive application / forced crash | **PARTIAL** — unresponsive and crash-after-planning verified; mid-action crash not induced |
| 21 | Recovery: resolution change, OS sleep, restart | **BLOCKED** — restart verified; resolution change and OS sleep not induced |
| 22 | Persistence, history, approvals, verification, learning | PASS |
| 23 | Phase-specific tests | PASS (22 tests; suites pass today) |
| 24 | Regression, static analysis, API sync | PASS — 22-file mypy clean; API in sync at 666 routes |
| 25 | Real application verification (ten scenarios) | **PASS** — the unexpected-dialog scenario is now live-verified (D3) |
| 26 | Performance and resource measurements | PASS |
| 27 | Long-running stability | **BLOCKED** — 30-observation soak only; no multi-hour soak |
| 28 | Documentation | PASS (with addendum) |
| 29 | Overall acceptance 1–15 | **PARTIAL** — rows 2, 12, 16, 20, 21 and 27 remain open |

### F.4 Phase 30 — Persistent Autonomy & Personal Operations (17 rows)

| # | Requirement | Status after this audit |
| --- | --- | --- |
| 1 | Manage persistent objectives | PASS |
| 2 | Decompose into tasks and milestones | PASS |
| 3 | Survive app and system restarts | **PARTIAL** — application restart and unclean process exit verified (D5); OS reboot not performed |
| 4 | Interrupted workflows resume safely | PASS |
| 5 | One-time and recurring scheduling | PASS |
| 6 | Deadline and stalled monitoring | PASS |
| 7 | Authorized operations execute autonomously | PASS |
| 8 | Sensitive operations keep approval | PASS |
| 9 | Progress reflects execution evidence | PASS |
| 10 | Notifications reflect real events | PASS |
| 11 | Phase 27 learning integration | PARTIAL — surfaced as evidence; does not adjust schedules |
| 12 | Phase 28 collaboration integration | PARTIAL — live agent pipelines not invoked from an objective |
| 13 | Phase 29 computer-operation integration | PASS |
| 14 | Recovery prevents duplicate side effects | PASS |
| 15 | Real workflows show persistent autonomy | **PARTIAL** — OS restart and multi-hour soak missing |
| 16 | Tests and regressions pass | PASS |
| 17 | Documentation and report | PASS (with addendum) |

### F.5 Phase 31 — Transfer Learning & General-Purpose Skills (18 rows)

| # | Requirement | Status after this audit |
| --- | --- | --- |
| 1 | Acquire reusable skills from genuine experience | PASS (live A: 2 real tool runs; one-run refusal) |
| 2 | Structured definitions and provenance | PASS |
| 3 | Retrieve relevant skills | PASS |
| 4 | Adapt skills to compatible new tasks | PASS (live B) |
| 5 | Demonstrate cross-task transfer | PASS (live A→B; desktop Control Panel→Character Map) |
| 6 | Cross-environment transfer where supported | **PARTIAL** — same-OS cross-application verified; cross-OS not implemented; browser transfer not live-run (D7) |
| 7 | Compose multiple validated skills | PASS (live D) |
| 8 | Independently validate adapted skills | PASS |
| 9 | Versioning and rollback | PASS |
| 10 | Reliability from actual evidence | PASS |
| 11 | Phase 27 continual-learning integration | PARTIAL — informs selection; does not auto-deprecate/auto-adapt |
| 12 | **Phase 28 multi-agent integration (in-pipeline selection)** | **PASS — implemented and tested in this audit (D1)** |
| 13 | Phase 29 computer-operation integration | PARTIAL — controllers verified; `ComputerOperator` is not a step kind |
| 14 | Phase 30 persistent-objective integration | PASS (live F) |
| 15 | Safety and permissions | PASS (client self-approval refused with HTTP 403) |
| 16 | Real application scenarios | PASS — 32 live checks + 14 resilience checks |
| 17 | Tests, regression, type checks | PASS — Phase 31 suite **42 passed** (was 31); agents+skills mypy clean (22 files) |
| 18 | Documentation and report | PASS (with addendum) |

Supporting rows that remain incomplete: cross-OS support (**PARTIAL**, not
implemented), a Skills UI (not built; routes declared `no-render`), and
large-catalog/concurrent-load performance (**not measured**, small-catalog
numbers only).

---

## G. Group 1 closure verdict

### G.1 Verification runs (exact commands and results)

Run from the repository root with `PYTHONPATH=src` and
`NOVACONTROL_DISABLE_OLLAMA=1`.

| # | Check | Command | Result |
| --- | --- | --- | --- |
| 1 | Baseline, five phase suites (before any change) | `pytest tests/test_continual_learning.py tests/test_multi_agent_collaboration.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py -q` | **207 passed** in 55.90 s |
| 2 | Phase 31 suite after the fix | `pytest tests/test_phase31_skills.py -q` | **42 passed** (was 31) |
| 3 | Shared agent paths after the pipeline change | `pytest tests/test_agents.py tests/test_multi_agent_collaboration.py tests/test_phase30_operations.py tests/test_phase29_computer.py tests/test_continual_learning.py -q` | **181 passed** |
| 4 | Group regression set | `pytest tests/test_phase31_skills.py tests/test_phase30_operations.py tests/test_phase29_computer.py tests/test_multi_agent_collaboration.py tests/test_continual_learning.py tests/test_agents.py tests/test_web_api.py tests/test_route_shape_contract.py tests/test_api_reference_sync.py -q` | **324 passed, 831 subtests passed**, 410.25 s, exit 0 |
| 5 | Phase 31 live scenarios (re-run on today's tree, fresh data dir) | `python tmp/phase31/verify_phase31.py` | **32 passed, 0 failed, 0 unverified** |
| 6 | Phase 30 live scenarios (re-run) | `python tmp/phase30/verify_phase30.py` | **45 passed, 0 failed** |
| 7 | New resilience harness | `python tmp/group1/verify_resilience.py` | **14 passed, 0 failed** |
| 8 | Types, agents + skills | `mypy src/novacontrol/agents src/novacontrol/skills --ignore-missing-imports` | **Success: no issues found in 22 source files** |
| 9 | Types, application | `mypy src/novacontrol/application.py --ignore-missing-imports` | 1 error — the **pre-existing** `VisionProvider.see(..., structured=True)` at `application.py:6259` |
| 10 | API reference | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (666 routes) |
| 11 | Lint, changed pipeline module | `ruff check src/novacontrol/agents/pipeline.py` | All checks passed |
| 12 | Lint, new test lines | `ruff check tests/test_phase31_skills.py` | No finding in the added lines (pre-existing E501 elsewhere in the file) |

Not run: the complete repository suite; that is not claimed as passing. The
repository-wide ruff run still reports pre-existing `E501`/`SIM105` findings in
files this audit did not touch.

### G.2 Verdict: **NOT CLOSED**

The strict gate cannot be satisfied: at least one requirement in Phases 29, 30
and 31 remains PARTIAL or BLOCKED, and a PARTIAL is not closable by editing a
document.

**Closed by this audit**

* Phase 28 ↔ 31 in-pipeline skill selection (D1) — implemented, tested, wired.
* Phase 29 unexpected-dialog requirement (D3) — live-verified with a real Win32
  dialog in production perception.
* Phase 29 forced-crash requirement (D4) — live-verified for a crash between
  planning and acting; the run fails honestly and records the failure.
* Phase 30 restart requirement (D5) — narrowed: an unclean `os._exit(9)` loses
  nothing that was durably written.

**Outstanding, with the requirement each blocks**

| Outstanding item | Blocks | Why it is not closed |
| --- | --- | --- |
| A physical second monitor | 29 #16; 29 #2 (multi-display capture/input) | The host has one display; geometry is unit-tested only |
| A live resolution/scaling change | 29 #2, #21 | Changing the user's display mode mid-audit is disruptive; not induced |
| OS sleep/resume followed by recovery observation | 29 #21 | Suspending the OS would interrupt the session |
| A multi-hour stability soak | 29 #27; 30 #15 | Only a 30-observation / 83 s soak exists; hour-scale runtime was not available |
| A live development-environment workflow | 29 #12 | Would require driving the user's editor/toolchain; the Settings workflow is the only live system workflow |
| A crash **mid-action** | 29 #20 | The verified crash is at plan→act; a target dying during input delivery was not induced |
| An actual OS reboot | 30 #3, #15 | Rebooting the host is out of scope for a code audit; a process exit is not a reboot |
| Cross-OS skill transfer | 31 #6 | No macOS/Linux desktop adapter is implemented |
| Browser skill transfer (live) | 31 #6 | Not run in this audit |
| `ComputerOperator` as a skill step kind | 31 #13 | Not implemented by design in Phase 31 |
| Phase 27 auto-deprecation/adaptation | 31 #11 | Advisory by design; lifecycle changes stay explicit |

Items that are scope, not defects, are labelled as such and must not be
silently upgraded: cross-OS adapters, a Skills UI (`no-render`) and the
`ComputerOperator` step kind were never part of the implemented contract.

### G.3 Exact next permissible action

1. **Do not start Group 2 (Phases 32–34), Group 3 (35–38), Group 4 (39) or
   Phase 40.** Group 1 has not passed its gate.
2. Obtain the environment the remaining rows need — a second display, an
   authorized resolution change, an authorized sleep/resume window, a
   multi-hour soak window, and an authorized OS reboot — or an explicit
   decision that those rows are deferred. Then run:
   * a two-monitor capture/input observation and a resolution-change recovery
     observation through `ComputerOperator.observe`;
   * a sleep/resume observation after wake;
   * an hour-scale soak with RSS sampling;
   * a live editor-based development workflow;
   * a mid-action crash (kill the target while input is being delivered).
3. Run the live browser skill transfer (`acquire → validate → adapt →
   execute` against a local page) to close 31 #6's browser half.
4. Re-run checks 2–4 and 8–11 in §G.1 after any further code change, then
   re-issue this matrix. Only when no in-scope row is PARTIAL/FAIL/BLOCKED may
   the group be declared PASS and Group 2 started.

Nothing was committed, pushed or deployed. All pre-existing uncommitted work is
preserved; no NovaForge or NovaMarket file was modified; no roadmap scope was
changed.

---

## H. Second Group 1 pass (2026-11-26) — live browser skill transfer, environment determination, deferral register

This section continues §A–§G and does not retract them. Two first-pass statements
are corrected here and marked: the D1 fix description claimed selection worked
"when an agent's own plan is empty (or a task explicitly names a skill)" — the
parenthetical was **not true** until D10 below was fixed.

### H.1 Repository state before and after

| Item | Before this pass | After this pass |
| --- | --- | --- |
| Branch / HEAD | `main` / `afa5ff6` | `main` / `afa5ff6` — unchanged, nothing committed |
| Working tree | 175 changed/untracked entries | 175 entries (the new evidence lives under git-ignored `tmp/`) |
| D1 remediation still present | yes | yes (`SKILL_TOOL`, `SkillProvider`, `_select_skill`, `_run_skill_step`, `_PipelineSkillProvider`) |
| Source changed by this pass | — | `src/novacontrol/agents/pipeline.py` (D10, D11), `tests/test_phase31_skills.py` (+3 tests) |
| New evidence | — | `tmp/group1/verify_browser_transfer.py`, `tmp/group1/browser_transfer_evidence.json` |

Existing evidence was not overwritten: `tmp/phase31/evidence.json`,
`tmp/phase30/evidence.json` and `tmp/group1/resilience_evidence.json` are
untouched, and `tmp/group1/browser_data/` was created fresh for this run.

### H.2 Live browser skill transfer — Phase 31 #6, browser half: **PASS**

| Item | Value (also in the evidence JSON) |
| --- | --- |
| Host | Windows (`win32`), Python 3.13.0, one display 1440×900 |
| Browser runtime | the application's own `PlaywrightBrowserRunner` launching Chromium **156.0.8078.4** headless |
| Target | a real `ThreadingHTTPServer` on `127.0.0.1:<ephemeral>` serving a page titled `NovaGroup1 Browser Target` |
| Independent evidence | the **server** logged every request with its User-Agent (`… HeadlessChrome/156.0.8078.4 …`), so real browser interaction is proven by the server, not by Playwright's own report |
| Command | `PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_browser_transfer.py` |
| Result | **32 passed, 0 failed, 32 checks**, exit 0 |
| Evidence | `tmp/group1/browser_transfer_evidence.json` |

The nine required facts, and the check that establishes each:

| # | Required | Evidence | Verdict |
| --- | --- | --- | --- |
| 1 | skill created through the supported workflow | H2.1 — acquisition from two genuine navigations, 2 verified observations, goal similarity 1.00 | PASS |
| 2 | validated and stored in the existing library | H2.2 `acquired` → H2.5 `validated` on its own evidence (2/0) | PASS |
| 3 | a different agent discovers and selects it | H3.1/H3.2 — the real `verification-agent` plans `skill.execute` targeting the library id, replacing its own `verify_claims` plan | PASS |
| 4 | `PermissionManager` authorizes | H3.3 parked (DENIED, *"step 1 requires approval before it can run"*), H4.1 approved resume, N5.2 unapproved refusal | PASS |
| 5 | the single `SkillService` executes it | H4.1, H5.3 — one execution row, driven by `SkillService` | PASS |
| 6 | browser interaction actually occurs | H1.5, H4.2, H6.2 — server-side hits, each after the corresponding execution | PASS |
| 7 | the outcome is independently verified | H1.4 page title/url read from the browser; the server log; H5.3 per-step evidence | PASS |
| 8 | the verdict is recorded in the library | H5.1 evidence rows and reliability (trials 3, successes 3), H5.2 lifecycle still `validated` | PASS |
| 9 | objective and execution records reflect it | H5.3 execution `completed`/`verified`; H3.3 refusal recorded as DENIED; N6.1 failure recorded and N6.2 lowers the rate | PASS |

Two honest limits on this test:

* the agent used is a real shipped specialist, but **every shipped specialist
  always returns a plan of its own**, so the automatic (no-plan) selection path
  is exercised by unit tests only; the reachable live in-pipeline path is an
  explicit `metadata={"skill": …}` request — which only worked after D10;
* the pipeline passes no `approved` flag, so a browser skill selected in-pipeline
  always parks for approval and is released by resuming the same execution (H4).
  That is the correct safety behaviour for browser control, not a bypass.

### H.3 Negative paths (all live, all real)

| # | Path | Expected | Observed | Verdict |
| --- | --- | --- | --- | --- |
| N1 | unvalidated skill named by a task | not selected | plan stayed `verification.verify_claims`; no skill execution; 0 browser hits | PASS |
| N2 | incompatible environment | refused with a reason | `compatible: False`, *"environment 'darwin' is unsupported by this skill (declared: windows)"*, no new version | PASS |
| N3 | missing required input | blocked before any work | `blocked`, *"missing required inputs: url"*, 0 hits | PASS |
| N4 | undeclared permission scope | see §H.4 L1 | `completed` — the scope name is undeclared, so the default LOW declaration applies | OBSERVED (limitation, not a pass) |
| N5 | approval-pending scope | parks, then runs after approval | `waiting_approval` (`automation.run` HIGH) with 0 hits → after approval `completed`/`verified` with 1 hit | PASS (2 checks) |
| N6 | real browser failure | fails, not verified, rate falls | `failed`, `verified: False`, `ERR_UNSAFE_PORT`, result row `Executed 0/1`, `failures: 1`, success rate 0.8 | PASS (2 checks) |
| N7 | invalid/replayed approval token | refused, nothing runs | `ValueError: Approval token is unknown…`, 0 hits | PASS |
| N8 | interrupted mid-navigation | fails, not verified | browser closed during `/slow`; `failed`, `verified: False`, `ERR_ABORTED` | PASS |

### H.4 Defects found and fixed in this pass

#### D10 — An explicitly requested skill was silently ignored (Phase 28 ↔ 31)

| Field | Value |
| --- | --- |
| Class | IMPLEMENTATION DEFECT — unreachable integration |
| Reproduction | `_stage_planner` consulted the library only when the agent's plan was empty; every shipped specialist (`verification`, `research`, `data-analysis`, `developer`) always returns a plan, so `metadata={"skill": …}` changed nothing. The first live run showed the plan `verification.verify_claims` and zero skill steps |
| Severity | High — the documented explicit-selection behaviour did not exist for any shipped agent, which is precisely why the integration had never been exercised |
| Fix | `_stage_planner` now asks the library **first** when the task names a skill, and uses the agent's own plan only if the named skill is unusable. A plan an agent was not asked to replace is still never replaced |
| Tests | `test_an_explicitly_requested_skill_wins_over_the_agents_own_plan`, `test_an_explicit_request_for_an_unusable_skill_keeps_the_own_plan` |
| Verification | live H3.1/H3.2; Phase 31 suite green |
| Status | **FIXED — VERIFIED** |

#### D11 — A refused skill step was retried, creating duplicate approval checkpoints

| Field | Value |
| --- | --- |
| Class | RELIABILITY DEFECT |
| Reproduction | the first live run produced **two** `waiting_approval` execution rows 23 ms apart for one pipeline step |
| Root cause | `_verify_and_recover` verified a `DENIED` outcome as a verification FAIL and recovered with `RETRY`; each retry calls `SkillService.execute`, which starts a *new* execution and a new approval checkpoint |
| Severity | Medium — one refusal became several pending approvals for one step; approving one leaves stale checkpoints, and a retry of a refusal cannot change the verdict |
| Fix | a `DENIED` outcome records `RECOVERY: SKIPPED — "refused, not retried"` and is never re-queued |
| Test | `test_a_refused_skill_step_is_not_retried` |
| Verification | live H3.4 — exactly one checkpoint |
| Status | **FIXED — VERIFIED** |

**L1 (limitation, recorded not fixed).** The permission layer is
*declaration-driven*: a scope name nobody declared resolves to the default LOW
declaration and does not deny. A skill can therefore only **raise** what its
steps require, never grant itself authority — but a misspelled custom scope
passes silently. Changing that default is a global permission-policy change, out
of scope for this pass; the live denial evidence is the declared-but-unapproved
scope in N5.2 and the pipeline-level DENIED outcome in H3.3.

### H.5 Environment-dependent criteria — determination

| Criterion (phase #) | Environment available? | Safe to run now? | Evidence that would close it | Completable in this environment? |
| --- | --- | --- | --- | --- |
| Second physical monitor — 29 #16, #2 | **No** — `GetSystemMetrics(80) = 1`, virtual desktop 1440×900 | n/a | a two-display observation through `ComputerOperator.observe` (both geometries) plus one input aimed at a window on the secondary display | No — needs a second display attached |
| Live resolution/scaling change — 29 #2, #21 | Only one mode available; changing it mutates the user's desktop | **No** — disruptive, unauthorized | a real mode change, then a recovery observation and a successful re-target | Not without operator authorization |
| OS sleep/resume — 29 #21 | Yes, but it suspends **this session** | **No** — would interrupt the audit and the user's machine | suspend → wake → `observe` → recovery verdict | Not without an authorized window |
| Multi-hour soak — 29 #27, 30 #15 | Only a 30-observation / 83 s soak exists | Yes, but hours long | hour-scale run with RSS sampling and zero failures | Not inside a bounded turn; needs an unattended window |
| Live development-environment workflow — 29 #12 | Would need the user's editor/toolchain | **No** — unauthorized use of the user's tools | a disposable editor project driven end-to-end with a verified result | Not without authorization |
| Mid-action crash — 29 #20 | Yes: a disposable window plus a process kill | Yes | kill the target **while input is being delivered**; run must fail and record it | Yes — not attempted in this pass (the plan→act crash is already verified) |
| Actual OS reboot — 30 #3, #15 | Would restart the user's machine | **No** — destroys the session | reboot → fresh instance → objective, task and checkpoint restored, no duplicate step | Not without explicit authorization |
| Cross-OS skill transfer — 31 #6 | **No** — no macOS/Linux desktop adapter exists | n/a | a real second-OS adapter and a transfer run on it | No — scope, not environment; the **refusal** is now live-verified (N2) |
| `ComputerOperator` as a skill step kind — 31 #13 | n/a | n/a | implementation | No — out of the implemented contract |
| Auto-deprecate/adapt from Phase 27 — 31 #11 | n/a | n/a | implementation | No — advisory by design |

### H.6 Deferral register

Every row below remains **outstanding**. None is converted to PASS.

| ID | Phase · criterion | Original requirement | Why it cannot be tested now | Missing | Risk left unverified | Evidence needed to close | Operator approval? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DF-1 | 29 #16 (and #2) | operate across multiple monitors | the host has one display | a second physical display | multi-monitor geometry, capture and input targeting | two-display observation + secondary-display input | No (hardware only) |
| DF-2 | 29 #2, #21 | survive a live resolution/scaling change | changing the mode mutates the user's desktop | authorized window | recovery after a mode change | mode change → observe → recovery verdict | **Yes** |
| DF-3 | 29 #21 | recover from OS sleep/resume | suspending the OS interrupts this session | authorized window | post-wake perception and recovery | suspend → wake → observe | **Yes** |
| DF-4 | 29 #27, 30 #15 | multi-hour stability | hours of runtime are not available in a bounded turn | unattended window | slow drift, leaks, long-run scheduling | hour-scale soak with RSS sampling | **Yes** (unattended window) |
| DF-5 | 29 #12 | development/system workflow | needs the user's editor and toolchain | authorization + a disposable project | editor-driven workflow reliability | end-to-end disposable dev workflow | **Yes** |
| DF-6 | 29 #20 | mid-action crash recovery | not attempted in this pass; the plan→act crash is verified | one harness run | target dying during input delivery | kill during delivery → run fails honestly | No |
| DF-7 | 30 #3, #15 | survive an OS reboot | rebooting destroys the user's session | authorized reboot | reboot-level durability | reboot → fresh instance → state restored | **Yes** |
| DF-8 | 31 #13 | `ComputerOperator` as a skill step | never implemented; controllers are the step kind | implementation | operator-level steps in skills | implementation + tests | n/a (scope) |

### H.7 Regression, type and lint results

| Check | Command | Result |
| --- | --- | --- |
| Five phase suites | `pytest tests/test_continual_learning.py tests/test_multi_agent_collaboration.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py -q` | **221 passed** in 51.60 s, exit 0 (218 before this pass's 3 new tests) |
| Affected agents / collaboration / Phase 31 | `pytest tests/test_phase31_skills.py tests/test_agents.py tests/test_multi_agent_collaboration.py -q` | **111 passed** in 24.12 s, exit 0 |
| Types, agents + skills | `mypy src/novacontrol/agents src/novacontrol/skills --ignore-missing-imports` | Success: no issues found in 22 source files |
| Lint, changed module | `ruff check src/novacontrol/agents/pipeline.py` | All checks passed |
| Whitespace | `git diff --check` on the changed files | clean, exit 0 |
| API reference | `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (666 routes) |
| Live browser transfer | `python tmp/group1/verify_browser_transfer.py` | **32 passed, 0 failed**, exit 0 |

Not run in this pass: the complete repository suite, and the phases' own live
harnesses (they were re-run in the first pass on the same tree; nothing they
cover changed except the two pipeline branches, which their suites exercise).

### H.8 Matrix changes

| Row | Before | After | Why |
| --- | --- | --- | --- |
| 31 #6 cross-environment transfer | PARTIAL | **PASS** | same-OS cross-application was already live-verified; the browser half is now live-verified (H2), and the unsupported cross-OS case is now verified to be *refused* (N2), which is what "where supported" requires |
| 31 #17 tests/regression/types | PASS | PASS | 3 new tests, mypy/ruff clean |
| 32 — 28 rows | unchanged | unchanged | — |
| 29 #2/#12/#16/#20/#21/#27, 30 #3/#15, 31 #11/#13 | PARTIAL/BLOCKED | unchanged | see DF-1…DF-8 |

### H.9 Revised verdict: **NOT CLOSED**

Closed by this pass: Phase 31 #6 (browser transfer; cross-OS refusal verified),
plus defects D10 and D11.

Still outstanding, unchanged in kind: DF-1 … DF-8. Because rows in Phases 29 and
30 remain PARTIAL/BLOCKED, the strict closure rule cannot be met and Group 2 must
not start.

The next permissible actions are therefore, in order: (a) an authorized window
for DF-2/DF-3/DF-4/DF-5/DF-7, (b) DF-1 by attaching a second display, (c) DF-6 on
request (it is safe and needs no new environment), (d) DF-8 only if the scope is
reopened. Nothing was committed, pushed or deployed; no NovaForge, NovaMarket,
roadmap or new-phase file was touched.

---

## I. Third Group 1 pass (2026-11-26) — DF-6 execution, D12, and the DF-8 decision

Continues §A–§H. No source file was changed in this pass; the only additions are
two acceptance harnesses and their timestamped evidence.

### I.1 Repository state

| Item | Value |
| --- | --- |
| Branch / HEAD | `main` / `afa5ff6` — unchanged, nothing committed, pushed or deployed |
| Working tree | 175 entries before and after |
| Previous evidence | `tmp/group1/browser_transfer_evidence.json` intact (32/32, 7 recorded requests) |
| Source changed by this pass | **none** — the only writes were under `tmp/group1/` (two harnesses + evidence) and these docs. The 69-file `src`/`tests` diff against HEAD is the pre-existing uncommitted work, untouched |
| New evidence | `midaction_crash_evidence_20261126T232321.json` (operator attempt), `midaction_controller_evidence_20261126T232731.json` (8/8) |

### I.2 DF-6 — the operator-loop attempt, and the defect it exposed (D12)

The first attempt drove the real path: `plan_computer_task` → single-use token →
`execute_computer_task`, on a fresh data directory, with a disposable Tk window
titled `NovaGroup1MidAction` open and a probe that dies on the first key it
receives during a paste.

It did **not** produce a mid-action crash, because the operator never targeted the
probe. Evidence (`midaction_crash_evidence_20261126T232321.json`):

| Fact | Observed |
| --- | --- |
| Objective | `type the phrase hello world into the NovaGroup1MidAction window` |
| Observed window the operator grounded on | **`Realtek Audio Console`** — an unrelated live application |
| Executed action | `type`, target `Realtek Audio Console`, `success: true`, `verified: true` (`clipboard_staged`, `focused_window: Realtek Audio Console`) |
| Probe marker | **absent** — the intended target received nothing |
| Final verdict | `fail` — *"No observed evidence supports goal completion."* |
| Recorded recovery | `relocate` onto `Realtek Audio Console`, `succeeded: false` |

**Disclosure:** that paste was delivered while the user's *Realtek Audio Console*
was the foreground window, so the text `the phrase hello world` was very likely
typed into that application. No file, account or financial state was involved,
but it is a real, unintended interaction with a user application.

DF-6 was therefore **not** exercised by that run, and repeating it with a
window-named objective would risk more stray input, so the operator path was
stopped there under the safety boundary. The attempt is kept as evidence for the
defect it found, not as a pass.

### I.3 DF-6 — mid-action crash verified on the token-gated input path

`tmp/group1/verify_midaction_controller.py` exercises the **same**
input-delivery code the operator uses, through the application's supported
desktop action path, with the target confirmed in the foreground *before*
anything is typed (so a mistargeted paste is impossible: the paste follows a
confirmed `GetForegroundWindow` title check, and the probe holds focus).

Command: `PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_midaction_controller.py`
Result: **8 passed, 0 failed**, exit 0 — `midaction_controller_evidence_20261126T232731.json`.

| # | Check | Observed |
| --- | --- | --- |
| C1.1 | target window open | `NovaGroup1MidAction` |
| C1.2 | target confirmed foreground before input | `foreground: NovaGroup1MidAction` |
| C2.1 | input actually reached the target before it died | marker: `event_type=2` (KeyPress), `keysym=Control_L` — the paste's modifier arrived |
| C2.2 | target died mid-delivery | probe exit code **3**, no unwinding |
| C2.3 | action not reported completed | `status: failed` — `VerificationError: Typed text was not verified: no window is focused — the paste had nowhere to land.` |
| C2.4 | no verified success claimed | `output: {}`, `verified` absent |
| C2.5 | audit trail recorded the failed attempt | `AuditEntry{status: failed, error: VerificationError…}` |
| C3.1 | dead window gone from the next probe | not listed |

What this proves and what it does not:

* **Proves:** a target dying *while input is being delivered* fails the action
  with a real verification error, is never recorded as success, and is recorded
  once on the audit trail. That is the semantic requirement behind 29 #20.
* **Does not prove:** the *operator loop's* own recovery of a mid-action crash.
  That variant remains unverified and is now **blocked by D12**, because driving
  the loop with a window-named objective can send input to the wrong application.

### I.4 D12 — the operator grounds a window-named objective on a different app

| Field | Value |
| --- | --- |
| Class | SAFETY / GROUNDING DEFECT (Phase 29 operator) |
| Requirement | 29 #5 "no wrong-app input"; #20 recovery |
| Reproduction | as §I.2, on a fresh data directory with the named window open |
| Root cause | perception sets the observed window to the **first titled window the OS probe returns** (`agentcore/perception.py`: `window_title = window_title or title`), not to a window named by the objective nor to the foreground window. The planner then grounds `type` on that window, and `TYPE_TEXT`'s `expect` is derived from the *same* wrong window, so the per-action verifier confirms the wrong target and reports `verified: true` |
| Impact | input can be delivered to an unrelated application while the objective names another; the per-action verifier cannot catch it because it checks the same mis-grounded title. Only the final goal verification fails, after the input has already been sent |
| Remediation | **not attempted** — see §I.5. It needs a policy decision and touches the shared perception/planner stages every Phase 29/30 workflow uses |
| Evidence | `tmp/group1/midaction_crash_evidence_20261126T232321.json` |

### I.5 The decision D12 requires

The fix is a policy choice, not a mechanical patch. The options, all small in
code but different in behaviour:

1. **Goal-named window binding (recommended).** If the objective names a title
   that exists in the observed window list, bind the observation (and the
   `expect` of every input action) to that window; if it names one that is *not*
   observed, refuse before any input. Cost: a title extractor plus a binding step
   in the perception/planner seam; risk: title matching must not bind loosely.
2. **Foreground-only.** When an objective is about a specific window, require
   that window to be foreground before planning, else refuse. Smaller, but
   changes behaviour for multi-window workflows.
3. **Refuse explicit window names** that do not match the observed window, with
   a clarification request, and otherwise keep today's behaviour. Smallest and
   safest, but pushes work back to the user.

I did not choose one: it changes how every Phase 29 objective is grounded, and
the brief for Phase 29 does not state which rule was intended.

### I.6 DF-8 — `ComputerOperator` as a skill step kind: analysis

| Question | Finding |
| --- | --- |
| What was originally expected? | The original Phase 31 brief is **not in the repository** (documented defect D2: `ROADMAP.md` stops at Phase 20, `docs/PHASES.md` at Phase 15). The only record is the phase report's own row 13: "desktop/browser skill steps use the real controllers and token path — no `ComputerOperator` step — PARTIAL" |
| Is the capability missing, or the integration, or the verification? | **Verification and definition, not capability.** `SkillStepKind` has `desktop` and `browser`, and `_execute_skill_step` routes both through the same token-gated `plan_*_command`/`execute_*_command` path a typed device command uses, including per-action verification. A skill *can* drive real device input today |
| Does it exist under another supported name? | Yes: skill steps `desktop`/`browser`, the `request` path, and Phase 30's `agent="skill:<id>"` dispatch. Phase 29's operator is reachable separately through the objectives/computer-task path |
| Would a `computer` step kind duplicate a subsystem? | Not a second engine (it would call the existing `ComputerOperator`), but it would create a **second routing for the same desktop input** and a **nested loop**: the operator's own perceive→plan→act→verify inside the skill executor, on top of the skill's own verification, with its own token minting per step |
| Is the requirement unambiguous? | **No.** Nothing states whether a skill must be able to invoke the whole operator loop (classification, durable operation history, Phase 27 learning capture) or merely perform computer operations — the latter already works |

Proposed smallest additive change, **if** the decision is to implement:

* `models.py`: add `COMPUTER = "computer"` to `SkillStepKind`.
* `application.py::_execute_skill_step`: a `COMPUTER` branch that requires
  `context["approved"]`, calls `plan_computer_task(rendered)` for its token, then
  `execute_computer_task(...)`, and reads the operator's own verdict
  (`state.verification.final` + every executed action `completed`) into
  `StepOutcome` — reusing `_skill_device_outcome` semantics.
* Safety: the operator's own preflight stays the gate (sensitive/destructive
  objectives still require the token); the skill's declared scopes are checked
  before the step, as today; the step must never bypass `ApprovedApprovalGateway`.
* Tests: a `COMPUTER` step that completes and verifies; one whose operator run
  fails (no success); one parked for approval (DENIED); one where the objective
  is sensitive and the skill has no approval (blocked); plus the plan-shape test
  that an existing `desktop`/`browser` step is unchanged.
* Real-execution proof would be required before claiming acceptance (the brief
  forbids a unit test alone).

**Not implemented in this pass.** It needs (a) the product decision above, and
(b) D12 resolved first: a `computer` step kind would inherit the same wrong-window
grounding, so shipping it now would put mis-targeted input on a second path.

### I.7 Verification run in this pass

| Check | Command | Result |
| --- | --- | --- |
| Five phase suites (tree intact, no source change) | `pytest tests/test_continual_learning.py tests/test_multi_agent_collaboration.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py -q` | **221 passed**, exit 0 |
| Types (agents, skills, computer) | `mypy src/novacontrol/agents src/novacontrol/skills src/novacontrol/computer --ignore-missing-imports` | Success: no issues found in 28 source files |
| Mid-action crash, token-gated input path | `python tmp/group1/verify_midaction_controller.py` | **8 passed, 0 failed**, exit 0 |
| Operator mid-action attempt | `python tmp/group1/verify_midaction_crash.py` | 11 passed, 5 failed — the failures are the D12 evidence, not a pass |

### I.8 Matrix changes

| Row | Before | After | Why |
| --- | --- | --- | --- |
| 29 #20 recovery: unresponsive / forced crash | PARTIAL (plan→act only) | PARTIAL at the time — mid-action crash verified on the token-gated input path (8/8); operator-loop variant blocked by D12. **Superseded: PASS in §J.5 (16/16)** | §I.3, §I.4, §J.5 |
| 29 #5 no wrong-app input | PASS | REGRESSION FOUND (D12) at the time. **Superseded: PASS in §J.6** | input was delivered to an unrelated app on a window-named objective; objective-bound targeting now prevents it |
| 31 #13 Phase 29 operator as a skill step | PARTIAL | PARTIAL — decision pending (§I.6); not implemented | §I.6 |
| everything else | — | unchanged | — |

### I.9 Verdict for this pass: **NOT CLOSED**

DF-6 is partially closed (input-path mid-action crash verified; operator-loop
variant blocked by a newly found safety defect). DF-8 is analysed, not
implemented, and awaits a product decision. DF-1…DF-5 and DF-7 are untouched.
No criterion was converted to PASS without direct evidence, and no deferral was
treated as a pass.---

## J. Fourth Group 1 pass (2026-11-26) — D12 remediation and closure

### J.1 Root cause, stated precisely

`agentcore/perception.py` chose the observed window as **the first titled window the
OS probe returned** (`window_title = window_title or title`). The objective's own
words were never consulted, and the foreground window was recorded but never used
for selection. `orchestrator._dispatch` then typed into `before.window`, and
`actions.type_into_active` handed that same title to `TYPE_TEXT` as its `expect`,
so the per-action verifier compared the paste against the window the planner had
already mis-grounded on. The verifier was not lying — it was asked the wrong
question. That is why the run recorded `success: true, verified: true` while the
text sat in `Realtek Audio Console`.

Two further weaknesses were found while validating the fix:

* The **final** goal verification replaced `state.verification` wholesale, erasing
  every per-step verdict, and could pass on a goal word that happened to appear in
  a window title ("…NovaD12Target"). A failed input step could therefore still end
  as `completed`.
* `TYPE_TEXT` verified the clipboard and *a* focused window, not **which** window:
  a same-titled window owned by another process was indistinguishable.

### J.2 The safety policy implemented

> **The target must be explicitly grounded in the objective and independently
> identified in the current OS window observations.**

| # | Requirement | Implementation |
| --- | --- | --- |
| 1 | Extract the intended target from the objective | `agentcore/targeting.py::requested_target` — quoted title, then `<name> window/app/application/program`, then `into/in/on/to <name>`; generic roles ("the window", "the browser") are refused as targets |
| 2 | Match it against observed windows | `bind_window` over `metadata["window_details"]` (title, handle, pid, process, foreground) |
| 3 | Require an unambiguous match | exact title match first; a unique containment match second; two or more matches → `ambiguous`, never a pick |
| 4 | Preserve stable identity through planning/execution/verification | `WindowRef.identity = (handle, pid)`; stored on `state.interpretation["target_identity"]` for the whole task and in every `Expectation.target_identity` |
| 5 | Revalidate immediately before input | the per-step `before` observation *is* the revalidation; `_target_refusal` re-checks status, identity continuity and handle/pid non-zero, then `TYPE_TEXT` re-checks the foreground handle/pid at paste time |
| 6 | Require the target foreground and eligible | `window_focus` before typing; `_verify_type_windows` now returns `FGHWND`/`FGPID` and `_type_text` requires both to equal the bound identity |
| 7 | Verify against that same target, not whatever is foreground | `Expectation.target_identity` makes the Verifier fail when the fresh observation's bound target differs |
| 8 | Detect disappearance, identity change, focus loss, ambiguity | `revalidate()` → `absent` / `identity_changed` (same title, different handle/pid) / `retitled`; focus loss → `identity_match: false`; ambiguity → refusal before dispatch |
| 9 | Refuse when the target cannot be established | `_target_refusal` fails the task with an explicit reason and records a `refused` ActionRecord; **no input is sent** |
| 10 | Never fall back to the first titled window | with a target named, `perception` reports that window or `window=""` plus `target_refused`; with no target named, `metadata["target"] = none_requested` and every input action refuses |

`type_into_active` no longer has an "active window" destination: without a title
**and** a handle/pid it refuses. Desktop clicks in the operator path pass
`require_bound_target=True`; an unbound observation cannot be clicked by
coordinates. Permissions, approval tokens and the audit trail are untouched.

### J.3 Files changed

| File | Change |
| --- | --- |
| `src/novacontrol/agentcore/targeting.py` | **new** — target extraction, `WindowRef`/`WindowBinding`, `bind_window`, `revalidate`, `binding_from_dict` |
| `src/novacontrol/agentcore/perception.py` | `observe(..., target_window=)`: bind the objective's window from `window_details`; never substitute the first titled window for a named target |
| `src/novacontrol/agentcore/orchestrator.py` | extract the target once; pass it to every observation; refuse input steps that are unbound/ambiguous/identity-changed; carry the identity into `TYPE_TEXT`; stop the final verdict from overriding an unverified input step |
| `src/novacontrol/agentcore/actions.py` | `type_into_active` requires title+handle+pid; `click_element(require_bound_target=)` refuses unbound desktop coordinates |
| `src/novacontrol/agentcore/verifier.py` | `Expectation.target_identity` / `target_title`: a PASS requires the same window to be observed after the action |
| `src/novacontrol/desktop/controller.py` | `FGHWND`/`FGPID` in the paste probe; `_type_text(expect_handle, expect_pid)` fails on any identity mismatch and reports `identity_match`/`expected_identity`/`observed_identity` |
| `tests/test_d12_targeting.py` | **new** — 28 focused tests, fixtures only, no real input |
| `tmp/group1/probe_d12_target.py`, `tmp/group1/verify_d12_live.py` | **new** — the isolated live gate |

### J.4 Negative-path coverage (all in `tests/test_d12_targeting.py`, no desktop input)

| Required case | Test | Result |
| --- | --- | --- |
| Intended window exists and is uniquely identified | `test_unique_target_binds_with_identity`, `test_realtek_scenario_types_only_into_the_named_window` | PASS |
| Intended window is absent | `test_absent_target_sends_no_input`, `test_first_titled_window_is_not_a_fallback` | PASS |
| Multiple windows match | `test_similarly_named_windows_are_ambiguous`, `test_duplicate_exact_titles_are_ambiguous`, `test_ambiguous_target_sends_no_input` | PASS |
| Only unrelated titled windows exist | `test_only_unrelated_windows_sends_no_input` | PASS |
| Foreground differs from the intended target | `test_paste_into_a_different_foreground_window_is_not_success` | PASS |
| Target loses foreground before input | same controller test via handle/pid mismatch | PASS |
| Target closes during execution | `test_target_closing_before_input_sends_no_input`, `test_revalidate_detects_disappearance` | PASS |
| Target identity changes during execution | `test_identity_change_between_steps_sends_no_second_input`, `test_revalidate_detects_identity_change_under_same_title` | PASS |
| Similarly named unrelated applications | `test_similarly_named_windows_are_ambiguous` | PASS |
| Verification observes an unexpected destination | `test_unexpected_destination_fails_even_when_the_action_succeeded` | PASS |
| No target is available | `test_no_named_target_refuses_input`, `test_typing_without_a_bound_identity_is_refused`, `test_typing_without_handle_identity_is_refused` | PASS |
| Permission and approval requirements stay enforced | `test_destructive_actions_still_require_approval`, `test_desktop_click_requires_a_bound_target`; Phase 29 approval tests unchanged | PASS |
| **Realtek regression (no real input)** | `test_realtek_scenario_types_only_into_the_named_window`: fixtures put `Realtek Audio Console` first and foreground; the objective names `NovaGroup1MidAction`; the only type call carries the probe window's title/handle/pid and no call mentions Realtek | PASS |

### J.5 DF-6 reassessment — operator loop, 16/16

The gate was satisfied (focused tests green, live targeting proven), so the
original scenario was re-run through the same production path
(`verify_midaction_crash.py`, fresh data directory). The disposable
`NovaGroup1MidAction` window received the paste's first key
(`event_type=2`, `keysym=Control_L`) and exited with code 3 mid-delivery.

* the run status and the durable history record are **failed**, not completed;
* no executed action claims success;
* the final verdict is `fail` — "1 input step(s) were never verified in the
  intended target";
* the action's failure names the identity mismatch explicitly: expected
  handle/pid `[2164478, 39052]`, observed `[15729620, 4192]` — the target's pid is
  the probe's, and the foreground after its death is some other window;
* recovery is recorded with a strategy and a reason, the next observation no
  longer lists the dead window, the approval token cannot be replayed, and a fresh
  application instance restores the failed record with its non-successful action.

**16/16 checks**, evidence `tmp/group1/midaction_crash_evidence_20261126T235403.json`.
The controller-only 8/8 test is no longer the DF-6 substitute: the operator loop
itself is now verified.

> **Corrected 2026-11-27 (§N.4).** This PASS is retracted. The cited evidence records
> the paste's tail being observed in an unrelated window (`[15729620, 4192]`) and a
> recovery strategy of `retry`; under the live-acceptance criteria the operator-loop
> row is **PARTIAL**, and the current-tree run chooses `recheck` instead.

### J.6 Live D12 gate — 18/18

Two runs, evidence `tmp/group1/d12_live_evidence_20261126T235325.json`:

1. **Absent target (no input possible).** Objective names
   `NovaD12AbsentTarget`, which does not exist. Refused with "the objective names
   'NovaD12AbsentTarget', but no observed window matches…", zero typing actions,
   status `failed`.
2. **Isolated disposable window.** A Tk window titled `NovaD12Target` (pid 41800,
   handle 2231302, process `python`) was the only window with that title. The real
   operator path typed through it: the window's own text buffer read back
   `the phrase d12 isolated check`; the delivery-time foreground identity was
   `[2231302, 41800]` with `identity_match: true`; every action target was
   `NovaD12Target`; the unrelated applications present (YouTube/Brave, Chrome,
   Freebuff) were only observed.
   The run then reported **failed**, not completed, because NovaControl cannot read
   an arbitrary application's buffer: "1 input step(s) were never verified in the
   intended target". That is the honest outcome the independent-verification rule
   requires — the destination is proven, the effect is not, so no success is claimed.

### J.7 Regression checks run

| Check | Result |
| --- | --- |
| `tests/test_d12_targeting.py` | 28 passed |
| `tests/test_agentcore.py`, `test_phase29_computer.py`, `test_phase30_operations.py`, `test_phase31_skills.py` + D12 | **175 passed**, exit 0 |
| `mypy src/novacontrol/agentcore src/novacontrol/desktop` | Success, 23 source files |
| `ruff check` on every changed file and the new test | `targeting.py` and `test_d12_targeting.py` are clean; the touched modules keep the project's pre-existing E501 style (repo baseline 242 errors), and the added lines follow that existing convention |
| `git diff --check` | clean |
| Live D12 gate | 18/18 |
| DF-6 operator loop | 16/16 (2026-11-26; **retracted 2026-11-27 — PARTIAL, §N**) |
| DF-6 controller path (previous pass) | 8/8 |

### J.8 Known limitations recorded, not hidden

* NovaControl cannot verify typed content inside an arbitrary application, so a
  typing objective now ends `failed` with "input step(s) were never verified"
  unless the effect is observable. This is deliberate: an input API returning is
  not evidence, and reporting success was the D12 failure mode.
* The recovery diagnosis for the live typing run read "Target 'NovaD12Target' is
  gone but a similar element 'NovaD12Target' exists" — a cosmetic mis-diagnosis in
  `RecoveryEngine` (it compares a step target to element text). It did not affect
  the verdict, which came from the input-step check.
* Window identity is `(handle, pid)`. A window recreated with the same title by the
  same process is `identity_changed` (correct), but a legitimate app restart during
  one objective would also require re-planning — that is the safe direction.

### J.9 DF-8 — unchanged, still a product decision

Not implemented, by instruction. The analysis in §I.6 stands: a `computer` skill
step kind would duplicate routing for the same desktop input and nest the
operator's loop inside the skill executor. The exact decision required is:

> Should a Phase 31 skill be able to invoke the **whole Phase 29 operator loop**
> (classification, durable operation history, learning capture) as one step, or is
> the existing `desktop`/`browser` step — which already performs verified computer
> operations through the token-gated controllers — the intended reading of
> "desktop/browser skill steps use the real controllers and token path"?

If the former: add `COMPUTER` to `SkillStepKind`, route it through
`plan_computer_task`/`execute_computer_task`, require `context["approved"]`, and
read the operator's own verdict into `StepOutcome`. If the latter: 31 #13 is
satisfied by the existing steps and the row is a documentation gap, not missing
capability. No code was written for either branch.

### J.10 Closure statement for D12

**D12: CLOSED.** The wrong-window regression is prevented on three independent
levels — the observation cannot bind a named target to another window, the action
cannot type without a bound handle/pid and a matching delivery-time foreground, and
the verifier fails if the window observed after the action is not the same
identity. The original scenario (Realtek first and foreground) is a fixture test;
the live run typed into an isolated window and nowhere else; the DF-6 operator loop
now fails honestly when its target dies mid-paste.

**Group 1 remains NOT CLOSED**, for environmental reasons only: DF-1 (second
display), DF-2 (resolution change), DF-3 (sleep/resume), DF-4 (multi-hour soak),
DF-5 (development-environment workflow), DF-7 (OS reboot), and the DF-8 decision
above. 31 #13 stays PARTIAL pending that decision. Group 2 has not been started.---

## K. Fifth Group 1 pass (2026-11-26) — DF-8 requirement resolution and final acceptance planning

This pass changed **no source code**. It revalidated the D12/DF-6 evidence against the
current tree, investigated what Phase 31 #13 actually requires, assessed the
architecture against each reading, prepared (but did not apply) the smallest
implementation for the wider reading, and wrote the environmental acceptance plan.

### K.1 D12 and DF-6 evidence revalidation (read-only; no live interaction)

| Claim | Evidence in the current tree | Result |
| --- | --- | --- |
| Objective-bound targeting is implemented | `agentcore/targeting.py` (extraction, `bind_window`, `revalidate`); `perception.observe(target_window=)` at lines 79/154/244; `orchestrator` sets `interpretation["target_window"]` (113) and `target_identity` (343); `actions.type_into_active(window_handle, window_pid)`; controller `expect_handle`/`expect_pid` (1183, 1872-1896); `Expectation.target_identity` | Present |
| Ambiguous and absent targets are refused | `targeting.bind_window` → `absent`/`ambiguous`; `orchestrator._target_refusal` records a `refused` action and sends nothing; `tests/test_d12_targeting.py::test_absent_target_sends_no_input`, `test_ambiguous_target_sends_no_input`, `test_similarly_named_windows_are_ambiguous`, `test_duplicate_exact_titles_are_ambiguous` | Present |
| Identity is preserved through planning, execution, verification | `(handle, pid)` stored per task, passed into the action, re-checked at paste time (`FGHWND`/`FGPID`), and required again by the verifier after the action | Present |
| Wrong-window regression is covered | `test_realtek_scenario_types_only_into_the_named_window` (Realtek first and foreground in fixtures; no real input) | Present |
| Live isolated-window evidence is intact | `tmp/group1/d12_live_evidence_20261126T235325.json`: 20 checks, 18 pass, 0 fail (2 informational); the disposable window's own buffer read back `the phrase d12 isolated check`; delivery identity `[2231302, 41800]` matched | Intact |
| DF-6 terminated during input delivery | `tmp/group1/midaction_crash_evidence_20261126T235403.json`: marker `event_type=2`, `keysym=Control_L`, probe exit 3; action failed on `expected handle/pid [2164478, 39052]` vs `observed [15729620, 4192]` | Intact |
| Failed state and recovery are durable | Same evidence: run and history `failed`; fresh instance restored the failed record with its non-successful action (M4.2–M4.4); recovery recorded with strategy/reason (M3.1) | Intact |
| No duplicate execution or token replay | M4.1 (replayed token → `Approval token is unknown`), M4.5 (objective recorded exactly once), one executed action | Intact |
| Focused suites still pass | `pytest tests/test_d12_targeting.py tests/test_agentcore.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py -q` → **175 passed**, exit 0 | Verified today |

**Discrepancies recorded (none affects the closure):**

1. `tmp/group1/d12_live_evidence_20261126T235031.json` is an **earlier, superseded**
   live run whose L3.1 failed because the probe's Tk focus sat on the toplevel rather
   than the Text widget. The probe was corrected and re-run; §J.6 cites only the
   passing run. The superseded file is retained as history.
2. `tmp/group1/midaction_crash_evidence_20261126T232321.json` is the D12-finding run
   (7 failing checks) and `...232648`/`...232711` are controller runs that failed the
   audit-trail check before the fix; `...232731` supersedes them (8/8). These files
   are append-only history, not current state.
3. §J.7's "175 passed" was re-measured today and is correct; the count includes
   `test_agentcore.py`, which the D12-section command list omits explicitly.

### K.2 Phase 31 #13 — what the original requirement actually says

| Source | What it records |
| --- | --- |
| Canonical phase brief | **Unavailable.** `ROADMAP.md` stops at Phase 20 (a numbered backlog of infrastructure items), `docs/PHASES.md` ends in narrative for the Phase 15-era work, and no `PHASE31*` brief/spec exists in the repository (defect D2, already registered) |
| `docs/PHASE31_REPORT.md` row 13 | Requirement: "Phase 29 computer-operation integration"; implementation evidence: "desktop/browser skill steps use the real controllers and token path"; verification: "Live C real application transfer; no `ComputerOperator` step"; verdict PARTIAL — "controllers verified, operator package path not" |
| `docs/SKILLS.md` | "Phase 29 (computer operation): desktop and browser skill steps use the same controllers and token-gated plan/execute path a computer operation uses, including closed-loop action verification. The Phase 29 `ComputerOperator` itself is not a skill step type yet." |
| README | "…executes each step through the existing tool/request/desktop/browser/agent paths…" (no operator claim) |

**Limitation stated explicitly:** the requirement's wording comes from the phase
report that graded it, not from an original brief. Any "must" below is either the
report's own recorded gap or an inference, and §K.3 marks which is which.

### K.3 Interpretation comparison

| Reading | What it would require | Repository evidence | Assessment |
| --- | --- | --- | --- |
| **A. Skills perform computer operations via the existing desktop/browser controllers** | A skill step can drive real desktop/browser automation, under permissions and a single-use token, with per-action verification | Implemented: `_execute_skill_step`'s DESKTOP/BROWSER branches require `context["approved"]`, mint a token through `plan_desktop_command`/`plan_browser_command`, execute through `execute_desktop_command`/`execute_browser_command`, and verify via `_skill_device_outcome` (every action `completed`, else failure). Live C transferred a real Control Panel skill to a second application; browser transfer 32/32 | **Satisfied today** — this is what the report's "implementation evidence" column describes |
| **B. Skills can invoke the whole `ComputerOperator` workflow** (classification, approval, perception/planning, execution, verification, durable operation history, learning capture) | A named step kind that calls `plan_computer_task`/`execute_computer_task` as one unit, with the operator's own verdict as the step result | **Not implemented.** `SkillStepKind` has `request/tool/desktop/browser/agent/verify`; no path in `_execute_skill_step` reaches `computer`. The report itself records this absence ("no `ComputerOperator` step"), which is why #13 is PARTIAL | Not satisfied; **plausible reading** of the report's own limitation, but stated nowhere as a requirement |
| **C. A distinct capability specified by the original brief** | Some third behaviour (e.g. a separate operator-step contract, its own approval surface) | No brief, no design note, no test, no doc describing it | **No evidence exists.** Would be invented |

The distinction between A and B is real, not naming: a `desktop` step takes a command
phrase and the JARVIS grammar (`parse_desktop_command`) plans it. It cannot express a
goal the operator must itself interpret, classify, perceive, replan and recover — a
multi-step objective such as "open the editor and save a scratch file" is not one
desktop command. So A gives skills *actions*; B would give a skill one *objective*.

### K.4 Architecture assessment against B

| Question | Finding |
| --- | --- |
| Do desktop/browser operations already work through `SkillService`? | Yes. The service is executor-free (`_execute_skill_step` is a router); device steps mint and consume the same single-use token a typed command uses, and verification reads per-action `completed` status |
| Is `ComputerOperator` integration missing, or only a named step kind? | The **operator entry point is not wired from skills**; the operator itself is complete and reachable through `plan_computer_task`/`execute_computer_task` (the Phase 29 path). So this is an integration gap, not a missing engine |
| Would a new `COMPUTER` step duplicate existing controllers? | No controller would be duplicated. It would call the same plan/execute entry points, which already compose the same `AgenticOrchestrator`. It would duplicate **authorization bookkeeping** (skill scope check + operator preflight) and add a second history record per step — acceptable if deliberate, not if hidden |
| How should the approval token be obtained/consumed? | Exactly as the device branches do: the step requires `context["approved"]`, then `plan_computer_task(rendered)` mints `{objective, plan}`-bound token, then `execute_computer_task(rendered, approval_token=token)`. The token is consumed by the API before operator input; single-use, TTL-bound |
| How are sensitive operations authorized? | Two independent gates remain: `SkillService.execute` checks every declared scope and parks/blocks (`waiting_approval`), and `ComputerOperator.run` classifies the objective and raises `PermissionError` for SENSITIVE/DESTRUCTIVE/PRIVILEGED without approval. The API's `execute_computer_task` also consumes the plan token. A `COMPUTER` branch must not bypass either |
| How is D12 targeting preserved? | Automatically. `execute_computer_task` → `ComputerOperator.run` → `AgenticOrchestrator`, which extracts the objective's window, binds identity, refuses absent/ambiguous targets and verifies the same identity. The skill step's rendered instruction is the objective, so the window must be named in it |
| How is independent verification obtained? | From the operator's own state: every action's `status`, `verification.final.status`, and the durable `ComputerOperationRecord`. A step must not report success from `status: completed` alone if the final verdict is not `pass` |
| How are failures, interruption and recovery recorded? | The operator already records failures and blocked runs durably (`record_task`/`record_blocked`), keeps the state on exceptions, and restores it in a fresh instance. A skill step reuses that record; `SkillExecution` records the step's own result/checkpoint. Duplicate history must be avoided by keeping ONE operation record for the step |
| Does Phase 27 learning stay honest? | `_capture_agentic_learning` runs on the returned state inside `execute_computer_task`; it records what the run observed, and a failed run yields failure evidence. A `COMPUTER` step must call the API (not the operator directly) so this path stays the only learning entry |
| Is a nested operator loop required? | **No.** One `execute_computer_task` call runs one operator loop for the step — the same shape as a `request` step running one `handle_request` pipeline. Nesting would only appear if a skill *re-planned* inside its own step loop, which the proposal forbids |

### K.5 Recommended product decision

**Recommendation: adopt reading A.** It is the only reading supported by text in the
repository, it is already implemented and live-verified, and #13 would convert to PASS
on evidence rather than on new code — which also satisfies the "do not implement
functionality merely to make a row PASS" constraint.

If the product intent is actually B (one skill step = one whole computer objective),
that is a **new feature**, not a correction of an incomplete one. It should be
recorded as such and implemented only under §K.6's constraints. Reading C has no
evidence and should not be adopted.

Neither decision is taken in this pass; #13 stays **PARTIAL** until the user answers
§K.9 Q1.

### K.6 Implementation proposal for B (not applied)

**Files and interfaces (smallest additive change):**

| File | Change |
| --- | --- |
| `src/novacontrol/skills/models.py` | `SkillStepKind.COMPUTER = "computer"` (serialization already falls back to REQUEST for unknown kinds, so old catalogs keep loading) |
| `src/novacontrol/application.py` | new branch in `_execute_skill_step` after BROWSER; new `_skill_computer_outcome(result)` next to `_skill_device_outcome` |
| `tests/test_phase31_skills.py` (+ maybe `tests/test_phase31_computer_step.py`) | positive/negative cases below |
| `docs/SKILLS.md`, `docs/PHASE31_REPORT.md`, API/step-kind docs | the new kind is part of the skill contract, so it must be documented as such |

**Execution flow:**

```python
if step.kind is SkillStepKind.COMPUTER:
    if not bool(context.get("approved")):
        return None                      # park: approval precedes any step
    plan = await self.plan_computer_task(rendered)
    token = str((plan.get("approval") or {}).get("token") or "")
    if not token:
        return StepOutcome(False, error="the computer plan produced no approval token")
    result = await self.execute_computer_task(rendered, approval_token=token)
    return self._skill_computer_outcome(result)
```

`_skill_computer_outcome` semantics:
* verified **only** when `result["status"] == "completed"` **and**
  `result["state"]["verification"]["final"]["status"] == "pass"` **and** at least one
  executed action exists and every action's `success` is true;
* `evidence` = per-action detail (bounded) + the durable operation record id;
* `error` = the final verdict reason / first failing action / `result["reason"]`;
* `data` = `{"operation_id", "status", "verification"}` only (declared skill outputs
  are filtered by the service, so no full state leaks into composition);
* **no retry inside the step.** Failure returns a failed `StepOutcome`; recovery is the
  skill's/operator's existing concern.

**Permission and approval boundaries:** the service's declared-scope check and
`context["approved"]` gate stay first; the operator's `PermissionError` preflight and
the API token consumption stay second; nothing in the branch may construct
`ApprovedApprovalGateway` or call `ComputerOperator.run` directly.

**D12 safety integration:** none needed beyond passing the rendered instruction as the
objective; the operator's targeting refuses unnamed/absent/ambiguous windows. A test
must pin that a skill step whose instruction names a window ends with input only in
that window, and that a typing instruction with no window is refused.

**Durable state and recovery:** exactly one `ComputerOperationRecord` per step (written
by the operator); `SkillExecution.results` stores the step's verdict and the operation
id. A parked step (`None`) is unchanged: checkpoint + `waiting_approval`, no operation
record, no input. Interruption/cancel must propagate through the operator's existing
`cancel()`; the step must not start a second run.

**Positive tests:** a skill with one `computer` step whose objective completes and
passes final verification; a step that completes with the operator's record present;
a composed skill whose declared output binds from `data`.

**Negative tests:** final verdict `fail` → step fails; absent named window → step fails
with zero input and the refusal reason; sensitive objective without `approved` → blocked
by the operator preflight; no `context["approved"]` → `None` park (not a failure) and no
token minted; a second concurrent computer step → `ComputerTaskBusy` surfaces as a
failure, never as success; a step declared for another environment → refused before the
operator; a `COMPUTER` step with an unregistered/absent operator → blocked.

**Live acceptance requirement:** one `computer` skill step against a disposable,
uniquely titled window (identity verified, buffer readback, no unrelated app targeted)
and one absent-target refusal — the D12 live-gate shape. Unit tests alone are not
acceptance.

**Regression risks:** new enum value in a public step contract (old catalogs load, but
any exhaustive `SkillStepKind` switch must be checked); skill validation may need to
reject a `computer` step on environments without a desktop controller; two approval
surfaces could confuse operators (document that both are required); a per-step operator
run adds latency and a durable record per step; Phase 27 learning will capture one
objective per step, which is intended but should be stated.

### K.7 Environmental acceptance plan (no test executed in this pass)

Every row needs explicit operator authorization before it is attempted; none is
authorized by this document.

| ID | Acceptance criterion (from §H.6) | Environment / hardware | Authorization | Safety precautions | Procedure | Evidence required | Cleanup / recovery |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DF-1 | 29 #16/#2: operate across multiple monitors | A second physical display attached and extended | Operator attaches hardware and agrees to a short session | Use only a disposable window on the second display; no input to other apps; keep the primary display free of sensitive windows | Enumerate displays; observe geometry; open a titled probe window on display 2; run one objective naming it; verify capture side, coordinates and identity | Observation JSON (displays + bounds), before/after window identity, probe buffer readback | Close the probe; return windows to the primary display; detach the display if requested |
| DF-2 | 29 #2/#21: survive a live resolution/scaling change | Windows display settings; record current mode first | **Explicit** authorization to change display mode | Save all work; use a short-lived mode; never change to an unsupported mode; stop on any visual failure | Record mode → run an objective → change mode mid-run (or between steps) → observe → verify the run fails honestly or recovers; restore mode | Original/current mode values, run state/verification, recovery record, post-restore observation | Restore the exact original resolution and scaling; confirm visually |
| DF-3 | 29 #21: recover from OS sleep/resume | Sleep-capable host; wake timer or manual wake | **Explicit** authorization to suspend | Save all work; no pending long jobs; expect network drops | Start an ACTIVE objective with a persisted checkpoint → suspend → wake → fresh observation/`resume_objective` | Pre/post timestamps, restored objective/task state, resume verdict | None beyond confirming services reconnected |
| DF-4 | 29 #27, 30 #15: multi-hour stability | Unattended machine for the full window | **Explicit** authorization for an unattended run | Only non-destructive objectives; watchdog + emergency-stop path; no sensitive data on screen; bounded task count | Run periodic verified tasks with RSS/thread/handle sampling for the agreed duration; record failures | Time series (RSS, latency, failures), final state restore, no-duplicate check | Stop the loop, cancel remaining tasks, confirm no stray processes or held input |
| DF-5 | 29 #12: development/system workflow | The user's editor/toolchain + a disposable project under `tmp/` | **Explicit** authorization to drive the editor | Never open the real project; editor on a scratch file only; no terminal commands beyond the scratch project | Open the disposable project → perform a small edit/save through the operator → verify file content and window identity | Buffer/readback of the scratch file, before/after hashes, action audit trail | Close the editor window; delete only the disposable `tmp/` project |
| DF-7 | 30 #3/#15: survive an OS reboot | Reboot-capable host | **Explicit** authorization to reboot | Save all work; close apps; expect this session to end | Persist an ACTIVE objective with a task → reboot → fresh application instance → inspect `resume_required` → resume | Pre-reboot state snapshot, post-reboot restored record, resume result, no duplicate execution | None; re-open the environment and confirm the objective's terminal state |

### K.8 Functional limitations (retained, not acceptance blockers)

1. **LIM-1 — typing objectives cannot observe the effect.** The controller proves the
   destination (clipboard staged, foreground handle/pid matches) but cannot read an
   arbitrary application's buffer. Consequence: an otherwise correct typing objective
   ends `failed` ("input step(s) were never verified"). This is **not** a D12 blocker
   (D12 is about *where* input goes, now proven), and it is **not** fixed by relaxing
   verification. Options for a future decision: (a) accept the failed verdict
   (current, safest); (b) add per-application readback adapters (editor buffers,
   accessible text) as opt-in verification; (c) OCR the target region and require a
   match. (b)/(c) are new capabilities with their own false-positive risk.
2. **LIM-2 — recovery mis-diagnoses a present target as absent.** In the live typing
   run the recovery text read "Target 'NovaD12Target' is gone but a similar element
   'NovaD12Target' exists." Cause: `RecoveryEngine._find_relocation`
   (`agentcore/recovery.py:158`) scores `state.interactive()` elements by word overlap;
   the window element's text **is** the target, so it scores full overlap and is
   returned as a "similar" element, while the message asserts the target is gone. The
   immediate risk is wording; the latent risk is that `RELOCATE` to the same element
   can re-deliver input that already landed (duplicate typing) on a retry. It is
   **not** an acceptance blocker for D12/DF-6 (no false success; the live run executed
   one action), but it should be tracked and fixed deliberately: when an element's
   text equals the failed target, report "present but the effect was not observed" and
   prefer re-observe/verify over relocate, and make a relocate onto an identical
   target a no-op for input actions.

### K.9 Updated Group 1 acceptance matrix (delta only)

| Row | Before this pass | After | Why |
| --- | --- | --- | --- |
| 29 #5 no wrong-app input | PASS (D12 closed) | PASS | revalidated against source + 28 fixtures + live 18/18 |
| 29 #20 mid-action crash | PASS (16/16) | PASS | evidence re-read; durability, replay refusal, single record confirmed |
| 31 #13 Phase 29 operator as a skill step | PARTIAL | **PARTIAL — decision required (Q1)**; reading A would make it PASS on existing evidence, reading B would need §K.6 |
| 31 #11, #12 | PARTIAL | PARTIAL | unchanged; outside this pass |
| 31 #6 | PASS | PASS | unchanged |
| DF-1…DF-5, DF-7 | outstanding | outstanding | plan written in §K.7; no authorization |
| DF-6 | PASS | PASS | revalidated |
| DF-8 | analysed | **resolved as a decision, not code** | §K.5/§K.6 |

**Group 1 remains NOT CLOSED.** No criterion was converted on inference; #13 awaits the
product decision and the six environmental rows await authorization.

### K.10 Decisions required from the operator

1. **#13 interpretation:** adopt A (skills perform computer operations through the
   existing desktop/browser controllers) and close #13 on existing evidence, or adopt B
   (whole-operator step kind) and authorize §K.6.
2. If B: authorize implementing `COMPUTER` with the exact semantics in §K.6, including
   the live acceptance gate.
3. Environmental authorizations, individually: DF-1 (hardware), DF-2 (resolution
   change), DF-3 (sleep), DF-4 (unattended soak), DF-5 (editor workflow), DF-7 (reboot).
4. LIM-1 policy: keep failing unobservable typing objectives, or authorize readback
   adapters/OCR as an opt-in verification capability.
5. LIM-2: authorize the recovery-diagnosis correction (and the duplicate-input guard)
   as a separate small fix.

## L. LIM-2 remediation — recovery diagnosis and duplicate-input prevention (2026-11-26)

Scope: LIM-2 only. No Group 2 / Phase 32 work, no `COMPUTER` skill-step, no DF-8 decision,
no change to LIM-1 typing verification, no live desktop input, no NovaMarket/NovaForge edit,
no commit, push or deploy. Uncommitted work and prior evidence were preserved; HEAD is
`afa5ff6`.

### L.1 Root cause

`RecoveryEngine._find_relocation` scored every `UiState.interactive()` element by word
overlap between the failed target and `f"{element.text} {element.selector}"`. An element
whose text **is** the target scores full overlap, so it was returned as a "similar"
element and a target that was still on screen was reported as gone:

    Target 'NovaD12Target' is gone but a similar element 'NovaD12Target' exists.

(observed in the DF-6/D12 live pass). `AgenticOrchestrator._recover` then replanned with
`description="Click the relocated element '<target>' instead."`, replacing the failed step,
and a `type …` step re-entered dispatch as a click on that window. After a click that had
been delivered but not verified (the LIM-1 situation) the same code named the target itself
as its own replacement — a second click on the same element.

### L.2 Was duplicate input reachable, or only theoretical?

**Theoretically possible; not reachable through the shipped orchestrator as it stood.**
`_next_step` retires any plan entry whose id already appears in `state.actions`, and
`AdaptivePlanner.replan` reuses the failed step's id (`replace(failed_step, description=…)`),
so the replanned RETRY/RELOCATE step was never re-dispatched — automatic recovery was
inert. The new fixture `test_not_delivered_failure_retries_through_the_target_gate` shows
that: before giving a retry a fresh attempt id, the second call never happened.

The defect was nevertheless real, not cosmetic:

1. the operator-facing diagnosis was false (a present window reported as gone);
2. the recovery object's `alternative_target` was the failed target itself — a duplicate
   click for any dispatcher, and clearly the code's intent;
3. the replanned description rewrote `type …` into a click, so a retried typing step would
   have clicked the window instead of typing;
4. one id change, or any other caller of `planner.replan` that dispatches the returned plan,
   would have turned 2 and 3 into duplicate or unintended input.

### L.3 Behaviour before and after

| Situation | Before | After |
| --- | --- | --- |
| Target present, same handle+pid, effect unobserved | `RELOCATE` to the same element, "gone but a similar element exists" | `RECHECK`: "still present with the same identity", no relocation, no input |
| Input delivered (`success=True`) but expected effect not observed | replanned click on the same target | `RECHECK`: re-observe and re-verify only; run fails if the effect is still absent |
| Delivery unknown (interruption, failed post-send verification) | replanned click | `RECHECK`: "could not be confirmed … repeating it could duplicate input" |
| Input provably never delivered (refused, no matching element, no window) | relocation or research | `RETRY` of the *same* step (no click degradation), through the existing target/permission gates, with a fresh attempt id |
| Target present but identity changed (same title, new handle or pid) | relocation candidate | `ABORT`, "it is not the window that was acted on" |
| Target ambiguous | relocation candidate | `ABORT`, "refusing to guess which one is meant" |
| Target genuinely absent, different element matches | `RELOCATE` (could name the target itself) | `RELOCATE` to a **different** element only; the target's own text/selector is excluded |
| Non-input actions (observe, focus, open, navigate, screenshot) | retry | retry (classified idempotent) |

Implementation:

* `RecoveryStrategy.RECHECK` added (re-observe and re-verify only, never dispatches).
* `input_delivery_state(record)` classifies `delivered` / `not_delivered` / `uncertain` /
  `not_input` from the durable `ActionRecord`, against an explicit
  `NON_IDEMPOTENT_ACTIONS` set (`type`, `click`, `press_key`, `drag`, `scroll`, `save`,
  `paste`, `execute_terminal_command`, …). `focus_window`, `open_application`, `navigate`,
  `go_back`, `minimize_window`, `maximize_window`, `screenshot` remain retryable. There is
  no blanket retry ban, and no reliance on the input API's return value for success.
* `RecoveryEngine._ground_target` compares the fresh observation's
  `metadata["target"]` binding with the objective's bound identity
  (`state.interpretation["target_identity"]`) and reports `present_same`,
  `present_changed`, `absent`, `ambiguous` or `unknown` (browser flows report no binding).
* `_recover` forces `RECHECK` for any `delivered`/`uncertain` input even if an engine
  selects RETRY or RELOCATE (defence in depth), records the delivery class in
  `state.recoveries`, and hands `RECHECK` to the new `AgenticOrchestrator._recheck`, which
  observes once, verifies with the bound identity, marks the step done on PASS, and fails
  the run with "The action was not repeated because its input may already have been
  delivered …" otherwise. No action is dispatched and no second `ActionRecord` is appended.
* An authorized `RETRY` keeps the original step description and executes as a new attempt
  (`<step-id>:retry:<n>`), so `_next_step` no longer retires it; the original action keeps
  its id, outcome and record. Every retry still passes `_target_refusal`/`_bound_target`
  (D12) and the `ActionEngine` permission and approval checks.

### L.4 Files changed

* `src/novacontrol/agentcore/recovery.py` — `RECHECK`, delivery classification,
  `_ground_target`, identity-aware `diagnose`, same-target exclusion in `_find_relocation`.
* `src/novacontrol/agentcore/orchestrator.py` — `_recover` (expected identity, delivery
  override, RECHECK branch, retry attempt id, retry keeps its step), new `_recheck`.
* `tests/test_lim2_recovery.py` — new, 21 focused tests (fixtures and recorders only).
* `tests/test_agentcore.py` — two recovery fixtures now state the failure they model
  (`"no matching element in the current UI state"`); an empty detail is correctly read as
  an uncertain delivery. One assertion (`outcome.strategy == RELOCATE`) was added to a test
  whose expression was previously a no-op; no assertion was weakened.
* Documentation: this section, `docs/PHASE29_REPORT.md` §17, `docs/STATUS.md`,
  `docs/DEVELOPMENT_LOG.md`.

### L.5 Regression evidence

| Command | Result |
| --- | --- |
| `pytest tests/test_lim2_recovery.py -q` | **21 passed**, exit 0 |
| `pytest tests/test_lim2_recovery.py tests/test_d12_targeting.py tests/test_agentcore.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py -q` | **196 passed**, exit 0 (24.6 s) |
| `mypy src/novacontrol/agentcore src/novacontrol/desktop` | **Success**, 23 files, exit 0 |
| `ruff check tests/test_lim2_recovery.py` | **All checks passed**, exit 0 |
| `git diff --check` | clean, exit 0 |

`ruff` on the two changed source files still reports the module's pre-existing line-length
debt (`recovery.py` 7 findings vs 8 at HEAD; `orchestrator.py` 37 vs 32 — `E501`/`F401`
only, in code that already failed the 100-column rule at HEAD). No new rule class is
introduced and no lint suppression was added; the new test file is clean.

The 21 tests cover every mandated case: identical identity, genuinely absent, replacement
with a different identity, similar title/different pid, same title/different handle,
ambiguous candidates, delivered-but-unobservable effect, not delivered, uncertain after
interruption, relocation onto the original target, non-idempotent no-replay, authorized
retry through the safety path, exactly one durable record for the original action, and D12
wrong-window refusal (absent/ambiguous objectives send nothing).

### L.6 D12 and DF-6 preservation

* `tests/test_d12_targeting.py` — all 28 tests pass unchanged inside the 196.
* `tmp/group1/d12_live_evidence_20261126T235325.json` (18 checks / 18 pass / 0 fail) and
  `tmp/group1/midaction_crash_evidence_20261126T235403.json` (16 pass / 0 fail) were not
  modified by this pass.
* No targeting, permission, approval or execution safeguard was relaxed: `_ground_target`
  only reads the binding, `_recheck` never dispatches, and retries still go through
  `_target_refusal` plus the controller's foreground-identity re-check.

### L.7 Remaining limitations

* Delivery classification reads the action's outcome and detail. An engine that reports a
  delivered action as a bare failure (no detail) is classified `uncertain` and the run
  fails safely rather than retrying; a new action kind must be added to
  `NON_IDEMPOTENT_ACTIONS` to be treated conservatively.
* `_recheck` can only pass when the step's `expected` text becomes observable. An objective
  with no observable expectation therefore still fails — deliberately: recovery may never
  turn an unverified action into a success.
* LIM-1 is untouched: typing objectives still require independent application-effect
  verification, and no clipboard/OCR/readback capability was added.
* No live desktop run was performed in this pass (out of scope); the change is proven by
  fixtures only, and the next live gate must confirm the operator loop end to end.
* Retrying input that provably never reached the target remains automatic, gated by D12 and
  the engine's permission/approval path; no additional operator prompt was introduced.

### L.8 Acceptance matrix (LIM-2 view; §F remains the overall matrix)

| Row | Criterion | Status |
| --- | --- | --- |
| LIM-2a | A present target is never reported as gone or relocated | **PASS** (fixtures: 3 tests) |
| LIM-2b | Target identity: present-unchanged / present-changed / absent / replacement / ambiguous | **PASS** (6 tests) |
| LIM-2c | Possibly-delivered input is never replayed | **PASS** (unit + 2 orchestrator tests, one action record) |
| LIM-2d | Not-delivered input may retry through the safety path | **PASS** (orchestrator test: ≥2 calls, identical handle/pid) |
| LIM-2e | Recovery never converts an unverified action into success | **PASS** (`_recheck` fails closed; run status `failed`) |
| D12 | Objective-bound targeting and wrong-window protection | **PASS** (28 tests, unchanged) |
| DF-6 | Operator-loop mid-action crash | **PASS** (16/16, evidence unchanged) |
| LIM-1 | Typing effect verification | **LIMITATION** (unchanged, by design) |
| DF-8 | `COMPUTER` step kind | **DECISION PENDING** |
| Group 1 | Closure | **NOT CLOSED** |

### L.9 Decisions still required from the operator

1. **DF-8 / #13:** adopt reading A (skills perform computer operations through the existing
   desktop/browser controllers) or authorize reading B (`COMPUTER` step kind, §K.6).
2. **LIM-1 policy:** keep failing unobservable typing objectives, or authorize readback/OCR
   as an opt-in verification capability (separate work; not implemented).
3. **Environmental authorizations**, individually: DF-1 (second display), DF-2 (resolution
   change), DF-3 (sleep/resume), DF-4 (unattended soak), DF-5 (editor workflow), DF-7
   (reboot).
4. **Authorization to resume live computer-operation testing** now that LIM-2 is remediated
   — the operator-loop live gate has not been re-run since this change.

## M. Sixth Group 1 pass (2026-11-26) — DF-8 adopted as interpretation A, LIM-2 retry audit, live preflight

Scope: Group 1 only. No Group 2 / Phase 32 work, no `COMPUTER` step kind, no change to
LIM-1, no live desktop interaction, no NovaMarket/NovaForge edit, no commit, push or
deploy. Uncommitted work and prior evidence preserved; HEAD `afa5ff6`.

### M.1 DF-8 decision: interpretation A adopted

**Decision (operator, 2026-11-26):** Phase 31 criterion #13 is satisfied by skills
performing computer operations through the **existing** desktop/browser controllers.
No `COMPUTER` `SkillStepKind`, no second operator loop, controller, approval gateway or
execution subsystem is added, and none is planned.

**This is an adopted interpretation, not a recovered requirement.** No Phase 31 brief
exists in the repository: `ROADMAP.md` ends at Phase 20 and `docs/PHASES.md` at the
Phase 15-era narrative (D2). The only wording of #13 is the phase report's own
acceptance row and `docs/SKILLS.md` §10.

### M.2 Evidence basis for A (verified in this pass)

| Claim | Where it is proven |
| --- | --- |
| Desktop/browser skill steps drive the real controllers through the token-gated plan/execute path | `_execute_skill_step` (`application.py`) → `plan_*_command` → `execute_desktop_command` / `execute_browser_command`; `docs/SKILLS.md` §10 "Phase 29 (computer operation)" |
| Per-action independent verification, not "the call returned" | skill step verdict comes from the controller's own per-action result; §10 Security |
| Authorization is the existing one | `PermissionManager` scope check per step + plan single-use token; a client `approved` flag is refused with 403 |
| Target identity is the D12 one | desktop steps inherit objective-bound targeting and the foreground handle/pid re-check (D12 PASS, report §J) |
| Live evidence | Live C real-application desktop transfer and the browser transfer run (32/32, `tmp/group1/browser_transfer_evidence.json`) |
| The operator loop is still reachable, separately | Phase 30 `agent="skill:<id>"` dispatch and the `ComputerOperator` entry points; §K.6 notes this |

### M.3 Conflict check

No repository document requires a `COMPUTER` step kind. Searching `COMPUTER` across
`docs/ROADMAP.md`, `docs/PHASES.md`, `docs/SKILLS.md` and `docs/PHASE31_REPORT.md`
returns only the fifth-pass analysis itself (proposal §K.6, never a requirement). The
report's note that "the Phase 29 `ComputerOperator` itself is not a skill step type"
describes reading B as unimplemented; under the adopted definition it is a scope
statement, not a missing deliverable. **No conflicting requirement was found.**

### M.4 Documentation and status changes (no source change for DF-8)

* `docs/PHASE31_REPORT.md`: acceptance row #13 → **PASS (adopted A)**; the analysis
  paragraph now states the decision; new section "15. DF-8 adopted".
* `docs/SKILLS.md` §10: the Phase 29 bullet states the adopted definition and drops the
  "yet".
* `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`, this report.

### M.5 Phase 31 criterion #13 verdict

**PASS — under adopted interpretation A (2026-11-26).** The capability was already
implemented and live-verified; the definition is now explicit. Reading B remains
unimplemented by decision, not by omission.

### M.6 LIM-1 preservation

Unchanged and re-verified by inspection: no edit to `actions.py`, `desktop/controller.py`
or `verifier.py` in this pass. A typing objective still cannot pass because an input API
returned success; clipboard contents, intended text, action arguments and the window title
are not accepted as proof; an unobservable application effect fails the run. No OCR or
per-application readback adapter was added. LIM-1 remains a **documented functional
limitation**, not an acceptance criterion.

### M.7 LIM-2 retry-classification audit

Every action label the agentcore engine can place on an `ActionRecord` was enumerated
(`actions.py` outcomes + the orchestrator's own guard outcomes) and compared with the
classifier. One **fail-open** gap was found and closed:

| Check | Before this pass | After |
| --- | --- | --- |
| Unknown/new action kind | returned `not_input` → eligible for automatic RETRY | treated as **input** (allowlist discipline): `delivered` / `uncertain` / `not_delivered`, never retry-safe |
| `go_back` | retried as if idempotent — a second retry navigates back **again** | `NON_IDEMPOTENT_ACTIONS` |
| Position/state-moving desktop kinds (`mouse_move/click/drag`, `key_down/up`, `execute_script`, `write_file`, `organize_files`, `stop_app`, `app_navigate`, `search_*`, `vision_click`) | unlisted → would have been retry-safe if wired to agentcore | listed non-idempotent |
| Retry-safe set | implicit (everything not in one list) | explicit `IDEMPOTENT_ACTIONS` allowlist (observe/reason/screenshot/focus/min-max/launch/navigate/read/list/system info) |

Confirmed by inspection and tests: uncertain delivery never replays; only a
`not_delivered` failure reaches RETRY; RETRY re-runs the same step through
`_target_refusal`/`_bound_target` (D12) and the engine's permission/approval path; a retry
gets its own attempt id `"<step-id>:retry:<n>"` while the original record keeps its id;
`_recheck` contains no dispatch call and appends no record; a failed recheck ends the run
`failed` with "The action was not repeated…", so recovery cannot turn an unverified action
into a success. Four tests were added for the gaps (unclassified fails closed; unclassified
is RECHECKed; `go_back` not retry-safe; the full label inventory is classified; two
distinct step ids on the retry path): `tests/test_lim2_recovery.py` is now **25 tests**.

### M.8 Ruff baseline versus current

Measured with `ruff check --output-format json`, baseline taken from `git show HEAD:<path>`
via `--stdin-filename` so the same config applies.

| File | HEAD | Now | Added | Removed |
| --- | --- | --- | --- | --- |
| `agentcore/recovery.py` | 8 (`E501` 7, `F401` 1) | **7** (`E501` 6, `F401` 1) | **0** | 1 (the over-long "gone but a similar element" line the fix rewrote) |
| `agentcore/orchestrator.py` | 32 (`E501` 29, `SIM105` 2, `I001` 1) | **36** (`E501` 34, `SIM105` 2) | 10 `E501` | 6 (5 `E501`, 1 `I001`) |
| `tests/test_lim2_recovery.py` | — | **0** | — | — |

Attribution: LIM-2's only ruff finding was `I001` from the new multi-line import, and it
was **fixed** (dataclasses import reordered) — `orchestrator.py` now has no `I001`. The 10
added `E501` lines are all in `_dispatch`/`_finalize` code from the **earlier uncommitted
D12 work** (`require_bound_target=True`, `close_application`, the text/save guards,
`agentcore.task_finished`, the evaluation-ledger call), not in the LIM-2 hunks; five long
verification lines were also removed by that earlier work, so HEAD is not a pre-LIM-2
baseline for this file. The `_recover` signature that LIM-2 extended was already 127
characters at HEAD and is 126 now. No finding indicates a functional defect: all remaining
findings are line-length or `try/except/pass` in pre-existing code. Nothing was suppressed
and no unrelated formatting was done. `F401` in `recovery.py` (`dataclasses.field`) is
pre-existing.

### M.9 Live operator-loop acceptance — preflight (NOT executed)

Harness: `tmp/group1/verify_midaction_crash.py` (369 → 383 lines after this pass).
It drives the supported production path, not a mock: `plan_computer_task` mints the
single-use token, `execute_computer_task` consumes it and runs the real `ComputerOperator`
loop over the real desktop controller, against a disposable Tk window titled
`NovaGroup1MidAction` whose probe process writes a marker on the first key event of the
paste and exits with code 3 mid-action.

Command (single run, from the repository root):

```
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_midaction_crash.py
```

Isolation requirements:

* Run on an attended machine; the disposable window takes the foreground for a few
  seconds. Do **not** type, click or switch windows during the run.
* No other automation, agent run or preview server may be active; port 8000 stays free.
* Fresh data directory `tmp/group1/midaction_data` (the harness deletes and recreates it);
  no production data, no NovaMarket/NovaForge path, Ollama disabled.
* Budget: 20 s window wait + 120 s marker wait + 240 s execute wait; normally under a
  minute. One evidence file per run, never overwritten.

Expected observations (checks M1.1–M4.5, plus the new M2.7/M2.8/M3.4):

1. the disposable window is open and uniquely titled;
2. the plan minted a single-use token (M1.2);
3. the marker exists **and** the probe exited 3 — input arrived, then the target died
   (M2.1, M2.2);
4. run status is not `completed`, no executed action claims the typing succeeded, the
   final verification is not a pass (M2.3–M2.5);
5. **at most one typing attempt is recorded (M2.7)** and every recorded input identity
   equals its bound `(handle, pid)` (M2.8);
6. the operation history records the failure and one objective record only (M2.6, M4.5);
7. recovery is recorded with a reason, and **never selects `retry` or `relocate`
   (M3.4)** — with the target dead and delivery uncertain the LIM-2 engine must choose
   `recheck` or `abort`;
8. the next observation no longer lists the dead window (M3.3);
9. a replayed token cannot re-run the action (M4.1), and a fresh application instance
   restores the failed record with no successful action (M4.2–M4.4).

Emergency stop: `Ctrl+C` in the terminal — the harness's `finally` kills the probe process
and calls `app.stop()`. If the terminal is unresponsive, close the `NovaGroup1MidAction`
window (the probe exits), then stop the terminal's process tree. Never kill by name pattern
and never run `taskkill /IM`. If the run exceeds ~6 minutes it has hung: stop it and
inspect the newest evidence file's `timeline` before retrying.

Cleanup: the probe is killed and exits with the window; `tmp/group1/midaction_data` and the
evidence JSON are left in place as the artefact (delete only that run's data directory if
re-running); nothing else is created outside `tmp/group1`.

Limitations of the harness: it observes the run through the operator's own state and
controller results (no independent screen readback); the marker proves input arrived, not
exactly which keystroke completed; the disposable window is a Tk process, not a third-party
application.

### O.10 Live-UIA preparation, test-suite recovery and closure readiness (2026-11-27, second pass — still no live input)

This pass prepared the live UIA verification, repaired the collection failure that had hidden
the suite, and found one real defect by running it. **No desktop input was performed and no
window belonging to the user was touched.**

| Item | Result |
| --- | --- |
| UIA live harness | **built** — `tmp/group1/uia_probe_app.ps1` (disposable WinForms probe: writable text box, invokable button, panel with no pattern, disabled button, two controls sharing one AutomationId) + `tmp/group1/verify_uia_live.py` (14 checks, `--authorize-live` gate, default mode only prints the plan) |
| COM class availability | **measured `S_OK`** for `CLSID_CUIAutomation`; the production `ComtypesUiaBackend` constructs and reports `available: true`, `verified_live: false` |
| Live UIA operations | **not executed** — awaiting the authorization described in `DF6_LIVE_UIA_VERIFICATION_REPORT.md` §6 |
| `tests/` collection | **fixed** — was failing on a `SkillInvocation` re-export the Phase 31 rewrite dropped; **6,712 tests now collect** with zero errors |
| Staged suite | run one file at a time with a 150 s per-file bound (`tmp/group1/run_suite_by_file.py`, resumable, writes `suite_by_file.json`) — see §O.10's report for the final counts |
| Defect found by the suite | a cancellation **during capture-worker startup** left the host's `ready WxH` banner unread, so the next exchange read it as its frame reply (`ValueError: int('800x600')`) — fixed at the cause and made fail-closed; 2 new regression tests; mutation-checked |

A correction worth recording: the first COM probe reported `REGDB_E_CLASSNOTREG`, which would
have been filed as "UI Automation is not registered on this host". The CLSID **is** registered;
the probe had packed the GUID big-endian. With `uuid.UUID(...).bytes_le` both `CUIAutomation`
and a control CLSID return `S_OK`. The wrong result is recorded here rather than quietly
dropped, because it was one measurement away from becoming this pass's headline blocker.

Statuses are unchanged by preparation alone: interactive keyboard **BEST_EFFORT**, UIA
**implemented and transport-constructible, not live-verified**, isolated session **BLOCKED**
(`no_session_transport`), DF-6 **PARTIAL**, Group 1 **NOT CLOSED**. The preserved evidence is
still byte-identical (sha256 `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34`).
A UIA PASS, when it happens, will not cover games, canvas UIs or custom-drawn surfaces — those
have no pattern and need the session/VM transport.

Reports: [DF6_LIVE_UIA_VERIFICATION_REPORT.md](DF6_LIVE_UIA_VERIFICATION_REPORT.md),
[DF6_TEST_SUITE_RECOVERY_REPORT.md](DF6_TEST_SUITE_RECOVERY_REPORT.md),
[DF6_SESSION_TRANSPORT_READINESS.md](DF6_SESSION_TRANSPORT_READINESS.md).

**Status: prepared, not executed.** The three new checks compile (`py_compile`) but have
never run; the recorded 16/16 DF-6 evidence predates them and remains valid for its own
checks.

### M.10 Acceptance matrix (updated)

| Row | Criterion | Status |
| --- | --- | --- |
| Phase 31 #6 | cross-application and browser skill transfer | **PASS** (live) |
| Phase 31 #13 | computer-operation integration, **adopted interpretation A** | **PASS** (adopted 2026-11-26; report §M) |
| D12 | objective-bound targeting, wrong-window refusal | **PASS** (28 tests, live 18/18) |
| DF-6 | controller mid-action crash | **PASS** (8/8) |
| DF-6 | operator-loop mid-action crash | **PARTIAL** (§N: crash/fail-closed PASS, identity containment FAIL) |
| DF-6-L1 | paste tail after a mid-delivery target death | **CONTAINED in the guaranteed profiles, BOUNDED in the labelled one (§O.9)** |
| DF-6-L1 | interactive keyboard profile | **BEST_EFFORT** — chord split into gated edges, ≤1 input event per identity check, race recorded (43 ledger tests) |
| DF-6-L1 | UIA profile | **IMPLEMENTED, TRANSPORT CONSTRUCTIBLE, NOT LIVE-VERIFIED** — the COM class creates on this host and the production backend reports available; no element was acted on (§O.10) |
| DF-6-L1 | UIA live harness | **PREPARED, AWAITING AUTHORIZATION** — 14 checks, disposable probe, zero-keyboard evidence, `--authorize-live` gate |
| DF-6-L1 | isolated-session profile | **BLOCKED** — no session/VM transport configured (`no_session_transport`), requirements now written down exactly |
| LIM-2 | recovery diagnosis + duplicate-input prevention | **PASS** (25 tests) |
| LIM-2 audit | retry classification fails closed | **PASS** (this pass) |
| LIM-1 | typing effect verification | **LIMITATION** (documented, not an acceptance criterion) |
| DF-1…DF-5, DF-7 | environmental acceptance | **OUTSTANDING** (not executed) |
| Live operator-loop gate | post-LIM-2 confirmation | **EXECUTED 2026-11-27** (§N: LIM-2 `recheck` confirmed; DF-6 PARTIAL) |
| Group 1 | Closure | **NOT CLOSED** |

### M.11 Authorization required before any live interaction

1. The preflight run above (`verify_midaction_crash.py`), with the operator present —
   this is the only live desktop interaction this pass proposes.
2. Environmental criteria remain separately authorized and unexecuted: DF-1 (second
   physical monitor), DF-2 (resolution change), DF-3 (OS sleep/resume), DF-4 (multi-hour
   soak), DF-5 (development-environment workflow), DF-7 (OS reboot).
3. Post-run acceptance: the LIM-2 guard is "PASS" on fixtures; the live gate is what
   converts the operator-loop row from "PASS on the pre-LIM-2 harness" to "PASS on the
   current tree". Group 1 stays **NOT CLOSED** until that run is authorized and passes,
   and until the environmental rows are executed or explicitly waived by the operator.

## N. Seventh Group 1 pass (2026-11-27) — DF-6 operator-loop, current tree

One authorized attended live run of the prepared preflight was executed. No production
source changed in this pass; the only code edit is a *strengthening* of one harness check
(§N.5). Group 1 is **NOT CLOSED**.

### N.1 Execution record

* **Command (exactly as authorized, run once):**
  `PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_midaction_crash.py`
* **Tree:** `main` / `afa5ff6`, pre-existing uncommitted worktree preserved; no reset,
  clean, stash, commit, push or deploy. Python 3.13.0, win32.
* **Path:** production only — `plan_computer_task` (single-use token) →
  `execute_computer_task` → `ComputerOperator` → real `DesktopController`. No mocks.
* **Isolation:** fresh `tmp/group1/midaction_data` (the harness deletes and recreates it);
  the preceding run's directory was copied to
  `tmp/group1/midaction_data_prev_20261126T235403` *before* launch, so no earlier artefact
  was lost. Target: the disposable `NovaGroup1MidAction` window only.
* **Pre-flight confirmations:** no `NovaGroup1*` window existed; the harness (`finally`
  kills the probe, then `app.stop()`) was inspected; no automation or computer-operation
  session was active — two orphaned `uvicorn` workers (pids 20092/23632) had no listener
  (`netstat` shows nothing on 55126), and `automation.json`/`automation_tasks.json` hold no
  workflows or tasks. They were left untouched.
* **Outcome:** **19/19 harness checks as written**, exit 0; evidence
  `tmp/group1/midaction_crash_evidence_20261127T010222.json`, sha256
  `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34`, `at`
  2026-11-27T01:02:36, `data_dir` = that run's `midaction_data`.
* **Crash:** probe pid **33624** wrote `event_type=2 / keysym=Control_L / pid=33624` on the
  paste's first key event and exited **3** mid-delivery. Task
  `96d678e5d0d04a818877cdd70a696223`, status `failed`.
* **Cleanup:** probe gone (no `NovaGroup1*` window or process remains), no test process
  running, evidence and data directory retained, no other application modified.

### N.2 Mandatory acceptance assertions, one by one

| # | Assertion (brief §4) | Verdict | Direct evidence |
| --- | --- | --- | --- |
| 1 | target receives the initial input event | **PASS** | marker `event_type=2`, `keysym=Control_L`, `pid=33624` (the disposable window's process) |
| 2 | target terminates during input delivery | **PASS** | marker written on the paste's first key; probe exit 3 |
| 3 | expected termination code | **PASS** | `probe_exit = 3` |
| 4 | action not recorded as completed | **PASS** | run `status: failed`; history `failed`; `successful_type_actions = 0` |
| 5 | input identity matches the objective-bound target | **PASS** | expected identity `[7670084, 33624]` is the probe's handle/pid; action target `NovaGroup1MidAction`; the probe received the key |
| 6 | expected handle/PID and observed delivery identity are consistent | **FAIL** | expected `[7670084, 33624]`, observed `[15729620, 4192]`; controller `identity_match = false` → `VerificationError`; final verdict: target `absent` (0, 0), "The input cannot be attributed to the intended application" |
| 7 | no unrelated application receives input | **NOT SATISFIED** | post-paste foreground was pid **4192 (Freebuff)**; the remaining `V`/`V-up`/`Ctrl-up` of the `SendKeys` chord were dispatched after the probe exited (§N.3) |
| 8 | M2.7 — at most one typing attempt | **PASS** | exactly one `type` action record |
| 9 | no automatic retry or relocation replays uncertain input | **PASS** | recovery `delivery: uncertain`, `strategy: recheck`, `succeeded: false`; no `retry`/`relocate` (M3.4) |
| 10 | M2.8 — recorded input identity matches the bound target | **NOT EVIDENCED** | the check was vacuous: the record carries no `verification` dict, the mismatch lives only in `detail` (§N.5) |
| 11 | run and objective recorded as failed | **PASS** | `status: failed`, history `failed`, one objective record |
| 12 | recovery records the correct reason | **PASS** | "Delivery of type … could not be confirmed after the failure; repeating it could duplicate input"; `recheck` → `fail` |
| 13 | dead target absent from the subsequent observation | **PASS** | M3.3; observation candidates list Chrome/Cline/Freebuff/…, no `NovaGroup1MidAction` |
| 14 | fresh instance restores the failed record | **PASS** | M4.2/M4.3: 1 record, `failed` |
| 15 | exactly one objective record | **PASS** | M4.5 |
| 16 | approval token cannot be replayed | **PASS** | `ValueError: Approval token is unknown…` |

Fourteen assertions pass. **Two fail or are unevidenced**, and both concern the same thing:
what the system can prove about *where* the input went once its target died mid-delivery.

### N.3 DF-6-L1 — the paste tail after a mid-delivery death

The Windows typing path stages the clipboard and sends `[System.Windows.Forms.SendKeys]::SendWait('^v')`
(`desktop/controller.py::_stage_and_paste_windows`) — one synthetic chord: **Ctrl-down,
V-down, V-up, Ctrl-up**. The probe dies on the *first* of those (`Control_L`). The chord
was already in flight, so the remaining events are delivered by Windows to whatever window
holds the foreground next — recorded here as pid 4192 (Freebuff), the same window the
2026-11-26 run observed.

The controller's identity check for `type` runs *after* the paste
(`_verify_type_windows` → `_type_text`), so it can only refuse success, not recall the
events. It does exactly that: `identity_match = false` → `VerificationError`, delivery
`uncertain`, final verdict `fail` with "The input cannot be attributed to the intended
application". **Fail-closed behaviour is correct and complete; containment is not.** The
tail most likely landed in the newly focused Freebuff input; that is an inference from the
recorded foreground, not a readback (LIM-1: NovaControl cannot read an arbitrary
application's field, and the run deliberately does not).

Recorded as **DF-6-L1** — a structural limitation of chord injection into a foreground
window that vanishes mid-chord. Evaluated option-by-option, with the Windows guarantees,
in §O; none is implemented.

1. **Harness-side:** keep a second disposable window ready to take focus, so the orphaned
   tail lands in disposable hands and assertion 7 becomes provable. This is the cheapest
   way to re-accept the scenario and changes no product behaviour.
2. **Product-side:** inject per-window (e.g. `PostMessage` to the bound HWND) instead of a
   global chord — changes what "real input" means and needs its own acceptance.
3. **Accept and document:** treat the tail as an accepted residual risk, with the
   fail-closed guarantee as the mitigation.

### N.4 Correction: the 2026-11-26 "16/16 PASS" for DF-6 operator-loop is retracted

The earlier evidence (`midaction_crash_evidence_20261126T235403.json`) records the *same*
unrelated foreground — observed `[15729620, 4192]` — and a recovery `strategy: retry`.
Under this pass's criteria that run fails assertion 6/7 on the same ground, and fails the
LIM-2 live requirement that uncertain delivery must never select `retry`. §J.5 read the
identity mismatch as benign ("the foreground after its death is some other window") and
never asserted it; §J.7, §M.10, `PHASE29_REPORT` §16 and `STATUS.md` inherited that PASS.
**Those PASS rows are retracted.** What that run did validly prove is unchanged: the target
receives input, dies mid-delivery, and the run fails honestly with a durable record.

Today's run also supplies the missing **live confirmation of LIM-2**: the same scenario now
classifies delivery as `uncertain` and chooses `recheck`, with one typing attempt and no
replay — the duplicate-input risk the earlier `retry` selection carried is gone on the
current tree.

### N.5 Harness defect found by this run: M2.8 passed by finding nothing

A failed typing record stores `output = {status, approval_id}` only; the identity pair
survives solely in `detail`. M2.8 read `output["verification"]["expected_identity"]`, found
nothing, and reported PASS. The check is now strengthened (not weakened): it also parses the
message pair and **fails when an input action has no identity evidence at all**. It was not
re-run (no rerun authorized), so this run's M2.8 PASS is **not** evidence of anything; a
corrected M2.8 reports **FAIL** for this scenario, consistent with §N.2. It was then
split for precision into M2.8a (the objective bound an identity), M2.8b (every attempted
input action recorded a pair) and M2.8c (the pair names the bound target), so the harness now
emits **21** checks; the 19/19 above describes the harness as it ran and is not retroactively
comparable.

### N.6 Checks run after the live test

| Check | Result |
| --- | --- |
| `tests/test_lim2_recovery.py`, `test_d12_targeting.py`, `test_agentcore.py`, `test_phase29_computer.py`, `test_phase30_operations.py`, `test_phase31_skills.py` | **200 passed**, exit 0 |
| `mypy src/novacontrol/agentcore src/novacontrol/desktop` | Success, 23 source files |
| `git diff --check` | clean |
| `py_compile tmp/group1/verify_midaction_crash.py` | OK (after the M2.8 strengthening) |

### N.7 Verdict

**DF-6 operator-loop: PARTIAL.** The crash itself, the honest failure, the durable record,
the single attempt, the no-replay recovery and the token refusal all pass on the current
tree; the two identity-containment assertions do not. **Group 1: NOT CLOSED.** Outstanding:
DF-6-L1 (operator decision), DF-1…DF-5 and DF-7 (environmental, separately authorized and
unexecuted), LIM-1 (documented limitation), DF-8 (decided under interpretation A). No
further live desktop or environmental test was executed, and none was rerun.

## O. DF-6-L1 — input containment investigation (2026-11-27)

Offline architecture investigation. **No live desktop interaction, no input, no harness run.**
Evidence from the failed 2026-11-27 run is preserved unchanged
(`tmp/group1/midaction_crash_evidence_20261127T010222.json`,
sha256 `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34`).

### O.1 Incident, traced to the event

The objective was `type the phrase hello world into the NovaGroup1MidAction window`. On
Windows, `TYPE_TEXT` is not keystroke typing — it stages the exact text on the clipboard and
sends one `SendKeys` chord:

```python
# desktop/controller.py :: _stage_and_paste_windows
script = (
    f"Set-Clipboard -Value {quoted}; "
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.SendKeys]::SendWait('^v')"
)
await self._powershell(script, timeout=10)
```

Observed sequence, from the preserved evidence:

| # | Event | Destination | Evidence |
| --- | --- | --- | --- |
| 1 | Ctrl-down (first event of `^v`) | bound target, hwnd 7670084 / pid 33624 | probe marker `event_type=2`, `keysym=Control_L`, `pid=33624` |
| 2 | target process exits (3) inside its own key handler | — | probe `os._exit(3)`; `probe_exit = 3` |
| 3 | V-down, V-up, Ctrl-up still in the injected stream | the new foreground, pid 4192 (Freebuff) | `_verify_type_windows` read foreground `[15729620, 4192]` immediately after the paste |
| 4 | verification | `identity_match = false` → `VerificationError` | record detail: expected `[7670084, 33624]`, observed `[15729620, 4192]` |

The controller behaved exactly as designed. Its identity check runs **after** the paste
(`_type_text` → `_stage_and_paste_windows` → `_verify_type_windows`), so it can refuse
success but cannot recall events 3. Its final verdict says so plainly: the target is
`absent` and *"The input cannot be attributed to the intended application."*

### O.2 Root cause

Three properties combine; any one of them removed would prevent this class of leak:

1. **The destination is chosen per event, at dispatch time, by the input system — not by the
   caller.** Synthetic keyboard events are inserted into the session's input stream, and the
   system delivers each to whichever window holds keyboard focus *when that event is
   dispatched*. `SendKeys` has no target parameter at all: it sends to "the active
   application" (Microsoft). `SendInput` likewise "inserts the events… serially into the
   keyboard or mouse input stream"; its target is the foreground window's focus.
2. **Nothing can be recalled.** Neither `SendInput` nor `SendKeys` exposes cancellation,
   rollback or a commit point. `SendKeys.Flush()` waits for the queue; it does not empty it
   selectively. The documented contract is insertion, not delivery to an address.
3. **The controller's identity gate is post-send.** For `type`, D12 identity is verified from
   the foreground *after* the paste (`_verify_type_windows`), and the failure is raised
   afterwards. (Coordinate input is different: `_mouse_click` calls
   `validate_input_target(..., require_foreground=True)` before the click, and the
   `InputGuard` checks its stop flag before each press — but `_type_text` uses neither.)

So DF-6-L1 is not a D12 regression and not a LIM-2 regression: the destination was correct
when input started, the delivery correctly became `uncertain`, and recovery correctly chose
`recheck`. It is a **containment** gap: the tail of a multi-event operation outlives the
identity that justified it.

### O.3 What Windows actually guarantees

Grounded in Microsoft documentation and one independent measured report:

| Question | Documented answer | Consequence for NovaControl |
| --- | --- | --- |
| Which window receives `SendKeys`? | "the active application" — no target parameter | cannot address a window |
| Which window receives `SendInput`? | the foreground window's keyboard focus; events are inserted "serially" and never interleaved with other input | destination fixed per event, at dispatch |
| Is a `SendInput` array atomic? | no — non-interleaving only; each event still dispatches, and there is no rollback/cancel | batching cannot contain |
| What if focus changes mid-sequence? | the remaining events go to the new focus (the classic modal-dialog steal) | exactly the observed failure |
| Can injection be blocked silently? | yes: UIPI — "permitted to inject input only into applications… at an equal or lesser integrity level"; neither `GetLastError` nor the return value reports it | elevated targets fail invisibly; must be refused up front |
| Can a hidden desktop receive it? | no: `SendInput` first checks the calling thread is on the **input desktop**, else fails with `ERROR_ACCESS_DENIED`; only `SwitchDesktop` makes a created desktop the input desktop (which hides the user's) | "hidden desktop worker" is *not* an input-isolation option |
| Can `PostMessage`/`SendMessage` replace it? | no — posted `WM_KEYDOWN` bypasses the input queue, `WH_KEYBOARD`, and `GetKeyState`/Raw Input consumers (Microsoft's own guidance: use UI Automation, or `SendInput`) | option A cannot be a general replacement |
| Is there a supported non-input path? | yes — UI Automation patterns (`ValuePattern.SetValue`, `InvokePattern`, …) act on a specific element without synthetic input | the only containment-by-construction path in the user's session |

Microsoft's own automation CLI documents the same guard NovaControl lacks pre-send:
"`send-input` verifies the target window is actually in the foreground immediately before
injecting and fails (`foreground_not_target`) rather than typing into the wrong window".

### O.4 Approach-by-approach evaluation

**A. Per-window input delivery (`PostMessage`/`SendMessage` `WM_KEYDOWN`).** Reliable
HWND/PID addressing and no foreground dependence, but not real input: apps that read
`GetKeyState`/`GetKeyboardState`, use `WH_KEYBOARD` hooks or Raw Input ignore it; Chromium,
Electron, games, consoles and custom-drawn editors are the common failures. Key state and
modifiers must be simulated by hand (`lParam` bits, scan codes, `GetKeyState` never changes),
and IME/text composition does not work, so a "paste" becomes per-character messages that
differ from the real path. Also blocked by UIPI (access denied) to higher-integrity windows.
Delivery is asynchronous per message with no read-back, so success is unverifiable. Verdict:
**not a replacement**; usable only as an explicitly-opted per-app adapter — which is the
same narrow, UIA-style scope with worse semantics.

**B. Isolated desktop / session / VM.** Two very different things:
* *Hidden desktop in `WinSta0`*: `SendInput` fails (not the input desktop), so the current
  controller cannot drive a window there at all. Making it the input desktop via
  `SwitchDesktop` hides the user's own desktop for the duration — and a created desktop in a
  non-interactive window station cannot be switched to at all. Not viable as implemented.
* *Separate Windows session (second user/RDP loopback) or a VM*: real, fully functional
  synthetic input inside the target session, and the user's session is untouched by
  construction. This is the only option that both works and **guarantees** the user's
  desktop receives nothing. Cost: an agent inside that session, a screen/observation
  transport, session lifecycle and credentials, GPU/virtualisation overhead, and a new device
  adapter under `computer/devices.py`. Emergency stop must reach across the transport;
  durable state stays in the host. Complexity is a milestone, not a patch.

**C. Dedicated decoy focus window.** Windows does not guarantee which window becomes
foreground when the target dies: it is the next window in activation/Z-order, which can be
an unrelated app or a shell surface, and a modal dialog, UAC prompt or the secure desktop can
take it regardless of `-topmost`. A decoy can only bias the outcome, and no supported API
promises the bias. Per the brief's own rule — not sufficient unless the guarantee can be
demonstrated — **it cannot be**, so a decoy may be used *in a harness* to keep an experiment's
fallout disposable, never as the product's containment claim.

**D. Batching / cancellation.** `SendInput`'s array is non-interleaved but not transactional:
each event dispatches, and a destroyed target does not stop the rest (measured here). There is
no documented cancellation of inserted events; `SendKeys.Flush` only waits. Verdict: **no
guarantee available**; the only real reduction is sending fewer events with a gate between
them, which is A-hardening, not atomicity.

**E. Existing repository capabilities.** Nothing in the tree provides input isolation: no
`CreateDesktop`/`SwitchDesktop`, no window-station handling, no session transport
(`grep` over `src/novacontrol` finds none). What *does* exist and helps: `InputGuard`
(records every press and guarantees the matching release, and checks its stop flag **between
edges**), `validate_input_target(require_foreground=True)` for coordinate input, the
per-edge worker path (`keybd_event` per command, so a shortcut *can* be gated between
modifier and key), D12's bound `(handle, pid)`, and LIM-2's delivery classification. The
`_stage_and_paste_windows` chord is the one path that bypasses all of those gates.

### O.5 Recommended architecture

No available mechanism guarantees containment for synthetic keyboard input into arbitrary
desktop applications inside the user's interactive session. The recommendation is therefore
two-part, and it **narrows the supported operating scope** rather than claiming universal
safety:

1. **Guaranteed-containment profiles (the only ones DF-6 may PASS on):**
   * **UIA profile.** Text and activation through UI Automation patterns addressed at the
     element (`ValuePattern.SetValue`, `InvokePattern`, `SelectionItemPattern`,
     `TogglePattern`). No synthetic keyboard events are generated at all, so there is
     nothing that can leak to another window — containment is structural, not verified after
     the fact. Coverage is the honest limit: custom-drawn surfaces, games, canvas apps and
     some Electron views do not implement the patterns.
   * **Isolated-session profile.** `ComputerOperator` driving a dedicated Windows session or
     VM through a device adapter. The user's desktop cannot receive the input because it is a
     different session. This is the only profile that covers arbitrary applications, and it is
     a separate milestone (§O.7 decision 1).
2. **Best-effort hardening for the current interactive-session keyboard path** (ship only
   with the label, never as a PASS): gate identity immediately before the first event, split
   the chord into individually gated events with a revalidation between each, abort the
   remaining events on any identity change, refuse higher-integrity targets up front (UIPI is
   silent), and record `containment: best_effort` plus the number of events dispatched after
   the last confirmed identity check. This reduces the exposure from a four-event chord to at
   most the single event in flight, and turns today's silent tail into a bounded, reported
   one — it does not eliminate it.

Ranking (safety / reliability / compatibility / complexity / cost):

| Rank | Approach | Prevents unrelated input? | Note |
| --- | --- | --- | --- |
| 1 | Isolated session or VM | **Yes**, by construction | highest complexity; only path for arbitrary apps |
| 2 | UIA-pattern input | **Yes**, by construction | coverage gaps; no keyboard events |
| 3 | A + pre-send gate + per-edge gating | No — narrows to ≤1 event | cheap; must be labelled best-effort |
| 4 | Decoy focus window | No — unprovable bias | harness hygiene only |
| 5 | Hidden desktop (no switch) | Not applicable | `SendInput` fails: not the input desktop |
| 6 | `PostMessage` keyboard | No — and often ignored entirely | not real input |
| 7 | Batch/cancel | No | no cancellation exists |

### O.6 Acceptance test specification (offline; nothing executed)

All twelve scenarios assert on a **per-event dispatch ledger**, not on the controller's
`success`/`detail`: a record of `(event, destination hwnd/pid, gated?)` produced by a
simulated foreground model that the test mutates (kill target, steal focus, retitle, replace).
"A controller reporting failure" is explicitly not accepted as proof.

Harness shape: extend the existing `ProbingRunner` pattern (`tests/test_desktop.py`) with a
`DispatchSimulator` that owns (a) the bound identity, (b) the current foreground identity,
(c) an event ledger, and (d) scripted hazards ("the target dies on event N", "an unrelated
window takes the foreground after event N"). The runner's `_stage_and_paste_windows` and
`InputGuard` edges are recorded with the *current* simulated foreground at the moment each
event would be dispatched. This tests decision logic against the documented semantics; it is
not evidence about the OS, which is why the live gate stays separate.

| # | Scenario | Scripted hazard | Required observation (both profiles unless noted) |
| --- | --- | --- | --- |
| 1 | Normal delivery | none | every event → bound target; ledger has zero foreign destinations |
| 2 | Target destroyed before input | kill before the first event | zero events dispatched; run fails with a refusal, not a delivery |
| 3 | Target destroyed during the first event | kill on event 1 | no further event dispatched; zero foreign destinations |
| 4 | Destroyed between modifier and key | kill after Ctrl-down | the key event is never dispatched; zero foreign destinations |
| 5 | Destroyed between key-down and key-up | kill after key-down | key-up is not dispatched to a foreign window; a withheld key-up is recorded as a stuck-key risk |
| 6 | Foreground change during input | unrelated window takes focus after event 1 | input aborts; zero foreign destinations |
| 7 | Identity change (same title, new hwnd/pid) | replace window mid-run | refused before the first event of the new identity |
| 8 | Replacement window with identical title | different hwnd/pid, same title | refused: identity, not title, is the key; zero events |
| 9 | Unrelated foreground application | foreground ≠ bound target before send | refused with `foreground_not_target`; zero events |
| 10 | Interrupted delivery (cancel) | `stop_input()` between events | no event after the stop; held edges released only while the target is bound, otherwise recorded |
| 11 | Recovery after uncertain delivery | delivery `uncertain` | no replay: ledger shows exactly one delivery attempt (LIM-2) |
| 12 | Emergency stop | `emergency_stop()` mid-run | no subsequent event to any window; every held key released or recorded as unreleased |

The corrected M2.8a/b/c implement, for one run, what the ledger asserts for a whole suite:
identity evidence is mandatory, and it is compared with the objective-bound identity rather
than with itself.

Two gate profiles per row: **UIA/session** (requirement: zero foreign events — this is the
DF-6 PASS gate) and **interactive keyboard** (today expected to show ≤1 foreign event in
scenarios 3–6; recorded, not passed). Note that `desktop-only` scenarios 7–9 are pure pre-send
refusals and can be proven in the interactive profile too.

### O.7 Remaining limitations, stated rather than hidden

* No cancellation of an event already in the input stream (documented; measured here).
* UIPI: an unelevated host cannot drive an elevated or AppContainer target; the drop is
  silent, so the only safe response is a pre-send integrity refusal (not implemented).
* UIA coverage: custom-drawn controls, games, many canvas UIs and some Electron views expose
  no writable pattern; those fall back to the keyboard path and inherit DF-6-L1.
* Clipboard residue: the staged text remains on the session clipboard after a failed paste —
  a privacy residue, not an input leak (not addressed; a restore would be a separate change).
* Hidden desktops cannot receive input; `SwitchDesktop` hides the user's desktop, so "isolate
  without a session" does not exist.
* The acceptance tests above are a specification: **none has been written or run**, and no
  production source was changed in this investigation.

### O.8 Operator decisions required

1. **Scope decision:** adopt UIA-first input plus isolated-session execution as the two
   guaranteed profiles (DF-6 can PASS only there), and label interactive keyboard injection
   `containment: best_effort` — or commission the session/VM adapter milestone so arbitrary
   apps can be covered honestly.
2. **Authorise the hardening patch** (pre-send foreground identity gate for `TYPE_TEXT`,
   per-edge gating of the `^v` chord, pre-send integrity-level refusal, `containment` +
   `events_after_last_identity_check` in the record). Not implemented.
3. **Decide the decoy's role:** harness-only hygiene, never a product claim.
4. **Accept clipboard residue** as a documented limitation, or authorise a clipboard restore.
5. **Keep DF-6 PARTIAL and Group 1 open** until a guaranteed profile is implemented and
   demonstrated with a dispatch ledger plus a live run inside that profile.

### O.9 Containment implemented (2026-11-27, offline — no input, no harness run)

§O.5's recommendation is now code rather than a proposal. The chord is gone and the
profiles are explicit.

| Change | Where |
| --- | --- |
| Paste chord split into individually gated edges (`ctrl_down, v_down, v_up, ctrl_up`), identity re-checked before **every** edge, remainder withheld on mismatch | `desktop/containment.py::GatedChord`, `controller._stage_and_paste_windows` |
| Pre-send foreground gate, `containment: best_effort`, `bound_identity`, `destination_identity`, `events_after_last_identity_check`, `stuck_keys`, `unbound_releases`, `residual_risk` on every attempt | `controller._type_text` |
| Failure carries its record: `PasteContainmentError` (a `VerificationError` **and** a `ContainmentError`) → `ActionOutcome.output` | `controller`, `orchestrator._dispatch` |
| UIPI preflight: `target_higher` refused before injection, `unknown` refused by the guaranteed profiles and recorded in best-effort | `containment.compare_integrity`, `controller._integrity_verdict` |
| Clipboard snapshot/restore, concurrency-safe, residue reported | `desktop/clipboard.py`, `controller._restore_clipboard` |
| UIA profile: element-scoped patterns, ownership/ambiguity/staleness/availability checks, `keyboard_events: 0`, no fallback | `desktop/uia.py` |
| Isolated-session boundary with lifecycle, identity, dispatch accounting, emergency stop, teardown and audit | `computer/session.py` |
| Twelve-scenario acceptance, UIA, session, clipboard and integrity suites | `tests/test_df6_dispatch_ledger.py` (43 tests) |

Status per profile:

| Profile | Implemented | Provenance | May claim a DF-6 PASS |
| --- | --- | --- | --- |
| `best_effort` keyboard | yes | 15 ledger scenarios + integrity + clipboard suites, all offline | **no** |
| `uia` | yes (adapter + comtypes transport, `verified_live = False`) | 11 adapter tests with a simulated backend | only after a live run |
| `isolated_session` | boundary + feasibility | 7 boundary tests | **blocked** — `no_session_transport` |

What the twelve scenarios showed, on an independent per-event ledger (destination read from
the simulator at dispatch time, never from the controller's message): normal delivery reaches
the bound window and nothing else; a kill before the first event sends nothing; a kill after
the modifier sends no paste key; a kill after the key-down withholds the key-up and records
`stuck_keys = ["v"]`; a stolen foreground, a same-title replacement and an unrelated
foreground are all refused before any input event; a stop mid-chord leaves no input event
after it; an uncertain delivery is re-checked, never replayed (one ledger delivery); and an
emergency stop sends nothing. The one residual the profile cannot close — a focus change
between the final check and its event — is reproduced and bounded at exactly one input
event, and every record reports it.

Decisions 1, 2, 3 and 5 of §O.8 are answered by this implementation: UIA and the isolated
session are the guaranteed profiles, interactive keyboard injection is labelled
`best_effort`; the hardening patch is authorised and implemented; no production code uses a
decoy window; and DF-6 stays PARTIAL. Decision 4 (clipboard residue) is answered by the
restore: the staged text no longer survives a failed paste unless another application took
the clipboard first, in which case their value is kept and the residue is recorded.

Remaining, and each is an authorisation or platform dependency rather than code:
(a) live verification of the UIA transport against a real control; (b) a session/VM
transport (`DF6_WINDOWS_SESSION_ARCHITECTURE.md` §3–§4); (c) the separately authorised live
run inside a guaranteed profile. Verification: 43 new tests, **399 passed** across the
DF-6/desktop/DF-6-input/D12/LIM-2/AgentCore/Phase 29/30/31 suites, mypy clean on six modules, ruff clean on the new
files, `git diff --check` clean. The preserved 2026-11-27 evidence is unchanged
(sha256 `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34`) and the harness
was not executed.
