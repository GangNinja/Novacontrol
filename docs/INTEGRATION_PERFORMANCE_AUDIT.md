# NovaControl — Integration, Performance & Regression Audit (Phases 1–26)

**Audit date:** 2026-10-16 (Asia/Calcutta)
**Scope:** whether the capabilities of all 26 build phases are integrated with the running
application, whether they work through their intended execution paths, and the reported
Explore latency regression (≈5–6 s historically, ≈3–4 min later).
**Not in scope:** Phase 27, new product features, and any redesign unrelated to a confirmed defect.

Statuses used throughout, per requirement: **PASS**, **PARTIAL**, **FAIL**, **UNAVAILABLE**.
`UNAVAILABLE` is never a pass. A module existing, or a mocked test passing, is never counted as
application-level integration.

---

## 1. Audit scope, repository identity and environment

| Item | Value |
|---|---|
| Repository | `https://github.com/GangNinja/Novacontrol.git` |
| Branch | `main` |
| Commit audited | `ec03e877092e36a578cda128a37f8511f1a43621` — *feat: add phase 23-26 learning and control systems* (2026-10-16) |
| Tree state at start | Clean (`git status --porcelain` empty); `origin/main...HEAD` = `0  0` (in sync) |
| Host | Windows, Git Bash (MINGW64) |
| Python | CPython 3.13.0 (`MSC v.1941 64-bit`), editable install of `src/novacontrol` |
| Model/provider | No Ollama installed (its HTTP probe fails in 2.0 s), no cloud key configured — brain mode `scratch`/echo. Explore's LLM stages are therefore on the local-template path; that is a *test condition*, not a capability claim. |
| Suite size | 4,016 tests in 114 files (`pytest --collect-only`, 20.25 s) |
| CI definition | `.github/workflows/ci.yml` — test matrix (Python 3.12 and 3.13), API-reference sync, mypy in the linux and windows platform views |

`ROADMAP.md` and `AUDIT.md` are gitignored (present on disk, blocked from tool reads). The
canonical phase names used in §2 are taken from `docs/STATUS.md`, whose section headings are the
authoritative numbering for the staged-build phases 1–26.

**Repository safety.** No destructive Git command was run: no `reset`, no `checkout` of tracked
files, no stash, no cleanup. Historical comparison used `git archive <commit> | tar -x` into a
system temp directory, so the working tree and `.git` were never touched by it. No file was
deleted or overwritten to test an older version.

---

## 2. Per-phase integration matrix

Modules are the real packages; routes are the registered `ApiSurface` paths; tests are the files
that actually exist. "App integration" means the capability is reachable through the running
application (`/ask`, a registered route, or the chat router) — not merely importable.

