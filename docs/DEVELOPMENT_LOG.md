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

## 14. Phase 2: making the lightweight layer production-ready

Phase 1 proved a request could be understood without a model. Phase 2 was about
making that understanding *reliable, inspectable and cheap enough to trust* —
and about the one thing Phase 1's verification had left broken.

**The fallback was returning nothing, and we knew why.** The Phase 1 audit ran
the real slow-path request through the real provider and got **143 s, then 115 s,
and zero characters of answer** — the model spent its entire reply budget
thinking. The fix was to match the endpoint to the capability rather than trying
to flag a workaround: `/v1/chat/completions` *ignores* `think: false` (OpenAI's
shape has no place for it), but Ollama's own `/api/chat` honours it. Structured
local calls now take that path. Measured on this CPU-only machine with the
project's real understanding prompt: **118.2 s returning nothing → 13.4 s
returning valid JSON**, which parsed into `recall` with `project: NovaControl`.
Chat is deliberately left on the compatible surface — it wants prose, and a
model's reasoning costs latency it does not need to pay.

**Everything else in the phase was about not guessing.** A normalization
pipeline collapses politeness, contractions, spelling variants and product
aliases (*"hey, can you bring up Google Chrome for me?"* and *"please launch
chrome"* reach the same reading) while proving it never eats an entity —
`open report-final.pdf` still yields `file: report-final.pdf`, hyphen and dot
intact. Confidence stopped being a similarity score wearing a label and became a
sum: match strength, entity completeness against the catalog's declared
requirements, how close the runner-up came, whether a reference was resolved,
and what the request needs next. Ambiguity *lowers* a reading and a missing
entity lowers it without zeroing it, which is what makes *"open chrome"* land at
0.90 and *"open it"* at 0.74 without a special case for either.

**A table, not three modules.** Intent definitions moved into one declarative
catalog: description, examples drawn from the exemplar corpus, required and
optional entities, tools, handler, confirmation, web, vision, confidence floor,
execution category. Adding an intent is now a table entry, and two modules
cannot silently disagree about what an intent means. The catalog also made an
uncomfortable question answerable — *which* intents actually dispatch — which is
how the nineteen unrouted ones became a written-down gap instead of a surprise.

**Complexity, counted rather than measured in words.** `SIMPLE` / `MODERATE` /
`COMPLEX` is decided from actions, dependencies between them, unresolved
references, ambiguity and reasoning words. "Open Chrome" is local; "Open Chrome
and search YouTube for Python tutorials" needs the planner; "Find the project I
was working on yesterday, inspect the latest changes, run the tests, identify
failures and explain what to fix" is the one case that reaches the model because
of its *shape* rather than its confidence.

**One test I had to correct rather than satisfy.** A new guard asserted that
every intent's confidence floor sits below its own rule's reading — the point
being that a deterministic match should never be silently demoted off the fast
path. It failed on eight intents. Investigating showed the guard was wrong, not
the catalog: its lookup took the *last* rule for an intent instead of the
*strongest* (which is what the registry derives from), and the remaining seven
cases were floors declared **deliberately** on actions that close, delete, spend
or type, where paying for a second look is the whole point. The test now asserts
the honest invariant — a floor above the rule's reading must be one of those
deliberate cases, enumerated so a new accidental floor fails and silently
removing a deliberate one fails too.

**Measured, not estimated.** Phase 2 adds the timing breakdown a local model
reports about its own work — weights load, time to first token, decode rate,
true inference total — converted from Ollama's nanoseconds and recorded per
escalated request. A cloud provider reports none of that, and now *says* it
reported none: the aggregate is empty rather than a row of fabricated zeroes.

**What the measurements actually say.** Deterministic understanding across the
fast-path phrasings and their paraphrases (`Open Chrome.`, `Close Spotify.`,
`Take a screenshot.`, `Open VS Code.`, `Turn volume to 50%`, `launch`/`start`/
`bring up chrome`, `hey, can you open chrome for me?`, `check my memory`, `how
much RAM am I using?`, `what programs are using my memory?`): **0.13–0.41 ms**,
median 0.26 ms, every one on `fast_path` with no model — against a target of
sub-100 ms. End-to-end through the real application, including the live
`system_monitor` read and the response shaping: **137 ms** for *"What's my RAM
usage?"* ("You're currently using 12.9 GB of 15.4 GB RAM (83.8%).") and **100 ms**
for *"What's my battery percentage?"* — both returned as finished sentences with
no model call.

**One measurement deliberately not taken.** Re-running the live Qwen3 fallback
would mean loading an 8B model against **2.7 GiB** of free memory on a 16 GB
machine — enough to make the whole desktop thrash for minutes. No model was
resident (`/api/ps` empty), so there was nothing to unload and nothing safe to
load, and the decision was left to the user rather than taken at the machine's
expense. The verified numbers from the fix above stand.

**Validation at the tip of this work**: **1097 passed / 12 skipped**, mypy clean
on both the linux and win32 views, `docs/API.md` in sync, the full suite run
rather than the new tests alone.

## 15. Phase 1's own bugs, and the embedding layer Phase 1 asked for

An audit against the original specification — run the spec's own examples
rather than re-reading the code — turned up two defects in behaviour shipped in
section 13, one requirement that had been met only in prose, and one honest
refusal.

**A hand-built reading skipped the step that describes it.** *"Open Chrome and
search YouTube for the latest AI news"* is detected as ONE browser task before
the clause splitter can turn it into an app launch plus a stray search. But the
composite path assembled its `StructuredIntent` directly and returned it, so it
never passed through the post-processing that derives each reading's requirement
flags from the catalog. It reported **`requires_web=False` for a web task** — the
planner and the approval layer would have judged it by a different description
of the same work than the catalog gives. Both the sync and async paths now
post-process every reading, and its goal is rebuilt from the parts read
(`verb + site + query`) instead of reporting the dangling *"for the latest ai
news"* a greedy split left behind.

**A project was being launched as a program.** *"Open my NovaControl project"*
matched the generic *"open ___"* rule, which read the whole phrase as an
application called *novacontrol project*. A project is named in prose, never by
extension, so it now has its own rule and reads as `project: novacontrol`; the
entity is mirrored to `folder` because a project IS a folder and that is what
the capability opens. The companion fix was on the other side of the same
coin: adding a *project* rule immediately broke *"show me that project"* into a
project named *"show me that"* — a kind word preceded only by neutral words is a
**reference**, so `project` joined the reference table and resolves from context
(or asks) instead of inventing a name.

**The same mistake was one layer down, and only end-to-end checking found it.**
The NLU reading was right and the plan was still wrong: `plan_desktop_command`
re-parses the raw request in the desktop controller, so with the intent and its
`folder` entity resolved correctly, the planner still produced
`open_application` targeting *"my novacontrol project"* — a program that does
not exist. Unit tests could not see it (the NLU layer was correct); running the
spec's own example through the HTTP API could. The controller now has a project
pattern next to its other prose-folder patterns, with a lookahead so the bare
*"open my project"* stays a reference rather than a folder named *my*. The
request now plans `Open folder novacontrol`, resolved on disk like any other
spoken folder name.

**Six specification names had no machine-readable equivalent.** The taxonomy's
docstring mapped `launch_website → navigate`, `create_file → write_file`,
`execute_command → run_command`, `screenshot → take_screenshot`, `explain →
answer_question` and `unknown → clarify`, but prose is not a mapping a caller can
use. They are now an exported `INTENT_ALIASES` table plus `resolve_intent()`,
which resolves either vocabulary and returns `None` for a name this system does
not have. A second intent per synonym would have routed twice to one handler; an
alias is the same name spelled differently, so it resolves.

**The embedding layer Phase 1 asked for did not exist**, and the honest question
was whether it *could* be built without a model. It can, and the interesting
part of the work was deciding what it is allowed to decide. The default vector
space is deterministic and dependency-free: content-word, bigram and character
n-gram features, hashed with blake2b (not `hash()`, which is salted per process)
and weighted by inverse document frequency. Two findings shaped it, both from
running a leave-one-out benchmark over the whole exemplar corpus rather than
trusting intuition:

- **Function words were dominating the vectors.** With them in, *"how much ram
do i have"* matched **battery_status** above memory_status — *"how much … do i
have left"* is almost entirely function words. Dropping them raised precision at
the acting threshold from 23% to 62% on the same sweep.
- **A raw similarity is not a decision.** *"Show me the florb"* scores 0.61
against *"show me the documents folder"* because it shares that phrase's shape,
and sits within 0.04 of a different intent. Trusting similarity would have let
nonsense select a tool — and worse, inside a multi-clause request it would have
silently *resolved* the unresolvable clause, deleting the "I did not understand
this part" signal the multi-step path depends on (a pre-existing test caught
exactly that within minutes of the layer being wired in). The gate is therefore
on the **calibrated** confidence after the multi-signal arithmetic: measured
precision is 41% at 0.50, 48% at 0.55, **62% at 0.60 (8% of phrases)**, 67% at
0.70, 80% at 0.80, and the default floor is the knee. That default is a config
value, and the number describes the local hashing space — a deployment with a
real embedding backend should lower it against its own measurement.

