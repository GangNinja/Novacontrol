# NovaControl Development Log

Work record from the routing-explorer + phone-control checkpoint (`580ae07`,
2026-09-10) through the current tip (`7b17629`, 2026-09-20). Every section is
anchored to the commit that carries it. For feature-level docs see the rest of
`docs/`; this file is the "what changed, when, and why" record.

## Timeline at a glance

| Commit | Date | Intent |
|---|---|---|
| `580ae07` | 09-10 | Routing explorer, live-verified phone control, auto-synced API docs *(checkpoint)* |
| `d741fe7` | 09-12 | Casual-language research, trending daily topics, answer-quality fixes |
| `0aa499c` | 09-13 | Headline-topic research hijack fixes, natural-phrasing math rows |
| `2df5280` | 09-14 | Routing-ladder demo versioned as a docs/tools teaching snapshot |
| `4b9071b` | 09-16 | Real coding agent, server-side chat history, cross-platform CI fixes |
| `9e12d4e` | 09-16 | Focus-ring browser walk hardening (Copilot-contributed) |
| `e38e8ff` | 09-16 | Hardening delta: deadline loop + Job Object zombie reaping |
| `ef3b24e` | 09-17 | Multi-file project coding agent, zombie fix, perf hardening |
| `ddac7d5` | 09-18 | Windows-only process flags guarded for Linux CI — run #7 fully green |
| `8a91efd` | 09-20 | Question-first research pipeline (planner, ranked evidence, synthesis rewrite) and real system telemetry for the Command Center |
| `b0139c7` | 09-20 | CI fixes: POSIX storage invariant, host-dependent PowerShell routing test, mypy `spent` annotation |
| `a378f26` | 09-20 | mypy checks both platform views (linux + win32); one clean browser relaunch on a stuck CDP port |
| `7b17629` | 09-20 | Local Ollama made actually usable: memory-guarded one-model residency, real vision capability gate, configurable provider timeout |

## 1. Brain intelligence: the scratch engine grew real math coverage

All in `src/novacontrol/brain/scratch.py` with its JS mirror in
`web/static/js/routing-explorer.js` kept in lockstep.

- **Compound worded questions**: "what is 2 cubed plus the square root of 9"
  and "15 percent of 200" evaluate as expressions, not single-op lookups.
- **JEE-style phrasing**: compound arithmetic with exponents, roots, and
  percentages composed in one utterance.
- **Fraction words and large scales**: "half of 10", "one quarter of 8",
  "3 million times 2" — cardinals extended through billion/trillion.
- **Spelled-out add form**: "add five and seven" computes 12 like "add 5 and 7"
  (the `_ADD_FORM` grammar now uses the shared `_OPERAND_SEQ` resolver).
- **Natural phrasings**: "what does 3 times 4 equal", "if x is 5", "double 7",
  plus cross-intent boundary variants ("what is the roadmap for the project",
  "compare the roadmap", "explain the milestones") so the plan gate stopped
  stealing "remember this: ..." store phrasing.
- **Drift protection**: `tests/test_math_parity.py` runs a shared corpus
  through both the Python engine and the JS mirror; `test_math_rules_drift_guard.py`
  pins the rule registry shape. Detection drift between brain and explorer UI
  fails CI instead of surfacing as user confusion.

## 2. Explore: casual language, honest answers, live topics

