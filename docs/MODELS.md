# Models and hardware (Phase 7)

NovaControl does not assume that one model handles everything, and it does not
decide which model to use by recognising a **name**. This document is the phase
that makes both of those statements true in code: a capability registry, a
hardware monitor, and one manager that answers *which model should serve this,
and is there room for it?*

## The pipeline

```
              ┌──────────────────────────────────────────────┐
request ────► │ ModelManager.select(required capabilities)    │
              │   · capability registry (profiles.py)         │
              │   · measured resources (hardware.py)          │
              │   · what the runtime reports (capabilities()) │
              │   · what is already resident                  │
              └───────────────┬──────────────────────────────┘
                              │  ModelSelection(route, model, provider, reason, match)
                              ▼
        local ─────── RAM-aware load ─────── verify ──► ready
          │                                            ▲
          ├── cloud (a provider is configured) ─────────┘  never removes the cloud path
          └── none  (nothing fits) — a RESULT, not an error
```

The three layers, and what each one is allowed to know:

| Layer | Question | Public surface |
|---|---|---|
| `models/profiles.py` | *What can each model do?* | `ModelCapability`, `ModelProfile`, `ModelRegistry`, `select_profile`, `apply_reported`, `infer_profile` |
| `models/hardware.py` | *What does this machine have?* | `HardwareMonitor`, `HardwareSnapshot`, `gpu_memory_bytes` |
| `models/manager.py` | *Which model, and is there room?* | `ModelManager`, `ModelProvider`, `ModelRuntimeStatus`, `ModelSelection`, `ModelLoadOutcome`, `KeepAlivePolicy`, `KeepAliveSettings`, `ModelTelemetry` |

`ModelManager` is a decision layer, **not a new runtime**. Its provider is the
`OllamaBackend` the lifecycle layer (`intelligence/model_manager.py`) already
drives, imported rather than reimplemented — and the backend can describe a
model's capabilities, which is what closes the loop below.

## Capability registry

Every model declares what it supports:

```
text   reasoning   coding   vision   tool_calling   structured_output   embeddings
```

`qwen3:8b` appears in exactly one place in this codebase: the declaration table
in `models/profiles.py`, as **data**. Nothing branches on those names, and
selection is a set comparison: a vision request asks for `{VISION}` and whatever
declares it wins.

Three rules keep the declarations honest.

**Unknown is not a yes.** A capability nobody declared is absent, so a model
whose abilities were never written down fails a requirement instead of being
guessed at. That is the opposite of convenient and the only safe direction: a
text model trusted with an image invents coordinates, and a model trusted with
tool calls emits prose no executor can run.

**A claim says where it came from.** A *declared* profile is a statement, an
*inferred* one is a guess read off the name (`inferred_from`), and a profile the
runtime described carries the runtime's own words (`reported`,
`runtime_confirmed`). `declared: false, runtime_confirmed: true` and
`declared: false, inferred_from: "a vision marker is in the name"` are different
claims, and a caller can tell them apart.

**A measurement outranks both, in the directions it can be trusted.**

```python
apply_reported(profile, ["completion"])          # a text-only build named "llava"
  → vision, tool_calling … removed               #   the runtime is the authority
apply_reported(profile, ["completion", "vision"])  # an undeclared VL build
  → vision added, marked runtime_confirmed        #   now selectable for real work
apply_reported(profile, [])                       # silence
  → unchanged                                     #   silence is not evidence
```

`coding` and `structured_output` are never *removed* by a runtime report: no
runtime here has a word for them, so silence about them is not evidence against
them. Denying what a runtime cannot express is how a capability table starts
lying in the other direction.

This is what makes a machine work without configuration. On the development box,
`qwen3-vl:4b` was installed and nobody had declared it; Ollama's `/api/show`
reported `vision`, so the vision pipeline selects it — where before this phase a
vision request fell through to the text brain and the pipeline declined.

## Hardware awareness