One arithmetic bug was worth the whole exercise. An embedding similarity was
being passed through the **lexical** slot of the confidence blend, which weights
it at 35% — so a genuine paraphrase calibrated to ~0.22 and nonsense to ~0.16,
i.e. the layer could never act and the two were nearly indistinguishable. An
embedding match is not a hint *about* a deterministic match; it IS the evidence,
so it now enters at full weight in its own signal (and a rule, when present,
still carries a 65/35 blend — a feel-alike never overrules a real match).

What the layer deliberately does **not** do: it is not taught as a learned
variation (turning a similarity into a deterministic rule is a stronger claim
than the evidence supports), it never reports itself as the language model (its
source is `embedding`; `semantic` in this system means the model), and it is not
consulted at all when the request is a bare reference or names a technical
token. Per-layer costs (`rules`, `lexical`, `semantic`, `model`) now travel with
each reading under `layer_ms`, so a slow request can be attributed to a step
instead of to the pipeline.

**Two existing tests had to be corrected rather than satisfied**, and both were
wrong in instructive ways. My new invariant *"no catalog floor exceeds its
rule's own confidence"* failed on eight intents — because the guard took the
*last* matching rule instead of the *strongest* (which is what the registry
derives from), and because the remaining cases are floors declared
**deliberately** on actions that close, delete, spend or type. The guard now
asserts the honest invariant with those cases enumerated, so a new accidental
floor still fails. And `test_lexical_matching_can_be_disabled` asserted that
turning off TF-IDF makes an exemplar-only phrasing unreadable — true until the
embedding index read the same corpus. It now switches off *both* corpus-based
layers and asserts the intermediate state explicitly: with TF-IDF off the
embedding still reads a near-exact exemplar, which is exactly what an exact
match should earn.

**Measured, at the tip of this work** (Windows, CPU-only):

| | |
|---|---|
| Embedding index build (200 exemplar phrases) | **~14 ms**, once, lazily |
| Embedding query, uncached / cached | **0.9–1.5 ms** / **0.02–0.05 ms** |
| Leave-one-out precision at the default floor | **62%** at 8% coverage (TF-IDF layer: 27% at its gate) |
| Fast-path understanding | **0.2–4 ms**, no model |
| `"open my NovaControl project"` | end-to-end **108–127 ms**: `open_folder`, `project: novacontrol`, plan `Open folder novacontrol`, no model |
| `"open chrome and search youtube for X"` | `browser_action`, `requires_web=True`, goal rebuilt |

**Validation at the tip of this work**: **1134 passed / 12 skipped**, mypy
clean on both the linux and win32 views, `docs/API.md` in sync, `ruff` clean on
the new modules and test files (the repo's pre-existing lint findings are
untouched, and one import-sorting finding was removed rather than added). One pre-existing timing test
(`test_telemetry.py::test_polling_is_cheap`, a 1.0 s budget for ten polls)
measured 1.12 s under a loaded full-suite run and passes in isolation and on the
next full run — load-sensitive, not caused by these changes, but it is a budget
worth loosening.

## 16. Phase 2, verified against its own specification

Phase 2 was implemented and then reported as done. This section is what came of
re-deriving all eighteen of its requirements from the code rather than from the
report, running the spec's own example sentences through the live engine, and
fixing everything that disagreed. Six real defects fell out — five of them
silent, none of them caught by the suite that shipped with the work.

**What held up under checking.** The normalization pipeline collapses
politeness, contractions, synonyms and product aliases without eating a
filename (`"open report-final.pdf"` keeps `report-final.pdf`). The catalog
carries every field the spec names (description, examples, required and
optional entities, tools, confirmation, web, vision, confidence floor,
execution category). The lexical matcher returns the agreed contract —
`candidate_intent`, `score`, `matched_examples`. Thirteen entity readers cover
the spec's thirteen types. Confidence combines all six factors the spec lists.
Strict validation does repair → stricter retry → controlled failure, and never
lets a malformed reply reach tool execution. Fast-path understanding measured
0.13–0.41 ms across the spec's seven example commands with no model call; the
provider records load, first-token, decode and true inference timings; vision
is schema-only with no VLM added, as item 12 requires; no additional model was
installed (item 13); the spec's A–I case list is a test class; the UI readout
shows Mode/NLU/Intent/Confidence/LLM/Latency; and an ununderstood request still
falls through to the legacy brain path (item 17).

**The defect that mattered most: a continuation was answered as a GPU reading.**
*"Continue from where I stopped"* reached the embedding layer, whose nearest
exemplar was `gpu_status` at **0.566 raw**, calibrating to **0.63** — over the
0.60 floor — so a request to resume work was routed as hardware telemetry and
would have been carried out. The floor was not the bug: a raw similarity has no
way to know the phrase names no target at all. The fix is a shape test, not a
threshold (`is_continuation`, narrow on purpose): a *bare* continuation is
declined by the embedding layer and resolved from memory by the context
resolver — replaying the last understanding — or, when nothing is remembered,
left to the model and then to one precise question. The narrowness is the
design: *"continue the project I was working on yesterday"* names something and
still flows through the pipeline as the spec's COMPLEX example.

**The learning loop was teaching phrases that must never become rules.**
*"Do the same thing"* was taught against a file read — and because the learned
layer runs *before* the context resolver, it then replayed that read after an
unrelated browser task (`-> read_file`, expected `browser_action`). One measured
scenario, two requests apart. Nothing is taught now for a phrase carrying a
resolved reference, a bare reference, or a bare continuation; ordinary phrasings
still teach, which a companion test pins down so the guard cannot quietly widen
into "teach nothing".

**A composite reading counted as one action.** *"Open Chrome and search YouTube
for Python tutorials"* — the spec's own MODERATE example — was assessed
`simple` with `needs_planner=False`, because the composite path called
`_finalize` without its step count while every other multi-step path passed one.
The reading already knew its actions (`open_application`, `navigate`,
`search`); nothing asked. It is now `moderate` / `needs_planner=True`, and
*"Open Chrome."* stays `simple`.

**The vision requirement was lost exactly when it mattered.**
*"Describe the image on screen"* has both a vision subject and a vision verb,
but it resolves to nothing the lightweight layers can act on, so it took the
clarification path — which routes *without* deriving requirements. Result:
`requires_vision=False`, route `clarify`. The request was answerable by
*looking*, and the system asked a text question instead. Requirements are now
derived before routing on that path too: `requires_vision=True`, route
`vision`, `reasoning_level=vision` — and the clarification record carries the
route it was routed to, so the aggregate no longer files a vision request under
"question" without saying where it actually went.

**Item 15's success/failure was not recorded anywhere.** The record carried
request id, timestamp, normalized text, intent, confidence, method, model use,
model latency, tool and total latency — and stopped at "was it understood".
Worse, the request id was minted separately from the intent's own id, so nothing
*upstream* could report an outcome against it. The id is now derived from the
intent (`req-<id[:12]>`), travels to the client in the NLU payload, and
`record_outcome()` annotates the same sample after the handler runs — success or
failure, with the exception's type, re-raised unchanged. An outcome nobody
reported stays absent rather than counting as a success.

**Deliberately left alone.** A fast-path reading whose target came from memory
(*"open it"* after opening VS Code) still reports `source="fast_path"` while its
`references` tuple and the routing reason record the context resolution and its
confidence is capped at 0.74. The label describes where the *intent* came from —
the fast path — and the evidence for the target travels beside it, so changing
it would edit a contract nothing is currently misled by.

**Measured after the fixes** (Windows, CPU-only, `NOVACONTROL_NLU_ALLOW_LLM=false`):

| | |
|---|---|
| `"continue from where I stopped"`, no memory | one precise question, no invented intent |
| same, after `"read report.pdf"` | `read_file`, `contextual`, **0.70** |
| `"do the same thing"` after a read, then after a browser task | `read_file` → **`browser_action`** (was `read_file` both times) |
| `"Open Chrome and search YouTube for Python tutorials."` | **moderate**, `needs_planner=True` (was `simple`) |
| `"describe the image on screen"` | `requires_vision=True`, route **vision** (was `False` / clarify) |
| record id vs intent id | identical (`req-<id[:12]>`), outcome recorded against it |

**Validation at the tip of this work**: **1148 passed / 12 skipped**, 14 tests
added for these fixes; mypy clean on both the linux and win32 views; `docs/API.md`
in sync; `ruff` clean on every new module and test file. The repo's ~1.3k
pre-existing lint findings are unchanged (one import-sorting finding removed
earlier in this work, none added here).

**Still true, and worth repeating:** the confidence thresholds are the spec's
defaults rather than values tuned against a corpus of real requests; the
embedding floor is measured against the *local* hashing vector space and must be
re-measured for a real embedding backend; and one honest ambiguity remains —
with no context at all, the single word *"resume"* reads as `media_control` via
the exemplar matcher (a legitimate media command) rather than asking. With any
context present it resumes the remembered task instead.

## 17. Phases 3–7: deciding, planning, selecting, looking, loading

Five layers landed on top of the understanding stack. The rule each one was
built to obey: **it can refuse the next step, and it cannot grant itself one.**

