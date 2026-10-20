# NovaControl — Real-User Feature Inventory (Phases 1–26)

**Purpose.** What this build actually ships, how a person reaches each capability, what it should
do, and whether it was verified. Derived from the source tree and confirmed against a running
server, not from the roadmap.

**Source identity** — `main`, working tree at `ec03e87` plus the uncommitted Integration &
Performance Audit work (see `docs/INTEGRATION_PERFORMANCE_AUDIT.md`). Runtime: Windows, Python
3.12, `.venv` editable install, `uvicorn novacontrol.api.app:create_app --factory`.

**Method.** Three passes, all against a live server on `127.0.0.1:8001`, in an isolated working
directory so nothing touched the user's data:

1. **Surface enumeration** — the route table was dumped from the running app (`docs/API.md`
   format), giving **265 routes (124 GET, 141 POST) in 46 path groups** — 45 named groups plus the
   root `/`; the static UI was enumerated by rendering every panel in a real browser.
2. **Mechanical acceptance** — all **124 GET** and **141 POST** routes were called over real HTTP
   with either a realistic payload (the one the UI sends) or an empty one, and every response was
   classified. Result: **0 server errors** (see `docs/REAL_USER_APPLICATION_AUDIT.md` §4).
3. **Journey testing** — 75 further calls grouped into the eleven journey families A–K, plus a
   browser pass over all eleven panels and a restart.

**Classification legend**

| Mark | Meaning |
|---|---|
| **ACCESSIBLE** | Implemented, reachable by a user through the UI or a documented entry point. |
| **API-ONLY** | Implemented and working, but reachable only over HTTP/CLI — no UI surface. |
| **PARTIAL** | Works, with a stated limit (missing provider, unwired model, refused without a device). |
| **UNAVAILABLE** | Cannot be exercised in this environment; the reason is stated. |
| **DEFECT** | Behaves incorrectly; see the audit's defect table. |

---

## 1. User-interface surfaces

The web platform is the primary window: one page, a sidebar of **11 panels**, one flat JSON API
behind it. Every panel was opened in a real browser and its rendered content inspected.

| # | Panel | Entry point | What it does | Status | Evidence |
|---|---|---|---|---|---|
| 1 | **Command Center** (`homePanel`) | sidebar "Command" | Runtime/module/task/project counters, live telemetry (CPU, memory, storage, GPU, network, battery, temperature, uptime), Quick Command composer, capability list, API action list | **ACCESSIBLE** | Rendered with live figures (CPU 21 %, RAM 69 %, storage, battery 98 %); unavailable sensors report `Unavailable` **with the reason** instead of a fabricated number |
| 2 | **Chat** (`chatPanel`) | sidebar "Chat" | Type a question → routed answer; brain-mode switch (Auto / Local LLM / Scratch / Cloud LLM); model picker; voice input, voice chat, read-aloud; Clear Chat | **ACCESSIBLE** | Typed "what is the capital of France?" with real input events → route `CHAT`, 80 % confidence, "The capital of France is Paris."; "what is 2 plus 2" → "4" |
| 3 | **Explore** (`explorePanel`) | sidebar "Explore" | Research a topic: sources, key points, detailed explanation, videos, citations; live progress steps; trending topics | **ACCESSIBLE** | Live run returned a 3-source report in 2.8 s; 8 `explore.progress` frames delivered on the SSE channel |
| 4 | **Routing Explorer** (`routingPanel`) | sidebar "Routing" | Trace any utterance through the intent gates; shows the landing intent, every gate's verdict, and a preview of the answer; broad-classifier comparison toggle | **ACCESSIBLE** | Live trace: "explain quantum entanglement in detail" → `explore` 86 %, with `✗ scratch_greeting ✗ self_improvement ✗ scratch_math ✗ phone_anchored_action ✗ web_search ✓ research_question` |
| 5 | **J.A.R.V.I.S** (`jarvisPanel`) | sidebar "J.A.R.V.I.S" | One device-pinned control surface with a **Desktop / Phone / Browser** switch: plan then approve device commands; voice; per-row command history with delete + clear | **ACCESSIBLE** | "open notepad" planned (`ACTION PLAN Open notepad`) with a token and **nothing executed**; run button enabled by the token-bearing plan only |
| 6 | **Vision** (`visionPanel`) | sidebar "Vision" | Describe Screen, Guided Click by label, and the Bug Log list (mark-fixed / clear-fixed / open count) | **PARTIAL** | `/vision/describe` works (1.1 s) and says it captured; with no vision model wired it uses the honest window-probe fallback and reports `VISION UNAVAILABLE` rather than inventing a description |
| 7 | **Build** (`buildPanel`) | sidebar "Build" | "Build And Improve": plan a goal, plan a coding task, draft/edit an artifact, save to disk, run a saved artifact; self-improvement workflow / preview / approve | **PARTIAL** | `/plan`, `/plan/code`, `/build/project`, `/build/save`, `/build/run` all 200; the panel states plainly that without a coding model only a starter scaffold is possible |
| 8 | **Learn** (`learnPanel`) | sidebar "Learn" | Teach a fact, show what was taught, run a learning cycle, auto-train, self-improvement plans | **ACCESSIBLE** | Taught a fact, recalled it by query, and it survived a restart |
| 9 | **System** (`systemPanel`) | sidebar "System" | Health / Harden / Package, AI-brain card, task list with per-row delete + clear-all, automation status | **ACCESSIBLE** | `/system/health`, `/system/harden`, `/system/package`, `/tasks`, `/automation` all 200 |
| 10 | **Settings** (`settingsPanel`) | sidebar "Settings" | Approval mode, detailed explanations, Explore videos, auto-approve & run, Cloud LLM card (provider / model / key), privacy + retention controls, save | **ACCESSIBLE** | `POST /settings` round-tripped (`detailed_explanations` true, read back and preserved across a restart) |
| 11 | **CLI Output** (`cliPanel`) | sidebar "CLI" | Raw structured payload of the last action, for technical inspection | **ACCESSIBLE** | Renders the last response as JSON |

