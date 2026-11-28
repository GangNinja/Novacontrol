# Transfer Learning & General-Purpose Skills (Phase 31)

Phase 31 gives NovaControl a library of **versioned, evidence-based procedures**
that can be retrieved for a new task, adapted to a different context, composed
with other skills, executed through the existing pipeline and judged by what
actually happened.

It is not a second planner, memory system or execution engine. The existing
`SkillRegistry` (callable function skills) is preserved beside the new library.

## 1. Architecture

```
evidence (real execution)
        │
        ▼
SkillAcquisition ──► SkillCatalog (immutable versions, token index)
        │                    │
        │                    ├── SkillTransfer  (compatibility + adaptation)
        │                    ├── SkillComposer  (dependencies + ordering)
        │                    └── retrieval      (lexical + reliability +
        │                                        compatibility + Phase 27)
        ▼
SkillService ── PermissionManager ──► injected step executor
        │                                   │
        │                                   ├── ToolExecutor (registered tools)
        │                                   ├── handle_request (intent → decision →
        │                                   │   plan → tool → permission → verify)
        │                                   ├── desktop plan + single-use token
        │                                   ├── browser plan + single-use token
        │                                   └── research / developer pipelines
        ▼
JsonStateStore("skills") + EventBus("skills.*")
```

| Module | Responsibility |
|---|---|
| `skills/models.py` | Value objects: `Skill`, `SkillStep`, `SkillInput`, `SkillReliability`, `SkillEvidence`, `SkillMatch`, `SkillExecution`, `Compatibility`, `AdaptationResult`, `CompositionResult`, `SkillObservation`, `AcquisitionResult`; the original `FunctionSkill` types remain. |
| `skills/catalog.py` | Versioned store, duplicate detection by name + procedure signature, inverted token index, ranked retrieval, promote/deprecate/rollback, tolerant persistence. |
| `skills/acquisition.py` | Turns repeated verified observations into a skill; rejects one success, mixed procedures and unverified evidence. |
| `skills/transfer.py` | Compatibility checks and explicit adaptation with recorded changes. |
| `skills/composition.py` | Dependency, cycle, input, output, tool and permission checks; ordered plan. |
| `skills/service.py` | Lifecycle, evidence, reliability, execution routing and checkpoints. It owns no executor. |

## 2. Skill contract

A stored `Skill` carries:

- identity (`id`), name, description and objective;
- category (`file`, `research`, `desktop`, `browser`, `data`, `document`,
  `general`) and lifecycle;
- ordered `steps` (instruction with `{input}` placeholders, kind, tool,
  permission, environment, verify/optional flags);
- declared inputs (name, required, example, kind), expected outputs, tools and
  permission scopes;
- preconditions, postconditions, dependencies and supported environments;
- known limitations and validation notes;
- provenance (`observation:<id>`, `agent:<name>`, adaptation source, requester);
- reliability (trials, successes, failures, corrections, transfer outcomes,
  duration, derived success/transfer rates and confidence);
- version, parent version, timestamps and metadata.

The procedure signature is `(kind, tool, normalised_instruction)` for every
step. Two skills with the same name and signature are a duplicate, even if they
arrived from different observations.

## 3. Lifecycle

| State | Meaning | Executable? |
|---|---|---|
| `ACQUIRED` | Evidence showed the same procedure twice; not yet validated by its own execution. | No |
| `VALIDATED` | Its own version has enough verified executions. | Yes |
| `ADAPTED` | A new version for a different task/context, with empty reliability until it proves itself. | Yes (for evaluation) |
| `DEPRECATED` | Replaced or withdrawn; history and evidence kept. | No |
| `FAILED` | Evidence shows it should not be reused. | No |

Validation is explicit and evidence-based. It requires
`SkillAcquisition.min_support` (2 by default) verified executions **of that
version**, and refuses when failures outnumber successes. An adapted version
never inherits its parent's reliability or validation. Adaptation requires a
`VALIDATED` source and records refusal evidence when the source is only
`ACQUIRED` or `ADAPTED`. The source version is never mutated; promote makes an
independently validated version active, and rollback deprecates the replaced
version and restores an earlier **validated** version only.

