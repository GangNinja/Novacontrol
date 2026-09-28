# Specialized Agents (Phase 12)

Phase 12 asks for specialized agents and forbids a second agent framework. Both
requirements are the same requirement: **one pipeline, many specialists.**

```
NLU → Context → Decision → Planner → Tool Selection
    → Execution → Verification → Recovery
```

`agents/pipeline.py` is that loop. A specialist is not an agent that *has* a
pipeline; it is a set of stage hooks that the pipeline runs — what it is being
asked to do (`decide`), what doing it takes (`plan`), how to do one step
(`execute`), and how to check it (`verify`, `recover`). Interpretation, context
assembly, permission checks, the execute/verify/recover loop, event publication
and the final verdict are the pipeline's, which is why two specialists cannot
drift apart in how careful they are.

Two specialists are implemented on it:

| Agent | Role | What it does |
|---|---|---|
| `developer-agent` | CODING | inspects a repository, searches code, reads open errors, plans and (when authorized) applies and tests a change, diagnoses failures, runs git |
| `research-agent` | RESEARCH | answers a question with cited claims drawn from the existing research engine, comparing sources and labelling evidence apart from inference |

Both register in the **same** `AgentRegistry` the coordinator, the agent event
bus and the chat path already dispatch through — a specialist *replaces* the
deterministic stub for its role (`prefer=True`), and the stubs stay registered
for the roles nobody implements, so a build with no workspace still answers.

## The stages, and what each one actually is

| Stage | Its implementation | Why it is that and not something new |
|---|---|---|
| NLU | `agentcore.TaskInterpreter` | the build already interprets goals; a specialist does not get its own parser |
| Context | the Phase 11 knowledge engine's `context_for` + the specialist's own cheap local facts | the developer reads its project context, the researcher reads its index |
| Decision | the specialist's routing of the request to one of the things it may do | the coordinator's *role* routing already happened; repeating it here would be a second dispatch |
| Planner | `PipelineStep` objects, each with the `expected` result verification will check | a plan step nobody can verify is not plannable |
| Tool Selection | the Phase 8 `PermissionManager` + the build's approval gateway + the live tool registry | one risk layer, not a per-agent opinion |
| Execution | the specialist's handlers | the only stage a specialist owns outright |
| Verification | the Phase 8 `VerificationResult` vocabulary | pass / fail / inconclusive, never "it returned" |
| Recovery | the Phase 8 `RecoveryStrategy` vocabulary | retry, or say why not |

Every run records all eight stages **in order**, whether or not a stage had
anything to do: a stage with nothing to do is recorded `SKIPPED` with the reason,
never omitted. A run that ends early still has eight entries, and the last four
say `the run ended before this stage` — which is how a reader can tell a stage
that failed from a stage that never happened.

The pipeline contains failure by design. A stage hook that raises is recorded as
FAILED and the run ends with a report, so a broken specialist cannot take its
caller down; a stage whose failure is survivable (a knowledge index that is still
building) degrades to `SKIPPED` with the reason in `errors`.

### Events

A run publishes the typed lifecycle vocabulary `core/events.py` already
declares — `task.started`, `intent.detected`, `context.resolved`,
`decision.created`, `plan.created`, `tool.selected`/`started`/`completed`/
`failed`, `verification.started`/`completed`, `recovery.started`/`completed`,
and `task.completed`/`task.failed`. Nothing new was added: a specialist run is
observable through the same bus, in the same correlation id, as every other
piece of work in the build.

## Never claim success without verification

The rule is enforced structurally, not by habit:

* The pipeline refuses to report **COMPLETED** unless at least one step was
  verified **PASS**. A run whose steps merely returned is FAILED.
* The verdict is taken from where each step **ended up**, not from every attempt,
  so a step that failed and was recovered by a retry is a success with a story
  (the retry is in `recoveries`, the failure is in `outcomes`).
* A step that never ran (`SKIPPED`) is neither a success nor a failure: its
  verification is **INCONCLUSIVE**, the verification stage says `SKIPPED`, and
  recovery is never asked about it — retrying something that was not applicable
  cannot change anything.
* A step that completed and produced nothing observable is INCONCLUSIVE, never a
  pass. That is the whole point: "the call returned" is the confusion
  verification exists to prevent.

## Nothing writes or executes without permission

Every capability an agent may select is *declared* to the risk layer, with its
risk stated rather than inferred from a verb:

