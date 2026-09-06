# NovaControl

**An agentic computer-control and self-improving AI system** — one local brain that understands natural language, plans tasks, and actually *does* them: driving your desktop, your browser, and your Android phone, with vision-guided verification, a live web UI, and a built-in bug journal.

---

## What NovaControl Can Do Today

### 🧠 Global Intelligence Layer (GIL) — *understands what you mean, not what you typed*

Every input — chat, J.A.R.V.I.S commands, research — flows through ONE language layer before any subsystem sees it:

| You type | NovaControl understands |
|---|---|
| `open chrome`, `open chrome.`, `Open Chrome!`, `OPEN   CHROME`, `please open chrome`, `launch chrome`, `opn chrme` | **OPEN_APPLICATION(chrome)** — all identical |
| `take a screenshot`, `capture my screen` | **TAKE_SCREENSHOT** |
| `text mom on my phone saying hi` | **PHONE_SEND_TEXT(recipient: mom, message: hi)** |
| `open steam and go to library and launch gta v` | A 3-step plan: launch → in-app navigate → vision-guided click |
| `open it` (after opening Chrome) | **OPEN_APPLICATION(chrome)** — resolved from context |
| `do the same thing` | Re-runs the last intent from conversation memory |

- **Layered resolution:** deterministic fast path → typo correction (fuzzy) → learned variations → contextual/reference resolution → semantic LLM fallback → one precise clarification question. Trivial punctuation differences never wake the model.
- **Multi-intent decomposition:** "open notepad and take a screenshot" becomes two planned, ordered steps.
- **Confidence + clarification policy:** high confidence executes; ambiguous high-risk actions ask *"Which application — Chrome or Steam?"*, never a vague "please provide more info".
- **Learning loop:** fuzzy/contextual resolutions teach their phrase back to the registry, so the same typo takes the fast path next time.
- **Self-improvement telemetry:** every resolution, clarification, unknown intent, and failed entity resolution is recorded (`/intelligence`) and turned into human-readable improvement findings.

### 🗂 Capability Registry

31 registered capabilities spanning **desktop, phone, browser, research, chat, planning, automation, scheduling, memory, projects, self-improvement, and agents** — each declaring required entities, risk level (opening an app is LOW; closing one is MEDIUM), executor, and its **verification strategy**. The orchestrator derives execution and confirmation policy from this table, not from hard-coded call sites.

### 🖥 J.A.R.V.I.S — Desktop + Phone control in one tab

**Desktop (Windows):**
- Open **any** installed app — Start-Menu scan, known app aliases, plus spoken folders (`open downloads / documents / pictures / music / videos / desktop`)
- **In-app chains:** "open steam and go to library and launch gta v" → launches Steam, drives `steam://open/library` deep links, then vision-guided clicks
- **Dictation:** "open notepad and type meeting notes" — clipboard-staged paste with verification
- **"stop that"** — graceful WM_CLOSE of the last app, never a hard kill
- Every plan is previewed and requires an **approval token** minted server-side (or flip **Auto-approve & Run** in Settings)

**Phone (Android via adb):**
- Pair a tablet/phone over USB (USB debugging); the bridge auto-discovers adb even off PATH
- Real, verified actions: `open youtube on my phone` → launched and confirmed via foreground-window check
- **Binary-safe screenshots** pulled from the device to `data/screenshots/*.png`, PNG-magic-verified
- Texts open pre-filled — **nothing sends** and **no call dials** without you pressing the button on the device
- Full how-to-connect guidance built into the UI

### 👁 Vision tab — perceive → locate → act → verify

- **Describe Screen** — captures your real screen and reports what's visible
- **Guided Click** — type a target ("File", "Library", "Play GTA V"); the vision layer locates it (LLM region → OCR band → UI landmarks), moves the cursor, clicks, and saves before/after screenshots
- **Honest verification:** when a click's effect can't be confirmed, it records *unverified* — never fake success

### 🐞 Bug Log

Every failed or unverifiable action lands in `data/bugs.json` with **what, where, and when** — reviewable in the Vision tab with per-bug "Mark fixed" / "Clear fixed".

### 🔎 Research that streams live

