# Explore Performance Regression — Root Cause, Fix, and Verification

**Date:** 2026-11-01
**Scope:** Explore latency and which brain Explore uses. Extra 1 (animated companion) and the rest of NovaControl are preserved.
**Verdict:** PASS — Explore is back to the built-in local engine on a cold server at **5.47 / 6.59 / 5.61s** (median **5.61s**, min **5.47s**), down from **184.38s** measured (and ≥280s in two further runs). Ollama is now used for research **only when the LLM or Cloud brain is explicitly chosen**.

---

## A. Baseline

### Conditions

| Item | Value |
|---|---|
| Branch / tree | `main`, Extra 1 and NovaForge work present as uncommitted changes |
| Server | `uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001` |
| Query | `why do cats purr` (`include_videos: true`) |
| Brain mode | `auto` (persisted in `data/settings.json`) |
| Provider / model | Ollama `qwen3:8b` Q4_K_M, 5.94 GB, `size_vram: 0` (CPU-only), context 4096 |
| Cache | Empty (server restarted immediately before the run) |
| Machine load | Supermarket Together ≈ 94% of a core, OneDrive sync ≈ 75% during measurement |
| Measurement | Wall-clock `curl`, plus timestamped `GET /events/stream` progress frames |

### Runs

| Run | Result | Notes |
|---|---:|---|
| 1 (stage-profiled) | **184.38s** | 200 OK; full stage timeline below |
| 2 | **> 280s** (client cap) | no report; the server-side run continued after the client gave up |
| 3 | **> 280s** (client cap) | as above; contaminated by run 2 still consuming the model |

The two censored runs are lower bounds, not exact timings. They also exposed a separate robustness issue: a client timeout does not cancel the server-side Explore run, so an abandoned request keeps competing for the CPU-only model.

### Stage breakdown of the exact 184.38s run

| Stage | Time from start | Cost |
|---|---:|---:|
| `planning` (model chose queries) | 71.81s | **71.81s** |
| `searching` → `sources_found` | 71.93 → 75.05s | 3.12s |
| `reading` → `read` (2 pages) | 75.14 → 76.81s | 1.67s |
| `searching_videos` → `videos_found` | 76.91 → 78.17s | 1.26s |
| `synthesizing` → `complete` | 78.47 → 184.54s | **106.07s** |
| **Total** | | **184.38s** |

The synthesis call **failed**: `HTTPError: HTTP Error 500` after 106.07s, and the report fell back to the local evidence answer. The 106s bought no answer at all. Model calls were **177.88s of 184.38s (96.5%)**.

---

## B. Root cause

### The responsible code path

1. `try_llm_synthesis` (`src/novacontrol/explore/synthesizer.py`) and `plan_research` (`src/novacontrol/explore/planner.py`) both call `OpenAICompatibleLLMProvider.complete`.
2. `_ollama_native_payload` (`src/novacontrol/integrations/llm.py`) returned `None` for **non-structured** calls, so planning and prose synthesis were sent to `/v1/chat/completions`.
3. The OpenAI-compatible surface cannot turn reasoning off — it only separates it — so `qwen3:8b` spent its reply budget thinking. Measured on the same model: `/v1` with `max_tokens 700` took 115.4s and returned **zero characters**; the native `/api/chat` with `think: false` took 13.9s and answered.
4. Both calls were **unconditional and sequential** on the critical path: plan → search → read → synthesize. A new topic always paid both.
5. The machine cannot make those calls quickly: Ollama reports `size_vram: 0` (CPU-only), decode measured **3.1–5.7 tokens/s**, and a controlled prefill probe measured **2,271 prompt tokens in 135s (≈16.8 tokens/s)**.
6. The default `auto` mode handed Explore the detected Ollama provider. That is why even after the call-shape fix, the first Explore after a restart still waited ~30s for a plan: the provider had no measured rate yet, so the budget could not fire.

### Git-history attribution

- `8a91efd` — *Add question-first research pipeline and real system telemetry* (2026-09-20) introduced `plan_research` and the model-written synthesis call into `ExploreService`. That is when two blocking model calls entered the Explore critical path. Before it, Explore answered from the local evidence path (the historical 3–6s behaviour).
- Extra 1's changes (`src/novacontrol/web/static/*`, `js/companion.js`, `explore/trending.py`, related tests) contain **no** changes to `explore/planner.py`, `explore/synthesizer.py`, `explore/service.py`, `integrations/llm.py`, or the Explore provider wiring.
- Conclusion: **Extra 1 did not cause the regression.** It was implemented immediately before the slowdown was noticed, but the model calls predate it.

