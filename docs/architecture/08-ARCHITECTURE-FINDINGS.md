# 08 — Architecture Findings and Recommendations

**Scope:** current implementation only. Findings are observations; recommendations are explicitly separated and are **not** implemented. No code was changed.

## 1. Confirmed architectural strengths

| Strength | Evidence | Why it matters |
|---|---|---|
| One composition root and runtime lifecycle | `NovaControlApplication`, `EventDrivenRuntime` | Startup/shutdown order is explicit and testable |
| Decision does not equal authorization | `decision/models.py`, `reliability/permissions.py` | A route cannot grant itself execution permission |
| Central tool permission gate | `ToolExecutor` + `PermissionManager` | Sensitive actions cannot bypass approval by choosing a tool |
| Deterministic fallbacks | Scratch brain, rules, telemetry answers | Local functionality survives a missing model |
| Bounded loops and retries | `PlanningSettings`, `AgentLoop`, `RetryPolicy` | Failures do not become infinite repetition |
| Event correlation | `EventType`, correlation IDs in `handle_request` | A request can be traced through intent, decision, plan and tools |
| Incremental evidence model | Explore evidence, NovaMarket event evidence | Claims retain source/provenance instead of being opaque prose |
| Independent financial risk | NM-08 re-evaluated by NM-10 | Execution cannot rely on a stale or forged approval |
| Paper-only by construction | No broker client; explicit false flags; no short path | The current system cannot place real-money orders |
| Idempotent accounting | Wallet keys, ledger entries, order replay, inflight journal | Retries and recovery do not intentionally double-charge |
| Local-first privacy | Loopback binding; local models/stores | Default deployment does not require a cloud account |
| ADRs/contracts as code | `novamarket/architecture.py`, status/contracts endpoints | Boundaries are executable and test-pinned rather than prose-only |

## 2. Confirmed findings

### F-01 — Root NovaControl and nested NovaMarket fork are divergent codebases

| Field | Value |
|---|---|
| Modules | Entire repository |
| Evidence | Root `src/novacontrol` 490 Python files; nested `novamarket/src/novacontrol` 605, including 151 NovaMarket files; root has no `novamarket` package/import; nested has its own `.git`; root excludes `novamarket/` |
| Impact | Running the root tree provides no NovaMarket. Fixes/features in one tree do not automatically reach the other. Documentation can describe a capability that the “main” app does not have. |
| Severity | **High** |
| Confidence | Confirmed |
| Future improvement | Decide the canonical tree; make the other a true package/dependency or generated artifact; add a single source-of-truth integration test that asserts NovaMarket registration |

### F-02 — `application.py` is a 14,397-line composition and feature god object

| Field | Value |
|---|---|
| Modules | Application composition, nearly every feature |
| Evidence | `wc -l src/novacontrol/application.py` = 14,397; nested fork application = 11,618; constructor builds stores, models, agents, tools, automation, plugins, training, RL, vision, etc. |
| Impact | High merge-conflict surface; difficult reasoning about initialization order; feature tests instantiate a huge object; changes to one subsystem can affect boot of all. |
| Severity | **High** |
| Confidence | Confirmed |
| Future improvement | Extract feature composition into small builders/factories with explicit dependency protocols; keep the root as ordering only |

### F-03 — API authentication is optional and single-token

| Field | Value |
|---|---|
| Modules | `api/app.py`, `api/auth.py` |
| Evidence | `ApiTokenAuthenticator(os.getenv("NOVACONTROL_API_TOKEN"))`; `/status` reports `auth_enabled`; all routes depend on the same `require_auth`; no users, roles or resource ownership |
| Impact | On a shared host with no token configured, any local process can call powerful desktop/browser/model APIs. With a token, every caller has full operator authority. |
| Severity | **High** in untrusted multi-user environments; **Moderate** on a single-user loopback machine |
| Confidence | Confirmed |
| Future improvement | Default to a generated local token; OS credential storage; split read/write/admin scopes; explicit remote-access opt-in with TLS |

### F-04 — Cloud provider API key is persisted in plaintext JSON

