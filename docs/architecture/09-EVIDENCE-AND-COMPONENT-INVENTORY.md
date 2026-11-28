# 09 — Architecture Evidence and Component Inventory

All paths are relative to the repository root. “Status” follows the project-wide labels. Counts are from the current checkout on 2026-11-14.

## 1. Repository inventory

| Item | Measurement |
|---|---|
| Root Python files | 490 under `src/novacontrol` |
| Nested fork Python files | 605 under `novamarket/src/novacontrol` |
| NovaMarket Python files | 151 under `novamarket/src/novacontrol/novamarket` |
| Root test files | 160 `test_*.py` under `tests/` |
| NovaMarket test files | 181 `test_*.py` under `novamarket/tests/` |
| Root API routes | 454 HTTP decorators + 1 WebSocket |
| Nested API routes | 630 HTTP decorators + 1 WebSocket |
| NovaMarket `/market` routes | 258 HTTP decorators |
| Root web panels | 14 |
| NovaMarket navigation destinations | 26 |
| NovaMarket primary DB | 99 tables, 35,827,712 bytes |
| Root active memory DB | 1 table, 205 rows, 479,232 bytes |

Route counts were measured by matching `@app.<method>("…")` decorators in `api/app.py`. They count endpoints, not unique business capabilities.

## 2. Major component inventory

| Module | Status | Purpose | Entry point / interface | Primary evidence |
|---|---|---|---|---|
| Composition root | IMPLEMENTED | Construct and wire the application | `NovaControlApplication` | `src/novacontrol/application.py:1388` |
| Runtime/event bus | IMPLEMENTED | Module lifecycle and typed events | `runtime.start/stop`, `EventBus.publish/subscribe` | `core/runtime.py`, `core/events.py` |
| FastAPI surface | IMPLEMENTED | HTTP/SSE/WS interface | `create_app`, `/ask`, `/explore`, `/events/stream`, `/ws/events` | `api/app.py`, `api/models.py` |
| Web SPA | IMPLEMENTED | Operator workspace | `/`, 14 panels | `web/static/index.html`, `web/static/js/` |
| Chat/Brain | IMPLEMENTED | Conversation, provider selection, response shaping | `POST /ask` → `handle_request` → `NovaBrain.chat` | `brain/brain.py`, `brain/conversation.py` |
| Explore | IMPLEMENTED | Web research and evidence synthesis | `POST /explore` → `ExploreService.research` | `explore/service.py:116` |
| Jarvis workspace | PARTIALLY_IMPLEMENTED | Device-control UI and command lifecycle | `jarvisPanel`, `/command/plan`, `/command/execute` | `web/static/index.html:617`, `api/app.py:1188` |
| Memory | IMPLEMENTED | Persistent lexical memory and context | `MemoryManager`, event module | `memory/manager.py`, `memory/stores.py` |
| Personal knowledge | IMPLEMENTED | User/project knowledge records | `PersonalKnowledgeService` | `memory/personal.py:630` |
| NLU | IMPLEMENTED | Normalize, match, extract, resolve references | `GlobalInputIntelligence.understand_async` | `intelligence/engine.py` |
| Decision | IMPLEMENTED | Select route/executor without executing | `DecisionEngine.decide_async` | `decision/engine.py`, `decision/models.py` |
| Jev-compatible provider | PARTIAL / OPTIONAL | External advisory decision route | `JevDecisionProvider` | `decision/providers.py:589` |
| Planner | IMPLEMENTED | Deterministic plan generation | `PlanningEngine.create_plan` | `planning/engine.py:22` |
| Agent loop | IMPLEMENTED | Understand–decide–plan–execute–verify | `AgentLoop.run` | `planning/agent.py:137` |
| Agentic orchestrator | IMPLEMENTED | UI observe/act/verify/recover loop | `AgenticOrchestrator.run_task` | `agentcore/orchestrator.py:49` |
| Specialist agents | IMPLEMENTED | Developer/research/data/verification work | `AgentRegistry`, `SpecialistPipeline` | `agents/` |
| Collaboration | IMPLEMENTED | Assess, decompose, run, aggregate, conflict-check | `CollaborationEngine.run` | `agents/collaboration.py:1147` |
| Tool registry/catalog | IMPLEMENTED | Declare and register tools | `ToolRegistry`, 24 metadata entries | `tools/registry.py`, `tools/catalog.py` |
| Tool selection | IMPLEMENTED | Deterministic choice with evidence | `ToolSelector.select` | `tools/selection.py:193` |
| Tool execution | IMPLEMENTED | Validate, permission-gate, run, cache | `ToolExecutor.execute` | `tools/executor.py:58` |
| Permissions/approvals | IMPLEMENTED | Risk and permission decisions | `PermissionManager`, approval gateways | `reliability/permissions.py`, `core/security.py` |
| Vision | IMPLEMENTED | OCR-first image/screen understanding | `VisionManager.analyze`, `VisionController` | `vision/manager.py`, `desktop/vision.py` |
| Perception | IMPLEMENTED | Frames, scenes, tracking, VLM escalation | `PerceptionEngine.perceive` | `perception/engine.py` |
| Models | IMPLEMENTED | Lifecycle, fit, routing | `ModelManager`, `AdaptiveModelRouter` | `models/manager.py`, `models/adaptive.py` |
| Legacy model manager | IMPLEMENTED / DUPLICATE | Brain-local Ollama lifecycle | `intelligence/model_manager.py` | same |
| Hardware telemetry | IMPLEMENTED | RAM/CPU/GPU/NPU readings | `HardwareMonitor`, `SystemTelemetry` | `models/hardware.py`, `telemetry/hardware.py` |
| Plugins | IMPLEMENTED | Discover, install, trust, load, enable, withdraw | `PluginMarketplace`, `PluginManager` | `plugins/` |
| Reliability | IMPLEMENTED | Task state, control, recovery, verification | `TaskController`, `RecoveryEngine` | `reliability/` |
| Desktop automation | IMPLEMENTED | Plan/execute/verify desktop actions | `DesktopAutomationController` | `desktop/` |
| Browser automation | IMPLEMENTED | Navigate, search, extract | `BrowserAutomationController` | `browser/` |
| Phone control | PARTIAL / DEVICE-DEPENDENT | Bridge to phone companion | `PhoneControlModule`, `/phone/status` | `phone/` |
| Voice backend | IMPLEMENTED, SEPARATE | Event-driven speech surface | `VoiceModule` | `voice/runtime.py` |
| Voice browser UI | IMPLEMENTED | Web Speech recognition/synthesis | `voice.js` | `web/static/js/voice.js` |
| Automation | IMPLEMENTED | Scheduled automations | `AutomationManager`, ticker | `automation/`, `application.py` |
| Knowledge | IMPLEMENTED | Search, ingest, project context | `KnowledgeModule` | `knowledge/` |
| Evaluation | IMPLEMENTED | Trajectory recording and metrics | `EvaluationModule`, JSONL | `evaluation/` |
| Training/Preference/RLHF/RLVR | IMPLEMENTED as separate governed surfaces | Local training and review workflows | CLI/API modules | `training/`, `preference/`, `rlhf/`, `rlvr/` |
| NovaForge | IMPLEMENTED | Game/desktop orchestration and evaluation | `novaforge/` | 40 files |
| CLI | IMPLEMENTED | Local commands | `python -m novacontrol …` | `__main__.py`, `cli/` |
| GUI | IMPLEMENTED (optional PySide6) | Desktop dashboard | `novacontrol-dashboard` | `gui/` |

