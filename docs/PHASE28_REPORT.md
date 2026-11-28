# Phase 28 — Multi-Agent Collaboration: Implementation Report

**Verdict: PASS (closed under the Phase 29 focused-verification policy).** The
core collaboration capability is implemented and demonstrated: real specialists
are registered, objectives are assessed and decomposed, independent work runs
concurrently, dependent work is ordered, results are independently verified,
conflicts are surfaced or evidence-resolved, failures preserve completed
siblings, state persists, and Phase 27 captures the evidence. The resource and
provider controls are implemented and covered by focused tests. The original
report was PARTIAL only because it waited for a full-repository-suite run; the
Phase 29 acceptance policy explicitly replaces that with phase, modified-module,
integration and focused regression verification. The focused evidence below is
therefore the closure record. No full-suite pass is claimed.

## 1. The specification that was recovered

There is no canonical Phase 28 specification in the repository:

- `ROADMAP.md` stops at Phase 20.
- `docs/PHASES.md` stops at Phase 15.
- `docs/PHASE27_REPORT.md` records the same gap for Phase 27.

The Phase 28 brief supplied with the request was therefore treated as the
acceptance contract. No roadmap entry was invented and no Phase 29 work was
started.

## 2. What was implemented

- `src/novacontrol/agents/collaboration.py` — benefit assessment, deterministic
  decomposition, validated dependency graph, assignment, bounded concurrent
  scheduling, timeouts, retries/reassignment, cancellation/resume, context
  isolation, structured messages, conflict detection/resolution, verified
  aggregation, persistence and Phase 27 capture.
- `src/novacontrol/agents/collaboration_controls.py` — provider-capability
  declarations/preflight, the central token ledger, Retry-After-aware backoff,
  and hardware-derived concurrency. Pure and injectable; it is not a second
  scheduler, provider layer or execution engine.
- `src/novacontrol/agents/analysis.py` — `DataAnalysisAgent`, a real CSV
  specialist: SHA-256, row/column statistics, recomputable claims, root
  confinement and a row/byte ceiling.
- `src/novacontrol/agents/verification.py` — `VerificationAgent`, a real
  independent verifier: claim/citation checks, metric recomputation, file hashes
  and the project's own test suite.
- `AgentRole.DATA_ANALYSIS` and `AgentRole.VERIFICATION`.
- `CollaborationSettings` and the collaboration status/overview application
  surface.
- Seven typed collaboration events and eight `/collaboration/*` routes.
- `tests/test_multi_agent_collaboration.py` — 46 tests.

The existing `AgentRegistry`, `SpecialistPipeline`, planner interfaces, event
bus, message bus, state store, permission manager, Phase 27 engine and real
specialists are reused. There is no second planner, verifier, memory store or
execution engine.

## 3. Architectural integration

The collaboration layer is a coordinator above the Phase 12 specialist
pipeline. `CollaborationEngine` hands every task to a registered agent's
ordinary `handle_task`/`run_task`; the agent still traverses NLU → Context →
Decision → Planner → Tool Selection → Execution → Verification → Recovery. The
engine's own aggregation only consumes the run's verification result.

The application builds one `CollaborationEngine` from the live registry,
state store, shared message bus, application event bus, Phase 27 engine and an
authorized-pipeline factory. It publishes a status section, restores on boot,
saves on stop, and exposes plan/run/tasks/task/cancel/resume methods.

## 4. Delegation and correctness findings

Two defects were found by real probes and fixed:

1. **All same-role subtasks initially went to the first agent.** The conflict
   probe could not produce a conflict because the second specialist never ran.
   Assignment now rotates by subtask position and moves to the next candidate on
   retry. This is pinned by the conflict and retry tests.
2. **The coding subtask included the test clause.** `DeveloperAgent` routes
   “run the tests” before “implement”, so the coder ran the suite and wrote
   nothing while collaboration reported success. The coding plan now sends
   `apply the change: <change request>` to the coder and leaves the test clause
   to the independent verifier. The coding test asserts that the file exists and
   that the verifier is a different agent.

