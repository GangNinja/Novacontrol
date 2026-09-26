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

---

# Phase 6: the vision pipeline

*The specification series whose Phase 3 is the decision engine, Phase 4 the
planner and Phase 5 tool selection names vision as its Phase 6 — see
[DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) section 17. It is not the older
roadmap's "Phase 6: Planning" in [PHASES.md](PHASES.md).*

Vision is its own capability, not a mode of the text model. The primary
`qwen3`-class chat model is **never** handed an image: a separate manager,
provider and result type do the looking.

```
Screenshot / image
  -> VisionManager        capture, read, decide, structure
  -> VisionProvider       the VLM, replaceable, configuration-chosen
  -> VisionResult         structured, bounded, provenance attached
  -> Planner              steps are compiled against what was seen
```

## Components

| Piece | Module | What it owns |
| --- | --- | --- |
| `VisionRequest` | `vision/models.py` | one question about one image: source, question, task, `prefer_ocr`, `allow_vlm`, `target` |
| `VisionResult` | `vision/models.py` | the documented structured shape plus `answered` / `escalated` / `metadata` provenance |
| `VisionManager` | `vision/manager.py` | the OCR-first decision and the structuring; provider and OCR engine injected, both hot-swappable |
| `VisionProvider` | `vision/providers.py` | the seam: `name`, `model`, `available`, `see(prompt, image_path)` |
| `NullVisionProvider` | `vision/providers.py` | the honest default — `available = False`, no invented description |
| `CompletionVisionProvider` | `vision/providers.py` | wraps any completion provider (local Ollama VLM, cloud preset, test double) |
| `OcrEngine` | `vision/ocr.py` | the cheap half: `TextFileOcrEngine` then the existing Windows OCR, with real coordinates |
| `VisionActionProposal` | `vision/proposals.py` | what *could* be done about what was seen — nothing executes |

The provider is chosen by configuration (`vision.provider` / `vision.model`, and
`NOVACONTROL_VISION_*` overrides), so Qwen3-VL, Gemma vision, another local VLM
or a cloud endpoint is a wiring change. The existing capability gate
(`provider_supports_vision`) still applies: a text-only chat model resolves to
`NullVisionProvider` instead of being trusted to invent coordinates.

## Routing

The NLU/decision layers set `requires_vision` — and route to `vision` — for an
attached image, a supplied screenshot, or a request that explicitly needs visual
understanding:

| Request | `requires_vision` | route | task |
| --- | --- | --- | --- |
| "What is this error?" | true | `vision` | `UNDERSTAND` |
| "Where is the login button?" | true | `vision` | `LOCATE` |
| "Read the text in this screenshot." | true | `vision` | `OCR` |
| "Open Chrome." | false | `direct_tool` | — |
| "Open Chrome." **with an image attached** | true | `vision` | `UNDERSTAND` |

"Fix this error" stays a coding request: it is about the error, not about a
picture of it.

An attached picture is a FACT, not an inference. `POST /ask` takes an optional
`image` path, `handle_request(..., image=...)` passes it on, and the
understanding layer is told `has_image=True` rather than left to read the
wording — so *"what is this?"* with a picture needs eyes while the same words
without one stay the plain question they are. The pipeline then reads THAT image
instead of capturing the desktop, because a screenshot would answer about the
wrong pixels when the user attached the one they meant.

`requires_vision` can only ever be ADDED by these rules, never removed: a caller
cannot phrase its way out of needing eyes.

## OCR before VLM

Every request is read before it is interpreted. `task_for_question()` picks the
cheap path from the words, and the manager only escalates when the text cannot
answer:

| Question | Task | Path |
| --- | --- | --- |
| "Read the text in this screenshot." | `OCR` | OCR only — the model is never called |
| "What is this error?" | `UNDERSTAND` | OCR if the text covers the question's content words, else the model |
| "Where is the login button?" | `LOCATE` | OCR when the read words carry POSITIONS (the label's coordinates are the answer); the model otherwise |

`text_answers_question()` is the gate for an understand question, and
`question_coverage()` is the confidence it reports — a measured fraction of the
question's content words the text actually contains, not a band someone chose. A
text-only request (`allow_vlm=False`) answers from OCR or says it needs eyes; it
never guesses.

A **locate** question is answered from the text only when the text carries a
position: `_locate_from_words()` looks for the label's own content words (control
nouns like "button" are dropped, longest word first) among the words the reader
PLACED, and a match with no geometry is no match — a plain text file knows its
words and not where they were drawn, so answering from it would return the
origin as a click target. The region it returns quotes the reader's own
coordinates (bounds *and* centre), and the confidence is what finding a word is
worth (0.5), never a figure the pipeline did not earn.

## The structured result

```json
{
  "image_type": "application_screenshot",
  "detected_text": ["..."],
  "ui_elements": [{"label": "Login", "kind": "button", "bounds": {"x": 0, "y": 0, "width": 0, "height": 0}}],
  "errors": ["..."],
  "relevant_regions": [{"label": "...", "x": 0, "y": 0}],
  "summary": "...",
  "confidence": 0.91,
  "metadata": {"answer_source": "ocr", "escalated": false, "question_answered": true}
}
```

The model's reasoning is never part of this: `metadata` carries *provenance*
(which reader answered, whether the model was consulted, whether the question was
actually answered) and nothing else. `answered` is the honest bit — no model
wired, an unreadable image, or a question that needs eyes produces a result that
says so rather than a summary that reads like an answer. It travels at the top
level of the `/ask` payload as well as in `metadata`, because a client should
not have to dig to see that nothing answered the question.

Two rules keep the summary and the confidence honest:

* **The locate contract's JSON is a wire format, not a sentence.** When a model
  answers in the contract without a `summary`, the pipeline builds one from what
  was determined — *"Login is visible at (500, 250) on a 0-1000 grid."*, *"No
  logout button is visible in the image."*, *"The element was reported as
  visible, but no position was given."* A person is never shown the raw object.
* **A confidence is either measured or named as unquantified.** A figure the
  model reports is used as given (`confidence_basis: model-reported`); an
  answered-but-unquantified answer quotes one named placeholder with
  `confidence_basis: none`; a "not visible" answer is `0.0`, because there is no
  position to be confident about and a middle figure beside an empty region list
  reads as a located element.

## Computer use (designed, not enabled)

```
screenshot -> understand UI -> identify target -> action proposal
  -> permission/safety -> click/type -> screenshot again -> verify
```

Steps 1 and 6–8 exist and are approval-gated; steps 2–3 are this manager; step 5
is the approval gateway every side-effecting action already passes through. Only
step 4 is new, and `vision/proposals.py` stops there: `VisionActionProposal`
carries `requires_approval = True` as a constant (not a field a caller can flip),
and there is deliberately no function that clicks. A proposal is only produced
when the target was seen WITH geometry to act on — a model that says "probably
top-right" without a point produces no proposal at all.

## Future Adapters

OpenCV, EasyOCR, cloud vision models beyond the registered presets, and
screen/window APIs can implement the `VisionProcessor` interface without
changing the core runtime. A new VLM needs only the `VisionProvider` protocol —
one method — and a config entry.
