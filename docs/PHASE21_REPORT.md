# Phase 21 Report — Real-Time Perception & Abstraction

## Summary

Phase 21 turns NovaControl's existing vision layer into a **perception layer**: a
frame becomes a structured, temporally consistent, abstract representation that a
later phase can consume, cheaply when the cheap path suffices and honestly when it
does not.

It is built **on** Phase 6 rather than beside it. The same `VisionManager` answers
the deep questions, the same `OcrEngine` chain reads text, the same
`VisionProvider` boundary decides whether a model can see at all, and the same
`VisionResult` shape is what a model's answer arrives as. What is new is
everything the phase is about:

```
screen / camera / image / frame stream
        │
    frame acquisition        perception/frames.py        identity, extent, no pixels
        │
    preprocessing            perception/preprocessing.py decode → grey preview → scale
        │
    routing + sampling       perception/routing.py, sampling.py
        │
    fast perception          perception/providers.py     OCR text geometry + classical regions
        │                                                 + real classical masks
        ├─────────────── sufficient? ───────────────┐
        │ no                                        │ yes
    deep path (VLM)          Phase 6 VisionManager   │
        │                                             │
    spatial relationships    perception/spatial.py   ← pairs, with their arithmetic
        │
    tracking                 perception/tracking.py  ← identity by position, not recognition
        │
    temporal perception      perception/temporal.py  ← observed change between two scenes
        │
    structured scene         perception/scene.py     ← serializable, no pixels
        │
    abstraction              perception/scene.py     ← counts, relations, a summary
        │
    confidence + uncertainty perception/scene.py     ← from evidence, `None` when unmeasured
        │
    consumers                engine.py + agentic.py + the event bus + the API
```

Three rules are load-bearing and are enforced in code rather than in prose:

1. **The fast path runs first, always.** OCR and the classical region pass are
   deterministic and cheap; they run before any model is considered, and for a text
   or object question they usually *are* the answer.
2. **The model is asked through Phase 6.** Escalation builds a `VisionRequest` and
   calls the `VisionManager` the application already owns, so there is one place a
   model is asked about an image and one structured result shape to map.
3. **A refusal is a result.** No vision model, a provider that raised, an unreadable
   frame, a capability switched off — each produces a status that names the
   capability responsible. Nothing returns SUCCESS because *something* happened.

Verdict: **PASS**, with the machine-dependent capabilities classified as such on
purpose (see §18 and §27).

## 1. Files added

### New package: `src/novacontrol/perception/` (15 modules, ≈7,000 lines)

| Module | What it owns |
| --- | --- |
| `models.py` | The value types: `BBox` (with real arithmetic), `DetectedObject`, `OcrText`, `SegmentationResult`, `Relationship`, `TrackedObject`, `TemporalEvent`, `CapabilityStatus`, `SceneRepresentation`, `SceneAbstraction`, `PerceptionRequest`/`Plan`/`Result`, and the five vocabularies. |
| `frames.py` | Frame acquisition: `Frame`, `ImageFrameSource`, `SequenceFrameSource`, `ScreenFrameSource`, `CameraFrameSource`, `TestFrameSource`, `probe_image_file`, `describe_frame`, the walk-length rule. |
| `preprocessing.py` | Decode once: `GrayPreview` (rows of luminance, mean, 16-hex signature, diff), `PreprocessSpec`, `PreparedFrame` (scale + `to_frame_bbox`), `validate_frame`, `FrameUnreadable`. |
| `providers.py` | The provider-neutral fast path: `OcrTextDetectionProvider`, `RegionDetectionProvider`, `RegionSegmentationProvider` (real masks behind a bounded store), `ChainDetectionProvider`, the null providers, `PerceptionProviderError`. |
| `sampling.py` | `SamplingPolicy`, `SamplingDecision`, `FrameSampler` — change detection, adaptive backoff, and the counters that make a declined frame visible. |
| `tracking.py` | `SpatialTracker`, `TrackUpdate`: greedy IoU + distance matching, the five-state life cycle, measured velocity, relabelling. |
| `spatial.py` | `derive_relationships` → `RelationshipReport` (ordered pairs, evidence strings, shared confidence, pair budget with `capped`). |
| `temporal.py` | `TemporalPerception.observe` → `TemporalEvent`s between two scenes, plus `events_by_kind`. |
| `scene.py` | `build_scene`, `abstraction_for`, `focus_for`, `scene_confidence` — the structured scene and its compact form. |
| `routing.py` | `describe_question` and `PerceptionRouter.plan` — what the request needs, and why, with availability passed in rather than probed. |
| `governance.py` | `PerceptionProfile` (three budgets), `sampling_for`/`preprocess_for`/`max_objects_for`, `PerceptionResourceGate`, `AdmissionDecision`. |
| `capabilities.py` | The 8-row capability table with classifications, live availability probes, and `register_perception_capabilities`. |
| `engine.py` | `PerceptionEngine` — the walk, the fast/deep split, the verdict, telemetry, the event seam, and the honest summary. |
| `agentic.py` | `scene_observation`, `perception_observation`, `attach_observation`, `states_from_results` — the consumer adapter for Phase 20's `AgentState`. |
| `__init__.py` | The package surface, `PHASE`, `PERCEPTION_SCHEMA_VERSION`, `DEFERRED_PHASES`, and `overview()` with the honesty flags. |