A third defect was found in the data specialist: “analyze every column in
data.csv” parsed `in data` as a column name. The column grammar now rejects
stop words in the bare form while still allowing a quoted column name. The test
suite pins the all-columns path.

## 4.1 Post-verification audit fixes

The final audit found and fixed defects the first pass did not cover:

1. **Resume reused an approval.** `resume()` now requires explicit
   `authorize=True` for a plan that writes or executes, and the application/API
   pass that fresh decision through. A non-idempotent unit cancelled after it
   started is reported failed rather than replayed.
2. **Unavailable agents were advertised as available and could be scheduled.**
   `SpecialistAgent.availability()` and each real specialist now report a live
   probe; the catalog publishes the reason and candidate selection excludes an
   unavailable agent.
3. **A non-COMPLETED response could be treated as work.** Any agent response
   whose status is not COMPLETED is now a failed subtask.
4. **Usage and backoff were insufficient.** A central `TokenLedger` now
   accounts every attempt per task, agent and subtask, enforces the optional
   `token_budget`, and blocks only work that has not started. Rate-limited
   idempotent failures honor `Retry-After` before capped exponential backoff.
5. **Provider and hardware context was absent.** Declared provider capabilities
   are preflighted before dispatch; live resource assessments lower the
   semaphore limit, and an unmeasured machine keeps the policy limit with an
   explicit `unmeasured` level rather than an invented reading.
6. **Authorized cloning failed without a clear result.** An agent without a
   clone hook now fails the subtask honestly instead of running on the shared
   pipeline.
7. **Flush reported retained tasks, not persisted tasks.** The count now
   matches the rows actually written.

## 5. Safety, permissions and verification

- A coding plan carries `requires_approval=True`; `run(authorize=False)` returns
  `DECLINED` and writes nothing.
- `authorize=True` builds a per-run authorized pipeline; the shared build
  pipeline is not mutated. Resuming a write/execute plan requires fresh
  authorization; the original run's approval is not reused.
- An unavailable agent is reported with its reason and is never scheduled; an
  agent response that is not COMPLETED is a failed subtask, not a success.
- The developer write remains destructive/non-reversible with its existing
  approval declaration; the engine does not retry writes.
- Destructive objectives are refused unless policy explicitly permits them.
- An agent's `COMPLETED` status never counts as verification. Aggregation
  requires every required subtask's run verification to pass.
- A verifier with nothing to check reports failure, not a vacuous pass.
- Context is declared per subtask; unrelated tasks do not see each other.

## 6. Persistence and recovery

Tasks are persisted in the shared `JsonStateStore` under `collaboration`,
schema `phase28.1`. Completed results survive restore. A record left `running`
becomes `INTERRUPTED`, never replayed. Non-idempotent interrupted units are
reported failed rather than retried. `resume()` explicitly re-queues cancelled
or retryable failed units while preserving completed work, and requires fresh
`authorize=True` for a plan that writes or executes.

## 7. Live scenarios

**Scenario A — collaborative research (live provider).** A real
`ExploreService` research run with an independent verification agent returned
`COMPLETED`, `2/2` required subtasks verified, 9 claims and 3 sources. This was a
live web run, not a scripted result.

**Scenario B — collaborative coding (disposable project).** A temporary project
with a real file write and a real pytest suite: the developer wrote
`src/example.py`; the separate verification agent ran the suite and passed. The
test asserts both agents and the on-disk content.

**Scenario C — parallel data analysis (real CSV).** Three independent column
analyses plus an independent recomputation: `COMPLETED`, `4/4` required
subtasks verified. The 20,000-row benchmark is in §8.

**Scenario D — agent failure.** A missing dataset fails one subtask while a
completed sibling remains verified; timeout, retry-to-another-agent, cancellation
and resume are separately pinned. The collaboration is `PARTIAL`, not a
fabricated success.

**Scenario E — conflicting results.** Two agents report `10.0` and `20.0` for
the same metric. Without independent evidence the conflict remains unresolved
and the task cannot complete. With a third agent's recomputation matching
`10.0`, the conflict is resolved and the task completes. Majority vote is never
used.

## 8. Benchmark

