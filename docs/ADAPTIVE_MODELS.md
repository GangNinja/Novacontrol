# Adaptive models and compute (Phase 33)

Phase 33 adds one selection layer over the model and hardware machinery that
already exists. It does not add a second model registry, task classifier,
scheduler or resource manager: `ModelManager`, `ModelProfile`,
`HardwareMonitor`, `ModelBenchmarking`, `TaskCostEstimator`, the Phase 27
learning engine and the configured cloud provider remain the owners of what
they already owned.

```
request text / intent / image
        │
        ▼
AdaptiveTaskClassifier ── uses TaskCostEstimator + StructuredIntent hints
        │  TaskProfile (capabilities, context, privacy, latency/cost preference)
        ▼
AdaptiveModelRouter
        │  eligible candidates from ProviderRegistry + endpoints
        │  gates: enabled/configured/health/cooldown, privacy/cloud, capability,
        │         context, cost cap, measured RAM fit
        │  score: capability/role fit, benchmark + observed success/latency,
        │         cost, local preference, learned policy bonus
        ▼
RouteDecision ── winner, rationale, rejected candidates, compatible fallbacks
        │
        ▼
provider adapter (existing local/cloud completion object)
        │  failure classified → retryable? → next provider-diverse candidate
        ▼
RouteOutcome + observation → performance evidence → Phase 27 learning
                                      │
                                      └→ bounded policy proposal → activate/rollback
```

## Registry

`ProviderRecord` describes a provider, not a model: kind (`local`/`cloud`),
enabled, configured, base URL, auth requirement, advertised rate limit and
concurrency, pricing and notes. It never stores a key. Re-registration refreshes
the provider's metadata but preserves its existing enable/disable switch, so a
status refresh cannot undo an operator's choice. `ProviderHealth` holds
process-local measured state: runs, successes, failures, last failure kind,
mean latency and a bounded cooldown after a retryable failure.

`ModelEndpoint` is a model attached to a provider. It carries the existing
`ModelProfile` capability declaration plus role, family, context/output limits,
pricing, rate limit, declared latency/reliability, limitations and
`metadata_source` (`runtime-reported`, `declared`, `profile`,
`configured-name-only`). Missing metadata stays missing: an unknown price is
`None`, never "free", and an unknown context length is not treated as unlimited
when a task supplies a hard context requirement.

Live local models come from `ModelManager.refresh_capabilities()` and
`discover()`, throttled to once per 30 seconds, and only when Ollama is enabled.
The configured cloud model is registered from the existing cloud provider
store. A cloud model defaults to TEXT only unless a caller supplies a declared
profile, because the OpenAI-compatible transport proves text and no provider
metadata in this build proves more. In explicit cloud brain mode the active
cloud model is registered only as a cloud endpoint, never under the local
provider; otherwise a local-only route could look available and cloud traffic
could be pointed at the local runtime.

## Task classification

`AdaptiveTaskClassifier` translates the existing `TaskCostEstimator` result and
`StructuredIntent` hints into a `TaskProfile`:

- category and complexity from the estimator; token estimates are labelled
  estimates, with larger floors for document/research/build tasks;
- required capabilities: TEXT always, plus VISION for an image, CODING for
  code/build, REASONING when the estimator calls for a capable model or the
  intent is high-reasoning, TOOL_CALLING and STRUCTURED_OUTPUT when requested;
- latency sensitivity: high for trivial/low tasks, quality for capable-model
  tasks, otherwise balanced;
- cost sensitivity: high for local-only/trivial work, low for high-cost work;
- `deterministic` when the estimator says an existing deterministic tool can
  carry the request and no image is involved.

A deterministic result returns `route="deterministic"` and spends no model
tokens. `complete()` reports such a request as **not answered** by the model
router (it names the deterministic path in its error), because the deterministic
handler owns the actual answer; it never returns success with empty text. A
vision task never requires REASONING merely because it is complex.

## Selection gates and score