### New test file

`tests/test_perception.py` — 90 tests over the real pipeline, with two documented
doubles (`ScriptedOcr`, `ScriptedVisionProvider`) and seven generated image
scenarios.

### New documentation

`docs/PHASE21_REPORT.md` (this file).

## 2. Files modified

| File | Change |
| --- | --- |
| `src/novacontrol/core/events.py` | 8 lifecycle event types (`perception.started/completed/failed`, `perception.frame`, `perception.scene_changed`, `perception.object_appeared/disappeared/moved`) with their required payload fields, so a payload that cannot be read is rejected where it is built. |
| `src/novacontrol/core/config.py` | `PerceptionSettings` (profile, target/max fps, min interval, max frame side, max objects, allow_vlm, allow_segmentation, temporal_context), `_PERCEPTION_PROFILES`, the ceilings `MAX_PERCEPTION_FPS`/`MAX_PERCEPTION_FRAME_SIDE`/`MAX_PERCEPTION_OBJECTS`, `_rate_value`/`_rate_setting`, and the `perception:` section in `from_mapping`. |
| `src/novacontrol/application.py` | The engine is constructed with the live `VisionManager`, the real governor/monitor/model-manager gate, the event observer and the approval-gated screen capture; the 8 capabilities are registered on the ONE registry; `status()["perception_pipeline"]` exposes the engine's own status. |
| `src/novacontrol/api/app.py` | `POST /perception`, `GET /perception/status`, `GET /perception/capabilities`. |
| `src/novacontrol/api/models.py` | The three `ApiRoute` rows (210 routes total). |
| `src/novacontrol/api/route_consumers.py` | The three `no-render` consumer declarations — the same reasoning as `POST /plan/run`: these are API/CLI surfaces, and claiming a panel that does not exist would be a lie in the contract. |
| `docs/API.md` | Regenerated (`scripts/generate_api_reference.py`), in sync. |

No existing vision behaviour was replaced or duplicated: `novacontrol/vision/`
and `desktop/vision.py` are untouched.

## 3. Frame acquisition

Every source implements the same protocol (`source_id`, `kind`, `available`,
`unavailable_reason`, `read()`, `close()`), so the pipeline has **one** frame path.

* `ImageFrameSource` — one file, and **exactly one frame**: re-reading the same
  still image twelve times because twelve is the stream ceiling was a defect found
  and fixed during verification (§26).
* `SequenceFrameSource` — a `;`/newline-separated list, read to its declared extent.
* `ScreenFrameSource` — a capture **callable**. In the application that callable is
  `VisionController.capture_screen()`, the same approval-gated path the Vision
  panel's Describe Screen uses; the perception package never captures anything
  itself and has no desktop import.
* `CameraFrameSource` — no capture callable, `available = False`, reason
  `no camera backend is wired`. A camera request ends as UNAVAILABLE naming the
  missing provider instead of failing somewhere deeper.
* `TestFrameSource` — real files, in order, with a repeat count (the static-stream
  case); being a test source is not a licence to fabricate frames.
* `_FrameListSource` (engine) — caller-supplied `Frame`s walk the same path, which
  is what makes the temporal case *the ordinary case*.

Frames carry identity and extent, never pixels: `Frame.event_payload()` has
`frame_id`/`source_id`/`sequence`/size and **no path**, which the tests pin.

## 4. Preprocessing

One decode per frame, then everything else works on a bounded grey preview:

* `PreprocessSpec(max_side)` bounds the preview by profile (128 / 192 / 320 px) and
  can crop to a region of interest in the *original* image's coordinates.
* `GrayPreview` holds rows of luminance, answers `mean_luminance()` (or `None` for
  an empty preview) and `signature()` (a 16-hex digest of size + rows), and
  `diff()` reports `comparable`, `mean_delta`, `changed_fraction`, `max_delta` with
  a pixel delta of 16. Mismatched sizes report `comparable = False` instead of
  comparing the shared region and producing a number that looks measured.
* `PreparedFrame` keeps `scale_x`/`scale_y` and an offset, and `to_frame_bbox()`
  maps a preview box back into the frame's own pixels **one way only** — nothing
  reduces a full-resolution position, because there is no reason to.
* `validate_frame` answers "why can this not be read" cheaply (no path, missing
  file, zero size, undecodable headers) and `prepare_frame` raises
  `FrameUnreadable` with the reason when the pixels cannot be decoded.

## 5. Fast perception — OCR