| # | Phase (canonical name from `docs/STATUS.md`) | Modules / entry points | Route(s) | Tests | App integration | Status |
|---|---|---|---|---|---|---|
| 1 | Foundation (runtime contracts, event bus, config, security) | `core/`, `persistence/`, `core/events.py`, `core/config.py`, `core/security.py` | — | `test_core.py`, `test_persistence.py` | Used by every request; `NovaControlApplication` boots from it | PASS |
| 2 | Core engine (service container, journal, logging, retries) | `agentcore/`, `core/` | `/health`, `/status` | `test_agentic_architecture.py` | `/health` returned `{"status":"ok","ui_version":"9f5a7c8f7d1e"}` in 0.028 s live | PASS |
| 3 | Memory | `memory/` | `/chat/history`, `/agent/knowledge` | `test_memory.py` | Chat history endpoint live; wired through the brain | PASS |
| 4 | Tool system (registry, schemas, scopes, approval) | `tools/` | `/capabilities` | `test_tools.py` | `/capabilities` 200, 122,631 bytes of live registry | PASS |
| 5 | Agents (coordinator, contracts, bus, delegation) | `agents/`, `intelligence/engine.py` | `/agent/run`, `/agent/metrics` | `test_agents.py` | Coordinator dispatch present in `application.py` | PASS |
| 6 | Planning (decomposition, workflow graph, recovery, clarification) | `planning/` (early layer) | `/plan`, `/plan/run` | `test_planner.py` | Planning requests flow through `handle_request` | PASS |
| 7 | Desktop automation | `desktop/` | `/desktop/plan`, `/desktop/execute` | `test_desktop.py`, `test_guided_click_e2e.py` | Device-pinned via the JARVIS panel; desktop execute is token-gated | PASS (approval path); UNAVAILABLE for real window control off-Windows/CI |
| 8 | Browser automation | `browser/` | `/browser/plan`, `/browser/execute` | `test_browser.py` | Live; `NoopBrowserRunner` 12 skips are the Playwright-dependent checks | PARTIAL — Playwright is not installed (12 skipped tests) |
| 9 | Vision | `vision/` | `/vision/status`, `/vision/describe`, `/vision/click` | `test_vision.py` | `/vision/status` live, reports no model connected | PARTIAL — no VLM installed; screen/OCR paths only |
| 10 | Explore (online research) | `explore/` | `/explore`, `/explore/trending` | `test_explore.py`, `test_page_reading.py`, `test_evidence_ranking.py`, `test_explore_question_first.py` | Live end-to-end: 200 in **5.59 s**, 3 sources, 5 videos, real answer text | PASS (after this audit's fixes) |
| 11 | Voice | `voice/` | — (browser Web Speech API) | `test_voice.py` | Front-end only; server-side surface is a provider-probe | PARTIAL — provider-dependent |
| 12 | GUI / web platform | `web/`, `gui/`, `api/` | `/`, `/static/*`, all routes | `test_web_api.py`, `test_web_platform.py` | Served and cache-busting verified by the suite | PASS |
| 13 | API (REST, WebSockets, auth, plugin API) | `api/` | 150+ registered routes | `test_web_api.py`, `test_api_reference_sync.py` | `docs/API.md` in sync (exit 0); `auth_enabled: false` on this host | PASS |
| 14 | Plugin marketplace | `plugins/` | `/plugins` | `test_plugins.py`, `test_plugin_sdk_verification.py` | Live | PASS |
| 15 | Performance optimization | `performance/` | `/benchmark` | `test_performance.py` (TtlCache) | TtlCache backs the Explore report cache; cached run measured **0.005 s** | PASS |
| 16 | Release / app bootstrap | `release/`, `cli/` | `/system/package`, `/system/harden` | `test_release.py` | Boot verified live | PASS |
| 17 | Model & hardware manager | `models/` | `/system/telemetry`, `/brain/local/model`, `/brain/ollama/models` | `test_models.py`, `test_telemetry.py` | `/system/telemetry` live and cached | PASS (probe figures honest: `None`/unknown where unreadable) |
| 18 | Reliability, recovery and safety | `reliability/` | (via planner) | `test_reliability.py` | VerificationEngine/RecoveryEngine compose the planner | PASS |
| 19 | Internal event bus and capability registry | `core/events.py`, `intelligence/capabilities.py` | `/capabilities`, `/events/stream` | `test_events.py`, `test_capabilities.py` | SSE channel live; `/capabilities` live | PASS |
| 20 | Plugin SDK | `plugins/` | `/plugins` | `test_plugin_sdk_verification.py` | Live | PASS |
| 21 | Local knowledge & project awareness | `knowledge/`, `projects/` | `/knowledge`, `/knowledge/recall`, `/knowledge/teach` | `test_knowledge_rag.py`, `test_knowledge_rag_verification.py` | `/knowledge` live (200) | PASS |
| 22 | Specialized agents | `agents/` specialist hooks | `/agent/run` | `test_specialist_agents.py` | Registered through the same `AgentRegistry` the chat path dispatches | PASS |
| 23 | Scheduler, automation and audit trail | `automation/`, `audit/`, `scheduler/` | `/automation*`, `/audit*` | `test_automation.py`, `test_audit.py` | `/automation` and `/audit` live (200) | PASS |
| 24 | Optimization, resource governance, self-diagnostics | `optimization/`, `diagnostics/`, `telemetry/` | `/diagnostics`, `/cost/estimate`, `/privacy` | `test_optimization.py` | `/diagnostics` live, 2.03 s (roster runs in worker threads) | PASS |
| 25 | RL foundation: data collection, evaluation, reward | `evaluation/` | `/evaluation/*` | `test_evaluation.py` | `/evaluation/summary` live, 0.056 s | PASS |
| 26 | Learning stack: SFT → preference → RLHF/RLAIF → RLVR → agentic RL | `training/`, `preference/`, `rlhf/`, `rlvr/`, `agentic/` | `/training/*`, `/preference/*`, `/rlhf/*`, `/rlvr/*` | `test_training.py`, `test_preference.py`, `test_rlhf.py`, `test_rlvr.py`, `test_agentic_rl.py` | `/rlvr/status` and `/training/status` live (200) | PASS (all training is dry-run-gated; no run was started) |
| 27 | Real-time perception & abstraction (Phase 21) | `perception/` | `/perception/status`, `/perception/capabilities` | `test_perception.py` | Both live: 200, 3,360 B / 1,343 B | PASS for image/screen/stream; UNAVAILABLE for camera (no backend) and VLM |
| 28 | World model, memory & state reasoning (Phase 22) | `world/` | `/world/status`, `/world/observe`, `/world/query` | `test_world_model.py` | Status live (3,060 B) | PASS; model-backed prediction UNAVAILABLE (no provider) |
| 29 | Interactive learning & exploration (Phase 23) | `interactive/` | `/learning/status`, `/learning/objectives`, `/learning/environments` | `test_interactive_learning_workflows.py` | All live (200) | PASS; no dedicated UI (API-only by design) |
| 30 | Planning, reasoning & action policy (Phase 24) | `planning/` (goal manager, candidates, policy) | `/planning/status`, `/planning/goals*` | `test_goal_planning.py`, `test_interactive_learning_workflows.py` | Status live (3,700 B) | PASS; A*/MCTS not shipped (UNAVAILABLE, declared) |
| 31 | Embodied agents & controlled environments (Phase 25) | `embodied/` | `/embodied/status`, `/embodied/sessions*` | `test_embodied.py` | Status + environments live (10,301 B / 1,527 B) | PASS for the owned gridworld; real screen/game adapter UNAVAILABLE |
| 32 | Generalization, ARC-style reasoning (Phase 26) | `generalization/` | `/generalization/status`, `/generalization/overview`, `/generalization/evaluate` | `test_generalization.py` | Both live (14,812 B / 7,268 B) | PARTIAL — bounded 22-operator library over a hand-written corpus; no aggregate score, no gameplay claim |
| 33 | Global intelligence layer (cross-cutting) | `intelligence/` | `/intelligence`, `/brain/decide` | `test_intelligence*.py`, `test_model_probe_dedup.py` | Live and fast — **22.1 s measured, then fixed to 4.03 s** (§7, D4) on one request-scoped probe budget | PASS |

> Rows 27–33 are the phase numbers 21–26 of the milestone under audit; rows 1–26 are the
> staged-build phases 1–20 plus their cross-cutting layers. The numbering above is continuous
> (1–33) only so every required capability has a row; the phase *names* are canonical.

### Requirements explicitly NOT claimed

* Camera capture, semantic object detection, semantic segmentation, VLM description — no backend
  or model is installed. **UNAVAILABLE.**
* Model-backed world prediction and model-backed planning estimates — no provider configured.
  **UNAVAILABLE.**
* Post-Phase-26 real gameplay evaluation — deferred by the project. **UNAVAILABLE.**
* Playwright-driven browser execution — Playwright is not installed (12 tests skip).
  **UNAVAILABLE** in this environment.
* Linux CI — this is a Windows/CPython 3.13 run. **UNAVAILABLE** locally.

---

## 3. Cross-phase dependency matrix

Each path was traced in the source and, where it has a live surface, exercised against the running
application (§11). "Producer → consumer" names the real objects.

| Integration path | Producer → consumer | Contract | Evidence | Status |
|---|---|---|---|---|
| Perception → World model | `PerceptionResult` → `world/ingest.py` | normalizes a perception result into an observation | `test_world_model.py`; prior milestone live `/world/observe` | PASS |
| World state → Interactive learning | world snapshot + state store → `interactive/manager.py` | shared persistence contract | `test_interactive_learning_workflows.py`; `/learning/status` live | PASS |
| Learned rules → Planning | `interactive` knowledge scope → `planning/grounding.py` | scoped, evidence-carrying rules only | `test_goal_planning.py` scope/retired-rule cases | PASS |
| Planner → Executor → Verifier → Embodied loop | `planning/goal_manager.py` → `embodied/agent.py` | `DeterministicVerifier` + the goal manager are the shipping path | `test_embodied.py` + `scripts/benchmark_embodied.py` (9/9) | PASS |
| Embodied sessions → Generalization evaluation | session contract → `generalization/evaluation.py` | `gameplay_claimed=False` until a real player is evaluated | `test_generalization.py`; `/generalization/game-eval/contracts` live | PARTIAL (contract exists; the real-game milestone is deferred) |
| Exploration → Evaluation recorder | `EventBus` lifecycle events → `evaluation/recorder.py` | recorder observes the *same* bus, grouped by correlation id | `test_evaluation.py` (including the "recording can never fail the work" pin) | PASS |
| Evaluation → RLVR/RLHF datasets | trajectories + verifiers → `rlvr/datasets.py` | dataset versioning with held-out validation | `test_rlvr.py`; `/rlvr/status` live | PASS |
| Automation → the same request pipeline | `AutomationEngine` → `NovaControlApplication.handle_request` | **executes nothing itself**; reads `response.payload` | `test_automation.py` | PASS |
| Capability registry → Decision engine | `intelligence/capabilities.py` → `decision/engine.py` | availability reported, never re-routing | `/capabilities` live; `test_capabilities.py` | PASS |
| Knowledge index → Brain | `knowledge/manager.py` → `brain/brain.py` `_build_chat_messages` | retrieved block becomes its own labelled system message | `test_knowledge_rag.py` | PASS |
| Progress events → UI | `explore.progress` / `command.progress` → `/events/stream` → `render-utils.js` | one app-wide bus, one EventSource | `test_event_correlation.py`; live SSE endpoint registered | PASS |
| Plugins → Tools/Capabilities | `PluginManager` → `ToolRegistry`, `CapabilityRegistry`, `PermissionManager` | register at enable, withdraw at disable | `test_plugin_sdk_verification.py` | PASS |

---

## 4. Feature inventory and workflows tested

Inventory method: enumerating the registered `ApiSurface` routes
(`src/novacontrol/api/route_consumers.py`, 150+ paths) and grouping them by the package that
owns each consumer, then keeping the user-facing entry points. The 50 packages under
`src/novacontrol/` total roughly 170k lines; the largest are `rlvr` (10,425), `rlhf` (10,381),
`planning` (10,050), `agentic` (9,988), `interactive` (9,414), `world` (9,196), `intelligence`
(9,217).

Workflows exercised against the live application (§11): app boot and `/health`; Chat via `/ask`;
Explore research via `/explore`; the capability/tool surface; diagnostics; the audit trail; the
evaluation summary; and a read of every Phase 21–26 status surface. Multi-step device-control
workflows (desktop/browser/phone) were *not* driven end-to-end here — they need a desktop session,
Playwright, or a paired phone, none of which this environment has. They are covered by their
suites and the token-gate API tests, and are reported as PARTIAL rather than PASS.

---

## 5. Historical baseline evidence

**Git history examined** (`git log --oneline`, 20 commits on `main`):

```
ec03e87 feat: add phase 23-26 learning and control systems
04503a1 Record what CI caught, and correct the claims that said it could not
0089e89 Guard the state store's constructor, so an unusable root cannot fail a boot
19d18ae Deliver Phases 21-22, and repair the nine defects their audits surfaced
...
7862de7 Initial commit: NovaControl baseline — current full working tree
```

`git log -S "ResilientSearchProvider" -- src/novacontrol/explore/` and
`git log -S "timeout=10" -- src/novacontrol/explore/providers.py` both return **only**
`7862de7`. The three-provider sequential search stack with its 10 s socket timeouts is therefore
present in the repository's *first* commit — it is not something a later commit introduced. The
repository's local history is a squashed baseline plus 19 commits, so `git blame` cannot date the
search stack; that is stated plainly rather than guessed.

**Method used to obtain a fast version.** `git archive 7862de7 | tar -x -C <tmp>` produced a
read-only export of the baseline tree, which was then measured with the *same* probe script and
the *same* machine and network as the current tree. Nothing in the working tree was reverted.

**What DID change between that baseline and HEAD**, from
`git diff 7862de7 HEAD -- src/novacontrol/explore/service.py`: three stages were added on top of
the search the baseline already had —

1. `plan_research(...)` — an optional planning model call before searching;
2. `_search_with_retry(...)` — the empty-result retry, which re-runs the **whole** provider ×
   query cascade a second time;
3. `_read_sources(...)` — `PageReader` fetching and extracting each source page.

Only (2) multiplies the failure path; (1) is inert with no model connected; (3) is bounded by
`PageReader`'s own concurrency and timeouts.

---

## 6. Before-versus-after code comparison

| Area | Baseline `7862de7` | HEAD before this audit | Change made by this audit |
|---|---|---|---|
| Query execution | one query at a time, `await` in a `for` loop | unchanged (sequential) | **concurrent wave** under one deadline |
| Search retry | none | whole cascade retried when everything came back empty (22 calls total) | retry kept, but every attempt now runs inside the bounded wave |
| Page reading | none | `asyncio.to_thread` + semaphore, `gather` | unchanged behaviour; the *documented* batch deadline now actually exists |
| Provider transport | `urlopen(...)` inside `async def` (blocking) | unchanged (blocking) | `await asyncio.to_thread(...)`, mirroring `PageReader` |
| Wikipedia transport | `urlopen(timeout=5)` inside `async def` | unchanged (blocking) | same thread offload |

The `_multi_platform_search` selection logic (candidate pooling, `is_relevant` gate,
`relevance_score` ranking, the ≥½-of-best cutoff, `max_sources` trimming) was **not** changed.
Results are folded in query order, so the candidate pool is the same one the sequential code
built; only the waiting is different.

---

## 7. Confirmed root causes, contributing factors, ruled-out hypotheses

### D1 — Explore's search stage was an unbounded sequential cascade (CONFIRMED, FIXED)

**Cause.** `ExploreService._multi_platform_search` awaited one query at a time, and each query
walks `ResilientSearchProvider`'s three providers in sequence, each with a 10 s socket timeout
(`_SEARCH_TIMEOUT_SECONDS`). `_search_with_retry` then re-runs the entire cascade whenever the
attempt produced nothing. `PageReader._DEFAULT_BATCH_DEADLINE_SECONDS` did not exist, though the
docstring claimed a deadline did.

**Measured call count** (shipped control flow; providers stubbed to count calls and return
nothing): **22 network calls** — 6 per web provider (3 queries × 2 attempts) plus 4 Wikipedia
calls. At the shipped 10 s timeout that is **220 s of search**, plus the 10 s video fetch — the
reported 3–4 minutes, arithmetically reproduced.

**Measured wall clock, same simulation** (every provider hanging for its shipped socket timeout
and returning nothing): **210.2 s (3.50 min) before → 70.1 s (1.17 min) after**. That is the
headline fix: a 3.0× reduction in the degraded case, and the reported 3–4 min figure is
reproduced and explained.

### D2 — The Explore network stack blocked the asyncio event loop (CONFIRMED, FIXED)

**Cause.** `providers.py` (`DuckDuckGoLite/Bing/Google/YouTube`) and
`web_search.py` (`WikipediaSearchProvider`) call blocking `urlopen` directly inside `async def`.
Threading was applied to `PageReader` only.

**Measured.** A 10 ms heartbeat task run alongside one full Explore research recorded a **maximum
event-loop stall of 3.654 s** (a single uninterrupted block, against 125 ticks). Enumerating the
YouTube search page was the largest single block. During that window no other request, chat
answer or SSE event can be serviced.

### D3 — PageReader's documented batch deadline did not exist (CONFIRMED, FIXED)

`read_many`'s docstring said "the whole batch is capped by one deadline — a single slow host must
not hold the research run open", but the code used a bare `asyncio.gather`. The stage was bounded
only by `ceil(max_pages / concurrency)` socket timeouts. **Measured before:** 6 URLs at
concurrency 2 with 0.5 s reads = 1.56 s (unbounded); **after:** the batch stops at its deadline.

### D4 — `GET /intelligence` took 22.1 s (CONFIRMED, **FIXED**)

**Cause (measured).** The handler calls `nova._model_routing_report()`, which runs
`model_manager.select_for_request(...)` **five times** (reasoning, coding, vision, vision+tools,
strongest). Each selection reaches `OllamaBackend.list_models()` → `ollama_models()` (2.0 s
timeout), and each selection ALSO calls `runtime_status()` → `resident_models()` →
`ollama_resident_models()` (2.0 s). That is **ten** bounded probes for one report, plus an
**eleventh** for the route's own runtime block. With nothing answering on `127.0.0.1:11434`, each
probe waits out its full timeout (~2.01 s), so one status read cost ≈22 s.

Directly measured in isolation: `ollama_models()` **2.027 / 2.002 / 2.010 s**,
`ollama_resident_models()` **2.010 / 2.002 / 2.012 s**; `/intelligence` **22.119 / 22.108 /
22.089 s** on three consecutive requests (so it is not a cold-start cost), reproduced on a
pre-fix tree at `HEAD`. The raw hardware probe is *not* the cause: `HardwareTelemetry().snapshot()`
measured **0.07 s** and is cached, and `models_status()`, `headroom_report()` and
`provider.available_memory_bytes()` each measured **0.000 s** — the endpoint's entire cost was the
probe count.

**Fix.** One request-scoped probe memo — `ModelProbeSnapshot` (`models/manager.py`) — created and
dropped inside the read it serves. The five selections, the runtime block beside them and the
report's own residency lookup all share it, so `GET /intelligence` asks the runtime **once per
fact**. It is deliberately **not** a cache: no TTL, no invalidation, no global, no lock. The
invariant this protects is unchanged — `ollama_model_capabilities` documents that "a miss is never
cached, so pulling the model later is picked up without a restart", and that stays exactly true
because the memo lives for one call and the next call probes again. The other candidate fix, a
short negative TTL, was rejected for that reason: it would make the miss *persist* for the TTL and
break the property the comment exists to protect.

### 7.1 D4 fixed — before/after, probe counts, tests, and the freshness proof

**Change (four files).** `ModelProbeSnapshot` (`src/novacontrol/models/manager.py`, exported from
`models/__init__.py`) memoises two facts — the provider's model list and its resident list — one
method each, distinguishing "not asked yet" from a real answer and from "could not ask"
(`None`). `ModelManager.discover`, `_resident`, `runtime_status`, `select`, `select_for_request`
and `health` take an optional `snapshot=`; **without one they behave exactly as before**, which is
why no existing caller changed either its cost or its freshness. `_model_routing_report` accepts a
caller's snapshot (creating its own when none is given). `NovaControlApplication.intelligence_model_report()`
(new) builds the whole model half of `GET /intelligence` on ONE snapshot, and `model_status()` —
which reads the same two blocks for `/status` — shares one too, because that is the same defect.
The `/intelligence` route now calls that method instead of assembling the block inline, so the
probe budget belongs to the read rather than to the HTTP layer. **The response shape is
unchanged**: the same 7 top-level keys, the same 9 `models` keys, the same 5 selection labels.

**Live measurement.** Real uvicorn servers, pre-fix tree on port 8003 and post-fix on 8004,
nothing listening on `127.0.0.1:11434`, three consecutive requests each, same host, same
conditions:

| `GET /intelligence` | request 1 | request 2 | request 3 | mean |
|---|---:|---:|---:|---:|
| BEFORE (pre-fix tree at `HEAD`) | 22.119 s | 22.108 s | 22.089 s | **22.105 s** |
| AFTER (this change) | 4.042 s | 4.021 s | 4.016 s | **4.026 s** |

**18.08 s saved per request (81.8 %), a 5.5× speed-up**, and the sub-5 s target is met on this
host. HTTP 200 in every case.

**Re-verified on the final tree** (after the tests and this document were written, same conditions
— nothing on `127.0.0.1:11434`, a fresh isolated data directory, port 8001): **4.083 / 4.046 /
4.044 s**, three consecutive requests, HTTP 200 each, 49,371–49,373 B, the same 7 top-level keys,
the same 9 `models` keys and the same 5 selection labels, `runtime.reachable: false` reported
honestly, and the selections unchanged (`reasoning`/`coding`/`strongest` → `qwen3:8b`, `vision` →
`moondream`, `vision_tools` → `qwen2.5vl`). The freshness proof was repeated on that same
un-restarted server afterwards: no runtime (`reachable false`, `registry.known` 6) → a fake runtime
on 11434 (`reachable **true**`, `known` **7**, `active_model: fresh-fake:7b`, `considered:
[fresh-fake:7b]`, 0.025 s) → runtime killed again (`reachable false`, selections back to the
baseline, 4.043 s).

**Probe counts per request**, from a counting fake provider driven through the real report and the
real route:

| Read | model-list probes | residency probes | total |
|---|---:|---:|---:|
| `_model_routing_report()` BEFORE (five questions, each probing) | 5 | 5 | **10** |
| `_model_routing_report()` AFTER (five questions, one memo) | 1 | 1 | **2** |
| `GET /intelligence` AFTER (report + runtime block, one memo) | 1 | 1 | **2** |
| one selection outside any report (unchanged path) | 1 | 1 | 2 |

**The selection results are identical, not merely similar.** Against the same absent runtime the
pre-fix and post-fix trees both answered `reasoning` / `coding` / `strongest` →
`local/qwen3:8b`, `vision` → `local/moondream`, `vision_tools` → `local/qwen2.5vl`. The fix
changed the cost, not the answers.

**Freshness, verified live on the running post-fix server with no restart at any point:**

| Step | `runtime.reachable` | `registry.known` | selection |
|---|---|---|---|
| 1. no runtime | `False` | 6 | `vision=moondream`, `reasoning=qwen3:8b`, request 4.030 s |
| 2. a fake runtime appears on 11434 | — | — | — |
| 3. the next request, same server | **`True`** | **7** (`fresh-fake:7b` discovered) | read from the runtime's own list |
| 4. the runtime is gone again | **`False`** | 7 | `vision=moondream`, `reasoning=qwen3:8b`, request 4.029 s |

A runtime that becomes available after boot **is** discovered without a restart, and one that
disappears is not served from a stale answer either — in both directions the next request is
fresh. (The registry keeps the model NAME it learned, which is pre-existing `ModelRegistry`
behaviour, untouched here.)

**Tests** — `tests/test_model_probe_dedup.py`, 18 tests, fakes only, no runtime required: the
probe count per report and per request; the five scenarios still distinct; an opt-in snapshot
leaving every existing caller's cost alone; two reports excluding each other (object identity held
by the test, so a freed id cannot be reused); two reports running concurrently in real worker
threads, paying two probes each and sharing nothing; an unreachable runtime answering honestly;
a probe that times out being answered once per fact rather than once per question; a residency
probe failure handled like an unanswerable one; a model pulled later being found; a model that
disappears not being served from an earlier report; and the endpoint itself at a 2-probe budget.

