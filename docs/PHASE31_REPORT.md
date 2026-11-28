# Phase 31 — Transfer Learning & General-Purpose Skills

**Verdict: PARTIAL (phase open)**
**Date:** 2026-11-13
**Branch/HEAD:** `main` / `afa5ff6`, uncommitted worktree. No commit, push or deployment was made.

## 1. Objective and scope

Phase 31 turns isolated task automation into a reusable skill library:
NovaControl must acquire a procedure from genuine execution evidence, retrieve
it for a related task, adapt it to a different context, compose several skills,
execute them through the existing pipeline and verify what actually happened.

The original phase text supplied in the request is the canonical specification;
the repository contains no separate Phase 31 document and `ROADMAP.md` stops at
Phase 20. No roadmap entry was edited and no Phase 32 work was started.

## 2. Dependency assessment

- **Phase 30 is still PARTIAL**: OS-level restart and a multi-hour soak remain
  unverified. Phase 31's Phase 30 integration therefore claims only an
  application-level restart test, not an OS restart.
- **Phase 29 is still PARTIAL**: physical/environmental verification remains
  outstanding. Phase 31 reuses the same desktop/browser controllers and
  token-gated plan/execute path; it does not inherit Phase 29's verdict, and
  `ComputerOperator` is still not a skill step type.
- **Phase 27 and Phase 28 are implemented**, and their relevant interfaces are
  used directly. No deferral was requested or invented, and no Phase 30/29 gap
  is silently treated as closed.

## 3. What was built and repaired

`src/novacontrol/skills/` was extended, not duplicated. The original
`FunctionSkill` / `SkillRegistry` surface remains.

- **Domain models** (`models.py`): `Skill`, `SkillStep`, `SkillInput`,
  `SkillReliability`, `SkillEvidence`, `SkillMatch`, `SkillExecution` (including
  persisted `outputs`), `Compatibility`, `AdaptationResult`,
  `CompositionResult`, `SkillObservation` and lifecycle/category/step-kind
  enums. All are frozen value objects with explicit serialisation; the Phase 31
  types coexist with the old ones.
- **Catalog** (`catalog.py`): immutable versions, duplicate detection by name +
  procedure signature, inverted token index, ranked retrieval, lifecycle
  transitions, promote/deprecate/rollback and tolerant restore. A duplicate
  rejection no longer leaves a phantom empty entry; the active version is
  chosen from validated versions only; deprecation never activates an
  unvalidated adaptation; rollback accepts only a `VALIDATED` target.
- **Acquisition** (`acquisition.py`): two independent verified observations (or
  a demonstration plus one verified execution), procedure-shape matching,
  placeholder templating, input/tool/permission extraction and provenance.
- **Transfer** (`transfer.py`): environment/tool/input compatibility, explicit
  one-pass target replacement, environment and tool rewriting, recomputed input
  contract and tool set, `ADAPTED` version, original preserved. Adaptation
  requires a `VALIDATED` source and records refusal evidence. Environment
  transfer now accepts only declared environments, not an arbitrary
  `target_map` entry such as `plan9`.
- **Composition** (`composition.py`): executable lifecycle, dependencies,
  cycles, missing inputs, duplicate outputs, tool availability and permissions;
  returns an ordered plan. Inputs produced by an earlier composed skill satisfy
  a downstream declared input, and declared outputs are bound into the next
  skill.
- **Service** (`service.py`): evidence, validation, reliability, retrieval
  re-ranking from Phase 27, execution routing through an injected executor,
  permission checks, per-step environment checks, checkpoints, resume,
  persistence and events. It owns no execution engine. Required inputs are
  truly required: a `SkillInput.example` is documentation, not a default value,
  so a missing required input blocks execution instead of silently running the
  example. Resume persists only results from verified completed steps.
- **Application wiring** (`application.py`): `SkillService` construction after
  operations, `skills` state-store namespace, `status()["skill_library"]`,
  public skill methods including `promote_skill` and
  `execute_composed_skills`, `_execute_skill_step` routing to the existing tool,
  request, desktop, browser and agent pipelines, and Phase 30
  `agent="skill:<id>"` dispatch with checkpoint resume. Skill outputs are
  extracted from tool/research/developer outcomes only for declared output
  names.
