# Extra 2 — Interactive Isometric UI (the Atrium)

**Acceptance report.** Scope is Extra 2 only. Extra 1 (animated companion) and the
NovaForge workstream were inspected and preserved; no Extra 3 / NovaMarket work was
started.

- Report date: 2026-11-02
- Application under test: NovaControl web workspace (`novacontrol.api.app:create_app`)
- Runtime: `python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port 8001`
- Evidence classes are labelled: **live** (observed in the running app), **test**
  (automated), **code** (inspection only).

---

## A. Resource assessment — Hairline

**What it actually is.** <https://github.com/lucasmarkes/hairline> is a set of
isometric *line figures* published as an ESM package (`@lucasmarkes/hairline`,
version **0.5.1**). Each export is a factory `figure(element, options)` that draws
one animated SVG figure into that element and returns a handle with `destroy()`.
It is an artwork/animation layer, **not** a UI framework: it supplies no scene,
navigation, layout, state, hit-testing or panels. The Atrium therefore builds the
scene, layout, connectors, focus order, badges and navigation in NovaControl's own
ES modules, and Hairline draws the objects.

**Verified contents and licence** (read from the vendored copy in the tree):

| Item | Verification |
| --- | --- |
| Licence file | `static/vendor/hairline/LICENSE` — first line `MIT License`, `Copyright (c) 2026 Lucas Marques` |
| Bundle | `static/vendor/hairline/index.js`, 213,894 bytes, single ESM file, no runtime dependencies, no `sourceMappingURL` |
| Exported figures | 33 (`basket` … `vault`), counted from the bundle's `export { … }` block |
| Attribution | `static/vendor/hairline/ATTRIBUTION.md` records project, author, source URL, package version 0.5.1, MIT, what was modified (only the source-map comment), and the eleven figures used |
| Page credit | the Atrium footer links Hairline with “MIT © Lucas Marques” |

**Reuse obligations.** MIT requires the copyright notice and permission notice to
travel with the copies; both are vendored in `LICENSE`, and the attribution file
states the modification. No npm toolchain is introduced (the project has none by
policy); the published bundle is vendored and imported lazily.

**Integration approach.** The figures are used only as object art. NovaControl owns
the stage, coordinates, labels, live badges, keyboard behaviour, persistence and
navigation. This is stated plainly in `ATTRIBUTION.md` rather than presented as a
“Hairline UI”.

**Spec deviation, documented.** The Extra 2 text refers to “the current PySide6
application”, but the repository's actively served application — and the surface
that the project's normal startup command runs — is the FastAPI web workspace. The
isometric interface is implemented there. No Qt code was modified.

---

## B. Implementation

### Files

| File | Change |
| --- | --- |
| `src/novacontrol/web/static/js/isometric.js` (new) | the Atrium: one object table, scene build, status derivation, live refresh, lazy figure loading/cleanup, activation lifecycle, `window.NovaAtrium` door |
| `src/novacontrol/web/static/index.html` | `Atrium` sidebar nav item and `#atriumPanel` (stage, refresh, source pill, art status, credit), module script with the standard `?v=__NC_ASSET_VERSION__` marker |
| `src/novacontrol/web/static/styles.css` | isometric stage, object plates, connectors, tones, grid fallback at ≤900 px |
| `src/novacontrol/web/static/app.js` | `setupAtrium()` during init, before `restoreActivePanel()` |
| `src/novacontrol/web/static/vendor/hairline/` (new) | vendored bundle, `LICENSE`, `ATTRIBUTION.md` |
| `src/novacontrol/api/middleware.py` | defect fix (§C6): the page shell and `/static/` assets no longer consume or hit the API rate-limit budget |
| `tests/test_extra2_isometric.py` (new) | 21 tests / 22 subtests over the real module and the served assets |
| `tests/test_api.py` | 2 tests pinning the rate-limit scope |

### Rendering technology and why

The web workspace already owns navigation, state and panels, and Hairline is
ESM/SVG, so the smallest maintainable integration is: DOM buttons positioned on a
CSS stage, one `<svg>` for the connectors, and Hairline's SVG figures mounted
inside each button. No new dependency, no canvas loop, no second navigation
mechanism. The 214 KB bundle is imported **lazily** on the first Atrium visit
(`import(/static/vendor/hairline/index.js)`), so it is not part of the initial
page load.

### The eleven objects and what each really does

Every object is a `<button>` whose action is `document.querySelector('.nav-item[data-panel="<target>"]').click()`
— literally the sidebar's own nav element — so panel switching, the persisted
panel, scroll reset and the active-nav highlight all stay in the one existing
implementation.