**Mutation-verified.** Reverting the fix in `_model_routing_report` (no shared snapshot):
`test_a_report_probes_the_runtime_once_for_each_fact` → `AssertionError: 5 != 1`;
`test_the_probe_count_does_not_grow_with_the_question_count` → `AssertionError: 20 != 12`;
`test_the_endpoint_answers_all_five_scenarios_within_a_bounded_probe_budget` →
`AssertionError: 5 != 1`. **3 failed → restored → 18 passed.**

**Test-count cross-check:** `pytest --collect-only` reports **4,034** collected tests; the audit
recorded 4,016 and this change adds exactly 18. No existing test was modified, skipped or weakened.

### Contributing factors

* The YouTube video stage is a second variable cost: measured **4.48 s–18.22 s** across runs for
  the same query. The socket timeout bounds each *read*, not the whole response body, so a large
  page can exceed it. This is the single largest remaining Explore stage and is **not** fixed.
* Page reading costs a consistent 1.8–3.9 s and is correctly bounded.
* Synthesis is ~0.2 s with no model connected.

### Ruled out

* **A regression introduced by a later commit in this repository.** The search stack and its 10 s
  timeouts are in the first commit; `_search_with_retry` was added later and doubled the cost but
  did not create it. The behaviour is environment-triggered (a provider that hangs or returns
  nothing) rather than commit-triggered.