## 3. NovaMarket component inventory

| Domain | Status | Main components | Data owned | Evidence |
|---|---|---|---|---|
| Integration/lifecycle | IMPLEMENTED | `NovaMarketModule`, `NovaMarketService` | `novamarket` state namespace, lifecycle history | `novamarket/module.py`, `service.py` |
| Configuration | IMPLEMENTED | `NovaMarketConfig` | `data/novamarket.json` | `config.py` |
| Market data | IMPLEMENTED | `MarketDataEngine`, gateway, providers, store | `market_*` | `marketdata/` |
| Event radar | IMPLEMENTED | `MarketEventRadar`, stores | `market_events`, evidence, mappings | `radar/` |
| Discovery/watchlists | IMPLEMENTED | `AssetDiscoveryEngine`, `DiscoveryStore` | `discovery_*` | `discovery/` |
| Wallet/ledger | IMPLEMENTED | `WalletEngine`, `WalletStore` | `wallets`, `wallet_transactions`, `ledger_entries` | `wallet/` |
| Portfolios | IMPLEMENTED | `PortfolioEngine`, `PortfolioStore` | `portfolios`, positions, valuations | `portfolio/` |
| Forecasting | IMPLEMENTED | `ForecastEngine` | `forecast_*` | `forecasting/` |
| Statistical forecasting | IMPLEMENTED (PI-05) | `StatisticalForecastingService`, 11 estimators | statistical tables in primary DB | `statistical/` |
| Datasets/features | IMPLEMENTED (PI-02/PI-04) | `HistoricalDatasetService`, `FeatureService` | dataset/feature tables | `datasets/`, `features/` |
| Prediction contracts/service | IMPLEMENTED (PI-01) | `PredictionIntelligenceService`, `EnsembleAssembler` | `prediction_*` | `prediction/` |
| Risk | IMPLEMENTED | `RiskEngine`, policy/metrics/store | `risk_*` | `risk/` |
| Strategies | IMPLEMENTED | `StrategyEngine`, catalog, lifecycle | `strateg*` | `strategies/` |
| Paper execution | IMPLEMENTED | `ExecutionEngine`, pricing, reconciliation | `execution_*` | `execution/` |
| Backtesting | IMPLEMENTED | `BacktestingService` | isolated backtest DB/tables | `backtesting/` |
| Shadow | IMPLEMENTED | `ShadowService` | separate shadow DB | `shadow/` |
| Learning | IMPLEMENTED | `LearningService` | separate learning DB | `learning/` |
| Autonomy shell | IMPLEMENTED | `AutonomyRuntime`, monitoring | `autonomy_*` | `autonomy/` |
| Autonomy decisions | IMPLEMENTED | `AutonomyDecisionPipeline` | `decision_*` | `decision/pipeline.py` |
| Autonomy execution | IMPLEMENTED | `AutonomyPaperExecution` | `autonomy_execution_*` | `autonomy_execution/` |
| Exit supervisor | IMPLEMENTED | `AutonomyExitSupervisor` | `autonomy_exit_*` | `autonomy_exits/` |
| React desk | IMPLEMENTED | 26 destinations, Vite proxy | no direct DB | `novamarket/web/` |
| Named financial agents | NOT_IMPLEMENTED | — | — | no `*Agent` class in package |
| RL policy/training | PLANNED | — | contract only | PI-13–PI-15 |