- **Casual phrasing in, good research out**: the query layer rewrites chatty
  requests ("whats the deal with..." / typo'd topics) into research-grade
  queries instead of punting to chat (`d741fe7`).
- **Headline hijack fix**: research questions that merely *contained* trending
  headline words were being hijacked toward news topics; matching tightened so
  the user's actual question wins (`0aa499c`).
- **Trending topics, never hardcoded**: the Explore panel's example chips are
  served from `GET /explore/trending`, which trims real top-story headlines
  (Google News RSS) into clean topics with hourly rotation — no fixed list
  anywhere in the code.
- **Answer quality**: suggestions route back into the research engine so
  clicking a chip always produces an answer with sources, never a dead end.

## 3. The coding agent (Build tab)

`4b9071b` built the single-file agent; `ef3b24e` extended it to projects.

- **Single-file mode**: model drafts code, the sandbox runs it, real stderr
  feeds back into fix rounds (3-round budget), result card shows the trace.
  Falls back to deterministic scaffolds when no model is configured.
- **Plan The Project (multi-file)**: the model returns a JSON file map
  (2–5 files + entry point); the entry runs in the sandbox and failures drive
  fixes to the affected files with whole-project re-runs.
- **Saved Artifacts workspace**: every file renders with Copy/Save; saved
  artifacts list in a strip and **Run** re-executes any saved file through the
  same sandbox. Routes: `GET /build/artifacts`, `POST /build/run`,
  `POST /build/save`.
- **Security subtleties the tests forced**: path sanitization *drops*
  `../evil.py` rather than renaming it to `evil.py`, and SQL workspaces get an
  explicit runtime check instead of relying on exception order.
  Pinned by 16 tests in `tests/test_build_project.py`.

## 4. Chat: server-side history and provider honesty

- **Cross-browser transcript**: chat history moved from per-browser
  localStorage to the server's gitignored data dir (`chat_transcript.json` via
  JsonStateStore) — every tab, browser, and the CLI render the same thread
  (`4b9071b`).
- **Provider telemetry**: the System panel shows token usage and last-error
  info for the active cloud LLM, using the existing `.pill.error` styling
  (`ef3b24e`-adjacent work in `integrations/llm.py` + `render-panels.js`).
- **Known issue surfaced**: a Vertex-format key (`AQ.…`) pasted into the
  Gemini AI Studio preset 404s on every call. The key-format warning at save
  time and the Test Connection button exist so this fails loudly at
  configuration, not silently at chat time.

## 5. Vision and cloud LLM configuration

- **Real multimodal models**: vision element location can use Ollama or a
  cloud provider (OpenAI-compatible preset + Anthropic Claude with its own
  native `/v1/messages` provider class) instead of OCR-only — configured in
  the Vision panel's model card with Test Connection and Clear
  (`029499f`, `4b9071b`).
- **Ollama model picker**: the brain switch chooses *which* local model, not
  just on/off; the picker probe is what the performance audit later hardened.
- **Explore follows brain mode**: research synthesis uses the same
  scratch/cloud/local mode selection as Chat.

## 6. UI/UX: the premium redesign (functionality locked)

- Dark command-center identity kept; typography, spacing, cards, toasts,
  modals, sidebar grouping, and responsive behavior redesigned as a
  frontend-only layer. Zero backend/API/logic changes — verified by the
  route-contract and UI-contract suites.
- **Accessibility contracts with teeth**: every `:focus-visible` rule must
  declare an outline *and* name a class that exists in the DOM (dead rules
  fail CI); reduced-motion handling stops canvas effects live via
  `matchMedia` change events, not just at load.
- **Responsive nav fix**: at tablet widths the nav pill deck overflowed and
  hid destinations; it now wraps, and the UI audit asserts all 11
  destinations are visible at the breakpoint that used to clip them.

## 7. Cross-platform CI

- `.github/workflows/ci.yml`: Test py3.12 + py3.13 on ubuntu-latest, mypy
  typecheck, and `scripts/generate_api_reference.py --check` so `docs/API.md`
  can never drift from the `ROUTE_CONSUMERS` registry.
- **Dead-code hunts**: AST-driven dead-def/dead-param sweeps over api,
  application, integrations, explore, and desktop packages; findings fixed or
  explicitly justified, tests updated where contracts had actually shifted.
- Linux-only failures (path separators, subprocess quirks, browser
  availability) fixed until the **full suite passes on both Windows and
  Linux**; current green: run #7, 4/4 jobs.