* **The report cache.** It works: a repeated query returned in **0.005 s**.
* **Page reading as the 3–4 minute cause.** Bounded to ~12 s worst case by construction.
* **`plan_research` as the cause.** `usable_provider()` rejects the echo/no-model provider, so the
  planning call does not happen at all on this host.
* **Duplicated or recursive orchestration.** `_emit` publishes one progress event per stage;
  the trace shows a single event per stage per run.
* **The hardware probe behind `/intelligence`.** Measured at 0.07 s (cached).

---

## 8. Baseline and post-fix latency measurements

All figures are `time.perf_counter()` wall clock on this host, Ollama disabled, no cloud key.

### Healthy network (the normal case)

| Scenario | Baseline `7862de7` | HEAD before fixes | HEAD after fixes | Live HTTP (`POST /explore`, after fixes) |
|---|---|---|---|---|
| Cold run, "why do cats purr" | 10.69 s | 12.39 s | 9.89 s | **5.59 s** |
| Cold run, distinct topic | 9.18 s | 19.86 s | 27.06 s* | — |
| Warm/identical repeat | — | 0.005 s | 0.005 s | — |
| Search stage | 4.69 s | 5.59–8.03 s | **2.53–5.93 s** | — |
| Page-read stage | n/a (did not exist) | 2.15–3.71 s | 1.80–3.94 s | — |
| Video stage | 5.99 s | 4.48–7.89 s | 4.48–18.22 s* | — |
| Synthesis stage | ~0.01 s | ~0.17–0.24 s | ~0.13–0.23 s | — |