- **API**: 15 authenticated `/skills*` routes in `api/app.py`, 15 `ApiSurface`
  rows, 15 `route_consumers` declarations; `docs/API.md` regenerated at
  **434 routes**. `POST /skills/promote` and `POST /skills/compose/execute` are
  new. `/skills/execute`, `/skills/resume` and `/skills/compose/execute` no
  longer accept a client-supplied `approved: true`; they return HTTP 403 and
  require the objective/task approval path. Deprecation accepts an optional
  version and returns 422 for an invalid one.
- **Documentation**: this report and [SKILLS.md](SKILLS.md).

## 4. Architecture and integration evidence

| Layer | Reused | Evidence |
|---|---|---|
| Permissions | `PermissionManager` | scopes checked before a run and per step; no manager → blocked; confirmation → `waiting_approval` |
| Persistence | `JsonStateStore` | catalog/evidence/executions/failures restored across a new `NovaControlApplication`; a persistence failure is reported instead of fabricated as an execution failure |
| Events | `EventBus` via application seam | `skills.skill_acquired`, `.validated`, `.adapted`, `.executed`, `.deprecated`, `.rolled_back` |
| Tool execution | `ToolExecutor` | tool steps pass schema validation, approval and normalisation |
| Request execution | `handle_request` | request/verify steps traverse intent → decision → plan → tool → permission → verification |
| Desktop/browser | plan + single-use token execution | real Control Panel→Character Map transfer in Scenario C |
| Phase 27 | `ContinualLearningEngine.recommend` | validated tool preferences re-rank retrieval; avoid-tool evidence is named and discounted |
| Phase 28 | provenance + centralized versioning/permissions | `origin_agent`, versioned adaptations, no agent-local permission bypass; no in-pipeline skill tool yet |
| Phase 29 | desktop/browser controllers | same action execution and per-action verification path; `ComputerOperator` itself is not a step type |
| Phase 30 | objectives service + executor | `agent="skill:<id>"`, metadata inputs/version/environment, approval parking and checkpoint resume |

## 5. Requirement-by-requirement acceptance matrix

