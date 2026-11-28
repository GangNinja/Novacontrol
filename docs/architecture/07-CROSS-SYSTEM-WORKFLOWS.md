# 07 — Cross-System Workflows

These sequences show the actual current paths. Where the implementation differs from the originally proposed flow, the difference is called out.

## Flow A — Chat request

**Actual:** `/ask` always passes through global input intelligence and the decision engine before a chat answer. Research-looking chat questions can be redirected into Explore. The brain shapes a response only when a handler has no finished sentence.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Chat panel
    participant API as POST /ask
    participant APP as Application
    participant MEM as Memory and transcript
    participant GIL as NLU
    participant DEC as Decision
    participant B as NovaBrain
    participant LLM as Local/cloud model
    participant EX as Explore
    U->>UI: message
    UI->>API: request, optional image
    API->>APP: handle_request
    APP->>MEM: personal context + store request
    APP->>GIL: understand
    GIL-->>APP: intent, entities, confidence
    APP->>DEC: choose route/executor
    alt conversation
        APP->>B: chat
        B->>LLM: messages + history + knowledge
        LLM-->>B: answer
        B-->>APP: BrainResponse
    else research question
        APP->>EX: research
        EX-->>APP: sourced report
    else tool/action
        APP->>APP: dispatch tool/capability
    end
    APP->>MEM: store response, append transcript
    APP-->>UI: JSON response + SSE progress
    UI-->>U: answer
```

**Differences from the proposed flow:** model routing/tool selection is not a single LLM function-calling loop. Tool selection is deterministic and occurs before execution; Chat itself does not call tools.

## Flow B — Explore research

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Explore panel
    participant API as POST /explore
    participant S as ExploreService
    participant Q as Query resolver
    participant P as Research planner
    participant SR as Search providers
    participant PR as Page reader
    participant SY as Synthesizer
    participant M as Model
    U->>UI: research query
    UI->>API: topic, depth, sources, videos
    API->>S: research(ExploreRequest)
    S->>Q: resolve follow-up/reference
    alt cache hit
        Q-->>S: cached report
    else cache miss
        S->>P: plan queries (if allowed)
        P-->>S: query plan or deterministic fallback
        S->>SR: multi-provider search
        SR-->>S: sources + warnings
        par page reading
            S->>PR: fetch/extract text
            PR-->>S: passages
        and video search
            S->>S: YouTube lookup
        end
        S->>SY: evidence + sources + question
        SY->>M: synthesis when available
        M-->>SY: written answer
        SY-->>S: report with citations
    end
    S-->>API: ExploreReport
    API-->>UI: result
    UI-->>U: answer, sources, warnings
```

**Failure differences:** provider failures do not abort the whole search; a degraded report is returned with warnings. Model synthesis can be skipped because of slow local hardware; the evidence-ranked answer is used.

## Flow C — Jarvis command

**Actual:** “Jarvis” is a UI/device-control workflow. There is no Jarvis NLU or Jarvis agent; intent parsing and device dispatch are in the application’s command planner/executor.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Jarvis panel
    participant V as Browser speech
    participant API as /command/plan
    participant APP as Application
    participant G as Approval gate
    participant D as Desktop controller
    participant B as Browser controller
    participant P as Phone bridge
    U->>UI: speak or type command
    UI->>V: optional speech recognition
    V-->>UI: transcript in Chat input
    UI->>API: command
    API->>APP: plan command by device/intent
    APP-->>UI: steps, target, approval token, bridge status
    alt phone unavailable
        UI-->>U: pairing guidance, no run button
    else approval required and not approved
        U->>UI: Approve and Run
        UI->>API: /command/execute + token
        API->>G: verify token and permission
        G-->>API: approved
        API->>D: desktop actions
        API->>B: browser actions
        API->>P: phone actions
        D-->>API: verified results
        B-->>API: extracted/navigated results
        P-->>API: bridge result
        API-->>UI: execution results
        UI-->>U: status and evidence
    end
```

## Flow D — Complex task execution

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant APP as Application
    participant GIL as NLU
    participant DEC as Decision
    participant L as AgentLoop / orchestrator
    participant P as Planner
    participant E as WorkflowExecutor
    participant T as ToolExecutor
    participant V as Verifier
    participant R as RecoveryEngine
    U->>APP: multi-action goal
    APP->>GIL: understand clauses
    GIL-->>APP: multi-intent, requires_planning
    APP->>DEC: decide
    DEC-->>APP: planner/agent route
    APP->>L: run goal
    L->>P: plan with dependencies
    P-->>L: steps
    loop ready steps
        L->>E: execute
        E->>T: tool call with permissions
        T-->>E: result
        E->>V: verify
        alt failure
            V-->>E: failed
            E->>R: classify and recover/replan
            R-->>E: retry, alternative, or fail
        end
    end
    E-->>L: workflow result
    L-->>APP: verified result or failure
    APP-->>U: response and task record
```

**Difference:** planning is deterministic decomposition plus bounded agentic execution, not an LLM-authored arbitrary DAG. `AdaptivePlanner` can replan desktop/browser steps after observation.

## Flow E — Visual perception and action

```mermaid
sequenceDiagram
    autonumber
    participant UI as User/agent goal
    participant V as VisionController
    participant C as Desktop capture
    participant O as OCR
    participant M as Vision model
    participant A as Action engine
    participant S as After-capture
    UI->>V: describe or click target
    V->>C: approved screenshot
    C-->>V: image
    V->>O: OCR text + word geometry
    alt text or locate answer is sufficient
        O-->>V: answer/point
    else visual understanding required
        V->>M: image + constrained question
        M-->>V: summary/element/point
    end
    V->>A: click/focus target
    A-->>V: action result
    V->>S: after screenshot
    S-->>V: pixel diff and OCR evidence
    V-->>UI: verified result or bug record
```

