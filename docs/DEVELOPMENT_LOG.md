# NovaControl Development Log

Work record from the routing-explorer + phone-control checkpoint (`580ae07`,
2026-09-10) through the current tip (`ddac7d5`, 2026-09-18). Every section is
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

## 11. Where things stand

- **Scale**: ~56,000 lines total (28.9k Python source, 15.6k tests, 7.8k web
  UI, remainder docs/scripts/config).
- **Validation at the tip**: full suite 759 passed / 0 failed (Windows);
  mypy clean; `docs/API.md` in sync; CI run #7 green on py3.12 + py3.13;
  browser-walk verified on real Edge with zero zombie processes.
- **History**: all work pushed; `main` == `origin/main` at `ddac7d5`.
- **Open threads**: project-mode drafting requires a working model (single-file
  mode has deterministic fallback, projects deliberately error honestly);
  stored Gemini key is still the Vertex-format one (Settings → Test Connection
  with an `AIza…` key fixes Chat/Coding/Explore in one step); NovaLink M1
  (companion transport) is designed and ready to build when scheduled.
