# NovaControl & NovaMarket — Current Architecture Discovery Report

**Report date:** 2026-11-14  
**Scope:** the repository currently on disk, including the root NovaControl tree and the nested NovaMarket fork.  
**Mode:** discovery and documentation only. No application code, schema, API, configuration, dependency, branch or runtime behavior was changed.

## 1. Repository inspected

| Item | Finding |
|---|---|
| Root application | `src/novacontrol`, 490 Python files |
| Nested application/fork | `novamarket/src/novacontrol`, 605 Python files |
| NovaMarket package | 151 Python files under the nested `novamarket/` package |
| Root tests | 160 `test_*.py` files |
| NovaMarket tests | 181 `test_*.py` files |
| Root runtime data | `data/memory.sqlite3`, JSON state, `data/audit/audit.jsonl` |
| NovaMarket runtime data | `data/novamarket.sqlite3` (99 tables), shadow and learning SQLite files, shared audit |
| Root API surface | 454 HTTP route decorators + 1 WebSocket |
| Nested API surface | 630 HTTP + 1 WebSocket; 258 `/market` routes |
| Root UI | No-build vanilla SPA, 14 panels |
| NovaMarket UI | React 19 + TypeScript + Vite, 26 navigation destinations |
| Frontend build | Root none; NovaMarket Vite build |
| Deployment | Local Uvicorn + local SQLite/JSON; no Docker, Redis, Celery or PostgreSQL in the default path |

## 2. Current overall architecture

NovaControl is a **modular monolith for a single operator**, not a microservice platform. One composition root wires a FastAPI interface, an event-driven runtime, deterministic NLU/decision, a model-backed brain, planning/agents/tools, local memory, perception and automation. Most modules run in one Python process.

NovaMarket is a **financial-intelligence subsystem inside the nested fork**, not in the root application. It composes the fork's event bus, permission manager, capability registry, state store and audit logger, while keeping its financial stores separate. It is paper-only and has no broker or live-order path.

The most consequential architectural fact in this repository is therefore a scope fact:

> The root tree is NovaControl without NovaMarket. The nested fork is the only actual integrated NovaControl + NovaMarket application.

Any diagram or document that shows NovaMarket as part of the root application is inaccurate for this checkout.

## 3. NovaControl architectural layers

Confirmed layers:

1. **Experience:** root SPA, CLI, PySide6 GUI, browser speech; React desk only in the fork.
2. **Interface:** FastAPI, optional bearer token, SSE, WebSocket, static assets.
3. **Understanding:** normalization, rules, lexical/semantic matching, references, multi-intent assessment, context and memory.
4. **Decision:** deterministic route/executor selection with optional Jev-compatible advisor.
5. **Orchestration:** brain, planning engine, agent loop, agentic UI orchestrator, specialist agents and collaboration.
6. **Execution:** tool executor, Explore, desktop/browser/phone, NovaMarket domains, plugins.
7. **Perception:** VisionManager, VisionController, PerceptionEngine.
8. **Model infrastructure:** Ollama, optional cloud, adaptive routing, hardware telemetry.
9. **Platform:** EventBus/runtime, permissions/approvals, recovery/verification, audit, JSON/SQLite persistence.

The layers are logical, not separately deployed services.

## 4. Major NovaControl modules documented

- Composition root and event-driven runtime.
- FastAPI/API/SSE/WebSocket and SPA.
- Explore.
- Chat/Brain.
- Jarvis/device control.
- Memory and context.
- NLU and decision engine.
- Planner and task decomposition.
- Agent execution and collaboration.
- Tool selection and execution.
- Vision and perception.
- Model and hardware management.
- Plugin and capability management.
- Reliability, recovery and lifecycle.
- Supporting automation, knowledge, evaluation, training, preference, RLHF/RLVR, NovaForge, CLI and GUI inventory entries.

## 5. Individual module diagrams completed

Document 03 has twelve module diagrams: Explore, Chat, Jarvis, Memory/Context, NLU/Decision, Planner, Agents, Tools, Vision/Perception, Models/Hardware, Plugins and Reliability. It also contains one planner task-lifecycle sequence and one tool-execution sequence.

The diagrams show inputs, internal components, processing, outputs, persistence, dependencies and cross-module communication.

## 6. NovaMarket architecture

NovaMarket is implemented in the nested fork with:

- a `NovaMarketService` that lazily composes all domain engines;
- a thin `NovaMarketModule` for runtime lifecycle;
- shared fork event bus, permissions, capabilities, state store and audit;
- separate SQLite stores for primary financial data, shadow mode and adaptive learning;
- a React desk with 26 navigation destinations and 258 `/market` HTTP routes;
- paper-only wallet, ledger, portfolio, risk, strategy, execution, backtest, shadow and learning domains;
- NM-01 through NM-14 accepted as implemented; PI-01/PI-02/PI-04/PI-05 implemented additively;
- no implementation of named financial agents, an RL environment, live trading or broker connectivity.

## 7. NovaMarket major domain diagrams

Document 04 contains separate diagrams for:

