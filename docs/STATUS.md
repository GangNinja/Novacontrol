# Project Status

Current phase: **Phase 26 — Generalization, ARC-style Reasoning & Intelligence Evaluation**

Status: **Complete**

> The original baseline ended at Phase 15; the staged build that followed it — what
> it verified, what it changed, and what remains — is recorded below under
> "Completed Staged-Build Verification (Phases 1–7)" and the Phase 8 … Phase 24
> sections that continue it.

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

## Completed Data Collection, Evaluation and Reward Foundation (Phase 15)

**What it delivers.** `evaluation/` records what the system did, scores it, and
puts a weighted price on it — without becoming a second execution path and
without training anything.

* **`models.py`** — a versioned `AgentTrajectory`: identity, request, structured
  intent, decision, plan, execution steps, tool calls, observations,
  verification, recovery, final result, outcome, model information, latency,
  resources, feedback, reward and quality. Structured only: there is no field for
  hidden reasoning, every field is optional with a usable default, and an
  unmeasured figure is `None` rather than `0.0`.
* **`recorder.py`** — `TrajectoryRecorder` observes the EXISTING `EventBus`
  lifecycle and groups it by correlation id: bounded in-flight drafts, a small
  ring of finished rows so a late annotation finds one, redaction on the way in,
  a switch, and failure isolation for both the observer and the sink.
* **`quality.py`** — `DataQualityFilter` returns ACCEPTED / REJECTED /
  NEEDS_REVIEW with structured reasons (presence, outcome, serialisability,
  schema, empty result, contradictions, failed verification, malformed calls,
  refusals kept as safety evidence, residual sensitive text, noise, duplicates)
  and never deletes a row. Every line it draws is `QualityConfig`.
* **`evaluator.py`** — nine dimensions scored separately (NLU, decision, tool
  selection, planning, execution, verification, recovery, safety, efficiency),
  each with findings and figures, `unknown` where evidence is missing, plus
  false-success / false-failure detection and golden expectations.
* **`reward.py`** — `RewardEngine` + `RewardConfig` + `RewardResult`: weighted
  observable components minus penalties, with per-factor reasons, a version, a
  normalised total and an explanation. A refusal is never penalised.
* **`datasets.py`** — versioned `GoldenDataset`/`GoldenExample` and the small
  deterministic built-in set (`novacontrol-core` 1.0.0, six examples).