| Field | Value |
|---|---|
| Modules | Brain/cloud configuration |
| Evidence | `data/cloud_llm.json` contains a non-empty `api_key` string alongside provider/model fields; the application loads this file at boot |
| Impact | Any process or backup with read access to the data directory obtains a billable external credential. Filesystem permissions are the only protection. |
| Severity | **High** |
| Confidence | Confirmed (key presence/shape inspected; value intentionally not read or reproduced) |
| Future improvement | Store secrets in OS keyring/secret service; encrypt at rest with an OS-bound key; redact before logs/backups; rotate the current key |

### F-05 — Paper execution spans multiple stores without a single transaction

| Field | Value |
|---|---|
| Modules | NM-10 execution, NM-03 wallet/ledger, NM-07 portfolio |
| Evidence | `execution/engine.py` saves an inflight journal, performs wallet gross/fee transfers, writes positions in a portfolio transaction, then records fill/order; failures call idempotent compensation; recovery retains pending journal rows |
| Impact | A process crash between writes can leave wallet and portfolio temporarily inconsistent until recovery runs. Compensation itself can fail, requiring operator intervention. |
| Severity | **High** for financial correctness; mitigated by journaling/idempotency |
| Confidence | Confirmed |
| Future improvement | Document and test every crash window; add a recovery drill and invariant checker; consider an outbox/single-database transaction design if the stores remain co-located |

### F-06 — NM-10 writes positions through the portfolio store directly

| Field | Value |
|---|---|
| Modules | NM-10, NM-07 |
| Evidence | `ExecutionEngine._apply_position` accesses `self._portfolios.store`, then `upsert_position`/`record_position_history`; it does not call a public `PortfolioEngine.apply_fill` method |
| Impact | The portfolio service is authoritative for valuation, but execution has intimate write access to its storage. A schema or invariant change in NM-07 can break NM-10 silently; tests may not catch cross-module contract drift. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Define an explicit portfolio fill-application contract owned by NM-07 and make NM-10 call it; keep the store private |

### F-07 — Multiple overlapping planning and agent abstractions

| Field | Value |
|---|---|
| Modules | `planning/`, `agentcore/`, `agents/`, `application.py` |
| Evidence | `PlanningEngine` + `WorkflowExecutor`; `AgentLoop`; `AgenticOrchestrator` + `AdaptivePlanner`; `CoordinatorAgent`; `CollaborationEngine`; separate intent/decision layers feeding them |
| Impact | Unclear which layer owns decomposition/replanning; duplicated concepts (`Plan`, `AgentPlan`, `CollaborationPlan`); higher cognitive load and test surface; inconsistent behavior across entry points. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Publish a decision table for which orchestrator owns which task class; converge shared plan/step contracts; avoid adding a fourth loop |

### F-08 — Two model-management stacks coexist

| Field | Value |
|---|---|
| Modules | `intelligence/model_manager.py`, `models/manager.py`, `models/adaptive.py` |
| Evidence | `OllamaBackend`/`ModelManager` in intelligence; a second `ModelManager`/`ModelBackend`/`HardwareMonitor` under `models/`; adaptive router uses the latter, brain uses the former |
| Impact | Duplicate load/unload behavior, status semantics and residency rules; fixing a probe in one does not fix the other; UI status can disagree with actual brain behavior. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Make one lifecycle manager authoritative, adapt the brain to it, and keep hardware probing behind one interface |

### F-09 — Extremely broad API surface with duplicated inherited routes

| Field | Value |
|---|---|
| Modules | Root and fork FastAPI apps |
| Evidence | 454 HTTP route decorators + 1 WebSocket in root; 630 HTTP + 1 WebSocket in fork; 258 `/market` routes; the fork inherits all NovaControl routes |
| Impact | Large attack surface; route naming/progress inconsistency; documentation drift; harder authorization review; many endpoints share one token and one process. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Generate an authoritative route manifest in CI; group routers by domain; enforce auth/risk policy per router; remove legacy duplicates |

### F-10 — “Multi-agent financial intelligence” and RL are not implemented