Ask a research question in **Chat** ("what is the migration pattern of arctic terns") and it runs the same pipeline as the Explore tab — the UI shows **live stages** (searching → sources found → synthesizing → complete) instead of a static spinner, and the answer arrives as a sourced report with key points, sections, and follow-ups.

### 💬 Dual brain — your choice

- **Scratch brain:** fully local, zero-dependency — greetings, math, unit conversions, local knowledge, recommendations
- **Real LLM:** Ollama (auto-probed at boot) or cloud providers (OpenAI-compatible, Gemini, etc.) — one switch in Settings, per the brain-mode selector
- Cloud keys are stored locally and never returned by any API endpoint

### Plus

Event-driven core runtime · memory (SQLite) · projects · automation workflows · scheduler · planning engine · agent registry + coordinator · self-improvement engine (sandboxed previews before anything applies) · plugin marketplace · voice input/output · web UI (single SSE activity channel) · CLI

---

## Quick Start

Pure-Python FastAPI app — no build step. Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# Run the web app
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/** — try `open notepad and type hello`, `what is quantum tunneling`, or `take a screenshot on my phone`.

Run the tests:

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/ -q
```

Type-check:

```powershell
python -m mypy src --ignore-missing-imports
```

### Phone control setup

1. Download [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) and unzip (NovaControl also auto-finds it in `~/platform-tools`)
2. On the device: **Settings → About → tap Build number 7×** → **Developer options → enable USB debugging**
3. Plug in over USB → press **Connect Phone** in the J.A.R.V.I.S tab → accept the RSA prompt on the device

---

## Architecture

```
MESSY HUMAN INPUT
      ↓
GLOBAL INTELLIGENCE LAYER          ← normalize · typos · context · references
      ↓                                multi-intent · confidence · clarify
STRUCTURED INTENT
      ↓
CAPABILITY REGISTRY                ← risk · required entities · verifier
      ↓
ORCHESTRATOR (handle_request)
      ↓
PLANNER → SPECIALIZED CONTROLLER   ← desktop · phone · browser · explore ·
      ↓                               chat · planning · agents · memory
ACTION ENGINE → OBSERVE → VERIFY
      ↓ (on failure)
RECOVERY → RETRY → VERIFY
      ↓
BUG LOG (on failure) · MEMORY · RESPONSE
```

**One brain → many capabilities.** No subsystem parses raw user strings on its own; language understanding is a platform capability, like logging or security.

Key modules:

```text
src/novacontrol/
  intelligence/   Global Input Intelligence: normalize, intent rules,
                  context/references, capability registry, telemetry,
                  StandardResult envelope
  brain/          Scratch brain (fully local answers) + LLM chat + routing
  desktop/        Desktop controller, parser/planner, vision-guided control
  phone/          adb bridge: connect, apps, texts, calls, screenshots
  browser/        Playwright browser automation
  vision/         Screen understanding (LLM provider, heuristic fallback)
  agentcore/      Task interpreter, adaptive planner, verifier, recovery
  core/           Event bus, runtime, security (deny-by-default approvals),
                  bug log, audit trail
  api/            FastAPI surface + single SSE activity channel
  web/static/     The cyber-styled web UI (vanilla JS, no build step)
```

### Safety model

- **Deny-by-default approvals:** device actions execute only with a server-minted, single-use, expiring token issued for that exact plan
- **Auto-approve is opt-in** (Settings checkbox, default off)
- Destructive/ambiguous + high-risk → explicit confirmation
- Phone texts/calls are *pre-fill only* — the human always presses send/dial
- Self-improvement applies nothing without a sandboxed preview + approval

---

## Status

- ✅ Global Intelligence Layer: complete, regression-tested across capabilities (punctuation, case, typos, variations, context, multi-intent)
- ✅ Desktop control: all apps, folders, in-app chains, dictation, stop, vision-guided clicks
- ✅ Phone control: live-verified against a real device (Xiaomi Pad 5) — apps, screenshots, pre-filled texts/calls
- ✅ Vision tab, bug log, auto-approve, live research streaming, dual-brain switching
- ✅ 459 tests · mypy clean across 171 source files

## Repository Layout

See the module map above; `AGENTS.md` documents deeper architectural conventions for contributors.