The OCR engine is Phase 6's own chain (`text-file + windows-ocr`, falling back to a
null engine), and the perception layer treats it as a *provider*: `OcrText` records
carry the text, the line, the position when the engine has one, and the provenance
(`ocr:text-file+windows-ocr`). A word read from a text file has **no geometry** and
says so; a placed word from the platform engine has real geometry, and it becomes a
detection with that box. Measured on this machine: a 1000×320 PNG with 64 px glyphs
reads as `SAVE CHANGES / Error: disk full` in ≈400 ms.

The engine records an OCR row on every text-planned request, with
`available=True` and reason `the OCR engine read no text` when the frame held
nothing — "the reader worked and found nothing" is a different statement from "no
reader exists", and both are visible.

## 6. Fast perception — object detection

`RegionDetectionProvider` is a real detector that needs no model, no download and no
GPU: it finds pixels that differ from the frame's own background in the luminance
preview (`pixel_delta=24`, `min_area=12`) and labels the components it forms.

It must not claim to know what those regions *are*, and it does not: the label is
`region`, `class_id` is `None`, and `confidence` is `None` — a connected component
is a measurement, not a probability. The measurements that decided it (area, mean
luminance, contrast) travel in `metadata` so a caller can judge for itself.
`ChainDetectionProvider` runs the text detector and the region detector and takes
the **union** (a button *is* a region and the word on it *is* text), dropping
near-identical boxes from a later provider so the same thing never arrives twice
with two provenances.

## 7. Fast perception — segmentation

`RegionSegmentationProvider` returns **real masks**, not boxes: for each region it
re-runs the classical pass, keeps the component's pixels, crops them to the region's
own box (255 = inside), and stores them behind a bounded store (32 segments × 4
frames). `mask_pixels(mask_id)` is the only door to those bytes; the serialized
`SegmentationResult` carries the *reference* (`mask_id`, `pixel_count`,
`mask_origin`/`mask_width`/`mask_height`) and never the rows, which is pinned by
test. Measured: a red ellipse's mask covers 1,248 pixels while its bounding box
covers 17,589 — the difference is the whole point.

Segmentation is planned only for an object question and only when the request allows
it (`allow_segmentation`, default off for cost), and it is off in the balanced
profile's default request.

## 8. Tracking

`SpatialTracker` matches detections to identities by **position**: IoU ≥ 0.2 first,
then proximity within 12 % of the frame diagonal (which makes the tolerance
resolution-independent), with a small bonus for an unchanged label. Every tunable is
a named constructor argument.

The life cycle is forgiving for one frame and strict after that: `NEW` → `VISIBLE`
→ `OCCLUDED` (first miss — something stepped in front of it) → `LOST` (second miss —
report the disappearance) → `REMOVED` (after `max_missed=3`; the identity ends and
its state is reported one last time). A detection that reappears before removal
resumes its identity, frame count and all. Movement beyond 1 % of the diagonal
emits `OBJECT_MOVED` with the measured pixel delta, and velocity is computed from
timestamps **only when both are readable** — otherwise it is `None`, never a guess.
`TrackedObject.spatial_only` is `True` on every track this build produces: this is
continuity, not recognition.

## 9. Spatial relationships

`derive_relationships(objects)` visits pairs in a stable order and reports every
relation the boxes support — `left_of`, `right_of`, `above`, `below`, `inside`,
`contains`, `overlaps`, `near` — each with:

* `evidence`: the arithmetic in words (for example
  `a.right=120 <= b.x=160`), so a relation can be checked rather than believed;
* `confidence`: the **weaker** of the two objects' confidences, or `None` when either
  is unmeasured;
* `provenance`: `geometry`, qualified by the readers that produced the boxes.

Objects without extent are skipped entirely (a box with no area cannot support a
claim about position), and the pair budget is **reported**: a capped run says
`capped=True` with `pairs_considered` and `pairs_skipped` rather than silently
returning fewer relations.

## 10. Temporal perception

`TemporalPerception.observe(scene, object_events=…)` compares the current scene with
the previous one it saw and returns the events that difference supports:
`scene_static`, `scene_changed`, `text_appeared`, `text_changed`, `text_removed`,
`confidence_increased`, `confidence_decreased`. Text comparison is
whitespace-normalized and case-folded, so a re-wrap is not a change; the events list
is bounded (200) and truncation is counted, not hidden.

Tracker events are **passed in** rather than re-derived: this layer has no
identities of its own, and inventing a second tracking mechanism would be exactly
the duplication the phase rules out. Cross-frame state is opt-in through
`PerceptionRequest.temporal_context`: a request that does not ask for it starts from
a clean tracker, temporal baseline and sampler, so a fresh look can never report
disappearances for objects that were never in this frame's world (§26).

## 11. Structured scene representation

`SceneRepresentation` is the serializable record a consumer reads: scene and frame
identity, timestamp, sequence, width/height, objects, text, relationships, tracks,
segmentation references, temporal events, focus, confidence, uncertainty,
providers, per-capability states, the abstraction, and metadata (sampling decision,
relationship accounting, escalation and sufficiency, preprocessing, the plan).