Eligibility is checked before any scoring, in this order: provider registered,
enabled, configured, not cooling down, local if privacy requires it, cloud
allowed if not local-only, required capabilities present, context sufficient,
cost cap respected (an unknown cloud price is rejected when a cap is set), a
measured latency within the configured budget, and declared memory footprint no
larger than currently available RAM for a local provider. A latency budget uses
only a measured value; an unmeasured candidate is not rejected as if it had
failed the budget.

Eligible endpoints start at `0.5`. Evidence-based adjustments include role and
capability fit, parameter count for high-reasoning work, a vision specialist
preferred for images and not preferred for text-only work, measured provider
latency when latency matters, local preference, benchmark success rate, and the
router's own observed success/latency with shrinkage so one run cannot outrank a
fleet. The learned policy, when active, adds only its bounded per-category
bonus. All reasons are returned, including reasons for rejected candidates.

Selection is synchronous and does no runtime probe. Its measured cost on this
machine is in the Phase 33 report (0.2–0.4 ms for a decision).

## Execution, failures and fallback

`complete()` obtains a concurrency slot (default limit 2), acquires a local
model through the existing `ModelManager` (measured fit, room made, verified),
calls the endpoint's adapter, records provider health and a routing
observation, then releases the model. An empty answer is a malformed response,
not a successful call.

At most three compatible candidates are kept. A different provider is preferred
over another model on the provider that just failed, because a runtime that is
down takes all its models with it. Only timeout, rate-limit, overload,
connection, malformed-response and resource-exhaustion failures are retried.
Authentication, context-length, unsupported-capability, cancellation and unknown
failures stop the chain and return the classified reason; the router does not
try an unsuitable fallback just to claim success.

## Hardware, compute and local models

The router reuses `HardwareMonitor` and the model manager rather than starting
processes or loading weights itself:

- a local endpoint whose declared `memory_bytes` exceeds currently available RAM
  is rejected with both figures;
- the manager performs residency, eviction and verification; the router only
  asks for the selected model and releases it after use;
- `Hosting` here means local execution on the same machine. No GPU or NPU is
  claimed unless the hardware monitor measured it; VRAM-aware placement remains
  the Phase 7 limitation.
- concurrency is bounded by `RoutingPreferences.max_concurrency`, and the status
  read reports `inflight` and `queued`.

## Cost

`estimate_cost()` uses estimated input/output tokens and the endpoint's
per-million-token prices. Local discovery also prunes endpoints the runtime no
longer lists after a successful probe; an empty or failed probe is not treated
as evidence that nothing is installed. A changed cloud model replaces only its
stale endpoint, preserving provider health and the enable switch.

`estimate_cost()` uses estimated input/output tokens and the endpoint's
per-million-token prices. Local provider charge is exactly zero because the
machine is already paid for. Cloud pricing is only used when both input and
output prices were supplied; otherwise the estimate is `None` and the UI/API say
unknown. A configured `max_cost_per_request` is a real gate, and an unknown
cloud price with a cap set is rejected rather than assumed cheap.

## Learning and rollback

Every completion attempt becomes a `RoutingObservation` (provider, model,
category, success, latency, failure kind, tokens, fallback, retries). The same
observation is offered to the Phase 27 continual-learning engine as
provider-reliability evidence; it is explicitly not marked as user-task
verification.

`propose_policy()` needs at least three independent runs per model and at least
two measured models in a category. Its keys are **provider-qualified**
(`provider/model`), so a local and a cloud model with the same name keep
separate success histories; a model-only key from an older persisted policy is
still read as a fallback. It computes smoothed success rates, a bounded bonus
(`±0.35 × difference from the unweighted mean`) and requires an expected
improvement of at least 0.05 over baseline. A policy that only says "use the
single measured model" is not eligible. Activation snapshots the previous policy
(including the inactive v0 baseline), and rollback restores that snapshot. The
policy is persisted by the application's existing store and reloaded at startup;
a corrupt/missing record falls back to the evidence-only default.

## Integrations

- **Chat (Phase 2/Brain):** used only when a real local model is configured and
  an executable endpoint exists. `cloud` and `scratch` modes keep their existing
  semantics; router failure falls back to the previously configured provider.
