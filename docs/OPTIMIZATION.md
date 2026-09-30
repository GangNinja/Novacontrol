# Optimization, Resource Governance and Self-Diagnostics (Phase 14)

NovaControl is written for a specific machine — 16 GB of system RAM, an Intel
Arc with 8 GB, an Intel NPU, Ollama, local models — and Phase 14's job is to make
it *adaptive to the machine it actually finds itself on*, using runtime
telemetry rather than assumptions about the hardware.

The rule that shaped every module below is the same one Phase 7 established for
`models/hardware.py`: **a figure that was not measured is `None`, never a zero
that reads as "free" and never a guess wearing a number's clothes.** The NPU is
the clearest case. This build does not assume the NPU has dedicated memory, and
it will not claim an accelerator it cannot see: with no probe configured, the
NPU component reports `unknown` with the reason, not `ok` and not `failing`.

Four services, one shared vocabulary (`optimization/models.py`), and one policy
that every outbound path asks.

## 14.1 Model benchmarking — `optimization/benchmark.py`

`ModelBenchmarking` measures a model on tasks through an injected runner and
stores one `BenchmarkRecord` per task:

| figure | field |
| --- | --- |
| first-token latency | `first_token_ms` |
| tokens/sec | `tokens_per_second` |
| total latency | `total_ms` |
| RAM | `ram_bytes` (used, sampled around the run) |
| GPU utilization / GPU memory | `gpu_utilization`, `gpu_memory_bytes` |
| NPU usage where available | `npu_used` |
| structured-output success | `structured_ok` |
| task success | `task_ok` |
| tool-selection accuracy | `tool_ok` |
| failure rate | `failure` (plus `runs`/`failures` in the comparison) |

A runner that raises produces a **failed record**, not a missing one: the
failure rate is one of the specification's figures, and dropping failures would
report a better model than was measured. Every optional field means "not
measured" when it is `None`. First-token latency is measured only when a runner
can report it (the benchmark layer accepts it per completion); the completion
providers this build talks to are non-streaming, so the application's own runs
record `None` there rather than inventing a number.

`compare(category)` returns `ModelScore` rows per model, and rate properties
(`success_rate`, `failure_rate`, `structured_rate`, `tool_rate`) are `None`
rather than `0.0` when nothing measured them — "never succeeded" and "nobody
checked" are different claims. **No model is hard-coded as best**: whatever
`compare()`/`best_for()` return is read off the stored measurements. Storage is
JSONL (`JsonlBenchmarkStore`) with a configured cap; unreadable rows are skipped
rather than silently repaired.

The application runs a benchmark through the provider it already uses
(`POST /benchmark`), so what is measured is what a real request would get rather
than a second code path built for the benchmark.

## 14.2 Execution modes — `optimization/models.py`

`ExecutionMode` is a **policy**, not a claim about what works:

- `LOCAL_ONLY` — nothing leaves the machine: no cloud model, no remote model,
  no external search, no external tool, no telemetry. Local NLU, local models,
  local tools and local knowledge carry every request that can be carried.
- `BALANCED` — local first; an external path may be used when the privacy
  controls permit it and the local route cannot serve the request.
- `PERFORMANCE` — the operator has said quality/latency may justify an external
  call; permission and privacy policy still apply to every one of them.

An unknown spelling reads as `BALANCED` rather than the most permissive mode
(settings persistence and the manager both use `ExecutionMode.from_text`).

## 14.3 Privacy policy — `optimization/privacy.py`

**One** `PrivacyPolicy` answers "may this leave the machine?" — the requirement
"do not scatter privacy checks throughout the application" is met structurally.
Every control has exactly one enforcement point:

| control | enforced at |
| --- | --- |
| `allow_cloud` | `NovaBrain.set_cloud_allowed` — the ONE method that selects the cloud provider. Closing the slot makes `set_mode("cloud")` behave like `"llm"`, and `_apply_execution_mode` re-syncs Explore's synthesis provider so Chat AND Explore go local together |
| `allow_external_search` | the predicate `ExploreService` is constructed with (search, video search and page reading all sit behind it), the `TrendingTopicsProvider` that serves `/explore/trending`, and `research_task`, which degrades to local knowledge instead of failing |
| `allow_external_tools` | `_run_plan_step`, the single step runner every plan goes through: a step whose tool declares an external side effect raises `PrivacyDenied` (a `PermissionError`, so the executor records a DENIED step rather than a failure) |
| `allow_remote_model` | `PrivacyGatedDecisionProvider` wraps a configured remote decision provider; the engine sees a switched-off provider and answers locally, and `decide` declines outright even if `enabled` was read before the mode changed |
| `allow_telemetry` | the policy's answer for outbound telemetry; this build's sampling is local-only (reading this machine and keeping the result here is not a disclosure) |
| `sensitive_data_redaction` | the same operator switch the audit trail's redaction uses (one intent, one switch) |