## 8. Performance: measured, not guessed

Lab audit (loopback, warm cache, desktop; ResourceTiming + PerformanceObserver):

| Signal | Before | After |
|---|---|---|
| Document wire size | 34 KB | **7 KB** (gzip) |
| styles.css wire | 83 KB | **17 KB** (−80%) |
| app.js wire | 48 KB | **13 KB** (−74%) |
| `/brain/ollama/models` fetch | 2,025 ms **on the event loop** | ~510 ms, off-loop (`asyncio.to_thread`, 0.5 s timeout) |
| Font files | 12 woff2 | 8 (URL cut to weights CSS uses) |
| Scripts | 10 parser-blocking | `defer` + `preconnect` |

The event-loop freeze was the real defect: a dead Ollama stalled SSE and
every route ~2 s per page load. All fixes trace-backed to that measurement;
no speculative changes.

## 9. Test infrastructure: the Edge zombie saga

Three forensic rounds, each caught by evidence rather than guessed:

1. **Launcher handoff** (`e38e8ff`): Edge's launcher process exits 0 within
   ~0.3 s while the real browser keeps serving CDP. The wait loop now treats
   the **debugging port as the only truth** — launcher exit only accelerates
   failure after a grace window — and a browser that dies fast fails with its
   stderr instead of burning the window.
2. **Zombie reaping** (`ef3b24e`): terminating the launcher pid orphaned the
   whole Edge tree (120 leaked processes observed). Fix: a kill-on-close
   **Job Object** — the Extended-limits struct (the basic class fails with
   error 87), assigned via `CREATE_SUSPENDED` → `OpenProcess` →
   `AssignProcessToJobObject` → thread resume, so children can't escape
   before assignment. Post-run zombie count: **0**.
3. **Linux guards** (`ddac7d5`): the suspended-launch flags ran
   unconditionally, but POSIX `Popen` rejects any nonzero `creationflags`
   with `ValueError` — this is what broke CI run #6 on ubuntu (which has
   Chrome, so the walk executes there; WSL skips honestly without a browser
   binary). All Windows-only process machinery now sits behind
   `sys.platform == "win32"`.

## 10. Phone mode: research and the NovaLink plan

- **Today**: phone control rides adb (USB debugging; wireless after pairing),
  with live-verified actions — open apps, in-app actions (YouTube/Google
  search, texting by saved contact name), file/folder opening (`580ae07`).
- **Researched**: an AccessibilityService companion app needs no USB, no
  Developer Options, no adb — and unlocks the semantic UI tree (element
  names instead of OCR). Frictions documented honestly: Android 13+
  restricted-settings flow, OEM battery killers, dual-use API stigma.
  Full sketch: `docs/PHONE_COMPANION_APP.md`. Architecture fits the existing
  `PhoneCommandRunner` Protocol — the companion app is a third runner, not a
  rewrite. **Not implemented yet; plan only.**

## 11. Local Ollama: detection was never the problem, usability was

Wiring a newly pulled local model (`qwen3:8b`) into the project surfaced this:
every layer *reported* the model as the active brain, and not one completion
had ever succeeded. Three independent defects, each invisible from the outside.

**The timeout rejected requests the machine could serve.** `_default_transport`
hardcoded a 30s socket timeout. On this CPU-only box a one-line answer takes
20–60s, so `/ask` burned 90s (two timeouts plus the shaping call) and returned
`Model provider failed: TimeoutError` — while the payload still reported
`provider: "ollama"`, and the provider's own usage counter stayed at 0
requests. The timeout is now `NOVACONTROL_LLM_TIMEOUT` (default 600s); a dead
socket still fails immediately, so the ceiling costs nothing on cloud.