## 4. Acquisition

`SkillService.acquire(goal, observations, ...)`:

1. Accepts only verified observations.
2. Requires at least two independent observations (or a demonstration *and* at
   least one verified execution — a demonstration alone is not execution).
3. Normalises the goal and checks procedure-shape similarity across
   observations; different shapes are rejected rather than averaged.
4. Templatises observed input values into `{placeholders}`.
5. Extracts input examples, tools, permissions, environment and category.
6. Records observation provenance and reliability support.
7. Stores the skill as `ACQUIRED` (not executable yet).

A single success is not a reusable skill. A prompt template, a stored plan or a
generated description is not evidence and is never accepted as one.

## 5. Retrieval

`SkillService.retrieve(goal, ...)` uses an inverted token index, not a scan of
every skill. Candidates are ranked by:

- lexical relevance of goal/name/objective/description/category/inputs/tools;
- evidence-derived reliability (success rate, confidence);
- lifecycle (validated/adapted rank above acquired/deprecated);
- compatibility with the requested environment, inputs and available tools.

Incompatible skills are omitted unless `include_incompatible=True`; then the
match names the missing inputs/capabilities rather than pretending portability.
Phase 27 validated recommendations re-rank matches: preferred tools gain a small
bonus, disallowed tools lose more, and both are named in the reasons with the
strategy and evidence ids. A recommendation never overrides the permission
layer or executes anything.

## 6. Transfer and adaptation

`SkillService.adapt(skill_id, goal=..., environment=..., inputs=...,
target_map=...)`:

1. Checks that the source version is `VALIDATED`, then checks environment,
   tools and (for execution) inputs for compatibility. An `ACQUIRED` or
   `ADAPTED` source is refused and the refusal is recorded.
2. Applies explicit target substitutions in one pass (a target that is also a
   source cannot undo its own replacement).
3. Rewrites environment-specific steps and step tools when the mapping is
   declared.
4. Recomputes the adapted input contract: placeholders and supplied values are
   kept, new task inputs are added, and inputs the adapted procedure no longer
   binds are removed with a recorded reason.
5. Recomputes the declared tool set.
6. Writes a new version (`ADAPTED`, parent version recorded) and leaves the
   original untouched.
7. Records the adaptation changes in metadata and provenance.

If the environment or a required tool is unsupported, adaptation returns
`compatible=False` with the exact missing capability and records the failure
evidence. It does not produce a best-effort plan.

**Cross-application** transfer is real for structurally substitutable target
words (Calculator ↔ Notepad, Chrome ↔ Edge) but every step still runs through
the actual desktop/browser controller and must verify. **Cross-OS** transfer is
not claimed: environment strings are declarations and checks, not adapters for
another operating system. A skill that declares `windows` is refused on an
unknown environment rather than silently attempted.

## 7. Composition

`SkillService.compose(objective, skill_ids, ...)` checks:

- every skill is executable (`VALIDATED` or `ADAPTED`);
- dependencies name skills in the composition (missing dependencies are
  conflicts);
- the dependency graph is acyclic and produces a deterministic order;
- required inputs are supplied by the caller or by a composed skill's outputs;
- no two skills produce the same output;
- declared tools are available and permissions are declared.

The result is a plan, not an execution. An input produced by an earlier skill
satisfies a downstream declared input, and a verified step's declared outputs
are bound into the next skill's inputs. `compose_and_execute` (also exposed as
`POST /skills/compose/execute`) runs the ordered plan and stops at the first
non-completed step, preserving the partial results. Each execution keeps its
own checkpoint and evidence.

## 8. Execution and checkpoints

`SkillService.execute` first checks lifecycle, compatibility, inputs,
per-step environments, tool availability and permission scopes. A required
input's `example` is documentation only; it is never used as a default, so a
missing required input blocks execution. A sensitive scope with no approval
returns `waiting_approval` **before** a step runs. Every step is then routed through the
application's executor:

- registered tool → existing `ToolExecutor` (schema validation, approval,
  cache, normalisation);