| ID | Original requirement | Implementation evidence | Verification evidence | Verdict |
|---|---|---|---|---|
| 1 | Acquire reusable skills from genuine experience | `acquisition.py`; two verified observations, demonstration requires execution | Live A (2 real tool runs, one-run refusal); unit tests | PASS |
| 2 | Structured definitions and provenance | `Skill` contract; observation/agent/adaptation provenance | Live A/B; persistence test; detail API | PASS |
| 3 | Retrieve relevant skills for new tasks | token index, relevance + reliability + compatibility ranking | Live B; retrieval unit tests; HTTP search test | PASS |
| 4 | Adapt skills to compatible new tasks | `transfer.py`; explicit changes; input/tool/environment rewrite; validated source required | Live B (machine_facts→installed_applications); refusal tests | PASS |
| 5 | Demonstrate cross-task transfer | live A→B and desktop Control Panel→Character Map | 8 A checks, 6 B checks, 2 C checks | PASS |
| 6 | Cross-environment transfer where supported | declared environments, refusal with missing capability; app target map | E (plan9 refused); C (same-OS cross-application); **cross-OS and browser transfer not run** | PARTIAL |
| 7 | Compose multiple validated skills | `composition.py`; dependency ordering, conflicts, output/input binding, stop-on-failure; `POST /skills/compose/execute` | Live D (2 skills, dependency order, verified results); output-binding and HTTP tests | PASS |
| 8 | Independently validate adapted skills | `ADAPTED` starts with empty reliability; validation needs its own verified executions | Adaptation/validation unit tests; B shows source preserved; live adapted run recorded | PASS |
| 9 | Versioning and rollback | catalog versions, parent version, promote/deprecate/rollback; only validated versions become active | lifecycle/promote/rollback unit tests; restart preserves validated version; API routes | PASS |
| 10 | Reliability from actual evidence | `SkillReliability` from `SkillEvidence`; no model confidence | A3/A8; adaptation B2; failure evidence E4 | PASS |
| 11 | Phase 27 continual-learning integration | retrieval calls `recommend`; prefer/avoid tools re-rank; strategy/evidence named | `test_validated_learning_preference_reranks_and_discourages`; existing annotation test | PARTIAL — selection is improved, but learning does not yet detect/deprecate outdated skills or feed adaptation automatically |
| 12 | Phase 28 multi-agent integration | `origin_agent` provenance; shared catalog; versioned adaptation; centralized permissions | unit provenance/version tests; app-level execution | PARTIAL — no in-pipeline “use skill” tool for collaboration agents |
| 13 | Phase 29 computer-operation integration | desktop/browser skill steps use the real controllers and token path | Live C real application transfer; adopted interpretation A (Group 1 report §M) | **PASS** — adopted definition: the existing controllers and token path; no `COMPUTER` step kind |
| 14 | Phase 30 persistent-objective integration | `agent="skill:<id>"`; metadata inputs/version/environment; resume | Live F: paused objective, new app instance, skill task completed; F6–F8 checkpoint | PASS |
| 15 | Safety and permissions | per-skill and per-step checks; tool/device gates; approval parking; API cannot self-approve | unit permission/approval tests; live no bypass; `blocked` on no manager; HTTP 403 tests | PASS |
| 16 | Real application scenarios | A–F live script against the real application | **32 PASS / 0 FAIL / 0 unverified**, one informational latency measurement | PASS |
| 17 | Tests, regression, type checks | phase, HTTP, API contract, live and targeted regression | 31 phase tests; 2 HTTP tests; 11 API contract tests / 434 subtests; 171 targeted regression tests / 552 subtests (202 / 552 with the phase suite); mypy skills clean; app mypy one pre-existing error | PASS |
| 18 | Documentation and report | this report, SKILLS.md, STATUS, log, README, AGENTS, API | `generate_api_reference.py --check` in sync at 434 routes; `git diff --check` clean | PASS |

Supporting requirements:

| Area | Evidence | Verdict |
|---|---|---|
| User-demonstrated acquisition | `demonstrated=True` still requires one verified execution | PASS |
| Duplicate/conflict detection | name + signature duplicate refusal; composition output conflicts | PASS |
| Adapted plan validation | compatibility + explicit changes + independent evidence | PASS |
| Partial workflow failure | `compose_and_execute` stops at first non-completed step | PASS |
| Pause/resume without duplicate side effects | checkpoint skips completed steps; resume result list is filtered to completed indexes; F8 | PASS |
| Permission escalation prevention | no manager blocks; every scope checked; device token remains; client approval flag refused | PASS |
| External/imported instructions | treated as observations, not executed during acquisition | PASS (design + tests) |
| Storage efficiency | token index; no whole-library scan per retrieval | PASS for measured small catalog |
| Concurrent execution | independent execution ids/checkpoints; no shared mutable skill | PASS (unit-level; no load test) |
| Cross-OS support | not tested; unsupported environment is named | PARTIAL |

## 6. Live application verification

Script: `tmp/phase31/verify_phase31.py` → `tmp/phase31/evidence.json`.
Host: Windows (`win32`), Python 3.13.0, no model required. **33 reported
entries: 32 passed, 0 failed, 0 unverified, 1 informational latency measurement.**

- **A — acquire and reuse:** two real `machine_facts` tool executions produced
  two verified observations; acquisition created an `ACQUIRED` skill with 2
  trials; a one-observation attempt was rejected; validation succeeded; the
  validated skill executed through the app and reliability rose to 3 trials.
- **B — transfer:** adapted the machine-facts skill to the
  `installed_applications` tool with input `filter=e`; v2 is `ADAPTED` with zero
  trials and no borrowed reliability; v1 remained `VALIDATED`; the adapted tool
  executed and verified. The change log records tool, target, input-add and
  input-remove changes.