**"Not Echo" was treated as evidence of eyes.** `has_vision_model` and the
runner-side locate gate both accepted any non-Echo provider, so a text-only
chat brain was reported as a vision model. The protocol fails loudly here —
Ollama answers HTTP 400 when `qwen3:8b` is handed an image — but `/vision/status`
claimed vision was available, and a guided click would have used coordinates a
blind model invented. The gate is now `provider_supports_vision`: local models
are checked against Ollama's own `/api/show` capability metadata (`qwen3:8b` →
`completion/tools/thinking`, no `vision`; `qwen3-vl:4b` → includes `vision`),
cloud presets against the registered vision-capable list, and the Echo fallback
refused. Name prefixes remain only as the fallback when metadata is unavailable,
and the prefix list now knows the `qwen3-vl` line.

**Residency is now measured, not accidental.** A chat brain and a vision brain
cannot both be held in RAM here: 5.9 GB + 3.3 GB against a desktop that already
holds ~8.7 GB, with 1.4 GB already swapped at the time of measurement.
`ollama_memory_plan()` measures free memory (reusing the telemetry layer's
platform reads, so "free memory" has one definition in the codebase), reads
resident sizes from `/api/ps`, sizes the incoming model from `/api/tags`, and
returns what must be unloaded — with a 512 MB headroom so "fits" means "fits
and stays usable". Two reasons to evict are deliberately separate: exclusivity
is policy (`NOVACONTROL_OLLAMA_UNLOAD_ON_SWITCH`, default on) and memory forces
an eviction even when the policy is off. Unmeasurable values are `None` — never
a confident "fits" — and an unreachable server evicts nothing.

Two smaller fixes rode along. The chat auto-pick now skips vision models: the
VL model is listed *first* on this instance, and the old auto-pick would have
reassigned the chat brain to it. And keep-alive is applied through the native
`/api/generate`, because `/v1/chat/completions` accepts the field and silently
ignores it — verified against a live server, where the model stayed resident.

Measured after the fixes, on the machine that motivated them: `/ask` returns a
real model answer (3 successful calls, 1725 tokens, no error);
`locate_element("Settings")` lands at a real `llm_vision` point inside the
target button; and the resident roster holds exactly one model across
chat↔vision switches (`['qwen3:8b']` → `['qwen3-vl:4b']` → `['qwen3:8b']`).

The work also exposed two test-isolation gaps: the vision-wiring test read the
developer's real `data/` dir (so merely configuring a vision model broke it),
and the local-model picker test assumed no Ollama was running. Both now hold on
any machine.

## 12. Making local vision fast and honest

The vision layer worked but was slow and brittle in three specific ways, each
found by driving it against the live local model rather than by reading it.

**The image was sent at full resolution.** Every screenshot pixel becomes image
tokens the model must process, and on a CPU-only box that token count is most of
the wall clock. Captures are now downscaled to a longest edge of 1280 px
(`NOVACONTROL_VISION_MAX_IMAGE_SIDE`, `0` disables) and re-encoded losslessly so
UI text stays legible. Coordinates are unaffected by construction: the model
answers on a 0–1000 grid and the answer is mapped onto the **original**
dimensions, read before encoding. Verified: a 1920×1080 probe reached the model
as 768×432 under a 768 budget, and a `{"x": 500, "y": 500}` answer still
returned the original-resolution point.

**Answers were parsed for exactly one shape.** The old parser required
`{"found": true, "x": int, "y": int}` with real numbers, and read *any* other
text through the coarse-region vocabulary — so a model answering in prose
("the top of the window shows nothing") had the word "top" turned into a click
on the top edge of the screen. Answers are now classified as `found` /
`absent` / `unparseable`, stringified numbers and `center`/`cx` pairs and
bounding boxes are all accepted (a box is clicked at its centre), and the region
vocabulary applies only to a bare short phrase. Only an `unparseable` reply
gets one stricter re-ask — a model that already said `{"found": false}` has
answered, and a second call would cost another slow inference to hear it again.

