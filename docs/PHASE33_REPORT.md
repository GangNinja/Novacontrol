# Phase 33 — Adaptive Model & Compute Intelligence

**Verdict: PASS** on the five gates in this environment, with the limitations
stated at the end. Scope was Phase 33 only. No Phase 34 work was started, and
nothing was committed or pushed.

This report was re-audited after the first PASS. The audit found and fixed twelve
concrete defects — including the completion API ignoring `local_only`, a
deterministic completion reporting success with no answer, provider switches
being reset by every status refresh, and the router not being reattached after
a scratch/lazy-local upgrade. The fixes, their tests and the re-run evidence are
recorded under [Audit pass](#audit-pass).

**Environment:** Windows 11, Python 3.13.0, 16 GB RAM, no detected GPU/NPU.
Ollama was already running at `http://127.0.0.1:11434`; the installed models
were `qwen3:8b` (text/reasoning/coding/tools, 32,768 context as registered) and
`qwen3-vl:4b` (text/reasoning/tools/vision). No model was downloaded and no
paid provider was activated or called. The `qwen3:8b` completion path had
already answered a direct `/v1/chat/completions` probe before the verification
run.

The phase did not create a second model registry, router, scheduler or resource
manager. It extends the Phase 7/14 model stack and the existing provider,
hardware, benchmark, cost, learning, agent and privacy layers. Architecture and
operating details are in [ADAPTIVE_MODELS.md](ADAPTIVE_MODELS.md).

## What was built

| Area | Implementation |
|---|---|
| Registry | `ProviderRegistry`, `ProviderRecord`, `ProviderHealth` and `ModelEndpoint` in `models/adaptive.py`; local endpoints are discovered through the existing `ModelManager`, and the configured cloud model is registered from the existing provider store |
| Classification | `AdaptiveTaskClassifier` translates `TaskCostEstimator` plus `StructuredIntent` hints into capabilities, context floor, privacy, latency/cost sensitivity and a deterministic flag; no second NLU |
| Selection | Eligibility gates for provider state/health, privacy/cloud policy, capability, context, cost cap and measured local RAM fit; scored ranking with benchmark, observed success/latency, role/capability, cost and policy evidence; every rejection carries a reason |
| Fallback | At most three compatible candidates, provider-diverse first; retries only timeout, rate-limit, overload, connection, malformed-response and resource-exhaustion failures; auth/context/unsupported/cancellation stop the chain |
| Local compute | Uses the existing `ModelManager` acquire/release path, measured fit, residency and verification; no model is loaded by the router itself; concurrency bounded by a semaphore and reported as inflight/queued |
| Cost | Token estimates × known per-million prices; local charge is exactly zero; unknown cloud price is `None` and is rejected when a cost cap is configured, never assumed free |
| Learning | Observations feed Phase 27 as provider-reliability evidence; policy needs ≥3 runs/model, ≥2 models/category and ≥0.05 expected improvement; activation snapshots the prior policy and rollback restores it; persisted through the existing store |
| Chat | `NovaBrain.set_model_router` and an optional routed path for `auto`/`llm`; existing provider remains the fallback; `cloud` and `scratch` semantics unchanged |
| Agents | Phase 28 `CollaborationEngine` receives per-subtask model assignments in metadata/context and scheduling state through the application callback |
| API | Nine authenticated `/models/routing*` routes; API reference regenerated to **454 routes** |
| UI | Adaptive Model Routing card in the System panel: live providers/models/health, route preview, concurrency and cost controls, provider toggles, policy activate/rollback |

## Requirement matrix

| # | Requirement | Evidence | Result |
|---|---|---|---|
| 1 | One unified provider/model registry | `ProviderRegistry` + `ModelEndpoint`; no duplicate registry; local discovery reuses `ModelManager`, cloud reuses the configured store | PASS |
| 2 | Truthful metadata | `metadata_source` distinguishes runtime-reported/declared/profile/configured-name-only; unknown price/context stays unknown; cloud defaults to TEXT only | PASS |
| 3 | Adaptive task classification | Capability/context/privacy/latency/cost requirements from the existing estimator; deterministic fast path; vision does not imply reasoning | PASS |
| 4 | Dynamic provider routing | Live route chose `qwen3:8b` for reasoning and coding, `qwen3-vl:4b` for an image, and `deterministic` for a RAM-status question; cloud/local gate unit-tested | PASS |
| 5 | Compatible fallback | Real connection refusal → installed `qwen3:8b` answered; incompatible and non-retryable classes are refused by tests | PASS |
| 6 | Hardware-aware selection | Live F: a 1.57 GB declared requirement was rejected with 0.49 GB available and a real model answered instead | PASS |
| 7 | Local model lifecycle | Selection/acquire/release through `ModelManager`; live runs loaded, used and unloaded both installed models without a second loader | PASS |
| 8 | Cost/latency/quality evidence | Cost estimates, measured latency, success observations and Phase 14 benchmark hooks feed ranking; no invented pricing | PASS |
| 9 | Reversible learning | Six genuine runs produced a validated chat policy (provider-qualified `ollama/qwen3:8b` +0.1181 / `ollama/qwen3-vl:4b` −0.1181), activated as v1 and rolled back to the inactive v0 baseline | PASS |
| 10 | Phase 27 integration | Observations captured as `model_router` evidence with `verified: false` | PASS |
| 11 | Phase 28 multi-agent assignment | Per-subtask assignment model/provider/rationale reaches metadata, context and scheduling | PASS |
| 12 | Phase 29–32 seams | Chat/agent/vision paths use the same routing where applicable; privacy, approval, permission and memory/skill gates are unchanged | PASS with stated boundary |
| 13 | API visibility and controls | Nine authenticated routes; all return real state, validated controls and 422/404/409 on bad input | PASS |
| 14 | UI visibility and controls | Live card plus real fetch handlers; static tests and JS syntax pass (not manually viewed) | PASS |
| 15 | Security and privacy | No secrets in records/API; local-only rejects cloud before an adapter call; auth required on all routes | PASS |
| 16 | Tests and regressions | Phase suite and targeted model/agent regressions; API/static contract; commands below | PASS |
| 17 | Documentation | This report, ADAPTIVE_MODELS.md, MODELS.md, STATUS.md, DEVELOPMENT_LOG.md, regenerated API.md | PASS |

## Live verification A–H

The driver is `tmp/phase33/verify_phase33.py`; full machine-readable evidence is
`tmp/phase33/evidence.json`, with stdout/stderr in `verify.log`/`verify.err`.
It uses a fresh temporary application data directory, the real router and the
real installed models. Synthetic parts are labelled: E intentionally points an
extra local provider at closed port 9 to produce a real connection failure; F
registers an explicitly oversized endpoint rather than starting a memory
pressure process; G registers a cloud endpoint whose adapter raises if called.

| Scenario | Actual result | Timing | Verdict |
|---|---|---|---|
| A. Simple RAM question | route `deterministic`, no model, no tokens | 0.1 ms selection | PASS |
| B. Reasoning ("all but 9 sheep") | `qwen3:8b` answered "Nine sheep remain."; `qwen3-vl:4b` comparison returned an empty answer at a 64-token cap | 26,564 ms total (26,553 ms model) | PASS |
| C. Coding | `qwen3:8b` produced `def add(a, b): return a + b`; the generated file executed and asserted `add(2,3)==5` (`ADD_OK`, exit 0) | 18,807 ms | PASS |
| D. Vision | `qwen3-vl:4b` read a generated image and answered exactly `NOVA-42` | 96,791 ms | PASS |
| E. Provider failure | preferred endpoint refused the connection (`connection`, 2,147 ms); provider-diverse fallback `qwen3:8b` answered the reversal question | 24,787 ms total | PASS |
| F. Resource pressure | declared 1,852,071,936 bytes vs 778,330,112 available; endpoint rejected; `qwen3:8b` served the request | selection-time | PASS |
| G. Privacy | `local_only` rejected the registered cloud endpoint ("privacy policy requires local execution"); cloud adapter never called; local `qwen3:8b` selected | selection-time | PASS |
| H. Learning | 3 `qwen3:8b` successes (1,973/445/442 ms) and 3 empty `qwen3-vl:4b` responses (11,706/1,566/1,522 ms); proposal eligible with support 9, baseline 0.537 → expected 0.875, provider-qualified bonuses; activated v1 then rollback restored v0 | — | PASS |

The first B run selected `qwen3-vl:4b` first because both models had the same
generic role before role inference was added at the endpoint boundary. The fix
infers a role from the capability profile (`VISION`/`CODING`/`REASONING`/…),
and the final run selected `qwen3:8b` directly. The comparison still records the
VL model's empty 64-token text answer as a real measured limitation, not a
scored benchmark.

Hardware at the start of the final run (after unloading previous residents):
15.37 GB total, 3.91 GB available, CPU 32.5%, GPU unavailable with its reason,
NPU unavailable with its reason. After the run: 2.27 GB available and 33.0 MB
driver RSS. The models were released at the end of the run; the user's Ollama
service was never stopped.

## Test commands and exact results

```bash
# Phase 33 suite (includes the API route tests and the agent-assignment test)
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_phase33_adaptive_models.py -q
# 40 passed, 61 warnings

# Phase suite + existing model, pipeline, lifecycle, optimization and Phase 28 regressions
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_phase33_adaptive_models.py \
    tests/test_model_phase7.py tests/test_model_pipeline_phase7.py \
    tests/test_model_lifecycle.py tests/test_optimization.py \
    tests/test_multi_agent_collaboration.py -q
# 282 passed, 33 subtests passed

# API reference sync + web platform/static + brain-mode tests
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_api_reference_sync.py tests/test_web_platform.py \
    tests/test_brain_mode_and_tasks.py -q
# 72 passed, 951 subtests passed

# Filtered web API routes/static contract
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m pytest tests/test_web_api.py -q \
    -k "static or Static or cache or commit or routes or Routing or Model"
# 8 passed, 15 subtests passed, 69 deselected

# Generated API reference is current (454 routes)
PYTHONPATH=src python scripts/generate_api_reference.py --check
# docs/API.md: in sync

# Modified JavaScript
node --check src/novacontrol/web/static/app.js
node --check src/novacontrol/web/static/js/render-panels.js
# both exit 0

# New routing package and integration seams
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m mypy src/novacontrol/models/adaptive.py src/novacontrol/models/__init__.py \
    src/novacontrol/agents/collaboration.py src/novacontrol/brain/brain.py
# Success: no issues found in 4 source files

# Application/API modules
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 \
  python -m mypy src/novacontrol/application.py src/novacontrol/api/app.py \
    src/novacontrol/api/models.py src/novacontrol/api/route_consumers.py
# 1 error: application.py:6068 pre-existing VisionProvider.see(..., structured=True)
# (present before Phase 33; no new error in the changed code)

git diff --check
# exit 0
```

No full-suite run was performed. The live driver is not part of the pytest
suite; it is the evidence-producing script named above.

## Audit pass

All findings below were fixed and covered by a regression test; the full Phase
suite and the A–H live run were then repeated. The audit found:

1. **`local_only` was ignored by `/models/routing/complete`.** The application
   forwarded the flag only in its response, so a cloud adapter could still be
   called. Fixed by threading `local_only` through `route_model_complete` and
   `AdaptiveModelRouter.complete`; tested at router and HTTP level.
2. **A deterministic completion claimed `answered=True` with empty text.** It
   now reports `answered=False` and says the deterministic request is handled
   outside the model router. Chat already falls back to its normal path.
3. **Re-registering a provider reset its user switch.** Every status refresh
   rebuilt the provider record with `enabled=True`; `ProviderRegistry.register`
   now preserves the existing enable/disable state. Tested with a disabled
   provider followed by a sync.
4. **The cloud provider was removed and re-added on every sync.** That erased
   its measured health and switch. The sync now keeps the same provider record,
   removes only a changed model endpoint, and still prunes a provider when the
   stored cloud configuration disappears.
5. **The router was not reattached after a scratch→LLM or lazy-Ollama upgrade.**
   The provider-upgrade callback only re-synced Explore. A single application
   helper now re-syncs Explore and routing and attaches/detaches the brain's
   router from the live catalogue; tested with a local-provider switch.
6. **Learned-policy bonuses merged same-named models from different providers.**
   Policy keys are now `provider/model` (with a backward-compatible model-only
   read), so a local and cloud model with the same name keep separate evidence.
7. **Uninstalled local models stayed selectable.** Runtime discovery now prunes
   local endpoints that a successful probe no longer lists, while an empty or
   failed probe is not treated as evidence of an empty install.
8. **The route-consumer contract still said `/models/routing` had no panel.** It
   now names `renderModelRouting`, matching the System-panel card; `docs/API.md`
   was regenerated.
9. **The unknown-context reason claimed there was no hard limit even when a
   context floor existed.** The reason now states that the context need is an
   estimate rather than a reported hard limit.
10. **App-registered local endpoints lost their image modality and could label
    inferred profiles as declared.** Modalities and provenance are now carried
    from the profile; inferred profiles report `metadata_source="inferred"`.
11. **Cloud mode registered the active cloud model as a LOCAL endpoint.** The
    local catalogue now excludes the cloud model when the brain is explicitly in
    cloud mode; a test asserts every endpoint for that model is a cloud record.
12. **The configured latency budget had no effect.** A measured latency above
    `latency_budget_ms` now rejects that candidate with its measured figure;
    unmeasured latency is not treated as failing.

## Gate verdicts

| Gate | Verdict | Basis |
|---|---|---|
| Functional | **PASS** | A–H live scenarios, phase tests, real installed models |
| Integration | **PASS** | Chat, agents, vision, learning, API and UI seams exercised; existing model/provider/privacy gates preserved |
| Testing | **PASS** | 40 phase tests; 282 targeted regressions + 33 subtests; 72 API/static/brain tests + 951 subtests; 8 filtered web API tests + 15 subtests; API reference in sync; JS syntax clean |
| Safety | **PASS** | Authenticated routes, no credentials in records/responses, local-only enforced before adapter call, no paid provider activation, no model download |
| Documentation | **PASS** | Requirement matrix, architecture/ops guide, API reference, status/dev-log updates, exact commands and evidence paths |

## Limitations (stated, not hidden)

- **Cloud completion was not live-tested.** No paid provider was authorized and
  no key was available in this environment. The cloud registration, pricing,
  privacy, cost-cap and adapter path are covered by phase tests and API
  contracts; live A–H used the installed local models. No cloud claim is made.
- **One local runtime family (Ollama) and two installed models** were available;
  cross-provider live diversity beyond the intentionally unreachable endpoint
  was therefore not exercised.
- **The vision call is slow** (93 s for one image on this CPU-only machine).
  That is measured, not a failure, and it is why image work keeps the existing
  local vision pipeline and its fit/refusal behaviour.
- **The routing UI was not manually viewed.** Its static contract, element IDs,
  fetch handlers and JavaScript syntax were checked; the visual walkthrough is
  outstanding. The card exposes concurrency and cost controls, provider toggles
  and policy controls; `allow_cloud`, `prefer_local` and `latency_budget_ms` are
  API-level controls rather than UI widgets.
- **Provider-declared rate limits and per-provider concurrency are metadata,
  not enforced limits.** The router enforces the global
  `RoutingPreferences.max_concurrency` semaphore and classifies a provider's
  rate-limit failure for fallback, but it does not run a per-provider token
  bucket. The fields are reported so this boundary is visible.
- **Route evidence is process-local** (bounded history/observations). The learned
  policy itself is persisted; raw histories are not a durable analytics store.
  This is deliberate and documented in [ADAPTIVE_MODELS.md](ADAPTIVE_MODELS.md).
- **Agent assignments are decisions, not a new execution path.** Phase 28
  receives the selected model/provider for each subtask; it does not grant tools
  or bypass approvals, and this phase did not run a new multi-agent live
  workflow against a real model.
- **GPU/NPU placement is unchanged.** Acceleration is reported only when the
  hardware monitor measured it; this machine reported neither.
