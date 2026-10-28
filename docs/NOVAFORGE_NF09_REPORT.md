# NovaForge NF-09 — vision-model integration and evidence-grounded state interpretation

**Date:** 2026-10-22
**Verdicts:**

| Area | Verdict |
| --- | --- |
| Model integration (detection, selection, capability reporting) | **PASS** |
| Visual interpretation contract and grounding | **PASS WITH LIMITATIONS** |
| Safety integration (cannot act, cannot verify, NF-07/NF-08 preserved) | **PASS** |
| Live model inference | **FAIL — model invoked twice through the real endpoint, no usable answer within the bounded budget** |

**Post-implementation verification pass (2026-10-22):** seven defects were found and
fixed — cache identity, capture/target binding, a stale reason that printed `None`,
a cancellation that could hold the inference slot, a stale observation blamed on the
model in the panel, and a boot-time Ollama probe that ignored
`NOVACONTROL_DISABLE_OLLAMA`. All phase tests and the touched regression groups pass;
the live-inference FAIL is unchanged because the model, not the wiring, is the limit.
See §9.1.

NF-09 fills the one seam NF-08 left open — `ObservationBuilder` accepted a `describe`
step that nothing implemented — without promoting a model's opinion into evidence.
A model can now look at a fresh, target-cropped frame through the **existing** vision
provider, and its answer becomes a typed, versioned value that names the frame, the
model and its own uncertainty. It remains a **claim about pixels**: it cannot plan,
approve or execute anything, it is never an objective verdict, and the bounded loop
does not call it.

The integration is implemented and unit/HTTP-tested. The installed model
(`qwen3-vl:4b`, Ollama, CPU-only) was invoked through the production endpoint with a
real captured frame and **did not produce an interpretation** within two bounded
attempts (194.9 s at a 1200-token reply budget: empty answer; 295.0 s at a
2000-token budget: timeout). A short-prompt probe of the same model read the harness
text and returned JSON in 54.2 s, so the model *can* see; the production prompt and
its reasoning length do not fit the budget on this hardware. That is recorded as a
failed live-inference verdict, not papered over.

---

## 1. Initial repository state

| Fact | Value |
| --- | --- |
| Branch | `main` |
| HEAD | `8d10852` (`feat: give NovaForge a guarded action path and an objective verifier`) |
| Working tree | dirty before this pass: uncommitted NF-07/NF-08 work plus the user's edits; NF-09 adds to it. No reset, stash, clean, commit or push was performed. |
| Python | 3.13.0 (`.venv`, editable install) |
| Node | v24.19.0 |
| Test runner | `pytest` with `PYTHONPATH=src` |
| Installed local models | `qwen3` (text), `qwen3-vl:4b` (3.30 GB, `family: qwen3vl`) |
| Ollama | installed at `%LOCALAPPDATA%\Programs\Ollama`; a session server was started for the live run and stopped afterwards |

The pre-existing NF-07/NF-08 documents were read before changing anything:
`docs/NOVAFORGE_NF08_REPORT.md`, `docs/NOVAFORGE_NF07_REPORT.md`,
`docs/NOVAFORGE_NF07_INTEGRATION_GAP_AUDIT.md`, `docs/STATUS.md`, `docs/API.md`,
`docs/NOVAFORGE_VERIFICATION.md`, and the NF-01…NF-06 reports for the perception,
model-management, safety and evaluation conventions this phase had to preserve.

## 2. Reuse / gap matrix