* **`storage.py`** — JSONL repositories (the audit trail's own pattern) with
  upsert-by-id, caps, retention, and filtering by task, model, date and status;
  replaceable stores, in-memory included.
* **`metrics.py`** — the aggregate figures from stored rows: success, failure,
  verification, tool selection, planning, recovery, safety interventions,
  retries, average reward, latency with p50/p95, fast path %, LLM escalation %,
  quality distribution and the nine dimension means.
* **`service.py` / `runtime.py`** — the order the pieces run in, and the bus seam
  that attaches the recorder (`EvaluationModule`) with correlated replies to
  `evaluation.*_requested`.
* **Read-only API** — `GET /evaluation/summary`, `/evaluation/trajectory/{id}`,
  `/evaluation/metrics`, `/evaluation/rewards` (four routes; 92 in the surface).
* **Settings** — `evaluation:` config section (switch, thresholds, retention,
  reward weights) and the user-facing `evaluation_enabled` /
  `evaluation_retention_days` / `evaluation_max_records`, applied live from
  `POST /settings`.

**Not implemented, on purpose:** preference optimisation and reinforcement
learning — DPO/ORPO, RLHF/RLAIF, RLVR and agentic RL. Phase 15 is the data, the
evaluation and the reward foundation those phases would consume; it changes no
weights, no prompts and no behaviour. (Phase 16 adds supervised fine-tuning
*infrastructure* on top of it; see below.)

The pass is documented in [docs/EVALUATION.md](EVALUATION.md) and §38–§39 of
[DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md), and pinned by `tests/test_evaluation.py`
(108 tests / 6 subtests).

## Completed Supervised Fine-Tuning (Phase 16)

Phase 16 in one line: it turns the Phase 15 record of what NovaControl DID into
supervised fine-tuning datasets, runs a trainer against them (a dry run by
default), checkpoints as it goes, evaluates the result against the base model,
and registers the candidate as an EXPERIMENTAL model that only an explicit,
evidence-backed approval can promote. It is documented in
[docs/TRAINING.md](TRAINING.md) and §40–§41 of
[DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md), and pinned by `tests/test_training.py`
(259 tests).

**Datasets** — `SFTDatasetBuilder` builds seven dataset types (`nlu`, `decision`,
`tool_selection`, `planning`, `recovery`, `developer`, `research`), one target
schema each, from accepted trajectories. Eligibility is one `SelectionRules`
table (quality verdict, success, verification must exist and pass, minimum reward
or dimension score, model, task category, source, tags, date window, row cap,
developer/research scope). Every refusal has a name — `quality_not_accepted`,
`not_successful`, `verification_failed`, `below_min_reward`, `duplicate`,
`residual_sensitive_data`, `hidden_reasoning`, `missing_structured_data`, … — and
the counts stay on the version. `SFTTrainingExample` is a structured input and a
structured target, with `metadata` free-form and round-tripping exactly, so an
importer can extend it without a schema change.

**Splits and versioning** — `SplitConfig` defaults to 80/10/10 with `seed=42` and
`group_by="task"`: whole groups go to one split, so a model is never tested on a
task whose other half it trained on. The walk measures each split's shortfall
RELATIVE to its own target, so a handful of large groups cannot starve validation
or test (four groups of ten become 20/10/10, not 30/10/0). Versions are monotonic
and immutable (`nlu@1.0.0 → 1.0.1`), and `validate()` returns every reason a
dataset must not be trained on rather than an `ok` nobody can act on.

**Configuration and resources** — `TrainingConfig` is the one validated place
every trainer parameter lives: an out-of-range value is an error rather than a
silent clamp, and a typo that cannot be read keeps the default (parsing is not
validation). `ResourceEstimator` measures the component breakdown (weights,
adapter or gradients + optimizer, activations, the dataset, checkpoints) against
this machine's real memory reading and returns SAFE / WARNING / UNSAFE with its
reasons — and an UNSAFE estimate is a refusal: `start` returns `ok=false,
refused=true` and does not move the run unless the deployment allows an override
AND one was asked for. An explicit hardware policy that names a device this
machine does not have says so instead of quietly becoming a CPU run.

**Backends** — `SFTTrainer` is a small library-free interface the orchestrator
drives (so a backend cannot smuggle in a second scheduler, store or lifecycle),
with `TrainingCallbacks` as the way back. `DryRunTrainer` needs nothing installed
and writes real checkpoints; `PeftLoraBackend` is the isolated PEFT/LoRA boundary
that probes for `torch`/`transformers`/`peft` without importing them and explains
what is missing. The concrete Transformers/PEFT loop is deliberately not shipped
— it would be untestable on a 16 GB machine with no CUDA device, and untested
code in a training path is worse than a clear boundary. `model_metadata()` is what
a backend says it produced, so a real adapter's path reaches the registry instead
of the registry guessing from the configuration.

**Runs and checkpoints** — nine run statuses (`created`, `validating`,
`preparing`, `running`, `paused`, `evaluating`, `completed`, `failed`,
`cancelled`), with `create_run` having no side effects at all and a real
(non-dry-run) start requiring confirmation. `CheckpointManager` writes atomically,
validates by re-reading the file, reports `complete`/`incomplete`/`corrupt`,
finds the best and the resume point, and applies retention.

**Evaluation and the registry** — `TrainingEvaluator` compares base against
candidate on the held-out split with per-metric deltas, `LOWER_IS_BETTER`
latency/memory, a noise floor so jitter cannot fail a candidate, and a verdict of
`pass` / `regress` / `inconclusive` — never `pass` when nothing usable was
measured. `SFTModelRegistry` enforces the six statuses and the explicit
transition table, requires a recorded passing evaluation before approval, demotes
the previous production model on promotion (with a rollback point), auto-rejects
a regression, and keeps a bounded `{from, to, reason, at}` history. The base model
and the adapter are separate fields.

**Surfaces** — 25 `/training/*` routes, a 24-action `novacontrol training` CLI, a
`training:` config section plus five user settings and
`NOVACONTROL_TRAINING_*` environment overrides, eight `training.*` events, and a
`Training` diagnostics row. `start`, `resume` and `evaluate` run in a worker
thread, so the API keeps answering while a real run trains.

**Not implemented, on purpose:** the concrete Transformers/PEFT training loop,
DPO/ORPO, RLHF/RLAIF, RLVR, agentic RL, distributed training, and any evaluation
that approves a model on its loss curve.

## Completed Preference Optimization (Phase 17)

Phase 17 in one line: it turns the pairs the system has already **observed** into
versioned preference datasets, runs DPO or ORPO against them (a dry run by
default), and lets a model improve only where a measured behaviour comparison
says it improved. It is documented in [docs/PREFERENCE.md](PREFERENCE.md) and
§42–§43 of [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md), and pinned by
`tests/test_preference.py` (167 tests). It adds no second store, no second
registry, no second trainer interface and no reinforcement learning.

**Pairs from observation, never invention** — `PreferenceDatasetBuilder` builds
six families (`nlu`, `decision`, `tool_selection`, `planning`, `recovery`,
`response`) from verified outcomes (one run verified, the other not), structured
corrections, evaluation margins beyond a minimum, golden-fixture disagreements
(judged only on the fields the fixture asserts — a run that did what was
expected has no pair), human review, and marked teacher/synthetic sources. Both
sides are observable behaviour; the same two candidates the other way round are
a contradiction that neither orientation trains on; duplicates are counted and
dropped; and a row carrying hidden chain-of-thought is refused **before a pair
exists**, never trimmed.

**Quality and provenance** — `PreferenceQualityFilter` classifies ACCEPTED /
REJECTED / NEEDS_REVIEW from one severity table (an `ERROR` rejects, a `WARN`
holds for review unless a person settled the pair), redacts both candidates and
their outcomes on the way in, and rejects residual secrets under the default
policy. Every pair carries its source, its confidence, a three-axis
`PreferenceStrength` (`confidence` / `evidence_quality` / `verification_strength`)
with its `PreferenceEvidence` list, provenance ids and a content
`fingerprint()` that makes a rebuild recognisable and a different version
refusable.

**Versions and splits** — `name@version`, monotonic and immutable, with the
rules, statistics and a `phase17.1` preprocessing stamp on the version.
`validate()` returns every reason a version must not be trained on (unknown
family, a pair in two splits or none, duplicates, identical candidates, hidden
reasoning, missing evidence or provenance, group leakage). Splits are Phase
16's group-safe, deterministic splitter applied to pairs, so "leak-free" has one
implementation in the codebase; `split(name)` returns only ACCEPTED pairs by
default.

**Configuration and resources** — `PreferenceTrainingConfig` validates
`algorithm` (`dpo`/`orpo` only), `beta` in `[0.01, 1.0]` (default 0.1), the
reference model, LoRA/QLoRA and the shared Phase 16 fields, and `dry_run` is
`True` by default. `PreferenceResourceEstimator` reuses the Phase 16 walk and
adds `preference_pairs` (both sides, at twice a supervised example's bytes per
token) and, for DPO only, the **reference model** as a second copy of the
weights — or says plainly that the size is unknown and the figure is a lower
bound. AUTO/CPU/GPU/NPU, no CUDA assumption, and an UNSAFE verdict is still a
refusal.

**Backends and runs** — `DPOTrainer` and `ORPOTrainer` share every step but the
objective; `DryRunPreferenceTrainer` walks a real schedule and labels every
figure `simulated`, a missing dependency or a missing runner is named, and a
supervised dataset handed to a preference backend is refused by name. A run is a
Phase 16 `TrainingRun` with `algorithm` and `preference_metrics`, the same
checkpoints, pause/resume/cancel and worker-thread start.

**Evaluation and the one registry** — `PreferenceEvaluator` compares base vs SFT
vs preference-optimized on held-out pairs with `preference_accuracy`,
`chosen_match_rate`, `rejected_match_rate` and Phase 16's metrics, deltas, noise
floors and blocking regression areas; a loss is never consulted
(`loss_consulted: false`), and without two predictors the run is `skipped` and
approval stays impossible. A payload that is not a predictor is reported as
`inconclusive` rather than crashing the route. Approval, promotion and rollback
are the Phase 16 registry's own explicit transitions.

**Human review** — a queue the API serves: a person can submit a pair, or decide
`choose_a` / `choose_b` (B swaps the sides) / `tie` / `reject`; both refusals
need a reason, hidden reasoning is never queued, and only **settled** pairs are
pulled into a later build.

**Surfaces** — 29 `/preference/*` routes, a 26-action `novacontrol preference`
CLI that dispatches to the application's own methods, a `preference:` config
section plus five user settings and `NOVACONTROL_PREFERENCE_*` overrides, five
`preference.*` events on the existing bus, a runtime module that answers
questions and starts nothing, and a 23rd diagnostics row (`Preference
optimization`).

**Not implemented, on purpose:** RLHF/RLAIF, RLVR, critique learning, agentic
RL, distributed training, and the concrete Transformers/PEFT loop (the adapter
boundary is where it lands). Tests use mocks, synthetic rows and dry runs — no
model is downloaded and no GPU is required.

## Completed Reinforcement Learning from Human and AI Feedback (Phase 18)

Phase 18 in one line: it turns the feedback the system already receives — a
person's verdict and an evaluator's structured rating — into a reward with
provenance and an integrity verdict, a versioned reward dataset, a simulated
rollout and an experimental policy update that only a measured comparison can
approve. It is documented in [docs/RLHF.md](RLHF.md) and pinned by
`tests/test_rlhf.py` (171 tests). It adds no second store, no second registry, no
second trajectory format and no second event bus, and normal NovaControl
operation works with every RL dependency absent.

**Rewards are claims with provenance** — `RewardProvider` abstracts four voices:
human (`HumanRewardProvider`), AI (`AIRatingRewardProvider`), Phase 15's weighted
engine re-used untouched (`EvaluationRewardProvider`), and a weighted composite.
Every `RewardResult` carries source, confidence, evaluator identity, reward
version, component and penalty breakdowns and evidence; the default weights keep
the sources distinguishable (human 1.0 / verifier 0.8 / rule 0.6 / AI 0.4), each
feedback type maps to a signed value, and normalisation keeps the raw total while
keeping a safety penalty its own component with a floor. A provider asked for a
source the run has none of returns a refusal, never a guessed number.

**Rewards are audited before they are taught** — `RewardIntegrityChecker`
returns VALID / SUSPICIOUS / INVALID / NEEDS_REVIEW with named findings (an
unsafe run with a positive reward, a failed run scoring high, length gaming,
repeated actions, reward without verification, without evidence, low confidence,
a disallowed source, two sources disagreeing). `RewardDatasetRules` holds a
suspicious row and refuses an invalid one, and **nothing is deleted** — `held`
lists exactly what was excluded and why.

**Human is not AI** — feedback, ratings, rewards and dataset rows each record
their source; `FeedbackDisagreementDetector` records a `human_vs_ai`
disagreement with `recommended: review` and never picks a winner; an `rlhf`
dataset refuses evaluator-only rows, an `rlaif` dataset refuses human-backed
rows, and a `mixed` dataset keeps both kinds of row.

**The reward dataset** — immutable `name@version` versions built from stored
trajectories, evaluations, rewards, feedback and ratings; every refusal has a
name and the counts stay on the version; splits reuse Phase 16's deterministic,
leak-free group walk; content-fingerprinted and rebuild-stable.

**The optimizer boundary** — `mock_policy` is implemented and does not learn;
`ppo` and `grpo` are named in the vocabulary and refused with "not implemented".
Every simulated figure is labelled `simulated`. `dry_run` is true by default and
creating a run has no side effects; a real run needs the deployment's permission,
an explicit confirmation and a wired runner. `RLResourceEstimator` reuses Phase
16's walk, counts the reference model only when KL > 0, assumes no CUDA, and an
UNSAFE verdict is a refusal.

**Runs and the gate** — a run is Phase 16's `TrainingRun` with the same
checkpoints, pause/resume/cancel and worker-thread start; `create_run` also
audits the dataset (RLHF needs human-backed rows, RLAIF rating-backed) and lists
what failed. `RLModelEvaluator` compares base, candidate, SFT and preference
models on held-out data, the verdict is taken from the worst comparison, and
`reward_metrics_consulted: false` — a higher reward is never the verdict.
Approval and promotion stay the registry's own explicit transitions.

**Surfaces** — 31 `/rlhf/*` routes, a 30-action `novacontrol rlhf` CLI that
dispatches to the application's own methods, a `rlhf:` config section plus five
user settings and `NOVACONTROL_RLHF_*` overrides, seven `rlhf.*` events on the
existing bus, a runtime module that answers questions and starts nothing, and a
24th diagnostics row (`RLHF / RLAIF`).

**Not implemented, on purpose:** RLVR, critique-based learning, RLCD-style
training, agentic RL, game agents, distributed training, and the concrete
Transformers/PEFT loop (the adapter boundary is where it lands). Tests use mocks,
deterministic environments, synthetic rewards and dry runs — no model is
downloaded and no CUDA or GPU is required. The phase stopped here, before
Phase 19, as instructed.

## Completed Reinforcement Learning with Verifiable Rewards (Phase 19)

Phase 19 in one line: it turns a verifier the machine can re-run into the
ground truth of a reward, and turns a verifier's finding into a corrected
example — without touching the architectures Phase 15–18 already own. It is
documented in [docs/RLVR.md](RLVR.md) and pinned by `tests/test_rlvr.py`
(188 tests), and it extends the existing diagnostics roster to 25 rows.

**A reward is evidence, not opinion.** `VerifiableRewardProvider` is a
`RewardProvider` voice (`reward_source="verifier"`) that scores a trajectory by
replaying it through registered, re-runnable verifiers and summing the verified
checks — each contributing a signed term with an evidence trail and the
`verifier_policy_version` that produced it. An un-checked claim contributes
nothing; a human/AI component can still compose in, but it rides beside the
verifiable signal. The Phase 18 `RewardIntegrityChecker` runs unchanged on every
row; RLVR widens what counts as a safety failure (adding `error_category`) and
adds the policy-fingerprint reuse guard.

**Critique earns correction earns examples.** `CritiqueEngine` turns structured
verifier findings into `CritiqueResult`s, `CritiqueCorrector` proposes
corrections, and `CorrectedExampleBuilder` turns a verified correction into a
`CorrectedExample` — verified before acceptance (hidden-reasoning corrections
are refused outright), feeding a later SFT pass. `CritiqueDatasetBuilder` writes
immutable, content-fingerprinted datasets whose `accepted_examples()` train and
whose `held()` never do — a dataset with no accepted example is **refused**
before the run can start.

**Security gates.** `novacontrol.rlvr.security` owns the protections: no
self-reward (`expected_result_changed`), no editing a verifier into passing
(`verification_control`), policy-locked rewards
(`verifiers_changed`/`reward_config_changed`), no hidden chain-of-thought
(`HIDDEN_REASONING_KEYS` discarded on intake, no field for it anywhere), no
secrets (Phase 15 redaction), and `confirmation_required` when a real step is
attempted without explicit confirmation.

**Nothing starts by itself.** `dry_run` is the default, `create` has no side
effects, `mock_policy` is the only shipped optimizer (figures labelled
`simulated`), `ppo`/`grpo` are named and refused as "not implemented",
`RLVRTrainingConfig` rejects a `critique_dataset_version` on `RLTrainingConfig`
where it has no home, and no model is loaded or downloaded — no CUDA or NVIDIA
GPU is required. The RLVR switch is **off by default**; with `rlvr_enabled`
false, mutating actions answer `ok: false` with the reason.

**Surfaces** — 31 `/rlvr/*` routes, a `novacontrol rlvr` CLI dispatching to
the application's own methods, a `rlvr:` config section plus
`NOVACONTROL_RLVR_*` overrides, seven `rlvr.*` request/reply event pairs on the
existing bus (`rlvr.status_*`/`rlvr.verifiers_*`/`rlvr.critiques_*`/`
rlvr.corrections_*`/`rlvr.datasets_*`/`rlvr.pipeline_*`/`rlvr.dry_run_*`), and
the 25th diagnostics row (`RLVR` — SKIPPED when switched off, DEGRADED when the
optional training dependencies are absent, OK when installed).

**Reused** — `RLVRManager` extends `RLHFManager` (which extends the Phase 16
`TrainingManager`); a run is the same `TrainingRun` with `algorithm="rlvr"` and
`RLVREvaluation` carrying the run-scoped verdict. `RLTrainingConfig.from_training_config` projects a Phase 16 caller's configuration onto the RL schedule, and `resolve_rlvr_config` routes flat CLI keys into the nested `rl` block via `_RLVR_KEYS`/`_RL_KEYS`.

**Not implemented, on purpose:** agentic RL, game agents, distributed training, and
the concrete Transformers/PEFT optimisation loop (the optimizer boundary Phase 18
named — RLVR only fills it). The phase stops here, before Phase 20.

**Dry-run status** — a deterministic dry run walks all ten stages as `done`:
2 tasks, t-ok reward `1.0`, t-fail reward `-1.0`, reward total `0.0`, reward
validation findings `[]`, integrity `{valid: 2}`, verification accuracy `1.0`,
fp/fn `0`. Critiques surfaced two honest `output_format_error` findings from the
failing task, which became corrected examples and were held back from the
dataset.

**Gates** — `pytest tests/ -q` (188 new tests pass, plus the 25-row diagnostics
roster pins in `tests/test_optimization.py` and `tests/test_web_api.py`);
`generate_api_reference.py --check` (docs in sync, 207 routes, 31 `/rlvr/*`);
mypy clean on both platforms (320 source files). Ruff is local-only and not a CI
gate; `rlvr/` is ruff-clean apart from the documented `E501` baseline.

## Completed Agentic Reinforcement Learning (Phase 20)

Phase 20 in one line: it makes a whole multi-step TASK the unit of learning —
goal, state, observation, decision, action, outcome, verification, reward and
state transition — on top of everything Phases 15–19 already own, without adding
a second trajectory format, reward engine, registry, verifier, retry loop or risk
table. It is documented in [docs/AGENTIC_RL.md](AGENTIC_RL.md), reported in
[docs/PHASE20_REPORT.md](PHASE20_REPORT.md) and pinned by `tests/test_agentic_rl.py`
(175 tests + 6 subtests).

**Optional and inert.** Nothing in `src/novacontrol/agentic/` is imported by the
application, the API, the CLI or the runtime — the only file outside the package
that names it is its own test file — so normal NovaControl operation is
unchanged. `dry_run` is `True` by default, `mock_agentic_policy` is the only
implemented optimizer (`learns: False`), PPO/GRPO/actor-critic/policy-gradient
are named as planned and refused as not implemented, no model is loaded or
downloaded, and **no CUDA or NVIDIA GPU is required**. `agentic.DEFERRED_PHASES`
and `agentic.overview()` state all of it, including
`automatic_training: false`, `automatic_model_loading: false` and
`stores_hidden_reasoning: false`.

**The mask is the safety boundary.** `ActionMasker` asks four questions of every
candidate — is it `unavailable` in this state, `unauthorized` by the
`PermissionManager`, `unsafe` (irreversible and unapproved) or `incompatible`
with the environment — and a fifth (`confirmation_required`) when an approval is
needed and nobody can give one. The mask also NAMES the actions it kept only
because a person is reachable, so the rollout asks about every one of them: an
unanswerable ask is a refusal, an approval already given is not re-asked, and a
hook that raises is never a yes. A decision is validated, never trusted: an
action the mask did not allow is refused and recorded (`executed: false`), and an
INVENTED action that declares it needs approval, cannot be undone or is rated
HIGH or worse ends the episode as a `safety_stop` rather than a quiet failure.

**Verification stays in the loop, and recovery stays bounded.** An action that
changes something is verified by the environment's own observation, with a wired
Phase 8 `VerificationEngine` and an optional verifier hook composing in; the most
cautious MEANINGFUL verdict wins (a `fail` outranks a `pass`, a check that did
not run never overturns one that did), a crashed engine becomes `inconclusive`,
and every non-winning verdict keeps its reason so a check that ran and exploded
cannot disappear. Recovery reuses Phase 8's rules: never a destructive or
external action, never a refusal, never a missing dependency, always inside the
plan's retry ceiling. Every episode ends for one of seven recorded reasons
(`success`, `failure`, `max_steps`, `cancelled`, `timeout`, `safety_stop`,
`environment_error`) and every bound is explicit (steps, planning horizon,
retries, wall-clock timeout, resource budget, refusal tolerance).

**Rewards keep their dimensions apart.** Nine dimensions
(`task_success`, `verification`, `safety`, `efficiency`, `latency`,
`resource_usage`, `tool_correctness`, `planning_efficiency`,
`recovery_quality`) with six signals and nine penalties, all stored beside the
total — **safety is never averaged into efficiency** — the shaped (intermediate)
share is capped and the cap is reported, and the episode projects into Phase 15's
`RewardResult`. `CreditAssigner` implements five methods with a configurable
discount factor and backwards-accumulated returns that stay stable over long
episodes. Exploration is seeded, draws only from the mask, refuses to explore
into a HIGH-risk, irreversible or confirmation-requiring action, and stops on one
of six named budget reasons.

**Evidence decides, and a person promotes.** Task success is measured over
DECIDED episodes (a bounded run is neither a success nor a failure), safety and
efficiency are reported as separate blocks, unmeasurable figures are `None` with
a reason, and a higher average reward is explicitly **not** evidence. The
ten-task default set can meet the default minimum sample, so the default
configuration and the default task distribution do not contradict each other. Shadow mode lets a
policy only propose (`shadow_executed_anything: false`), A/B switches nothing,
and promotion runs ten configurable checks — sample size, success, verification,
safety, latency, resources, three regressions and a NAMED approver — where the
safety checks REJECT. `PolicyRegistry` tracks the six statuses and refuses a
promotion the gates did not approve.

**Nothing unsafe starts.** An UNSAFE resource estimate is a refusal
(`allows_training: false`, `override_required: true`), the trainer's real-run path
refuses when the optional training dependencies are absent rather than
pretending, checkpoints carry the policy/model/config/curriculum/environment
versions with an integrity digest and a rollback target, and `run_agentic_dry_run`
walks all thirteen stages (`environment` → `state` → `policy` → `action` →
`execution` → `verification` → `reward` → `credit_assignment` →
`state_transition` → `episode_termination` → `evaluation` → `checkpoint` →
`registration`) reporting `trained: false`, `model_loaded: false`.

**Nine defects in the phase's own code were found and fixed by driving it against
its specification** (all pinned by tests, all recorded in
[docs/PHASE20_REPORT.md](PHASE20_REPORT.md) §16): a random `action_id` made the
same action unrecognisable between calls; an APPROVED action could be refused
while an action kept only because a person was reachable could run unasked; an
invented action declaring itself unsafe ended as a plain failure; a crashed
verification engine left no trace in the record; the default task set could not
meet its own minimum sample size; `ENVIRONMENT_LEVELS` and the derived
difficulty gave two different answers for the same task; the trainer recorded an
evaluation against a policy that was never registered; a shadow that proposed
nothing had no way to say so; and eight mypy errors sat in the new package. No
suppressions were added and no assertion was weakened.

**Deferred, on purpose** — Phases 21–26 (real-time perception, world model and
state reasoning, interactive learning, planning/action-policy research, embodied
and game agents, generalization + ARC + intelligence evaluation), real optimizer
implementations, real environments, and any promotion of a learned policy into
production. The phase stops here.

### Phases 15–20 verified end to end against their requirements

The whole learning-and-training stack — 15 (evaluation and reward), 16
(supervised fine-tuning), 17 (preference optimization), 18 (RLHF/RLAIF), 19 (RLVR
and critique learning) and 20 (agentic RL) — was re-verified on the frozen tree:
each phase's own suite, the shared gates, and each phase's public surface driven
directly rather than read off the layer that describes itself.

- **Every phase's surface answered as specified.** `/evaluation/*` (4 routes),
  `/training/*` (25), `/preference/*` (28), `/rlhf/*` (31) and `/rlvr/*` (31) make
  up the 208-route surface `docs/API.md` is generated from, and Phase 20 adds none
  by design: its interface is the package, and `overview()` reports the thirteen
  dry-run stages, `trained: false`, `model_loaded: false`,
  `automatic_training: false`, `automatic_model_loading: false`,
  `cuda_required: false`, `stores_hidden_reasoning: false`,
  `mock_agentic_policy` as the only implemented optimizer, and the six deferred
  phases.
- **The defaults are still the careful ones.** `dry_run` is `True` in all four
  training configurations (`training`, `preference`, `rlhf`, `rlvr`), the RLHF and
  RLVR algorithm is the non-learning `mock_policy`, RLVR registers the eight
  deterministic verifiers, and importing `novacontrol.application` pulls in none
  of `agentic`, `rlvr`, `rlhf` or `preference`.
- **Compatibility held.** A Phase 15 `AgentTrajectory` row is unchanged — all
  nineteen agentic fields optional, `is_agentic` false, `to_dict`/`from_dict`
  round-tripping equal — and Phase 20 required no change to the diagnostic
  roster, the route set or the generated API reference.
- **Three defects were found and repaired**, none of them in a phase's own
  learning code: a test that asserted a process-global property its own module had
  already polluted, a state store that could raise out of the application's
  constructor, and an API assertion that measured how loaded the host machine was.
  Each is recorded in [docs/DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) §49.

## Completed Real-Time Perception & Abstraction (Phase 21)

Phase 21 in one line: it turns a frame — screen, camera, image or frame stream —
into a **structured, temporally consistent, abstract representation** that a later
difference can consume, cheaply when the cheap path suffices and honestly when it
does not. It is built **on** the Phase 6 vision layer rather than beside it: the
same `VisionManager` answers the deep questions, the same `OcrEngine` chain reads
text, the same `VisionProvider` boundary decides whether a model can see at all.
It is reported in [docs/PHASE21_REPORT.md](PHASE21_REPORT.md) and pinned by
`tests/test_perception.py` (90 tests).

**The pipeline is one shape.** frames → preprocessing → fast perception → VLM
escalation via Phase 6 → spatial → temporal → structured scene → abstraction →
confidence/uncertainty, all under `src/novacontrol/perception/` (15 modules). Three
rules are enforced in code: the fast path (OCR + classical regions) always runs
first and usually *is* the answer; escalation builds a `VisionRequest` and calls
the `VisionManager` the application already owns, so there is one place a model is
asked about an image; and **a refusal is a result** — no vision model, a provider
that raised, an unreadable frame, a capability switched off — each produces a
status that names the capability responsible, never SUCCESS because *something*
happened.

**Honest by construction.** The 8-row capability table classifies `ocr` and
`tracking`/`relationships`/`temporal`/`abstraction` IMPLEMENTED, `detection` and
`segmentation` PARTIALLY_IMPLEMENTED (region/text level, no classes), and `vlm`
PROVIDER_DEPENDENT — live availability on this machine reads `no vision model is
wired`. `overview()` reports `cuda_required`, `automatic_model_loading`,
`automatic_model_downloads`, `stores_raw_frames`, `stores_masks`,
`stores_hidden_reasoning`, `action_execution` and `predicts_future_state` all
false, and `DEFERRED_PHASES` names Phases 22–26 explicitly.

**Wired into the running application, not beside it.** The engine is constructed
with the live `VisionManager`, the real resource governor/monitor/model-manager
gate, the approval-gated screen capture and an event observer; the 8 capabilities
register on the ONE `CapabilityRegistry` (`category="perception"`);
`status()["perception_pipeline"]` sits beside `vision_pipeline`; and 8 typed
events (`perception.started/completed/failed`, `perception.frame`,
`perception.scene_changed`, `perception.object_appeared/disappeared/moved`) travel
through the same `_announce_soon` every other publisher uses — payloads carry
identifiers and measurements, never a frame path, never text.

**Three API routes** (five-place contract, 210 routes total, `docs/API.md`
regenerated): `POST /perception` (reading only — nothing clicks, types or runs;
missing source is a 422 naming what to send), `GET /perception/status` (providers,
budgets, telemetry, the *shape* of the last scene — no path, no text, no pixels)
and `GET /perception/capabilities` (the classification table with live
availability). Consumers: the result's accessor surface, Phase 20's agentic loop
via `perception_observation` (bounded lists, duck-typed `attach_observation`), and
the event bus.

**Measured on this machine** (no GPU, real OCR chain): text read 409 ms
(`fast_sufficient`), objects+masks 724 ms with 6 relations, a two-frame 24 px move
reported as `object_moved: moved 23px right`, screen capture 48 objects/157 lines
with stages totalling 1.39 s, camera UNAVAILABLE with its reason, describe-without-
model PARTIAL naming `vlm`, and a 12-frame stream peaking at **3.54 MB** of Python
heap. Stage latency is reported per stage and `total` sums it.

**Ten defects were found and fixed by driving the implementation against its
specification** (all pinned by tests, all recorded in
[docs/PHASE21_REPORT.md](PHASE21_REPORT.md) §26): a still image was read twelve
times; `frame=` was accepted and ignored; `temporal_context` was dead API surface
(a fresh look could report bogus disappearances); the sampler's decision was
recorded but not enforced; per-stage latency was missing; a failed deep path was
reported as SUCCESS; a deterministic capability's reason implied a provider
dependency; two mypy errors in the new wiring; one test expectation contradicted
the tracker's own tolerance; and ruff debt in the new code. No suppressions were
added and no assertion was weakened.

**Deferred, on purpose** — Phases 22–26, semantic object detection (no ONNX/torch
detector was added: no heavy dependency, no auto-download), a camera backend,
class-labelled masks, identity recognition (tracking is spatial), future-state
prediction, and any action execution (the layer reads; it never clicks, types,
opens or runs).

## Completed World Model, Memory & State Reasoning (Phase 22)

Phase 22 in one line: it turns Phase 21's perception output into a **structured,
uncertainty-aware state of the environment** that is updated from observations,
remembered over time, queried currently and historically, and reasoned about — and
that says plainly when it cannot predict. It is reported in
[docs/PHASE22_REPORT.md](PHASE22_REPORT.md) and pinned by
`tests/test_world_model.py` (254 tests + 28 subtests).

**The pipeline is one shape.** observation normalization (content key, duplicate,
ordering) → state estimation (identity, attributes, absence, expiration) → world
state (entities, relationships, conditions, uncertainty) → entity tracking →
relationship graph → temporal memory → state transitions → change detection → 11
query kinds → 7 named deterministic reasoning rules → an honest prediction
boundary, all under `src/novacontrol/world/` (16 modules + `__init__`, 9,121
lines). Built **on** what exists rather than beside it: the shared `JsonStateStore`
persists a world (one sanitized key per world), the ONE event bus carries 9 typed
events through an injected observer seam, the ONE `CapabilityRegistry` holds the 12
capability rows, Phase 21's own `derive_relationships` decides geometry, and the
SAME resource governor stands in front of any model-backed prediction.

**Honest by construction.** The 12-row capability table classifies 11 rows
IMPLEMENTED and `world.prediction` **PROVIDER_DEPENDENT** — live availability reads
`no predictive provider is wired; this build ships a state store, not a learned
world model`. `overview()` reports `cuda_required`, `automatic_model_loading`,
`automatic_model_downloads`, `stores_raw_frames`, `stores_observation_content`,
`stores_hidden_reasoning`, `exposes_chain_of_thought`, `action_execution`,
`predicts_future_state`, `predictive_model_available` and `trains_anything` all
false, with `rule_projection_available` true and Phases 23–26 named in `deferred`.
The only projection that ships is `RuleProjectionProvider` — a constant-velocity
extrapolation from two measured versions, labelled `rule_based=True` with
`confidence=None`, opt-in via `settings.rule_projection`.

**The honesty rules are enforced in code, not promised.** A fact that was not
observed is never stored as observed (every attribute carries a `FactBasis` and an
evidence reference); an unmeasured figure is `None`, never 0 (a fresh entity's
`identity_confidence`, an unprobed availability, an unmeasured latency); absence
needs a *complete* observation before it means anything; an ambiguous identity stays
provisional with an `ambiguous_identity` uncertainty row; a stale relation is kept
and labelled rather than deleted; a historical question is answered `NOT_FOUND`
with a limitation that says it was **not substituted**; and nothing here acts,
trains or downloads anything.

**Wiring.** `_build_world_model` constructs the engine with the shared store, the
Phase 21 gate and (only when `rule_projection` is on) the rule provider;
`register_world_capabilities` declares 12 rows on the ONE registry;
`status()["world_model"]` sits beside `perception_pipeline` and the telemetry
service exposes a **flat, content-light** slice on `/status`; `persist()` saves the
world and boot restores it; 9 `world.*` event types are declared in
`EVENT_PAYLOAD_FIELDS`; and five routes (`GET /world/status`, `GET /world/state`,
`POST /world/observe`, `POST /world/query`, `POST /world/predict`) were added in all
five contract places — **215 routes = 215 consumers**, `docs/API.md` regenerated and
in sync.

**Ten defects were found by driving the implementation through its own nine
workflows, through an independent audit of every requirement question, and — the
last one — by CI on Linux** (report §25, each pinned): `_conditions` counted the previous version's
entities so `entity_count` was always one version stale; `observation_from_perception`
raised `UnboundLocalError` for a bare `SceneRepresentation`; a failed look
(`scene=None`) was rejected as "reports nothing" instead of being recorded as a
`perception_status` fact; ephemeral conditions could linger, so `EPHEMERAL_CONDITIONS`
now drops `perception_status` on carry-forward; `_ordering` had the staleness test
inverted (`age < 0`), flagging a future observation and not an old one;
`queries._evidence` treated an unresolved subject as "no filter" and returned
unrelated rows; `observe()` ignored the observation's own `correlation_id`; and the
audit that followed found two more, both about a cap being reported as something it
is not — `ingest._bounded` folded entities past the ingest ceiling, facts past the
fact ceiling and genuinely unreadable rows into ONE counter that `validate`
rendered as "N unusable entity row(s) dropped" (a caller who sent 300 valid rows
was told 172 were unusable), and the state ceiling reported nothing when the
overage was live entities, so a state could hold 480 entities while
`status()["policy"]["max_entities"]` said 256. Both are fixed — truncated rows are
counted as truncated and the reason names the ceiling, and the live overage is
published as `status()["entities_over_ceiling"]` — with two existing expectations
corrected from the conflation and three new tests pinning the corrected behaviour.
The tenth was found the only way it could be: CI run #25 went red on both test jobs
because the shared `persistence/json_store.py` called `mkdir` in its **constructor**
unguarded, so `JsonStateStore("/definitely/not/a/directory")` raised
`PermissionError` on Linux while Windows silently created that directory — and the
world model's own "unreadable store" test wrote a snapshot into it, outside the
repo, without exercising the case it named. The constructor now degrades like the
reads and writes beside it, and the test names a root that cannot exist on either
platform (its parent is a regular file). Both pins were mutation-checked against the
unguarded constructor, and CI run #26 on the fixed commit is green in all four jobs.

**Measured on this machine** (report §24): 200 entities → `observe()` mean 12.9 ms,
`query(entities, 200)` 0.31 ms, single-entity 0.009 ms, `reason()` 0.33 ms,
`changes()` 0.25 ms, rule projection 0.019 ms; 200 entities × 32 retained snapshots
→ 4.1 MiB tracemalloc peak, 6.0 MiB serialized, 10.2 MB stored and ~118 ms to save
(snapshots are full states, not deltas — the phase's dominant cost, bounded by
`max_snapshots`/`max_transitions`/`retention_seconds`).

**Gates on the final tree.** Three-group full regression: **3,666 passed / 12
skipped / 2,146 subtests, all three groups exit 0** (1,399 + 1,124 + 1,143), run
after the last source edit. `mypy src` and `mypy src --platform win32` clean over
365 files; `ruff check src/novacontrol/world tests/test_world_model.py` clean;
`generate_api_reference.py --check` → `docs/API.md: in sync`; routes == consumers at
215 = 215 with missing/extra both empty.

**Deferred, on purpose** — Phases 24–26 (planning/reasoning/action policy, embodied
and game agents, generalization/ARC/evaluation), a learned predictive world model,
attribute schema inference, cross-world queries, delta-encoded snapshots, and any
action execution.

## Completed Interactive Learning & Exploration (Phase 23)

Phase 23 in one line: it turns a bounded question into a bounded experiment,
compares what it expected with what it observed, and keeps the answer only when the
evidence supports it — and says so plainly when it cannot. It is reported in
[docs/PHASE23_REPORT.md](PHASE23_REPORT.md) and pinned by
`tests/test_interactive_learning_workflows.py` (26 workflows A–K through the real
`create_app()` surface).

**The cycle is one shape.** observe → identify uncertainty → form hypothesis →
select experiment → check safety and permissions → execute bounded action → observe
result → compare prediction with actual result → update knowledge → evaluate
learning → select next experiment, all under `src/novacontrol/interactive/`
(16 Python files including `__init__.py`, 9,414 lines in the current tree). Built **on** what exists rather than beside
it: the shared `JsonStateStore` persists one sanitized key per objective through
`JsonLearningRepository`, the ONE event bus carries 15 declared `learning.*` types
through an injected observer seam, the ONE `CapabilityRegistry` holds the 12-row
capability table in the `learning` category, the Phase 8 permission/risk layer is
what the safety gate asks about real actions (no layer wired → refusal, not
assumption), Phase 14's policy pattern supplies every ceiling, and Phase 22's world
state is read through a **read-only** environment that exposes no action at all.

**Honest by construction.** No candidate hypothesis is invented (they come from the
environment's declared space); no prediction is fabricated (with no rule and no
model the run is recorded as prediction-unavailable and is **not scored for
agreement**); one successful experiment never establishes a rule (promotion needs
`support_threshold` independent supporting observations and fewer than
`contradiction_threshold` contradicting ones); repeated observations from one source
are not counted as independent evidence; a blocked, rejected, timed-out or
unexecuted experiment is never a success; an interrupted experiment is never
completed by a restart; and every report and metric repeats that the shipped
environments are simulators, so their results are evidence about the simulator. The
12 capability rows classify 8 IMPLEMENTED, `interactive.prediction` and
`interactive.real_environment_actions` PROVIDER_DEPENDENT,
`interactive.world_observation` PARTIALLY_IMPLEMENTED and
`interactive.model_training` DEFERRED, with live availability read from the manager
on every call.

**Wiring.** `_build_interactive_learning` constructs the manager with the shared
store, the shared event relay, the safety gate over the shared permission layer, and
the read-only world environment; `register_learning_capabilities` declares the 12
rows on the ONE registry; `status()["interactive_learning"]` sits beside
`world_model` and `perception_pipeline` and carries the boot restore report; boot
`restore()` and `save()` on stop; a `learning:` config section with six hard
ceilings and 23 policy fields; 15 event types with declared payload fields; and 16
routes added in all five contract places — **231 routes = 231 consumers**,
`docs/API.md` regenerated and in sync.

**Nine defects were found by building, testing and re-testing the phase**, each
fixed and pinned (report §13): independent support never accumulated (the count is
now recomputed from the evidence); two hand-written field copies dropped any field
added to the model (both now use `replace()` through `with_updates`); two capability
tables existed and the prediction probe recursed through `status()` (now ONE table
reading the manager's own records); a preview before any run showed nothing (it now
shows the hypotheses the cycle WOULD propose, labelled `dry_run`); refusals were
invisible in the run response (the refused experiments now travel with it); and four
were found only by the **live restart** check — a real server restart came back with
`rules: 0`, because terminal objectives skipped their stored record entirely and
because each payload's whole rule book was assigned rather than merged (both fixed:
full restore for every objective, and `RuleBook.absorb()` merging by id, higher
version winning); a `timed_out` flag set by an earlier attempt could mislabel a
later ordinary failure (the final attempt decides); and the destructive refusaldid not say that approval is not the answer (it does now). Two harness defects were
fixed too: the workflow tests read the event bus synchronously while announcements
are scheduled on the loop, so a bounded wait replaced the race (three consecutive
green runs); and one full-suite run exposed 7 host-memory-dependent failures in
`tests/test_training.py`, whose live-application tests let this machine's free
memory decide whether a run may start — those now pin the machine reading, so the
host is no longer the variable.

