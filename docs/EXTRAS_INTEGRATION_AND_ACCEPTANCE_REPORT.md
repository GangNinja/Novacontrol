# Extras — Integration and Acceptance Report (NovaControl)

- Date: 2026-11-28
- Scope: Extra 1 (companion + workspace) and Extra 2 (isometric UI) in
  NovaControl.
- **Outcome: no NovaControl file was modified. The tree is occupied by the
  active DF-6 task.** Everything below is audit, concurrency evidence, and a
  ready-to-apply plan.

---

## A. Concurrency evidence

Captured at the start of this session (21:17–21:25 IST) and used to decide what
could be touched:

| Signal | Observation |
| --- | --- |
| Working tree | `C:/Users/NARASIMHA/OneDrive/Desktop/NovaControl` on `main` at `afa5ff6`, with a large uncommitted change set |
| Worktrees | the shared tree plus `…/NovaControl/novamarket` on `feature/novamarket` (a genuinely separate directory) |
| Active processes | `python -m pytest …` started 21:17:21; `uvicorn … --port 6003` at 21:21:29; a Playwright driver at 21:21:42; further `python.exe` at 21:24:53 |
| Files written during the session | `docs/DF6_TEST_SUITE_RECOVERY_REPORT.md` 21:17; `tests/test_novaforge_policy.py` 21:23; `tests/test_novaforge_gameplay_api.py` 21:24 |
| DF-6 state in the tree | **PARTIAL**, awaiting a live run (`docs/DF6_*` reports) |

Conclusion: **DF-6 and this task share one working tree**, and that task is
actively writing and running suites in it. Part A forbids modification. No
`git reset`, `clean`, `stash`, `revert`, commit, push or deploy was performed.

NovaMarket lives in its own worktree directory, so its extras were implemented
there without touching NovaControl's files. The only shared thing is the git
object database, and no index or branch operation was issued.

## B. What the audit found (short form)

Detailed in the two feature reports; the material findings:

1. **Extra 1 exists** as an untracked web module (`companion.js`, 727 lines) with
   real event wiring (`run()`, SSE progress, voice) and 24 tests, previously
   accepted 18/18. It **does not** implement this mission's eight states:
   `ANALYZING` and `UNCERTAINTY` are absent, `working`/`responding` stand in for
   `EXECUTING`/`SUCCESS`, and there is no transition table, so a success is shown
   for any `run()` that did not throw.
2. **Extra 2 exists** as the Atrium (`isometric.js`, 510 lines, 11 objects,
   vendored MIT Hairline, lazy load, 21 tests, previously accepted 14/14). It
   covers navigation and live status well; it does not cover the mission's
   empty/loading/error-state requirement, four objects are needlessly unmetered,
   connectors are unlabelled, and the figures ignore `prefers-reduced-motion`.
3. **Reference identity**: the brief's `hongkiat/Hairline` is a mislabel; the
   project matching "33 illustrations, ≈63.5 kB compressed" is
   `lucasmarkes/hairline`, MIT © 2026 Lucas Marques. The vendored copy is that
   package with its licence and attribution intact. Bible Strong Avatar Lab is
   AGPL-3.0-only; NovaControl consumes only a user-authored *export definition*,
   never its code (§A of `EXTRA1_COMPANION_ACCEPTANCE_REPORT.md`).
4. **PySide6 deviation stands**: NovaControl's served UI is the FastAPI web
   workspace. The Qt shell (`novacontrol/gui`) is a separate phase-11 dashboard
   and carries neither extra. Adding Qt-only implementations would create a
   second UI that the product does not serve, so the mission's Qt guidance is
   satisfied by the documented deviation, not by a parallel build.

## C. Why nothing was implemented here

- Modifying `web/static/js/companion.js`, `render-utils.js`, `voice.js`,
  `isometric.js`, `index.html`, `styles.css` or the two Extra test files would
  race the DF-6 session's writes and its suite run, and could corrupt the
  evidence it is producing.
- A third git worktree on the same branch would not isolate a running suite: the
  suites read the static tree from the checkout they run in, and the DF-6
  evidence refers to the current one.

## D. Ready-to-apply work (no further design needed)

Extra 1: add `analyzing` + `uncertain` rows and the transition table; source
research progress → ANALYZING and refusals/blocked plans/insufficient-evidence
reports → UNCERTAINTY; gate SUCCESS on a non-refusal payload; add the companion
workspace panel reusing `/operations/*` controls; extend the 24 tests.

Extra 2: reduced-motion option for the figures; two real badge rows; labelled
connectors; an `.iso-empty` treatment shared by empty/loading/error states;
extend the 21 tests.

Both plans are specified with file names and exact call sites in
`docs/EXTRA1_COMPANION_IMPLEMENTATION_REPORT.md` §D and
`docs/EXTRA2_ISOMETRIC_UI_IMPLEMENTATION_REPORT.md` §D.

## E. Verification performed here

- Read-only: `git status`, `git worktree list`, process list, file mtimes, the
  two extra modules, their tests' presence, the vendor licence/attribution, the
  index.html wiring, and the acceptance reports.
- **Not performed**: the NovaControl test suites, `mypy`, a live NovaControl
  session, screenshots. Running them in the occupied tree would produce
  interleaved results; none of them is claimed.

## F. Acceptance

| # | Requirement | Status |
| --- | --- | --- |
| 1 | Do not interfere with DF-6 | **MET** — no file touched, no git operation |
| 2 | Do not overwrite existing work | **MET** |
| 3 | Do not copy AGPL code | **MET** — data-format integration only, pre-existing |
| 4 | Do not misrepresent MIT assets | **MET** — licence and attribution verified in-tree |
| 5 | No fake controls, no fake activity | **PARTIAL (pre-existing)** — the companion has no transition guard; the Atrium's four non-live objects are honestly labelled |
| 6 | Companion matches real task events, all eight states | **NOT MET** — two states missing, success ungated |
| 7 | Activity timeline reflects real events | **MET (pre-existing)** |
| 8 | Workspace controls use authorized APIs | **PARTIAL** — operations controls exist in their own panel, not in a companion workspace |
| 9 | SVG renders, HiDPI, theme, no extra dependency | **MET (pre-existing)** |
| 10 | Interactive objects navigate to real workspaces | **MET (pre-existing; 11/11 previously verified live)** |
| 11 | No UI performance regression | **UNVERIFIED THIS RUN** — no measurement taken in the occupied tree |

**Verdict: NovaControl extras remain at their previous partial state. This
session added no code and claims no new acceptance.** The remaining work is
specified and deliberately deferred to avoid destroying the DF-6 task's work.