- **C — cross-application:** acquired “Open Control Panel” from **two real
  desktop executions** (each launch used a fresh single-use token; the window
  was closed between observations so the controller verified a new one),
  adapted it to “open character map”, executed with explicit approval through
  the same plan/token path, and the controller reported the window action
  completed and verified. The target is selected from an application with no
  live process so the launcher's *new-window* verification is honest; a
  previous Magnifier run had left a process alive and the launcher correctly
  refused to call a reused process a new window. That is a launcher limitation,
  not a transfer success, and the harness now avoids it rather than hiding it.
- **D — composition:** acquired/validated a second “Report capabilities” skill,
  added a real dependency on the machine-facts skill, composed, and executed
  both in dependency order with verified results. Composition output binding is
  also covered by unit tests.
- **E — failed adaptation:** adapting to `plan9` returned incompatible with the
  declared `windows` environment, left the original validated version
  untouched, blocked execution with the same reason, and recorded the failure.
- **F — persistent objective and checkpoint:** paused an objective before its
  skill task, built a second `NovaControlApplication` on the same data
  directory, restored the objective/task and validated skill, resumed and ran
  the skill to a verified completed task. A separate two-step skill was
  interrupted after its first verified step (`completed_steps == [0]`) and
  resumed; the first step was not repeated (`machine_facts` once,
  `capabilities` twice).

These are genuine executions of the build's registered tools and desktop
controller. No result was supplied by a canned fixture.

## 7. Performance and resources

From the final live evidence (small catalog, 6 stored skill versions):

| Metric | Result |
|---|---|
| Retrieval (3 matches) | 0.312 ms |
| Version lookup | 0.0015 ms |
| Adaptation | 14.983 ms |
| Stored state (`data` JSON) | 60,170 bytes |
| Process RSS | 89.2 MB |

No claim is made about a large library, concurrent load or memory growth; the
token index is the intended scaling mechanism but was not load-tested.

## 8. Exact test and check results

Run from the repository root with `PYTHONPATH=src` and
`NOVACONTROL_DISABLE_OLLAMA=1`.

| Check | Command | Result |
|---|---|---|
| Phase 31 suite | `python -m pytest tests/test_phase31_skills.py -q` | **31 passed** in 1.00 s |
| HTTP skill routes | `python -m pytest tests/test_web_api.py -q -k SkillApi` | **2 passed** |
| API contract and reference | `python -m pytest tests/test_api_reference_sync.py tests/test_api.py -q` | **11 passed, 434 subtests passed** |
| Live scenarios A–F | `python tmp/phase31/verify_phase31.py` | **32 passed, 0 failed, 0 unverified** (33 entries; one informational measurement) |
| Targeted regression | `python -m pytest tests/test_phase30_operations.py tests/test_web_api.py tests/test_api.py tests/test_web_platform.py -q` | **171 passed, 552 subtests passed** in 77.53 s |
| Combined phase + regression | phase suite plus the command above | **202 passed, 552 subtests passed** |
| Type check (new package + changed API/app) | `python -m mypy src/novacontrol/skills src/novacontrol/application.py src/novacontrol/api/app.py src/novacontrol/api/models.py` | 1 pre-existing error at `application.py:5667` (`VisionProvider.see(..., structured=True)`); **no Phase 31 error** |
| API docs | `python scripts/generate_api_reference.py --check` | in sync at **434 routes** |
| Whitespace | `git diff --check` | clean |
| Static JS | `node --check src/novacontrol/web/static/js/render-operations.js` | clean (unchanged by this phase) |

No mandatory test was skipped or removed. The full repository suite was not run
because the universal policy restricts phase work to targeted scope.

## 9. Safety and reliability verification

- Missing `PermissionManager` → `blocked`, with no step started.
- A declared sensitive scope with no approval → `waiting_approval`, zero steps.
- Approval → execution; denial → blocked; step-level confirmation parks the run
  after prior verified checkpoints.
- Tool steps go through the existing tool executor, including schema validation
  and approval.
- Desktop/browser steps require task approval and still consume the plan's
  single-use token; the skill cannot mint authority itself.
- `POST /skills/execute`, `/skills/resume` and `/skills/compose/execute` reject a
  client-supplied `approved` flag with HTTP 403; approval is only the
  objective/task path. This closes the API-level self-approval bypass found
  during verification.
- A required input with only an `example` is not silently defaulted; execution
  is blocked until the input is actually supplied.
