# NovaControl

> 🤖 **An AI-powered desktop automation assistant** — it understands natural-language commands and actually *does* them: driving your desktop, browser, and Android phone, with vision-guided verification, a live web UI, and a built-in bug journal.

NovaControl is a local-first, agentic computer-control system. You type or speak a command like *"open steam and go to library and launch gta v"*, and NovaControl normalizes it, resolves typos and context, plans ordered steps, executes them, and **verifies the result** — asking a precise clarification question only when the request is genuinely ambiguous.

One brain → many capabilities:

| Capability | What it does |
|---|---|
| 🧠 **Global Intelligence Layer** | Normalization, typo tolerance, intent detection, context/reference resolution, multi-intent decomposition |
| 🖥 **Desktop automation** | Open any installed app or folder, in-app chains, dictation, window focus, screenshots (Windows) |
| 🌐 **Browser automation** | Playwright-backed navigation, extraction, form filling, and web-app checks behind an approval policy |
| 👁 **Vision** | Screen understanding, guided clicks ("click the Library button"), pixel-diff verification, optional multimodal vision model |
| 📱 **Phone control** | Android over adb: launch apps, verified screenshots, pre-filled texts/calls |
| 🔎 **Research** | Multi-stage research pipeline that streams live progress into the UI; topics suggested from live daily news, answers synthesized from site-chrome-cleaned facts |
| 🧭 **Local by default** | A cloud key is STORED when you paste it and never becomes the brain until you pick Cloud in the Brain switch — auto mode means the local Ollama model when one is running, otherwise the offline scratch engine |
| 💬 **Dual brain** | Fully local scratch brain — incl. worded & exam-grade (JEE) math — or a real LLM: local **Ollama** (with a model picker) or a cloud key (**OpenAI-compatible, Gemini, Groq, and Claude** speaking its native `/v1/messages` protocol) |

---

## ✨ Key Features

- **Natural-language understanding that forgives humans** — punctuation, capitalization, spacing, and common typos all collapse to the same structured intent:

  | You type | NovaControl understands |
  |---|---|
  | `open chrome`, `open chrome.`, `Open Chrome!`, `OPEN   CHROME`, `please open chrome`, `launch chrome`, `opn chrme` | **OPEN_APPLICATION(chrome)** — all identical |
  | `take a screenshot`, `capture my screen` | **TAKE_SCREENSHOT** |
  | `text mom on my phone saying hi` | **PHONE_SEND_TEXT(recipient: mom, message: hi)** |
  | `open it` (after opening Chrome) | **OPEN_APPLICATION(chrome)** — resolved from context |

- **Layered resolution** — deterministic fast path → fuzzy typo correction → learned variations → contextual/reference resolution → semantic LLM fallback → one precise clarification. Trivial punctuation differences never wake the model.
- **Worded & exam-grade math, fully offline** — the scratch brain's declarative phrase registry computes compound worded arithmetic ("what is 2 cubed plus the square root of 9"), percentages ("15 percent of 200"), the natural fraction family ("half of 10", "one quarter of 8", "a third of 90", "three quarters of 200" — bare, article, and spelled numerators all work), large-scale operands ("3 million times 2", "2.5 billion divided by 4", "half of three million" — digits or words, through trillion), unit/time conversions, and JEE-style questions: logs (`log base 2 of 8`), degree trig (`sin 30 degrees`), combinatorics (`10C3`, `8 choose 2`, `factorial of 5`), full quadratic solving with discriminant and roots, and arithmetic-progression term/sum questions.
- **Multi-intent decomposition** — *"open notepad and take a screenshot"* becomes two planned, ordered steps.
- **Concurrent-activity safe** — every research and command execution is tagged with a run-scoped `correlation_id` that flows from the request through the event bus to the SSE channel; the web UI routes each progress frame to the run that owns it, so parallel activities never bleed into each other's panels.
- **Learning loop** — fuzzy/contextual resolutions teach the phrase back to the registry, so the same typo takes the fast path next time.
- **Teachable knowledge** — the Learn tab's *Teach* box and chat's "remember this: …" both persist typed facts as durable knowledge (SQLite-backed KNOWLEDGE namespace, duplicate detection included). Ask about a taught topic later — in chat or the Learn tab's recall — and it comes back: "remember this: the wifi password is hunter2" → "what is the wifi password?" → *"You taught me: the wifi password is hunter2"*.
- **Code-aware Build planning + coding agent** — the Build tab's *Plan The Code* turns a goal plus a language (Python, JS, TS, Go, Rust, Java, C#, C++, Ruby, Shell, SQL) into a real coding plan: language-idiomatic step sequence, a derived artifact file name (goal words → `fibonacci_generator.py`, `todo_cli_go.go`, …), language-appropriate test-file naming, and a drafted code artifact. When a model is configured, **the coding agent** (`core/code_agent.py`) drafts the code, **executes it in a sandboxed subprocess** (fresh temp cwd, no network on POSIX, 15s wall-clock timeout), reads the real interpreter error, and asks the model to fix exactly that — up to 3 fix rounds — shipping only code that ran clean (or an honest failure trace). The agent's draft → run → fix steps render in the Build tab, which also states up front whether the agent is live or only the runnable scaffold fallback is possible. A math-detection **parity corpus** also runs every math phrasing through both scratch.py and the routing explorer's JS mirror in CI, so the two classifiers can never silently drift apart.
  **Plan The Project** extends the same agent to multi-file work: the model returns a JSON file map (2–5 files + an entry point), the entry runs in the same sandbox, and real errors drive fixes to the affected files (same 3-round budget, whole-project re-runs). Every drafted file renders with a per-file Copy/Save, saved artifacts list in a **Saved Artifacts** strip, and **Run** re-executes any saved file through the same sandbox (`POST /build/artifacts`, `/build/run`, `/build/save`).