- **Agents (Phase 28):** `CollaborationEngine` receives an assignment callback.
  Each subtask gets a model, provider, route and rationale in
  `metadata`/`context["model_assignment"]` and in scheduling state. Assignments
  are decisions for the agent pipeline; no new execution authority is granted.
- **Vision (Phase 6):** image tasks require the VISION capability; the live
  verification used the installed `qwen3-vl:4b` through the existing vision
  provider wrapper.
- **Phase 27:** observations are captured as learning evidence.
- **Phase 29/30:** they reach the same chat/agent seams and therefore the same
  model assignment; their approval and permission gates are unchanged.
- **Phase 31/32:** skill and personal-context requests are classified like any
  other text and never bypass privacy or context limits.

## API and UI

Nine authenticated routes (see [API.md](API.md)):

| Route | Purpose |
|---|---|
| `GET /models/routing` | providers, endpoints, health, preferences, policy, last route |
| `POST /models/routing/route` | select a model and show eligible/rejected reasons |
| `POST /models/routing/complete` | execute one routed completion with real fallbacks |
| `POST /models/routing/config` | change concurrency, cost cap, latency budget, cloud/local preference |
| `POST /models/routing/providers` | enable or disable a provider |
| `GET /models/routing/history` | recent decisions and observations |
| `GET /models/routing/policy` | proposal, active policy and performance evidence |
| `POST /models/routing/policy/activate` | activate a validated proposal |
| `POST /models/routing/policy/rollback` | restore the previous policy |

The System panel's **Adaptive Model Routing** card reads the live status,
previews a route for typed text, adjusts the concurrency/cost controls, toggles
providers, and activates/rolls back the learned policy. A failed read hides the
card rather than inventing state.

## Security

- Provider records contain no credentials; adapters retain their own existing
  key handling, and the routing routes never accept a key from the request.
- Every route requires authentication. A route is not an authorization bypass:
  tool calls, device actions and paid provider calls still pass their existing
  permission, token and cost controls.
- Local-only/privacy routing is enforced in selection, in `complete()` and in
  the application's completion route, and is recorded in the decision. Sending a
  cloud request is a separate, already-configured provider action; a routing
  preference cannot re-open a slot closed by the privacy mode.
- A lazy local upgrade or a scratch→LLM switch re-syncs the catalogue and
  reattaches the router to the brain, rather than leaving Chat on the old
  provider while the router sits idle.
- The API/UI expose provider health, model names, scores and failure
  classifications, not prompts, secrets or chain-of-thought.

## Configuration

Routing preferences are runtime controls exposed by `/models/routing/config`:
`allow_cloud`, `prefer_local`, `preferred_model`, `max_cost_per_request`,
`max_context_tokens`, `max_concurrency`, `latency_budget_ms` and `enabled`.
Unknown fields and out-of-range values are rejected (HTTP 422), not silently
clamped. The preference set is not a second configuration file; model
declarations, provider keys, privacy mode and hardware thresholds remain in the
existing configuration and settings.

## Known limitations

- Only one local runtime family (Ollama) and two installed models were available
  for live verification. The cloud completion path is tested at the adapter/API
  boundary; no paid provider was activated or charged.
- Selection uses estimates and declared metadata. It does not discover pricing
  or benchmark figures for a provider that did not report them.
- The GPU is reported, not used for placement; NPU support remains injectable
  and unset.
- A local model's declared footprint is the fit signal; measured runtime
  footprints come from the existing model manager and are preferred where it
  reports them.
- Provider `rate_limit_per_minute` and `max_concurrency` are reported metadata,
  not enforced limits; the router enforces one global concurrency semaphore and
  uses a rate-limit failure to trigger compatible fallback.
- The routing UI was checked by static tests and JavaScript syntax, not by a
  manual visual walkthrough in this phase. The card exposes concurrency/cost,
  provider and policy controls; `allow_cloud`, `prefer_local` and
  `latency_budget_ms` are API-level controls.
