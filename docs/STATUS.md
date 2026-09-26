# Project Status

Current phase: **Phase 15 - Complete Local Baseline**

Status: **Complete**

> The phase label above is the original fifteen-phase baseline. The staged build that
> followed it — what it verified, what it changed, and what remains — is recorded below
> under "Completed Staged-Build Verification (Phases 1–7)" and the Phase 8 / Phase 9
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

## Next Work

Phase 10 onward: making this architecture carry real work — external adapters, packaging,
and wiring the understood-but-unexecuted file operations (`find_file`, `read_file`,
`list_files`, `write_file`) so "read report.pdf" reads the file instead of apologising.
The bus is the seam for a live UI: the events already carry correlation ids, so a panel
can subscribe to one request's whole thread instead of polling; and the capability
registry can back a real capability browser and a "why can't you do that?" answer, using
`report()` and `availability_of()` as they stand.