Environment: this Windows machine, Python 3.13, real 20,000-row, 3-column CSV,
`DataAnalysisAgent` + `VerificationAgent`, `max_concurrency=4`.

| Measurement | Result |
|---|---:|
| Single agent, all columns | 135.79 ms, verified |
| Parallel collaboration median wall (3 runs) | 572.43 ms |
| Engine wall (last run) | 568.50 ms |
| Summed subtask time | 845.32 ms |
| Measured parallelism | 1.49 |
| Serial collaboration median wall (3 runs) | 592.00 ms |
| Parallel vs serial wall speedup | 1.03× |
| Peak traced memory (one run) | 21,969 KiB |

Interpretation: independent units really do overlap (controlled sleep test
measures 1.5×+ parallelism), but this workload is CPU/IO-bound CSV parsing under
the GIL, so wall-clock gain over serial collaboration is only 1.03×. One agent
is substantially faster on this small dataset because collaboration deliberately
adds per-column agents and independent recomputation. The engine reports this
as overhead; no linear-speedup claim is made. Simple objectives are declined
before any agent runs, so they do not pay the coordination cost.

## 9. Tests and checks

- `tests/test_multi_agent_collaboration.py`: **61 passed**.
  Covers planner/decline, registry/catalog, real CSV analysis and verification,
  real coding write + pytest, parallel timing, dependency context, retry,
  reassignment, timeout, failure isolation, unavailable agents, non-completed
  responses, centralized token accounting and budget exhaustion,
  provider-capability preflight, Retry-After/exponential backoff,
  hardware-aware concurrency (ample/tight/critical/disabled/unmeasured),
  cancellation, authorized resume, clone failure, conflict
  surfacing/resolution, idempotency, persistence/restore, Phase 27 capture and
  HTTP routes.
- Config/application/API/continual/specialist regressions (`test_config`,
  `test_application`, `test_web_api`, `test_continual_learning`,
  `test_api_reference_sync`, `test_specialist_agents_verification`):
  **245 passed, 561 subtests**.
- Agent dependency group (`test_agents`, `test_specialist_agents`,
  `test_code_agent`, `test_agentcore`): **121 passed, 13 subtests**. This
  includes the updated “four installed specialists” assertion.
- Targeted mypy: one remaining error at
  `application.py:5159` (`VisionProvider.see` does not accept `structured`),
  in the pre-existing vision call path, outside Phase 28. The collaboration,
  analysis, verification, config, events, API and wiring files are clean.
- `python scripts/generate_api_reference.py --check`: **395 routes in sync**.

Not run: the complete repository suite. It was not claimed as passing.

## 10. Files changed

New:

- `src/novacontrol/agents/collaboration_controls.py`
- `src/novacontrol/agents/collaboration.py`
- `src/novacontrol/agents/analysis.py`
- `src/novacontrol/agents/verification.py`
- `tests/test_multi_agent_collaboration.py`
- `docs/MULTI_AGENT_COLLABORATION.md`
- `docs/PHASE28_REPORT.md`

Modified:

- `src/novacontrol/agents/__init__.py`, `models.py`, `pipeline.py`
- `src/novacontrol/core/config.py`, `core/events.py`
- `src/novacontrol/application.py`
- `src/novacontrol/api/app.py`, `models.py`, `route_consumers.py`
- `tests/test_specialist_agents_verification.py`
- `docs/API.md`, `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md`
- `AGENTS.md`, `docs/AGENTS.md`
- `tmp/phase28_probe.py`, `tmp/phase28_bench.py` (verification probes)

Nothing was committed or pushed.

## 11. Remaining issues and limitations

1. **Resource/provider controls are implemented; discovery and billing are
   not.** The central token ledger enforces a per-collaboration budget from
   reported usage, provider declarations are preflighted, Retry-After-aware
   backoff is bounded, and live resource assessments cap concurrency. Remaining
   limits: token counts cannot be reconciled against a provider invoice, quota
   discovery is not attempted (declarations only), and a provider that does not
   declare a context/output ceiling fails a requirement for one rather than
   having a value guessed for it.