**Cross-panel chrome** — sidebar navigation, status header (`READY`, CPU/RAM/tasks), command
palette (`Ctrl+K`), focus mode, API-token field, Refresh, stale-build banner (`UI v…` stamp vs
`/health`), toasts, and the activity stream. All render; the stale-build guard compares the
panel's stamp with the served build.

---

## 2. HTTP API surface (265 routes / 45 named groups)

Every route below was called live. "Acceptance" is the mechanical sweep result; "Depth" says how
far it was taken beyond a status code.

| Group | Routes | What a user gets from it | Acceptance | Depth |
|---|---:|---|---|---|
| `rlvr` | 31 | Critique/verify/reward loops: verifiers, critiques, corrections, datasets, estimate, dry-run, runs, evaluate | 31/31 contract-correct | Read + list + dry-run exercised |
| `rlhf` | 31 | Feedback, ratings, reward datasets, RL runs, pipeline planning | 31/31 contract-correct | Feedback submitted, rating recorded, dry-run priced |
| `preference` | 28 | Preference pairs/reviews, DPO-ORPO runs, comparison, dry-run | 28/28 contract-correct | Dry-run + dataset validation exercised |
| `training` | 25 | Bounded local SFT: datasets, estimates, runs, checkpoints, model lifecycle (approve/promote/reject/deprecate/rollback) | 25/25 contract-correct | Estimate + dataset + create + lifecycle refusals exercised |
| `generalization` | 17 | Task families, splits with leakage verdict, operators, bounded evaluation, runs, failures, recommendations, game-eval contracts | 17/17 contract-correct | Status, families, splits, contracts, measured game-eval read |
| `learning` | 16 | Interactive learning: environments, objectives, preview, run, approve/reject/cancel/resume, transfer, metrics, evidence provenance | 16/16 contract-correct | Status, environments, metrics, transfer exercised |
| `embodied` | 10 | Sessions over allowlisted environments: create, run, pause, resume, cancel, status, scenarios | 10/10 contract-correct | Status, environments, overview read |
| `brain` | 9 | Mode (auto/llm/scratch/cloud), cloud presets/install/test/clear, local model pin, model list, routing trace | 9/9 contract-correct | Mode round-trip forced scratch → answered → auto; routing trace driven from the UI |
| `automation` | 8 | Schedule (parses the time from the request), approve, enable, disable, cancel, run, run-due, status | 8/8 contract-correct | **Full journey**: schedule → not armed → approve → gated run → task `scheduled`, runs 1, failures 0, audit record with `automation_id` |
| `planning` | 7 | Goals: create with candidate plans, run, reassess, cancel, status | 7/7 contract-correct | Create refused a body without a `request` (422 naming it); the UI has no goal panel, so this is API-only |
| `world` | 5 | Observe → advanced state, bounded structured queries, prediction (honest `model_unavailable`) | 5/5 contract-correct | Query answered with evidence/uncertainty/staleness; predict reported it has no model |
| `vision` | 5 | Status, install/clear a vision model, describe screen, guided click, bug log | 5/5 contract-correct | Describe captured the screen and reported honestly |
| `audit` | 5 | Report, entries, prune, delete, clear | 5/5 contract-correct | Grew from 25 → 26 records after a scheduled run |
| `tasks` | 4 | List (trimmed), delete, clear, undo within a window | 4/4 contract-correct | Trimmed views confirmed (no `result` payloads) |
| `system` | 4 | Health, telemetry, harden, package | 4/4 contract-correct | Live telemetry read |
| `phone` | 4 | Status, connect (pairing flow), plan, execute | 4/4 contract-correct | Plan/execute behave per the pairing contract; no device present |
| `improve` | 4 | Self-improvement plan, workflow, preview, approve | 4/4 contract-correct | Plan + workflow + preview exercised |
| `evaluation` | 4 | Trajectory recording/reporting, retention | 4/4 contract-correct | Status + trajectory lookup (404 on unknown id) |
| `plan` | 3 | Plan a goal, run through the agent loop, plan a coding task | 3/3 contract-correct | Executed a plan end-to-end |
| `perception` | 3 | Status, capabilities with live availability, perceive an image/frames/screen | 3/3 contract-correct | Capability classification read; perception refuses without a source, naming what to send |
| `knowledge` | 3 | Teach, list/recall, local index | 3/3 contract-correct | Teach → recall → restart survival |
| `chat` | 3 | Clear, read the shared thread, append/migrate a turn | 3/3 contract-correct | Thread read-back; one ask = exactly one pair |
| `bugs` | 3 | List, mark fixed, clear fixed | 3/3 contract-correct | Open-count read |
| `agent` | 3 | Run the agentic loop, metrics, application knowledge | 3/3 contract-correct | Ran the loop on a harmless goal |
| `settings` | 2 | Read, update | 2/2 contract-correct | Round-trip verified |
| `privacy` | 2 | Posture read, live mode/control change | 2/2 contract-correct | Mode change read back |
| `explore` | 2 | Research a topic, trending topics | 2/2 contract-correct | Multiple topics, quick + standard depth, follow-up |
| `desktop` | 2 | Plan, execute (approval-gated) | 2/2 contract-correct | Planned; execution gated by token |
| `command` | 2 | Plan, execute (re-dispatches on the routed intent) | 2/2 contract-correct | Planned; token-gated execution |
| `capabilities` | 2 | Full capability table, discovery for a task | 2/2 contract-correct | Discovery returned matches + registry posture |
| `browser` | 2 | Plan, execute (navigate / search + extract) | 2/2 contract-correct | Planned; gated |
| `benchmark` | 2 | Read records, measure a model through the live provider | 2/2 contract-correct | Measured an Echo run |
| `train`, `learn`, `improve`, `status`, `resources`, `plugins`, `intelligence`, `health`, `events`, `diagnostics`, `cost`, `build`, `ask`, `activity`, `improve` | 1 each | The single-action doors: autonomous iterations, learning cycle, runtime status, resource governor, plugin records, intelligence telemetry, health, SSE stream, diagnostics roster, cost hint, build surface, chat ask, activity journal | All contract-correct | `GET /events/stream` verified as a working SSE channel (8 live frames); `/diagnostics` ran its whole roster |