| Object | Opens | Live badge source |
| --- | --- | --- |
| Command Center (hub, centre) | `homePanel` | `/status.app.runtime_started`, `.modules` |
| Chat | `chatPanel` | `/status.app.brain` (effective mode, `model_configured`) |
| Explore | `explorePanel` | none — states “research desk”, marked `data-live="false"` |
| J.A.R.V.I.S | `jarvisPanel` | `/status.app.phone_bridge` (availability, state, devices) |
| Vision | `visionPanel` | `/status.app.vision_pipeline` (provider/OCR availability) |
| NovaForge | `novaforgePanel` | `/novaforge/status` (`active_session`, `capture_supported`) |
| Build | `buildPanel` | `/status.app.brain.model_configured` + model name |
| Learn | `learnPanel` | none — “knowledge & training”, non-live |
| System | `systemPanel` | `/status.app.modules.length`, `.tracked_tasks` |
| Settings | `settingsPanel` | none — “preferences”, non-live |
| CLI | `cliPanel` | none — “raw responses”, non-live |

Honesty rules enforced in code and tests: a missing field becomes
“status unavailable”, never a zero; an unmetered object says what it opens and is
rendered in italics with `data-live="false"`; a failed `/status` marks live objects
unavailable while the map keeps navigating; a failed `/novaforge/status` only
affects the NovaForge object.

### Lifecycle

`setupAtrium()` builds the scene once, binds refresh/visibility/nav delegation
idempotently, and activates if the saved panel is already open. Activation starts
one 20 s poll and the lazy figure import; leaving the panel clears the poll and
destroys every figure handle; a hidden tab pauses polling (the existing
`document.hidden` rule) and a visible tab refreshes. A failed figure import is
reported as “figures unavailable — the map still navigates” and is retried with a
cache-busting URL.

---

## C. Runtime verification

**The application was launched** with its normal uvicorn command (port 8001), the
page was driven in the Chromium preview, and the same build was exercised through
the rendered accessibility tree and real clicks.

### C1. Render and live state (live)

- Sidebar shows **Atrium**; the panel opened automatically because
  `novacontrol.activePanel` was already `atriumPanel` (persistence verified).
- 11 objects rendered, 11 mounted Hairline figures, “figures live”, source pill
  `live · 11:37:50 AM`.
- Observed badges, all from the reported payloads of that run:
  `runtime online · 17 modules`, `scratch brain · no model`, `research desk`,
  `no device paired`, `OCR only`, `ready · capture`, `scaffold only`,
  `knowledge & training`, `17 modules · 1143 tasks`, `preferences`, `raw responses`.
- `/static/vendor/hairline/index.js` served as `text/javascript` (213,894 bytes);
  `/` and `/static/js/isometric.js` carry the exact no-cache trio and the injected
  `?v=74e58450d72a` token.

### C2. Every object's real operation (live)

Each object was clicked after returning to the Atrium; the active panel and the
sidebar nav were read back immediately:

```
core→homePanel  chat→chatPanel  explore→explorePanel  jarvis→jarvisPanel
vision→visionPanel  novaforge→novaforgePanel  build→buildPanel  learn→learnPanel
system→systemPanel  settings→settingsPanel  cli→cliPanel        (11/11 correct)
```

The unknown-target path (no such nav item) returns `false` and raises the toast
“That workspace is not available in this build.” — covered by the Node harness
because the shipped scene contains no such object.

### C3. Keyboard (live)

The Vision object was focused through the page and **Enter** was pressed with real
browser input: `activePanel === visionPanel` and the sidebar nav became active.

### C4. Failure and recovery (live)

With `/status` unreachable in the page, a refresh produced:

- Command Center: `status unavailable` (`data-tone="error"`)
- Learn: `knowledge & training` unchanged, `data-live="false"`
- Source pill: `status unavailable — navigation still works` (`pill error`)

Navigation still worked in that state. After the endpoint returned, the next
refresh restored `runtime online · 17 modules` and `live · 11:44:13 AM`.

The same honest state was observed for real during the rate-limit window in §C6
(`status unavailable`, figures still live), and it recovered on the next refresh
(`live · 11:51:52 AM`).

### C5. Leave, return, reload, sizes (live)

- Leaving the Atrium: `active=false`, `polling=false`, figures `0`.
- Returning: `active=true`, `polling=true`, `figures=11`, `artState="ready"`.
- Reload with the Atrium persisted: panel restored, figures mounted again.
- Layout measured with real bounding boxes:

| Viewport | Mode | Objects | Overlaps | Off-stage | Figures |
| --- | --- | --- | --- | --- | ---: |
| Desktop (default panel) | absolute isometric stage | 11 | 0 | 0 | 11 |
| 900 px | wrapped grid | 11 | 0 | 0 | 11 |
| 520 px | 2-column grid (`221px 221px`) | 11 | 0 | 0 | 11 |

### C6. Defect found during verification, and fixed

**Symptom.** On a fresh page the figures reported “figures unavailable — the map
still navigates”, and `/static/vendor/hairline/index.js` returned **429**.

**Root cause.** `RateLimitMiddleware` (120 requests / 60 s per client) counted
*every* HTTP request, including the page and its assets. The web UI's own polling
(`/system/telemetry`, `/health`, `/novaforge/*`) plus ordinary panel switching can
exhaust the budget; the lazily imported figure bundle was then refused, and even a
reload of `/` could be refused.

**Fix (`src/novacontrol/api/middleware.py`).** `GET`/`HEAD` for `/` and `/static/`
pass through without being counted and without rate-limit headers. API routes keep
their limit.