- request/verify → `handle_request` (the ordinary intent/decision/plan/tool/
  permission/verification pipeline);
- desktop/browser → plan then execute with the same single-use approval token a
  typed command uses; the skill router requires the step to be approved;
- agent → existing research/developer pipelines.

A step counts as complete only when its outcome is verified. Required steps must
all complete for the execution to be `completed`; optional step failures are
recorded but do not by themselves fail the run. `completed_steps` is persisted
in the execution record, so `resume(execution_id)` skips verified steps and does
not replay their side effects. A Phase 30 task that was parked for approval
resumes the matching skill execution rather than starting a new one.

## 9. Reliability

Reliability is derived from `SkillEvidence` rows, not from model confidence:

- trials, successes, failures, corrections, transfer successes/failures;
- success rate, transfer rate, average duration;
- confidence grows with trials and is capped by the evidence;
- every execution appends evidence (bounded), updates the version in the catalog
  and emits `skills.skill_executed`.

A newly acquired, adapted or unexecuted skill honestly reports zero reliability.

## 10. Phase integration

- **Phase 27 (continual learning):** retrieval calls the existing
  `ContinualLearningEngine.recommend`; validated strategy/tool preferences
  re-rank skills, and avoid-tool evidence is named. A skill's own evidence is
  separate from Phase 27's execution corpus; neither silently overwrites the
  other.
- **Phase 28 (multi-agent collaboration):** acquisition accepts `origin_agent`
  provenance; adaptation and validation are centralized and versioned; a skill
  run always goes through the application's `PermissionManager`, so an agent
  cannot self-approve. Collaboration agents can discover and execute skills
  through the application's skill methods; direct in-pipeline skill selection
  is not yet wired (see limitations).
- **Phase 29 (computer operation):** desktop and browser skill steps use the
  same controllers and token-gated plan/execute path a computer operation uses,
  including closed-loop action verification. **Adopted definition (2026-11-26,
  Group 1 DF-8):** this IS how a skill performs a computer operation — Phase 31
  criterion #13 is satisfied by these steps, and no `COMPUTER` step kind exists
  or is planned. The Phase 29 `ComputerOperator` loop stays reachable separately
  through Phase 30's `agent="skill:<id>"` dispatch. The original Phase 31 brief is
  not in the repository, so this is an adopted interpretation, not recovered
  wording.
- **Phase 30 (persistent autonomy):** a task with
  `agent="skill:<id>"` and `metadata["skill_inputs"]` executes the skill through
  `_execute_operation_task`; `metadata["skill_version"]` and
  `["skill_environment"]` are honored; task approval is the skill approval; a
  parked skill run resumes from its checkpoint.

## 11. Security

- Skills never carry their own authority. The service requires a
  `PermissionManager`; without one it blocks.
- Every declared skill scope and every step scope is checked. Confirmation
  requirements park the run; a denial blocks it.
- Tool steps pass through the existing tool executor's schema validation,
  approval gateway and risk layer.
- Desktop/browser steps require explicit task approval and still consume the
  plan's single-use token.
- `/skills/execute`, `/skills/resume` and `/skills/compose/execute` refuse a
  client-supplied `approved` flag with HTTP 403; approval comes from the
  objective/task path, never from the request body.
- Inputs are validated against the declared contract before execution; outputs
  are judged by the step's own verifier. A step environment that differs from
  the run environment is refused before the executor is called.
- External instructions and imported procedures are observations only after
  verification; they are not executed during acquisition.
- Lifecycle changes, adaptations and failed adaptations are stored and
  announced. No destructive operation is started automatically.

## 12. Persistence and API

The service persists one `skills` key through the shared `JsonStateStore`:
catalog versions, evidence, executions (checkpoints) and failed adaptations.
Boot restores them tolerantly; a damaged record is skipped rather than turned
into a plausible empty skill.

15 authenticated routes are registered in `ApiSurface` and
`route_consumers` (all `no-render` — this phase has no dedicated panel):