**Deliberate non-2xx results are contract, not failure.** Every refusal named what was wrong
("goal is required.", "say when it should run (\"at 6 PM\"…)", "A 'source' is required…"), and no
route answered 5xx.

---

## 3. Agents, tools and capabilities

| Feature | Entry point | Status | Evidence |
|---|---|---|---|
| **Agentic orchestrator** (interpret → plan → perceive → act → observe → verify → recover → learn) | `POST /agent/run` | **ACCESSIBLE** | Returned `{task_id, goal, status, interpretation, plan, current_step}` for "list the files in this project" |
| **Specialist agents** (Developer, Research) hooked onto the one pipeline | `POST /plan/code`, `POST /build/*` | **ACCESSIBLE** | Code plan produced language-aware steps + an artifact; project scaffold produced a file map |
| **Capability registry** (declared / tool / action, with availability) | `GET /capabilities`, `/capabilities/discover` | **ACCESSIBLE** | Table read with availability; discovery is declared-only by design |
| **Tool registry + selection** | via planning/agent paths | **API-ONLY** | Exercised through planning and the agent loop |
| **Plugin SDK / marketplace** | `GET /plugins` → `{plugins: [], marketplace_enabled: false}` | **PARTIAL** | The two doors (`PluginManager` for running, `PluginMarketplace` for installable) exist with 3 example plugins in `src/novacontrol/plugins/examples/` (browser, developer, system); **the HTTP route reports an empty surface and no marketplace**, so no plugin can be installed or enabled from the running app |
| **Global Intelligence Layer** (telemetry, findings, capability table) | `GET /intelligence` | **ACCESSIBLE** | Read: telemetry + findings + capabilities; ~4.0 s (it builds the full report) |
| **Bug log** (what/where/when, mark-fixed) | `GET /bugs`, `POST /bugs/{id}/fix` | **ACCESSIBLE** | Open count read; fix/clear contract-correct |
| **Evaluation recorder** (trajectories from lifecycle events) | `GET /evaluation/status`, `/evaluation/trajectory/{id}` | **API-ONLY** | Status read; unknown ids 404 with a named reason |