## 4. Interface inventory

### Root HTTP groups

`/health`, `/status`, `/system/*`, `/settings`, `/ask`, `/brain/*`, `/models/*`, `/chat/*`, `/tasks/*`, `/improve*`, `/learn`, `/knowledge*`, `/memory/personal/*`, `/train`, `/plan*`, `/build/*`, `/command/*`, `/desktop/*`, `/browser/*`, `/phone/*`, `/vision/*`, `/novaforge/*`, `/explore*`, `/events/stream`, `/ws/events`, `/perception/*`, `/evaluation/*`, `/training/*`, `/preference/*`, `/rlhf/*`, `/rlvr/*`, `/plugins/*`, `/automation/*`, `/skills/*`, `/goals/*`, `/world/*`, `/embodied/*`, `/generalization/*`.

The count above (454) is larger than this group list because many features expose multiple CRUD/status endpoints.

### Event families

`runtime.*`, `intent.detected`, `context.resolved`, `decision.created`, `plan.created`, `task.*`, `tool.*`, `verification.*`, `recovery.*`, `model.*`, `vision.*`, `voice.*`, `plugin.*`, `agent.*`, `explore.*`, `knowledge.*`, desktop/browser/phone progress, and NovaMarket `novamarket.*`.

### NovaMarket HTTP groups

`/market/*` includes workspace, overview, contracts, config, enable/disable, autonomy (contracts/status/health/reliability/recover/start/pause/resume/stop/session/monitoring/decisions/executions/exits), wallets/ledger, events, discovery, watchlists, forecasts, portfolios, risk, strategies, execution, backtests, shadow, learning, notifications, activity and audit.

## 5. Module evidence records

### Explore

- **Purpose:** answer a research question from web sources with evidence and citations.
- **Entry:** `POST /explore`, `ExploreModule`, Chat research routing.
- **Components:** `ExploreService`, `plan_research`, `ResilientSearchProvider`, `page_reader`, `build_evidence`, `Synthesizer`.
- **Inputs:** topic, depth, source/video limits, prior topics, conversation context.
- **Outputs:** `ExploreReport` with summary, sections, sources, videos, warnings, provider status.
- **Dependencies:** search providers, public web pages, optional model, cache.
- **Persistence:** in-process cache/recent topics; memory/transcript via application.
- **Status:** IMPLEMENTED.
- **Evidence:** `explore/service.py:116–225`, `explore/providers.py`, `explore/synthesizer.py`, `api/app.py:6119`.

### Chat

- **Purpose:** conversation and final response shaping.
- **Entry:** `POST /ask`.
- **Inputs:** text, optional attached image, memory/context.
- **Outputs:** `ApplicationResponse`, provider/mode metadata, transcript turns.
- **Dependencies:** NLU, decision, model provider/router, memory.
- **Persistence:** `ConversationManager` in memory; `chat_transcript.json`; memory namespace.
- **Status:** IMPLEMENTED.
- **Evidence:** `brain/brain.py`, `application.py:3760`, `core/chat_transcript.py`.

### Jarvis

- **Purpose:** operator device-control workspace.
- **Entry:** `jarvisPanel`, `/command/plan`, `/command/execute`.
- **Inputs:** typed/spoken command, device tab.
- **Outputs:** plan, approval token, execution results, bridge state.
- **Dependencies:** desktop/browser/phone controllers, approvals, optional browser speech.
- **Persistence:** conversation/command history and bug log; no Jarvis DB.
- **Status:** PARTIALLY_IMPLEMENTED as a named module; underlying capabilities implemented.
- **Evidence:** `web/static/index.html:617`, `render-panels.js`, `voice.js`, `api/app.py:1188`.