| Need | Existing facility | Gap | Decision |
| --- | --- | --- | --- |
| Ask a model about an image | `vision/providers.py`: `VisionProvider.see(prompt, image_path)`, `CompletionVisionProvider` over any completion provider | no implementation of the `describe` seam | **reuse the seam**, implement `VisionDescriber` |
| Decide what may see | `provider_supports_vision()` — Ollama capability metadata (`vision`), registered cloud vision presets, `vision:*` wrappers | nothing | reuse; no new registry |
| Choose the model | `vision/manager.py` + the persisted `vision_llm` settings namespace + `config.vision.provider/model` | nothing | reuse; the panel and `/vision/model` already set it |
| Image preparation | `_load_image_base64_for_vision` (Pillow downscale, `NOVACONTROL_VISION_MAX_IMAGE_SIDE`, default 1280) | nothing | reuse, and report the measured consequence |
| Fresh frame of the target | `NovaForgeManager.capture_frame`, capture worker crop, `ObservationBuilder` freshness rules | nothing | reuse; a stale/clipped frame is refused **before** inference |
| Observation value | `GameObservation` (NF-08) with evidence/interpretations/uncertainty | no visual field | **extend** with a `vision` member (optional, typed) |
| Downstream policy access | `ObservationBuilder.describe` | unimplemented | implemented; the loop deliberately leaves it OFF (see §7) |
| Capability reporting | `/novaforge/capabilities` | no vision row | add `vision_interpretation`, computed from the live provider |
| UI | Forge panel, Guardian tab | no way to ask or see an interpretation | add a compact **Describe Frame** section |
| Structured output | `response_format` on the OpenAI-compatible surface | Ollama's native endpoint cannot carry OpenAI image blocks | **stay on the compatible surface**; no second inference path |
| Confidence | none supplied by the model | none | keep `confidence: null`; record a self-assessment only as `reported_confidence` |

## 3. Architecture and interfaces reused

`src/novacontrol/novaforge/visual.py` (new) contains:

* `VisualInterpretation` — frozen, slotted, versioned (`VISUAL_INTERPRETATION_VERSION = 1`)
  and JSON-serialisable. Fields: status, observation id, frame descriptor, target,
  provider, model, duration, description, labels, elements, claimed title,
  `text_visible`, evidence refs, `confidence` (always `None`), `reported_confidence`,
  uncertainty, conflicts, error, reason, `schema_valid`, `truncated`, `cached`,
  `ignored_fields`, and `provenance = "model_interpretation"`.
* `VisualElement` — a bounded claim (`label`, `kind`, optional pixel bounds) from the
  model, never a located control.
* `build_interpretation(answer, frame=…, target=…)` — validates a raw answer into the
  contract. It never raises to an observation in progress; every failure is a status
  plus a reason.
* `VisionDescriber` — the `FrameDescriber` seam. It resolves the provider **per call**
  (`provider=lambda: manager.provider`), so a model configured or swapped after boot
  is used without a restart; `capability()` reports availability, provider, model,
  schema version, timeout, frame-age bound, image-byte bound, preparation note, the
  untrusted-input note and call statistics.
* `vision_timeout_from_environment()` — `NOVACONTROL_NOVAFORGE_VISION_TIMEOUT`,
  bounded to 1–300 s. Invalid or out-of-range values fall back to 60 s.

`src/novacontrol/novaforge/observation.py` gained an optional `vision` member on
`GameObservation`; `ObservationBuilder` passes the describer's typed result through.
Measured evidence (title, OCR words, frame metadata) is untouched by it.

`src/novacontrol/application.py` wires the describer to the **live** vision provider
and reports `vision_interpretation` in `/novaforge/capabilities`, including the exact
reason when unavailable (disabled by configuration, text-only provider, not installed).

`src/novacontrol/api/app.py` adds `POST /novaforge/sessions/{session_id}/vision/interpret`
(read-only). It captures a fresh frame through the session's own capture path, builds
an observation with `describe=True`, and returns the typed interpretation beside the
measured evidence refs. It is registered in `api/models.py` (`ApiSurface`),
`api/route_consumers.py` (`no-render`, the Forge panel module drives it) and
`docs/API.md` (generator check in sync).

### Contract behaviour