Pixels and masks are **structurally absent** from `to_dict()`, and a test asserts
the JSON holds no `mask`, `pixels` or `base64` key. `build_scene` is the only
constructor, so every consumer sees the same shape.

## 12. Abstract representation

`abstraction_for(scene)` produces the compact form: a one-line summary built from
what was actually there (`2 objects detected (1 person, 1 region). 2 lines of text
read.`), label counts, up to 6 highlights drawn from relations and located text, an
excerpt, provenance names, and `basis` — `deterministic` or `deterministic+vlm`,
because a summary that mixed model nouns with measured regions without saying so
would read as more certain than it is.

`focus_for(question, scene)` answers "which objects and lines was this question
about" **lexically**, with the same content-word test the vision layer uses, and
reports `match: none` with empty lists rather than pointing at whichever object
happened to be nearest. An invented focus is worse than an empty one.

## 13. Confidence and uncertainty

Confidence is computed from the evidence that exists — how many capabilities
answered, how many objects support the scene, whether the text was read with
geometry — and never from a model's style. `uncertainty` is `1 − confidence` when
confidence is known and `None` when it is not; a scene with nothing measured reports
`None`, not `0.0`.

A model's own reported confidence travels beside a `vlm:` provenance and changes the
abstraction's `basis`; it never silently replaces a deterministic measurement.
`CapabilityStatus.latency_ms` and the result's `latency` map follow the same rule:
a stage that did not run has **no key** rather than a zero.

## 14. Routing: fast first, deep only when asked for

`describe_question()` reads the request as `text`, `objects`, `locate`, `scene` or
`general`, and `PerceptionRouter.plan()` turns that into a plan with its reasons
recorded (`reasons` travels in the result, so "why did that go to the model?" is
answerable from a run):

* text → `[ocr]`, no model (a reader answers it);
* objects → `[ocr, detection]` (+ `segmentation` when allowed), no model unless the
  fast evidence comes back empty;
* locate → `[ocr, …]` with the model as the **fallback**, because a placed word is
  the answer when it exists;
* scene → the model is planned, because describing needs semantics the fast path
  does not produce;
* `general`/unrecognised → the model is allowed as a fallback.

Modes force the outcome: `fast` never escalates, `deep` always tries, `auto`/`hybrid`
escalate only when the fast evidence did not suffice (`fast_sufficient` and
`fast_reason` are in the result and in the scene metadata), and `allow_vlm=False`
forbids the deep path entirely — the reason says `the request forbids a vision
model`, and no model is contacted.

Three doors stand in front of the model, checked in order: the request's own latency
budget (already spent is already spent), whether a model exists at all, and the
resource gate. The deep question then travels as a Phase 6 `VisionRequest` with
`prefer_ocr=False`, and the answer is mapped back with `vlm:` provenance — only
elements **with geometry** become objects (a model that affirmed an element without
saying where it is has not produced a located object, and inventing a box would make
the detection list lie), and model text carries no positions at all.

## 15. Resource governance

`PerceptionResourceGate` is the only place this layer asks to spend memory, and it
asks the **existing** `ResourceGovernor` (the same one the model manager consults,
reading the same `HardwareMonitor`). Its rules are stated rather than implicit:

* the fast path needs no gate (`allowed=True`, "the fast path does not need a model");
* a model already resident is allowed without asking (nothing would be loaded);
* **no governor wired means the model path is refused** — absence of a gate is not
  permission to spend the RAM;
* the governor's three-valued advice is honoured: `allow=None` ("could not measure")
  is a refusal, with the governor's own reason quoted.

The required size comes from the model manager's own catalogue
(`model_size_bytes`), or `None` when nobody can say — passed through rather than
guessed, because a guessed size either refuses a load that would have fit or permits
one that would not. Nothing here loads, downloads or unloads a model; the gate only
decides whether the *request* may take the expensive path, and `degrade_to` names
the profile a bounded run should use instead.

**Two paths ask about residency, and only one may probe.** `admit()` — the rare,
model-backed path, reached only when a vision model is actually wired — may ask the
runtime what is in memory, because that answer feeds the governor's `loaded=`
eviction list. The **status surface may not**: `gate.status()` rides in every
`app.status()` poll (which `/system/telemetry` makes on each request), so it reads
residency from the governor's own assessment row instead of the runtime — the same
probe-free contract `models_status()` states. `test_status_never_asks_the_model_runtime`
pins it.

## 16. Sampling and the perception budgets

`FrameSampler` decides which frames are worth analysing, using the preview diff:
the first frame is always processed, a changed scene is processed (and counted), a
static scene is skipped with an adaptive backoff (2× per consecutive skip, up to 4
steps, at most 10 consecutive skips, `min_interval_ms` floor, `max_fps` ceiling).