**Nothing capped the reply, and this model never stops thinking.** Requests had
no output budget at all, so a local model generated until the socket timeout.
`qwen3-vl:4b` (4.4B Q4_K_M, 100% CPU) turned that into minutes of nothing:

| request | result |
|---|---|
| no output cap | never returned — killed at 170 s |
| `max_tokens: 80` | 13.7 s, `finish_reason=length`, **empty content**, 332 chars of reasoning |
| `max_tokens: 600` | 109.8 s, all 600 tokens consumed as reasoning, **empty content** |
| `think: false` (native `/api/chat`, and OpenAI-compatible) | ignored — the model's template cannot disable thinking |
| `/no_think` in the prompt | ignored — same 32-token reasoning-only reply |

So the fix is not to persuade the model but to bound it. A locate request now
carries a small budget (`NOVACONTROL_VISION_MAX_TOKENS`, default 256), which
turns a multi-minute stall into a fast explicit failure that falls back to OCR;
the provider records *why* a reply stopped (`answer_was_truncated`, plus
`last_error` naming the exhausted budget) and the locate layer skips the re-ask
when that is the reason — the problem is the model, not the answer's format.
Local completions generally carry a ceiling too
(`NOVACONTROL_OLLAMA_MAX_TOKENS`, default 1024), applied only when the provider
talks to the local Ollama: cloud servers already cap their replies, and a local
ceiling would silently shorten long answers from gpt-4o and friends.

The honest conclusion for this hardware, recorded in the README and
`docs/VISION.md`: prefer a **non-reasoning** vision model (`qwen2.5vl:3b`,
`llava`, `moondream`). On this box the `qwen3-vl` build cannot stop thinking, so
its budget goes to reasoning and the OCR/landmark fallback is what actually
clicks the element — the layer now says so quickly and honestly instead of
stalling for minutes.

## 13. Hybrid NLU: understanding before the model

**The problem.** Every request went to a large local model first. That made a
one-word command cost 20–110s on this CPU-only box, made the answer
non-deterministic where it did not need to be, and spent a 5.5 GB model on
questions the machine could already answer from its own telemetry. The fix was
not a better prompt — it was to put a **lightweight understanding layer in front
of the model** and let the model be the last resort.

**The extension point already existed.** `novacontrol/intelligence/` was the one
place raw language was interpreted (normalization, rule registry, intent
taxonomy, context resolution, semantic fallback). Nothing was rebuilt: the
hybrid layers were added *inside* it, and every subsystem — brain, planner,
desktop runner, phone mode, Explore — keeps consuming the same single
`GlobalInputIntelligence` entry point.

```
normalize → rules → fuzzy (typos) → learned phrasings → context
    → exemplar similarity (TF-IDF) → language model (only if still unresolved)
```

**New in the layer.** A 62-intent taxonomy with a `StructuredIntent` contract
(`goal`, `entities`, `actions`, `confidence`, `requires_llm/vision/web/tools/
confirmation`, `source`, `reasoning_level`, `latency_ms`, and the route
decision with its reason); a pure-Python **TF-IDF + character-ngram** matcher
over 200 exemplar phrasings (no new dependency, no embeddings required);
**modular entity readers** (application, file, folder, project, url/website,
query, level, text, numbers); a strict Pydantic `UserIntent` at the
model/JSON boundary; centralised, **configurable thresholds** (env + config)
with an explicit `fast / verify / llm / vision / clarify` route; the
`ModelManager` lifecycle abstraction over the Ollama primitives; and per-layer
latency telemetry surfaced in Chat and `GET /intelligence`.

**Measured** (deterministic path, no provider attached, 27 phrasings):
mean **0.98 ms**, median **0.15 ms**, p95 **1.44 ms**, worst case 13.9 ms — the
worst case being the *multi-step* request, which is still ~1000× cheaper than
asking a model about it.