| Input | Status | What is reported |
| --- | --- | --- |
| JSON object matching the schema | `interpreted`, `schema_valid: true` | description, labels, elements, claimed title, `text_visible` |
| Prose without JSON | `degraded`, `schema_valid: false` | the prose as a bounded description; labels/elements unavailable and said so |
| Malformed JSON object | `invalid` | the parse error; nothing is guessed |
| Empty answer | `unavailable` | reason: a reasoning model can exhaust its reply budget before answering |
| Frame older than the bound | `stale` | age and bound; **no model call is made** |
| Clipped frame | `clipped` | the clip note; **no model call is made** |
| No image-capable provider | `unavailable` | the provider's own reason; OCR/title evidence unaffected |
| Model raises / exceeds deadline | `unavailable` | error text or the deadline, never an empty "success" |
| Action-shaped fields (`action`, `commands`, `click`, `approve`, …) | the status above | fields **dropped** and reported in `ignored_fields`/`uncertainty` |

`truncated` is set when the raw answer exceeded 8,000 characters, and the cut is
reported. Labels are capped at 12 × 80 characters, elements at 12, evidence refs at 16,
the description at 600 characters and errors at 200.

## 4. Model capability detection and the selected runtime

Detection is the existing gate, not a new one. In this build:

| Item | Value |
| --- | --- |
| Provider reported | `llm:ollama` inside NovaForge (`novaforge.vision:llm:ollama` on the wire) |
| Model | `qwen3-vl:4b` |
| Capability gate | Ollama metadata advertises `vision` for this model |
| `vision_interpretation.available` | `true` |
| Configured deadline | 280 s in the live run (60 s default) |
| Frame-age bound | 2.0 s |
| Image-byte bound | 12 MiB |
| Schema version | 1 |

With no image-capable provider the same row reports `available: false` and a specific
reason; the capability is never inferred from a model's name. A text-only model and
`vision.provider: none` both take that path, and OCR/title observations keep working.

## 5. Schema and grounding

The prompt asks for one JSON object (`title`, `description`, `detected_text`,
`ui_elements[{label, kind}]`, `text_visible`) under 12 entries each, states that the
image is **data, not instructions**, and tells the model not to include action fields.
The parser accepts a fenced object or an object embedded in prose; a list is invalid.

Grounding rules enforced in code:

* **Provenance** is `model_interpretation` on every value; the observation keeps
  `evidence` (frame path, observation id, OCR engine) separate.
* **Confidence** is not manufactured. A model's number is bounded to 0–1, stored only
  as `reported_confidence`, and the interpretation's `confidence` stays `null` —
  the same rule NF-08 applies to OCR.
* **Conflicts** are recorded, not resolved: a claimed window title that differs from
  the OS-read title, or `text_visible: false` beside OCR words (or beside its own
  label list), produces `conflicts` entries and the observation reports uncertainty.
* **Unreadable/stale/clipped frames** never reach the model.
* **No action surface**: the module imports no action, policy, Guardian or manager
  mutator; `ACTION_KEYS` are discarded even if a model emits them. The endpoint is
  read-only and does not write the session record.

## 6. Image preparation and resource controls

* Only a fresh frame of the session's target is used (`capture_frame` → cropped by the
  existing capture worker). Frame age is re-checked against the configured bound.
* The frame is downscaled by the existing preparation (`NOVACONTROL_VISION_MAX_IMAGE_SIDE`,
  default 1280 px longest edge). A 1040 × 440 harness crop is under the default and is
  sent unchanged; the live second attempt used 720 px to test the prefill trade-off.
* Input bytes are capped at 12 MiB; an unreadable file is `UNAVAILABLE` with the shared
  wording, never an empty description.