**Measured on this machine** (report §12): learning an unknown transition (3
experiments) 2.05 ms; one decisive experiment between competing hypotheses 0.88 ms;
a contradicted rule's failed prediction 0.77 ms; a partial observation's two
inconclusive experiments 1.05 ms; a device that declares nothing stops without an
experiment in 0.12 ms; and after one rule is learned, **200 new questions were
answered with 0 experiments at 0.258 ms each**. The transfer check reports its
agreement (1/2 caller-supplied cases, the mismatch named) with the two limitations
it actually has.

**Gates on the final tree.** Full regression: **3,696 passed, 12 skipped, 2,178
subtests, exit 0** (one run, 8:50). `mypy src` and
`mypy src --platform win32` clean over 381 files; `ruff check` clean on the
interactive package and its workflow file; `generate_api_reference.py --check` →
`docs/API.md: in sync`; 231 routes = 231 consumers with missing/extra both empty.
A REAL uvicorn server on `127.0.0.1:8001` passed a 17/17 scripted HTTP workflow and
a restart against the same data directory restored 4 objectives and the learned
rule, then answered a new question with 0 experiments.

**Deferred, on purpose (as of Phase 23)** — planning/reasoning/action policy
(implemented in Phase 24, below), embodied and game agents, generalization/ARC/evaluation); training from experiments
(`interactive.model_training`), counterfactual replay, automatic environment
discovery, a CLI command and a frontend panel, and any action outside a registered
environment. Simulators declared over the API are session declarations: what
persists is the record the capability table names — objectives, hypotheses,
experiments, runs, evidence references, budget usage and rules.

