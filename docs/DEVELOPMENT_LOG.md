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

## 12. Where things stand

- **Scale**: ~65,000 tracked lines (31.8k Python source, 18.2k tests, 10k
  web UI, 2.9k markdown, remainder scripts/config).
- **Validation at the tip**: full suite 893 passed / 12 skipped (Windows);
  mypy clean on both the linux and win32 views; `docs/API.md` in sync; CI
  green through run #11 (`a378f26`), which is where the both-views type check
  landed — the run for `7b17629` carries the same checks.
- **History**: all work pushed; `main` == `origin/main` at `7b17629`.
- **Open threads**: stored Gemini key is still the Vertex-format one (Settings
  → Test Connection with an `AIza…` key fixes Chat/Coding/Explore in one
  step); local answers take 20–110s on this CPU-only box — the timeout now
  lets them finish, but streaming would make them feel far faster; NovaLink
  M1 (companion transport) is designed and ready to build when scheduled.