**Phase 3 — the Decision Engine** (`decision/`: models, routing, providers,
engine). A new package between Context/Memory and everything that executes,
answering *what should NovaControl do with this?* It resolves a route
(`direct_tool`, `system_tools`, `local_capability`, `planner`, `agent`,
`local_llm`, `cloud`, `vision`, `chat`, `clarify`), the capability, the model,
the actions and the requirement flags, with a stable `reason_code` (callers
branch on the why, never on prose) and a templated sentence for humans. It
executes nothing and holds no conversation state — the same intent plus the same
environment gives the same decision, which is what makes the fast path testable.

The cheapest-first hierarchy is the whole point: an image request goes to vision
**before** clarification (asking a better question cannot answer a question about
a picture), a system reading is measured rather than modelled, and a model is
reached only when the lightweight layers already declined, the complexity
assessment asked for one, or part of the request was never read at all.

`INTENT_HANDLERS` moved out of the application into `decision/routing.py`,
because "this intent is carried out by that subsystem" **is** a decision rather
than application policy; `NovaControlApplication._GIL_ROUTES` remains as a view
of it, so existing callers keep working against one source of truth.

**Jev as an optional provider, not a dependency.** `build_decision_provider()`
maps configuration onto a provider and returns `None` for the local one — which
is every default install. The external provider is constructed only when an
endpoint *and* consent are configured, posts a **redacted** summary (intent,
action, flags, complexity, environment: no raw or normalized text, no entities,
no history), and every field of the reply is validated against the local
vocabularies. Three rules survived review: an unknown route is **refused**
rather than mapped; the handler is always resolved locally, so a remote service
can suggest a route but never name an executor; and caution is monotone —
`_validate` ORs the intent's own `requires_confirmation` with the provider's
answer, because a provider that could clear it would be holding authorization.
A declining provider is not a failure: it answers locally and reports
`provider_fallback`, while a *switched-off* provider is simply the answer and
reports nothing (otherwise a default install would look like it was recovering
on every request).

**Phase 5 — intelligent tool selection** (`tools/selection.py`). "Which tool
would this reach?" was a single lookup. It is now `ToolSelector`, a pure
function that assembles candidates from three surfaces that can disagree — the
catalog's declared tools in preference order, the capability's executor, and the
live tool registry — and scores them on declared order, registration,
**entity completeness** and risk. Measured on the shipped taxonomy:

```
"Open Chrome."                  desktop_controller  declared 1.00  no confirm
"What's my RAM usage?"          system_monitor      declared 1.00  no confirm
"Delete the file report.pdf"    file_manager        executor 0.75  CONFIRM
"Find my NovaControl project…"  file_manager        executor 0.45  missing: file
```

Two design calls are load-bearing. A missing required entity *multiplies* the
score (0.6) rather than disqualifying the tool, because an incomplete step is
exactly what the approval prompt is for. And a registered tool is offered as a
candidate only when its schema **says** it serves the intent or the catalog
already names it — a registered-but-unrelated name is ignored, because guessing
from a name is how a selector starts running the wrong thing. The selector
executes nothing; `requires_confirmation` can only be raised by it, never
cleared. The engine names the tool on every decision (including one an external
provider produced) and records `tool_source`, `tool_confidence`, `tool_reason`
and the candidate list.

**Phase 4 — the decision reaches the planner.** The planning handler and the
agent loop now receive `request.context["decision"]` beside the structured
intent, and `/plan` payloads carry both the decision and a rebuilt selection, so
a plan path can say which tool a step would reach without re-reading the
sentence. A reading the handler cannot reconstruct yields an **empty** selection
rather than a guessed one.

**Phase 6 — the vision pipeline answers the question asked** (and the text model
is still never handed an image). `understand_screen(source, question=…)` puts the
user's words first and keeps the checklist, because answering "why isn't the
button working" needs the visible error text too. `describe_screen(question=…)`
threads it end to end. What counts as a question is decided before capture: the
NLU goal when present, the raw text otherwise, and nothing for a bare *"look at
the screen"* — where the words are the instruction rather than a question about
the picture. And the honesty rule held: with no vision model wired, the window
probe now returns `question_answered: false` with the reason, instead of a
window-title list presented as an answer to a question about pixels.

**Phase 7 — model + hardware lifecycle.** The `ModelManager` surface
(`is_loaded`, `get_active_model`, `get_available_memory`, `get_model_status`,
`load_model`, `unload_model`, `unload_all`, `ensure_exclusive`) is exposed as
`GET /models/status`, `POST /models/load`, `POST /models/unload`, backed by the
application's own `model_status`/`load_model`/`unload_model`. Exclusivity is the
default on this 16 GB box, the plan carries the measured figures it was decided
from, unmeasurable memory reports `None` rather than zero, and no NPU
acceleration path is claimed that was not measured.

**Verification pass over Phase 3.** The decision engine was then re-checked
against the four examples its own spec names, measured on this box with the
local-model switch off, and three real defects surfaced — each one now fixed
with a test that would have caught it:

- *The second metric was dropped.* *"Check my RAM and CPU"* routed correctly to
  `system_tools` / `system_monitoring`, but the reading carried only
  `memory_status`, so half the question went unanswered. A status reading is now
  expanded by the NLU layer into every metric it named (`_expand_status_metrics`,
  whitelisted against the metric vocabulary before anything is read) and the
  application answers one templated sentence per metric. Measured: *"check my
  RAM and CPU"* → `['memory_status', 'cpu_status']`, 159.8 ms; the single-metric
  *"what's my RAM usage?"* is unchanged at 74.5 ms.
- *Work was answered with a question.* The spec's *"Find my NovaControl project,
  inspect the latest changes and fix the failing tests"* came back as `clarify`:
  the NLU layer asks for confirmation when it cannot confirm a catalogue reading,
  and the decision layer deferred to that. A request that names work — needs a
  model, needs a planner, or has unresolved entities — is now never answered with
  a question; it escalates. Measured: `local_llm`, `requires_planning=True`,
  `qwen3:8b`; with no model configured, the same route with an honest "no model
  is configured" note; *"open it"* still clarifies, because that genuinely is
  ambiguous.
- *A broken context layer cost routing.* The decision engine's context snapshot
  is now defensive — a raising or absent resolver means no context, not a failed
  decision — and context is reported as an input (`context_available`,
  `context_resolved`) without copying its contents into the decision.

**Validation at this point**: full suite **1280 passed / 12 skipped** (1621
subtests, Windows); mypy clean on both the linux and win32 views (199 modules);
`docs/API.md` in sync; ruff clean on every file this work added, with the
pre-existing findings in the touched files unchanged (212 findings in the
modified files, before and after). Four new suites:
`tests/test_decision_engine.py` (62), `tests/test_tool_selection.py` (32),
`tests/test_vision_pipeline.py` (16) and `tests/test_model_lifecycle.py` (22).

## 18. Where things stand

- **Scale**: 52.2k Python source across **217 modules**, 26.8k lines of tests
  across 90 files, 4.7k web UI, remainder markdown/scripts/config.
- **Validation at the tip**: full suite **1574 passed / 12 skipped** (1661
  subtests, Windows); mypy clean on both the linux and win32 views (217 modules);
  `docs/API.md` in sync. CI last went green on `efb309f`, which carries
  everything through section 16; the commit of section 17–20 has not been
  confirmed green from here (the Actions API rate-limited the check).
- **History**: the vision work of section 12 is committed as `8a203e6`, the
  hybrid NLU layer of section 13 as `d44e0a3`, and the log entry covering both as
  `4dc67c2`; the Phase 2 work of sections 14–16 went green in CI as `efb309f`.
  The five layers of section 17 (decision engine, tool selection, planning
  bridge, vision question, model lifecycle), the Phase 4 planner of section 19
  and the Phase 5 tool layer of section 20 — with the `decision/` package they
  live in — land as one commit on `main`, the first state in which the layers
  below the understanding stack are tracked rather than only working-tree.