| Declaration | Risk | Why |
|---|---|---|
| `developer.inspect_repository`, `inspect_structure`, `search_code`, `inspect_errors`, `plan_changes`, `diagnose`, `verify_changes`, `git_read` | LOW | they read the workspace and write nothing |
| `research.search_web`, `retrieve_evidence`, `compare_sources`, `detect_conflicts`, `synthesize`, `verify_citations` | LOW | a network read that changes nothing — but declared, so an offline or stricter build refuses it instead of reaching out |
| `developer.apply_changes` | HIGH, `filesystem:write`, **not reversible** | there is no backup to restore |
| `developer.run_tests` | HIGH, `shell:execute`, external side effect | running the project's tests runs the project's code |
| `developer.git_write` | HIGH, `shell:execute`, destructive | a commit or a push rewrites history that cannot be recalled |

The pipeline's Tool Selection stage is the gate: a step whose tool is not
registered with the build is refused outright, a step whose risk nobody approved
is refused with the reason, and a refused step is **recorded as DENIED and never
executed**. With no approval flow wired (the default) that is the end of a write
attempt — the run reports the refusals instead of performing them. Authorization
is a **per-run pipeline** (`agent.clone(pipeline)`, `developer_task(goal,
authorize=True)`), never a flag flipped on shared state, so a caller who
authorizes one run does not loosen the build for everyone else.

## The developer agent

Each responsibility has one home, and none of them is new machinery:

| Responsibility | Where it happens |
|---|---|
| inspect repositories | `SelfImprovementEngine.inspect()` |
| understand project structure | `ProjectDetector.detect()` + a bounded top level |
| search code | a local lexical scan of the workspace (deterministic, no model, no index build) |
| inspect errors | the bug log plus the project context's open errors; an unreadable log costs only the errors |
| propose changes | `SelfImprovementEngine.plan()` — with `writes_require_approval` stated in the report |
| modify files when authorized | `SelfImprovementEngine.apply_changes()`, behind both the pipeline's gate and the engine's own |
| run tests | a command runner (`SubprocessRunner`: exec-form, no shell, timeout, captured output) behind a permission gate |
| inspect failures / debug | the suite is reproduced, then the failure is diagnosed by signature |
| perform Git operations | a local parser plus the same runner; reads by default, writes gated |
| verify changes | the bytes on disk compared with the proposal, plus the test tally |
| report | the run's own evidence (`report()`, and the summary) |

The workflow the phase names — *Understand → Inspect → Plan → Modify → Test →
Verify → Recover if needed → Report* — is the pipeline, so an `apply_change` run
plans `inspect_repository → plan_changes → apply_changes → run_tests →
verify_changes`: the proposal always precedes the write, and the write is always
followed by the tests and by the byte comparison of what was written.

### Reading test output

Both tally formats this build actually produces are parsed, and neither is
guessed at:

* pytest — `3 passed, 1 skipped in 0.42s`;
* unittest — `Ran 5 tests` plus `OK (skipped=1)` / `FAILED (failures=2, errors=1)`.

Which command runs is recognised from the project the agent is standing in, not
assumed: `npm test` where a `package.json` and npm are present, this
interpreter's `-m pytest -q tests` where the tree is a Python one, and the pytest
default only when nothing is recognised — so "run the tests" in a Node project
does not run a command that cannot exist. The application's own test-command
helper asks the same function, so a plan and a run cannot disagree about which
command they mean.