**Three defects found by driving the layer rather than reading it**, all of the
same family — a plausible reading that is confidently wrong:

| Input | Was | Now |
|---|---|---|
| `read notes.txt`, `open notes.txt` | *nothing* / an application called "notes.txt" | `read_file` with `file: notes.txt` |
| `find my NovaControl project` | clarify (0.00 confidence) | `find_file` with `project: novacontrol` |
| `where is my NovaControl folder` | clarify — hijacked as a bare "the folder" reference | `find_file`, `folder: novacontrol` |
| `Open VS Code, find my NovaControl project and run the tests.` | 2 of 3 clauses, route `clarify` | 3 planned steps, 0 unresolved, no model |

The root cause of the last two was a reference heuristic that fired on *any*
short phrase ending in a kind word (`file`, `folder`, `site`). It now fires only
when nothing before that word names a target, so "open that file" still resolves
from context while a folder the user just named is read by name. Content files
gained a rule that must precede `open_application` (a text file has no launcher),
and content files, named folders and projects each gained their own rule or
reader rather than a branch inside the engine.

**The remaining cost was outside the layer.** Installing it exposed a defect
*after* understanding: the result was also handed to a model to be re-worded.
`open chrome` was understood in **1.4 ms** and still took **86.1 s**, because the
desktop plan already carried its own finished sentence (`Planned: Open chrome`)
and the shaping step asked the local model to paraphrase it. Every non-status
route paid that tax — the chat handler's answer was even summarized a *second*
time on the way out. A result that already reads as a sentence (a chat answer, a
research report, a measured reading, a planned step list) is now returned
verbatim, and the model words only results that arrive with no sentence of their
own (a bare plan or project record), where the local wording would be generic.
Same request: **86.1 s → 2.2 s**, with the same model still configured. Four
tests pin it by counting provider calls, so a finished result that reaches the
model again fails the suite.

**Understood is not the same as executable.** Nineteen intents the rules resolve
— the file operations (`find_file`, `read_file`, `list_files`, `write_file`, …),
the device controls (`volume_control`, `brightness_control`, `media_control`) and
the code family — have no executor wired, so they fall through to the legacy
handlers exactly as before. That is safe (nothing dispatches a capability the
dispatcher does not know) but it means the reported understanding can name a
capability this build cannot serve, so the gap is written down in a drift guard:
every intent the rules can produce must be either routed or listed as
not-yet-dispatchable, and a stale exemption fails the suite.

**Validation at the tip of this work**: **1014 passed / 12 skipped**, mypy clean
on both the linux and win32 views, `docs/API.md` in sync. The regression that
mattered was caught the same way as before — by running the whole suite, not the
new tests.

## 14. Where things stand

- **Scale**: ~70,000 lines (35.4k Python source across 189 modules, 19.4k tests,
  10k web UI, 3.1k markdown, remainder scripts/config).
- **Validation at the tip**: full suite **1014 passed / 12 skipped** (Windows);
  mypy clean on both the linux and win32 views; `docs/API.md` in sync; CI
  green through run #12 (`316b95a`), the first run after the both-views type
  check landed.
- **History**: all work pushed through `316b95a`; the vision work of section 12
  is committed as `8a203e6` and the hybrid NLU layer of section 13 as
  `d44e0a3`, with this log entry on top.
- **Open threads**: nineteen taxonomy intents (file operations, device
  controls, the code family) are understood but have no executor wired — see
  the drift guard in `tests/test_nlu_engine.py`; stored
  Gemini key is still the Vertex-format one (Settings
  → Test Connection with an `AIza…` key fixes Chat/Coding/Explore in one
  step); local answers take 20–110s on this CPU-only box — the timeout now
  lets them finish, but streaming would make them feel far faster; the local
  vision model on this machine is reasoning-only and should be swapped for a
  non-reasoning VL build; NovaLink M1 (companion transport) is designed and
  ready to build when scheduled.