## Completed Planning, Reasoning & Action Policy (Phase 24)

Phase 24 in one line: it turns a goal into **candidate** plans, grounds them in state it
can actually read, asks a person before a consequential step, runs the selected plan
through the executor the build already has, and decides whether the goal is complete
from an observation of its own success conditions rather than from the steps ending. It
is reported in [docs/PHASE24_REPORT.md](PHASE24_REPORT.md) and pinned by
`tests/test_goal_planning.py` (90 tests: the contracts, the audit's run-limit pins,
plus workflows A–L over the real
`create_app()` HTTP surface).

**One flow, delegated at every step.** goal representation → world state → available
capabilities → decomposition → candidate plans → precondition/safety checks → outcome
estimation → selection → **bounded execution through the existing executor** →
verification → reassessment. Nine new modules in `src/novacontrol/planning/` (5,357
lines: `goals`, `grounding`, `candidates`, `ranking`, `estimation`, `policy`, `monitor`,
`goal_manager`, `capabilities`). Nothing was forked: Phase 22's world model is read
**read-only** through `StateGroundingService`, steps run on the application's own
`WorkflowExecutor` behind a fresh-state gate, results are checked by Phase 8's
`VerificationEngine`, risk and approval come from the same `self.risk` layer the tool
executor gates on, Phase 23's rule book is consulted for an outcome estimate only, the
ONE `CapabilityRegistry` holds the 20 declared rows, the ONE `EventBus` carries 8 new
`goal.*` types through an injected observer seam, and the shared `JsonStateStore`
persists one key per goal. It is the ORDER they are called in plus the state that
remembers what happened.