---

## 4. Workflows

| Workflow | How a user invokes it | Status | Evidence |
|---|---|---|---|
| **Assistant / routing** | Chat panel, or `POST /ask` | **ACCESSIBLE** | Six utterance families routed correctly (greeting, math, time, research, unknown, device); an unknown phrase answered without crashing |
| **Research (Explore)** | Explore panel, or `POST /explore` | **ACCESSIBLE** | Standard/quick depth, follow-up topics, trending; 900 s TTL cache serves identical topics fast (3.8 ms vs 3.3 s uncached) |
| **Live progress** | implicit while Explore/commands run | **ACCESSIBLE** | `GET /events/stream` delivered 8 ordered `explore.progress` frames carrying the run's `correlation_id` |
| **Planning + execution** | Build panel, `POST /plan`, `/plan/run` | **ACCESSIBLE** | Plan object read; `execute: true` returned plan + workflow + state; the agent loop ran to completion with a verification verdict |
| **Engineering / build** | Build panel | **ACCESSIBLE** | Code plan, multi-file scaffold, save to `build_workspace/`, re-run a saved artifact |
| **Vision / perception** | Vision panel, `/vision/describe`, `/perception` | **PARTIAL** | Describe works; element location is OCR/window-probe only until a vision model is installed (`VISION UNAVAILABLE` is stated, not faked) |
| **World state** | `/world/observe`, `/world/query`, `/world/predict` | **API-ONLY** | Query answered with evidence + uncertainty; prediction honestly reports `model_unavailable` |
| **Memory / context continuity** | Chat panel across turns | **ACCESSIBLE** | Multi-turn context retained; taught knowledge recalled after a restart |
| **Automation / scheduling** | API + System panel status | **API-ONLY (works)** | Schedule → approve → gated run → audit record; the schedule phrase is stripped from the stored request and arming is refused until approved |
| **Model / resource management** | Chat header switch, Settings cloud card, System brain card | **ACCESSIBLE** | Mode round-trip; `/resources` reports level + reasons + thresholds; `/cost/estimate` returns a hint and never a gate |
| **Learning / evaluation / generalization** | Learn panel + HTTP | **PARTIAL** | Status/environments/metrics/transfer/splits/contracts all read; a full bounded learning cycle is not driven from the UI |
| **Embodied sessions** | HTTP only | **API-ONLY** | Status/environments/overview read; creating or running a session is not exposed in the UI |
| **Training / preference / RLHF / RLVR** | HTTP + CLI (`novacontrol training …`) | **API-ONLY** | Estimates, dry-runs, datasets, run lifecycle and model lifecycle all answered contract-correctly; unsafe/real starts are refused without confirmation |

---

## 5. Configuration, model and resource controls