| Field | Value |
|---|---|
| Modules | NovaMarket documentation and architecture vocabulary |
| Evidence | No `*Agent` class in `novamarket/`; no RL environment/policy/training; `PI_PHASE = "PI-01"`; RL phases PI-13–PI-15 are roadmap; `novamarket/docs/AGENTS.md` is inherited generic NovaControl text, not a NovaMarket agent team |
| Impact | Stakeholders may believe risk decisions come from collaborating agents or learned policies. In reality they come from deterministic pipelines and statistical models. This is a documentation and governance risk, not a functional failure. |
| Severity | **Moderate** (High if presented externally as implemented) |
| Confidence | Confirmed |
| Future improvement | Use “pipeline stages” in current docs; keep agent/RL labels behind explicit status; require acceptance evidence before exposing roadmap terms in UI |

### F-11 — JSON state is fragmented and lacks a unified schema/migration path

| Field | Value |
|---|---|
| Modules | Persistence, chat, goals, NovaForge, automation, tasks |
| Evidence | Dozens of separate JSON files under `data/`; `JsonStateStore` namespaces; SQLAlchemy models/migrations exist but the default DB is absent |
| Impact | Backup/restore and cross-feature queries are manual; schema evolution is ad hoc; corruption in one file is discovered feature by feature; sensitive state is spread across files. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Define versioned schemas for persisted namespaces; add startup validation and centralized backup; migrate high-value state to the relational store |

### F-12 — Explore and desktop automation expand the trust boundary

| Field | Value |
|---|---|
| Modules | `explore/`, `desktop/`, `browser/`, `phone/` |
| Evidence | Public search/page/video retrieval; page text enters synthesis prompts; desktop/browser actions execute with operator privileges; image/screenshot data can be sent to local/cloud models |
| Impact | Prompt injection from web content, data exfiltration via a cloud model, malicious page behavior, or unintended OS actions if approvals are too permissive. Approval controls reduce but do not eliminate these risks. |
| Severity | **Moderate to High**, depending on execution mode and cloud usage |
| Confidence | Confirmed capability; exploitation not tested |
| Future improvement | Treat retrieved content as untrusted data, isolate it from instructions, add URL/domain policy, require explicit cloud-egress consent per request, and sandbox automation where feasible |

### F-13 — No durable job execution or process supervisor

| Field | Value |
|---|---|
| Modules | Runtime, automation, NovaMarket monitor, autonomy |
| Evidence | In-process asyncio tasks/threads; no Celery/Redis/worker; no process supervisor; startup via Uvicorn; state persists but coroutines do not |
| Impact | A crash loses in-flight work; monitors stop until restart; no automatic restart or health-based failover; long research/backtests cannot survive deploys. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | External supervisor/service definition; explicit restart policy; durable job records with resume semantics; separate long-running workers if needed |

### F-14 — NovaMarket schema is created/migrated ad hoc

| Field | Value |
|---|---|
| Modules | NovaMarket stores |
| Evidence | Stores execute `CREATE TABLE IF NOT EXISTS` and manual additive migration logic; 99 tables in one SQLite file; no Alembic-style migration chain for NovaMarket |
| Impact | Schema drift across versions is hard to audit; rollback is manual; partial migrations can leave a store unusable; tests may create schema differently from production. |
| Severity | **Moderate** |
| Confidence | Confirmed |
| Future improvement | Versioned migrations with forward/backup/rollback procedures; schema checksum at startup; integration test from an empty and an old database |

### F-15 — Event publication failures do not roll back committed state

| Field | Value |
|---|---|
| Modules | Event bus, NovaMarket publishers, execution/risk |
| Evidence | `_emit`/`_publish` catch exceptions and log warnings; committed decisions/fills stand |
| Impact | UI/audit consumers can miss a state change even though the state is committed. This is a deliberate availability trade-off, but it weakens event-driven observability. |
| Severity | **Low to Moderate** |
| Confidence | Confirmed |
| Future improvement | Add durable outbox/replay for critical financial events; separate “best effort UI” from “must-deliver audit” channels |

### F-16 — “Jarvis” naming overstates a backend module

