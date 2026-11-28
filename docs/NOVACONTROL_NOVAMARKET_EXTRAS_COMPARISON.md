# NovaControl vs NovaMarket — Extras comparison

- Date: 2026-11-28
- Companion documents:
  - `novamarket/docs/EXTRA1_COMPANION_IMPLEMENTATION_REPORT.md`
  - `novamarket/docs/EXTRA2_ISOMETRIC_UI_IMPLEMENTATION_REPORT.md`
  - `novamarket/docs/EXTRAS_INTEGRATION_AND_ACCEPTANCE_REPORT.md`
  - `docs/EXTRA1_COMPANION_IMPLEMENTATION_REPORT.md` (NovaControl)
  - `docs/EXTRA2_ISOMETRIC_UI_IMPLEMENTATION_REPORT.md` (NovaControl)
  - `docs/EXTRAS_INTEGRATION_AND_ACCEPTANCE_REPORT.md` (NovaControl)

---

## A. The two architectures are not the same, and were not made the same

| | NovaControl | NovaMarket |
| --- | --- | --- |
| Served UI | FastAPI + hand-written ES modules (`web/static`, 28 panels) | Vite + React 19 + TypeScript (`novamarket/web`, 30+ pages) |
| Event backbone | one app-wide `EventBus` + `GET /events/stream` (SSE) | same server bus + `/events/stream`, with a session-cookie handshake |
| Task model | objectives → tasks → approvals (`/operations/*`), token-gated desktop/browser execution | market pipeline: ingestion → forecast → risk gate → paper lifecycle, plus a governed autonomy loop |
| Persistence | JSON state store + business namespaces | financial stores (ledger, books, paper) + audit log |
| Safety boundary | permission manager + single-use approval tokens | NM-08/NM-10 risk + paper-only execution, operator-token autonomy controls |
| Window model | one served page, panels | client-side routed SPA |

Nothing was copied across the boundary: no shared component library, no shared
state, no shared database, no reuse of NovaControl's task architecture inside
NovaMarket.

## B. Extra 1 — companion

| Aspect | NovaControl | NovaMarket |
| --- | --- | --- |
| State names | idle, listening, thinking, working, waiting, responding, speaking, error | idle, listening, thinking, analyzing, executing, success, uncertainty, error |
| ANALYZING | **missing** | present (forecast generation, validation, regime work) |
| UNCERTAINTY | **missing** | present (stale data, abstention, degraded model/policy, risk alert, paused loop) |
| Transition table | none | full table; illegal moves refused and counted |
| Success gating | any `run()` success | requires work in flight + `verified` (2xx write or confirming domain event) |
| Read completion | success-ish (`responding`) | a 200 on a read emits no state at all |
| REST vs SSE | SSE progress + `run()` | request lifecycle + SSE + status snapshot + operator focus |
| Workspace | activity panel (`#homeActivity`), operations board | dock + workspace: stage, in-flight, channel, agents, timeline, error, gated controls, state model |
| Pause/resume/cancel | operations panel (`/operations/*`) | companion → `/market/autonomy/pause|resume|stop`, operator-gated |
| Verification | 24 tests (pre-existing), not re-run | 24 companion tests in a 40-test suite, all passing |
| Status | **incomplete vs the eight-state requirement** | **complete**, with stated limitations |

The important divergence is philosophical as well as technical: NovaControl's
companion shows *what this tab is doing*; NovaMarket's shows *what the desk is
doing and how sure it is*, because a forecast desk must be able to say
"insufficient evidence" without calling it a failure.

## C. Extra 2 — isometric UI

| Aspect | NovaControl (Atrium) | NovaMarket (Atrium) |
| --- | --- | --- |
| Implementation | DOM buttons on a CSS stage + vendored Hairline SVG figures (lazy) | SVG plates + positioned HTML buttons, drawn from the app's own tokens |
| Dependency | `@lucasmarkes/hairline` 0.5.1, MIT, vendored (214 kB, lazy) | none added |
| Objects | 11 workspaces (Command Center, Chat, Explore, JARVIS, Vision, NovaForge, Build, Learn, System, Settings, CLI) | 8 facilities (ingest, validate, forecast, risk, portfolio, paper, autonomy, audit) |
| Live badges | 7 of 11 live (`/status`, `/novaforge/status`); 4 honestly non-live | 8 of 8 live from `/market/overview`, `/market/forecasts/status`, `/market/autonomy/status`, `/market/risk/alerts` |
| Connectors | unlabelled lines to the core | labelled dependency edges with stated payloads |
| Navigation | clicks the real sidebar nav item | calls the desk's own `navigate()` |
| Responsive | grid ≤900 px | grid ≤980 px |
| Reduced motion | not handled for figures | transitions disabled; map is static by design |
| Empty/loading/error states | not covered | not covered in the map itself; the desk's existing states are unchanged |
| Tests | 21 (pre-existing), not re-run | 16 in the 40-test suite, passing |
| Status | **implemented, small gaps** (reduced motion, 2 badges, labels, empty states) | **complete**, with no pixel review |

## D. Shared visual principles (kept deliberately consistent)

- Dark technical surfaces, one accent, one gold warning tone, one danger tone.
- Mono type for machine values (badges, ids, timestamps), sans for prose.
- Motion is a state change, never ambient noise; both honour reduced motion.
- An absent measurement is *said*, never rendered as zero.
- Every interactive object is a real control with an accessible name; decoration
  is `aria-hidden`.

They are **not** one design system: NovaControl's tokens are cyan on navy
(`--accent #00f0ff` family), NovaMarket's are teal/blue/gold on a deeper navy
(`--accent #55d6c2`, `--blue #6fa9ff`, `--gold #e7b96f`). Branding, information
architecture, and domain language stay separate.

## E. Licensing summary

| Asset | Licence | Use |
| --- | --- | --- |
| `lucasmarkes/hairline` (@0.5.1) | MIT © 2026 Lucas Marques | vendored in NovaControl only, licence + attribution in `static/vendor/hairline/` |
| Bible Strong Avatar Lab | AGPL-3.0-only | **no code used anywhere**; NovaControl reads a user-exported `.avatar.json` definition only |
| `hongkiat/Hairline` (brief) | n/a | not the project described; the quoted figures match lucasmarkes/hairline |
| NovaMarket isometric art | first-party | drawn from the app's tokens; no attribution obligation |

## F. Where each project stands

| | Extra 1 | Extra 2 |
| --- | --- | --- |
| NovaMarket | **PASS** (limitations stated) | **PASS** (limitations stated) |
| NovaControl | **INCOMPLETE** — 6 of 8 states, no transition guard; plan ready | **PARTIAL** — functional and accepted previously; 4 gaps listed; plan ready |

NovaControl's work is blocked on the DF-6 session that shares its working tree,
by the mission's own concurrency rule. NovaMarket's was implementable in its
separate worktree and is done, tested and live-verified.