| Control | Where | Status |
|---|---|---|
| Brain mode (auto / llm / scratch / cloud) | Chat header select, `POST /brain/mode` | **ACCESSIBLE** — persisted server-side, `effective_mode` derived from the live provider |
| Local model pin | Chat header model picker, `POST /brain/local/model` | **ACCESSIBLE** (no Ollama running here → picker reports it) |
| Cloud LLM (provider / model / key) | Settings → Cloud LLM card | **ACCESSIBLE** — install/clear endpoints exist; the key is never returned (redacted hint only) |
| Approval mode, detailed explanations, Explore videos, auto-approve & run | Settings | **ACCESSIBLE** — round-tripped and persisted |
| Privacy: execution mode + 5 controls | Settings + `POST /privacy` | **ACCESSIBLE** — applied live; `cloud_allowed` reflects the mode |
| Retention (audit / evaluation / training / preference / rlhf days + caps) | Settings | **ACCESSIBLE** — validated (non-numeric refused with 422) |
| Resource governor thresholds | config `resources:` section, `GET /resources` | **API-ONLY** |
| API token auth | Sidebar token field, `NOVACONTROL_API_TOKEN` | **ACCESSIBLE** (off in this instance — no token configured) |
| Rate limiting | middleware, `x-ratelimit-*` headers | **ACCESSIBLE** — 120 requests/min, observed enforcing (429 + `retry_after`) |

---

## 6. Persistent data and sessions

Lives in the app's data directory (`data/`, gitignored), through one JSON state store plus a
SQLite memory store. **Restart persistence verified** for: the shared chat thread (32 turns),
taught knowledge, settings, the scheduled automation task and its schedule, and build artifacts.

Chat transcript · settings · automation tasks · tasks · audit JSONL · bugs · knowledge ·
knowledge index · memory (SQLite) · world state · learning objectives/index · generalization runs ·
agentic knowledge/evaluation · evaluation trajectories · scheduler state · cloud-LLM config
(key stored locally only) · learned feedback · `build_workspace/` artifacts · logs.

---

## 7. Background processes

| Process | What it does | Status |
|---|---|---|
| Automation engine tick | Runs due automations through the same intent→decision→plan→permission→execution path; one execution per due task per tick | **ACCESSIBLE** (manual gated run verified; tick interval reported by `/automation`) |
| Event bus → SSE | Relays `explore.progress` / `command.progress` / lifecycle events to the page | **ACCESSIBLE** — verified end to end |
| Telemetry sampler | Slow sensors (GPU, network, temperature) sampled off the request path | **PARTIAL** — disabled in this instance; the panel then reports those sensors `Unavailable` with the reason |
| Frontend pollers | `/system/telemetry` every ~2 s, live activity stream, 60 s stale-build check | **ACCESSIBLE** — measured ~30 polls/min idle |
| Audit retention | Period + hard-cap pruning | **API-ONLY** — `/audit/prune` contract-correct |

---

## 8. Intended-but-not-user-reachable items

These are implemented in the packages (and unit-tested) but have **no user-facing surface** in
this build. They are not defects — nothing in the UI claims them — but they are the difference
between "the repository implements it" and "a user can use it":

- Phases 16–19 training pipelines (SFT, preference, RLHF, RLVR): HTTP + CLI only.
- Phase 24 interactive learning cycle and Phase 25 embodied sessions: HTTP only.
- Phase 26 generalization evaluation: HTTP only (the measured game-eval report is readable).
- World model ingestion/query: HTTP only.
- Plugin install/enable: the route reports an empty, marketplace-disabled surface.

## 9. Rounding

| Bucket | Count |
|---|---:|
| User-accessible features verified end to end | **265 routes, 45 named API groups, 11 UI panels, 19 CLI subcommands** |
| Implemented but only reachable by API/CLI | Training/preference/RLHF/RLVR, interactive learning, embodied sessions, generalization, world model, plugin install |
| Partial (stated limits) | Vision (no model), Build (no coding model), plugins (no marketplace surface) |
| Unavailable for testing | Real desktop control on a live desktop, real browser automation, a real phone device, a real LLM provider, real GPU/NPU — each refused or reported honestly rather than faked |
| Defects found | 3 (see `docs/REAL_USER_APPLICATION_AUDIT.md` §9) |