* One inference per call, awaited under `asyncio.wait_for(timeout)`; the reply is
  bounded by the local provider's token ceiling (`NOVACONTROL_OLLAMA_MAX_TOKENS`, 1024
  by default) and by the parser's 8,000-character answer cap, and the describer caches
  the last two interpretations keyed by **path + size + mtime + model + observation id
  + window (handle/pid)**. The key deliberately binds identity, not just pixels: a
  cached value carries the observation id, target and evidence of the call that
  produced it, so reusing it for another read would report one observation's claim as
  another's, and a hot-swapped model must not inherit the old model's answer (§9.1).
* The loop never queues inference: it does not call the model at all (§7).
* Latency is reported from `time.perf_counter()` as `duration_ms`; GPU/NPU acceleration
  is never claimed (the runtime is CPU).

## 7. Safety integration with NF-08 (and the defect the loop found)

NF-08 behaviour is preserved: stale-observation rejection, target identity, bounded
actions, cancellation, deadlines, action budget, Guardian-gated execution. The
interpretation is an optional member of the observation and changes no policy decision.

**A regression was found and fixed during verification.** Wiring a describable
observation into the closed loop unconditionally cost **24.3 s for a one-action loop**
(12 s description cap plus action + verification), so the NF-08 HTTP acceptance test
with a 20 s deadline returned `stopped — the deadline was reached` instead of
`completed`. That is the correct failure mode, and the wrong design: a bounded control
loop must not pay a model's latency to read evidence it already has. The loop now
builds observations with `describe=False` (frame, title and OCR only) and the
read-only interpret endpoint is the deliberate way to ask a model. The NF-08 loop test
went from a 239 s failure to **3.44 s for the class**, `completed` with one action.