| Method | Route | Purpose |
|---|---|---|
| GET | `/skills` | library status/counts |
| GET | `/skills/catalog` | all versions, evidence, failures |
| GET | `/skills/search` | ranked retrieval (`query`, `environment`, `limit`) |
| GET | `/skills/evidence` | bounded evidence and failed adaptations |
| GET | `/skills/{skill_id}` | one skill, its versions and executions |
| POST | `/skills/acquire` | acquire from observations |
| POST | `/skills/validate` | validate from its own evidence |
| POST | `/skills/adapt` | create an adapted version |
| POST | `/skills/compose` | check/order a composition |
| POST | `/skills/compose/execute` | execute an ordered workflow and bind outputs |
| POST | `/skills/execute` | execute a validated/adapted skill |
| POST | `/skills/resume` | resume a parked execution |
| POST | `/skills/promote` | make an independently validated version active |
| POST | `/skills/deprecate` | deprecate a version |
| POST | `/skills/rollback` | restore an earlier version |

`docs/API.md` is generated from the same registries and was regenerated at
**434 routes**.

## 13. Measured performance and limits

On the verification host (Windows 11, Python 3.13, no model):

- retrieval: 0.312 ms for 3 matches on the measured small catalog;
- adaptation: 14.983 ms in the final live scenario (including input-contract
  rewrite);
- version lookup: 0.0015 ms; storage was 60,170 bytes and RSS 89.2 MB;
- the library deliberately uses a token index; a very large catalog would need
  a persistent index, which is not implemented.

Honest limitations:

- live verification covers Windows only; macOS/Linux desktop and cross-OS
  transfer are **not verified**;
- browser skill transfer is live-verified (Windows, Chromium 156.0.8078.4,
  32/32 checks) from the second Group 1 pass; cross-OS transfer is still not
  implemented and is refused with a reason;
- in-pipeline selection exists (see below); because every shipped specialist
  returns a plan of its own, the reachable in-pipeline path is an explicit
  `metadata={"skill": …}` request;
- Phase 29's `ComputerOperator` is not a skill step type; desktop/browser
  controllers are the executed capability;
- retrieval is lexical, not semantic/embedding-based;
- no dedicated Skills UI; API/CLI only;
- performance figures are for a small catalog, not a large library benchmark.

See [PHASE31_REPORT.md](PHASE31_REPORT.md) for the acceptance matrix and exact
commands.

## In-pipeline selection (Phase 28 specialists)

A specialist can select a skill inside its own pipeline — it is not limited to
the `/skills` API or to a Phase 30 objective.

- **Where it happens:** the planner stage of `SpecialistPipeline`. A task that
  explicitly names a skill with `metadata={"skill": "<id or name>"}` (plus
  optional `metadata={"skill_inputs": {...}}`) is answered from the library
  **first**; otherwise the library is consulted only when the agent has **no
  procedure of its own** for the goal. A plan nobody asked to replace is never
  replaced.
- **What is selected:** a `VALIDATED` or `ADAPTED` version whose environment is
  compatible, whose required inputs are bound, and whose tools are available. An
  `ACQUIRED` version, an incompatible one, or one with an unbound required input
  is skipped and the run reports "nothing to do", exactly as before.
- **How it runs:** the step (`skill.execute`) is authorized by the same
  `PermissionManager` as every other step, then executed by `SkillService`, which
  routes each step through the application's existing tool, request, desktop,
  browser or agent path. The pipeline owns no executor.
- **How it is verified:** from the library's recorded verdict. `{"status":
  "completed", "verified": true}` is a PASS; a completed-but-unverified skill is
  a FAIL; `waiting_approval`, `blocked` or `refused` is a DENIED step, and a run
  with a denied step cannot complete.
- **What it is not:** a second registry. There is one catalog, one evidence
  store, one permission table and one execution path, shared with `/skills` and
  with Phase 30's `agent="skill:<id>"` dispatch.
- **Refusals are not retried:** a `DENIED` step records `RECOVERY: SKIPPED —
  refused, not retried`. A retry cannot change a refusal's verdict, and for a
  skill it would start a second execution with a second approval checkpoint.