2. **No UI panel.** The phase is exposed through the API and application
   methods; there is no dedicated web surface.
3. **Coding verification is in a disposable project.** The real DeveloperAgent
   wrote a file and a real verifier ran pytest, but the product's auto-change
   generator was not run against the live repository.
4. **Research depends on the live provider/network.** The recorded run succeeded,
   but it is not a hermetic test.
5. **Full suite not completed.** Focused and relevant regression groups passed.
   Under the Phase 29 focused-verification policy this is no longer an open
   acceptance criterion; no full-suite pass is claimed.
6. `sequential_fallback` is a policy flag; it is not a separate degradation
   path because the semaphore already queues work and `max_concurrency=1` is the
   explicit serial mode.

## 12. Acceptance matrix

| Criterion | Result | Evidence |
|---|---|---|
| 1. Agents registered/discovered | PASS | Registry, catalog, four real specialists, application boot test |
| 2. Collaboration benefit determined | PASS | Planner/decline tests; simple task refused |
| 3. Decompose and delegate | PASS | Research/coding/data plans; validated DAG |
| 4. Independent concurrency | PASS | Controlled timing test, measured parallelism 1.49 |
| 5. Dependent ordering | PASS | Dependency context/order test; failed dependency skip |
| 6. Structured exchange | PASS | Messages, declared context, provenance fields |
| 7. Conflicts handled | PASS | Unresolved and recomputation-resolved tests |
| 8. Aggregate and verify | PASS | 4/4 real CSV verification; unverified output cannot complete |
| 9. Failure preserves work | PASS | Failed sibling, timeout, retry, cancellation tests |
| 10. Restart/recovery | PASS | Completed restore; running→interrupted; resume |
| 11. Phase 27 learning | PASS | Capture test with `collaboration.data_analysis` |
| 12. Permission/resource limits | PASS | Permissions/approvals enforced; central token budget, provider preflight, bounded Retry-After/exponential backoff and hardware-capped concurrency covered by tests |
| 13. Real scenarios | PASS (with B/C/D/E harness scope) | Live research A, real CSV C, disposable coding B, failure D, conflict E |
| 14. Tests pass | PASS (focused policy) | 61 phase tests, 67 config/phase tests, 109 application/HTTP tests with 90 subtests; relevant agent/continual groups passed; no full-suite claim |
| 15. Documentation/report | PASS | This report, architecture doc, API regeneration, status/log updates |

## 13. Verdict

**Phase 28: PASS (focused-verification closure).** The genuine multi-agent
capability is implemented, integrated and demonstrated, including central
reported-token accounting, declared-provider preflight, bounded
Retry-After/exponential backoff and live hardware-aware concurrency. The
previous PARTIAL verdict was caused solely by the missing full-repository-suite
run; the Phase 29 policy removes that requirement in favour of phase,
modified-module, integration and focused regression evidence, which is recorded
in §9. No full-suite pass is claimed. Phase 28 is closed, and Phase 29 may
proceed. Nothing was committed or pushed.

## 14. Group 1 audit addendum (2026-11-26)

A group-level audit of Phases 27–31 found that this report's "PASS" and Phase
31's recorded limitation 3 were both true at the same time: specialists could
not select a skill inside their own pipeline (`src/novacontrol/agents/` contained
no reference to skills). That was an integration defect, and it is now closed.

* `SpecialistPipeline` accepts an optional `SkillProvider`; when an agent's own
  plan is empty (or a task names `metadata={"skill": ...}`), it selects a
  validated, compatible, fully-bound skill, authorizes it through the same
  `PermissionManager`, executes it through `SkillService`, and verifies it from
  the library's own verified verdict.
* The application wires its single `SkillService` in; no second catalog,
  registry, executor or permission table exists.
* An agent's own plan is never replaced, an unvalidated skill is never selected,
  and a skill awaiting approval is reported as a refusal.

Evidence: `tests/test_phase31_skills.py` (42 passed, up from 31), including an
application-level test that runs a real validated skill through the real
pipeline. Full record: `docs/GROUP1_PHASES27_31_AUDIT_AND_ACCEPTANCE_REPORT.md`
defect D1.
