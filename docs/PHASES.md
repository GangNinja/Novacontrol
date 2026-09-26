# Delivery Phases

> **Two numbering schemes share numbers.** The phases on this page are the original
> fifteen-phase build-out plan — Phase 8 is browser automation, Phase 9 is vision. The
> *staged-build* phases that followed are also numbered 1–9 with different meanings
> (Phase 7 model/hardware manager, Phase 8 reliability, recovery and safety, Phase 9
> internal event bus and capability registry); those are recorded in [STATUS.md](STATUS.md)
> and [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) and extend these layers in place rather
> than adding a parallel architecture.

NovaControl should be built incrementally. Each phase must end with passing tests and updated documentation.

## Phase 1: Project Foundation

Complete in this repository state.

- Package layout
- Runtime contracts
- Event bus
- Configuration scaffold
- Security approval primitives
- Documentation scaffold
- Standard-library test suite

## Phase 2: Core Engine

Complete in this repository state.

- Runtime service container
- Durable event journal
- Lifecycle management
- Structured logging integration
- Error boundaries and retries
- Core diagnostics

## Phase 3: Memory

Complete in this repository state.

- Conversation memory
- Project memory
- Knowledge memory
- Short-term and long-term stores
- Vector database adapter interface
- Summarization and cleanup policies

## Phase 4: Tool System

Complete in this repository state.

- Tool registry
- Tool schemas
- Permission scopes
- Human approval flow
- Mock tool execution for tests

## Phase 5: Agents

Complete in this repository state.

- Coordinator agent
- Specialized agent contracts
- Agent message bus
- Delegation and progress tracking

## Phase 6: Planning

Complete in this repository state.

- Task decomposition
- Workflow graph execution
- Recovery strategies
- Clarification requests

## Phase 7: Desktop Automation

Complete in this repository state.

- Approved command execution
- Application launch adapters
- File organization workflows
- Audit logging

## Phase 8: Browser Automation

Complete in this repository state.

- Playwright adapter
- Navigation and extraction workflows
- Browser approval policy
- Web app testing harness

## Phase 9: Vision

Complete in this repository state.

- OCR adapters
- Screen understanding
- Document/image understanding interfaces

## Requested Feature: Explore

Complete in this repository state.

- Online research workflow
- Understandable explanations
- Source links
- Related video links
- GUI tab named `Explore` (web platform and desktop GUI)

## Phase 10: Voice

Complete in this repository state.

- Speech recognition
- Text to speech
- Wake word hooks
- Interruptible conversations

## Phase 11: GUI

Complete in this repository state.

- Dashboard
- Chat interface
- Task manager
- Memory explorer
- Plugin manager
- Settings, logs, and performance views
- Explore tab

## Phase 12: API

Complete in this repository state.

- REST API
- WebSockets
- Authentication
- Plugin API

## Phase 13: Plugin Marketplace

Complete in this repository state.

- Plugin packaging
- Discovery
- Installation
- Versioning
- Trust and permissions

## Phase 14: Performance Optimization

Complete in this repository state.

- Metrics
- Profiling
- Caching
- Load testing

## Phase 15: Documentation and Release

Complete in this repository state.

- Architecture guide
- Developer guide
- Plugin guide
- API documentation
- Deployment guide
- Release readiness checker

## Verification

Each phase ends with passing tests and updated documentation, and the staged
intelligence build (foundation → understanding/context → decision engine → planner →
tool discovery → vision → model/hardware manager) has been re-verified against the
running request path: every claim was re-derived from the code and driven through
`handle_request`/`run_plan` on a machine with no model installed, rather than read
off the layer that makes it. [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) §25 carries the
clause-by-clause result, the two live-path defects the pass found and fixed (context
references now reach the plan that acts on them; a plan's `run_tests`/`run_command`
steps have a real, approval-gated executor), and the residuals that remain open.

CI enforces four checks on every push: the test matrix on Python 3.12 and 3.13, the
API-reference sync check, and mypy in both the linux and windows platform views.