`HardwareMonitor` reads the platform probes that already exist in
`telemetry/hardware.py` — RAM, CPU, GPU utilization and memory, the resident
models — and adds one thing the model layer needs: an estimate.

```
total_ram_bytes / available_ram_bytes      measured, or None
cpu_percent                                measured, or None
gpu{available, utilization, memory}        only if actually detected
npu{available, reason, source}             injectable probe; absent by default
resident_models                            the runtime's own answer
estimate_bytes(parameters_b=8.0)           ≈ 4.8 GB (0.6 GB per billion, 4-bit)
```

**No accelerator is claimed that was not detected.** There is no portable way to
ask "does this machine have an NPU", so the probe is injectable and the default
answer is `available: false` *with the reason* — this build will not claim an
accelerator it cannot see. GPU figures are reported for the operator rather than
invented as a second constraint: the runtime loads into **system RAM**, so that
is what `fits()` decides on.

`fits()` has three answers, not two: `True`, `False`, and `None` for "could not
measure". An unknown is never read as a yes — that is the rule that keeps a load
from thrashing a machine whose free memory nobody could read.

## RAM-aware loading

```python
outcome = manager.load("qwen3-vl:4b")
outcome.steps   # every step, with what it saw
```
```
check_memory   → available_bytes, total_bytes
check_resident → models
estimate       → needed_bytes, fits, fits_after_evict, freed_by_eviction_bytes, source
evict          → models actually released
load           → accepted, seconds
verify         → verified: True | False | None
```

The fit is judged **twice**, and the difference is the whole point:

- `fits` — can the model live *alongside* what is resident? If yes, **nothing is
  unloaded**. Eviction is not a condition of loading: a model the incoming one can
  live beside stays resident, because a reload costs seconds and discards the
  warm pages the next request wants back.
- `fits_after_evict` — can it live there once the models that *may* be unloaded
  are gone? This is the specification's own switch (Qwen3 resident, a vision
  request, room made for the VLM). Refusing on the first figure alone would
  decline to switch on exactly the machine this layer exists for, and the reason
  would read plausibly the whole time.

Two protections hold regardless of the arithmetic:

- **A model an active task is using is never evicted**, and it is not counted as
  room either — a refusal has to be a refusal even in the arithmetic. Callers
  declare that with `begin_activity` / `end_activity`.
- **An unmeasured footprint is not free memory.** A resident model whose size the
  runtime did not report frees *nothing* in the calculation.

Step 6 is separate from `loaded` on purpose: the runtime accepting a load and the
runtime *listing* the model are two different facts, and only the second one
means the next request will work. When the probe cannot be carried out at all,
the outcome says so — *"the runtime accepted the load but could not confirm it"* —
rather than reporting a verified success.

### `acquire` — the pair a request path uses

```python
outcome = manager.acquire("qwen3-vl:4b")   # begin_activity + the six steps
...                                        # use the model
try:
    manager.release_after_use("qwen3-vl:4b")   # end_activity + the policy
```

`load` alone was reachable only from the explicit load endpoint, which meant an
AUTOMATIC request reached the runtime with the wrong model resident — the very
conflict this layer exists to resolve. `acquire` is the one call a handler makes
before a model works:

- the activity is taken **before** the load, so an eviction triggered by that
  load can never choose the model being acquired;
- a model already resident costs one probe and no switching (`already_loaded`);
- the pairing is `try`/`finally` at the call site, and `acquire` without a
  matching `release_after_use` is a leak.

A REFUSAL here is a result, not a hiccup: the vision handler answers from OCR and
reports that the model was not loaded, rather than asking the runtime to run a
model the measurement says does not fit.

## Keep-alive

| Policy | Rule |
|---|---|
| `immediate` | Release as soon as the last operation using it returns (`release_after_use`) |
| `warm` (default) | Hold it, release once idle for `keep_warm_seconds` (`enforce_keep_alive`) |
| `while_active` | Release once no task that selected it is still running |
| `never` | Never on the manager's initiative; only to make room |