## Flow F — NovaMarket prediction

```mermaid
sequenceDiagram
    autonumber
    participant R as Request/scheduler
    participant D as Dataset/features
    participant MD as Market data
    participant F as Forecast engine
    participant E as Ensemble
    participant L as Lifecycle
    participant V as Evaluation
    R->>D: instrument + horizon + as-of time
    D->>MD: point-in-time bars
    MD-->>D: observations with provenance
    D->>D: validate gaps, freshness, splits, features, regime
    D-->>F: eligible samples
    F->>F: fit/select statistical model
    F-->>E: model forecasts + uncertainty
    E->>E: reject ineligible, check disagreement
    alt ensemble abstains
        E-->>R: no forecast, explicit reasons
    else forecast produced
        E->>L: persist immutable T-20/T-5/T-1 stages
        L->>V: register outcome evaluation
        V->>MD: future bars
        V-->>R: measured error/calibration
    end
```

**Current scope:** 11 statistical models and forecast lifecycle. Deep/sequence models and full ensemble selection are planned; the flow above labels the existing contract/assembler as the ensemble stage.

## Flow G — NovaMarket autonomous paper trading

```mermaid
sequenceDiagram
    autonumber
    participant M as Monitor
    participant A as Autonomy runtime
    participant D as AT-03 decision
    participant S as Strategies
    participant R as NM-08 risk
    participant X as AT-04
    participant E as NM-10 execution
    participant W as Wallet/ledger
    participant P as Portfolio
    M->>A: cycle tick and session check
    A->>A: verify mode, lease, one-cycle gate
    A->>D: run cycle
    D->>S: evaluate active strategies
    S-->>D: signals
    D->>D: validate, create proposal
    D->>R: assess proposal
    R-->>D: persisted decision
    alt not approved
        D-->>A: skip, record reason
    else approved
        A->>X: select risk_approved proposals
        X->>X: re-verify decision, state, expiry, lease
        X->>E: submit proposal
        E->>R: re-assess actual notional
        E->>E: validate cash/position, reserve, fill
        E->>W: gross + fee transfers
        E->>P: position effect
        E-->>X: order/fills
        X->>X: reconcile
    end
    A-->>M: cycle result and events
```

## Flow H — Risk management

```mermaid
sequenceDiagram
    autonumber
    participant P as Portfolio state
    participant R as NM-08
    participant M as Market data
    participant F as Forecasts
    participant E as Events
    participant O as Operator
    participant A as Authorized workflow
    P->>R: positions, cash, exposure, valuation
    M->>R: prices and freshness
    F->>R: forecast age/uncertainty
    E->>R: event pressure
    R->>R: policy + metrics + correlations + stress
    alt sufficient data
        R-->>O: approved/restricted/rejected/review
    else insufficient
        R-->>O: insufficient_data, no approval
    end
    O->>A: act only through proposal/risk/execution
    A->>R: revalidate before execution
    R-->>A: enforceable decision
```

## Flow I — NovaControl and NovaMarket integration

```mermaid
sequenceDiagram
    autonumber
    participant NC as NovaControlApplication
    participant RT as EventDrivenRuntime
    participant NM as NovaMarketService
    participant ST as State store
    participant BUS as Event bus
    participant PERM as Permissions
    participant CAP as Capabilities
    participant API as Fork FastAPI
    NC->>NM: construct with shared store/bus/permissions/capabilities
    NC->>RT: register NovaMarketModule
    RT->>NM: start(event_bus)
    NM->>ST: load novamarket config namespace
    NM->>BUS: subscribe and publish novamarket.*
    NM->>PERM: declare lifecycle permissions
    NM->>CAP: register capabilities
    API->>NM: /market requests and /market/enable
    NM->>NM: domain engines, SQLite stores, monitor
    API->>NM: shutdown
    NM->>PERM: withdraw declarations
    NM->>CAP: withdraw capabilities
    NM->>BUS: detach
    RT->>NM: module stop (idempotent)
```

**Integration truth:** this sequence exists in `novamarket/src/novacontrol`, not in the root tree. NovaMarket uses the shared runtime, bus, state store, permissions, capabilities and audit; it does not create a second bus or second NovaControl runtime. Its financial stores are separate by design.

## Flow comparison summary

| Proposed flow | Actual implementation |
|---|---|
| Chat → model routing → tools | Deterministic NLU/decision chooses executor; Chat does not call tools |
| Explore → research acquisition → synthesis | Implemented, with provider/page/video concurrency and degraded answers |
| Jarvis voice → NLU → planner → execution | UI/voice path plus command planner; no Jarvis module or agent |
| Goal → planner → agents → tools → validation | Implemented with two planners and separate orchestration loops |
| Screen → OCR/VLM → decision → action | Implemented in VisionController/PerceptionEngine |
| Market data → features → models → ensemble → lifecycle | Statistical models and lifecycle implemented; deep models/full ensemble planned |
| Prediction + research → agents → portfolio → risk → execution | Implemented as deterministic pipeline, not agents |
| Portfolio → risk → stress → recommendation → NM-08 | Implemented; recommendations do not bypass proposal/decision gate |
| NovaControl hosts NovaMarket | Only in the nested fork, through runtime module and shared services |