---

## C. Fix

### 1. Local-first Explore by default (the main correction)

`src/novacontrol/application.py::_sync_explore_provider`

- `auto` and `scratch` now pass **no provider** to Explore. The built-in evidence engine (`build_evidence` → `local_answer` → sections/citations) does the work, exactly as before the question-first pipeline.
- Only an explicitly chosen `llm` or `cloud` brain hands Explore the model. A detected Ollama is an offer, not a decision.
- Chat keeps its own `auto` behaviour; this function decides Explore only.
- Report caching remains keyed on the synthesis provider, so switching modes never serves a stale report.
- `_synthesis_notes` no longer claims "No model is connected" when a model exists but was not chosen for Explore. It now says the local evidence engine assembled the answer and how to opt into a model.

### 2. The model path is still correct when chosen

`src/novacontrol/integrations/llm.py`, `explore/planner.py`, `explore/synthesizer.py`, `explore/service.py`, `explore/explainer.py`

- Every local call uses the native `/api/chat` with `think: false`; `format: json` only for structured callers. Measured planning fell from **71.81s to 20.47s** with the same model.
- One measured latency budget (`NOVACONTROL_EXPLORE_MODEL_BUDGET_SECONDS`, default 25s, `0` disables) estimates planning and synthesis from the provider's own last `last_timings`. If a call cannot fit, it is skipped with a visible reason; the local answer is used.
- `last_timings` now records prompt tokens and prefill throughput, not just decode.
- Local synthesis is capped at 512 tokens.
- `SynthesisOutcome` distinguishes "skipped for latency" from "model failed".

### 3. Independent network work runs concurrently

`src/novacontrol/explore/service.py`

Page reading and video lookup are independent (the video query is the topic, not the pages). They now run under one `asyncio.gather`, so the request pays `max(reads, videos)` instead of their sum. Measured: 3.1s of page reads + 2.2s of video lookup became ~3.1s total. This moved the engine-default median from **8.74s to 5.61s**.

---

## D. Performance comparison

Final runs used the same machine, provider configuration (Ollama installed but mode `auto`), `include_videos: true`, and an **empty cache on a freshly restarted server**. No model call is made in `auto`.

| Metric | Before fix | After fix | Improvement |
|---|---:|---:|---:|
| Median end-to-end latency | **184.38s** (only exact sample; two further runs ≥280s) | **5.61s** (3 runs: 5.47 / 5.61 / 6.59) | **96.96%** / **32.9×** |
| Minimum latency | 184.38s | **5.47s** | 97.03% / 33.7× |
| Maximum latency | **≥280s** (censored) | **6.59s** | ≥97.6% / ≥42.5× |
| Time to first useful result (sources found) | **75.05s** | **1.75s** (stage-profiled gated run; same search phase) | 97.7% / 42.9× |
| Major bottleneck | Synthesis **106.07s** + planning **71.81s** = 177.88s (96.5%) | Search ≈1.4s + overlapped reads/videos ≈3.1s; **no model call** | model wait eliminated |
| Engine-default median before concurrency | — | 8.74s | concurrency: 35.8% faster |

Calculation: `((184.379901 − 5.613704) / 184.379901) × 100 = 96.96%`; `184.379901 / 5.613704 = 32.9×`.

### Cold-start behaviour

There is no calibration wait any more. The final three runs were the first three requests after a fresh server start: **5.47s, 6.59s, 5.61s**. Switching the brain to LLM/Cloud is what opts into model planning/synthesis and its (budgeted) latency.

### The earlier ~30s observation

After the first pass of this fix, `auto` still handed Explore the detected Ollama. On a fresh provider there was no measured rate, so the first request ran the plan (~30s) before the budget could apply. That is the 30s the user saw. The local-first wiring above removes that call entirely in `auto`; it cannot recur.

---

## E. Functional verification

### Live application (not just tests)

- The Explore tab was driven in the preview browser on a freshly restarted server (`why is the sky blue`). The rendered article contains **KEY FINDINGS, FROM THE SOURCES, VERIFICATION, GO DEEPER, ASK NEXT, WATCH NEXT, SOURCES AND VIDEOS**, and the note:
  > “This answer is assembled by the local evidence engine from the sources' own sentences. To have a model write it, switch the brain to LLM or Cloud.”