Every policy is applied by a real caller: the chat handler marks the local model
in use for the duration of the answer it is writing (so an eviction cannot take
its weights mid-sentence) and releases it after; the vision handler does the same
through `acquire`. The `warm` idle window is applied where the manager is asked
about its state — the status read every client polls, and every explicit load —
because this process has no timer thread and inventing one would be a second
policy nobody configured. A deployment that sets `keep_warm_seconds: 0` (the
default) pays nothing for the check: it returns before touching the runtime.

`keep_warm_seconds: 0` — the default — is **not** "unload immediately": it means
*do not override the runtime's own residency*, because that number is the
runtime's to choose and inventing one here would be a guess wearing a
configuration's clothes.

The policy name is validated in one place (`models/manager.py`) and carried as a
string from configuration. A typo therefore cannot silently become `warm`:
`KeepAliveSettings.honoured` reports whether what was configured is what is in
force, and `status` shows both.

## Automatic routing

```python
manager.select_for_request(requires_vision=True, needs_tools=True)
manager.select({ModelCapability.CODING, "vision"})   # strings are accepted too
```

The inputs the specification names — intent, decision, complexity, vision
requirement, required capabilities, available resources, the loaded model,
latency preference — reach this as **capabilities**; the understanding and
decision layers decide what work the request is, and capabilities are how that
work is asked for. The rules, in order, none of which is a model name:

1. **Capability first.** A candidate that cannot do what was asked is not a
   candidate. A candidate whose measured footprint does not fit is rejected with
   that reason.