A declined frame is **not analysed** — the frame was still acquired (the event says
so and the decode was paid for), and the telemetry counts `frames_analysed` and
`frames_skipped` separately, so "the policy is decorative" is impossible to claim
and impossible to ship.

Three profiles set the budgets in one table: `low_resource` (0.5/1 fps, 400 ms,
128 px preview, 24 objects), `balanced` (2/5 fps, 120 ms, 192 px, 48 objects),
`performance` (6/12 fps, 60 ms, 320 px, 96 objects). `set_profile()` rebuilds the
sampling policy, the preprocessing spec, the object ceiling and the gate together,
so the profile a decision was taken under travels with the decision.

## 17. Configuration and ceilings

`PerceptionSettings` (config file `perception:` section) carries the profile, the
sampling overrides, the preview ceiling, the object ceiling, and whether VLM
escalation and segmentation are allowed at all. Values are clamped: fps to
`MAX_PERCEPTION_FPS = 30`, preview side to `MAX_PERCEPTION_FRAME_SIDE = 4096`,
objects to `MAX_PERCEPTION_OBJECTS = 512`, and an unknown profile resolves to
`balanced` rather than failing. Zero or negative numbers keep the default instead of
disabling a bound.

## 18. Capability classification (honest by construction)

`capabilities.py` carries one row per capability, and `capability_rows(engine)` reads
the **live** availability from the engine's own provider slots — a renamed field
makes a capability report UNKNOWN, which is exactly what a probe that cannot find its
provider should say.

| Capability | Classification | Live availability on this machine |
| --- | --- | --- |
| `ocr` | IMPLEMENTED | available — `text-file+windows-ocr` |
| `detection` | PARTIALLY_IMPLEMENTED | available — `ocr:…+region:classical` (region/text level only) |
| `segmentation` | PARTIALLY_IMPLEMENTED | available — `region:classical` (real masks, no classes) |
| `tracking` | IMPLEMENTED | available — spatial continuity only |
| `relationships` | IMPLEMENTED | available |
| `temporal` | IMPLEMENTED | available |
| `abstraction` | IMPLEMENTED | available |
| `vlm` | PROVIDER_DEPENDENT | **unavailable** — `no vision model is wired` |

The vocabulary also carries `mock`, `unavailable` and `future`, and a test pins that
the table uses only that vocabulary. `overview()` is the machine-readable form of the
phase's posture: `cuda_required`, `automatic_model_loading`, `automatic_model_downloads`,
`stores_raw_frames`, `stores_masks`, `stores_hidden_reasoning`, `action_execution`
and `predicts_future_state` are all `False`, and `DEFERRED_PHASES` names Phases
22–26 explicitly so a reader never has to infer the boundary from absence.

## 19. The capability registry and the event bus

The 8 capabilities are registered on the **ONE** `CapabilityRegistry` the
orchestrator plans from (`register_perception_capabilities`), with
`executor="perception_engine"`, `category="perception"`, `filesystem:read` declared
for the two capabilities that read the disk, `required_models=("vision",)` for the
VLM, and an availability reason that says which of the two situations applies:
`implemented in this build; no provider needed`, `provider: …`, or
`no vision model is wired`. Verified through the live registry: 8 rows, all with
availability, all under `category="perception"`.

Events travel through an **injected seam**, never a bus lookup: the engine announces
`perception.started`, `perception.frame`, `perception.completed`/`failed`,
`perception.scene_changed` and `perception.object_appeared/disappeared/moved`, and
the application's `_perception_event` translates them onto the app-wide
`EventBus` through the same `_announce_soon` every other publisher uses (so a
payload that cannot be read is refused where it is built). Payloads carry identifiers
and measurements — never a frame path, never a text block; a test asserts the path
never appears in any relayed event, and that a throwing observer is counted
(`observer_failures`) and never fails the perception it announced.

## 20. API endpoints

Three routes, added in the five places the contract requires (handler, `ApiSurface`
row, `ROUTE_CONSUMERS` row, regenerated `docs/API.md`, test):

* `GET /perception/status` — providers and their live availability, the budgets in
  force, tracker/temporal state, telemetry counters, and the **shape** of the last
  scene (counts and ids only). No frame path, no text, no pixels.
* `GET /perception/capabilities` — the classification table with live availability,
  the schema version, and the deferred phases.
* `POST /perception` — perceive an image, a frame list, the screen or the camera into
  a structured scene. **Reading only**: nothing here clicks, types, opens or runs
  anything. A missing source is a 422 with a message naming what to send; an
  unreadable frame is a 200 whose status is `failed` and whose reason names the
  problem; a refused deep path is a 200 whose status is `partial`/`unavailable` and
  whose reason names the capability.

Measured through the real `create_app()` + TestClient: all three answer, the
no-content assertions hold, and the route/consumer contract test passes (210 routes,
`set(routes) == set(ROUTE_CONSUMERS)`).

