# Vision

The Phase 9 vision subsystem defines OCR, screen understanding, window detection, image understanding, and document understanding boundaries.

## Components

- `OcrResult`: extracted text, confidence, and regions
- `DetectedWindow`: window title, bounds, and confidence
- `ScreenUnderstanding`: screen summary, windows, and OCR text
- `ImageObservation`: image summary, labels, and metadata
- `DocumentUnderstanding`: title, summary, sections, and metadata
- `VisionProcessor`: adapter interface for OCR and visual understanding engines
- `BasicScreenUnderstandingProcessor`: dependency-free baseline processor
- `VisionModule`: event-driven runtime module

## Events

- `vision.task_requested`: request OCR, screen understanding, window detection, image understanding, or document understanding
- `vision.task_completed`: emitted with structured vision results

## Vision models (multimodal element location)

The vision layer is not limited to OCR. A configured multimodal model locates
screen elements **semantically**: the screenshot travels inside an
OpenAI-format `image_url` message, the model answers with a JSON point on a
0–1000 grid (resolution-independent), and the runner clicks it. OCR and
geometric UI landmarks remain the automatic fallback, in the priority order
`llm_vision` → `ocr` → `ui_landmark` → `ocr_band`.

### Configuration

- **Surface**: the web UI's Vision panel, or `POST /vision/model` with
  `{provider, api_key, model}`. `DELETE`-style clearing is
  `POST /vision/model/clear`. `GET /vision/status` reports what is wired —
  with the key reduced to a redacted tail, never the key itself.
- **Providers**: a local Ollama instance (auto-detected), or a registered
  vision-capable cloud preset (OpenAI, Gemini, OpenRouter).
- **Hot swap**: `set_vision_llm` / `clear_vision_llm` rewire every consumer at
  runtime — the desktop runner's element locator, the `VisionController`, and
  the agentcore perception engine — with no restart. The choice persists in the
  gitignored `data/vision_llm.json` and is re-applied at boot.

### The capability gate

A provider counts as a vision model only when it can **actually see**:

- a purpose-built vision provider (`vision:*`) is trusted — it was validated
  against the provider's own capability metadata when it was built;
- a local Ollama model is accepted only when Ollama itself reports the
  `vision` capability for that exact model (`POST /api/show`), falling back to
  the known name prefixes (`qwen3-vl`, `qwen2.5vl`, `llava`,
  `llama3.2-vision`, `moondream`, …) when metadata is unavailable;
- a cloud preset is accepted when it is registered vision-capable;
- everything else — the Echo fallback included — is refused.

This is deliberate: "not Echo" is not evidence of eyes. A text-only chat model
(`qwen3`, `llama3.2`) reports a healthy name and model id, cannot see an image,
and will invent coordinates for a screenshot it never received — Ollama
rejects the same request outright with HTTP 400. The gate is what keeps
`/vision/status` honest on a machine whose chat brain is text-only.

### Local model lifecycle (one model resident at a time)

A local chat brain and a local vision brain cannot both be held in RAM on a
small machine: an 8B chat model is ~5.9 GB, a 4B vision model ~3.3 GB, and the
desktop holds several GB more. Before any local completion, the lifecycle layer
measures free system memory (psutil → Win32 `GlobalMemoryStatusEx` →
`/proc/meminfo`) and asks Ollama what it already holds, then unloads whatever
must go — with a 512 MB headroom so "fits" means "fits and stays usable".

Two reasons to evict, reported separately by `ollama_memory_plan()`:

- **exclusive** (default): only one model may be resident at all —
  `NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH=0` relaxes this;
- **memory**: a model that would not fit forces an eviction whatever the
  policy says, so the guarantee survives even a deliberate opt-out.

Every eviction is logged with its measured figures, and an unmeasurable
situation (`available_bytes: None`) is reported as unknown rather than read as
"nothing to worry about".

### Making a local vision call fast and honest

A local vision model is the slowest component in the loop, and three separate
mechanisms keep a call bounded and truthful:

**A token budget on the image.** Every screenshot pixel becomes image tokens the
model must process, and on a CPU-only box that token count *is* most of the
wall clock. Captures are therefore downscaled to a longest edge of 1280 px
(`NOVACONTROL_VISION_MAX_IMAGE_SIDE`, `0` disables) before being sent, and
re-encoded losslessly so UI text stays legible. Coordinates are unaffected: the
model answers on a normalized 0–1000 grid, parsed against the **original**
image dimensions, so a downscaled capture still yields correct
full-resolution click points.

**Tolerant reading of the answer.** The documented contract is
`{"found": true, "x": …, "y": …}`, but models also return stringified
numbers, `center`/`centre`/`cx` pairs, or a bounding box. All accepted shapes
are honoured (a box is clicked at its **centre**, never a corner). Every answer
is classified rather than merely parsed:

- `found` — located, with a pixel point;
- `absent` — the model explicitly reported the element is not visible;
- `unparseable` — no usable JSON and no bare region phrase.

`absent` is deliberately distinct from `unparseable`: only an unparseable reply
gets **one** stricter re-ask (bare JSON, no prose, no code fences). A model that
already said "not visible" has answered, and a second call would cost another
slow inference to hear the same thing. A prose sentence that merely mentions a
direction ("the top of the window shows nothing") is not read as a location —
only a short bare region phrase is, so such an answer cannot click the top edge
of the screen.

**A reply budget.** A locate request needs one small JSON object, so it is sent
with a capped budget (`NOVACONTROL_VISION_MAX_TOKENS`, default 256). This
matters more than it sounds: a local model generates without bound, and a
**reasoning-capable** one can spend its entire budget thinking and never emit an
answer. Measured on `qwen3-vl:4b` (4.4B, Q4_K_M, 100% CPU):

| request | result |
|---|---|
| no output cap | never returned — killed at 170 s |
| `max_tokens: 80` | 13.7 s, `finish_reason=length`, **content empty**, 332 chars of reasoning |
| `max_tokens: 600` | 109.8 s, all 600 tokens consumed as reasoning, **content empty** |
| `think: false` (native, and via OpenAI-compatible) | ignored — this model cannot stop thinking |

The cap turns that from a multi-minute stall into a fast, explicit failure that
falls back to OCR. When a reply is empty *because its budget ran out*, the
provider records it (`answer_was_truncated`, plus `last_error`) and the locate
layer skips the re-ask: the problem is the model, not the answer's format. Local
completions generally carry a ceiling too (`NOVACONTROL_OLLAMA_MAX_TOKENS`,
default 1024), while cloud providers keep their servers' own defaults so long
answers are never silently shortened.

**Practical guidance for a CPU-only machine:** prefer a non-reasoning vision
model (`qwen2.5vl:3b`, `llava`, `moondream`). A `qwen3-vl` build that cannot
stop thinking will consume its budget on reasoning regardless of the prompt, and
the fallback path (OCR, landmarks) is what will actually click the element.

## Future Adapters

OpenCV, EasyOCR, cloud vision models beyond the registered presets, and
screen/window APIs can implement the `VisionProcessor` interface without
changing the core runtime.