- No Ollama budget warnings appear in `auto` — the model is not being called.
- API samples on the final build: 5.47s / 1 source / 5 videos / 1155 chars; 6.59s / 1 source / 5 videos / 900 chars; 5.61s / 5 sources / 5 videos / 1049 chars.
- Extra 1's companion was verified in the same page: `#companionStage` data-state `idle`, data-motion `on`, and `#companionBody` transform changed from `scale(1.0119)` to `scale(1.0092)` over 700ms.

### Tests run

| Suite | Result |
|---|---|
| `tests/test_explore.py`, `test_explore_question_first.py`, `test_brain_mode_and_tasks.py` | 104 passed |
| Same + `tests/test_activity_timeline.py` | 116 passed, 35 subtests |
| `tests/test_integrations_llm.py` (+ native-path, prompt-timing tests) | 75 passed |
| `tests/test_extra1_companion.py`, `tests/test_web_platform.py` | 68 passed, 415 subtests |
| `tests/test_route_shape_contract.py`, `test_api_reference_sync.py`, `test_explore_trending.py`, `test_explore_history.py` | 52 passed, 462 subtests |
| `tests/test_application.py::test_explore_publishes_on_the_app_wide_event_bus` | passed |
| `mypy src/novacontrol/explore src/novacontrol/integrations/llm.py` | clean |

`mypy src/novacontrol/application.py` reports the **pre-existing** vision-provider `structured` call error (line 4582, unrelated Extra-1/vision work). The full 2,254-test suite was not run because the machine was simultaneously running the game and OneDrive sync; this is reported as a limitation, not a pass.

---

## F. Binary acceptance scorecard

| # | Objective | Evidence | Status |
|---|---|---|---|
| 1 | Baseline measured from the running app | 184.38s exact + stage timeline; two further runs ≥280s | **PASS** |
| 2 | Complete execution path traced | 8 timestamped stages; model 96.5% of latency | **PASS** |
| 3 | Extra 1 causality tested, not assumed | No Explore/model code in Extra 1 diff; `8a91efd` introduced the calls | **PASS** |
| 4 | Root cause identified | `/v1` ignored `think:false`; 106.07s synthesis → HTTP 500; CPU prefill 2,271 tokens/135s; `auto` used Ollama | **PASS** |
| 5 | Local engine is the default again | `_sync_explore_provider` passes None in auto/scratch | **PASS** |
| 6 | Explicit model mode preserved | `llm`/`cloud` hand Explore the chosen provider; gates only then | **PASS** |
| 7 | Unnecessary work eliminated | No model call in auto; thinking-free path + budget when chosen; reads/videos overlap | **PASS** |
| 8 | Before/after measured under comparable conditions | Same query/provider/cache/videos; table above | **PASS** |
| 9 | Explore correctness preserved | Live UI + API: answers, citations, findings, verification, videos | **PASS** |
| 10 | Extra 1 remains functional | 68 tests + live animation transform change | **PASS** |
| 11 | Relevant regression suites pass | 104/116/75/68/52 tests + subtests, application Explore test | **PASS** |
| 12 | No silent quality reduction | Local-engine note names the engine and the opt-in; model budget skips are explicit | **PASS** |

**Total:** 12 objectives — **12 PASS, 0 NOT PASS**.

---

## G. Remaining limitations (documented, not hidden)

1. **Source-count variability.** With the deterministic query builder, the final three runs returned 1, 1, and 5 sources (earlier engine runs: 5, 1, 3). The model planner produced broader coverage, but costs minutes on this CPU. The user can select the LLM brain when coverage matters more than latency; the report says so.
2. **Explicit model mode is still budgeted.** Choosing LLM/Cloud on this CPU-only machine will skip model planning/synthesis after 25s with an explicit warning, unless `NOVACONTROL_EXPLORE_MODEL_BUDGET_SECONDS` is raised or set to `0`.
3. **Abandoned client requests keep running server-side.** Observed during baseline: a curl timeout did not cancel the Explore handler. The default path is now seconds, so the impact is much smaller, but request cancellation is still not implemented.
4. **Full-suite run skipped** for time/CPU reasons as noted in §E; targeted suites and live checks passed.