A run with **no** tally is `recognised: false` and is *not* a pass. That matters
for the quiet failure mode: a collection error or a wrong command exits zero with
nothing to count, and calling that green is how an agent reports success after
running nothing. A debug run inverts the expectation deliberately — the failure
it was asked about is the result it wants — and a suite that passes when a
failure was reported is verification FAIL ("the reported failure was not
reproduced"). One nonzero exit code is never retried blindly: a debug run's
nonzero exit is the reproduction, not the problem.

### Diagnosing a failure

`diagnose_failure` names a signature that is actually present in the output —
`missing_dependency`, `syntax_error`, `assertion`, `type_error`, `name_error`,
`attribute_error`, `key_error`, `error` — and reports `unknown` when none is. A
guess sends the next attempt in the wrong direction, so an unrecognised failure
is reported as unrecognised rather than confidently mislabelled. A diagnosis also
comes only from a step that actually **ran**: a refused step produced no output to
read, so it is recorded SKIPPED with "no failing output was recorded to diagnose"
rather than having its own refusal read back as the failure.

### Recovering

Recovery is bounded and deliberate:

* a transient test failure is retried **once** ("rule out a flaky or ordering
  failure");
* a **missing dependency** aborts — retrying cannot install a package;
* a **failed write** aborts — a half-written file is not a state to re-run into;
* a **repository command** aborts, whether it mutated or not, because the honest
  next step is to re-read the state, not to repeat the command;
* the retry budget is finite (`max_recovery_attempts`), and the execution queue is
  capped independently, so a specialist cannot loop.

### Git operations

The verb the user named is the verb that runs, and its arguments are read back
rather than assumed. Flags, revisions (`HEAD~1`), paths (`src/x.py`), URLs,
remote and branch names the workspace **actually has** (read from `.git/config`,
`.git/HEAD`, `.git/refs/**` and `packed-refs` — read-only, no subprocess), and
quoted values (kept whole, so `git commit -m 'fix the widget'` survives having no
shell to quote it) are passed through as argv.

Everything else is reported rather than dropped:

* words that are English glue (`git commit these changes`) are recorded as
  **unused** and the command runs without them — a read-only command with unused
  words still answers, and the report says which words were not used;
* a word that might have been an argument and could not be shown to be one is
  **unreadable**, and a **mutation containing any unreadable word is refused
  unrun**: running `git commit` for "git commit the widget fix" is a different
  repository operation from the one that was asked for, and guessing is how an
  unattended agent rewrites history nobody asked it to touch.

Read-only classification follows the arguments, never the verb alone: `git branch`
lists and `git branch -D feature/x` deletes, `git remote` shows and `git remote
add` changes, `git stash list` reads and `git stash pop` writes. Anything
unrecognised is treated as a mutation, because the safe default for a repository
command nobody classified is to ask first.

## The research agent

Research here is **source-aware**: the answer is not a paragraph, it is a set of
claims, and every claim says where it came from.

| Responsibility | How |
|---|---|
| understand the research question | `TaskInterpreter` plus the query frame (`parse_query`) — subject, frame kind, depth |
| determine required sources | the decision: web research or the local index, honouring `allow_web` and an offline request |
| search the web when authorized | the **existing** `ExploreService` (the application injects it as the provider) — no second browser subsystem |
| retrieve information | the page text `Explore` already read, ranked into evidence sentences by `build_evidence` |
| compare sources | per-source claims, relevance and a cross-source agreement figure |
| identify conflicting claims | negation, figure and antonym disagreement between sources that demonstrably discuss the same thing |
| synthesize | claims, not prose blobs |
| provide citations | 1-based citation indexes preserved end to end, resolved against the gathered sources |
| distinguish evidence from inference | every claim is labelled; inference carries the evidence it rests on |

### Conflicts

Two claims are only called conflicting when they are demonstrably about the same
thing (their vocabulary overlaps, by Jaccard or by containment, which is what
catches a short claim fully contained in a longer one) and they disagree in one
of three checkable ways: one is negated and the other is not, they cite figures
that do not match, or they use opposite terms. A difference of emphasis or scope
is **not** reported, because a fabricated disagreement is worse than silence. One
source cannot conflict with itself, and inference claims are never compared —
they are this agent's own reasoning, not a source's claim.

### Labels, and the citation check

* An **evidence** claim quotes a source and carries at least one citation index.
* An **inference** claim carries no citation and names the evidence (`basis`) it
  was drawn from.

`verify_citations` then checks every index against the gathered sources and
**fails the step** on an evidence claim with no citation, a citation index with
no matching source, or an inference with no basis. That is the check that makes a
fabricated reference impossible to ship: a forged citation index is caught by
verification rather than by a reader noticing.

A run with no local knowledge indexed and no web authorized fails honestly — it
says it answered from local knowledge only and produced nothing, rather than
inventing an answer from nothing. A local answer that does find the index cites
it as `[1] Local knowledge index`.

### Recovering

A **fetch** (`search_sources`, `retrieve_evidence`) is retried once — a second
attempt might actually retrieve something. A **computation**
(`compare_sources`, `detect_conflicts`, `synthesize`, `verify_citations`) is not
retried at all: its inputs are already in the run, so a retry spends time to
arrive at the same failure.

## How the rest of the build reaches it

* **Chat / coordinator** — the specialists are registered in the same
  `AgentRegistry`; `CoordinatorAgent.delegate("fix the authentication issue")`
  reaches the developer agent, and a research request reaches the research agent.
* **Application** — `developer_task(goal, authorize=False)`,
  `research_task(question, allow_web=True)` and `specialist_agents()`.
* **Bus** — the `AgentModule` request/reply surface, and the lifecycle events a
  run publishes.
* **CLI** — `python -m novacontrol demo phase12_agents` runs the whole thing: an
  inspect, a proposal, a refused write, an authorized run and a cited research
  answer.

## What this does not do

* It does not invent a second orchestration: `agentcore` remains the
  task-level planner (perception, action engines, the evaluation ledger), and the
  specialist pipeline is the role-level loop. A specialist does not replace
  `AgenticOrchestrator`; it runs a narrower job through one shared stage runner.
* It does not make the developer agent a general code generator: it *coordinates*
  the build's change generator (`build_auto_code_changes`) and the
  self-improvement engine rather than writing its own model calls.
* It does not let a specialist widen its own authority. The declarations are
  stated in one place per agent, and the risk layer — not the agent — decides.
* It does not claim a mutation it could not read. Refusing and saying which word
  could not be read is the honest answer; running something adjacent is not.