## 21. Consumer integration

The perception layer is consumable from three directions, and all three are
supported by the same result:

1. **The character interface** — the tools and the suite's own recipe of
   `shape`/`text`/second-observation accessors (`objects`, `text`, `relationships`,
   `tracks`, `temporal_events`, `abstraction`, `text_block`, `capability(name)`).
2. **Phase 20's agentic loop** — `perception_observation(result)` returns a bounded
   mapping (objects, text lines, relationships, track ids, events, providers, status
   and status reason) with lists capped at 32/24/16 so an observation cannot grow
   without limit; `attach_observation(state, result)` duck-types on Phase 20's
   `advanced()` contract and returns the observation itself when the state has no
   such method — a caller with a plain dict is not broken by the adapter.
3. **The event bus** — the lifecycle announcements above, for anything that watches
   rather than asks.

## 22. Human-readable output and the status surface

`status()` answers "what is this wired to and what has it done": profile, providers
(with availability and reasons), the capability table, sampling, tracking, temporal
state, the resource gate's own view (assessment and thresholds — with resident
models read from that same assessment and memory figures from the monitor's own
`available_ram_bytes()`/`total_ram_bytes()`, never a runtime probe: this surface
rides in every `app.status()` poll), and
telemetry counters — with `raw_frames_stored`, `action_execution`, `cuda_required`
and `automatic_model_loading` all reported `False`. The application exposes it as
`status()["perception_pipeline"]`, beside the existing `vision_pipeline`.

## 23. Testing strategy

`tests/test_perception.py` (90 tests) drives the **real** pipeline over seven
generated image scenarios (blank, text, shapes, multi-object, complex panel, a
two-frame movement pair, a static pair) plus a non-image file and a missing path:

* **unit**: box arithmetic, request tolerance, vocabulary, frame probing/event
  payloads, preview measurements and box mapping, readability failures, source
  availability, walk lengths;
* **providers**: classical regions, text geometry, an OCR failure as a provider
  error, null providers, real mask pixels vs their boxes, dedup in the chain;
* **tracking**: the full five-state life cycle, measured movement and velocity,
  relabelling;
* **spatial**: containment/ordering with evidence, weaker-confidence inheritance,
  the pair budget;
* **temporal**: baseline, static, text change, removal, tracker-event passthrough,
  and the `temporal_context` opt-in (fresh state vs continued identity);
* **scene**: parts, counts, summary, basis, focus matching, no pixels in the wire
  shape;
* **routing**: question kinds, mode forcing, the model forbidden, the reasons;
* **resource gate**: no-governor refusal, an unmeasurable governor's refusal, a
  resident model, profile change preserving collaborators, status never asking
  the model runtime, residency read from the governor's assessment, the
  monitor's real memory figures (and the honest failure row), and the
  `loaded_models` field on the decision path — the last four are regression
  pins for §26's defects 11–12, each mutation-checked;
* **engine (integration)**: blank/success, shapes, text, complex, segmentation
  gating, camera unavailable, missing file failed, two-frame temporal change,
  `frame=` as a one-frame walk, the object ceiling, per-stage latency, real frame
  accounting and sampling skips, capability availability, observer events and a
  broken observer, a broken provider, profile changes;
* **escalation (provider double)**: a working model answering a description (asked
  exactly once, through the manager), the no-governor refusal, a failing model
  (recorded, PARTIAL, never "escalated"), `allow_vlm=False` never contacting a model,
  and a spent latency budget refusing by name without asking the governor;
* **consumers/privacy/honesty**: the agentic adapter, the refusal observation, the
  wire shape holding no pixels/masks/other frames, the package neither shelling out
  nor reaching the network, the capability vocabulary and table, deferred phases,
  and `overview()` agreeing with `status()`;
* **HTTP**: the three routes through the real app factory, including the 422 and the
  honest 200s.

The whole file runs in ≈31 s and needs no network, no model and no GPU; the suite is
deterministic on CI's Ubuntu runner because the OCR-dependent assertions use the
documented double and the real engine is only ever asked what it can actually do
(its own availability).

## 24. Measured performance (this machine)

Windows 11, Python 3.13, no GPU, ~3.8 GB free RAM (the governor reports the machine
as "tight"), the real OCR chain and the real classical providers, the application's
own wiring. Figures from `tracemalloc` and wall-clock timings; no claim here is
extrapolated from a different machine.