**Honest by construction.** A field the caller did not state stays empty (`create()`
never invents a condition, constraint, dependency or deadline); a success condition with
no expected value is *not measurable* and answers `None` rather than `False`; an asserted
initial state is `basis=caller_assertion`, never an observation; a stale or conflicting
fact is deliberately **not** offered to a precondition; a precondition whose state is
unknown produces `needs_observation`, never an attempt; an unmeasurable goal is refused
with the reason; every estimate carries a `basis` (`world_model` → `learned_rule` in
scope → `rule_based` declaration → `unavailable`) and `confidence=None` when nothing
measured it; a retired or out-of-scope rule is never a basis; a truncated search says
`truncated=true`; a candidate that is infeasible is never selected for execution; a plan
with no steps is the skip path, not a refusal; and completion requires the goal's own
conditions to be **observed** holding. Fourteen `IssueKind`/verdict/status values carry
the distinctions the phase asks for, and nothing that was refused reports success.

**Safety first, from configuration.** A goal's `exclusions` are absolute; its
`approval_conditions` force a confirmation; `DESTRUCTIVE`/`EXTERNAL`/`SYSTEM` steps
always ask, with or without a permission layer wired, and a step needing confirmation
with no channel is denied rather than attempted (fail closed in the executor, not only
in the policy). One approval set is read by both the policy and the executor's channel,
so an approved step is not refused twice. Every ceiling comes from the new
`goal_planning:` config section (13 settings, `allow_destructive` default **false**),
search ceilings are checked **inside** the loop, and parallel waves are capped at 4 and
read-only only. Replanning is counted at the place that would start another attempt and
stops at the goal's own ceiling with the count in the refusal.