\* Network variance dominates: the video stage ranged 4.5–18.2 s across identical requests, so
single end-to-end numbers move by more than the search fix's whole benefit. **No latency promise
is made from these samples.** The search stage — the stage the defect is in — is the only row
that moved consistently (concurrent queries instead of sequential ones).

Individual provider probes (one query, live network): DuckDuckGo Lite **1.554 s**/5 results,
Bing **0.976 s**/5 results, Google **1.373 s**/0 results, `ResilientSearchProvider` **1.137 s**
(returns on the first usable provider). `build_search_queries` produces **2–3 queries** per
research; with a planning model it would be up to 4.

### Degraded network (the reported 3–4 minute case)

| Measurement | Before | After | Method |
|---|---|---|---|
| Wall clock, every provider hanging for its shipped timeout | **210.18 s (3.50 min)** | **70.07 s (1.17 min)** | Real `ExploreService.research()`; provider sleeps stand in for the hanging host (simulation, labelled) |
| Provider calls per run | 22 | 16 observed (wave cut) | Same probe |
| Event-loop stall during one run | **3.654 s** | **0.270 s** | 10 ms heartbeat alongside the real research |
| Worst-case search stage (call count × shipped timeout) | 220 s | ≤ 2 × 20 s budget | Arithmetic from the shipped constants |

The remaining 70 s is: two bounded search waves (2 × 20 s budget) + the Wikipedia fallback +
the video fetch. **The historical 5–6 s baseline is not reproducible** on this host under either
condition — the baseline commit itself measured 9.2–10.7 s cold. The honest statement is: the
user's remembered 5–6 s is close to a *single* cheap stage (search alone was 4.7 s at the
baseline), the healthy path today is ~5.6 s over HTTP, and the 3–4 minute path was real and is
now ~1.2 min.

### Resource observations

The suite and the app ran concurrently during the live-workflow batch. Respective RSS was not
instrumented, so no memory figure is claimed. CPU: the audit ran single-process pytest and one
Uvicorn worker; no parallel suites were launched. Figures that could not be measured are `None`
here rather than a fabricated zero, matching the project's own Phase-14 rule.

