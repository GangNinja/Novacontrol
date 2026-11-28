# Extra 2 — Interactive Isometric UI (NovaControl): re-audit and blocked implementation

- Date: 2026-11-28
- Status: **AUDITED, IMPLEMENTATION BLOCKED** — the NovaControl working tree is
  being modified by the active DF-6 task. No NovaControl file was changed.
- Evidence classes: **code**, **live** (previous report only), **test**
  (previous report only; suites not re-run here — §E).

---

## A. What already exists (verified in the tree)

| Artefact | State |
| --- | --- |
| `src/novacontrol/web/static/js/isometric.js` (510 lines) | untracked, present — the "Atrium" |
| `src/novacontrol/web/static/index.html` — nav item `atriumPanel`, `#isoScene`, `#isoStatusSource`, `#isoArtStatus` | present (lines 41, 227–241) |
| `src/novacontrol/web/static/vendor/hairline/` — `index.js` (213,894 B), `LICENSE`, `ATTRIBUTION.md` | present, MIT © 2026 Lucas Marques |
| `tests/test_extra2_isometric.py` (659 lines, previously 21 tests) | untracked, present |
| `docs/EXTRA2_ISOMETRIC_ACCEPTANCE_REPORT.md` | untracked; claims 14/14 PASS (2026-11-02) |
| `.nc_extra2/` — vendor probe evidence | present (Nov 1–2) |

The Atrium is a real navigation surface: 11 objects, each a `<button>` that
clicks the sidebar's own `.nav-item[data-panel=…]`, with badges from `/status`
and `/novaforge/status`, lazy-loaded figures, poll cleanup on leave, and a
grid fallback below 900 px.

## B. Reference verification (re-done this session)

The brief names `github.com/hongkiat/Hairline`. Checked 2026-11-28:

- `lucasmarkes/hairline` is the project matching the quoted facts — MIT © 2026
  Lucas Marques, **33 figures**, "63.5 kB gzipped for all thirty-three", no
  dependencies, ESM only, `@lucasmarkes/hairline@0.5.1`.
- The vendored bundle in NovaControl is that package (33 exports, MIT licence
  file, attribution file recording the one modification — the removed source-map
  comment).
- No AGPL code is present in this path.

So the previous selection and its licence handling remain **correct**; only the
brief's repository path is mislabelled.

## C. Gap analysis

| Requirement | Existing | Gap |
| --- | --- | --- |
| Main dashboard / workspace navigation | 11 objects incl. Command Center | met |
| Empty states, loading/transition states, error/recovery states | only the Atrium's own failure copy ("figures unavailable — the map still navigates") | **no isometric language in the app's empty/loading/error surfaces** |
| Research workspace, project overview, agent activity | Explore/Learn objects are marked non-live (`data-live="false"`) and show a static blurb | **no live badge** even though `/status.app.tracked_tasks` and task lists exist |
| System architecture views | objects + connectors to the core | met, but the links are decorative lines (uniform), not labelled dependencies |
| Market data / portfolio / forecast (NovaMarket domain) | not applicable to NovaControl | correctly out of scope |
| Interactivity | every object navigates | met; no object opens a *detail* (dependencies, live execution state) |
| HiDPI / resizing / theme / reduced motion | SVG, tokens, a ≤900 px grid; the report pins sizes | met; reduced-motion was not part of the Extra 2 acceptance and is not handled for the figures |
| Accessibility | accessible names, keyboard Enter verified live previously | met |

The concrete, defensible gaps are therefore:

1. **No `prefers-reduced-motion` handling for the Hairline figures** (they honour
   pointer motion and an internal animation loop; `play:false` is passed but a
   global reduced-motion guard is not).
2. **Four objects are non-live without need** — Learn/Explore could carry a real
   count from `/status.app.tracked_tasks` or the knowledge index.
3. **No dependency labels or per-object detail** — the map shows *where* things
   are, not *what flows*.
4. **No isometric language in empty/loading/error states**, which the brief lists
   as required areas.

## D. Implementation plan (ready to apply once the tree is quiet)

**D1. Reduced motion.** In `isometric.js`, read
`window.matchMedia("(prefers-reduced-motion: reduce)")`, pass
`play: !reduced` into `ISO_FIGURE_OPTIONS`, and re-mount on change via the same
`change` listener pattern `companion.js` already uses. Add a Node-harness test
that the option value follows the media query.

**D2. Live badges for the four unmetered objects.** Extend `isoBadge` with two
rows reading fields the app already reports:
`explore` → `/status.app.tracked_tasks` (e.g. "N tracked tasks"),
`learn` → the knowledge index count from `/status.app.modules`/`/knowledge`
(only if a real field exists — otherwise leave `data-live="false"`; never
invent). Tests drive the pure `isoBadge` with real payload shapes.

**D3. Labelled dependencies.** Add `label` to each connector line and render it
as an `<text>` on the SVG (`aria-hidden`), plus a `title` on hover; keep the
button as the only interactive element.

**D4. Isometric empty/loading/error treatment** — one shared CSS class
(`.iso-empty`) using the same plate geometry as the map, applied to the Explore
and Learn panels' empty states and the Atrium's failure state. This is CSS plus
markup only, no new module.

**D5. Tests** — extend `test_extra2_isometric.py` with the reduced-motion
option, the two new badge rows, and the connector labels.

## E. Verification not performed

- `tests/test_extra2_isometric.py` and `tests/test_web_api.py` were **not**
  executed here: the DF-6 session is running the same tree's suites.
- No live Atrium session was opened by this work.

## F. Acceptance

| # | Requirement | Status |
| --- | --- | --- |
| 1 | Functional isometric surface with real navigation | **PASS (pre-existing, code-verified)** |
| 2 | Real values only, honest when absent | **PASS (pre-existing)** |
| 3 | Every listed visual area covered | **PARTIAL** — empty/loading/error states missing |
| 4 | Live badges where data exists | **PARTIAL** — 4 objects unmetered |
| 5 | Reduced motion | **NOT MET** for the figures |
| 6 | Dependency comprehension | **PARTIAL** — unlabelled connectors |
| 7 | HiDPI, theme, no extra dependency | **PASS (pre-existing: vendored MIT, lazy import)** |
| 8 | No DF-6 regression | **N/A — nothing changed** |

**Verdict: Extra 2 in NovaControl is implemented and previously accepted, but
does not yet cover every area this mission lists.** The gap list is small and
concrete; it is blocked by Part A, not by design uncertainty.