**Wiring.** `_build_goal_planning` constructs the manager from the collaborators the
build already owns; `register_planning_capabilities` declares the 20 rows on the ONE
registry; `status()["app"]["goal_planning"]` sits beside `world_model` and
`interactive_learning` and carries the boot restore report; boot `restore()` and
`save()` on stop; 8 typed events with declared payload fields; and 7 routes added in all
five contract places — **238 routes = 238 consumers**, `docs/API.md` regenerated and in
sync. Every planning route is registered `no-render` on purpose: the surface is
API/operator-facing and the phase does not claim a panel or a CLI command for it.

**Six defects were found and fixed**, each pinned by a test (report §13):
`StepOutcome.executed` counted a **DENIED** step as an execution — in shared Phase 4
code — so a plan whose only step was refused read as “work happened” while its own
summary said it did not; the fresh-state gate's first version kept its produced-facts
map in a closure and assigned to it without `nonlocal`, raising a real
`UnboundLocalError` on the first step (it now reads the executor's own
`StepContext.outputs`); a gate refusal was reported as `needs_observation` (“go and
look”) instead of `blocked`, so the gate's verdict now outranks the completion check;
and the boot restore report shipped whole stored payloads — the user's request text
included — on a status surface whose own docstring promises a content-light summary (it
is now a bounded row that also makes the previously chosen candidate id readable rather
than written and ignored). The final verification pass added two more, both in its own
budget work: a run that finished past its stated seconds budget reported `completed`
(the clock was only asked before each step, so a single-step plan could overrun its
whole budget unnoticed — `execute()` now asks once more after the last step and labels
the breach `budget_exhausted`/`deadline_exceeded` while keeping the completion verdict
it earned), and every non-proceed revalidation was stamped `kind: "state"`, so a
precondition the state could not settle reported `blocked` instead of
`needs_observation` (the stop now carries the gate's own verdict).

**Measured on this machine.** `scripts/benchmark_planning.py`: 13 deterministic
scenarios (A–M, 14 legs — the approval scenario is played with and without approval),
each checking **its own** expectation, all passing with the process exiting 0; **28
candidates compared, 64 search expansions, 7 step revalidations, planning mean 0.25 ms
/ max 0.625 ms**, and **20/20 estimates labelled `rule_based`** — with no predictor and
no learned rule wired, every estimate is a declaration, which is what it should say. The
benchmark was **mutation-tested** before it was trusted, and again on the audit's fixes:
flipping the refusal branch of `_status_for` back to `completed` made scenario G fail,
and disabling the post-run clock check made the new scenario M fail — each made the
script exit 1, and each revert was verified byte-identical (the last by sha256).

**Gates on the final tree.** `tests/test_goal_planning.py` **90 passed** (the build's
87 plus the audit's 3 run-limit pins); planning/reliability/persistence/world **372
passed + 50 subtests**; application/API/reference-sync **40 passed + 250 subtests**;
config/events/route-shape/web **133 passed + 124 subtests**; benchmark **14/14 with
exit 0**; full regression in 4 disjoint slices measured during the phase build, **3,789
passed,
0 failed, 12 skipped, 2,192 subtests, all four exit 0** (708 + 639 + 1,362 + 1,080;
the 12 skips are all `playwright not installed`, confirmed with `-rs`); `mypy src` and `mypy src --platform win32` clean over 390 files;
`generate_api_reference.py --check` → in sync; `dead_code_hunt.py planning` finds
nothing from this phase. A REAL uvicorn server on `127.0.0.1:8001` started from an
isolated data directory passed a **30/30** scripted HTTP workflow — plan (running
nothing) → run that refuses to claim completion while nothing observed the condition →
`POST /world/observe` → `completed` and verified — and a **restart restored 3 goals with
0 skipped**, keeping status, request, conditions and decomposition while correctly
restoring **no plan**. CI is the one item reported as unverified rather than passed
(§19 of the report): the verification above is local, and this repository has been
broken before by a defect only Linux CI could see.

**Deferred, on purpose** — Phases 25–26 (embodied/game agents,
generalization/ARC/intelligence evaluation), declared as the two `future` rows
`planning.game_agent` and `planning.generalization`; a learned predictive model
(`planning.prediction` is provider-dependent); a web panel and a CLI command for goal
planning; and any claim that a plan is a success. Twenty capability rows: 13
IMPLEMENTED, 2 PARTIALLY_IMPLEMENTED, 3 PROVIDER_DEPENDENT, 2 FUTURE, with live
availability read from the manager on every call.

## Completed Embodied Agents & Controlled Environments (Phase 25)

`embodied/` (9 Python files including `__init__.py`, 5,705 lines in the current tree) is an embodied-agent framework over a
controlled environment, and the whole cycle runs in the shipping build:
**observe → interpret → update the world model → identify the objective → select an
action through Phase 24 → check preconditions and safety → execute through the ONE
approved input door → observe the result → verify → update memory and trajectory →
recover or replan**, ending as one of `completed`, `budget_exhausted`,
`environment_failed`, `agent_error`, `denied`, `stuck`, `cancelled`, `interrupted`.

**The environment is owned and reproducible.** `GridWorldSimulation` is a
deterministic grid with walls, an agent and one collectible target, with structured
readings, optional fogged readings (unlabeled regions only) and four named fault
injections (`timeout`, `drift`, `reject`, `freeze`). Seven named scenarios
(`open-east`, `detour`, `drifting`, `refusing`, `frozen`, `timing-out`, `fogged`)
cover the happy path, a wall to route around, an environment that disagrees with the
input, a controller that refuses, an environment that stops answering, an input that
never reports back and a reading with no state at all. A scenario is *configuration*,
so `gridworld:detour` is its own environment id and never a mutation of a world
somebody else may be driving.

**One door, and nothing is forked.** Every input leaves through
`EmbodiedManager.execute_step`, reached as an `environment_input` plan action and
deliberately LAST in the step-handler table so every existing gate runs first. The
door checks the session (live, not paused, not awaiting revalidation), the allowlist by
name, the connection, the pace, the duration clamp, the permission layer, and it
records the result on the SAME automation audit trail the desktop, browser and phone
controllers write to. Phase 24's `GoalManager` selects and authorizes what is sent — a
session with no planner wired refuses to run rather than falling back to a private loop
— Phase 22's world model takes each reading as an observed fact with provenance, the ONE
`EventBus` carries 13 typed events, the ONE `CapabilityRegistry` holds 18 declared rows
with live availability, and the shared `JsonStateStore` persists every session under one
key.

**Honesty is enforced in code.** Only a fresh *structured* reading may authorize a
state-dependent action; a stale one is reported and refused. A region detected in a
frame is never given a name nothing supports (`embodied.semantic_perception` reports
UNAVAILABLE with that reason). `effect_observed` distinguishes "confirmed" from "checked
and failed" from "could not check", and `None` is never rounded up. Completion requires
a **fresh reading** of the goal's own conditions, so a budget that runs out, an
environment that stops answering and a stuck position are their own endings with their
own reasons — and an unmeasured rate is `None`, never `0.0`. The action count is what the
**door** sent, a refusal ends the session instead of being retried through, a timed-out
input is re-read before anything else is sent, and a restored session comes back
INTERRUPTED and sends nothing until it has been revalidated against a live reading.

**Verified.** `tests/test_embodied.py` — 122 tests + 84 subtests: the contracts
(objective measurability, provenance, the three-valued verification, the registry's
refuse-by-name, the simulation's bounds, the tactical choice, the door's seven
refusals, the lifecycle and restore rules, the capability table, the config clamps) plus
workflows A–L over the real `create_app()` surface. `scripts/benchmark_embodied.py` — 9
scenarios that **check their own expectations** (9/9 matched): baseline 4 inputs in
182 ms, a 20-cell corridor 21 inputs in 1,052 ms (≈2.5 ms of loop work per input above
the configured 50 ms pace), `frozen` and `fogged` send **0** inputs, and the baseline
repeated 10× is median 195.8 ms with a 1.5 ms stdev. Seven mutation checks were run
against the real source (each re-applied, watched to fail, restored byte-for-byte), and the
closing verification audit added two more: the environment echoing the requested duration
instead of measuring it, and the input door dropping its clamp. A real `uvicorn` server
passed a **16/16** scripted HTTP workflow, and a two-boot restart run passed **13/13**
restore checks plus a **161-check** sweep of all ten routes — which is how the last two
defects were found.
**Eleven defects were found and fixed** (report §13) — the three that matter most were a
pause/resume/cancel that never reached the registry the door reads, the loop's own
revalidation never reaching that registry (so a resumed session could never act again),
and an input budget checked against a count synced a cycle earlier. The last two were
driving-only finds: after a restart a session in a configured world (`gridworld:open-east`)
could be reported RESTORED yet die with "environment … is no longer registered" because
nothing rebuilt the world it was created in, and the sessions list published whole records
(observation and trajectory) where its contract promises the lifecycle only, with the
record on the single-session view. The closing audit's three are the kind only a read
against the phase's own claims finds: a policy refusal that ended the session as
`budget_exhausted` (a budget that never ran out) and counted the refusal as an *invalid*
action, a duration pin that passed with the clamp deleted (the field it asserted on is the
elapsed time the environment measured, so the assertion could not fail), and a second
`HARD_MAX_INPUT_MS` plus a `_clamp` whose result was discarded on every path — which is why
the pin could not see it. Details in
[docs/EMBODIED.md](EMBODIED.md) and [docs/PHASE25_REPORT.md](PHASE25_REPORT.md).

## Completed Generalization, ARC-style Reasoning & Intelligence Evaluation (Phase 26)

`generalization/` (13 Python files including `__init__.py`, 7,670 lines in the current tree) is the last phase of the intelligence
roadmap, and it answers one question: **does a rule inferred from examples still work
on an example that was never shown?** Anything else is not called generalization.

The cycle the phase names is implemented arrow by arrow — define task → establish
baseline → present unseen example → observe input → identify structure → form
hypothesis → generate candidate → verify → measure performance → compare against the
baseline → classify failure → record evidence → re-evaluate — as records rather than
narration. A task is a request, a success condition, a verifier, examples, an unseen
input and a **hand-written answer key**; `validate_task` refuses one with no success
condition (a task nobody can verify), no answer key, an unknown verifier or empty
examples, and the refusal names which rule refused it. The corpus is 39 tasks
(19 fitting, 20 held-out) across six families — `grid_transform`, `spatial_relation`,
`state_transition`, `small_planning`, `tool_selection`, `multi_step_reasoning` — each
with its own success condition and one of seven verifiers.

**A rule is believed only when it reproduces every example exactly.** The grid
capability is a named, enumerated library of 22 pure operators composed to depth 2:
the search proposes parameters from the examples and believes nothing until every
example is reproduced. Two rules that both fit and disagree report `ambiguous`; what
the library cannot express reports `unsupported`; neither is counted as a solve. The
phase's own words are enforced in code — `generalization.arc_general_solver` is
`partially_implemented`, and there is no general ARC solver.

**Held out means held out.** Three leakage checks run before every evaluation — a task
id in both splits, a held-out answer already present among the fitting examples, and an
example flagged `seen` while tuning — and with `require_clean_splits` (the default) a
run over a dirty split is **refused with 422** rather than reported. A run carries an
outcome fingerprint over what decides its numbers (task, status, candidate, source,
prediction, exact match) and nothing else, so two identical evaluations reproduce it
with different run ids.

**Measured on this machine.** Candidate `infer` over the 20 held-out tasks: **16 solved,
2 ambiguous, 2 unsupported, 0 failed, 22.5 ms** (grid family 8/10 at 0.89 cell accuracy
after examining 1,018 candidates). Baseline `identity`: 1 solved, 10 unsupported, 9
failed. `compare`: 16 improved, **1 regressed** — the ambiguity probe whose answer
equals its input, which the identity baseline echoes and the candidate honestly refuses
to guess — 3 unchanged, `baseline_preserved: true`. There is **no aggregate score**
(`aggregate_score: None`): the families are measured in different units, so a blended
number would hide a family failing behind a mean.

**Nothing is forked.** Five typed `generalization.*` events travel the ONE bus through
an injected observer seam; 22 capability rows sit on the ONE registry (16 implemented,
`arc_general_solver` partial, `model_proposed_rules` provider-dependent,
`single_intelligence_score` and `synthetic_results` unavailable, `training_from_findings`
and `gameplaying` future); the runs persist through the shared `JsonStateStore` and a
restart restored 4 runs with 0 skipped while correctly **not** restoring registered
tasks; the game-evaluation contracts read Phase 25's session record; and
`overview()` publishes 13 honesty flags — a general ARC solver, hardcoded benchmark
answers, seeing the held-out answer key during the search, leaking held-out answers into
tuning, a single intelligence score, fabricated scores, labelling a mock result as real,
training anything, downloading models, changing production weights, starting training
automatically, claiming gameplay, and bypassing permissions — all **false**.

17 routes in all five contract places — **265 routes = 265 consumers**, `docs/API.md` in
sync — all `no-render`, because this phase has no panel and the contract says so instead
of inventing a control. Verified by `tests/test_generalization.py`,
`scripts/benchmark_generalization.py` (**10/10 self-checking scenarios**, one of which
refuses a leaked split), and a real `uvicorn` server that passed **90/90** boot-1 HTTP
checks plus **7/7** restart checks.

**Six defects were found and fixed** — two by the first verification pass (the evaluate
route and the API reference promised a fingerprint on the run that only the export carried;
and `--json` in the benchmark crashed on a slots dataclass's missing `__dict__`, so the
machine-readable form of the benchmark could not run at all) and **four by a dedicated
verification audit that drove 59 probes through the shipped API** (36 engine-level, 23
through the real application, HTTP, a real Phase 25 embodied session and a restart): a
refused evaluation announced nothing on the bus although `generalization.leakage_detected`
exists for exactly that case; every stored run reported `checked: false, clean: None` for
the very leakage check that had gated it; the frozen settings omitted
`evaluate_held_out_only`, so a run that measured 20 of 39 tasks could not say why; and a
docstring claimed the Phase 15 trajectory layer consumes these runs when the recorder
observes the core task lifecycle only (the claim was corrected, not the code bent). The
audit also proved the leg a fresh install cannot reach — the game-eval contracts over a
REAL embodied session — and four `VerificationAuditTests` now pin all of it. Details in
[docs/GENERALIZATION.md](GENERALIZATION.md) and [docs/PHASE26_REPORT.md](PHASE26_REPORT.md).

## Next Work

The intelligence roadmap ends here, and the one thing Phase 26 deliberately did **not**
start is the dedicated **game-playing evaluation**: `generalization.game_eval` ships the
nine contracts it will be measured on (objective completion, action efficiency,
observation freshness, recovery success, constraint violations, time to completion,
initial-condition spread, reproducibility, resource use) as definitions plus an adapter
over Phase 25's session record, and `GET /generalization/game-eval` already summarises
whatever the embodied sessions measured — per environment, never pooled, with `null` for
an unmeasured figure and an explicit note when there is nothing to measure. A real game, a
real player and real gameplay claims are that milestone's job, and its first honest step
is to name the environment it can actually drive rather than to lower the bar for what
counts as playing.

Everything below is the older queued work, unchanged:

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