---

## 9. Defects found, fixes implemented, regression tests added

| ID | Severity | Phases | Defect | Fix | Regression test |
|---|---|---|---|---|---|
| D1 | HIGH | 10 (Explore) | Sequential provider×query cascade, doubled by the empty-result retry; 22 calls × 10 s = 4 min; batch deadline documented but absent | Independent queries run as one concurrent wave (`asyncio.create_task` + `asyncio.wait`) under `search_budget_seconds` (default 20 s, constructor-injectable); stragglers cancelled; results folded in query order; page-read batch deadline implemented | `ExploreSearchWaveTests::test_the_queries_of_one_run_are_searched_concurrently`, `::test_a_slow_provider_cannot_stretch_a_run_past_its_budget` (test_explore.py); `ReadManyDeadlineTests::test_a_slow_batch_stops_at_its_deadline` (test_page_reading.py) |
| D2 | HIGH | 10, 12 | Blocking `urlopen` inside `async def` in `providers.py` and `web_search.py` froze the event loop for up to 3.65 s per Explore run | Fetch moved to a worker thread via `asyncio.to_thread` in both modules | `ExploreProviderEventLoopTests::test_a_slow_provider_fetch_does_not_freeze_the_event_loop` |
| D3 | MEDIUM | 10 | `PageReader.read_many` had no batch deadline despite documenting one | `asyncio.wait(..., timeout=self.batch_deadline_seconds)`, unfinished reads cancelled; the batch deadline is now a named constant and a constructor argument | Covered by `ReadManyDeadlineTests` |
| D4 | MEDIUM | 17, 33 (models / intelligence) | `GET /intelligence` took 22.1 s: 5 model-selection dry-runs, each re-probing an unreachable local runtime at 2 s, and each probing it twice | **Fixed** — one request-scoped probe memo (`ModelProbeSnapshot`) shared by the report, the runtime block and `/status`, so the read asks the runtime once per fact; 22.105 s → **4.026 s** live, 11 probes → 2, no negative cache (§7.1) | `test_model_probe_dedup.py` (18 tests; 3 fail under mutation) |

**Mutation-verified.** All four new tests were run against code with each fix reverted in turn:
`test_the_queries_of_one_run_are_searched_concurrently` → `AssertionError: 1 not greater than or
equal to 2`; `test_a_slow_provider_cannot_stretch_a_run_past_its_budget` → fail;
`test_a_slow_provider_fetch_does_not_freeze_the_event_loop` → `AssertionError: 0.4055... not less
than 0.2` (with an asyncio warning that the blocking call "took 0.404 seconds");
`test_a_slow_batch_stops_at_its_deadline` → `AssertionError: 1.5559... not less than 1.2`. All
four pass with the fixes in place. **4 failed → restored → 4 passed.**

D4's pin was mutation-tested the same way, in the follow-up change that fixed it: with the shared snapshot removed from `_model_routing_report`, five questions probe the runtime ten times again and **3 tests fail** (`5 != 1`, `20 != 12`, `5 != 1`); restored, all 18 pass. **3 failed → restored → 18 passed.**

**Test count cross-check:** `pytest --collect-only` reported **4,016** collected tests at the end of
this audit (the prior milestone report records 4,012, and this audit added exactly 4). The D4
follow-up adds its own **18**, so the tree now collects **4,034** — exactly the 18 new tests, with
no existing test modified, skipped, weakened or deleted.

---

## 10. Exact commands, results, durations and limitations

| # | Command | Result | Duration |
|---|---|---|---|
| 1 | `python -m mypy src --platform linux` | **PASS** — `Success: no issues found in 412 source files`, exit 0 | 100 s (after fix) |
| 2 | `python -m mypy src --platform win32` | **PASS** — `Success: no issues found in 412 source files`, exit 0 | 121 s (after fix) |
| 3 | `python scripts/generate_api_reference.py --check` | **PASS** — `docs/API.md: in sync`, exit 0 | 3 s |
| 4 | `python -m pytest tests/test_explore.py tests/test_explore_question_first.py tests/test_page_reading.py tests/test_evidence_ranking.py tests/test_browser_search_extraction.py -q` | **PASS** — 121 passed, 2 skipped, 47 subtests passed, exit 0 | 74.6 s |
| 5 | `python -m pytest tests/test_explore.py tests/test_page_reading.py -q -k "SearchWave or ProviderEventLoop or ReadManyDeadline"` | **PASS** — 4 passed (with fixes); **4 failed** under the reverted-code mutation | 3.0 s / 33.8 s |
| 6 | `python -m ruff check <6 changed files>` (diagnostics mapped onto added lines) | 0 findings on added lines (3 were introduced and fixed); 59 pre-existing findings on unchanged lines | ~30 s |
| 7 | `python -m pytest tests/ -q` (full suite) | see below | see below |
| 8 | Live app `POST /ask` / `POST /explore` / 21 status routes | see §11 | — |
| 9 | Explore probes: `probe_providers`, `probe_stages2`, `probe_calls2`, `probe_degraded_wall`, `probe_loop_stall` | see §8 | 3–211 s per probe |

**CI check 1 (test matrix), exact state.** The workflow runs `python -m pytest tests/ -q` on
Python 3.12 and 3.13.

* **Python 3.13 leg:** run against this tree, split into three disjoint sequential groups so a
  timeout could not discard completed work. All 114 files and all 4,016 collected tests are
  covered — nothing was skipped or deselected to make the run pass.

  | Group | Files | Result | Duration |
  |---|---|---|---:|
  | 1 | 38 | `1077 passed, 3 skipped, 820 subtests passed` — **exit 0** | 2038.6 s (33:58) |
  | 2 | 38 | `1150 passed, 2 skipped, 1608 subtests passed` — **exit 0** | 2771.0 s (46:11) |
  | 3 | 38 | `1772 passed, 5 failed, 7 skipped, 740 subtests passed` — **exit 1** | 3858.7 s (64:18) |
  | **Total** | **114** | **3999 passed, 5 failed, 12 skipped** — the three group figures sum exactly to the 4,016 tests collected | **8668 s (2:24:28)** |

  The 12 skips are the documented Playwright-dependent checks. **The full suite is therefore NOT
  green on this host.** The five failures are analysed next, and that analysis is precisely why this
  report does not claim a pass for the test-matrix check.

#### The five group-3 failures: host memory, not a regression

All five are learning-stack API tests, and all five failed for one reason: the Phase-14 resource
governor **refused** a training/evaluation run because this machine was out of free memory at that
moment. The governor's own reason string, quoted from the failure output:

```
'the resource estimate is unsafe; pass override=True to proceed',
'free memory: 337252352 bytes (the resource governor's last reading); headroom 536870912 bytes'
'the estimate needs 135818342 bytes but only 0 bytes are usable: starting this configuration could
 exhaust the machine'
```

| Failing test | Assertion | What actually happened |
|---|---|---|
| `test_rlhf.py::ApplicationRLHFTests::test_a_dry_run_trains_through_the_application` | `started["ok"] is True` | `ok: False, refused: True` — *the estimate is unsafe* (337 MB free against 512 MB headroom) |
| `test_rlhf.py::ApplicationRLHFTests::test_a_dry_run_preview_starts_nothing_and_validates_everything` | `preview["ok"] is True` | `False`, with an empty `errors` list — the same refusal |
| `test_rlhf.py::ApplicationRLHFTests::test_the_four_model_comparison_reports_a_measured_verdict` | `compared["ok"] is True` | `run '…' is created, so it was not evaluated` |
| `test_preference.py::PreferenceApiTests::test_an_evaluation_without_predictors_is_refused_not_approved` | 409 + *"base predictor"* | `"run '…' is created: only a completed run is evaluated"` |
| `test_preference.py::PreferenceApiTests::test_evaluations_and_models_are_served` | a served run/model list | `IndexError: list index out of range` — the list was empty |

**Reproduced as passing on the same tree once memory was free.** Re-run in isolation after the
grouped run had exited — with **6.84 GB** free instead of 337 MB — the same files are green:

```
python -m pytest tests/test_preference.py tests/test_rlhf.py::ApplicationRLHFTests -q
  → 179 passed, 8 subtests passed, exit 0   (508 s)
python -m pytest tests/test_rlhf.py -q
  → 171 passed, 3 subtests passed, exit 0   (313 s)
```