An **empty model answer** used to be classified `degraded` ("answered without a usable
structure"), which claims an answer that does not exist. It is now `unavailable` with
the reason, and the change is pinned by a test. This is exactly the live-run outcome.

A smaller wire-level decision: the structured request stays on the OpenAI-compatible
surface. The local provider's `json_mode` hint selects Ollama's native endpoint, whose
message shape is a text `content` plus an `images` list; the vision wrapper sends
OpenAI content blocks with `image_url`, so using the hint would silently drop the
image. The native structured path was still corrected to send `format: json` (it
claimed structured support without constraining the reply), and Anthropic's provider
now drops the NovaControl-only hint instead of forwarding it.

## 8. Live inference evidence

Server: `uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001`
from a temporary working directory (`/tmp/nc_nf09`), so the user's `data/` was not
touched. Target: `scripts/forge_harness_window.ps1`, the disposable WinForms window.
The session was started through `POST /novaforge/sessions` and its frame captured by
the real manager.

| Fact | Attempt 1 | Attempt 2 |
| --- | --- | --- |
| Endpoint | `POST …/vision/interpret` | `POST …/vision/interpret` |
| Reply budget (`NOVACONTROL_OLLAMA_MAX_TOKENS`) | 1200 | 2000 |
| Vision deadline | 280 s | 295 s |
| Image longest edge | 1040 px (unchanged) | 720 px (downscaled) |
| Frame | fresh, not clipped, 1040 × 440 | fresh, not clipped |
| Prompt tokens | 1235 | not reported (request cancelled at the deadline) |
| Prompt eval | 46.7 s, 26.44 tok/s | — |
| Decode | 1200 tokens, 148.0 s, 8.10 tok/s | — |
| Wall clock | **194.9 s** | **295.0 s** |
| Result | `unavailable` (then classified `degraded`) — **empty answer** | `unavailable` — **timeout** |
| Panel reason | "The vision model returned an empty answer." | "The vision model did not answer within 295 s." |

The model spent the entire reply budget on reasoning and emitted no content. A
**direct** probe of the same model (native `/api/chat`, `think: false`,
`format: json`, 160-token prompt, synthetic 480 × 180 image) returned in **54.2 s**
with 512 decoded tokens, 1690 characters of thinking and the JSON content
`{"description": "The image displays the text \"NC FORGE HARNES - idle\" followed by
\"WAITING FOR INPUT\""}`. So the model can see the harness (it read the text, with a
visible misspelling), but on this CPU it reasons long enough that the production
prompt does not fit a 1200-token budget, and 2000 tokens did not finish inside 295 s.

**Live-inference verdict: FAIL.** The interface was exercised end to end with a real
image-capable model; no interpretation was produced. No claim of model-backed visual
understanding is made on this hardware.

## 9. Tests

New: `tests/test_novaforge_vision.py` (52 tests) covers the contract, schema parsing,
frame/target binding, stale and clipped refusal, timeouts, cancellation/errors,
malformed and contradictory output, missing and text-only providers, empty answers,
excess output, ignored/action fields, capability honesty, cache identity, the HTTP
endpoint (200/404, auth), the panel logic and the preservation of NF-08 behaviour.

Also extended: `tests/test_vision_manager.py` (a structured call stays on the
multimodal surface), `tests/test_integrations_llm.py` (the native structured body is
JSON-constrained and carries `num_predict`; one predicate owns the Ollama disable flag)
and `tests/test_vision_model_config.py` (vision selection never probes a disabled
runtime).

| Check | Result (this verification pass) |
| --- | --- |
| `pytest tests/test_novaforge_vision.py tests/test_novaforge_ui.py` | **169 passed**, 71 subtests |
| `pytest tests/test_novaforge_vision.py` | **52 passed** |
| `pytest` NovaForge core + API + gameplay + UI + vision + loop | **383 passed**, 88 subtests (run as four groups) |
| `pytest` NF-04/05/06 evaluation, benchmark, coach, experiment API, timeline | **251 passed**, 164 subtests |
| `pytest` vision suite (4 files: manager, model config, NovaForge vision, UI) | **292 passed**, 85 subtests |
| `pytest tests/test_web_api.py tests/test_route_shape_contract.py` | **93 passed**, 162 subtests |
| `pytest tests/test_integrations_llm.py tests/test_vision_model_config.py` | **124 passed**, 2 subtests |
| `python -m mypy src` | clean, **432 files** |
| `python -m mypy src --platform win32` | clean, **432 files** |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` |
| `node --check src/novacontrol/web/static/js/novaforge.js` | clean |

The full repository suite was **not** run in one pass; the groups above are the ones
this change touches, and none of them failed. The NF-08 loop regression described in §7
was reproduced (239 s failure) and fixed before the final runs. The audit runs used
`set -o pipefail` so a timeout in the pipeline could not be reported as a pass; one
group did hit its 540 s ceiling before the boot-probe fix in §9.1 and passed in 158 s
afterwards.

### 9.1 Defects found and fixed by the verification pass

| # | Defect | Where | Fix and pin |
| --- | --- | --- | --- |
| 1 | The interpretation cache key covered the frame file, so a **different observation of the same unchanged file** — or a **hot-swapped model** — reused the earlier claim, including its observation id, target and evidence. | `novaforge/visual.py` | The key now binds path, size, mtime, model, observation id and window handle/pid. Pinned by `test_a_cached_answer_is_bound_to_the_observation_it_was_made_for` and `test_a_hot_swapped_model_does_not_reuse_the_old_answer`. |
| 2 | The frame probe identifies the target by handle, but the capture worker crops **by title**. A same-titled window (or a stale crop) could be OCR'd and described as the observed target's state. | `novaforge/observation.py`, `novaforge/manager.py` | The capture reports the handle/pid it cropped; the builder refuses a frame whose handle or pid differs from the probed target, before OCR or any model call. Pinned by `test_a_capture_of_another_window_is_refused_before_ocr_or_a_model`. |
| 3 | A capture marked not fresh **with no usable age** rendered the literal `None s old` in the stale reason, and the freshness test relied on truthiness instead of an explicit numeric bound. | `novaforge/visual.py` | The age is narrowed to a number once; a missing age produces "marked not fresh by the capture, with no usable age". Pinned by `test_a_frame_marked_stale_without_an_age_says_so`. |
| 4 | Cancelling an inference (`asyncio.CancelledError`) unwound past the `finally` that released the single inference slot, leaving the describer permanently busy for the process lifetime. | `novaforge/visual.py` | The lock is released on every exit path, cancellation included; pinned by `test_cancelling_an_inference_releases_the_single_slot` (the next call still runs). |
| 4b | **A second concurrent caller that found the first caller's cached answer under the lock returned without releasing it.** `asyncio.Lock` has no owner and no reentrancy, so after any two simultaneous reads the describer refused **every** later interpretation as "another interpretation is still running" — a deadlock that only appears under concurrency, and the existing coalescing test never made a third call. | `novaforge/visual.py` | The cache-hit path releases the slot explicitly before returning (it sits outside the try/finally that owns the release). Pinned by `test_the_second_concurrent_reader_hands_the_slot_back`, which was mutation-checked: removing the release makes it fail, restoring it makes it pass. |
| 5 | The panel reported **every** missing interpretation as a model problem. A stale observation (no frame) showed the capability reason ("Frames are described by qwen3-vl:4b") instead of the observation's own reason, and a timed-out model still carried the `model interpretation` badge. | `web/static/js/novaforge.js` | A missing interpretation names the observation's status/reason when the observation did not produce a frame; the badge says `no interpretation` unless a description exists. Pinned by the `stale`/`failed` fixtures in `VisionPanelRendererTests`. |
| 6 | Vision model selection probed Ollama on **every** `NovaControlApplication` construction even with `NOVACONTROL_DISABLE_OLLAMA` set: the chat brain took the flag, the vision pipeline did not. On this host each socket attempt cost 1.5 s, making construction 13.8 s and the API test group exceed 9 minutes. | `application.py`, `integrations/llm.py` | One `ollama_disabled()` predicate now serves boot resolution, the lazy re-probe and vision selection (an explicit empty mapping stays authoritative). Construction measured 13.8 s → 2.2 s; `test_web_api.py + route contract` 540 s+ (timed out) → 158 s. Pinned by `test_vision_selection_never_probes_when_ollama_is_disabled` (any probe fails the test) and `test_the_disable_flag_has_one_predicate_with_an_authoritative_mapping`. |

Defect 5 is a reporting bug rather than an inference bug: the measured evidence was
never lost, but the panel's explanation sent an operator to the wrong place. Defect 6
is the opposite of a safety problem — it is the missing half of an existing switch —
and it mattered because it made the phase's own test runs look like a hang.

No NF-08 safety invariant changed: the loop still never calls the model, a model claim
still cannot complete an objective, and `vision.provider: none` still wins over an
installed model.

## 10. UI and API

* `/novaforge/capabilities` gains `vision_interpretation` with availability, provider,
  model, schema version, deadline, frame-age bound, image-byte bound, preparation and
  untrusted-input notes, call statistics and an exact reason when unavailable.
* `POST /novaforge/sessions/{id}/vision/interpret` is read-only and returns
  `{session_id, vision, measured, capability}`.
* The Forge panel's Guardian tab gained a compact **Frame description** section:
  one capability line (model · provider · "a model description is a claim, not
  evidence"), a **Describe Frame** button enabled only for an active session, and an
  interpretation card that separates **model claim** from **measured evidence**, shows
  status, model/inference time/frame age/cache/schema validity, labels the model
  claimed, conflicts, uncertainty, and the standing sentence that a description cannot
  approve, plan, execute or verify. An unavailable model renders its reason and never
  disables the OCR/title workflow. The Phase Roadmap carries an NF-09 row.

**Live UI check:** the page served from the restarted 8001 server rendered the NF-09
roadmap row and, in the Guardian tab, `Visual interpretation: qwen3-vl:4b via
llm:ollama · a model description is a claim, not evidence`, with **Describe Frame**
disabled while no session was active — the truthful state for that moment. The network
log showed no console errors. The button's request path was exercised twice through the
endpoint above, but **the click itself was not re-run live** (a click would have waited
up to 295 s for a known-failing inference); its handler and renderer are pinned by the
DOM tests.

## 11. Performance measurements (this machine)

| Measurement | Value |
| --- | --- |
| Model load + prompt eval (1235 tokens incl. a 1040 × 440 image) | 46.7 s |
| Decode rate | 8.10 tok/s (CPU, `qwen3-vl:4b`) |
| Attempt 1 wall clock (1200 tokens, empty answer) | 194.9 s |
| Attempt 2 wall clock (2000-token budget) | 295.0 s, timed out |
| Short-prompt native probe (512 tokens) | 54.2 s |
| Loop with a description wired (one action) | 24.3 s (12 s cap + action/verify) |
| Loop without a description (NF-08 acceptance class) | 3.44 s |
| Capability read | no model call; provider resolution only |

## 12. Known limitations and untested cases

1. **Live inference fails on this hardware/model.** `qwen3-vl:4b` on CPU reasons past
   the reply budget; not a wiring failure, but no interpretation was produced.
2. **The default 60 s deadline is too short for this model.** The capability reports the
   configured value; an operator must raise `NOVACONTROL_NOVAFORGE_VISION_TIMEOUT` to
   even attempt it (max 300 s).
3. **The production prompt was not optimised to shorten reasoning.** A shorter prompt
   may fit; that tuning was not done, and changing the shared prompt for one model was
   judged out of scope.
4. **A model description is not verified.** Conflicts are detected only for the title
   and `text_visible` vs OCR; a wrong description that contradicts nothing measurable is
   reported as a claim.
5. **No temporal/visual-diff reasoning.** "Changes" in observations remain OCR/title
   text diffs, not pixel comparisons.
6. **Spatial bounds are unverified claims.** If the model returns bounds they are kept
   as claims; nothing checks them against pixels.
7. **One frame per call**, no multi-image or video understanding; the prompt is capped
   at 12 labels and 12 elements.
8. **The UI click path was not exercised live this pass**; the backend endpoint was, and
   the DOM renderer/handler tests cover the panel.
9. **Cloud vision providers** are reachable through the same gate but were not exercised
   live (no credentials used, no paid request made).
10. **Non-Windows hosts** report no capture and therefore no interpretation; the reason
    is explicit.
11. **`NOVACONTROL_DISABLE_OLLAMA` now disables local vision *auto-selection* too.**
    That is the point of the switch (no probing), and it is what makes test runs
    deterministic on a host with Ollama. An explicitly configured `vision.model` or a
    model installed in the Vision panel still reaches Ollama: an explicit choice is a
    request, not a detection.
12. **The verification pass did not re-run the live UI click or the live model call.**
    The renderer changes are pinned by the Node DOM harness and the backend by HTTP
    tests; the live-inference verdict from §8 stands unchanged.

## 13. Files

* `src/novacontrol/novaforge/visual.py` (new) — typed interpretation, parser, describer
* `src/novacontrol/novaforge/observation.py` — `GameObservation.vision`
* `src/novacontrol/novaforge/manager.py` — frame/observation access used by the endpoint
* `src/novacontrol/novaforge/__init__.py` — public exports
* `src/novacontrol/application.py` — describer wiring, capability, `describe=False` loop
* `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py`, `docs/API.md`
* `src/novacontrol/vision/providers.py` — structured-call surface note
* `src/novacontrol/integrations/llm.py` — native structured body + hint hygiene
* `src/novacontrol/web/static/index.html`, `static/js/novaforge.js`, `static/nc-os.css`
* `tests/test_novaforge_vision.py` (new), `tests/test_vision_manager.py`,
  `tests/test_integrations_llm.py`, `tests/test_vision_model_config.py`,
  `tests/test_novaforge_ui.py`
