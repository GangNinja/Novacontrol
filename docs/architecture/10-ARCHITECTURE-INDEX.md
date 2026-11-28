# 10 — Architecture Index

This is the navigation and diagram index for the current-state architecture documentation.

## 1. Scope note

The repository contains two source trees. Read [01 §1](01-SYSTEM-ARCHITECTURE-OVERVIEW.md#1-scope-and-an-important-repository-finding) first. In short:

- root `src/novacontrol` = NovaControl without NovaMarket;
- nested `novamarket/src/novacontrol` = the integrated NovaControl + NovaMarket application;
- root and nested trees are not the same codebase and must not be treated as interchangeable.

## 2. Documents

| # | Document | What it answers |
|---|---|---|
| 00 | [Current Architecture Discovery Report](00-CURRENT-ARCHITECTURE-REPORT.md) | Consolidated findings, coverage, validation and completeness |
| 01 | [System Architecture Overview](01-SYSTEM-ARCHITECTURE-OVERVIEW.md) | Scope, layers, subsystem map, dependencies, status summary |
| 02 | [NovaControl Architecture](02-NOVACONTROL-ARCHITECTURE.md) | Composition root, runtime, API, request path, shared infrastructure |
| 03 | [NovaControl Module Architectures](03-NOVACONTROL-MODULE-ARCHITECTURES.md) | Twelve module-level architectures with diagrams |
| 04 | [NovaMarket Architecture](04-NOVAMARKET-ARCHITECTURE.md) | Integration, domains A–J, financial authority and order lifecycle |
| 05 | [Data Architecture](05-DATA-ARCHITECTURE.md) | Stores, flows, logical ER, ownership and retention |
| 06 | [Runtime and Deployment](06-RUNTIME-AND-DEPLOYMENT.md) | Processes, startup/shutdown, integrations, security posture |
| 07 | [Cross-System Workflows](07-CROSS-SYSTEM-WORKFLOWS.md) | Nine end-to-end execution flows |
| 08 | [Findings and Recommendations](08-ARCHITECTURE-FINDINGS.md) | Strengths, confirmed issues, suspected issues, recommendations |
| 09 | [Evidence and Component Inventory](09-EVIDENCE-AND-COMPONENT-INVENTORY.md) | Files, counts, interfaces, status, evidence and limitations |

## 3. Start by task

| I want to… | Read |
|---|---|
| Understand what the system is | 01, then 02 |
| Understand a specific module | 03 section for that module |
| Understand NovaMarket | 04, especially §5 authority and §6 order lifecycle |
| Trace a request | 02 §4, then 07 Flow A |
| Trace research | 03 §3.1 and 07 Flow B |
| Understand “Jarvis” | 03 §3.3 |
| Check data ownership | 05 §5 |
| Run or deploy it | 06 |
| Review risks | 08 |
| Verify a claim | 09 |
| Find a diagram | §4 below |

## 4. Diagram index

There are **52 Mermaid diagrams**. All are static-source diagrams; they were not generated from runtime traces.

### Document 01 — 3 diagrams

1. Architectural layers and cross-layer relationships.
2. Major subsystem map.
3. Internal and external dependency map.

### Document 02 — 4 diagrams

1. Runtime, event bus and store topology.
2. Request-to-response sequence.
3. Intelligence-to-executor routing.
4. Internal package dependency graph.

### Document 03 — 12 module diagrams

1. Explore architecture.
2. Chat architecture.
3. Jarvis/device-control architecture.
4. Memory and context architecture.
5. NLU and decision architecture.
6. Planner/task-lifecycle sequence.
7. Agent execution and collaboration.
8. Tool selection and execution sequence.
9. Vision and perception.
10. Model and hardware management.
11. Plugin lifecycle.
12. Reliability task-state lifecycle.

### Document 04 — 13 diagrams

1. NovaControl–NovaMarket integration.
2. Virtual market and financial system.
3. Market data intelligence.
4. Prediction intelligence.
5. Financial research intelligence.
6. Autonomous trading intelligence.
7. Reinforcement learning and adaptive learning boundary.
8. Multi-agent claim versus actual pipeline.
9. Portfolio management.
10. Risk intelligence.
11. Dashboard/UI.
12. Financial authority architecture.
13. Paper order lifecycle sequence.

### Document 05 — 5 diagrams

1. High-level data topology.
2. NovaControl data flows.
3. NovaMarket data flows.
4. Logical entity relationships.
5. Authoritative data ownership.

### Document 06 — 6 diagrams

1. Process and service relationships.
2. Root startup sequence.
3. NovaMarket startup sequence.
4. Shutdown sequence.
5. External integrations.
6. Deployment architecture.

### Document 07 — 9 diagrams

1. Flow A — Chat request.
2. Flow B — Explore research.
3. Flow C — Jarvis command.
4. Flow D — Complex task execution.
5. Flow E — Visual perception and action.
6. Flow F — NovaMarket prediction.
7. Flow G — NovaMarket autonomous paper trading.
8. Flow H — Risk management.
9. Flow I — NovaControl/NovaMarket integration.

Documents 08, 09 and this index contain no Mermaid diagrams.

## 5. Module index

### NovaControl

| Module | Section |
|---|---|
| Composition/runtime | [02 §1–2](02-NOVACONTROL-ARCHITECTURE.md#1-composition-root) |
| API and UI | [02 §3](02-NOVACONTROL-ARCHITECTURE.md#3-interface-layer) |
| Request routing | [02 §4](02-NOVACONTROL-ARCHITECTURE.md#4-request-to-response-architecture) |
| Explore | [03 §3.1](03-NOVACONTROL-MODULE-ARCHITECTURES.md#31-explore) |
| Chat | [03 §3.2](03-NOVACONTROL-MODULE-ARCHITECTURES.md#32-chat) |
| Jarvis | [03 §3.3](03-NOVACONTROL-MODULE-ARCHITECTURES.md#33-jarvis) |
| Memory/context | [03 §3.4](03-NOVACONTROL-MODULE-ARCHITECTURES.md#34-memory-and-context) |
| NLU/decision | [03 §3.5](03-NOVACONTROL-MODULE-ARCHITECTURES.md#35-nlu-and-decision-engine) |
| Planner | [03 §3.6](03-NOVACONTROL-MODULE-ARCHITECTURES.md#36-planner-and-task-decomposition) |
| Agents | [03 §3.7](03-NOVACONTROL-MODULE-ARCHITECTURES.md#37-agent-execution-and-orchestration) |
| Tools | [03 §3.8](03-NOVACONTROL-MODULE-ARCHITECTURES.md#38-tool-selection-and-execution) |
| Vision/perception | [03 §3.9](03-NOVACONTROL-MODULE-ARCHITECTURES.md#39-vision-and-perception) |
| Models/hardware | [03 §3.10](03-NOVACONTROL-MODULE-ARCHITECTURES.md#310-model-and-hardware-management) |
| Plugins | [03 §3.11](03-NOVACONTROL-MODULE-ARCHITECTURES.md#311-plugin-and-capability-management) |
| Reliability | [03 §3.12](03-NOVACONTROL-MODULE-ARCHITECTURES.md#312-reliability-recovery-and-lifecycle) |

### NovaMarket

| Domain | Section |
|---|---|
| Integration/lifecycle | [04 §1–2](04-NOVAMARKET-ARCHITECTURE.md#1-scope-and-integration-boundary) |
| Virtual market/accounting | [04 §4.1](04-NOVAMARKET-ARCHITECTURE.md#41-domain-a--virtual-market-and-financial-system) |
| Market data | [04 §4.2](04-NOVAMARKET-ARCHITECTURE.md#42-domain-b--market-data-intelligence) |
| Prediction | [04 §4.3](04-NOVAMARKET-ARCHITECTURE.md#43-domain-c--prediction-intelligence) |
| Research | [04 §4.4](04-NOVAMARKET-ARCHITECTURE.md#44-domain-d--financial-research-intelligence) |
| Autonomous trading | [04 §4.5](04-NOVAMARKET-ARCHITECTURE.md#45-domain-e--autonomous-trading-intelligence) |
| RL/adaptive | [04 §4.6](04-NOVAMARKET-ARCHITECTURE.md#46-domain-f--reinforcement-learning-and-adaptive-intelligence) |
| Multi-agent claim | [04 §4.7](04-NOVAMARKET-ARCHITECTURE.md#47-domain-g--multi-agent-financial-intelligence) |
| Portfolios | [04 §4.8](04-NOVAMARKET-ARCHITECTURE.md#48-domain-h--portfolio-management-and-optimization) |
| Risk | [04 §4.9](04-NOVAMARKET-ARCHITECTURE.md#49-domain-i--risk-intelligence) |
| Dashboard | [04 §4.10](04-NOVAMARKET-ARCHITECTURE.md#410-domain-j--dashboard-and-ui) |
| Financial authority | [04 §5](04-NOVAMARKET-ARCHITECTURE.md#5-financial-authority-architecture) |
| Order lifecycle | [04 §6](04-NOVAMARKET-ARCHITECTURE.md#6-paper-order-lifecycle) |

## 6. Status vocabulary

| Label | Meaning in these documents |
|---|---|
| IMPLEMENTED | Code exists, is wired, and a reachable path was found |
| PARTIALLY_IMPLEMENTED | Some real behavior exists, but the named module/function is incomplete or assembled from other modules |
| PLANNED | Roadmap only; no runtime implementation found |
| DEPRECATED | Present but intended to be replaced; not used as a primary path |
| EXPERIMENTAL | Present but not relied on as a production/default path |
| UNKNOWN | Not determinable from source inspection |
| NOT_IMPLEMENTED | Explicitly absent; documentation must not imply it |

## 7. Diagram conventions

- Implemented components are solid, blue-toned nodes.
- Partial/optional components are amber where styled.
- Planned components are grey and dashed.
- Financial authorities are red-bordered.
- Persistent stores are cylinders.
- External systems are violet.
- Solid arrows are confirmed paths; dashed arrows are optional, inferred, or planned.

Not every diagram uses all colors; when styling is omitted, node names and section text carry the status.

## 8. Evidence and freshness

- **Snapshot:** 2026-11-14.
- **Source of truth:** files on disk, not prior README claims.
- **Counts:** static measurement; see [09 §1](09-EVIDENCE-AND-COMPONENT-INVENTORY.md#1-repository-inventory).
- **Databases:** current local state, not acceptance totals.
- **No application code, schema, config, API, branch or dependency was modified** for this documentation set.
- If the code changes, the documented line references and counts must be refreshed. Treat any contradiction with older docs as a signal to inspect source again.
