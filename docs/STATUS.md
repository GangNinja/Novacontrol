# Project Status

Current phase: **Phase 15 - Complete Local Baseline**

Status: **Complete**

> The phase label above is the original fifteen-phase baseline. The staged build that
> followed it — what it verified, what it changed, and what remains — is recorded below
> under "Completed Staged-Build Verification (Phases 1–7)" and the Phase 8 / Phase 9 /
> Phase 10 sections that continue it.

Verified with:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
python -m novacontrol
```

## Completed Foundation Work

- Repository metadata and dependency declaration
- Source package layout for all requested modules
- Core event bus
- Runtime module contract
- Runtime lifecycle coordinator
- Configuration loader
- Security approval primitives
- FastAPI application factory scaffold
- Architecture, development, plugin, and phase documentation
- Unit tests for configuration, events, runtime, and security

## Completed Core Engine Work

- Runtime service container
- Durable JSONL event journal
- In-memory event journal
- Retry policy helper
- Lifecycle error handling
- Event handler delivery error policy
- Runtime health diagnostics
- Unit tests for diagnostics, journaling, retry, and services

## Completed Memory Work

- Conversation, project, knowledge, short-term, and long-term namespaces
- Typed memory records
- In-memory memory store
- SQLite durable memory store
- Memory manager for remember, recall, retrieve, summarize, and cleanup
- Event-driven memory runtime module
- Vector index interface and local hashing index
- Extractive summarization
- Unit tests for storage, persistence, retrieval, summarization, events, and vector search

## Completed Tool System Work

- Tool schemas and typed parameters
- Tool registry
- Callable tool adapter
- Tool executor with validation
- Permission-to-approval enforcement
- Deny-by-default sensitive tool behavior
- Event-driven tool runtime module
- Unit tests for registration, execution, validation, approval, denial, and events

## Completed Agent Work

- Agent roles for all requested specialized agents
- Deterministic specialized agent implementations
- Coordinator routing by task intent
- Agent registry
- Agent message bus and history
- Event-driven agent runtime module
- Unit tests for routing, delegation, messaging, events, and duplicate protection

## Completed Planning Work

- Clarification policy
- Goal decomposition
- Dependency-aware plan steps
- Workflow executor
- Step retry recovery policy
- Event-driven planning runtime module
- Unit tests for decomposition, clarification, dependency execution, retry, and events

## Completed Desktop Automation Work

- Desktop action and workflow models
- Approval-gated desktop automation controller
- No-op desktop runner for safe default behavior
- Application launch planning
- Script execution planning
- File organization planning
- Automation audit log interface and in-memory implementation
- Event-driven desktop automation module
- Unit tests for denial, approved mock execution, planning, audit logging, and events

## Completed Browser Automation Work

- Browser action and workflow models
- Browser runner adapter interface
- Safe no-op runner
- Navigation workflow
- Extraction workflow
- Form-fill workflow
- Web app test workflow
- Approval policy for sensitive browser actions
- Event-driven browser automation module
- Unit tests for denial, safe extraction, approved execution, and events

## Completed Vision Work

- OCR result model
- Screen understanding model
- Window detection model
- Image understanding model
- Document understanding model
- Vision processor interface
- Dependency-free baseline vision processor
- Event-driven vision runtime module
- Unit tests for OCR, screen understanding, document sections, image labels, and events

## Completed Explore Work

- Online search provider interface
- DuckDuckGo Lite search provider
- Video provider interface
- YouTube related video search link provider
- Clear explanation generator
- Explore report model with sources, videos, learning path, and follow-up questions
- Event-driven Explore runtime module
- CLI command: `python -m novacontrol explore "your topic"`
- Unit tests for report creation and events

## Completed Voice Work

- Speech transcript model
- Text-to-speech request and result models
- Wake word result model
- Conversation state management
- Speech recognizer adapter interface
- Text-to-speech adapter interface
- Wake word detector adapter interface
- Dependency-free speech recognizer baseline
- Safe text-only TTS baseline
- Keyword wake word detector
- Interruptible conversation manager
- Event-driven voice runtime module
- CLI demo: `python -m novacontrol demo phase10`
- Unit tests for transcription, wake word detection, interruption, and events

## Completed GUI Work

- Dashboard tab model
- Dashboard state model
- Dashboard view model
- PySide6 GUI launcher
- Chat, Explore, Tasks, Memory, Projects, Plugins, Settings, Logs, and Performance tabs
- Explore tab connected to the Explore backend
- CLI dry run: `python -m novacontrol gui --dry-run`
- CLI demo: `python -m novacontrol demo phase11`
- Unit tests for dashboard state and Explore tab availability

## Completed API Work

- API route metadata
- Bearer-token authenticator
- REST health endpoint
- REST status endpoint
- REST planning endpoint
- REST Explore endpoint
- REST plugin endpoint scaffold
- WebSocket event endpoint
- CLI demo: `python -m novacontrol demo phase12`
- Unit tests for authentication and route metadata

## Completed Plugin Marketplace Work

- Plugin manifest model
- Plugin capability model
- Plugin install record model
- Plugin discovery from `plugin.json`
- Plugin repository interface
- In-memory plugin repository
- Permission approval for sensitive plugin installation
- Trust, enable, and disable lifecycle
- Event-driven plugin marketplace module
- CLI demo: `python -m novacontrol demo phase13`
- Unit tests for manifests, discovery, denial, approval, enablement, and events

## Completed Performance Work

- Metrics registry
- Metric samples
- TTL cache
- Sync and async profiler
- Async load-test harness
- CLI demo: `python -m novacontrol demo phase14`
- Unit tests for metrics, cache expiration, profiling, and load testing

## Completed Release And App Bootstrap Work

- Release readiness checker
- Deployment and release runbooks
- Integrated `NovaControlApplication`
- Integrated CLI command: `python -m novacontrol ask "your request"`
- Runnable demos through Phase 15
- Remaining baseline modules implemented: skills, scheduler, projects, knowledge, automation, integrations

## Completed Staged-Build Verification (Phases 1–7)

- All seven staged objectives re-derived from the code and the docs, then driven through `handle_request`/`run_plan` with no local model installed
- Phase 2 context resolution reaches execution: "open it" after "open chrome" plans `open chrome`, and a named close ("close it") plans the graceful window close instead of a shell command
- Phase 4's own example now has an executor: `run_tests`/`run_command` steps locate the project, recognise its runner, run exec-form with a timeout, capture the output tail, and report the exit code to the verifier — gated on explicit approval of the step id
- Four CI checks confirmed locally: tests on Python 3.12/3.13 (1594 passed / 12 skipped, 1665 subtests), `docs/API.md` sync, and mypy in both platform views (217 modules)
- Residuals recorded in `DEVELOPMENT_LOG.md` §25: file operations understood but unwired, the legacy-classifier fallback still able to plan a literal reference, `nlu.model` reporting the provider name, and real VLM answer quality untestable without a vision model

## Completed Reliability, Recovery and Safety (Phase 8)

- New `novacontrol/reliability/` package: `VerificationEngine`, `RecoveryEngine`,
  `TaskStateMachine`, `TaskController`, `PermissionManager` — each extending an
  existing abstraction (`DeterministicVerifier`, `RecoveryAdvisor`, `ToolMetadata`,
  `core.security`) instead of replacing one
- Verification: the step's own check wins, then a per-tool strategy, then the check
  the action implies (`write_file` → the file exists, `open_application` → the process
  is there); window/network probes are injectable and report "cannot tell" rather than
  a failure; `VerificationResult` gained `success`/`verifier`/`expected_state`/`actual_state`/`confidence`/`error`/`metadata`, additively
- Recovery: bounded by the plan's retry policy (hard ceiling), never repeats a
  destructive or external action, asks a person for a denial instead of retrying into
  it, offers registered or built-in alternatives only for a structural missing
  dependency, and reports an unverifiable run as executed-and-unconfirmed
- Task control: eleven formally checked states (an illegal transition raises rather
  than overwrites), whole-phrase pause/resume/cancel, and cooperative cancellation
  honoured at checkpoints so nothing in flight is killed and a paused task keeps its state
- Permissions: one ordered layer (declared → tool metadata → action verb → LOW) that
  reports its source, with destructive/external/irreversible rules and an executor
  collaborator that puts an undeclared high-risk tool to a person
- Also fixed the CI failure at `9bfe82b`: the vision refusal now names the file when the
  file itself cannot be read, instead of reporting a text gap that depended on whether
  the host had a vision model installed
- Verified against the phase's own specification by driving every clause through the
  application (`tests/test_reliability.py::ApplicationReliabilityWiringTests`, 34 tests),
  including the specification's own "Open VS Code" example in all three readings
  (verified / failed / nobody can tell)
- Four defects fixed there, recorded in `DEVELOPMENT_LOG.md` §27: cancelling a *parked*
  task crashed the runner with a transition error instead of `TaskCancelled`; `resume`
  reported a continuation for a task whose cancellation was already pending; a step that
  failed its check reported `verification_result: "not_run"` because the failing
  verification was dropped on the way out; and the risk layer denied a plain read —
  `installed_applications` (declared LOW, read-only) derived MEDIUM from the letters of
  "install" in its *name*, which outranked the tool's own declaration
- Gates: **1703 passed / 12 skipped** (1678 subtests), mypy clean in both platform views
  (223 modules), `docs/API.md` in sync, ruff cleanup with no new findings

## Completed Internal Event Bus and Capability Registry (Phase 9)

- Extended the existing `core.events.EventBus` rather than adding a second one: a typed
  `EventType` vocabulary of all twenty-two lifecycle events, a payload schema each event
  is VALIDATED against at the publisher, a bounded in-memory history (`recent()`), and an
  `emit()` door that cannot raise because a subscriber failed
- Published from the live path and covered by tests: `intent.detected`,
  `context.resolved`, `decision.created`, `plan.created`, `task.started`/`paused`/
  `resumed`/`cancelled`/`completed`/`failed`, `tool.selected`/`started`/`completed`/
  `failed`, `verification.started`/`completed`, `recovery.started`/`completed`,
  `model.loaded`/`unloaded`, `vision.started`/`completed`
- Correlation comes from a context variable holding the request being served, so a tool
  call or a check three layers down carries the REQUEST's id; the task state machine's
  observer, the tool executor and the plan executor each take one injected observer or
  sink, so no component has to know the bus exists
- Extended the existing `CapabilityRegistry`: the twelve metadata fields the
  specification names, three sources (declared verbs, projected tools, application
  actions), availability MEASURED against this machine (a missing model or tool is
  UNAVAILABLE with the reason; an unprobeable requirement is UNKNOWN, never "available"),
  and a real gap declared as one (`system.reason`)
- Discovery answers "what capabilities are available for this task?": ranked with the
  evidence for each score, filters for intent/category/tools/models, unavailable matches
  only when asked for, and it executes nothing
- The DecisionEngine reports the matched capability id and its availability beside its
  route; the ToolSelector reports availability on every candidate and `degraded` on a
  choice this machine cannot run yet — neither re-routes on it, because availability is a
  property of the machine, not of the request
- New additive API routes `/capabilities` (the whole inventory) and
  `/capabilities/discover`; `docs/API.md` regenerated
- Eight defects found by driving the phase through the application and fixed, recorded in
  `DEVELOPMENT_LOG.md` §28: `tool.selected` never fired because its payload key shadowed
  the envelope's `source` (and the selector hid the raise); `vision.started` had the same
  collision plus an announcement made before the question existed; the "task names this
  capability" ranking rule never fired for dotted ids; a capability metadata row enriched
  a capability nothing registers; the tool inventory was a boot snapshot; the selector
  reported nothing about availability; a latent `AttributeError` on a capability with no
  intent; and `_HANDLERS` was a CLASS-level dict, so one test's injected handler
  re-routed every other application in the process
- Gates: **1796 passed / 12 skipped** (1686 subtests), mypy clean in both platform views
  (223 modules), `docs/API.md` in sync (68 routes), ruff at or below the pre-existing
  baseline on every touched file

## Completed Plugin SDK (Phase 10)

- Added the stable `Plugin` interface the phase asks for: identity (`plugin_id`,
  `name`, `version`, `description`, `author`), what it contributes (`capabilities`,
  `tools`), what it may touch (`permissions`, `risk_level`, `required_capabilities`,
  `external_services`, `filesystem_access`, `network_access`), how it is configured
  (`configuration_schema`), and the five lifecycle hooks (`load`, `initialize`, `enable`,
  `disable`, `shutdown`) — every hook defaulted, so a plugin implements only what it has,
  and every declaration mirrored into the existing `PluginManifest`
- Added `PluginManager`: discovery of plugin directories (`plugin.json` + `plugin.py`,
  imported by file location), validation of every declaration BEFORE anything runs
  (identity, version shape, core version, capability kinds, duplicate names, tool-name
  collisions, permission-scope honesty, configuration schema), the lifecycle walk, status
  exposure (`PluginRecord` with status, error and approval id) and bulk operations that
  stop at nothing
- Failures are contained, and that is the phase's headline promise: an un-importable
  module, a raising `load`, a raising `enable` and a raising `shutdown` each become a
  FAILED record with the exception named plus a `plugin.failed` event, and every other
  plugin in the same `enable_all()` call still enables. A plugin's mistake is never an
  exception the process has to survive
- Security is the centralized one: the plugin's declarations are registered with the
  existing `PermissionManager` (per plugin and per tool), the policy decides whether a
  person must be asked, and the same `ApprovalGateway` the device actions use is asked
  before `enable` runs — refused means `status="denied"`, nothing registered, and the
  plugin still loaded so the approval can be retried. A plugin whose code claims more
  than its `plugin.json` (extra scopes, lower risk, undeclared network/paths/services) is
  rejected at discovery
- Registration goes where the system already looks: tools into `ToolRegistry` and tool
  metadata into `ToolCatalog` (so the selector finds them), capabilities into the Phase 9
  `CapabilityRegistry`, and each tool's declaration into the permission layer so a plugin
  tool is gated exactly like a core one. All three gained an `unregister` so a disable or
  an unload withdraws what the plugin contributed
- Six `plugin.*` lifecycle events joined the typed vocabulary in `core/events.py`
  (`plugin.loaded`/`initialized`/`enabled`/`disabled`/`unloaded`/`failed`, payloads
  validated), published through the non-raising `emit()`; the marketplace's enable/disable
  events publish the same `plugin_id` field so one subscriber reads both publishers
- Three minimal examples (`SystemPlugin`, `DeveloperPlugin`, `BrowserPlugin`) that
  duplicate no core behaviour, and a runnable demo (`python -m novacontrol demo
  phase10_sdk`) that enables all three and shows the deliberately broken one failing while
  the rest keep working
- Four defects found while building and fixed, recorded in `DEVELOPMENT_LOG.md` §29: a
  shared class-level failure list would have leaked one manager's failures into another's
  events; the plugin's *id* was being passed to the permission layer as the ACTION, so
  `recorder` was derived from the verb "order" as CRITICAL and every low-risk plugin was
  denied; the rejection path created its failure coroutine without awaiting it; and a
  DENIED plugin could never be enabled later because the walk back to the security gate
  did not include that state
- Verified against the phase's own specification by driving each clause through the real
  manager and the demo (`tests/test_plugin_sdk_verification.py`, 30 tests), which found
  twelve more defects — all fixed and pinned by the test that caught each one, recorded in
  `DEVELOPMENT_LOG.md` §30: a plugin whose declarations could not be read (empty version,
  `capabilities = None`, a tool with no schema) RAISED out of `register()` instead of
  becoming a rejected record; discovery could overwrite a working plugin's record with a
  rejection when two names slugged to one id, and a second plugin claiming a taken id
  silently replaced the record; tools and capabilities were recorded only on success, so a
  failure on the second one left the first registered with nothing to withdraw it; unload
  dropped the plugin instance, making the UNLOADED state `load()` accepts unreachable
  ("the plugin was rejected before loading"); `configure()` refused only ENABLED plugins,
  so configuring a loaded or disabled plugin silently did nothing; `required_capabilities`
  was declared and never checked; a configuration default contradicting its own declared
  type became a load failure instead of a rejected schema; a `plugin.json`/code version
  mismatch and a code-declared configuration field the manifest never declared were both
  accepted; `BrowserPlugin` raised out of a tool on an out-of-range port; and
  `NovaControlApplication.stop()` left plugins running after the app stopped
- Two more defects were found later, in the cross-phase pass of §37: `enable()` did
  not accept the DISABLED state, so the `enable again after disable` transition the
  lifecycle guide draws was a silent no-op, and `_withdraw` took back a disabled
  plugin's tools, catalogue entries and capabilities but left its permission
  declarations on file — the same stale state the phase's own rationale rejects
- Gates: **1847 passed / 12 skipped** (1686 subtests), mypy clean in both platform views
  (229 modules), `docs/API.md` in sync, ruff at or below the pre-existing baseline on
  every touched file

## Completed Local Knowledge & Project Awareness (Phase 11)

- Implemented the pipeline as the shape of the code: **ingest → chunk → index →
  retrieve → rerank → context**. `extract.py` reads text, Markdown, documentation
  and code directly (encoding fallbacks, a NUL-byte binary sniff) and PDFs through
  whichever of `pypdf`, `PyPDF2`, `PyMuPDF`/`fitz`, `pdfminer.six` is installed —
  falling back to a standard-library reader that decodes Flate content streams and
  pulls text-showing operators — with an unreadable document reported as skipped and
  a reason rather than indexed as garbage
- `chunking.py` respects the structure that exists (Markdown headings, top-level code
  definitions, prose paragraphs), carries the heading PATH a chunk needs to be
  understood, and removes the heading LINE it would otherwise repeat — the fix that
  stopped every excerpt (and its token cost) from being doubled
- `index.py` fuses BM25 (always available, no model, no network) with optional
  embedding vectors over the same chunks, reports every hit's evidence (`terms:`,
  `bm25=`, `embedding=`), refuses to score a query against vectors from a different
  backend, and updates strictly per source, so re-ingesting one file never rebuilds
  the index and a stale chunk can never be retrieved
- Local-first and model-free by construction: the default backend is the
  dependency-free hashing embedder, `use_embedding_model` accepts any `Embedder` and
  REBUILDS the stored vectors so two backends never mix, `release_embedding_model`
  gives it back, and a provider that fails or has no model is treated as
  unavailable — the lexical signal answers regardless
- Sources carry `source_id`, `source_type`, `path`, `project`, `content_hash`,
  `version`, `chunk_count`, `bytes`, `indexed_at` and `metadata`; chunks carry
  `<source_id>:<ordinal>`, their line range and heading. Identical bytes are
  UNCHANGED for the cost of one hash, an edit replaces exactly that source's chunks
  and bumps its version, and a taught article joins the same index as a NOTE source
- `ProjectContext`/`ProjectDetector` read the active project — root, repository,
  branch, language, framework, configuration, recent files, recent errors, test
  status, active task — from the workspace and write nothing (asserted by comparing
  the tree byte-for-byte before and after a detection)
- The context budget spends itself best-first with source priority, recency,
  near-duplicate detection and a per-source cap, cuts a first hit that does not fit
  rather than dropping it, and reports `tokens`/`budget`/`considered`/`omitted`/
  `truncated` so a short answer cannot be mistaken for a complete one
- Reachable three ways: the `knowledge_search`, `knowledge_ingest` and
  `project_context` tools (the two that read the disk declare `filesystem:read`),
  the `KnowledgeModule` request/reply events, and `search_knowledge` /
  `ingest_knowledge` / `project_context` / `knowledge_context` on the application;
  the index persists with the rest of the state and is restored on boot
- The typed vocabulary gained `knowledge.indexed` and `knowledge.retrieved` (with
  validated payloads), completing 24 lifecycle events
- 52 tests in `tests/test_knowledge_rag.py` walk the phase clause by clause: ingesting
  a typed tree, PDF and skip paths, metadata, duplicate prevention, incremental
  re-indexing, project detection and the no-writes guarantee, ranking by project and
  kind, deduplication, the per-source cap, a tight budget, the no-embeddings
  fallback, the tools, the module's correlated events, persistence and the
  application wiring
- Defects found by driving the phase through its own code and fixed: a branch named
  `feature/timeouts` was reported as `timeouts` (the last path segment instead of the
  ref minus its namespace); a chunk cut to fit the budget kept its ORIGINAL token
  count, charging the budget for text that was never sent; a Markdown block repeated
  its own heading line under the path prefix; and `render_context` read `path` off
  the chunk instead of the hit, which is an `AttributeError` the first time a hit
  has no source
- Gates: **1899 passed / 12 skipped** (1686 subtests), mypy clean in both platform
  views (237 modules), `docs/API.md` in sync, ruff at or below the pre-existing
  baseline on every touched file

### Phase 11 verified against its own specification

- `tests/test_knowledge_rag_verification.py` (45 tests) drives every clause of the
  phase through the real manager, index, detector, tools, module, brain and
  application: the named pipeline stage by stage, every source kind (text,
  Markdown, documentation, code, note, and a PDF read by the standard-library
  reader on an installation with no PDF library), the local-first rules (no model
  at boot, a backend that refuses to embed loses only its own signal), the tracked
  source fields, duplicate prevention, incremental updates including deletion,
  the eleven project fields the phase lists, the worked example end to end, the
  no-writes guarantee, ranking by project and kind, deduplication, the budget,
  and the last arrow — the context reaching the model
- Eight defects found and fixed, each pinned by the test that caught it:
  **project-scoped retrieval filtered CHUNK ids against SOURCE ids**, so a scoped
  search returned nothing at all; deduplication ran BEFORE the priority weighting,
  so a duplicate from another project could knock out the active project's copy;
  the project header of a context was neither budgeted nor counted, and per-hit
  labels were not charged, so `tokens` under-reported what was sent; a directory
  ingest stopped silently at its file cap; a missing or blank path was reported as
  an unsupported file type (and `Path("")` is the current directory, so the naive
  reading of a blank argument was "index this entire tree"); `search`'s docstring
  promised deduplication it did not perform; and a **deleted** file's chunks
  stayed retrievable forever
- Added `prune_missing(root)` — the other half of an incremental update — plus
  `ingest_path(..., prune=True)`, `max_files_per_tree` with a REPORTED cap, the
  `PRUNED` ingest status, and the `CONTEXT → LLM` arrow (`BrainRequest.knowledge`
  as its own labelled system message, and `answer_with_knowledge(question)`)
- Gates after the fixes: **1944 passed / 12 skipped** (1686 subtests), mypy clean
  in both platform views (237 modules), `docs/API.md` in sync, ruff clean on the
  new files

## Completed Specialized Agents (Phase 12)

- The phase's two rules are the same rule, and the implementation says so: **one
  pipeline, many specialists**. `agents/pipeline.py` is the only loop a specialist
  runs — **NLU → Context → Decision → Planner → Tool Selection → Execution →
  Verification → Recovery** — and a specialist is a set of stage hooks on it
  (`decide`, `plan`, `execute`, `verify`, `recover`, `report`). Interpretation,
  context assembly, permission checks, the execute/verify/recover loop, event
  publication and the verdict belong to the pipeline, so two specialists cannot
  drift apart in how careful they are
- No new framework, and no new components: NLU is `TaskInterpreter`, Context is the
  Phase 11 knowledge engine (`context_for`) plus the specialist's own cheap local
  facts, Tool Selection is the Phase 8 `PermissionManager` plus the build's approval
  gateway plus the live tool registry, Verification reports the Phase 8
  `VerificationResult` vocabulary and Recovery the `RecoveryStrategy` one, and a run
  publishes the Phase 9 typed lifecycle events (`task.started`, `intent.detected`,
  `context.resolved`, `decision.created`, `plan.created`, `tool.*`,
  `verification.*`, `recovery.*`, `task.completed`/`failed`) with the request's
  correlation id
- Every run records all eight stages **in order**, including a run that ends early,
  so a stage that FAILED is never confused with a stage that never happened; a stage
  with nothing to do is `SKIPPED` with its reason, and a survivable failure (an index
  still building) degrades to SKIPPED instead of failing the run. A hook that raises
  is contained: the stage is recorded FAILED and the run still ends with a report
- **Success is never claimed without verification.** The pipeline refuses to report
  COMPLETED unless a step was verified PASS, the verdict comes from where each step
  ENDED UP rather than from every attempt (a retry that worked is a success with a
  story), and a step that completed but produced nothing observable is INCONCLUSIVE,
  never a pass
- **The developer agent** (`DeveloperAgent`, role CODING) covers the phase's whole
  responsibility list: `SelfImprovementEngine.inspect()` for repository inspection,
  `ProjectDetector` plus a bounded top level for project structure, a local lexical
  code search, the bug log and project context for open errors (an unreadable log
  costs only the errors), `SelfImprovementEngine.plan()` for a proposal that states
  `writes_require_approval`, `apply_changes()` for a write and a command runner for
  the tests, both behind the risk layer, failure reproduction plus name-not-guess
  diagnosis, read-only-by-default git, and verification of what was written by
  comparing the bytes on disk with the proposal plus the test tally
- Test output is counted, not eyeballed: pytest (`3 passed, 1 skipped`) and unittest
  (`Ran 5 tests` + `OK (skipped=1)`) are both parsed from the run's own output, and a
  run with **no tally at all** is not a pass — that is the quiet failure mode where a
  collection error exits zero and an agent reports success after running nothing. A
  debug run inverts the expectation deliberately, and a suite that passes when a
  failure was reported is verification FAIL
- Recovery is bounded and reasoned: one retry for a transient test failure, ABORT for
  a missing dependency (retrying cannot install it), for a failed write (a
  half-written file is not a state to re-run into) and for a repository command, with
  a finite retry budget and a separately capped execution queue
- **Git operations** are read back word by word instead of guessed from the verb:
  flags, revisions, paths, URLs and the remote/branch names the workspace actually
  has (read from `.git/config`, `.git/HEAD`, `.git/refs/**` and `packed-refs`, no
  subprocess) are passed as argv, quoted values are kept whole, English glue is
  recorded as UNUSED and reported, and a word that cannot be shown to be a git
  argument is UNREADABLE — which refuses a **mutation** unrun rather than reshaping
  it into a different command
- **The research agent** (`ResearchAgent`, role RESEARCH) covers its list: the query
  frame for the question, web-vs-local source selection (honouring `allow_web` and an
  offline request), the **existing** `ExploreService` for search, `build_evidence`
  over the page text it already read, per-source comparison with a cross-source
  agreement figure, conflict detection, claim-based synthesis, 1-based citations and
  the evidence/inference labels. Conflicts are only reported where two sources
  demonstrably discuss the same thing (vocabulary overlap by Jaccard or containment)
  and disagree in a checkable way (negation, figure, antonym); `verify_citations`
  fails the step on an evidence claim without a citation, a citation index with no
  matching source, or an inference with no basis, so a fabricated reference cannot
  survive
- Both specialists register in the SAME `AgentRegistry` the coordinator, the agent
  bus and the chat path already dispatch through, `prefer=True`, replacing the
  deterministic stub for their role while the stubs stay for the roles nobody
  implements. Authorization is a per-RUN pipeline (`agent.clone(pipeline)`,
  `developer_task(goal, authorize=True)`), never a flag on shared state
- Surfaces: `developer_task` / `research_task` / `specialist_agents` on the
  application, the coordinator and `AgentModule`, and
  `python -m novacontrol demo phase12_agents`. New doc: `docs/SPECIALIST_AGENTS.md`
- 63 tests in `tests/test_specialist_agents.py` cover the phase's nine required areas
  (routing, project context, repository inspection, test execution, failure recovery,
  research routing, source handling, citation preservation, permission enforcement)

### Phase 12 verified against its own specification

- `tests/test_specialist_agents_verification.py` (76 tests) walks the requirements
  text and drives every clause through the real specialists, asserting the MECHANICS
  of each stage rather than that a stage is named: NLU really is the interpreter,
  Context really is the Phase 11 engine (and degrades when it breaks), Tool Selection
  really is the risk layer reading the agent's own declarations and refusing a tool
  this build does not have, Verification really is the Phase 8 vocabulary, Recovery
  really is the strategy vocabulary, every stage is recorded even when a run stops
  early, and the events arrive in the phase's order with the request's id
- Eleven defects found across the implementation pass and this one (a twelfth — a
  skipped step making the execution stage read FAILED while the run completed — came
  from the cross-phase pass of §37), each pinned by the test that caught it.
  **Git arguments were silently discarded**: `git diff HEAD~1 --
  src` ran a bare `git diff`, `git remote add …` ran `git status`, and
  `git branch -D feature/x` — whose verb was on a read-only list — was classified as a
  **read**, which is the dangerous shape: a deletion the classifier called a read. A
  step that never ran was reported as a **verification FAILURE** while recovery, for
  that same step, said there was nothing to recover — two stages disagreeing about one
  non-event; it is now INCONCLUSIVE with a SKIPPED verification stage. "is X faster
  than Y?" was not read as a comparison, so the run reported agreement it never
  measured. A failed `synthesize` was retried although its inputs cannot change. The
  test command was hard-coded to pytest, so "run the tests" in a Node project ran a
  command that cannot exist (the runner is now recognised from the project — npm, or
  this interpreter's pytest — and the application's own helper delegates to the same
  function). And an agent's own refusal was read back as the diagnosis of a failure:
  a refused debug run now records its `diagnose` step SKIPPED with "no failing output
  was recorded to diagnose" and the report carries no `diagnosis` key.
  Earlier in the phase: a recovered run was still reported FAILED, the
  `src/<package>` layout was hard-coded to this repository's own `src/novacontrol`, a
  comparison frame needed two nouns, and a skipped step was retried
- Also fixed on the way: the configured test command was round-tripped through a
  display string and re-split, which breaks the moment a path contains a space, and
  the comment on the write path's second approval gate claimed a default that
  refuses when it deliberately mirrors the gate that already decided
- All **four CI checks** confirmed locally on the frozen tree, exactly as the
  workflow runs them: `pytest tests/ -q` — **2082 passed / 12 skipped** (1773
  subtests); `python scripts/generate_api_reference.py --check` — `docs/API.md` in
  sync; `python -m mypy src` and `python -m mypy src --platform win32` — clean in
  both platform views (240 modules). Ruff (not a CI gate) is clean on the new
  files, with the pre-existing E501 baseline elsewhere untouched

## Completed Scheduler, Automation and Audit Trail (Phase 13)

- `automation/` owns **WHEN** a stored request runs and performs nothing itself. An
  `AutomationEngine` stores one request plus a `Schedule` and hands the text to an
  injected runner — in this application `handle_request` — so a scheduled task
  travels the same intent → decision → plan → tool selection → permission →
  execution → verification path a spoken request does. There is no second execution
  path and no way for the scheduler to act on its own
- `Schedule` is a frozen value with a `ScheduleKind` (`once`, `interval`, `daily`,
  `weekly`) whose constructor refuses ambiguous combinations, and `next_after`
  always returns a strictly **future** occurrence: a machine asleep over a daily
  job's hour gets ONE next run rather than a burst of catch-up runs, and intervals
  compress missed occurrences the same way. `parse_schedule` returns `None` when the
  wording states no time — a request that merely *mentions* a schedule is never
  auto-executed, and creation still travels the whole pipeline — and `strip_schedule`
  removes the scheduling phrase from the request that will later be RUN, so a run
  cannot re-schedule itself
- Only approved tasks are armed: creation stores `pending_approval` and disarmed,
  `approve` is the only way in, and `enable` refuses a task that was never approved.
  The permission layer is asked at the moment of the run (under the declared HIGH
  `automation.run` scope), not only when the task was armed, and with no permission
  layer wired the run is **refused** — "no gate" is not permission
- A `denied` run or an unmet **named** condition is not a failure and does not
  increment the failure count; a denial disarms the task instead of retrying into
  the same refusal. Failures are counted and bounded: at the configured limit the
  task disables itself, and a runner that raises is a recorded failure rather than a
  crashed loop. Conditions are named checks (`always`, `path_exists`, `tests_pass`),
  never stored code
- The tracked state is exactly what the phase asks for — `automation_id`, request,
  schedule, status, next and previous execution, failure count, enabled,
  permissions, created timestamp — plus a bounded run history, and it survives a
  restart
- `audit/` records one request or one scheduled run end to end: task id, timestamp,
  request, automation id and source, route, intent, the decision, the plan joined to
  what each step ended up as, tools, actions, verification (what was checked *and*
  what was not), failures, recovery, model, provider, latency, resources and the
  permission decisions — and **no chain-of-thought**, because a free-text reasoning
  field would be the largest privacy surface in a long-kept, widely-read file. A
  test pins that absence
- Privacy is a property of the code path: redaction happens **on the way in**
  (private-key blocks, `Authorization` headers, bearer tokens, JWTs,
  `sk-`/`ghp_`/`xox`/`AKIA` credentials, URL-embedded credentials, `key=value`
  assignments, and secret-named mapping keys whose values are hidden whatever they
  look like), prose is deliberately left alone, the sink is local-only and the
  logger **refuses** a remotely-declared one, and retention is bounded by a period
  *and* a hard cap with explicit `delete`/`clear`. An unreadable timestamp is kept
  rather than deleted. Writing a row is best-effort: it can never fail a request
- Surfaces: application methods (`schedule_automation`, `approve_automation`,
  `cancel_automation`, `enable_automation`, `disable_automation`, `run_automation`,
  `run_due_automations`, `automation_status`, `audit_status`, `audit_entries`,
  `audit_prune`, `audit_delete`, `audit_clear`, …), plan actions (`schedule_task`,
  which stores a task disarmed unless the step is approved, and `run_automation`,
  which re-checks the run permission), ten new typed lifecycle events, a
  background ticker started by `start()`/stopped by `stop()` that ticks only after
  its sleep, and thirteen new HTTP routes (81 total, `docs/API.md` regenerated)
- Verified by driving every clause through the engine, the application, the HTTP
  surface and the request path (`tests/test_automation.py` 50 tests,
  `tests/test_audit.py` 25 tests, plus `AutomationAuditApiTests` in
  `tests/test_web_api.py`). The live probe found and fixed two real defects: the
  scheduled run read its own state from `response.data`, an attribute
  `ApplicationResponse` does not have, so every run failed with an
  `AttributeError` ("the call returned" never became a verified outcome) — the run
  now reads `response.payload`; and `POST /settings` never forwarded
  `automation_enabled` or the audit retention settings, leaving
  `apply_audit_settings` with no caller, so the two settings the phase adds were
  unreachable over HTTP and a changed retention policy was not picked up until a
  restart. Both settings now round-trip through `POST /settings` and take effect
  live. A test drives a spoken schedule *mention* through `handle_request` and
  pins that it is planned and stored pending, never executed, with the audit row
  filed as a request
- All **four CI checks** confirmed locally: `pytest tests/ -q` — **2167 passed / 12
  skipped** (1813 subtests); `python scripts/generate_api_reference.py --check` —
  `docs/API.md` in sync (81 routes); `python -m mypy src` and
  `python -m mypy src --platform win32` — clean in both platform views (246
  modules). Ruff (not a CI gate) is clean on the new files, with the pre-existing
  E501 baseline elsewhere untouched

## Completed Optimization, Resource Governance and Self-Diagnostics (Phase 14)

Phase 14 makes the build adaptive to the machine it actually runs on (the
reference machine: 16 GB RAM, an 8 GB Intel Arc, an Intel NPU, Ollama), using
runtime telemetry rather than assumptions. What was built, clause by clause:

- **14.1 Model benchmarking** (`optimization/benchmark.py`): `ModelBenchmarking`
  records one `BenchmarkRecord` per task — first-token latency, tokens/sec, total
  latency, RAM used, GPU utilization, GPU memory, NPU use where available,
  structured-output success, task success, tool-selection accuracy and failure —
  and a runner that raises produces a FAILED record rather than a missing one,
  because the failure rate is one of the figures. `compare(category)`/`best_for`
  read the measurements; **no model is hard-coded as best**, and an unmeasured
  rate is `None`, never `0.0`. Storage is JSONL with a configured cap.
- **14.2 Execution modes** (`optimization/models.py`): `LOCAL_ONLY`, `BALANCED`,
  `PERFORMANCE` as policy values, with the middle one as the fallback for an
  unrecognised spelling.
- **14.3 Privacy policy** (`optimization/privacy.py`): ONE `PrivacyPolicy` with
  `allow_cloud`, `allow_external_search`, `allow_external_tools`,
  `allow_telemetry`, `allow_remote_model` and `sensitive_data_redaction`, each
  decision carrying the single control that decided it. Enforced at the ONE
  place the cloud provider is selected (`NovaBrain.set_cloud_allowed`, so
  `set_mode("cloud")` lands on the local brain when the slot is closed) and at
  the research entry (web search closed → the run degrades to local knowledge).
- **14.4 Resource governor** (`optimization/governor.py`): AMPLE/TIGHT/CRITICAL
  from free RAM, CPU, GPU utilization, GPU-memory pressure, temperature, battery
  and the resident models, with the figure behind every escalation.
  `advise_load` answers yes/no/unknown and lists what to unload; thresholds are
  configuration (`resources:` in the config file), not constants. The two
  specification examples hold: 2.5 GB free → TIGHT, prefer lightweight, release
  inactive models; high GPU-memory pressure → a vision model's load is refused
  by the fit check.
- **14.5 Model loading policy** (`models/manager.py`): the governor is injected
  as the manager's load advisor; lazy loading, keep-alive with idle eviction,
  priority-ordered eviction, a `max_resident_models` limit, and a model an
  active task is using is never evicted underneath it.
- **14.6 Task cost estimation** (`decision/cost.py`): `TaskCostEstimator`
  returns a five-band estimate with reasons, model/cloud/GPU requirements,
  estimated RAM, expected latency and tool calls. The specification's examples
  are the acceptance test — RAM usage → trivial, a large PDF → medium, build and
  test a full application → very high — and `route_hint` is a preference, never
  a gate. The estimate is attached to the decision's metadata, which the audit
  trail already stores, and a test reads it back from the audited row.
- **14.7 Self-diagnostics** (`diagnostics/manager.py`): `DiagnosticManager`
  returns one structured result per component (component, status, severity,
  message, remediation, metadata) for the whole roster the specification names,
  with `skipped` for a deliberately-off component and `unknown` for a probe this
  platform cannot answer — the NPU reports `unknown` with its reason rather than
  claiming an accelerator it cannot see. A check that throws, times out or
  returns the wrong type becomes a FAILING row; overall state is the worst that
  matters; src checks run off the event loop.
- **API**: seven new routes (`GET`/`POST /privacy`, `GET /resources`,
  `POST /cost/estimate`, `GET /diagnostics` with `?only=`, `GET`/`POST
  /benchmark`), and `POST /settings` now forwards the execution mode and the
  five controls and re-applies the policy live.
- **Tests**: `tests/test_optimization.py` (75 tests / 5 subtests) plus
  `Phase14ApiTests` in `tests/test_web_api.py` (10 tests).
- **Verified clause by clause through the real code after the build**, which
  found and fixed six gaps: Explore kept a cloud synthesis provider after the
  mode closed it (Chat went local, Explore did not) — `_apply_execution_mode`
  now re-syncs it and `set_cloud_allowed` falls back to the boot provider or
  Echo rather than leaving the cloud provider active under a relabelled mode;
  Explore and the trending pool searched externally regardless of
  `allow_external_search` — both now sit behind the policy's one predicate and
  perform no call at all when it is closed; `allow_external_tools` had no
  enforcement point — it is now enforced in `_run_plan_step` against the step
  tool's own declaration; `allow_remote_model` had no enforcement point — a
  configured remote decision provider is now wrapped in
  `PrivacyGatedDecisionProvider`; privacy decisions emitted no events —
  `privacy.mode_changed` and `privacy.denied` are now published on the one
  bus; and the resource report lacked model memory estimates while the
  diagnostic timeout was a constant — both are now present and
  configuration-driven.
- All **four CI checks** confirmed locally: `pytest` in three file groups —
  **2254 passed / 12 skipped** (1832 subtests); `python
  scripts/generate_api_reference.py --check` — `docs/API.md` in sync (88 routes);
  `python -m mypy src` and `python -m mypy src --platform win32` — clean in both
  platform views (253 source files). Ruff (not a CI gate) is clean on the new
  files, with the pre-existing E501 baseline elsewhere untouched. Details in
  [docs/OPTIMIZATION.md](OPTIMIZATION.md).
- **Phases 8–14 verified end to end across their boundaries** against one live
  application (approval → execution → verification → recovery; event isolation
  and the capability registry; a plugin enabled, gated by the risk layer and
  withdrawn; ingest → cited retrieval and project awareness; the specialists'
  eight stages including the LOCAL_ONLY research path; a scheduled automation
  through the ordinary request path and its audit row; and the whole Phase 14
  surface plus a restart). Three defects found and fixed, each pinned by a test:
  a skipped step made the execution stage read FAILED, a fully denied plan's
  execution stage claimed the run never reached it, and a disabled plugin could
  neither be enabled again (the lifecycle the plugin guide draws) nor give back
  its permission declarations. See §37 of
  [docs/DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md).

## Next Work

Phase 13 onward: making this architecture carry real work — external adapters, packaging,
and wiring the understood-but-unexecuted file operations (`find_file`, `read_file`,
`list_files`, `write_file`) so "read report.pdf" reads the file instead of apologising.
The specialists are the natural callers for those operations: the developer agent
already declares what it may touch, so a `read_file`/`write_file` step is a declaration
and a handler, not a new permission model. The bus is the seam for a live UI: the events
already carry correlation ids, so a panel can subscribe to one request's whole thread
instead of polling — and a run's eight recorded stages are exactly what such a panel
would show. The capability registry can back a real capability browser and a "why can't
you do that?" answer, using `report()` and `availability_of()` as they stand.

The knowledge engine is the newest of those seams: the tools and the bus already answer
"what do we know about X", "read this in", "where am I", and the budgeted context is
produced but not yet fed into a chat answer automatically, nor is there a Knowledge panel
to ingest a folder and show what a query retrieved and why.