- A step whose declared environment differs from the run environment is blocked
  before the executor is called, and the refusal is persisted as failure
  evidence.
- Acquisition refuses one success, unverified observations and mixed
  procedures; imported procedures are not executed while being learned.
- Adapted versions cannot inherit validation or reliability; they require a
  validated source; rollback preserves the replaced version's evidence and
  history.
- A failed adaptation is recorded and does not touch the validated original.
- Skill state is persisted after every lifecycle mutation and execution; a
  damaged snapshot is skipped rather than converted into a plausible empty one.

## 10. Known limitations and unresolved issues

1. **Cross-OS transfer is not implemented or verified.** Environment strings are
   declarations and checks; there is no macOS/Linux desktop adapter in this
   phase. Only Windows was exercised.
2. **Browser skill transfer was not live-run.** Desktop application transfer was.
3. **Phase 28 remains partly external.** Agents can contribute provenance and
   execute through the application methods, but collaboration pipelines have no
   in-pipeline skill-selection tool; a future phase must add one without creating
   a second registry.
4. **Phase 29 `ComputerOperator` is not a skill step type.** Desktop/browser
   controllers are the executed capability and were live-tested.
5. **Phase 27 informs selection but does not auto-deprecate or auto-adapt.**
   Avoid-tool evidence discounts retrieval and is named; lifecycle changes remain
   explicit and evidence-based.
6. **Retrieval is lexical.** No embedding/vector index or semantic model is used.
7. **No Skills UI.** The phase exposes API/CLI only; the route-consumer contract
   declares all 15 routes `no-render`.
8. **Performance is small-catalog only.** No concurrent execution load test and
   no multi-hour soak were performed.
9. **Phase 30's OS restart and Phase 29's environmental gaps remain open**; this
   phase does not close them.
10. **The desktop launcher verifies a NEW window.** If an application process is
    still alive with no visible window, a second launch is reported as unverified
    even when the process was reused. Scenario C selects a target with no live
    process and closes it between observations; a repeat run without cleanup is
    not a transfer failure but cannot claim a fresh-window success.

## 11. Five acceptance gates

| Gate | Verdict | Reason |
|---|---|---|
| 1 — Functional completeness | PARTIAL | Core acquisition/retrieval/adaptation/composition/execution works and is live-verified; cross-OS, browser transfer and in-pipeline agent selection are incomplete. |
| 2 — Integration completeness | PARTIAL | Tool/request/desktop/browser/Phase 30 paths are real and tested; Phase 28 in-pipeline integration and Phase 29 `ComputerOperator` step are not. |
| 3 — Testing completeness | PASS | 31 phase tests, 2 HTTP tests, 11 API contract tests / 434 subtests, 32 live checks, 171 targeted regression tests / 552 subtests (202 with the phase suite); no mandatory failure or skip. |
| 4 — Safety and reliability | PASS | Permission, approval, token, evidence, checkpoint, rollback, client-approval refusal and failure-evidence behavior verified. |
| 5 — Documentation and evidence | PASS | This report, SKILLS.md, STATUS/log/README/AGENTS updates, API reference in sync at 434 routes, exact commands recorded. |

## 12. Overall verdict

**PARTIAL — phase open.**

The central promise — acquire a skill once, adapt it to a genuinely different
task and reuse it — is implemented and demonstrated with real tool and desktop
executions, including a real Control Panel→Character Map transfer and a Phase 30
objective restored across a fresh application instance. Composition, independent
adapted validation, versioning, rollback, reliability from evidence and the
safety gates also work. Verification found and fixed several defects that the
first pass had documented too optimistically: examples were being used as
execution defaults, duplicate rejection left a phantom catalog entry, an
unvalidated adaptation could become active during deprecation, rollback accepted
unvalidated targets, composition did not bind outputs or recognize produced
inputs, and the HTTP execute endpoints accepted a client-supplied approval flag.

The phase cannot honestly be marked PASS because cross-OS transfer, browser
skill transfer, Phase 28's in-pipeline agent skill selection and Phase 29's
`ComputerOperator` step are not verified/implemented. Those are recorded as
PARTIAL, not converted into PASS by documentation.