### Memory/context

- **Purpose:** retain and retrieve useful state.
- **Entry:** `MemoryManager.remember/retrieve`; personal knowledge API.
- **Inputs:** namespace, key, text, metadata, importance, TTL.
- **Outputs:** records and ranked lexical results; context blocks.
- **Dependencies:** SQLite repository, personal knowledge service, conversation manager.
- **Persistence:** `data/memory.sqlite3`; JSON transcript.
- **Status:** IMPLEMENTED.
- **Evidence:** `memory/manager.py`, `memory/stores.py:109`, `memory/personal.py:630`.

### NLU/decision

- **Purpose:** turn text into a structured intent and a non-executing route decision.
- **Inputs:** text, image presence, context, environment capabilities.
- **Outputs:** `UnderstandResult`, `Decision`.
- **Dependencies:** rules, registry, semantic matcher, context, optional external advisor.
- **Persistence:** telemetry trace; no decision database.
- **Status:** IMPLEMENTED.
- **Evidence:** `intelligence/engine.py`, `decision/engine.py`, `decision/providers.py`.

### Planner/agent execution/tools

- **Purpose:** decompose, sequence, execute, verify and recover work.
- **Inputs:** goal, intent, plan, tool results, UI state.
- **Outputs:** `Plan`, `WorkflowResult`, `AgentRun`, `AgentResponse`, `ToolResult`.
- **Dependencies:** tool executor, permissions, verification/recovery, model.
- **Persistence:** task records, agent/collaboration JSON, evaluation trajectories.
- **Status:** IMPLEMENTED.
- **Evidence:** `planning/`, `agentcore/`, `agents/`, `tools/`.

### Vision/models/plugins/reliability

Covered in [03](03-NOVACONTROL-MODULE-ARCHITECTURES.md) with per-module inputs, outputs, persistence and evidence.

## 6. NovaMarket authority evidence

| Claim | Source |
|---|---|
| NM-08 owns risk decisions | `risk/engine.py:244` `assess`, `:1558` `apply_decision` |
| NM-08 is sole risk authority | `decision/pipeline.py:8`, `execution/engine.py` revalidation, `risk/engine.py` persistence |
| NM-10 owns paper orders/fills | `execution/engine.py:197` `submit`, `:933` inflight, `:976–1006` wallet transfers, `:1308` position effect |
| No broker/live path | `execution/__init__.py`, `execution/engine.py`, explicit false constants in `decision/models.py` and `autonomy/models.py` |
| No short selling | `execution/engine.py` sell-position check; `strategies/catalog.py` position rules |
| Ledger authority | `wallet/engine.py`, `wallet/store.py`, `ledger_entries` |
| Portfolio authority | `portfolio/engine.py`, `portfolio/store.py` |
| Autonomy gates | `autonomy/runtime.py`, `decision/pipeline.py`, `autonomy_execution/pipeline.py` |

## 7. Data and runtime evidence

- **Root memory:** `data/memory.sqlite3`; one table `memory_records`; 205 rows.
- **Root transcript:** `data/chat_transcript.json`, 30,006 bytes.
- **Root audit:** `data/audit/audit.jsonl`, 8,573,899 bytes.
- **Root cloud config:** `data/cloud_llm.json` contains a plaintext `api_key` field (value not read).
- **NovaMarket config:** `novamarket/data/novamarket.json`, versioned config with providers, ingestion, freshness, cache, quality, resources and notifications.
- **NovaMarket primary DB:** 99 tables and the row counts in [04 §8](04-NOVAMARKET-ARCHITECTURE.md#8-current-data-snapshot).
- **Shadow DB:** 6 tables; 2 sessions, 1 decision, 112 error rows at earlier inspection.
- **Learning DB:** 10 tables; 5 candidates, 4 experiments, 0 versions/approvals at earlier inspection.
- **Root start:** `start.bat` → Uvicorn `novacontrol.api.app:create_app` on `127.0.0.1:8000`.
- **NovaMarket start:** `novamarket/start.ps1` → API `8011`, Vite `5173`, reuses existing servers when possible.

## 8. Verification limitations

- No live application was started and no browser interaction was performed for this documentation task.
- Route counts are static decorator matches, not a runtime OpenAPI dump.
- The NovaMarket API's full returned schema was not enumerated; documented paths are the major groups.
- The plaintext key was detected by field name/type and length only; its value was not printed.
- Current database row counts describe this checkout, not production usage or acceptance totals.
- “No code path found” means the searched package/files did not contain it; it is not proof that an imported optional dependency could never provide it.
