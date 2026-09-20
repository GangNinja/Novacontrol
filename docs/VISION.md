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

## Future Adapters

OpenCV, EasyOCR, cloud vision models beyond the registered presets, and
screen/window APIs can implement the `VisionProcessor` interface without
changing the core runtime.