## 13. Files created and modified

Created:

- `src/novacontrol/skills/catalog.py`
- `src/novacontrol/skills/acquisition.py`
- `src/novacontrol/skills/transfer.py`
- `src/novacontrol/skills/composition.py`
- `src/novacontrol/skills/service.py`
- `tests/test_phase31_skills.py`
- `tmp/phase31/verify_phase31.py`
- `tmp/phase31/evidence.json` (generated)
- `docs/SKILLS.md`
- `docs/PHASE31_REPORT.md`

Modified:

- `src/novacontrol/skills/models.py` (Phase 31 types added; legacy API preserved)
- `src/novacontrol/skills/__init__.py`
- `src/novacontrol/application.py`
- `src/novacontrol/api/app.py`
- `src/novacontrol/api/models.py`
- `src/novacontrol/api/route_consumers.py`
- `tests/test_web_api.py`
- `docs/API.md` (generated)
- `docs/STATUS.md`
- `docs/DEVELOPMENT_LOG.md`
- `README.md`
- `AGENTS.md`

No NovaForge or NovaMarket file was modified. Nothing was committed or pushed.

## 14. Group 1 audit addendum (2026-11-26)

Acceptance row 12 and limitation 3 are resolved. In-pipeline skill selection now
exists:

* `src/novacontrol/agents/pipeline.py` — `SKILL_TOOL`, the `SkillProvider`
  protocol and a `skills=` seam. Selection happens in the planner stage only
  when the specialist has **no plan of its own** (or the task names a skill),
  so an existing procedure is never replaced.
* `src/novacontrol/application.py` — `_PipelineSkillProvider` adapts the one
  `SkillService` (select, explicit resolve by id or name, execute).
* Refusals are honest: unvalidated/incompatible/under-specified skills are not
  selected; a skill awaiting approval is a DENIED step, not a success; a skill
  whose own verification failed cannot complete the run.
* Tests: 11 new (`InPipelineSkillSelectionTests`, `InPipelineSkillApplicationTests`).
  Phase 31 suite: **42 passed** (was 31).

Residual (unchanged): browser skill transfer was not live-run, cross-OS transfer
is not implemented, and `ComputerOperator` is still not a skill step kind.
Full record: `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` defect D1.

### Browser transfer and two further fixes (2026-11-26, second Group 1 pass)

* **Browser skill transfer is now live-verified.**
  `tmp/group1/verify_browser_transfer.py` acquired a browser skill from two real
  Chromium 156.0.8078.4 navigations of a local page, validated it from that
  evidence, had the real `verification-agent` select it (`skill.execute`), saw
  it park for approval, resumed it after approval, and counted **32/32** checks.
  The evidence is independent of Playwright: the local HTTP server logged each
  request with its User-Agent. Same-OS cross-application transfer was already
  verified; cross-OS transfer is now verified to be **refused** with a reason, so
  requirement #6 is PASS (cross-OS remains unsupported by design).
* **D10 fixed:** an explicitly named skill (`metadata={"skill": ...}`) was
  ignored by every shipped specialist, because the library was consulted only
  when the agent's plan was empty. The planner now consults the library first
  when a task names a skill; an unrequested plan is still never replaced.
* **D11 fixed:** a refused (approval-pending) skill step was verified as a
  failure and retried, which started a second execution and a second approval
  checkpoint for one step. A refusal is now recorded as
  `RECOVERY: SKIPPED — refused, not retried`.
* Phase 31 suite is now **45 tests**; the five phase suites are **221
  passed**; mypy and ruff are clean; the API reference is in sync at 666 routes.
  Evidence: `tmp/group1/browser_transfer_evidence.json`. Full record:
  `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` section H.

### `ComputerOperator` step kind — analysis, decision pending (2026-11-26)

Requirement 13 remains PARTIAL, and the analysis is now recorded rather than
assumed: the capability is **not** missing (skill steps `desktop`/`browser` drive
the same token-gated controllers, and Phase 30's `agent="skill:<id>"` reaches the
operator separately), the *definition* is. No canonical Phase 31 brief exists in
the repository (D2), so nothing states whether a skill must be able to invoke the
whole operator loop (risk classification, durable operation history, Phase 27
learning capture) or merely perform computer operations.