| Field | Value |
|---|---|
| Modules | Web UI, voice, desktop/browser/phone |
| Evidence | No `jarvis` package/class/module; panel is `jarvisPanel`; commands use `/command/plan` and `/command/execute`; backend voice module is separate and browser speech drives the Chat input |
| Impact | Readers may expect a Jarvis agent/assistant that does not exist; maintenance and onboarding confusion; documentation may overpromise. |
| Severity | **Low** |
| Confidence | Confirmed |
| Future improvement | Describe Jarvis as a device-control workspace; if a Jarvis orchestration layer is desired, build it explicitly rather than implying it |

### F-17 — Documentation and route-count claims are inconsistent

| Field | Value |
|---|---|
| Modules | README/docs, API surface |
| Evidence | NovaMarket README cites “25/25 routes” while the current fork API has 258 `/market` route decorators and 630 total HTTP routes; root architecture docs describe older counts |
| Impact | Readers cannot tell whether “routes” means pages, feature groups or HTTP endpoints; acceptance claims appear stale. |
| Severity | **Low** |
| Confidence | Confirmed (measurement method is decorator count) |
| Future improvement | State the counting method; generate counts automatically; link claims to a snapshot date |

## 3. Suspected or unverified issues

| ID | Finding | Evidence | Confidence | Why suspected |
|---|---|---|---|---|
| S-01 | Event bus has no durable delivery/backpressure contract for slow subscribers | In-process publish loop; no external queue | Suspected | Failures are logged; ordering/backpressure behavior under load not measured |
| S-02 | Explore page content can carry prompt injection into synthesis | Page reader feeds text to model prompts | Suspected | No exploit attempted; code treats source text as evidence but not as an explicit untrusted channel |
| S-03 | Large audit/evaluation files will degrade startup/scan performance | 8.57 MB audit; multi-MB JSONL evaluation; JSON state read/write | Suspected | No benchmark run; file sizes are observational |
| S-04 | 99-table NovaMarket schema may contain unused/legacy tables | Table list vs service usage not exhaustively traced | Suspected | Static count alone cannot prove each table is active |
| S-05 | Cloud model routing may leak more context than the user expects | Chat/Explore build prompts from memory, pages and knowledge | Suspected | Execution mode can close cloud, but per-request egress preview was not verified |
| S-06 | Vite dev server is the primary NovaMarket UI; a production static serving path may not be wired in the launcher | `start.ps1` uses dev server; API static dir contains legacy files, not the React build | Suspected | A build script exists, but deployment serving of `web/dist` was not traced end-to-end |

## 4. Recommendations (not implemented)

Prioritized, smallest useful scope:

1. **Resolve the canonical tree** and add a CI assertion that the integrated build contains NovaMarket and the root build does not claim to.
2. **Move the cloud API key out of JSON** into the OS keyring; rotate the exposed credential.
3. **Make API auth non-optional for mutating routes**, or generate a per-install token by default.
4. **Extract application composition** into builders; target a root under a few hundred lines.
5. **Publish one orchestration decision table** and converge plan contracts; do not add another loop.
6. **Define a portfolio fill-application API** so NM-10 no longer touches `PortfolioStore` directly.
7. **Add crash-window tests and an invariant checker** for wallet/ledger/positions; document manual recovery.
8. **Introduce NovaMarket migration versioning** and an empty-to-current upgrade test.
9. **Generate route inventories and documentation counts** from code in CI.
10. **Label current NovaMarket intelligence as pipeline stages**, not agents; keep RL/deep models in a roadmap section until accepted.
11. **Add outbox/replay for critical events** and keep audit delivery distinct from UI notifications.
12. **Add explicit untrusted-content and cloud-egress policy** for Explore and vision prompts.
13. **Add a supervisor and durable job semantics** if long-running work must survive restarts.

## 5. Architecture quality summary

The current architecture is stronger at **safety boundaries** than at **codebase simplicity**. The financial authority chain is unusually explicit, fail-closed and test-oriented. The main weaknesses are structural: two divergent trees, a huge composition root, duplicated model/planning stacks, a very broad API, fragmented local persistence, and documentation that sometimes uses aspirational agent/RL language for deterministic pipelines.

None of these findings justify changing runtime behavior during this documentation task. They are recorded so future work starts from evidence rather than from an idealized architecture.
