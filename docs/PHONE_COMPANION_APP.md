# NovaLink — AccessibilityService companion app for phone mode

*Research + architecture sketch. No code in this repo changes; this is the plan.*

## Why

Phone mode today rides adb: every action needs USB debugging enabled, and the
cable unless wireless pairing is set up. An on-device companion app removes
both requirements: no Developer Options, no USB, no adb. It also unlocks what
adb can never do well — **reading the semantic UI tree** (element names and
resource IDs instead of OCR over screenshots).

NovaControl's phone layer was designed for this: `PhoneCommandRunner` is a
runtime-checkable Protocol with just `status()` and `run(action)`, and
`NoopPhoneRunner`'s next-steps text already says *"Install Android platform
tools **or a companion phone app**."* The companion app is a third runner
implementation, not a rewrite.

## What AccessibilityService actually grants (research summary)

From the [official guide](https://developer.android.com/guide/topics/ui/accessibility/service):

| Capability | API | Phone-mode use |
|---|---|---|
| Read the UI tree | `AccessibilityNodeInfo`, `rootInActiveWindow` | Verify actions, find elements by id/text — replaces OCR |
| Click / set text on nodes | `performAction(ACTION_CLICK / ACTION_SET_TEXT)` | Semantic taps and typed messages, no coordinates |
| Inject gestures | `dispatchGesture(GestureDescription)` (API 24+) | Coordinate taps/swipes where no node exists |
| Global actions | `performGlobalAction` — BACK, HOME, RECENTS, TAKE_SCREENSHOT (API 28+), LOCK_SCREEN, … | Home, back, app switch, screenshots |
| Screenshot | `takeScreenshot()` (API 30+) | Vision-guided flows; pre-30 needs MediaProjection (one dialog) |
| Overlay windows | `TYPE_ACCESSIBILITY_OVERLAY` | A visible "NovaControl is acting" banner — used for transparency only |

Config: manifest service with `BIND_ACCESSIBILITY_SERVICE` + XML
`accessibility-service` element (`canRetrieveWindowContent`, `canPerformGestures`,
minimal `accessibilityEventTypes`). **No root, no adb, no bootless hacks.**

### The honest constraints

- **Android 13+ "Restricted settings"**: apps sideloaded outside the Play
  Store are blocked from enabling accessibility until the user opens App
  Info → ⋮ → *Allow restricted settings*. Every user hits this once during
  onboarding; the app must detect the block and show these exact steps.
  (Source: Esper, Kaspersky.) A Play release avoids the block but brings
  policy scrutiny for non-accessibility use of the API — sideload is the
  right first distribution.
- **Battery / OEM killers**: MIUI, EMUI, Oppo/Realme aggressively kill
  background services. Foreground service + battery-optimization exemption
  request + per-OEM autostart instructions in-app.
- **One toggle, total power**: the same API powers stalkerware (keylogging
  via `TYPE_VIEW_TEXT_CHANGED`, overlay phishing, auto-clicking permission
  dialogs — see the 2026 Chocapikk analysis). Our design must be visibly the
  opposite: subscribe to **window-state events only** (no text-change events,
  ever), never read arbitrary input content, overlay used only to show
  activity, every action mirrored in the PC audit log and announced on the
  phone. The app ships source-open.
- **SMS send**: some OEMs require the send-button tap anyway — we already
  plan to tap it via the tree, and the PC approval gateway (`RiskLevel.CRITICAL`)
  gates the action before it ever leaves the desktop.

### Prior art

[droidVNC-NG](https://github.com/bk138/droidVNC-NG) (open source, F-Droid)
ships exactly this combination: MediaProjection for screen capture +
an accessibility input service for remote touch, no root. Validates the
architecture and the permission UX.

## Architecture

```
┌──────────────────────────── PC (this repo) ────────────────────────────┐
│  NovaBrain → PhoneController (unchanged: approval + audit)             │
│        │                                                              │
│        ▼                                                              │
│  CompanionPhoneRunner  (new, implements PhoneCommandRunner)           │
│    status() → connected? adapter="companion"                          │
│    run(action) → JSON over the socket → await reply (timeout)         │
│        │                              ▲                               │
└────────┼──────────────────────────────┼───────────────────────────────┘
         │ WebSocket (phone dials OUT)  │ command / result JSON
┌────────▼──────────────────────────────┴───────────────────────────────┐
│  Phone (Kotlin, ~1 screen + 3 components)                             │
│  ForegroundService ── holds the outbound WS to ws://<pc>:8000/phone/ws│
│  CommandDispatcher ── JSON → typed commands                           │
│  NovaAccessibilityService ── gestures, tree, screenshot (toggle in OS)│
│  MainActivity ── pairing code entry, permission dashboard, status     │
└───────────────────────────────────────────────────────────────────────┘
```

**Phone dials out, PC never dials in.** The desktop already owns a stable
address and port (the NovaControl UI server); the phone is the device whose
IP changes and whose firewall is irrelevant. Same-LAN only for v1.

**Pairing & auth**: PC shows a 6-digit pairing code in the phone panel
(reusing the existing API-token machinery). Phone sends
`{pair: code, device_name}` once; the server replies with a long-lived
session token; the phone stores it. Subsequent messages carry the token.
Unpaired sockets can only send `pair`.

**Command envelope** (PC → phone):

```json
{"id": "a1b2", "op": "send_text", "args": {"contact": "Mom", "body": "hi"},
 "deadline_ms": 8000}
```

Reply (phone → PC): `{"id": "a1b2", "ok": true, "detail": {...}}` — mapped by
the runner into the `exit_code` / `output` dict shape
`PhoneController.execute_action` already consumes. Failures map to
`ok:false, error:"..."` → `PhoneActionStatus.FAILED`.

### Operation set (maps 1:1 to existing PhoneAction types)

| op | Phone-side mechanism | Notes |
|---|---|---|
| `ping` / `status` | none | battery, a11y enabled, screen on |
| `open_app` | plain `startActivity` (package manager lookup by label) | no a11y needed |
| `call` | `Intent.ACTION_CALL` (permission) | falls back to dialer |
| `send_text` | resolve contact (ContactsContract) → open SMS with body prefilled → find send node in tree → `ACTION_CLICK` | the workflow adb does today, minus adb |
| `tap` / `swipe` | `dispatchGesture` | coordinates from PC vision |
| `global` | `performGlobalAction` | back / home / recents / screenshot |
| `screenshot` | `takeScreenshot` (API 30+), MediaProjection fallback (one-time dialog) | JPEG bytes (base64) → PC vision pipeline |
| `read_screen` | serialize a11y tree → JSON (id, text, bounds, clickable) | **new superpower**: deterministic verification + element location, no OCR |
| `contacts` | ContactsContract query | keeps `resolve_contact_prefix` PC-side logic working |

**Verification without screenshots**: after `send_text`/`open_app`, the phone
answers with tree evidence (`current_package`, `node_id:X visible:true`), so
`execute_action` can report verified success the way desktop actions do with
pixel-diff — deterministic and instant.

## Phone-side components (Kotlin, minSdk 26, target latest)

1. **`NovaAccessibilityService`** — config XML requests window-state events
   only, `canRetrieveWindowContent`, `canPerformGestures`. Exposes the
   command primitives; refuses anything not matching the dispatcher's op set.
2. **`LinkForegroundService`** — WebSocket client (reconnect with backoff),
   holds a persistent notification "NovaControl bridge active", requests
   battery exemption, routes messages to the dispatcher.
3. **`CommandDispatcher`** — op → implementation, per-op permission checks,
   result JSON. Pure and unit-testable on the JVM.
4. **`MainActivity`** — pairing, status, permission board (a11y toggle,
   contacts, call, battery, notifications), OEM autostart hints.

## PC-side components (this repo, small)

- `CompanionPhoneRunner(PhoneCommandRunner)` in `src/novacontrol/phone/` —
  mirrors `AdbPhoneRunner`'s contract; `status()` reports
  `adapter="companion"`, connected device name.
- `/phone/ws` WebSocket endpoint in the API app (pairing handshake +
  authenticated command channel; the app already has WebSocket support).
- Runner selection: companion when connected, else adb, else noop — surfaced
  in the phone panel with which bridge is live.

## Milestones & effort

| M | Delivers | Rough size |
|---|---|---|
| M1 | WS link + pairing + status + `open_app` + `ping` end-to-end | app skeleton, ~1–2 days |
| M2 | Tree read + `send_text` + `call` + `contacts` + verification | the core value, ~2–3 days |
| M3 | Gestures + `screenshot` + PC-vision hookup | ~1–2 days |
| M4 | Hardening: restricted-settings onboarding, OEM killers, reconnection, audit mirroring | ~1–2 days |

Testable on your tab immediately (it runs modern Android, so the
restricted-settings flow must be built in M4, not skipped).

## Risks

- **Sideloading friction** (13+ restricted settings) — onboarding screen with
  exact steps; unavoidable for a sideloaded accessibility app.
- **OEM battery management** — mitigation is known (foreground service,
  exemptions, autostart docs) but per-device.
- **Service toggled off by user/OS** — `status()` shows it; PC falls back to
  adb/noop and says so.
- **Security surface** — source-open, no text-event subscription, activity
  overlay, audit mirroring; the PC approval gate stays in front of every
  action exactly as it is today.

## Recommendation

Build it as the third runner — the seam makes it additive, and M1 alone
proves the transport. Wireless adb remains a worthwhile cheap fallback for
phones the user doesn't want to sideload on; they compose (both runners can
coexist, `status()` picks the live one).