The settings surface re-derives the policy from the operator's values and
re-points the switches (`NovaControlApplication.apply_privacy_settings`, which
also announces `privacy.mode_changed` on the one event bus; a refused search is
announced as `privacy.denied`). Browser automation is deliberately NOT behind
`allow_external_search`: it drives a local browser whose own traffic is the
browser's business, while this control governs NovaControl's own outbound calls
(search providers, page reading, the trending pool).

Controls (`PrivacyControls`, all persisted in user settings):
`allow_cloud`, `allow_external_search`, `allow_external_tools`,
`allow_telemetry`, `allow_remote_model`, `sensitive_data_redaction` (the same
operator switch the audit trail's redaction uses, so one intent has one switch).

`evaluate(action)` returns a `PrivacyDecision` with **one** reason naming the
switch that decided it — the first thing to change, not a list of everything
closed. `require(action)` raises `PrivacyDenied` (a `PermissionError`) for paths
that must not proceed quietly. Local telemetry sampling is unaffected by
`allow_telemetry`: reading this machine and keeping the result here is not a
disclosure.

## 14.4 Resource governor — `optimization/governor.py`

`ResourceGovernor` reads the machine through the **same** `HardwareMonitor` the
model manager uses (one measurement, not two) and classes it `AMPLE`, `TIGHT`
or `CRITICAL` — the worst level any considered dimension reached, with the
figure that produced it:

- free RAM against a comfort line (default 4 GB) and a critical line (1.5 GB);
- CPU utilization, GPU utilization and GPU-memory pressure (≥ 90 % used);
- temperature where accessible, and battery state on the road;
- the models currently resident.

`advise_load(needed_bytes, ...)` answers three-valued — `True`, `False`, or
`None` for "could not measure" — and lists the inactive models worth unloading.
The resource report (`GET /resources`) also carries `loaded_model_sizes`: the
resident models with the size the model layer knows for each one, measured when
the runtime reports it and `None` when it does not.
`CRITICAL` refuses even an unmeasured load, because the pressure is already
visible and a large model is exactly what must not arrive into it; `TIGHT` still
allows a load that fits, because "busy" is not "full". Thresholds are
configuration (`resources:` in the config file → `ResourceSettings`), not
constants: the second machine this runs on will not be this one.

The two examples in the specification map directly: *RAM available 2.5 GB* →
`TIGHT`, prefer lightweight, release inactive models; *GPU memory pressure
high* → `TIGHT`, and a vision model's load is refused by the fit check rather
than by a special case.

## 14.5 Model loading policy — `models/manager.py`

The governor is injected into `ModelManager` as its `advisor` and consulted on
the load path (a refusal never crashes a load — an advisor that throws is
ignored, the memory check still applies). Alongside it:

- lazy loading: models load when a request selects them, never at boot;
- keep-alive with idle eviction, plus **priority-ordered** eviction;
- `max_resident_models` (configuration): a concurrent-resident limit;
- a model an active task is using is **never** evicted underneath it — the
  request waits for memory instead.

Nothing loads a model merely because it is installed.

## 14.6 Task cost estimation — `decision/cost.py`

`TaskCostEstimator` combines the counted signals that already exist
(`intelligence/complexity.py`) with a small task-kind table read from the
wording, and returns a five-band `CostEstimate` (`trivial` → `very_high`) with
the reasons behind it: model requirement, cloud requirement, GPU requirement,
estimated RAM, expected latency, expected tool calls, risk and complexity.

The specification's examples are the acceptance test, and they hold:

```
"What's my RAM usage?"               -> trivial   (a local counter, no model)
"Analyze a large PDF"                -> medium    (a model, document-sized context)
"Build and test a full application"  -> very high (a plan with many steps)
```

`route_hint(estimate)` says whether a deterministic path can carry the request,
whether a lightweight model is enough, whether the work needs a capable one, and
whether it wants external data. **It is a hint, never a gate**: nothing in the
module can refuse a request. The estimate rides on the decision's own metadata
(which the audit trail already stores), so a request's expected cost is visible
afterwards without a second record of it.

## 14.7 Self-diagnostics — `diagnostics/manager.py`

`DiagnosticManager` keeps one roster and returns one structured result per
component:

```
component, status, severity, message, remediation, metadata
```

`status` extends the existing `HealthState` vocabulary (`ok`, `degraded`,
`failing`, plus `skipped` for deliberately-off components and `unknown` for
"this platform would not tell"); `severity` is separate from it, because a
component can be working at WARNING severity or deliberately off at INFO. The
overall state is the **worst that matters** — a verified failure outranks an
unreadable probe (which could be hiding one) which outranks a degraded
component, and a skipped one is neutral. The display form is the checklist in
the specification:

```
NovaControl Health
Core              ✓
Fast NLU          ✓
Ollama            ✓
GPU               ?
NPU               ?
Scheduler         ✓
Tools             ⚠
```

A check that throws, times out or returns the wrong type becomes a FAILING row
with the exception in its metadata — one broken probe never takes the roster
down. The timeout is configuration (`resources.diagnostics_timeout_seconds`,
default 5 s). Synchronous checks run in a worker thread, so a probe that reaches
for a runtime or a driver cannot stall the event loop the rest of the
application is using.

The registered roster: **Core, Fast NLU, Context, Decision engine, Planner,
Tools, Vision, Ollama, Models, Model manager, GPU, NPU, Database, Redis (skipped
when not enabled), Plugin system, Knowledge system, Scheduler, Permissions,
Storage, Network, Configuration.** Every check reads what is already wired — no
probe starts a runtime, a browser or a model just to answer the roster.

## API surface

| method | path | what it returns |
| --- | --- | --- |
| GET | `/privacy` | the mode, the controls, and every outbound decision |
| POST | `/privacy` | apply a mode/control change, live |
| GET | `/resources` | the governor's reading, with its reasons |
| POST | `/cost/estimate` | a request's cost estimate and routing hint |
| GET | `/diagnostics` | the roster (`?only=Core,Storage` for a subset) |
| GET | `/benchmark` | stored measurements and the measured comparison |
| POST | `/benchmark` | measure a model on tasks through the live provider |

`POST /settings` forwards `execution_mode` and the five privacy controls as
well, and re-applies both the audit retention policy and the privacy policy, so
a settings change takes effect without a restart.

## Verification pass

Every clause was driven through the real code after the build, which found and
fixed six gaps — the reason the controls are trustworthy rather than nominal:

1. **Explore kept a cloud synthesis provider after the mode closed it.** Chat
   went local while Explore still synthesized reports through the cloud slot.
   `_apply_execution_mode` now re-syncs Explore, and `set_cloud_allowed` falls
   back to the boot provider or Echo rather than leaving the cloud provider
   active with a relabelled mode.
2. **Explore searched externally regardless of `allow_external_search`.**
   `ExploreService` now takes one predicate and performs NO search, video
   search or page read when it is closed (it returns the existing offline
   path with a warning); the trending pool obeys the same predicate.
3. **`allow_external_tools` had no enforcement point.** It is now enforced in
   `_run_plan_step` against the step tool's own declaration.
4. **`allow_remote_model` had no enforcement point.** A configured remote
   decision provider is now wrapped in `PrivacyGatedDecisionProvider`.
5. **No events for privacy decisions.** `privacy.mode_changed` and
   `privacy.denied` are now published on the one bus.
6. **The resource report lacked model memory estimates**, and the diagnostic
   timeout was a constant; both are now present and configuration-driven.

## Tests

`tests/test_optimization.py` holds the Phase 14 suites (75 tests / 5 subtests):
benchmarking (measured figures, failed runs, cap, comparison honesty),
execution modes, the privacy policy and cloud blocking, the gated decision
provider, the governor's thresholds and advice, the model manager's governance
and loading policy, cost estimation, the diagnostic manager itself, and the
application-level integration (privacy flips reach the brain, Explore and the
persistence layer, a closed search performs no external call, the estimate
reaches the audit row, the roster covers every component the specification
names, a benchmark runs through the live provider).
`tests/test_web_api.py::Phase14ApiTests` (10 tests) drives the HTTP surface.

## Gates

- `pytest` in three file groups: **2254 passed / 12 skipped / 1832 subtests**
  (the count rose by two in the Phases 8–14 cross-phase pass, which also fixed a
  stage-status misreport in the specialist pipeline, a plugin that could not be
  re-enabled after being disabled, and a withdrawal that left permission
  declarations behind — see §37 of [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md))
- `python scripts/generate_api_reference.py --check`: **in sync** (88 routes)
- `python -m mypy src` and `--platform win32`: **253 source files, no issues**
- ruff clean on the new files; the repository's pre-existing E501 baseline is
  untouched