- **Open threads**: the five layers of section 17, the Phase 4 planner and the
  Phase 5 tool layer reach CI for the first time with that commit; the plan compiler's clause recognition is a
  deterministic pattern set, so a goal phrased outside it lands on a reasoning
  step rather than a wrong action (deliberate, and the reason a real model is
  still the escalation path); the desktop step runner reaches the machine
  through the approval-gated tool executor, and no desktop tool is registered on
  a default install, so a plan that opens an application currently reports
  "nothing can carry this out" rather than opening it — the honest wiring gap,
  not a silent success;
  the Phase 6 vision pipeline has no API route of its own (the manager is
  reached through the request path and `POST /ask`'s optional `image`,
  `/vision/describe` and `/vision/click` remain the panel's endpoints), and its
  provider is resolved at boot and hot-swappable through `set_provider` but has
  not been measured against a real VLM on this box — the local vision model here
  is reasoning-only, so the model branch of the pipeline is proven against fake
  and failing providers rather than against a VL build that answers;
  the tool selector's confidence numbers are earned from declared order,
  registration and entity completeness rather than from measured task outcomes
  (the telemetry now records the selection, so that calibration is possible);
  the Jev provider has an HTTP round-trip test against a local server but no
  live service has ever answered it; the vision question is delivered to the
  model but has not been measured end to end on this box, because the local
  vision model is reasoning-only (see below); the embedding layer's floor (0.60)
  is measured against the *local*
  hashing vector space, so a deployment configuring a real embedding backend
  should re-measure and lower it; nineteen
  taxonomy intents (file operations, device controls, the code family) are
  understood but have no executor wired — see the drift guard in
  `tests/test_nlu_engine.py`; the live Qwen3 fallback has not been re-measured
  since the endpoint fix, because loading 8B against 2.7 GiB free on this box
  would thrash the desktop; the confidence thresholds are still the spec's
  defaults rather than values tuned against a real corpus of requests; stored
  Gemini key is still the Vertex-format one (Settings
  → Test Connection with an `AIza…` key fixes Chat/Coding/Explore in one
  step); local answers take 20–110s on this CPU-only box — the timeout now
  lets them finish, but streaming would make them feel far faster; the local
  vision model on this machine is reasoning-only and should be swapped for a
  non-reasoning VL build; NovaLink M1 (companion transport) is designed and
  ready to build when scheduled.

## 19. Phase 4: the planner, the agent loop, and what verification means

The decision layer answers *what should we do with this request*. Phase 4 is the
layer after it: given that answer and the goal, what are the actual STEPS, in
what order, with which tool, and — the part every automation system eventually
skips — how will each result be CHECKED.

**The plan is a graph, and the spec's example is the test.** *"Open VS Code,
open my NovaControl project, run the tests and tell me what failed"* is not four
steps, because the sentence contains prerequisites nobody said out loud. The
compiler emits exactly the seven the specification names — locate project, open
VS Code, open project, execute tests, collect output, analyse failures,
summarise result — each naming an action, a tool or capability, parameters,
dependencies, an expected result, a verification and a status. `StepEffect`
(READ_ONLY / LOCAL_WRITE / DESTRUCTIVE / EXTERNAL / SYSTEM) is what makes the
safety rules enforceable rather than aspirational: it decides whether a step may
run in parallel, whether its result must be verified, and whether it may ever be
retried. The model REFUSES to let a non-read-only step declare itself
parallel-safe, so "run this concurrently" cannot be opted into by an action that
changes something.

**Execution walks waves.** Everything whose dependencies are satisfied becomes
ready together, and runs concurrently only when every step in the wave is
read-only and none names the same explicit resource. The tool-name rule was the
first attempt and was wrong: one read-only tool (the system monitor) legitimately
serves CPU, RAM and GPU readings at once, while two steps about the same file are
not independent even if both only read it — so the guard is about the thing being
touched. A step that changes something waits for the previous such step (a write
must not overtake a write), a failure blocks everything downstream, and a plan
walked with no executor bound reports `executed: false` rather than a completed
workflow nobody performed.

**Verification is the difference between "it returned" and "it worked".** A step
is COMPLETED only after its check PASSED; a step that ran and could not be
checked is UNVERIFIED, which is neither a failure nor a success, and those steps
are listed in `unverified_steps` and carried into the final answer instead of
being rounded up. Checks read evidence — a file, an artifact, an exit code, the
captured output, an observed change, a named callable — and return INCONCLUSIVE
when they genuinely cannot tell. `EXIT_CODE` learned three readings of `expect`:
an int (a build must exit 0), a collection (a suite may exit 0 or 1), and `None` —
"any code" — because for *"run the tests and tell me what failed"* a non-zero exit
IS the result while still needing to be captured for the analysis.

**Recovery is a decision with a reason, not a hope.** `RecoveryAdvisor`
classifies the failure from its own message and picks RETRY, MODIFY_PARAMETERS,
ESCALATE or STOP. Destructive and external actions are never retried
automatically (a delete that half-succeeded, run again, deletes something else),
a refusal is never retried (only a person can change it), a missing prerequisite
escalates to the model rather than looping, and a rejected parameter set is
REPAIRED rather than repeated — the repair is only applied when it cannot change
meaning (a quoted number becomes a number; an argument the tool explicitly
rejected is dropped; a missing required value is never invented). Retry limits
are configurable, and `RetryPolicy` refuses to be built above a ceiling, so an
unbounded loop is unrepresentable rather than merely unlikely. The repair path
uses the retry BUDGET rather than the "is this kind worth repeating" set, since a
corrected attempt is not the same attempt again.

**The agent loop owns the sequence, and cannot skip the last two phases.**
UNDERSTAND → DECIDE → PLAN → EXECUTE → OBSERVE → VERIFY → CONTINUE/RETRY/
ESCALATE → FINAL RESPONSE, on a bounded cycle count (`max_cycles`). A decision
that says "ask first" ends the run before anything is planned; a step that needs
confirmation runs only when the caller approved that step id — with no approval
channel wired it is DENIED rather than attempted, and a tool refusal is a DENIAL
rather than a failure. Retrying is possible only because a `replan` callable was
given, which is why the loop cannot spin: it needs help to continue. Every phase
is one of the application's own methods (the same decision engine, the same
compiler, the same executor), so the loop has no opinion about tools or models
and the whole thing runs end to end in a test on five fake callables.

**Wiring.** `NovaControlApplication` builds one `PlanCompiler` (tool lookup into
the intent catalogue) and one `WorkflowExecutor` whose step handler is the only
route from a plan to the machine: deterministic actions by name (metric readings,
locate, collect, analyse, summarise) and everything else through the
approval-gated `ToolExecutor`. `POST /plan` returns the compiled plan and its
state; `POST /plan/run` runs the goal through the agent loop with an explicit
`approved` list of step ids and returns the phase trace, the plan, the workflow
and the unverified steps. A named check (`metric_read`: did the reading report a
value, or say why it could not measure one?) makes a read-only plan genuinely
verified rather than merely unchecked.

**Verified against its own requirements, and four defects fixed.** Re-running the
specification's cases against the finished layer found four things, each one now
pinned by a test:

  * **A plan could contain two steps with ONE id.** An id is a slug of a title,
    so a sentence that repeats a clause produces the same id twice — and two
    steps with one id are one step as far as execution is concerned: one outcome
    overwrites the other, and the plan can report COMPLETED while a step it
    listed was never accounted for. `Plan` now refuses to be built with repeated
    ids; a test proves the compiler cannot trip the guard (`"open chrome and
    open chrome"` compiles to `open-application`, `open-application-2`).
  * **A step whose dependency is not in the plan at all was reported as "a
    dependency failed"** — sending the reader to look for a failure that never
    happened. A blocked step now separates the two cases and NAMES the missing
    dependency (`FailureKind.MISSING_DEPENDENCY`), because a dangling dependency
    is a defect in the plan, not a runtime failure.
  * **A plan with nothing failed in it could still report `failed`**, with the
    summary "a step failed". A plan whose steps were never attempted — a dangling
    dependency, or a dependency cycle — is reported as BLOCKED, and the sentence
    says what did not happen (*"The plan did not run: Collect was not attempted:
    it depends on a step that is not in this plan"*) instead of inventing a
    failure. A real failure still reports FAILED, with the failing step
    `failed`, its dependent `blocked`, and the reason named.
  * **The plan layer's loop bounds were constants in the wiring**: two attempts
    per step and two agent-loop cycles, changeable only by editing the
    application. They are configuration now — `planning.max_cycles`,
    `planning.max_step_attempts`, `NOVACONTROL_PLANNING_MAX_CYCLES`,
    `NOVACONTROL_PLANNING_MAX_STEP_ATTEMPTS`, plus a `planning:` section in
    `configs/default.yaml` — read once at construction and handed to the
    compiler, the executor AND the agent loop, because a limit only some paths
    honour is not a limit. Unusable input keeps the default and a value above the
    plan layer's own ceiling is clamped rather than obeyed (a ceiling that can be
    configured away is not a ceiling), with a test pinning the two ceilings to
    each other so they cannot drift.

**Validation**: full suite **1347 passed / 12 skipped** (1628 subtests,
Windows, 5:02); mypy clean on both the linux and win32 views (203 modules);
`docs/API.md` regenerated and in sync (66 routes, the new one declared in
`ApiSurface` *and* in the route-consumer table, which is what the parity tests
enforce); `tests/test_planner_phase4.py` adds **63 tests** covering
the specification's plan step for step, clause recognition, derived dependencies
and parallel groups, concurrent readings, a shared resource refusing
concurrency, writes serialised, blocked dependents, bounded retries (including a
destructive step never retried), every verification method and the
inconclusive-is-not-a-pass rule, parameter repair, the classification table,
denial-without-a-channel, a permission error read as a denial, the agent loop's
phase order, clarification executing nothing, escalation, the cycle bound, and
the application's own planning path end to end, and the configured loop bounds.
`tests/test_config.py` covers the same bounds from the config side (mapping,
environment, clamping, unusable input). Ruff reports **no new findings** in any
file this work touched (the two E501s in `planning/engine.py` are pre-existing
and left alone).

## 20. Phase 5: finding the right tool, and the four rules after it

Phase 4 taught the system to plan with tools. Phase 5 is about the question the
plan silently assumed someone had already answered: *which* tool, out of
seventeen, and how does anything know?