- virtual market and financial system;
- market data intelligence;
- prediction intelligence;
- financial research intelligence;
- autonomous trading intelligence;
- reinforcement learning/adaptive learning boundary;
- multi-agent claim versus actual pipeline;
- portfolio management;
- risk intelligence;
- dashboard/UI.

Each section states implemented, partial and planned behavior.

## 8. Financial authority architecture

The authority chain is confirmed:

- **NM-08 RiskEngine** is the sole risk authority. It persists decisions with policy version, state version and expiry. Its outcomes are approved, rejected, restricted, requires_review or insufficient_data.
- **NM-10 ExecutionEngine** is the sole paper execution engine. It creates orders/fills/reservations, has no broker client and revalidates the exact NM-08 decision before accepting an order.
- **NM-03 wallet/ledger** is authoritative for NovaCoin movements.
- **NM-07 portfolio** is authoritative for portfolios, positions, valuation and performance.
- Analytical modules produce data, evidence, forecasts and proposals; they do not move money or create orders.
- RL policies have no production authority because no RL policy/training implementation exists.

Two caveats are documented rather than hidden:

1. NM-10 writes position rows directly through `PortfolioStore`, not through a public portfolio mutation contract.
2. Wallet, position and execution writes are compensated across separate stores; they are not one atomic transaction.