| Workflow | Result | Cost |
| --- | --- | --- |
| Read the text in a 1000×320 image | SUCCESS, `fast_sufficient=True`, text `SAVE CHANGES / Error: disk full` | 409 ms wall, of which OCR 400 ms, preprocess 3 ms |
| Objects + masks in a 640×480 image | SUCCESS, 3 objects, 3 masks, 6 relations | 724 ms wall: OCR 357, detection 344, segmentation 18, preprocess 3, the rest < 0.1 ms |
| Two-frame walk (24 px move) | SUCCESS, `object_moved: moved 23px right, 0px down`; abstraction `2 objects detected (2 region). 1 object moved since the previous frame.` | 1.42 s for two frames |
| Screen capture + perception | SUCCESS, 48 objects (the balanced ceiling), 157 text lines | 24.3 s wall — dominated by the approval-gated screenshot; the perception stages total 1.39 s |
| Camera request | UNAVAILABLE `no camera backend is wired` | 0 ms |
| "Describe what is happening" with no model | PARTIAL, `unavailable: vlm (no vision model is wired, so the deep path cannot run)` | — |
| Still-image ingest | 1.25 image/s (798 ms each) | dominated by the platform OCR pass (≈700 ms/image) |
| Bounded stream (12 frames) | SUCCESS; peak Python heap **3.54 MB**, live heap 3.28 MB | bounded memory regardless of frame count |
| `gate.status()` (inside every status poll) | memory row honest (`available: true`), residency from the governor's assessment | **1.2 ms** (was 2,014 ms — see §26 defect 11) |
| `status()["perception_pipeline"]` → `app.status()` | full status document including the new key | 78 ms cold / 49 ms warm (was 2,079 ms before the fix; `test_polling_is_cheap` passes again) |

The engine's own latency breakdown is honest about where the time goes
(`ocr` ≈ 400 ms on this machine, `detection` ≈ 344 ms — it includes the text
detector's OCR pass, `segmentation` ≈ 18 ms, `relationships`/`tracking`/`temporal`
each < 0.1 ms), and a stage that did not run has no key at all.

Two costs are worth stating plainly: the platform OCR pass dominates every
text-planned request (a property of the Windows engine, not of this layer), and a
screen capture costs an approval-gated screenshot (a property of the existing
design, deliberately unchanged). Neither is hidden: both appear in the per-stage
latency.

## 25. Privacy, safety and the "no fake intelligence" invariants

* **No pixels, ever.** A frame is a reference plus identity; masks live behind
  `mask_pixels()`; the abstracted scene, the status surface, the events and the
  observations carry counts, ids and measurements. Tests assert the absence.
* **No raw-image logging.** The package writes no file and no log line about content;
  telemetry is counters only, and the traffic that could carry a picture (events,
  status) is asserted free of paths and text.
* **No autonomous action.** The package has no desktop, browser, phone or subprocess
  import; a test scans every module and fails if `subprocess`, `os.system`,
  `os.popen`, `import requests`, `import httpx`, `import socket` or `urlopen` ever
  appears. Reading is separated from acting, and the approval gate still stands in
  front of the action.
* **No automatic model loading or downloading.** Nothing in the layer loads, pulls or
  trains a model; the deep path can only use a provider the application already
  wired, and it must pass the governor to do so.
* **No CUDA assumption.** No accelerator is required, referenced or expected; the
  whole fast path is CPU and dependency-light (Pillow, which the project already
  depends on).
* **No invented measurements.** `None` for an unmeasured confidence, velocity,
  latency or memory figure; `match: none` for an unmatched focus; `capped`/`skipped`
  counts where a budget cut work; a refusal named rather than an empty success.

## 26. Defects found and fixed during verification

The first implementation was measured, not assumed, and these are the things the
measurements and tests caught:

1. **A still image was read twelve times.** `_collect` looped to the stream ceiling
   for every source. Now the walk length is derived from the source: a declared
   extent wins, live sources are bounded by the ceiling, and anything else is a
   still — one frame. Telemetry immediately went from 24 frames per two requests to
   2.
2. **`frame=` was accepted and ignored.** `perceive(frame=…)` documented a one-frame
   walk and used the request's source instead. It now builds the same
   `_FrameListSource` a supplied stream uses.
3. **`temporal_context` was dead API surface.** Cross-frame state persisted across
   unrelated requests, so a fresh look could report `object_disappeared` for tracks
   from an earlier, unrelated observation (observed in the first measurement run).
   The flag now scopes tracker, temporal baseline and sampler: without it, a request
   starts clean; with it, identity and change detection continue across calls.
4. **The sampler's decision was recorded but not enforced.** Every frame was analysed
   regardless, which made the policy decorative. A declined frame is now skipped
   before any capability runs, and `frames_analysed`/`frames_skipped` make the saving
   visible.
5. **Per-stage latency was missing.** Only the capability rows carried timings, so a
   700 ms request reported 2.5 ms of "latency". The result's `latency` now includes
   each stage that ran, and `total` sums them.
6. **A failed deep path was reported as SUCCESS.** A description request whose model
   failed but whose fast path found text ended SUCCESS with the failure buried in a
   capability row. It now records the deep-path failure, so the verdict degrades to
   PARTIAL with the model's own reason.
7. **A deterministic capability's reason implied a dependency.** The reason read
   `provider: perception`; it now reads `implemented in this build; no provider
   needed`.