- **Self-improvement telemetry** — resolutions, clarifications, unknown intents, and failed entity resolutions are recorded and exposed as human-readable improvement findings.
- **Capability registry** — every subsystem declares its intents, required entities, risk level, executor, and *verification strategy*; the orchestrator derives execution and confirmation policy from that table.
- **Safety-first execution** — device actions require a server-minted, single-use, expiring approval token; auto-approve is opt-in.
- **One brain everywhere — Chat and Explore follow the same mode** — Explore's research synthesis uses whichever brain Chat uses (scratch templates, the local Ollama model, or the cloud provider), re-synced on every mode switch and boot; its report cache is keyed per synthesis brain, so switching modes never serves the previous brain's cached answer.
- **Cloud LLM with honest diagnostics** — Settings → Cloud LLM covers every preset with a **Test Connection** button that pings the provider with the pasted key *before* saving it (nothing is persisted on failure; the response names the failing stage — timeout vs rejected key). While a cloud brain is active, the System panel shows lifetime **token usage** (prompt/completion/total per the provider's own usage block) and the **last transport error** verbatim, so a dead key is visible at a glance.
- **Auto-resolving bug log** — open verification bugs are marked fixed automatically (with evidence) when a later pixel-diff proves the click actually changed the screen.
- **Routing Explorer** — a dedicated panel that traces any utterance through NovaBrain's intent gates, showing which rung owns it and a live preview of what you would actually see (the scratch answer text, the research headline, or the plan outline). Traced by the live server (`POST /brain/decide`); an embedded JS mirror takes over when the server is unreachable. A **broad-classifier toggle** re-runs the same utterance through the scratch engine's wide `_classify()` view and highlights where the narrow gate and the broad engine deliberately disagree (order / engine-only / breadth / unknown / match) — including casual phrasings, which the mirror's research detector knows ("things to know about X", "wtf is X", …), drift-guarded against the Python keyword list.
- **Full phone control over ADB** — the J.A.R.V.I.S phone mode opens any installed app (alias or package), runs in-app searches via deep links (YouTube/Google/Maps/Spotify…), texts saved contacts by name (resolved against the device contact book, never split by guesswork), and opens files/folders through the Files app or system chooser.
- **Answers built from content, not chrome** — search snippets routinely glue consent banners, footers, and trademark lines onto real text. A sentence-level site-chrome filter strips them *before* synthesis, so a researched answer explains the topic instead of quoting "By using this site, you agree to the Terms of Use" or "This page was last edited on …". Chat and Explore share the pipeline **and** the filter, so both answer from the same cleaned facts.
- **Research reads the pages, then answers from them** — a search result is a 150-character meta description, and no rule can turn one into an answer, so Explore fetches each source (`explore/page_reader.py`, standard library only) and works from the article's own prose: scripts, navigation, footers, labels, headings, and HTML artifacts are stripped, and the page is what the answer quotes. Reading is an upgrade, never a requirement — a blocked or script-only page keeps its summary, and the report says so.
- **Research answers the question, not the topic's keywords** — a connected model reads the user's own question and plans the searches itself (`explore/planner.py`): what is being asked, and two to four queries covering the angles that exist for it (direct answer, authoritative source, current state, counter-view). Relevance is then judged against that plan, so evidence that never repeats the user's phrasing still counts. Answer synthesis is question-first — the prompt carries the question and the planner's reading of it, plus the page prose, and the model is forbidden from listing source titles or pasting snippets, because a model that echoes the evidence reproduces the keyword-shaped non-answer this replaces.
- **The local answer is ranked evidence, not keyword buckets** — with no model connected, `explore/evidence.py` ranks every sentence the sources contain against the question (term rarity across the result set, how much the sentence actually says, cross-source agreement, specificity, position — no topic word lists anywhere) and composes the answer as prose plus supporting detail, attributed at the end rather than framed as "here is what the sources say about X". The answer, the highlight cards, the key points, and the sections each take a **different** slice of that pool, so the same sentence never appears twice on one page. A connected-but-broken brain (rejected key, stopped Ollama) is named in the report with its own error, never silently downgraded; with no model at all the report says the answer is assembled from source sentences.
- **Chat stays a conversation; Explore keeps the report** — a research question asked in Chat answers inside the chat stream as a structured answer card (the sourced answer text with its "What it is:" / "Key points:" sections, clickable source chips, and an `explore` chip that jumps to the full report); the full research page — sections, sources rail, related videos, follow-up suggestions — lives only in the Explore panel. "How to" instructional questions route to research instead of the chat fallback, and a compound topic needs two content-word matches before a source is accepted, so "container garden" no longer pulls in Docker pages.
- **Understands casual phrasing** — you don't have to type perfect questions. Scaffold forms ("things to know about X", "what should i know about X", "whats the deal with X", "tell me stuff about X", "any facts about X", "wtf is X") all route to research, and the search topic is anchored to what FOLLOWS the scaffold — so "the most important things to know about brics summit 2026" researches BRICS, not dictionary pages for the word "important". Explicit memory-store phrasing ("remember this: …") still outranks research words inside the remembered content.
- **Daily-updates topic suggestions** — the Explore panel's topic chips are never a hardcoded list: they come from `GET /explore/trending`, which fetches live top-story news headlines (Google News RSS, standard library only, no API key), trims each into a research topic, caches for 30 minutes, invalidates by day, and rotates the visible window hourly — so each visit suggests a different slice of what's actually happening in the world. When the feed is unreachable the panel degrades to a few static help examples.
- **Real system telemetry, never invented numbers** — the Command Center's **System Telemetry** section leads the panel with live metric cards (CPU, memory, storage, GPU, network, battery, temperature, uptime) and the ribbon carries a compact **NOVA CORE** indicator (`CPU 18% · RAM 40% · 3 TASKS`) that opens the full read-out. Every value is measured: `psutil` when installed, the platform's own facilities otherwise (Win32 `GetSystemTimes`/`GlobalMemoryStatusEx`/`GetSystemPowerStatus`, `shutil.disk_usage`, Windows GPU Engine perf counters, `Get-NetAdapterStatistics`, WMI thermal zones, `/proc` and `/sys` on Linux). A metric that cannot be obtained renders **Unavailable** with the reason and *no value at all* — never a zero that reads like a sensor reading — byte rates stay "measuring" until a second counter sample exists, and sampled values show their age. Cost is tiered so polling stays free: cheap metrics are read in-process per request, while GPU/network/temperature are sampled by a daemon thread on a slow cadence, so serving a poll never spawns a process (see [docs/TELEMETRY.md](docs/TELEMETRY.md)).
- **Command-center web UI** — one committed design layer over every panel: a fluid layout (`clamp()` gutters, content-sized rails, breakpoints from 1920px down to phones) that reflows instead of clipping, plus a verified fit audit across all 11 panels and 11 widths. The audit also proves the screen shows ONE panel: each nav click is checked for a lone laid-out panel starting inside the viewport (an id-selector `display` rule once beat `.panel { display: none }`, keeping the Command Center rendered underneath every other panel and pushing the panel you clicked below the fold) that fills the workspace instead of leaving a dead strip down the right edge.

## 🧠 Architecture

```mermaid
flowchart TD
    A["🗣 User input<br/>(web UI · voice · API · CLI)"] --> B["🧠 Global Intelligence Layer"]
    B --> C["Context + Memory"]
    C --> D["Structured Intent"]
    D --> E["Capability Registry<br/>(risk · entities · verifier)"]
    E --> F["Orchestrator"]
    F --> G1["Desktop"]
    F --> G2["Phone (adb)"]
    F --> G3["Browser (Playwright)"]
    F --> G4["Explore / Research"]
    F --> G5["Chat Brain"]
    F --> G6["Planning · Agents · Automation"]
    G1 & G2 & G3 & G4 & G5 & G6 --> H["Action → Observe → Verify"]
    H -- "failure" --> I["Recovery → Retry → Bug Log"]
    H -- "success" --> J["Memory + Response"]
    I --> J
```

**Language understanding is a platform capability.** No subsystem parses raw user strings on its own — JARVIS, Research, Automation, and the Browser Agent all consume the same structured intent.

### Key modules

```text
src/novacontrol/
  intelligence/   Global Input Intelligence: normalize, intent rules,
                  context/references, capability registry, telemetry,
                  StandardResult envelope
  brain/          Scratch brain (fully local answers) + LLM chat + routing
  desktop/        Desktop controller, parser/planner, vision-guided control
  phone/          adb bridge: connect, apps, texts, calls, screenshots
  browser/        Playwright runner + approval-aware workflow controller
  vision/         Screen understanding (LLM provider, heuristic fallback)
  agentcore/      Task interpreter, adaptive planner, verifier, recovery
  voice/          STT/TTS via OS-native speech services
  tasks/          Task center (create, update, delete, clear)
  core/           Event bus, runtime, config, security (deny-by-default
                  approvals), bug log, audit trail
  explore/        Research pipeline with live progress events
  memory/         Namespaced memory (SQLite-backed)
  api/            FastAPI surface + single SSE activity channel
  web/static/     The web UI (vanilla JS, no build step)
  gui/            PySide6 desktop dashboard
  cli/            Command-line interface and demos
```

### Safety model

- **Deny-by-default approvals** — device actions execute only with a server-minted, single-use, expiring token issued for that exact plan
- **Auto-approve is opt-in** (Settings checkbox, default off)
- Destructive/ambiguous + high-risk intents require explicit confirmation
- Phone texts and calls are **pre-fill only** — the human always presses send/dial on the device
- Self-improvement applies nothing without a sandboxed preview + approval

---

## 🚀 Requirements

| Requirement | Version |
|---|---|
| Python | **3.12+** |
| OS | Windows (desktop + voice features); Linux/macOS for core web/API |
| Ollama (optional) | Any recent version — local chat brain and/or a local vision model (e.g. `ollama pull llama3.2-vision`) |
| Android platform-tools (optional) | For phone control |

## 📦 Installation

```powershell
git clone <your-repo-url>
cd NovaControl

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Optional extras (browser automation, GUI, vision, voice, git, database, Redis):

```powershell
python -m pip install -e ".[browser,gui]"
```

## ⚙️ Configuration

NovaControl is configured via environment variables — no `.env` file is required to run:

| Variable | Purpose | Default |
|---|---|---|
| `NOVACONTROL_API_TOKEN` | Require bearer-token auth on protected API routes | *(auth off)* |
| `NOVACONTROL_ENV` | Environment name | `dev` |
| `NOVACONTROL_LOG_LEVEL` | Logging level | `INFO` |
| `NOVACONTROL_REQUIRE_APPROVAL` | Toggle approval enforcement | `true` |
| `NOVACONTROL_DATABASE_URL` | Database connection (SQLite default) | local SQLite |
| `NOVACONTROL_REDIS_URL` | Redis for optional worker/queue features | — |
| `NOVACONTROL_OLLAMA_URL` | Ollama endpoint for the LLM brain | `http://127.0.0.1:11434` |
| `NOVACONTROL_DISABLE_OLLAMA` | Skip Ollama auto-detection | unset |
| `NOVACONTROL_ENABLE_EXTERNAL_LLM` | Enable OpenAI-compatible/Gemini-style cloud providers | unset |
| `NOVACONTROL_LLM_BASE_URL` | Cloud provider base URL | — |
| `NOVACONTROL_LLM_API_KEY` | Cloud provider API key | — |
| `NOVACONTROL_LLM_MODEL` | Model name | provider default |

> 🔐 **Secret handling:** API keys are stored locally and never returned by any API endpoint. This includes the vision-model key (`POST /vision/model` accepts it once and only ever reports a redacted tail). Never commit `.env` files or tokens — local artifacts like `token.txt` should stay out of version control.
>
> 💡 **Key formats matter:** each preset expects the key from that provider's own console — the Gemini preset wants an AI Studio key (`AIza…`), not a Vertex/ADC credential (a mismatch shows up as HTTP 400/401 on first chat). Use **Test Connection** in Settings → Cloud LLM to verify a key before saving it.

## 🏃 Usage

### Run the web app

```powershell
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/** and try:

- `open notepad and type hello`
- `what is quantum tunneling` (research — streams live stages)
- `take a screenshot on my phone`
- `whats the deal with brics summit 2026` (casual phrasing — routes to research, searches the actual topic)
- The Explore panel opens with **today's news topics** as one-click research chips

### Natural-language command examples

```text
open chrome                              → launches Chrome, verifies the window
open steam and go to library and launch gta v
                                         → 3-step plan: launch → deep-link navigate →
                                           vision-guided launch with process verification
open notepad and type meeting notes      → dictation via clipboard-staged paste
open downloads                           → opens the folder in Explorer
open games folder                        → finds the real folder across drives and opens it
what is 2 cubed plus the square root of 9 → 11 (offline scratch brain)
log base 2 of 8 plus 10C3                → 123 (JEE-style: logs + combinatorics)
solve x^2 - 5x + 6 = 0                   → worked quadratic solution with roots
stop that                                → graceful WM_CLOSE of the last app
open youtube on my phone                 → launches YouTube on the paired device
search cats on youtube on my phone       → YouTube deep-link search (nothing typed)
open the report.pdf file on my phone     → opens the file via the system chooser
open the downloads folder on my phone    → opens the Files app
open steam and go to library and launch gta v
                                         → 3-step desktop plan: launch → deep-link navigate →
                                           vision-guided launch with process verification
take a screenshot on my phone            → PNG pulled to data/screenshots/, verified
text sesi 2 saying hi from NovaControl  → resolves the saved contact → pre-fills messaging; you press send
how to start a container garden on a balcony → researches gardening (not Docker)
whats the deal with quantum computing    → researches quantum computing
tell me stuff about the mariana trench   → researches the mariana trench
remember this: facts about cats          → stores to memory (does NOT research cats)
add five and seven                       → 12 (spelled-out add form)
remember this: my wifi password is hunter2
                                         → taught as durable knowledge (Learn tab / chat)
what is the wifi password                → "You taught me: my wifi password is hunter2"
```

### How Explore understands what you type

Research queries rarely arrive as perfect sentences, so Explore's language layer anchors on **scaffold markers** instead of rigid grammar: the real topic is whatever *follows* "things to know about", "should i know about", "the deal with", "stuff/facts about", "info on", "wtf is", "basics of". The router accepts the same casual forms, so "whats the deal with quantum computing" routes to research *and* searches for quantum computing — not for "deal". Scaffold adjectives never leak into the search (the fix for researching the word "important" instead of the BRICS summit). Sources must clear a topical-relevance gate: a compound topic ("container garden", "brics summit 2026") needs two content-word matches from a page, so Docker and shipping-container pages can't answer a gardening question, while comparison topics ("compare email, chat, and phone calls") stay per-item.

### Web interface

The browser UI is a single-page command center served by the API (`/`): a grouped sidebar (Core / Intelligence / Automation / Engineering) over eleven panels — **Command**, **Chat**, **Explore**, **Routing**, **J.A.R.V.I.S**, **Vision**, **Build**, **Learn**, **System**, **Settings**, **CLI** — backed by one SSE channel (`/events/stream`) for live progress and the Recent Activity timeline.

Layout is fluid rather than fixed: gutters, heights, and rails are `clamp()`-based tokens, so the same markup reflows from a 1920px desktop to a 390px phone without a separate mobile build. The navigation is never an overlay: the sidebar is a column at every width where a column fits (268px, narrowing to a 214px rail below 1040px) and becomes a top command deck below 700px, whose pills **wrap into rows** so every destination stays visible (never hidden behind a sideways scroll). There is no off-canvas drawer and no hamburger — a drawer covered the panel the reader had just opened, which reads as clipped content; `scripts/ui_audit.py` now fails the build if the sidebar is fixed over the workspace or has no width at any audited breakpoint. The stylesheet is a single design-token block plus one component layer — consolidated with a **computed-style A/B audit** (`scripts/ui_audit.py --baseline <old.css>`) that proves a cleanup changed nothing the browser can see — and reduced-motion is respected throughout.

The Command Center opens with **System Telemetry**: a `SAMPLED 12S AGO` freshness pill, then metric cards (CPU, memory, storage, GPU, network, battery, temperature, uptime) with a live bar each, a pill row for AI engine / Vision / automation / active tasks, and a **Details** table (host, OS, CPU model, cores, uptime, sensor backend, GPU, network interfaces, cloud brain, tracked tasks). Cards are built once and updated in place, so the bars animate to each new reading; the poll runs every 2s and pauses while the tab is hidden. A metric this machine cannot report is muted and says why — on an Intel-integrated laptop the TEMP card reads *Unavailable — the firmware reports no ACPI thermal zone*, and VRAM is reported as `shared` GPU memory rather than a fabricated total.

Chat presentation follows conversation conventions on purpose: research answers arrive as structured answer cards — the answer text with its heading sections and bullets, source chips, and an `explore` hand-off chip that renders the already-fetched report into the Explore panel (no second research round-trip) — while the full report furniture (query chip, verification, sources/videos rail, follow-up question chips) remains the Explore panel's job. `ChatResearchBubbleDomTests` pins this split — the Explore page must never render inside the chat stream. The Explore panel's welcome card shows live **trending topic chips** (today's news, rotating hourly) instead of fixed examples.

Verify the UI end to end:

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/ -q            # includes the UI contract + focus-ring + reduced-motion suites
python scripts/ui_audit.py --url http://127.0.0.1:8000/                  # every panel fits at every breakpoint
python scripts/ui_audit.py --url http://127.0.0.1:8000/ --baseline old.css  # zero computed-style drift
```

### Voice input

The J.A.R.V.I.S tab has a **VOICE** button using OS-native speech recognition and synthesis (Windows); see [docs/VOICE.md](docs/VOICE.md).

### Phone control setup

1. Download [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) and unzip (NovaControl auto-finds it in `~/platform-tools` even when it's not on `PATH`)
2. On the device: **Settings → About → tap Build number 7×** → **Developer options → enable USB debugging**
3. Plug in over USB → press **Connect Phone** in the J.A.R.V.I.S tab → accept the RSA prompt on the device

Once paired, phone mode can: **open any installed app** (by alias or package name, discovered from the device), **search inside apps** via deep links (`search cats on youtube on my phone` opens YouTube pre-loaded with the query — nothing is typed), **text a saved contact by name** (the leading words are resolved against the on-device contact book first, so free text is never split by guesswork; the messaging app is pre-filled and *you* press send), **call by saved name** (dialer pre-filled), and **open files/folders** (a named file opens through the system chooser; folders land in the Files app). Bridge status and next steps are surfaced in the J.A.R.V.I.S phone tab, and every executed action lands in the Recent Activity timeline.

> 🔮 **Beyond USB: a no-cable future.** Android 11+ wireless debugging (pair once over Wi-Fi, no cable) is the cheap path and needs zero code rewrites. For control **without USB debugging at all**, [docs/PHONE_COMPANION_APP.md](docs/PHONE_COMPANION_APP.md) sketches a companion Android app built on an AccessibilityService — the phone dials out to NovaControl over WebSocket and becomes a third `PhoneCommandRunner`, unlocking semantic-tree element location for vision-guided phone flows. Sketch only; nothing implemented yet.

### Routing Explorer

The **Routing** panel answers *"where does my utterance land?"*. Type any phrase and it shows the full gate walk — every intent gate NovaBrain evaluates in order, which one matched (and why), the landing intent with confidence, and a small preview of what you would actually see on that rung:

- **scratch** rungs (greetings, math, conversions, knowledge) — the real local answer text;
- **explore** — the report headline the research run would produce;
- **plan / device actions** — the real plan outline and action list.

While the server is up, tracing runs live through `POST /brain/decide` (the landing decision *is* `NovaBrain.decide()` — the explorer can't disagree with chat). If the server is unreachable, an **embedded JS mirror** of the gate table takes over so the explorer still answers offline; it is labelled as approximate in the UI, and its gate names/order are drift-guarded against the Python table in CI.

A **"Compare with broad classifier"** toggle re-runs the same utterance through the scratch engine's *broad* `_classify()` view and highlights where the narrow routing gate and the broad engine deliberately disagree: **order** (e.g. `what time is 2 plus 3` — the narrow router promotes math ahead of time), **engine-only** (`open notepad and type hello` — the broad engine sees desktop_control but its row is routing_safe=False so routing dispatches to real automation instead of a canned answer), **breadth** (a wide engine match with no exact canned key), **unknown** (neither classifier has a local answer), or **match**. The JS mirror computes the same relation offline, and the Python↔JS relation parity is drift-guarded in CI.

### CLI

```powershell
python -m novacontrol            # interactive CLI
python -m novacontrol demo phase12
```

---

## 🔌 API

FastAPI app factory with REST + SSE + WebSocket surfaces. The full route catalog in [docs/API.md](docs/API.md) is **generated** from the shared route registry (`novacontrol.api.route_consumers`) — after adding a route, run `python scripts/generate_api_reference.py` (CI enforces `--check`). The most useful endpoints:

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/ask` | Natural-language entry point — full pipeline |
| `POST` | `/command/plan` · `/command/execute` | Preview a J.A.R.V.I.S plan, then execute with the minted token |
| `GET` | `/phone/status` · `POST /phone/connect` · `/phone/plan` · `/phone/execute` | Phone bridge control |
| `GET`/`POST` | `/vision/describe` · `/vision/click` | Screen understanding and guided clicks |
| `POST` | `/vision/model` · `/vision/model/clear` | Configure/clear the multimodal vision model (hot swap) |
| `GET` | `/intelligence` | GIL telemetry, improvement findings, capability registry |
| `GET` | `/bugs` · `POST /bugs/{id}/fix` · `POST /bugs/clear-fixed` | Bug log review, resolution, and clearing resolved entries |
| `GET` | `/tasks` · `POST /tasks/delete` · `/tasks/clear` · `/tasks/clear/undo` | Task center (Clear All is undoable) |
| `POST` | `/brain/mode` · `/brain/cloud` | Switch scratch/LLM brain and configure cloud providers |
| `POST` | `/brain/cloud/test` | Ping a cloud provider with a pasted key before saving (one-shot, nothing persisted) |
| `GET` | `/brain/ollama/models` · `POST /brain/local/model` | List installed Ollama models / pin which one the local brain uses |
| `POST` | `/brain/decide` | Trace an utterance through the routing gates, with a per-rung preview and optional broad-classifier comparison |
| `POST` | `/knowledge/teach` · `GET /knowledge` · `POST /knowledge/recall` | Teach durable facts, list them, recall by query |
| `POST` | `/plan/code` | Coding agent: sandboxed draft → run → fix loop (bounded rounds) + plan, scaffold fallback when no model |
| `GET` | `/build/artifacts` · `POST /build/run` · `POST /build/save` | Saved Build workspace files, sandbox re-run of a saved file, save a drafted artifact to disk |
| `POST` | `/explore` | Research pipeline |
| `GET` | `/explore/trending` | Current daily research topics from live top-story news (rotating window; feeds the Explore panel's chips) |
| `GET` | `/events/stream` | Live SSE activity channel — every frame carries a `correlation_id` |
| `GET` | `/chat/history` · `POST /chat/history` | The server-side chat transcript shared by every browser/tab (and `POST /chat/clear` to wipe it) |
| `GET` | `/system/telemetry` | Live machine telemetry for the Command Center — CPU, memory, storage, GPU, network, battery, temperature, uptime, plus active tasks / AI engine / Vision / automation status |
| `GET` | `/health` · `/status` | Health and route metadata (`/health` also reports the build id the page compares itself against) |

Example with auth enabled:

```powershell
$env:NOVACONTROL_API_TOKEN='change-me'

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/ask `
  -Headers @{ Authorization = 'Bearer change-me' } `
  -ContentType 'application/json' `
  -Body '{"text":"open chrome"}'
```

---

## 🖥 Desktop Automation

Windows-first, using Start-Menu scans (`.lnk` index), registry App Paths, UWP/Store catalog, known app aliases, `shell:` deep links, and PowerShell for window verification:

- **Open any installed app or spoken folder** — not just a fixed alias list: Start Menu shortcuts, registry App Paths, UWP/Store apps, and folder names searched across the user profile and drive roots ("open games folder" → `D:\Games` on machines that have one)
- **In-app chains** — *"open steam and go to library and launch gta v"* launches Steam, drives `steam://open/library`, then launches the game via Steam's own `steam://rungameid/<appid>` protocol with a **process-level verification** (window title *or* known launcher process), polled up to 90 s for big titles
- **Dictation** — clipboard-staged paste with foreground-window verification
- **Window focus & graceful stop** — `stop that` sends WM_CLOSE, never a hard kill
- Details: [docs/DESKTOP_AUTOMATION.md](docs/DESKTOP_AUTOMATION.md)

## 🌐 Browser Automation

- `PlaywrightBrowserRunner` (optional extra: `pip install -e ".[browser]"`) with a safe `NoopBrowserRunner` fallback that records intent without launching a browser
- Actions: navigation, extraction, form filling, downloads, web-app checks
- **Approval policy:** navigation/form-fill/download/test actions require approval; extraction from already-available context does not
- Details: [docs/BROWSER_AUTOMATION.md](docs/BROWSER_AUTOMATION.md)

## 👁 Vision

The Vision tab wraps desktop control in a perceive → locate → act → verify loop:

- **Describe Screen** — captures your real screen and reports what's visible
- **Configurable multimodal vision model** — point the whole vision layer at a real vision-capable model (local **Ollama** — llava, llama3.2-vision, moondream — auto-detected; or a cloud preset: **OpenAI, Gemini, OpenRouter**) from the web UI's Vision panel or `POST /vision/model`. The screenshot is embedded as OpenAI-format image content and elements are located **semantically** (a 0–1000 grid point), with OCR/landmarks as automatic fallback. The config hot-swaps at runtime, persists across restarts, and the API key is stored only on your machine and never returned by any endpoint.
- **Guided Click** — type a target ("File", "Library", "Play GTA V"); the vision layer locates it via vision model → OCR → UI landmarks, moves the cursor, clicks, and saves before/after screenshots
- **Pixel-diff verification** — before/after frames are diffed with an **OpenCV fast path** (numpy-backed, with connected-component *change-region localization*: did the click site itself react?) and a pure-Pillow fallback where OpenCV is unavailable; results carry the `verified`/`unverified` status and the change regions
- **Honest verification** — when a click's effect can't be confirmed, it records *unverified* rather than claiming success; open verification bugs auto-resolve when a later verified click proves the change
- Details: [docs/VISION.md](docs/VISION.md)

---

## 🧪 Testing

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/ -q
```

Type checking:

```powershell
python -m mypy src
```

The suite is hermetic: phone and desktop tests use fake runners (`NoopPhoneRunner`, `NoopDesktopRunner`) — no real device or OS interaction during tests. Trending-topic tests inject fake headline fetchers, so the news feature is tested without network too (one live smoke is run manually, not in CI).

**Cross-platform CI, same command everywhere** — the full suite runs on Linux and Windows (CI: `pytest tests/ -q` on Ubuntu, Python 3.12 + 3.13). Tests that need OS-specific behavior either isolate it behind a seam or skip honestly:

- Desktop verification logic (window matching, clipboard-paste staging/focus checks) is exercised on every OS through the `ProbingRunner` stub and the `windows_type_paste` runner override — the *logic* is tested everywhere; only the actual Windows process spawning is platform-gated.
- `_window_stem` uses `PureWindowsPath`, so Windows-style app paths parse identically on Linux CI.
- Tests that shell out to `node` (JS parity mirrors, UI pacing) skip cleanly when Node isn't installed; `scripts/dead_code_hunt.py` is an AST-based dead-def/dead-param finder used to keep the core packages lean.
- The coding agent's sandbox degrades gracefully: its POSIX network isolation (`unshare`) is best-effort — kernels that restrict unprivileged user namespaces fall back to fresh-cwd + timeout isolation instead of crashing the run.
- Planning never performs device I/O: phone planning on a machine without `adb` still produces a reviewable plan (execution reports the real gap).
- Browser-walk e2e tests survive Edge's launcher handoff: the launcher process may exit 0 while the real browser keeps serving CDP, so the wait loop treats the **debugging port as the only truth** (exit only accelerates failure after a grace window), and the browser tree runs inside a **kill-on-close Job Object** so no zombie headless Edge processes survive a run to poison later ones.

**Web performance is measured, not guessed.** A lab-audit pass (loopback baseline → trace → targeted fixes → re-measure) landed four structural wins, each verified in the live page: gzip compression on every text asset (document 34→7 KB on the wire, styles −80%, scripts −74%), the ten app scripts load with `defer` plus `preconnect` for the font origins and a fonts URL slimmed to the weights the CSS actually uses, and the Ollama availability probe runs off the event loop (`asyncio.to_thread`, 0.5 s timeout) — a dead Ollama used to freeze the whole server for ~2 s on every page load; now it costs nothing and never blocks SSE.

UI and rendered-output quality have their own harnesses:

```powershell
python scripts/ui_audit.py --url http://127.0.0.1:8000/        # fit audit: 11 panels x 11 widths
python scripts/ui_audit.py --url http://127.0.0.1:8000/ --baseline old.css   # computed-style A/B
```

For a browser-driven smoke of the real page (guided-click result card, reduced motion), install the browser extra: `pip install -e ".[browser]"` then `python -m playwright install chromium`.

## 🛠 Development

- Docs live in [`docs/`](docs/) — start with [ARCHITECTURE.md](docs/ARCHITECTURE.md), [DEVELOPMENT.md](docs/DEVELOPMENT.md), and [STATUS.md](docs/STATUS.md); [DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) records recent work commit-by-commit
- [AGENTS.md](AGENTS.md) documents architectural conventions for contributors (one language layer, capability registration, verification-first actions)
- Lint with `ruff check src tests` (line length 100, configured in `pyproject.toml`)

## 🐞 Logging, Auditing & Debugging

- **Bug log** — every failed or unverifiable action lands in `data/bugs.json` with *what, where, when*; reviewable in the Vision tab with per-bug "Mark fixed", plus automatic resolution (with evidence) when a later pixel-diff proves the click worked
- **OpenCV-assisted verification** — click verification diffs frames with OpenCV when available (change-region localization around the click site), falling back to Pillow otherwise
- **Audit trail** — executed and denied desktop/browser/phone actions share one timestamped audit log
- **Event bus journal** — durable JSONL event journal plus in-memory journal for diagnostics
- **Live activity** — the web UI's single SSE channel (`/events/stream`) mirrors every subsystem's progress; every frame carries a **`correlation_id`** so two concurrent activities of the same family (a research in Explore while another runs from Chat, or two approved commands) interleave in the UI without mixing rows
- Log level via `NOVACONTROL_LOG_LEVEL`

## 📊 Current Status

| Area | State |
|---|---|
| Global Intelligence Layer | ✅ Complete — regression-tested (punctuation, case, typos, variations, context, multi-intent) |
| Desktop control | ✅ Working (Windows) — apps, folders, chains, dictation, vision-guided clicks |
| Phone control | ✅ Live-verified against a real device (Xiaomi Pad) — any installed app, in-app YouTube/Google/Maps searches, saved-contact texts/calls, files & folders, screenshots |
| Browser automation | ⚠️ Controller + Playwright runner implemented; real-page workflows still maturing |
| Voice | ⚠️ OS-native STT/TTS with graceful fallback; quality depends on platform speech services |
| GUI dashboard | ⚠️ PySide6 app present, secondary to the web UI |
| Vision tab | ✅ Working — guided clicks + pixel-diff verification (OpenCV fast path); optional multimodal vision model (Ollama/OpenAI/Gemini/OpenRouter) upgrades location to semantic |
| Scratch brain math | ✅ Worded arithmetic (incl. "what does 3 times 4 equal", "half of 10", "double 7", the fraction family — "two thirds of 300", "a fifth of 100" — spelled-out "add five and seven", scale operands "3 million times 2", variable assignment like "if x is 5, what is x times 3"), conversions, percentages, and JEE-style logs/trig/combinatorics/quadratics/AP — all offline, regression-pinned, with a Python↔JS parity corpus (`tests/test_math_parity.py`) that runs every phrasing through both the scratch engine and the routing explorer's mirror |
| Research answer quality | ✅ Site-chrome filter (consent banners, footers, trademark lines) applied before synthesis; scaffold-anchored topic extraction ("things to know about brics summit 2026" researches BRICS, not dictionary pages for "important"); topical-relevance guard rejects single-word hijacks on compound topics ("container garden" ≠ Docker); headline topics (trending chips, pasted titles) stay whole, search their proper-noun spine, and anchor relevance on those entities — a page matching only the headline's generic words cannot pass — pinned by `tests/test_synthesizer.py` + `tests/test_explore.py` |
| Trending topic suggestions | ✅ `GET /explore/trending` — live Google News RSS headlines (no API key, no hardcoded lists), cached 30 min, invalidated daily, rotated hourly; offline degrades to static help chips — pinned by `tests/test_explore_trending.py` |
| Web UI / responsive | ✅ Command-center redesign with fluid auto-fit — 11 panels verified overflow-free from 390px to 1920px, plus a computed-style A/B guard for stylesheet cleanups |
| Learn tab (teach & recall) | ✅ Typed facts persist as durable knowledge and are recallable in chat — pinned by `tests/test_build_learn.py` |
| Build tab (code planning) | ✅ Coding agent (draft → run in sandbox → read real errors → fix, 3 rounds) with visible step trace; language-aware plans, derived artifact names, editor + Save-to-disk — pinned by `tests/test_code_agent.py`, `tests/test_build_learn.py` |
| Build tab (multi-file projects) | ✅ `Plan The Project`: JSON file-map drafting, entry-point sandbox runs with real error feedback, per-file Copy/Save, Saved Artifacts strip + sandbox re-runs — pinned by `tests/test_build_project.py` |
| Cloud LLM | ✅ OpenAI-compatible / Gemini / Groq / **Claude (native `/v1/messages`)** presets; Test Connection (no-save ping), System-panel token usage + last-error surfacing, Explore synthesis follows the active brain; model picker for local Ollama — pinned by `tests/test_integrations_llm.py`, `tests/test_brain_mode_and_tasks.py`, `tests/test_vision_model_config.py` |
| Self-improvement | ⚠️ Telemetry + sandboxed previews implemented; fully autonomous improvement is *not* enabled |

See [docs/STATUS.md](docs/STATUS.md) for the phase-by-phase history.

## ⚠️ Limitations

- Desktop automation is **Windows-only** today (PowerShell, Start Menu, `shell:` URIs)
- Vision-guided clicks work without a vision model via OCR/landmarks, but semantic location needs one configured (Ollama vision model or cloud key)
- The offline scratch brain's research synthesis is deterministic — *sourced* reports need a configured LLM/online provider; without a model the answer is assembled from cleaned snippets, so its depth tracks what search returns
- Chrome filtering removes site furniture from snippets, but it cannot invent missing facts: a topic whose search results are all low quality still yields a shallow answer
- Trending topic suggestions track Google News top stories: very new stories may have thin coverage for a few minutes, and the rotation cadence (30-min refetch, hourly window) is fixed rather than user-configurable
- Template research answers quote source snippets, so bullets can be terse; connecting an LLM (Vision panel → model config) upgrades synthesis to flowing prose
- Phone texts/calls never send/dial themselves — by design
- No packaged binaries yet; run from source

## 🗺 Roadmap

- [ ] Cross-platform desktop automation (macOS/Linux)
- [ ] Vision-verified Steam/library UI state (today only the launch is verified)
- [ ] Non-Ollama local vision-model runtimes (llama.cpp / LM Studio); per-application learned UI maps
- [ ] Packaged desktop distribution
- [ ] Expanded cloud-LLM presets and model routing (Claude multimodal for Vision — the `/v1/messages` image-content shim)
- [ ] Trending-chip shuffle (exclude seen topics on demand) and a user-selectable news edition in Settings
- [ ] LLM-powered chat synthesis: flowing prose answers when a model is configured, template fallback otherwise
- [ ] Phone companion app (AccessibilityService over WebSocket — see [docs/PHONE_COMPANION_APP.md](docs/PHONE_COMPANION_APP.md)) and wireless-adb pairing for cable-free control

## 🤝 Contributing

1. Read [AGENTS.md](AGENTS.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
2. Add/extend tests for any new capability — every subsystem has hermetic tests
3. Run `python -m pytest tests/ -q` and `python -m mypy src` before proposing changes
4. Keep language understanding in the Global Intelligence Layer — don't add raw-string matching inside subsystems

## 📄 License

Proprietary — all rights reserved by the NovaControl Contributors. No standalone LICENSE file is distributed yet; contact the project maintainers for reuse terms.

## 👤 Project

Built and maintained by the **NovaControl Contributors** as a local-first exploration of agentic computer control: one brain, many capabilities, honest verification.