The complete paper order sequence is in [04 §6](04-NOVAMARKET-ARCHITECTURE.md#6-paper-order-lifecycle).

## 9. Cross-system integration flows

Document 07 contains nine sequences:

- chat request;
- Explore research;
- Jarvis command;
- complex task execution;
- visual perception/action;
- NovaMarket prediction;
- NovaMarket autonomous paper trading;
- risk management;
- NovaControl/NovaMarket integration.

Where the implementation differs from the requested conceptual flow, the difference is stated. The most important differences: Chat does not use an LLM function-calling loop; Jarvis is a UI/device workflow rather than a backend agent; NovaMarket’s “multi-agent” functions are deterministic pipeline stages; NovaMarket integration exists only in the fork.

## 10. Data architecture

Documented:

- local JSON/SQLite topology;
- root memory and transcript flows;
- NovaMarket market/portfolio/risk/execution flows;
- a grouped logical ER diagram;
- authoritative ownership by domain;
- cross-database boundaries;
- retention, backup and recovery;
- measured store sizes and row counts.

Observed current state includes 205 memory rows, 4,813 instruments, 33,561 bars, 7 portfolios, 0 positions, 0 orders, 0 fills, 1 risk decision, 6 forecast records and 2 backtest rows. These are local-state facts, not acceptance totals.

## 11. Runtime and deployment architecture

Documented actual startup, shutdown, processes, ports, background tasks and external integrations.

- Root: `start.bat` → Uvicorn on `127.0.0.1:8000`.
- Fork: `start.ps1` → API on `127.0.0.1:8011`, Vite on `127.0.0.1:5173`.
- Default persistence is local; no database/cache/queue server.
- Long-lived work is in-process asyncio tasks or threads; no durable worker or supervisor.
- Shutdown is ordered and isolates module failures.
- Local-only binding is the main network boundary; API auth is optional unless a token is configured.

## 12. Architecture evidence and traceability

Document 09 provides:

- file and route counts;
- module/status/entry/evidence inventory;
- NovaMarket domain inventory;
- interface families;
- authority evidence;
- data/runtime evidence;
- verification limitations.

A representative set of high-value evidence:

- `application.py:1388` — composition root;
- `application.py:2431–2470` — runtime module registration;
- `api/app.py:522` — `/ask` → `handle_request`;
- `explore/service.py:116` — research orchestration;
- `decision/models.py` — route/executor contract;
- `tools/executor.py:58` — central execution gate;
- `reliability/permissions.py:271` — permission manager;
- `novamarket/module.py` — NovaMarket runtime adapter;
- `novamarket/src/novacontrol/novamarket/service.py:739` — service composition;
- `risk/engine.py:244` — risk assessment;
- `execution/engine.py:197` — paper submission;
- `execution/engine.py:933–1006` — journalled fill and wallet transfers;
- `execution/engine.py:1308` — position effect.

## 13. Confirmed architectural findings

Seventeen confirmed findings are recorded in [08](08-ARCHITECTURE-FINDINGS.md). The highest-impact are:

- root/nested fork divergence;
- 14,397-line composition root;
- optional single-token API authentication;
- plaintext cloud API key in `data/cloud_llm.json`;
- non-atomic cross-store paper execution;
- NM-10 direct portfolio-store writes;
- overlapping planning/agent/model abstractions;
- 454/630-route API surfaces;
- absent “agent team”/RL capabilities despite aspirational language;
- fragmented JSON persistence;
- expanded trust boundary from web/desktop automation;
- no durable job execution/process supervisor;
- ad hoc NovaMarket schema management;
- best-effort event delivery for committed state;
- “Jarvis” naming overstating a backend module;
- inconsistent documentation/route counts.

## 14. Suspected issues

Six are explicitly marked suspected, not confirmed:

- event bus backpressure/durable delivery under load;
- prompt injection from Explore page content;
- large-file startup/scan degradation;
- unused/legacy NovaMarket tables;
- cloud prompt context exceeding user expectation;
- production serving path for the prebuilt React UI.

No exploit or performance benchmark was run.

## 15. Recommended future improvements

Recommendations are separated from findings and were **not implemented**:

1. choose a canonical source tree and assert integration in CI;
2. move cloud keys to an OS keyring and rotate the existing key;
3. require API auth for mutations or generate a per-install token;
4. extract composition builders from `application.py`;
5. publish one orchestration ownership table;
6. define a portfolio fill-application interface;
7. add crash-window tests and invariant checks for financial stores;
8. add versioned NovaMarket migrations;
9. generate route/doc counts in CI;
10. label financial intelligence by actual pipeline stage;
11. add durable outbox/replay for critical events;
12. define untrusted-content and cloud-egress policy;
13. add a supervisor and durable jobs for long-running work.

## 16. Documentation files created

All under `docs/architecture/`:

1. `00-CURRENT-ARCHITECTURE-REPORT.md` (this report)
2. `01-SYSTEM-ARCHITECTURE-OVERVIEW.md`
3. `02-NOVACONTROL-ARCHITECTURE.md`
4. `03-NOVACONTROL-MODULE-ARCHITECTURES.md`
5. `04-NOVAMARKET-ARCHITECTURE.md`
6. `05-DATA-ARCHITECTURE.md`
7. `06-RUNTIME-AND-DEPLOYMENT.md`
8. `07-CROSS-SYSTEM-WORKFLOWS.md`
9. `08-ARCHITECTURE-FINDINGS.md`
10. `09-EVIDENCE-AND-COMPONENT-INVENTORY.md`
11. `10-ARCHITECTURE-INDEX.md`

## 17. Diagram validation results

- **Mermaid blocks:** 52.
- **Parser/render check:** all 52 were loaded into Mermaid 11 in the local preview browser and rendered with `mermaid.render`.
- **Result:** 52/52 succeeded, 0 failures.
- **Code fences:** balanced in every document.
- **Cross-document links and anchors:** 58 internal links checked, 0 broken.
- **No screenshots:** diagrams were validated by actual render calls, not visually inspected one by one. Mermaid rendering does not prove a diagram is semantically complete; the accompanying prose and evidence tables carry that responsibility.

**Validation record:** `52 diagrams rendered; 0 failed`; `58 internal links; 0 broken`. Temporary validation HTML was deleted after the check.

## 18. Missing or partially implemented components

| Capability | Status | Meaning |
|---|---|---|
| NovaMarket in root NovaControl | NOT_IMPLEMENTED | No package/import/registration in the root tree |
| Jarvis backend module | NOT_IMPLEMENTED | No Jarvis package/class; UI and device capabilities exist |
| NovaMarket financial agent team | NOT_IMPLEMENTED | Deterministic pipelines, no financial agent classes |
| NovaMarket RL environment/policy training | PLANNED | PI-13–PI-15; no training store or policy authority |
| Sequence/deep prediction models | PLANNED | PI-06 |
| Full ensemble selection/calibration program | PARTIAL/PLANNED | Contract and assembler exist; PI-07 not complete |
| Fundamental statements/ratios/sentiment | NOT_IMPLEMENTED | PI-10 is planned |
| Live broker/real-money trading | NOT_IMPLEMENTED | Explicitly absent |
| Short selling | NOT_IMPLEMENTED | Sell requires existing long quantity |
| Durable distributed jobs/workers | NOT_IMPLEMENTED | In-process tasks only |
| Multi-user accounts/roles | NOT_IMPLEMENTED | Single optional API token |
| Database/cache servers | NOT_ACTIVE | Optional extras only |

## 19. Final documentation completeness assessment

**Complete for the requested current-state architecture scope.**

- All required module sections exist with individual diagrams.
- NovaMarket domains A–J each have a section and diagram.
- Financial authority and order lifecycle are explicit.
- Cross-system flows A–I are covered.
- Data and runtime architectures are covered.
- Findings separate confirmed from suspected and recommendations from implementation.
- Evidence is traceable to source paths and, where useful, line numbers.
- Planned/partial/missing capabilities are labelled.
- No application behavior was changed.

**Residual limitations**

- No live boot, API call, browser interaction or database write was performed solely for this documentation.
- Route counts are static decorator counts.
- NovaMarket’s 99-table schema was not exhaustively traced table by table.
- No external penetration, accessibility or investment-performance testing was performed.
- Mermaid render success does not replace visual review on every renderer; the diagrams use standard Mermaid 11 features.

**Bottom line:** the repository’s real architecture is a capable but heavily consolidated local AI platform, plus a separately forked and well-guarded paper-trading subsystem. Its strongest design property is the separation of decision, permission, risk and paper execution. Its largest structural risks are the duplicate source tree, the monolithic composition root, optional API security, plaintext cloud credentials and non-atomic multi-store financial writes.
