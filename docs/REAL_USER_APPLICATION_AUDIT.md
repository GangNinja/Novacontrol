# NovaControl — Full Application Real-User Acceptance Audit (Phases 1–26)

**Verdict: PASS WITH LIMITATIONS.** Every discovered user-facing feature was exercised through
its real entry point. The HTTP surface (265 routes) answered with **zero server errors**; all
eleven UI panels render and were driven in a real browser; the core journeys — research, routing,
chat, planning, build, device commands, automation, knowledge, persistence — work end to end.
Three defects (D5–D7, continuing the Integration & Performance Audit's numbering) were found,
fixed and pinned by regression tests. The limitations are capability
limits of *this machine* (no LLM, no vision model, no phone, no GPU/NPU), each of which the build
reports honestly instead of faking, plus the fact that training / embodied / interactive-learning
layers are reachable by HTTP and CLI but have no UI surface.

---

## 1. Source-tree identity and environment

| Item | Value |
|---|---|
| Branch | `main` |
| HEAD | `ec03e87 feat: add phase 23-26 learning and control systems` |
| Working tree | **dirty** — the uncommitted Integration & Performance Audit work (13 modified files, 2 new: `docs/INTEGRATION_PERFORMANCE_AUDIT.md`, `tests/test_model_probe_dedup.py`) was preserved untouched |
| Runtime | Windows (Git Bash), Python 3.12, `.venv` editable install |
| Server | `python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001` |
| Server env | `NOVACONTROL_DISABLE_OLLAMA=1`, `NOVACONTROL_DISABLE_TELEMETRY_SAMPLER=1` |
| Server working dir | an isolated temp directory, so `data/` was a throwaway store and the user's own `data/` was not used for verification |
| App state | auth **off** (no `NOVACONTROL_API_TOKEN` configured); rate limit 120 req/min; brain mode `scratch` (no LLM reachable) |

Everything below was measured on that instance. Nothing was reset, reverted, stashed or committed.

## 2. Testing method, and whether UI automation was available

**Yes — real browser automation was used.** A Chromium tab was driven against the running server:
navigation was exercised panel by panel, and the Chat journey used genuine typed input and a
genuine button click (not a scripted fetch).

Three layers, in this order:

1. **Mechanical surface acceptance.** The route table was dumped from the live app and every route
   called over real HTTP: `GET` routes with path parameters substituted and unknown ids supplied
   (so "unknown thing" is answered by the real 404 path), `POST` routes with either the exact
   payload the UI sends or an empty body (to test validation). Every response was classified
   OK / contract-correct refusal / defect.
2. **Journey testing.** 75 calls grouped into the brief's eleven families A–K, checking not just
   status but payload shape and the semantic claim.
3. **Browser pass.** All eleven panels rendered; Chat, Routing, JARVIS were driven functionally;
   the shared thread was re-read after a reload; console and network output were inspected.

Reused from earlier audits, and marked as such below: the Phase 21–26 milestone verification and
the Integration & Performance Audit. Those results were only reused where the source tree is
byte-identical in the relevant package; anything I could exercise live was re-exercised live.

**What could not be tested on this machine** (each is reported, never assumed): a real desktop
being controlled, a real browser being driven, a real phone, a real LLM/vision model, a real
GPU/NPU, and real training runs (which the build refuses without explicit confirmation).

## 3. Feature-by-feature acceptance

The complete inventory with entry points, dependencies and per-feature status is
[REAL_USER_FEATURE_INVENTORY.md](REAL_USER_FEATURE_INVENTORY.md). Summary of the acceptance:

| Surface | Count | Result |
|---|---:|---|
| UI panels | 11 | **11 render correctly, each with distinct content and exactly one visible panel per tab** |
| UI panels driven functionally | 3 | Chat (typed question → answer), Routing (live gate trace), JARVIS (command → plan, nothing executed) |
| HTTP routes | 265 (124 GET, 141 POST) | **0 server errors** — 156 OK, 108 contract-correct refusals, 1 harness artifact (the SSE route, separately proven correct) |
| CLI subcommands | 19 top-level | Reachable; not the focus of a UI acceptance audit |
| Example plugins | 3 | Present in the package; the HTTP plugin surface reports empty and marketplace-disabled |
| Application capabilities in `/capabilities` | 17 modules | All report `Available` |

## 4. Major end-to-end workflows

| Journey | Scenario | Result |
|---|---|---|
| **A. Assistant / command** | greeting, "what is 2 plus 2", "what time is it", a research question, an unknown phrase, a device command | **PASS** — each landed on the right route (chat / local math / local time / explore / safe fallback / device); the unknown phrase answered instead of crashing; ~0.2–0.25 s per local answer |
| **B. Research** | standard + quick depth, follow-up topics, trending | **PASS** — a real 3-source report in 2.8 s with key points and videos; 8 live progress frames on the SSE channel; identical repeats served from a 900 s cache |
| **C. Planning / multi-step** | plan a goal, plan+execute, run through the agent loop | **PASS** — plan object, then `execute: true` returned plan + workflow + state; the agent loop returned a verification verdict rather than a bare "done" |
| **D. Engineering** | code plan, multi-file scaffold, save, re-run a saved artifact | **PASS** — artifact written to `build_workspace/` and re-executed (`ran_ok` reported) |
| **E. Vision / perception / world** | describe the screen, perception capability report, world observe → query → predict | **PASS WITH LIMITS** — describe captured and reported a summary; world query answered with evidence and staleness; **predict reported `model_unavailable` rather than inventing a prediction** |
| **F. Memory / context** | multi-turn chat, teach → recall, restart | **PASS** — context retained across turns; a taught fact survived a full process restart |
| **G. Automation / audit** | schedule → approve → gated run → audit | **PASS** — the time was parsed from the request wording, the phrase was stripped from the stored request, the task stayed `pending_approval` and unarmed until approved, the run executed the harmless request under the permission gate (runs 1, failures 0), and a matching audit record carrying the `automation_id` appeared |
| **H. Models / resources** | brain-mode round-trip, local model list, cloud presets, resources, privacy, cost | **PASS** — forcing `scratch` changed the answering provider, `auto` restored it; `/resources` gave level + reasons + thresholds; `/cost/estimate` returned a hint and no gate |
| **I. Learning / evaluation** | learning status, environments, metrics, transfer, generalization splits + leakage, game-eval contracts | **PASS (read)** — all report honestly; `aggregate_score` is absent by design (families measured in different units); game-eval reports `gameplay_claimed: false` |
| **J. Embodied** | status, environments, overview | **PASS (read)** — allowlisted environments and lifecycle vocabularies reported; no session driven |
| **K. Plugins / configuration** | plugin surface, settings round-trip, capability discovery, diagnostics | **PASS** — settings changed and read back; diagnostics ran its 25-component roster |

## 5. Phase 1–26 integration matrix

Verified by *running* the relationship where possible; otherwise marked as inherited evidence.

| Phase | Capability | Runtime evidence this audit | Status |
|---|---|---|---|
| 1 Foundation / 2 Core engine / 3 Memory / 4 Tools / 5 Agents / 6 Planning | Boot, request pipeline, memory store, tool registry, agent registry, planner | Server boots in ~5–8 s; `/status`, `/capabilities`, `/agent/run`, `/plan` all answer; memory SQLite written and read back after restart | **PASS** (inherited + re-exercised) |
| 7 Desktop automation | Approval-gated desktop actions | `/desktop/plan` returns a plan with a token; `/desktop/execute` without a valid token is refused; planning a real desktop command from the JARVIS panel rendered a plan and executed nothing | **PASS (gate verified)**; real desktop execution not attempted |
| 8 Browser automation | Navigate / search + extract | `/browser/plan` plans; execution gated; browser runner is the no-op runner on this machine | **PARTIAL** — real browser driving unavailable |
| 9 Vision | Screen understanding, guided click | `/vision/status`, `/vision/describe` (captured, honest fallback), bug log | **PARTIAL** — no vision model installed, correctly reported |
| Explore | Research pipeline | Live multi-source report + SSE progress | **PASS** |
| 10 Voice | Web Speech chat + JARVIS voice | Panel renders with voice controls; mic unavailable in an automated browser | **UNAVAILABLE** (needs a human with a microphone) |
| 11 GUI | Web platform | All 11 panels rendered and driven | **PASS** |
| 12 API | 265 routes, auth, rate limit, no-cache static, SSE | Full sweep; 0 server errors; rate limiter enforced at 120/min; cache-bust tokens injected | **PASS** |
| 13 Plugins | SDK + marketplace | `/plugins` reports an empty, marketplace-disabled surface; the SDK and 3 example plugins exist in-package | **PARTIAL** — no user-visible plugin door |
| 14 Optimization / resources / privacy | Governor, privacy policy, diagnostics | `/resources`, `/privacy`, `/diagnostics`, `/cost/estimate` | **PASS** |
| 15 Evaluation / reward | Trajectory recorder + reward foundation | `/evaluation/status`, trajectory lookup, `/benchmark` measured a run | **PASS (read)** |
| 16 SFT | Datasets, estimates, runs, model lifecycle | Estimate/dataset/create + every lifecycle action answered contract-correctly; a real start is refused without confirmation | **PASS (gate verified)**; no training executed |
| 17 Preference | DPO/ORPO pipeline, reviews | Dry-run priced the run; dataset validation; review decide refused an unnamed pair | **PASS (gate verified)** |
| 18 RLHF | Feedback, ratings, reward datasets, RL runs | Feedback stored, a rating recorded, dry-run priced, pipeline planned | **PASS (gate verified)** |
| 19 RLVR | Verifiers, critiques, corrections, verifiable reward | Verify/reward/critiques answered; disabling an unnamed verifier 404'd with a named reason | **PASS** |
| 20 Agentic RL | Whole-task RL, inert behind a mask | Endpoints answer; nothing trained | **PASS (surface)** |
| 21 Perception & abstraction | Perception layer, budgets, tracking | `/perception/status`, `/perception/capabilities` with live availability; perception refuses without a source, naming the field | **PASS** |
| 22 World model | State ingestion, structured query, prediction posture | `/world/observe`, `/world/query` (evidence + uncertainty), `/world/predict` (`model_unavailable`) | **PASS** |
| 23 Interactive learning | Environments, objectives, hypothesise→experiment cycle | `/learning/status|environments|metrics`, `/learning/transfer` | **PASS (read)** |
| 24 Planning / action policy | Goal planning, candidate plans, verification | `/planning/*` answered; goal creation refused a body with no `request`, naming it | **PASS (API-only)** |
| 25 Embodied agents | Sessions over allowlisted environments | `/embodied/status|environments|overview` | **PASS (read)** |
| 26 Generalization | ARC-style families, splits/leakage, bounded evaluation, game-eval contracts | `/generalization/status|families|splits|game-eval/contracts`, `/generalization/game-eval` | **PASS** |

**Cross-cutting integrations verified by observing both ends:** chat → shared transcript (one
writer), Explore → event bus → SSE → panel (frames observed live), device command → token →
execute (gate enforced), automation → permission gate → `handle_request` → audit trail (record
observed with its `automation_id`), brain-mode switch → provider hot-swap (answer changed),
settings → persistence → restart (values survived).

## 6. UI usability and state-consistency findings

**Working.** Sidebar navigation switches cleanly (exactly one panel visible per tab); the active
panel is remembered across reloads; live telemetry renders with real figures and states the
*reason* when a sensor is unavailable; the Routing panel shows each gate's verdict; the JARVIS
panel keeps the run button gated on a token-bearing plan; the shared chat thread renders the same
conversation to any client; the stale-build banner compares the page's `UI v…` stamp with the
served build.

**Findings.**

1. **Every chat turn rendered twice after a reload** — see defect D6. Fixed.
2. **A rate-limited page fails silently.** One page load issues ~33 requests and then polls
   `/system/telemetry` about 30 times a minute; the limit is 120/min, so two or three open tabs
   exhaust it. When that happens the panels simply stay empty and the only signal is a
   429 in the browser console. Not fixed (a back-off or an exempt-class decision is a design
   change); recorded as a follow-up.
3. **`/automation` reports `id`, its actions demanded `automation_id`** — see defect D7. Fixed.
4. Cosmetic: the Chat answer card repeats the question above the "Understanding…" block, and the
   user bubble sits above it, so the question appears twice on screen. Not a correctness defect
   (the answer, route and confidence are right); left as-is.

## 7. Failure handling, safety, recovery and persistence

**Invalid / incomplete input — PASS.** 108 contract-correct refusals across the sweep, every one
naming what was missing or wrong: `"goal is required."`, `"'source' is required (a path, or a
';'-separated frame list)…"`, `"No schedule was given or stated … say when it should run
(\"at 6 PM\"…)"`, `"a goal needs a 'request'"`, `"no trajectory 'audit-unknown-id'"`. **Before the
fix, ten of these were HTTP 500s** (defect D5).

**Authorization boundaries — PASS.** Desktop/browser execution without a valid approval token is
refused (`403`, existing token-gate tests); a phone execution with no paired bridge returns the
pairing plan so the UI can explain how to pair; automation arming is refused until approved, and
approval is re-checked at the moment of the run.

**Proposed vs completed — PASS.** Device commands render as a plan with a token and execute
nothing until approved; the agent loop returns a verification verdict and lists steps it could
not verify; scheduled runs are recorded only when they actually ran (`run_count` 1, `failures` 0).

**Honest unavailability — PASS.** No GPU → `Unavailable` with the reason, not `0 %`. No thermal
sensor → the ACPI reason. No prediction model → `model_unavailable`. No vision model →
`VISION UNAVAILABLE`. No local LLM → `effective_mode: scratch`, reported rather than claimed.

**Rate limiting — PASS (enforced).** Measured: 429 with `retry_after` once the window filled, and
`x-ratelimit-limit/remaining` on responses.

**Restart / persistence — PASS.** After killing the process and starting it again, the same store
still held: 32 chat turns, the taught fact, the modified setting, the scheduled automation task
with its parsed schedule, and the build artifacts on disk.

**Duplicate submissions.** A chat send produced one `POST /ask` and one recorded pair (after D2);
rapid double-clicks are guarded by the button's loading state.

## 8. Performance (one configuration, stated sample sizes)

Measured on the live instance; medians and p95 from n samples, `p95` being the
`round(0.95·(n−1))`-th order statistic (so with n=7 it is effectively the second worst sample —
stated rather than hidden).

| Endpoint | n | median | p95 | max |
|---|---:|---:|---:|---:|
| `GET /health` | 7 | 19.9 ms | 34.6 ms | 34.6 ms |
| `GET /status` | 7 | 53.8 ms | 62.4 ms | 62.4 ms |
| `GET /capabilities` | 7 | 23.1 ms | 32.2 ms | 32.2 ms |
| `GET /chat/history` | 7 | 16.1 ms | 26.4 ms | 26.4 ms |
| `POST /world/query` | 7 | 3.0 ms | 26.5 ms | 26.5 ms |
| `POST /plan` | 7 | 1.9 ms | 15.6 ms | 15.6 ms |
| `POST /plan/code` | 5 | 3.3 ms | 46.7 ms | 46.7 ms |
| `POST /ask` (local math) | 7 | 252.6 ms | 258.1 ms | 258.1 ms |
| `POST /ask` (research route) | 5 | 230.6 ms | 6 680.6 ms | 6 680.6 ms |
| `POST /explore` (3 sources) | 5 | **3.8 ms** | 3 302.2 ms | 3 302.2 ms |
| `POST /vision/describe` | 3 | 1 035.2 ms | 1 068.1 ms | 1 068.1 ms |
| `GET /diagnostics` | 5 | 2 025.1 ms | 2 035.7 ms | 2 035.7 ms |
| `GET /intelligence` | 5 | 4 036.8 ms | 4 044.8 ms | 4 044.8 ms |

Reading these honestly:

- **`/explore`'s median is a cache hit.** `ExploreService` keeps a 900 s TTL cache keyed on the
  request (and on the synthesis provider, so a mode switch cannot serve a stale report). Repeated
  identical topics answered in 3.8 ms; the uncached run took 3.3 s. The honest statement is
  "~3.3 s uncached, ~4 ms on a repeat", not "4 ms".
- **`/intelligence` (~4.0 s) and `/diagnostics` (~2.0 s) are the two slow reads.** They build whole
  reports; they are panel-open reads, not per-request costs. (The Integration & Performance Audit
  already reduced `/intelligence`'s model probes from 10 to 2 per request; this audit re-measured
  the remaining report-construction cost, not the probe count.)
- **Startup: READY in 5–8 s** (health polling), consistently.
- **Frontend**: idle page ≈ 30 `/system/telemetry` polls/min plus ~33 requests on load.
- **No stalls, no duplicate work, no memory spikes observed** during the sweeps; the only
  truncation seen was the rate limiter doing its job.

## 9. Defects, root causes, fixes, regression evidence

### D5 — Incomplete request bodies returned HTTP 500 with a traceback *(severity: Medium)*

- **Affected:** 19 routes across the app (`/improve`, `/improve/workflow`, `/improve/preview`,
  `/improve/approve`, `/learn`, `/train`, `/knowledge/teach`, `/plan`, `/plan/code`,
  `/build/project`, `/command/execute`, `/desktop/plan`, `/desktop/execute`, `/browser/plan`,
  `/browser/execute`, `/phone/plan`, `/phone/execute`, `/agent/run`, `/automation/schedule`).
- **Reproduced:** `POST /plan {}` → `500 Internal Server Error`; the server log carried
  `KeyError: 'goal'` from `str(payload["goal"])`.
- **Root cause:** each handler indexed a *required* field with `payload[key]`. A missing key is
  the caller's mistake, and the rest of this API already answers 422 naming the field
  (`/plan/run`, `/audit/delete`, `/benchmark`), so these were the inconsistent ones.
- **Fix:** generalized the existing field guard to `_required_field(payload, key)` (it refuses a
  missing, blank **or JSON-null** value) and used it at all 19 sites. `null` mattered: the first
  version of the fix stringified `None` to `"None"`, which the new test caught.
- **Regression evidence:** `RequiredFieldValidationTests` in `tests/test_web_api.py` — 4 tests,
  38 subtests (every route × empty / blank / null bodies, plus positive controls). Verified live
  after a restart: **19/19 return `422` with the field named; 0 × 500**, and the worst-case
  outcome of the whole POST surface re-run dropped to zero 5xx.
- **Reachability:** the web UI always sends the required field, so a normal user did not hit this.
  It was reachable by any other client, and the old behaviour leaked a stack trace into the log.

### D6 — Every chat turn was stored and rendered twice *(severity: Medium — user-visible)*

- **Reproduced:** one typed message in the Chat panel produced **4** transcript rows (two identical
  user/assistant pairs); after a page reload the panel rendered **every message twice**.
- **Root cause:** the turn was written by two owners. `handle_request` (the path the UI, the CLI
  and the GUI all take) appends `user` + `assistant` to the shared transcript, and `app.js` posted
  the same two turns to `POST /chat/history` again from `submitChat`'s `onSuccess`. A one-post
  probe confirmed the endpoint itself stores exactly one row, so the duplication was purely the
  double write.
- **Fix:** the server is the single writer. The client's `recordChatTurn` calls and the now-dead
  `chatTextForHistory` / `recordChatTurn` helpers were deleted; the client only *reads* the thread
  and migrates a legacy localStorage copy once.
- **Regression evidence:** `ChatTurnRecordedOnceTests` (`tests/test_web_api.py`) — one `/ask`
  stores exactly one pair, two asks store two ordered pairs. The source-contract test
  `test_chat_history_is_server_fed_not_localstorage_fed` was **rewritten** to pin the single-owner
  truth (client append and `recordChatTurn(` must not return) rather than the buggy contract it
  previously asserted. Verified live through the UI: clear → type → send → reload →
  **exactly one pair**.

### D7 — `/automation` reported `id`, its actions demanded `automation_id` *(severity: Low)*

- **Reproduced:** read `GET /automation`, post the task's `id` back to `/automation/approve` →
  `422 "automation_id is required."`
- **Root cause:** the report and the actions used different field names for the same value.
- **Fix:** `_automation_id` accepts either spelling (the longer one wins); the 422 still names the
  field.
- **Regression evidence:** `AutomationIdSpellingTests` (`tests/test_web_api.py`) — 3 tests (short
  spelling works, long spelling still works, an unnamed task is still refused with a named 422).
  Verified live: the `id` read from the report drove `approve` → `run`, leaving the task
  `scheduled` with runs 1 and failures 0.

**Not fixed, documented as follow-ups:** the silent rate-limited UI (§6.2); the question shown
twice on the chat answer card (§6.4); `/plugins` reporting an empty surface while the SDK ships in
the package (§5, Phase 13).

## 10. Exact commands, results, durations, unavailable checks

Commands are given as run (Git Bash on Windows, `PYTHONPATH=src`, `.venv/Scripts/python`).
Harnesses live outside the repo under `%TEMP%\nc_audit\`.

| # | Command (essence) | Result | Duration |
|---|---|---|---|
| 1 | `python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001` (isolated cwd) | READY, `/health` 200 in 5–8 s | — |
| 2 | `python harness.py` — GET sweep of all 124 routes | 95 OK · 28 contract refusals · 1 harness artifact | ~4 min |
| 3 | `python sse_contract.py` — SSE channel during a real `/explore` | 8 ordered `explore.progress` frames, correct `correlation_id` | ~8 s |
| 4 | `python post_sweep.py` + `python post_retry_429.py` — all 141 POST routes | 61 OK · 80 contract refusals · **0 defects · 0 5xx · 0 unverified** | ~9 min |
| 5 | `python journeys.py` — journeys A–K | 75 calls: 67 × 2xx, 8 contract 4xx, **0 defects** | ~2 min |
| 6 | `python perf.py` + `python perf2.py` — latency | the table in §8 | ~6 min |
| 7 | Browser: 11-panel navigation; Chat typed input; Routing trace; JARVIS plan | 11/11 panels; answer "Paris"/"4"; live gate trace; plan with no execution | ~5 min |
| 8 | Restart (stop PID, start again) + state re-read | thread 32, taught fact, setting, schedule, artifacts all survived | ~1 min |
| 9 | `pytest tests/test_web_api.py tests/test_chat_transcript.py tests/test_web_platform.py tests/test_brain_mode_and_tasks.py -q` | **147 passed, 380 subtests passed, exit 0** | 988.6 s |
| 10 | `python -m mypy src/novacontrol/api/app.py` | `Success: no issues found in 1 source file` | ~20 s |

**Checks that could not run — reported as limitations, not passes:**

- `python -m ruff check …` — **ruff is not installed** in this environment (`No module named
  ruff`), so no lint pass was performed. `mypy` was run instead and is clean.
- The **full 2,254-test suite** was not run in one go: the four-file targeted run exceeded the
  10-minute tool ceiling and was re-run in the background (see §11 for how far it got).
- **Real desktop / browser / phone execution, real LLM or vision inference, real GPU/NPU
  telemetry** — unavailable on this machine by construction, and each surface reports that
  condition honestly rather than fabricating a result.

## 11. Test and type-check status

- The three defects each carry new regression tests, and each new test was run and observed
  passing (`RequiredFieldValidationTests` 4 + 38 subtests; `ChatTurnRecordedOnceTests` 3;
  `AutomationIdSpellingTests` 3) — see §9 for the exact evidence.
- The rewritten source-contract test `test_chat_history_is_server_fed_not_localstorage_fed`
  passes.
- `mypy src/novacontrol/api/app.py` — clean.
- **The targeted run of the four most affected files passed in full: 147 tests, 0 failures,
  380 subtests passed, exit 0, 988.55 s (16:28).** Command:
  `PYTHONPATH=src .venv/Scripts/python -m pytest tests/test_web_api.py tests/test_chat_transcript.py
  tests/test_web_platform.py tests/test_brain_mode_and_tasks.py -q -p no:warnings`
  (log: `%TEMP%\nc_audit\pytest_affected.log`). This matters because it is not only my new tests:
  the whole pre-existing content of those files passes alongside them, so the `payload[key]` →
  `_required_field` rename across 41 call sites and the single-owner chat-transcript change broke
  nothing they cover.
- The **full suite was not run in one go** — it is ~2,254 tests plus 1,832 subtests and exceeds a
  single tool window, and the brief asks not to repeat broad runs unnecessarily. The four files
  above are the ones my changes can reach; the broader suite is an open item (§12).
- The API-reference sync check (`python scripts/generate_api_reference.py --check`) is unaffected
  by these changes: no route was added, removed or renamed — only field validation inside existing
  handlers changed.

## 12. Remaining risks and recommended follow-ups

1. **The rate limiter can silently blank the UI.** Two or three tabs exhaust 120 req/min, and the
   failure mode is an empty panel plus a console error. Needs a decision: raise the limit, exempt
   static/SSE reads, or have the frontend back off and say so on screen.
2. **`/intelligence` (~4.0 s) and `/diagnostics` (~2.0 s) are slow panel reads.** Both build whole
   reports per call; caching or narrowing the default payload would remove a visible pause.
3. **No UI for the phases 16–26 layers.** Training, preference, RLHF, RLVR, interactive learning,
   embodied sessions and generalization have working HTTP surfaces and no panel; a user cannot
   reach them without curl or the CLI.
4. **`/plugins` reports an empty, marketplace-disabled surface** while the plugin SDK and three
   example plugins ship in the package — either wire the door or say on the page why it is shut.
5. **Doc drift to correct.** `docs/AGENTS.md` still describes chat history as living in
   localStorage (`novacontrol.chatHistory`); the code moved the thread server-side
   (`data/chat_transcript.json`), and the client only reads it and migrates the old copy once.
   `docs/PHASES.md` stops at Phase 15 while the build is at Phase 26.
6. **Unverified at the end of this audit:** the *full* suite (the four files my changes can reach
   were run and passed — 147 tests, 380 subtests; the remaining ~2,100 tests were not run), the
   lint pass (ruff is not installed here), and every capability that needs hardware this machine
   does not have. These are open items, not passes.

## Overall

**PASS WITH LIMITATIONS.** The application was exercised through the interface a person actually
uses — a browser, typing into Chat, clicking through eleven panels — and through all 265 HTTP
routes. Zero server errors remain on the API surface, the three defects found were fixed with
regression tests and re-verified live, persistence survives a real restart, and the build is
honest about every capability this machine cannot provide.