A smallest additive design was prepared (a `COMPUTER` step kind routed through
`plan_computer_task`/`execute_computer_task`, verified from the operator's own
final verdict) but **not implemented**: it needs a product decision, and it must
wait for D12 (wrong-window grounding), which it would inherit. Details:
`docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md` sections I.6 and I.5.

### DF-8 requirement resolution (2026-11-26, fifth Group 1 pass)

No source changed. The question "does #13 mean A or B" was investigated against the
repository rather than against the report's verdict:

* **The original Phase 31 brief does not exist in this repository** (D2): `ROADMAP.md`
  ends at Phase 20 and `docs/PHASES.md` at the Phase 15-era narrative. The only wording
  of #13 is the report's own row and `docs/SKILLS.md`.
* **Reading A — skills perform computer operations with the existing desktop/browser
  controllers — is implemented and live-verified** (`_execute_skill_step` →
  `plan_*_command` → single-use token → `execute_*_command` → per-action verification;
  Live C and the browser transfer run).
* **Reading B — a skill step invokes the whole `ComputerOperator` loop** is *not*
  implemented (`SkillStepKind` has no `computer`), is a plausible reading of the
  report's own "no `ComputerOperator` step" note, and would be a **new feature**, not a
  completion of an existing one.
* **Reading C — a distinct unspecified capability** has no evidence anywhere and must
  not be invented.

A `COMPUTER` kind would not duplicate controllers: it would call the same
`plan_computer_task`/`execute_computer_task` entry points, inheriting D12 targeting,
the operator's permission preflight, durable operation history, Phase 27 capture and
independent verification. The smallest design is one branch in `_execute_skill_step`
plus one outcome mapper, gated by `context["approved"]`, with no retry inside the step
and no direct `ComputerOperator.run` call. Full proposal, tests, live gate and
regression risks: Group 1 report §K.6.

**Decided 2026-11-26: interpretation A was adopted**, so #13 is PASS on the evidence
above and no `COMPUTER` kind was implemented (§15). Reading B would still require the
proposed implementation and its live acceptance. The original Phase 31 brief is absent
from the repository, so this is an adopted interpretation, not recovered wording.
`docs/SKILLS.md` and this report state the same definition.

## 15. DF-8 adopted: interpretation A (2026-11-26, sixth Group 1 pass)

The product decision is made: **criterion #13 is satisfied by skills performing computer
operations through the existing desktop and browser controllers** (interpretation A). No
`COMPUTER` step kind is implemented, and none is planned.

This is an **adopted interpretation, not a recovered requirement**. The original Phase 31
brief is not in this repository (`ROADMAP.md` ends at Phase 20, `docs/PHASES.md` at the
Phase 15-era narrative), so the only wording of #13 is this report's own acceptance row and
`docs/SKILLS.md` §10. No repository document requires a `COMPUTER` kind; the fifth-pass
analysis (§"DF-8 requirement resolution") described reading B as a new feature, not as an
existing deliverable.

Evidence that A is implemented — unchanged by this pass and re-checked by inspection:

* `_execute_skill_step` routes a `desktop`/`browser` skill step through
  `plan_*_command` → the single-use approval token → `execute_desktop_command` /
  `execute_browser_command`, i.e. the real controllers and the same token gate a computer
  operation uses;
* each step is authorized by the application `PermissionManager` and judged by the
  controller's per-action verification, not by "the call returned";
* desktop steps inherit D12 objective-bound targeting (handle/pid re-checked immediately
  before input), which is now PASS;
* live evidence: Live C real-application desktop transfer and the browser skill-transfer
  run (32/32, `tmp/group1/browser_transfer_evidence.json`);
* the Phase 29 `ComputerOperator` loop remains reachable separately through Phase 30's
  `agent="skill:<id>"` dispatch — reading B is a composition option, not a capability gap.

**Verdict: #13 is PASS under adopted interpretation A.** Row 13 of §12 records this.
`docs/SKILLS.md` states the same definition so the two documents cannot drift.