**Verification (live).** After 130 `/status` calls in one window:
`/status → 429` (limit still enforced), `/ → 200`,
`/static/vendor/hairline/index.js → 200` (213,894 bytes),
`/static/js/isometric.js → 200`. The same page at 520 px then reported
`figures live`, 11 mounted figures, and recovered badges (`live · 11:51:52 AM`).

New pins: `RateLimitScopeTests` in `tests/test_api.py` (assets never limited;
asset traffic does not consume the API budget).

### C7. Regression checks

| Check | Result |
| --- | --- |
| `tests/test_extra2_isometric.py` | **21 passed**, 22 subtests |
| `tests/test_api.py` (incl. 2 new limiter tests) | **5 passed** |
| `tests/test_web_api.py` (post-fix) | **75 passed**, 75 subtests |
| `tests/test_web_platform.py`, `test_extra1_companion.py`, `test_route_shape_contract.py` | **88 passed**, 505 subtests |
| Combined web/Extra-1/API/route run before the limiter fix | 163 passed, 580 subtests |
| `mypy src/novacontrol/api/middleware.py` | clean |
| Live chat workflow after using the Atrium | `hello` → scratch greeting, 411 chars, “LLM: Not required”, 1.2 ms |
| Server startup | clean; stderr empty during the session |

### C8. Not verified / limitations

- The **full** 2,254-test suite and `mypy src` (whole tree) were not run; only the
  targeted suites above. The middleware change is isolated to the rate limiter.
- The 429 window in §C6 was produced deliberately with a request burst; it is the
  same limiter path an ordinary heavy session reaches, and it is now closed for
  assets.
- Hairline's figures are decorative art: they do not hit-test, so the button is the
  target. That is by design and is why every object's accessible name is
  “Open <workspace>”.
- The PySide6 shell was neither used nor modified; the isometric UI lives in the
  served web workspace.

---

## D. Binary acceptance scorecard

| # | Acceptance criterion | Verification | Observed result | Status |
| --- | --- | --- | --- | --- |
| 1 | Selected resource inspected; licence and obligations verified before reuse | Vendored `LICENSE`, bundle export count, `ATTRIBUTION.md`, page credit | MIT, © 2026 Lucas Marques, 33 exports, no source map, attribution present | **PASS** |
| 2 | Interface is a functional part of the running application, not a prototype | Launched the app; Atrium nav + panel render in the served page | Renders in the real workspace, restored from the saved panel | **PASS** |
| 3 | All required interactive objects exist and are clearly interactive | Real clicks on all 11 objects; DOM is `<button>` with accessible names | 11/11 opened their own workspace; active nav followed | **PASS** |
| 4 | Each object performs its real NovaControl operation | Read the active panel and nav after every click | 11/11 exact target panels; same nav click as the sidebar | **PASS** |
| 5 | Displayed state comes from real services, never fabricated | Compared badges with `/status` and `/novaforge/status` payloads | Exact matches (17 modules, 1143 tasks, scratch brain, OCR only, …) | **PASS** |
| 6 | Objects without a metered state do not claim live data | Badge table + rendered `data-live` flags | 4 unmetered objects marked non-live and italic | **PASS** |
| 7 | Failures and unavailable capabilities are reported honestly | Forced `/status` failure; observed live 429 window | “status unavailable”, error pill, guide text; navigation intact | **PASS** |
| 8 | Navigation to **and from** represented workspaces works | Clicked each object, returned via sidebar; restored on reload | All round trips succeeded; panel persisted | **PASS** |
| 9 | Keyboard access and focus behaviour | Focused an object; real Enter key | Target panel opened; visible focus ring | **PASS** |
| 10 | Renders correctly and stays usable at supported sizes | Bounding-box measurement at desktop, 900 px, 520 px | 11 objects visible, 0 overlaps, 0 off-stage in all three | **PASS** |
| 11 | Assets load correctly; failures are handled, and nothing blocks the UI thread | Lazy import, served MIME/size, forced import failure, real 429 run | Figures mount once; failure states stated; fixed limiter restores loading | **PASS** |
| 12 | Cleanup/lifecycle: leaving stops work, no duplicate mounts, no stale state | Left and re-entered; counted timers, handles and markers | Poll cleared, 11 handles destroyed, remount 11, no duplicates | **PASS** |
| 13 | Existing behaviour and parallel work preserved | Targeted regression suites + live chat after Atrium use | All suites green; chat still answers; Extra 1 files untouched | **PASS** |
| 14 | Evidence-based report with binary scorecard | This document | — | **PASS** |

**Total:** 14 objectives — **14 PASS, 0 NOT PASS**.

---

## E. Overall verdict

**PASS.** The Atrium is an interactive isometric workspace map in the running
NovaControl application: eleven real workspaces are reachable by pointer and
keyboard through the application's own navigation, live badges are derived from
real endpoints (and honestly withhold when they cannot be), failures are stated
rather than disguised, figures are lazily loaded and cleaned up, and the layout
holds from desktop down to a 520 px panel. One real defect uncovered by this
verification — static assets sharing the API rate-limit budget — was fixed and
re-verified.