8. **The application's event relay had the wrong payload type** (mypy), and two
   capture paths dereferenced an `Any | None` callable. Both fixed; `mypy src` and
   `mypy src --platform win32` are clean on all 348 files.
9. **A test's expectation was wrong, not the code.** A 120 px move exceeds the
   tracker's own tolerance (12 % of the diagonal), so the honest report was
   `object_appeared` + occlusion, not `object_moved`. The test now moves the box
   24 px, which is what "the same object moved" means.
10. **Ruff/formatting debt in the new code** (import order, unused imports, long
    lines, a nested `if`) was paid down: `ruff check` is clean on the package and the
    test file.
11. **The status surface spent two seconds on every poll.** `gate.status()` asked
    the model manager's `runtime_status()` — one HTTP round trip to the local
    runtime, a 2 s timeout when Ollama is not running — and `gate.status()` rides in
    `perception.status()`, which rides in `app.status()`, which `/system/telemetry`
    calls on **every request**. Ten polls took 20.5 s against a 1.0 s budget, so
    `test_telemetry.py::test_polling_is_cheap` failed (under load *and* solo: it
    was this code, not contention). Residency now travels from the governor's own
    assessment row (already computed in the same call), the runtime probe is
    confined to `admit()` — the rare model-backed path, which only runs when a
    model is wired — and `gate.status()` measures **1.2 ms** (was 2,014 ms) with
    `nova.status()` back at 49–78 ms (was 2,079 ms). Pinned by
    `test_status_never_asks_the_model_runtime` (mutation-checked).
12. **The same read never worked, and the memory row never worked either.** The
    probe looked up `status.resident`, but `ModelRuntimeStatus` calls the field
    `loaded_models` — so the governor was always told `loaded=()` ("nothing is
    resident", eviction list always empty) while paying the two-second cost. And
    `gate.status()` called `monitor.memory()`, a method `HardwareMonitor` does not
    have, so every status said "the memory probe failed" and `available_ram_bytes`
    was always `None` — on a machine that measures fine. The field name is fixed
    (pinned by `test_admit_reads_residency_from_the_loaded_models_field`), the
    memory row now reads `available_ram_bytes()`/`total_ram_bytes()` and reports
    `available: true` with real figures (pinned by
    `test_status_reports_the_monitors_real_memory_figures`), and an unmeasured
    residency is `None` — unknown — rather than `[]`. Both mutation-checked.

## 27. Deferred items and explicit non-goals

Named so a reader never has to infer them from absence (also published by
`overview()["deferred"]`):

* **Phase 22** world model, memory and long-term state reasoning — this layer
  reports what was observed and what changed; it does not model the world.
* **Phase 23** interactive learning and exploration environments.
* **Phase 24** planning, reasoning and action policy — nothing here decides to act.
* **Phase 25** embodied and game agents.
* **Phase 26** generalization, ARC and intelligence evaluation.
* **Semantic object detection** (labels like `person`) does not ship: the fast path
  is text geometry and classical regions, and a semantic label can only come from a
  wired VLM. An ONNX/torch detector was deliberately not added (no new heavy
  dependency, no auto-download).
* **A camera backend** does not ship; the source declares itself unavailable with the
  reason instead of failing deeper.
* **Masks are preview-resolution** and carry no classes; **tracking is spatial**,
  not identity recognition; **abstraction is counting and quoting**, not reasoning;
  **no future-state prediction** is made.

## 28. Compatibility, rollout and verdict

* **Nothing existing changed behaviour.** The vision package, the desktop vision
  controller, the agentic perception engine (`agentcore/perception.py`, a *different*
  layer that fuses UI elements) and every existing route are untouched; the new
  routes were added, not moved.
* **One new status key** (`perception_pipeline`) and **8 new event types** (declared
  in the vocabulary, so a payload that cannot be read is refused at the publisher).
* **Costs are opt-in.** The engine is constructed at boot (cheap: no I/O, no model,
  no capture), and nothing runs until a request arrives; segmentation and VLM
  escalation are off unless asked for (segmentation) or needed and allowed (VLM),
  and the deep path must pass the governor.
* **Contract parity holds**: 210 routes = 210 consumers, `docs/API.md` regenerated
  and `--check` clean, the backend/frontend contract test and the route-shape
  contract test pass, `mypy src` (both platforms) is clean, and the full test suite
  (109 files, six balanced groups) passes.

**Verdict: PASS.**

The phase's deliverable — a reusable perception layer that converts screen, camera,
image and frame-stream input into structured, temporally consistent, abstract
representations with honest confidence and no fake intelligence — is implemented,
wired into the running application, exposed over the API and the capability
registry, and verified by measurement as well as by test. The two capabilities that
this machine cannot exercise end to end are classified exactly that way in the same
table a consumer reads: semantic labels and scene descriptions are
PROVIDER_DEPENDENT on a vision model (none is installed here), and the camera source
is UNAVAILABLE by design (no backend ships). Nothing in the layer claims otherwise.