**Every tool is described, once, by everything that knows it.** A tool was a
name, a schema and a callable — enough to run one and not enough to search for
one. Worse, three surfaces each knew a different part of the truth and nothing
joined them: the intent catalogue knew which tool carries out each intent (and
the phrases a person actually says), the capability registry knew the executor
and the risk, and the runtime registry knew what was really registered with
which permission scopes. `tools/metadata.py` adds the missing half —
description, category, capabilities, input and output schema, risk, permissions,
examples, tags, and a caching contract — and `tools/catalog.py` builds ONE
catalogue of seventeen tools from the four sources (declarations included), with
two rules that keep it honest: risk becomes the HIGHEST any source claims, and
`registered` is true only when a callable exists. A capability this build can
dispatch but has not wired up is reported as exactly that rather than hidden or
pretended ready — the same honesty the planner's tool errors already had.

**Discovery replaces a list with an answer.** Handing a model every tool
definition is not just expensive, it is *worse* than expensive: the names
(`system_monitor`, `explore_service`) say almost nothing about what a user
meant, so a model given seventeen of them picks by name recognition.
`tools/discovery.py` ranks the catalogue against the request with the lightweight
machinery the NLU layer already had — no new dependency, embedding support still
optional (a caller with a model passes it as the embedder):

  * **lexical**: the query's content words, weighted by the SQUARE of their
    rarity in the tool corpus, scored as the fraction of the query's meaning a
    tool covers;
  * **semantic**: cosine over hashed word/bigram/character-ngram features, the
    same deterministic offline vector space `intelligence/semantic.py` uses.

Both numbers are reported on every match, so a surprising ranking can be
explained rather than argued about. Measured on the shipped corpus
(`Which programs are consuming most of my memory?` → `system_monitor` **0.73**,
`what is chewing up my ram` → `system_monitor` **0.70** (the paraphrase case the
semantic half exists for), `send a text to alex` → `phone_controller` **0.77**,
`research the brics summit` → `explore_service` **0.77**, `what is 15% of 240` →
`scratch_brain` **0.75**, `take a screenshot and tell me what button is broken` →
`vision_pipeline` **0.45**). The model escalation is shown this shortlist only.

**Four defects the tests found, in the order they appeared:**

  * **Plain IDF ranked the browser above the system monitor** for *"what is
    chewing up my ram"*: the term `up` happens to appear in more tool documents
    than `ram` does, and a linear weight could not say which of the two the
    request was ABOUT. Squaring the rarity fixed the ranking (0.70 vs 0.22) —
    the same reasoning that makes inverse-document-frequency work at all, pushed
    further where the corpus is small and the vocabulary narrow.
  * **The floor was too permissive at 0.18**, which returned *"book me a flight
    to mars"* (0.23 — a browser can search, it cannot book) and *"write a poem
    about the sea"* (0.19, from a tag that happened to overlap) as candidates.
    Calibrated to 0.25, where every request the system can genuinely serve sits
    (0.45+) and those two do not — because *"no tool fits this"* is a real
    answer, and it is what stops a planner reaching for something unrelated just
    to have one.
  * **The normalizer's line cap was not a size cap.** Capping the NUMBER of
    lines retained assumed lines are short; a single 9000-character blob on one
    line (minified output, one-line JSON) left the "bounded" result at **18 kB**.
    `max_line_chars` bounds each retained line, and `truncated` now reports the
    cut even when the line count was fine.
  * **A Python traceback's most informative line was invisible**, and the
    summary it fell back to was the oldest line of the output. `\berror\b` does
    not match `ValueError` or `AssertionError` — there is no word boundary
    inside the name — so the pattern missed exactly what a traceback is for;
    and for a failure with no matched line, the summary was the FIRST line of
    the retained tail (*"line one"* for a three-line failure) rather than the
    last thing the command said. Both fixed, and a command that *succeeded* no
    longer gets an invented error summary from its final output line.

**A call is validated, its result bounded, and its answer reused only when that
is safe.** The pipeline is `ToolCall → schema validation → permission validation
→ cache → execution → normalization → (cache store)`, and the strictness is the
point: an **undeclared argument is refused** rather than passed to the
implementation, `{"command": ""}` is refused because it runs nothing and reports
success, a type name no schema can satisfy fails when the schema is CONSTRUCTED,
and every problem is reported at once. Caching is opt-in per tool *and per
operation*: only a tool may declare its result reusable (`cache_ttl_s`), no
metadata means no caching, `volatile_values` names the readings that change
between calls (a cached CPU percentage is a lie nobody can see, so
`cache_refusal` says why it was measured instead of remembered), failures,
denials and empty results are never stored, and the cache is bounded with LRU
eviction. Normalization maps a ten-thousand-line test run to an exit code, a
one-line summary, the relevant lines and a `lines`/`truncated` count — counting
what it dropped — while leaving a small result exactly as it was.

**Wiring.** `NovaControlApplication` builds the catalogue (intents +
capabilities + registry), the retriever, the cache and a normalizing executor
once, exposes `discover_tools()` / `discovered_tools()` / `tools_status()` (the
last is folded into `status()`), and passes the discovered shortlist into the
model escalation prompt. Nothing new became reachable by accident: discovery
names tools, the executor still runs them, and the approval layer is untouched —
a cached result is only returned for a call that passed the same checks as
before.

**Verified against its requirements, and seven more defects fixed.** Re-reading
the layer against the specification clause by clause found things the first pass
had left nominal:

  * **Both schemas were empty on all seventeen tools.** The specification lists
    an input and an output schema as metadata; the fields existed and carried
    nothing, so a model was offered a tool with no call shape and no idea what
    came back. Input schemas are now DERIVED from the required and optional
    entities the intent catalogue already records for the intents a tool carries
    out (`desktop_controller` expects `application` because `open_application`
    requires one); output schemas are declared contracts, and `system_monitor`'s
    — metric, value, unit, summary — is what the metric path really returns.
  * **The pipeline had no live path.** Validation, caching and normalization
    were each tested in isolation and never exercised by the running system,
    because the runtime registry was empty on a default install. Three read-only
    tools are now registered for real — `machine_facts`, `capabilities` and
    `installed_applications`, backed by the readers this application already owns
    — which makes the stages reachable and makes the specification's own caching
    examples (OS information, hardware information, installed applications,
    system capabilities) actual work. All four named operations are therefore
    covered, and each of the three tools answers from the machine itself —
    `installed_applications` reads the same index `open <app>` resolves against,
    so what it lists can actually be launched. The catalogue now describes the
    **twenty** tools this build ships, every one of them with both schemas.
  * **The first version of `machine_facts` failed on its first call**, with
    `TypeError: 'HardwareTelemetry' object is not callable`: `_hardware_status`
    is a property (the reader is created once so CPU deltas have a baseline),
    and calling it would have built a second reader with no baseline. The
    integration test that runs the tool through the app's executor is what
    caught it.
  * **The cacheable tool was returning live numbers.** `machine_facts` reported
    free disk space and uptime while declaring a ten-minute cache lifetime — a
    cached answer that would be wrong the moment it was reused, and the exact
    failure the specification warns about. It now returns stable facts only
    (capacity, not free space; no uptime), and a test asserts the cached payload
    carries none of `disk_free_bytes`, `uptime_seconds`, `used_bytes` or
    `percent`. Live readings stay with `system_monitor`, which declares them
    volatile.
  * **A request made only of function words matched nothing.** *"What can you
    do?"* has no content words, so both the lexical and the semantic layer saw
    an empty query and every tool scored zero — including the `capabilities`
    tool whose own declared example IS that phrase. When there is nothing to
    weigh, the query is now compared directly against the phrases each tool
    declares (character-trigram overlap), so it lands at 0.67 while *"can you
    help me?"* still returns no tool at all.
  * **A tool whose arguments only the capability registry knew had no call shape
    at all.** `file_manager` executes five file capabilities, each requiring a
    `file`, but the hand-written intent entries for `find_file`, `delete_file`
    and the rest record a required entity and name **no tool** — so the
    intent-first derivation found nothing to derive from and the tool that edits
    files was offered with zero arguments. The same defect as the empty schemas,
    surviving in the one corner the first fix did not reach. An input schema is
    now derived from BOTH surfaces the catalogue already merges: the intents a
    tool carries out and the capabilities it executes. `file_manager` expects a
    `file` (with an optional `folder` for a move), `desktop_controller` gains the
    `level` a volume or brightness change needs, and an invariant test fails
    wherever a capability requires an argument the tool does not offer.
  * **A docstring was duplicated.** `_registry_contributions` carried its
    docstring twice; the second copy was a dead string statement left behind by
    an edit, describing a rule it did not enforce. Removed — and the rule it
    stated is now pinned by a test instead: a registry contribution is neutral
    on `read_only`, so registering a tool can never make a read-only tool look
    like a write.

  One design fault surfaced with them: the registry contribution set
  `read_only=False` as its "unknown" default, and because the merge ANDs that
  claim, a declared read-only tool looked like a write and could never be
  cached. Contributions are now neutral on that field — caching still needs a
  declared `cache_ttl_s`, which no contribution can supply, so nothing became
  cacheable by accident.

  **Two empty fields are meaningful, and are now pinned as invariants.**
  `capabilities` is empty exactly for tools that are no registered capability's
  executor (the introspection tools), and `permissions` is empty for tools that
  touch nothing the approval layer gates; a test fails if either emptiness
  appears where the registry says otherwise.