**Not attributable to this audit's change.** The change touches only
`src/novacontrol/explore/{service,providers,web_search,page_reader}.py` plus four added Explore
tests, and those tests run in group 1 — which passed. The failing tests exercise the preference/RLHF
training API, which neither imports nor calls Explore, and both files pass against the modified tree.
The trigger is this host's free memory during a 64-minute grouped run, and those tests do not pin the
hardware monitor — the same class of fragility that commit `828be25` ("Pin the machine in the
resource-verdict tests") already repaired once elsewhere.

**Verdict for this check: FAIL as executed on this host**, with all five failures explained as
environmental and none attributable to the audited change. On an idle CI runner these are expected
to pass, but an expectation is not evidence, so it is not reported as one. This is the one required
CI check that could not be shown green here.

Run separately and to completion:

```
python -m pytest tests/test_routing_explorer.py tests/test_route_shape_contract.py \
       tests/test_explore_trending.py tests/test_browser_search_extraction.py -q
  → 52 passed, 2 skipped, 134 subtests passed, exit 0   (464.0 s)
python -m pytest tests/test_web_api.py -q
  → 65 passed, 34 subtests passed, exit 0   (844.1 s)
```

Those five files carry this change's regression surface: the Explore suite, the routing explorer, the
route-shape contract, the trending endpoint, the search-extraction parsers, and the whole web API
(chat, Explore, command planning and the approval-token gates).

* **Python 3.12 leg: UNAVAILABLE.** Only CPython 3.13.0 is installed on this host (`python -V`),
  and the parallel WSL attempt used by the previous milestone report returned
  `Wsl/E_ACCESSDENIED`. A green 3.13 run is **not** evidence of a green 3.12 run.

**Not run, and not claimed:** Linux CI, Playwright execution (12 skipped), Ruff repository-wide
(59 pre-existing findings across the six touched files alone; a clean repository-wide breadth is
not claimed), Docker, and any live paid-provider request (no key configured).

---

## 11. Real application workflow evidence

The real FastAPI application was started from the audited tree (`uvicorn
novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8002`) with an isolated working
directory so its `data/` tree could not touch the user's. Port 8000 was not used.

| Workflow | Request | Expected | Actual | Time |
|---|---|---|---|---|
| Boot & liveness | `GET /health` | ok | `{"status":"ok","ui_version":"9f5a7c8f7d1e"}` | 0.028 s |
| System status | `GET /status` | ok, auth state | `name=NovaControl`, `status=ok`, `auth_enabled=false` | — |
| **Chat** | `POST /ask {"request":"What is 9 times 9?"}` | route `chat`, answer 81 | HTTP 200, `route:"chat"`, `intent:"math"`, summary *"The result of `9 times 9` is **81**."*, `brain_mode:"scratch"`, NLU latency 2.467 ms | **0.235 s** |
| **Explore** | `POST /explore {"topic":"why do cats purr"}` | a real report | HTTP 200, 3 sources, 5 videos, `provider_status:"online"`, real answer prose, 17 report keys | **5.59 s** |
| Capabilities | `GET /capabilities` | registry | 200, 122,631 B | 0.015 s |
| Diagnostics | `GET /diagnostics` | roster | 200, 8,106 B | 2.03 s |
| Vision | `GET /vision/status`, `GET /bugs` | honest status | 200, 100 B / 51 B | 0.008 s |
| Perception (21) | `GET /perception/status`, `/perception/capabilities` | status | 200, 3,360 B / 1,343 B | 0.005 s |
| World (22) | `GET /world/status` | status | 200, 3,060 B | 0.005 s |
| Interactive learning (23) | `GET /learning/status`, `/learning/environments` | status | 200, 7,918 B / 2,866 B | 0.028 s |
| Planning (24) | `GET /planning/status` | status | 200, 3,700 B | 0.004 s |
| Embodied (25) | `GET /embodied/status`, `/embodied/environments` | status | 200, 10,301 B / 1,527 B | 0.005 s |
| Generalization (26) | `GET /generalization/status`, `/generalization/overview` | status | 200, 14,812 B / 7,268 B | 0.006 s |
| Knowledge (21) | `GET /knowledge` | status | 200 | 0.007 s |
| Automation / audit | `GET /automation`, `GET /audit` | status | 200, 213 B / 319 B | 0.005 s |
| Evaluation | `GET /evaluation/summary` | summary | 200, 1,591 B | 0.056 s |
| RL | `GET /rlvr/status`, `GET /training/status` | status | 200, 519 B / 997 B | 0.006 s |
| Intelligence | `GET /intelligence` | status | 200, 50,416 B — **22.1 s at audit time, 4.03 s after the D4 fix** (§7.1) | 22.13 s → 4.03 s |
| **Multi-step browser plan** | `POST /command/plan {"command":"navigate to example.com and search the web for cats"}` | a plan awaiting approval | HTTP 200, `status:"waiting_for_approval"`, one step — *"Navigate to https://example.com"* — with `approval.token` minted | — |
| **Approval gate (refusal)** | `POST /command/execute` with a forged token | refusal | **HTTP 403** — *"Approval token is unknown. Plan the command again and approve the freshly previewed action."* | — |
| **Phase 24 goal planning (positive path)** | `POST /planning/goals` with a measurable condition | candidates compared and one selected | HTTP 200, `planned:true`, **2 candidates** (`dependency_order`, `cost_aware_beam`), a selected candidate, 10 grounding facts, per-step policy verdicts, estimates with basis, and limitations naming what it is not ("not A*") | 0.040 s |
| **Phase 24 refusal (unmeasurable goal)** | same route with a free-text condition | an honest refusal, never a plan that claims to verify | HTTP 200, `planned:false`, `candidates:0`, both planners carrying *"the goal states no measurable success condition, so completion could never be verified; name what 'done' looks like"*, `truncated:false` | 0.029 s |

**Not exercised (honest gaps).** The browser was not used to submit the Chat or Explore workflow, so
"the UI was tested" is **not** claimed: the workflows above are the real HTTP surface the UI itself
calls, with the UI's request shapes. The device-control chain was driven as far as this host
allows — a real browser plan and a real 403 token refusal, above — but the approved *execution*
needs a browser (Playwright is absent), a paired phone, or a desktop session, so no browser action
was actually run. The embodied-session and generalization-evaluation endpoints were read, not run.
Cancellation and provider-failure behaviour was exercised at unit level for Explore (the
degraded-path simulation), not through the UI's cancel control.

---

## 12. Remaining limitations, risks and unverified requirements

1. **D4 fixed, with one honest floor.** `GET /intelligence` was 22.1 s and is now **4.03 s**
   on the same host and conditions, because the five selections and the runtime block share one
   request-scoped probe budget (§7.1). What remains is the floor: two bounded probes against a
   runtime that is not there. Removing those means caching the miss, which the design
   deliberately does not do — a model pulled later must work without a restart.
2. **Explore is still network-bound.** After the fixes the video stage alone measured 4.5–18.2 s
   and search 2.5–5.9 s on a healthy network. No part of the pipeline has an end-to-end request
   deadline, and `/explore` has no server-side timeout, so a pathologically slow (but not hanging)
   host can still make a single run slow. The budget bounds the *search wave*, not the request.
3. **The empty-result retry still doubles the bounded cost** (two waves instead of one). It is now
   bounded, but it fires precisely when a second full sweep is least likely to help.
4. **No Linux CI, no Python 3.12 run here.** Local green is not a CI claim.
5. **Playwright absent** (12 skipped). Browser execution and any real web-app workflow are
   unverified end to end.
6. **Provider-dependent capabilities remain UNAVAILABLE**: no vision model, no camera backend, no
   model-backed prediction, no cloud key, no LLM synthesis in Explore (answers are assembled from
   source sentences and the report says so).
7. **Phase 26 remains bounded** — a 22-operator library over a hand-written corpus, no aggregate
   score, no gameplay evaluation.
8. **Device-control execution is PARTIAL**: a real plan and a real 403 token refusal were exercised
   on the live API, but no browser/desktop/phone action was actually executed here (no Playwright,
   no paired phone, no desktop session). Those paths are evidenced by their suites and the token
   tests, not end to end.
9. **Aggregate search-stage statistics are not reported.** With 2–3 cold samples per condition,
   median/p95 would be noise; only per-stage ranges are given.
10. **The prior milestone report's own gaps are unchanged** — SSE graceful-shutdown behaviour under
    a persistent stream still was not confirmed.
11. **The full suite was not green on this host.** 3,999 passed, **5 failed**, 12 skipped. All five
    failures are the Phase-14 resource governor refusing a training run on a machine that had
    337 MB free; both affected files pass 179/179 in isolation on the same tree with memory free
    (§10). They are environmental and not attributable to this change, but they were failures, so
    the test-matrix check is reported below as **FAIL as executed here**, never as a pass.
12. **Those five tests are host-memory-sensitive by construction.** They assert on the resource
    governor's verdict without pinning the hardware monitor, so any machine under memory pressure
    can fail them. Pinning the monitor in those tests — as commit `828be25` did for others — would
    remove the fragility; it was not done here because it is unrelated to this audit's change.

---

## 13. Overall verdict

**PASS WITH LIMITATIONS.**

* The reported Explore regression is **investigated, reproduced and explained**: the same
  simulation measures 3.50 min before and 1.17 min after; the shipped flow makes 22 network calls
  at a 10 s socket timeout when the search surface degrades.
* Three confirmed defects were fixed with mutation-verified regression tests, and the fixes hold
  at the level the user experiences them (`POST /explore` = 5.59 s and correct over the real API;
  event-loop stall 3.654 s → 0.270 s).
* The fourth confirmed defect (D4) was **fixed in a follow-up change on the same tree**: `GET /intelligence` measured **22.105 s → 4.026 s** live (81.8 % faster, 5.5×), because one request-scoped probe memo replaced eleven bounded runtime probes with two — with the selection results byte-identical and the no-persistent-cache invariant verified live in both directions (§7.1). Its 18 tests are mutation-verified.
* Phase 1–26 surfaces are all reachable and live on the running application, and the
  cross-phase connections that matter were traced in code; but several capabilities are
  environment-limited (PARTIAL/UNAVAILABLE) and are not counted as passes.
* Of the four required CI checks, **two are green and one is not**: both mypy views pass
  (`Success: no issues found in 412 source files`, linux and win32) and the API-reference sync
  reports `docs/API.md: in sync` — but the test matrix is **FAIL as executed on this host**
  (3,999 passed, 5 failed, 12 skipped, exit 1), with all five failures root-caused to the resource
  governor refusing a training run at 337 MB free and both affected files passing 179/179 in
  isolation on the same tree (§10). The Python 3.12 leg and Linux CI are **UNAVAILABLE** here.
* **Nothing was committed or pushed.** The audit's own rule is that the changes are published only
  when all four checks are green, and the test matrix is not; the four Explore fixes and their
  tests are left uncommitted in the working tree with this exact reason recorded, rather than
  pushed on an expectation that an idle runner would behave differently.

No Phase 27 work was started. No feature unrelated to a confirmed defect was added.