2. **An explicit choice wins.** `preferred` (the operator's configuration) beats
   the size heuristic.
3. **A resident model that satisfies wins next.** Switching costs a load and an
   unload; this is where "avoid unnecessary switching" is enforced rather than
   asked of every caller.
4. **Then the smallest that fits, unless latency says `quality`.** When the
   ability that makes a model large is not needed, the specialist wins the tie:
   extra capability the request did not ask for is a cost, not a feature.

The pool is what the runtime **offers**, so a declaration alone is not enough:
a model that is not installed cannot be selected.

An unrecognised capability name **raises** rather than being dropped
(`as_capabilities`). Dropping is right for a config file, where a typo must not
stop the process; it is wrong for a *requirement*, where `{"visoin"}` would
quietly become "nothing in particular" and be answered by a model that cannot see.

## Cloud fallback

`route` is one of three values, because "no model" and "the cloud" are different
answers and only one of them is a hand-off:

```
local  — a profile satisfied the requirement
cloud  — nothing local could, and a provider is configured
none   — neither; the reason names the capabilities wanted and what each
         candidate lacked
```

NovaControl's existing cloud providers are untouched: the manager reports the
route, and the cloud path behaves exactly as before (keys are still never
returned by an endpoint, and nothing is logged that contains one). No model is
ever downloaded.

## Switching telemetry

| Figure | Meaning |
|---|---|
| `loads`, `load_skips` | Real loads, and models that were already resident (the cheapest answer) |
| `unloads`, `evictions` | Releases, and releases performed to make room |
| `refusals`, `verified_failures` | Loads declined, and loads the runtime did not confirm |
| `load_seconds`, `unload_seconds` | p50 / p95 / max for both |
| `selections` | How many requirements went `local` / `cloud` / `none` |

### Measured on the development machine (16 GB RAM, Intel Arc, Intel NPU)

```
capability probe (2 installed models)          0.058 s
load   qwen3-vl:4b (3.07 GB measured)          7.3–8.0 s, verified True
unload qwen3-vl:4b                             31 ms
switch: VL resident → qwen3:8b                 4.87 GB needed, 2.06 GB free +
                                               3.3 GB evictable < needed + 0.5 GB
                                               headroom → REFUSED, with the numbers
npu detected                                   no — available: false, with the reason
gpu detected                                   no — no probe answered on this box
```

The refusal above is the honest one: this box was holding ~0.6 GB free at the
time, and loading a 4.87 GB model would have paged the desktop it is supposed to
be describing. The point is that the decision came from measurement, and it names
what it would have had to unload.

## Request telemetry

One trace per request, opened before understanding and closed after the response
is shaped, exposed through `GET /intelligence`:

| Figure | Where it comes from |
|---|---|
| NLU, context, decision, planning, tool selection, tool execution, vision, model load, response | `stage("<name>")` at each seam; p50/p95/max per stage |
| Total request latency | the trace itself |
| RAM before / after / delta | sampled by the caller (which owns the monitor) at both ends |
| Model selected, provider selected | the decision layer's own answer |
| Fast-path usage | requests carried out by the deterministic layers with no model selected |
| LLM escalation rate, first-token latency, tokens/second | the existing interpretation telemetry |

A stage that never ran reports `count: 0` with `avg: null`, so "not reached" and
"instant" stay distinguishable. A request whose exception escapes before the
handler is filed as a **failure** by the next request, so the request count stays
equal to the number of requests. And nothing here carries a prompt, an answer or
a chain of thought: durations, identifiers and measurements only.

## Configuration

```yaml
models:
  keep_alive: warm          # immediate | warm | while_active | never
  keep_warm_seconds: 0      # 0 = leave the runtime's own residency alone
  headroom_mb: 512          # memory a load must leave free (clamped to 8192)
  declarations: []          # models this build has never heard of
#   declarations:
#     - name: my-vlm:3b
#       capabilities: [text, vision, tool_calling]
#       context_length: 8192
#       parameters_b: 3
```

Environment overrides: `NOVACONTROL_MODEL_KEEP_ALIVE`,
`NOVACONTROL_MODEL_KEEP_WARM_SECONDS`, `NOVACONTROL_MODEL_HEADROOM_MB`. The
capability probe is bounded (1.5 s per pass, 0.5 s per model) because it runs on
the boot path.

## Where the manager is on the request path

| Moment | What the manager does |
|---|---|
| Boot | Asked to describe the runtime's models (bounded), then to select one for `{VISION}` — the answer wires the vision pipeline's provider |
| Every request | The trace records the stage durations, the selected model and provider, and the RAM it moved |
| A chat request | Marks the local chat model in use for the answer, releases it after, and files the provider's own timing breakdown (first token, generation rate) |
| A vision request | `acquire`s the pipeline's LOCAL model first — measured fit, room made, verified — and refuses to run a model the measurement says will not fit |
| Every model call | The provider's `last_timings` are filed under the reason that produced them (`chat`, `vision`), so the two figures describe the model that answered rather than only the NLU fallback |
| `POST /models/load` | Loads through the manager: measured fit check, verified state, refusals reported rather than raised |
| `POST /models/unload` | Unloads through the manager, which refuses to pull a model out from under a running task |
| `GET /models` | The measured readout: resident models, free RAM, GPU/NPU, and what would be selected for each kind of work. Also where the `warm` idle window is applied |
| `GET /intelligence` | The same table plus the lifecycle telemetry, without a runtime probe on the hot path |

## Known limitations

- **The GPU is reported, not used for placement.** The runtime loads into system
  RAM and the manager decides on that; VRAM-aware placement is future work, and
  until it exists the manager will not claim an acceleration path it has not
  measured.
- **The NPU probe is injectable and unset by default**, so a machine with an NPU
  reports `available: false` with that reason rather than a guess.
- **Measured footprints come from the runtime**, so a model the runtime has never
  seen is estimated from its parameter count and returns `source: "parameters"`.
- **`qwen3:8b` cannot be resident with `qwen3-vl:4b` on a 16 GB box that is also
  running a desktop** — measured, and the reason the switch is a real decision
  rather than a formality.
- **The vision model's own call is proven against a runtime, not against a VL
  build.** The switch, the refusal and the release are measured; whether a given
  quantized VLM answers well is not something a fake provider can establish, and
  the local vision model on the development machine is reasoning-only.
- **A cloud model is never acquired.** The manager drives the LOCAL runtime; a
  cloud provider is loaded by someone else, and a model the runtime has never
  heard of is not something it can make room for.