**Validation**: full suite **1433 passed / 12 skipped** (1628 subtests,
Windows, 3:24); mypy clean on both the linux and win32 views (**209 modules**,
six new); `docs/API.md` in sync (no new routes — the layer is reachable through
the application and `status()`, and a route would need the `ApiSurface` +
route-consumer declarations the parity tests enforce). Two new suites,
`tests/test_tool_discovery.py` (50) and `tests/test_tool_pipeline.py` (36),
covering the metadata surface, catalogue assembly from every source (including a
capability whose risk must win and a registry tool that must be marked
available), the specification's discovery example, paraphrase, filters, the
floor, determinism and result caching, strict validation, normalization limits,
and the cache's four refusals. Ruff is clean on every file this work added.

## 21. Phase 6: vision as its own capability

Phase 6 of the specification series that begins with the decision engine in
section 17 — the series whose Phase 3 is the decision engine, Phase 4 the
planner and Phase 5 tool selection — asks for vision to stop being a mode of the
text model. (This is not the older roadmap's "Phase 6: Planning", which is
documented in [PHASES.md](PHASES.md) and [PLANNING.md](PLANNING.md).) The
specification's architecture is `screenshot → VisionManager → VisionProvider →
structured VisionResult → planner`, and the four named types exist with those
names — but the part worth recording is the two defects the *specification*
caught in the code that already looked finished, because both were cases where
the pipeline was described in a docstring and not actually on the path.

**The manager was built, wired, tested — and not in the pipeline.** `VisionManager`,
`VisionProvider`, the OCR engine and the structured result all existed, the
application constructed the manager and resolved its provider from
configuration, `status()` reported it, and `tests/test_vision_manager.py` drove
it end to end with a fake provider. What no test covered was the question the
architecture diagram actually asks: *does a vision request go through it?* It
did not. `_handle_vision` still called the older `VisionController.describe_screen`,
which understands a screen with its own separate path — so the Phase 6 manager
was a second opinion beside the real one, and every claim about "OCR first, the
model only when needed" was true of a layer nothing reached. The fix is small
and the reason it matters is not: the capture step was extracted from
`describe_screen` as `capture_screen` (one approval-gated screenshot, not two
implementations), and `_handle_vision` now captures through it and hands the
file to the manager, so the handler returns the structured result rather than a
prose summary. The seam each test stubbed moved with it: a pre-existing test
that stubbed `describe_screen` is now stubbed at `capture_screen`, because that
is the boundary that is actually on the path.

**A model that was consulted and failed was recorded as never consulted.** When
the provider raised, `_from_text` built the result with a literal
`escalated=False` and `answer_source="ocr"`. Both are wrong in the same
direction: the model *was* asked and could not answer, so the result credited
the OCR reader with an answer nobody produced and hid that the model had been
tried at all. `escalated` now means "a vision model was consulted" — which is
what a caller needs in order to decide whether retrying is worth anything — and
`answer_source` says who actually answered (`none` when nobody did). A live
probe found this: a locate question against a configured-but-unusable provider
returned `escalated: false`, which read as "we never needed the model" when the
truth was "the model was needed and failed".

**Three smaller honesty fixes, each surfaced by a test or a live probe.** The
question `"what does this screenshot say?"` was not recognised as a text
question (the pattern required *"the"*), so it escalated to a model for a task
OCR answers for free. `TypeError: …` was not treated as a failure line, because
`error` inside a class name has no word boundary — so the canonical screenshot a
person actually asks about ("what is this error?") could be classified from its
filename rather than from its content; exception-class names
(`\w+(?:Error|Exception)`) are now recognised as failure lines. And the last one
is the kind this log keeps finding: `"read the text in this screenshot"`
returned `answered: true` beside `confidence: 0.0`. The confidence came from
question coverage, but the words of a *request* are not claims about the image,
so a perfect extraction scored zero. An extraction that produced text is now a
complete answer to that request (1.0); anything else is still scored by how much
of the question the text really covers, and an unanswered question is still 0.0.

**What the pipeline does now, verified live.** Routing matches the
specification's four examples exactly — *"What is this error?"*, *"Where is the
login button?"* and *"Read the text in this screenshot."* all carry
`requires_vision: true` and route to `vision`, while *"Open Chrome."* does not
route to vision at all. Against a real source, *"what is this error?"* is
answered from the text with `image_type: error_screenshot`, the error lines
extracted, `answer_source: ocr`, `escalated: false` — no model call. The same
source with *"where is the login button?"* takes the LOCATE path, escalates, and
returns `answered: false` with the provider's own reason rather than a
description that reads like an answer. A structured result then travels beside
the plan (`payload["vision"]`), which is the "structured result → planner"
arrow made real: a follow-up is compiled against the error lines and elements
rather than against prose.

**Computer use is designed and not enabled.** `vision/proposals.py` holds the
eighth-step pipeline as data (`COMPUTER_USE_PIPELINE`), and
`VisionActionProposal` carries `requires_approval = True` as a constant rather
than a field — a component that can approve its own action is the one thing this
layer must never be. `propose_click` returns `None` when nothing seen matches the
target *or* when the match carries no geometry, because a click built on an
unlocatable guess is exactly what must not happen; there is deliberately no
function that clicks, and the caller takes the proposal to the existing
approval gate. Nothing in this section executes a desktop action.

**Validation**: full suite **1493 passed / 12 skipped** (1636 subtests, Windows,
3:48); mypy clean on both the linux and win32 views (**213 modules**, four new);
`docs/API.md` in sync (no new routes — the pipeline is reachable through the
application, and a route would need the `ApiSurface` + route-consumer
declarations the parity tests enforce). `tests/test_vision_manager.py` now has
60 tests and `tests/test_vision_pipeline.py` 16, covering the provider boundary
(a text-only provider is refused rather than trusted), the OCR engine chain,
structuring, the OCR-first decision in every branch (text question, coverage
question, locate, escalation, refusal, provider failure), the task read from the
question, the documented result shape, provider hot-swap, routing, config
resolution, and the application's own manager. Ruff is clean on every file this
work touched (`desktop/vision.py`'s three pre-existing long lines are untouched).

## 22. Phase 6 verified against its own specification

The specification was re-read clause by clause against the code, and the layer
that passed its own test suite still had five things to fix — three of them the
same shape as the defect section 21 found: a capability that exists, is
documented, is even unit-tested, and has **nothing on the live path** exercising
it.

**The `has_image` trigger had no caller anywhere in the codebase.** The
specification's first routing trigger is *"an image is attached"*, and the
understanding layer has supported exactly that since before this phase:
`understand(raw, has_image=True)` calls `_apply_image_requirement`, which widens
the requirement, re-derives the route from the widened requirement, and can only
ever ADD vision. A repo-wide search found the parameter declared on three public
methods and passed by **nobody** — the one caller of `understand_async` in the
application passed text alone. So the rule held only for callers that did not
exist, and an attached picture could not need eyes however it was phrased. It is
now wired end to end: `handle_request(text, image=...)`, an optional `image`
field on `POST /ask`, `has_image` supplied to the understanding layer, and the
vision handler reading **that** image instead of capturing the desktop — because
a screenshot would answer about the wrong pixels when the user attached the one
they meant. A picture that cannot be read refuses with the reader's own reason
rather than describing anything.

**A locate question always paid for a model, even when the answer was in the
text.** The specification's OCR-vs-VLM rule is *"can the task be solved from the
extracted text? yes → use the OCR result"*, and the pipeline applied it to
understanding and text questions but hard-wired it OFF for `LOCATE` — with a
comment asserting that text cannot tell you where something is drawn. That is
true of *text* and false of *OCR*: OCR words carry their own coordinates, so a
label the reader placed IS the answer. The comment was describing a plain text
extraction, not the engine the pipeline actually runs. `_locate_from_words()` now
answers from the reader's geometry when the label's own content words (control
nouns like "button" dropped, longest first) appear as placed words, and escalates
otherwise — a match with no geometry is no match, because answering from it would
return the origin as a click target. Confidence is what finding a word is worth
(0.5), the same figure the element list quotes, so the two can never disagree.

**A model's "not found" answer was reported as half-confident, and its summary
was raw JSON.** `{"found": false}` produced `confidence: 0.5` beside an empty
region list — a middle figure next to no located element reads as though one had
been found, and `answered: true` (correct: the model looked and said it was not
there) then carried the contract's own JSON as the sentence a person reads. The
locate contract's object is a wire format, not prose: three cases now build a
real sentence (*"Login is visible at (500, 250) on a 0-1000 grid."*, *"No logout
button is visible in the image."*, *"The element was reported as visible, but no
position was given."*), a "not visible" answer is `0.0`, and a model that answers
without a figure of its own quotes one named placeholder with
`confidence_basis: "none"` instead of a number nobody measured. The code also
contradicted its own comment there ("or nothing" beside a hard-coded 0.5), which
is now one function with the rule written once.

**A refusal claimed the image had not been read while returning its text.** With
no vision model wired, an image whose text WAS extracted produced the reason
*"the image was not read"* — sending the caller a contradiction next to a
non-empty `detected_text`. What did not happen is the PICTURE being looked at, so
that is what it now says: *"no vision model is wired, so the pixels were not
looked at."*

**The honest bit was buried.** `answered` — the field that says whether anything
actually answered the question — existed only inside `VisionResult.metadata` (as
`question_answered`) instead of also riding at the top of the response payload,
so the refusal case was the one a client had to dig for. It is now reported
beside the structured result.

**Verified clauses and the numbers.** Routing matches all four of the
specification's examples with the task each picks — *"What is this error?"* →
`requires_vision: true`, `vision`, `UNDERSTAND`; *"Where is the login button?"* →
`vision`, `LOCATE`; *"Read the text in this screenshot."* → `vision`, `OCR`;
*"Open Chrome."* → no vision, `direct_tool` — and *"Open Chrome."* **with an
image attached** now needs eyes too. The pipeline is unchanged in what it
guarantees: OCR first (`answer_source: ocr`, `escalated: false`, no model call
for an error question against a real source), the model only when the text cannot
answer, a structured result with no hidden reasoning, a provider that is a
configuration-chosen plug, and computer use that stops at a proposal nothing
executes.

**Validation**: full suite **1503 passed / 12 skipped** (1642 subtests, Windows,
3:47); mypy clean on both the linux and win32 views (**213 modules**);
`docs/API.md` regenerated and in sync (the `/ask` description now names the
optional `image`, which is a one-line change to the generated reference);
`tests/test_vision_manager.py` is now 70 tests and `tests/test_vision_pipeline.py`
16, covering the attached-image path at the application level (the pipeline reads
the attachment, the desktop is not captured, the requirement is the fact rather
than the wording), the OCR-locate decision in both directions (a placed label
answers and never calls the provider; an unplaced or absent one escalates), the
contract's sentences, the confidence rules, and that `vision.provider: none`
refuses a vision model on the live path rather than only in the config file.
Ruff is clean on every file this pass touched.

## 23. Phase 7: which model, and is there room for it

The phase exists because NovaControl must not assume one model handles
everything, and must not decide which one to use by recognising a NAME. What
shipped is a decision layer between "what does this request need?" and "what can
this machine hold right now?" — `models/profiles.py` (capability registry),
`models/hardware.py` (hardware monitor), `models/manager.py` (selection, loading,
lifecycle, telemetry), wired into the application and reachable through
`GET /intelligence` and `GET /models`.

`qwen3:8b` appears in exactly one place in this codebase: the declaration table,
as DATA. Selection is a set comparison against declared capabilities, and the
runtime's own `/api/show` report is applied LAST and directionally — it ADDS a
capability it confirms (an undeclared VLM pulled five minutes ago becomes
selectable for a screenshot, which is how the dev box's `qwen3-vl:4b` reached the
vision pipeline without one line of configuration), REMOVES one it can express
and does not list (a text-only build named `llava` is not trusted with an image),
and leaves the declaration alone when it says nothing (`coding` and
`structured_output` are never removed by silence, because no runtime here has a
word for them). A model that is not installed cannot be selected at all, because
the pool is what the runtime offers.

Loading is the specification's six steps, with the fit judged **twice**: whether
the model can live alongside what is resident (if yes, nothing is unloaded —
eviction is not a condition of loading, and a reload costs seconds), and whether
it can live there once the models that MAY be unloaded are gone. A model an
active task is using is never evicted and is not counted as room either; a
resident model whose size the runtime did not report frees nothing. Step 6 is
separate from `loaded`, and when verification cannot run the outcome says
*"accepted but could not confirm"* rather than reporting a verified success.

**The measurement found a bug the tests had been written around.** The first
measured load on this machine showed a load being refused with the reason *"needs
more memory than is free after unloading what could be unloaded"* — while nothing
had been unloaded. The fit check had been `available >= needed + headroom`, i.e.
it never counted the memory eviction would free, so a load that would have fit
comfortably after making room was declined. That is the specification's own
example flow (Qwen3 resident → vision request → unload → load the VLM) failing on
the machine the layer exists for, with a reason that read plausibly the whole
time. The check now computes the candidates, the freed bytes and
`fits_after_evict` before deciding, refuses only when the model does not fit even
then (naming what it would have unloaded), and names the in-use model when *that*
is what blocks the room. Re-measured afterwards, the switch performs exactly as
specified: loading `qwen3-vl:4b` evicted the resident chat model, verified in
7.3 s, and a subsequent `qwen3:8b` load was refused by the arithmetic (4.87 GB
needed, 2.06 GB free + 3.3 GB evictable < needed + 0.5 GB headroom) rather than
by a policy.

**The integration path found a second one, in the NLU rather than the manager.**
Phase 7's Example 2 is *"What's using so much RAM?" → system tools → NO LLM*, and
that phrasing resolved to nothing: the memory rule had no pattern for it, so the
general answer rule claimed it and the request went to research, and the reply
told the user NovaControl *"can't access your system directly"* while it reads
this machine's RAM for a living. The RAM rule now carries the phrasing a person
actually uses (`using so much ram`, `using all my memory`, `consuming most of my
memory`, `chewing up my ram`, …) plus matching exemplars, and all four phrasings
answer from this machine's own reading in ~27 ms with no model selected. The
specific rule sits above the general one deliberately; an unrelated question
("What is the capital of France?") is untouched.

**A third, from the unit tests written for the phase:** `manager.select({"vision"})`
— the spelling a caller naturally writes, and the one the tests used — crashed
with `AttributeError: 'str' object has no attribute 'value'` inside the rejection
path, because a `StrEnum` member equals its value so the set comparison worked
and only the *reason formatting* failed. Capability requirements are now
normalised at the boundary (`as_capabilities`), and an unrecognised name RAISES
instead of being dropped: dropping is right for a config file, where a typo must
not stop the process, and wrong for a requirement, where `{"visoin"}` would
quietly become "nothing in particular" and be answered by a model that cannot
see.

**Telemetry** is one trace per request — per-stage durations (NLU, context,
decision, planning, tool selection, tool execution, vision, model load,
response), total latency, RAM before/after, the model and provider selected,
fast-path usage — exposed through the existing `GET /intelligence` alongside the
interpretation telemetry, with stages that never ran reported as `count: 0`
rather than as an instant zero, and a request whose exception escapes before the
handler filed as a failure by the next one so the count stays honest. Nothing in
it carries a prompt, an answer or a chain of thought.

**Measured on this machine** (16 GB RAM, Intel Arc, Intel NPU): capability probe
**0.058 s** for 2 installed models; `qwen3-vl:4b` (3.07 GB measured) loaded and
**verified in 7.3–8.0 s**; unload **31 ms**; RAM-aware refusal with the numbers
rather than paging; NPU and GPU both reported as **not detected, with the
reason**; deterministic requests measured end to end at **27–34 ms** (NLU 2.1 ms,
decision 0.3 ms, response 0.1 ms) with a RAM delta under 2 MB; a request that
went to research moved −5.6 GB as the runtime loaded a model behind it.

**Files**: `models/{profiles,hardware,manager,__init__}.py` (new package),
`core/config.py` + `configs/default.yaml` (`ModelSettings`, env overrides),
`application.py` (manager built before the vision pipeline and consulted for its
provider, `models_status` on the cheap readout and the measured `model_status`,
load/unload routed through the manager, stage instrumentation),
`api/app.py` (`/intelligence` gains the model block), `intelligence/telemetry.py`
(stage + request telemetry), `intelligence/model_manager.py` (the backend can now
describe its models), `intelligence/{rules,exemplars}.py` (Example 2's phrasings),
`docs/MODELS.md` (new), `README.md`.

**Tests added**: `tests/test_model_phase7.py` (60, capability registry,
runtime-reported reconciliation, RAM-aware loading, keep-alive, hardware honesty,
routing, switching, telemetry) and `tests/test_model_pipeline_phase7.py` (14, the
four request flows, measured telemetry, the model surfaces, `/intelligence` at
the HTTP level). `tests/test_model_lifecycle.py` gained a stated-memory monitor so
its eviction assertions stop depending on how much RAM the machine running the
suite happens to have free, and its application surface now injects the fake into
the layer the application actually loads through.

**Validation**: full suite **1574 passed / 12 skipped** (1661 subtests, Windows,
4:36); mypy clean on both views (**217 modules**, four new); `docs/API.md` in
sync; ruff clean on every file this section touched. **Remaining limitation**:
Phase 7's Example 3 phrasing ("Find my NovaControl project, run the tests and
explain why they fail.") still reaches the research pipeline rather than the
agentic one — a decision-layer classification question that predates this phase,
recorded here rather than papered over; the shorter "find my … project and run
the tests" reaches the agent route, and either way the route is correctly not a
fast path.

## 24. Phase 7 verified against its own specification

The audit found the same shape of defect the previous two phases did: capabilities
that exist, are documented, are unit-tested, and have **nothing on the live path**
exercising them. Six findings, all fixed, plus one dead helper removed.

**1. The keep-alive policies were inert.** `immediate`, `warm`, `while_active` and
`never` were parsed, reported in status — and enforced nowhere: `enforce_keep_alive`,
`release_after_use`, `begin_activity` and `end_activity` had no caller outside the
tests. Worse, `while_active` *was* `never` (both returned `False` from
`release_after_use`), so a configured policy behaved exactly like a different one
and nothing could tell. Now: the chat handler marks the local model in use for the
duration of the answer it is writing and releases it after; the vision handler does
the same through the new `acquire`; `while_active` releases once the last task
using the model has finished; and the `warm` idle window is applied where the
manager is asked about its state (the status read clients poll) and at the start of
every load — no timer thread invented, and a `keep_warm_seconds: 0` deployment
returns before touching the runtime.

**2. An automatic request never went through the RAM-aware load.** `load` — the
six steps, the measured fit, the eviction, the verification — was reachable only
from `POST /models/load`. So a vision request reached the runtime with the chat
model still resident: the exact conflict Phase 7 exists to resolve, on the exact
flow its own example describes. `ModelManager.acquire` pairs the six steps with the
activity that protects the model being acquired, and the vision handler uses it
before the VLM is asked to look. A refusal is now a RESULT: the pipeline answers
from the screen's text with `allow_vlm=False`, the payload carries the lifecycle
record, and the message says the model was not loaded rather than presenting the
OCR answer as though no model were ever needed.

**3. Example 3 collapsed to its first clause.** *"Find my NovaControl project, run
the tests and explain why they fail"* decomposes into three actions
(`find_file`, `run_command`, `answer_question`) and its own assessment says
`needs_planner` — but the intent table has no executor for the first two, so the
decision fell through to the unhandled-capability branch and the application
classified the request from its **first** clause. The tests were never run and the
explanation was never written. Three narrow changes: the decision's rule 6 now
plans a multi-action reading whose primary intent no subsystem dispatches; rule 3
names the PLANNER (not `chat`) when the model is needed for several actions; and
the application honours the decision's own handler when the intent table names
none — while keeping the legacy classifier for the bare "let language handling deal
with it" fallback, which is how `self_improvement` stays reachable. Measured on this
machine: route `planning`, handler `plan`, **57 ms** and offline, where the same
sentence previously took **44 s** down the research path and answered about the
project's purpose instead of running its tests. The decision also now carries the
sequencing requirement to the executor that can honour it when the handler it
landed on cannot sequence at all — one shared `SEQUENCING_HANDLERS` set, defined
once beside the routing table.

**4. Context latency was never measured.** The stage was declared in
`REQUEST_STAGES`, exposed on `/intelligence`, and permanently `count: 0` — a metric
named in the specification and reported as never occurring. It is now timed in the
understanding engine where the context layer actually does its work (resolving a
reference against what is already known). Requests the cheap layers resolve without
it honestly report "not reached" instead of a fabricated zero, which is also what
the readout means everywhere else.

**5. First-token latency and generation rate described one path only.** They were
recorded exclusively through `record_escalation`, i.e. only when the NLU gave up and
asked a model — so the model that answered most requests reported nothing, and
filing chat timings through the same call would have inflated the escalation rate,
the number the architecture is judged by. `record_model_timings` files the
provider's own breakdown under the reason that produced it (`chat`, `vision`),
separate from `escalations`, and the application reads `last_timings` from the
provider that actually made the call (the vision pipeline's wrapper now exposes what
it wrapped, so the inner provider's measurements are reachable without touching a
private attribute).

**6. Dead code.** `models/hardware.py`'s `describe_processes` had no caller; removed.

**Gates**: full suite **1587 passed / 12 skipped** (1661 subtests, 4:03); mypy clean
on both views (**217 modules**); `docs/API.md` in sync; no new ruff findings on any
file touched (the repo's pre-existing baseline in `application.py`/`engine.py`/
`rules.py` is unchanged and untouched). New tests, 84 collected across the two
files together (`tests/test_model_phase7.py` 63, `tests/test_model_pipeline_phase7.py`
21): a `ModelAcquisitionTests` class for the `acquire`/release pairing, eviction
protection, the idle window applied on a load and `while_active` finally meaning
what it says; two cases for `record_model_timings` (measured without claiming an
escalation, and silent rather than zero when the backend reports none); the
stage-name contract now checked across the engine as well as the application;
a `ModelResidencyOnTheRequestPathTests` class covering the vision model acquired
and released around the capture, a refusal skipping the VLM with the reason in the
message, and the chat model marked in use for the duration of its answer; Example 3
end to end (route, handler, plan, tool discovery); and the context stage asserted
on both sides — populated where the layer is consulted, `count: 0` where it is not.

## 25. Phases 1–7 verified against the running request path

The earlier verifications checked phases 2, 3, 6 and 7 (`§16`, `§17`, `§22`, `§24`).
This pass covers the whole staged build — 1 foundation, 2 understanding and context,
3 the decision engine, 4 the planner, 5 tool discovery/validation/caching, 6 vision,
7 model + hardware management — with the same rule every time: **a capability counts
only if something on the live path exercises it.** Each claim was re-derived from the
code and the docs, then driven through `handle_request`/`run_plan` with no local model
installed, so nothing could pass by being explained rather than performed.

**What works, measured.** "Open Chrome." → `desktop_automation`/`open_application`,
0.9, fast path, no model. "What's my RAM usage?" → `system`/`memory_status`, a live
reading ("12.0 GB of 15.4 GB, 77.8%") with no model. "Continue from where I stopped"
→ one precise question on a fresh app, and after a request it replays the remembered
intent. "Open Chrome and search YouTube for Python tutorials." → browser planning
with `open_application`/`navigate`/`search`. "Look at this screenshot and tell me why
I can't log in." → the vision route, with the honesty note that no VLM is wired
rather than an invented answer. Discovery: "Which programs are consuming most of my
memory?" → `system_monitor` (0.73), "what can you do?" → `capabilities` (0.67), "can
you help me?" → no tool at all; the cache refuses volatile payloads and the floor is
0.25. Phase 7's acquisition, eviction protection, traces and telemetry behave as
`§24` recorded. The context layer resolves pronouns at its own layer: after "Open
Chrome.", `understand("open it")` is `open_application` with `application: chrome`.

**1. Two defects on the phase 2 → 4 seam: the resolved reference never reached the
plan.** The understanding layer resolved "open it" to chrome correctly — and the
desktop/browser handlers re-parsed the raw words anyway, so the plan said
`target: "it"` and the workflow would have launched a program called "it".
`close it` was worse: the parser had no named-close branch at all, so it planned
`EXECUTE_SCRIPT: "close it"`. Both are fixed at the seam rather than duplicated per
handler: `_command_with_resolved_references` restates a *reference-only* request as
the command the native parsers accept (`open it` → `open chrome`), a chain keeps its
own words because one template cannot carry its other clauses, and
`parse_desktop_command` gained a named-close branch (`close|quit|exit|kill|stop
<app>` → the same graceful `STOP_APP` window close `stop that` already used). Live
again: "open it" → `open chrome` (`open_application`, target `chrome`); "close it" →
`close chrome` (`stop_app`, target `chrome`).

**2. Phase 4's own example could plan the work but not perform it.** The compiler's
`run-tests` and `run-command` steps named no tool — `IntentName.RUN_COMMAND` carries
no `tools=` — so `_run_plan_step` raised `RuntimeError: No executor is registered for
action 'run_tests'`, and the flagship goal "find my project, run the tests and
explain why they fail" failed at its central step with an internal error. The step
handler now runs them for real: preset argv, exec-form (`create_subprocess_exec`, the
repo's no-shell invariant), a hard wall-clock timeout, output captured from the tail
where a test summary actually is, and exit code reported to the verifier. A test step
locates the project first and recognises its runner (`npm test`, else `pytest` via
`sys.executable`; anything unrecognised is reported rather than guessed at). None of
it starts without explicit approval: the handler refuses with a `PermissionError`
unless the caller named the step in `run_plan(approved=...)` — the same gate the
destructive steps already pass and the same one `POST /plan/run` exposes — so the
request path now reports "needs explicit approval" instead of a bug, and an approved
run completes and verifies.

**Residuals, recorded rather than papered over.** (a) `read_file`/`list_files` are
understood — "read report.pdf" reads as `read_file`, 0.85 — but no handler exists, so
the reply is chat's "I currently can't access or read files", and the declared
`file_manager` tool is still unregistered; wiring real file capabilities is a
separate piece of work, not a leak in this one. (b) The legacy classifier fallback
can still plan a literal reference ("open that folder" with nothing in context plans
a folder named "that") when the GIL's clarification does not win the route. (c)
Escalated requests report `nlu.model == "scratch"` — that is the provider's name
standing in for the model's; cosmetic. (d) A real VLM's answer quality still cannot
be validated on this machine (no vision model installed), as `§22`/`§24` recorded.

**Gates**: full suite **1594 passed / 12 skipped** (1665 subtests, 6:29); mypy clean
(**217 modules**); `docs/API.md` in sync; no new ruff findings in any line this work
added (the pre-existing baseline is untouched). Seven new tests:
`ResolvedReferenceReachesThePlannerTests` drives "open it" and "close it" through
`handle_request` and asserts the planned target (the old test only checked
`engine.understand`, which is exactly why the seam could break unnoticed); the desktop
suite pins the named-close branch; and `test_planner_phase4.py` adds the four rules
for command steps — refused without approval, runs and verifies with it, runs the
located project's real suite, and never starts a process the caller did not approve.
