# NovaForge NF-24 — Real-Game State Understanding and Objective Recognition — Implementation Plan

**Phase:** NF-24. **Tagline:** Observe. Decide. Act. Verify. Evolve.
**Status:** **complete — PARTIAL verdict.** Implementation, regression testing and reporting are finished; the
live-game half is **BLOCKED** for the same external reason NF-23 recorded (`SteamAPI_Init failed` for the only
safe installed offline title), re-measured in this phase with Steam running. Fixtures and recorded frames are
**not** treated as live evidence. See `docs/NOVAFORGE_NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md` and §7–§8
below for what was actually built and where it deliberately differs from the plan.
**Companion documents:** `docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md` (the observation path this
phase consumes), `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md`,
`docs/NOVAFORGE_NF17_OBJECTIVE_PROGRESS_REPORT.md`, `docs/NOVAFORGE_NF18_GAME_KNOWLEDGE_MEMORY_REPORT.md`,
`docs/NOVAFORGE_NF21_OUTCOME_VERIFICATION_RECOVERY_REPORT.md`, `docs/NOVAFORGE_VERIFICATION.md`.

---

## 1. Repository and working-tree state at phase start

| Fact | Value |
|---|---|
| Branch / HEAD | `main`, 145 working-tree entries (59 modified tracked files, 86 untracked) — NF-01…NF-23 work, all uncommitted. Nothing is reset, stashed, reverted or committed by this phase. |
| Host | Windows 11, Intel Core Ultra 5 125H, Intel Arc iGPU, 16 GB RAM, Python 3.13 in `.venv`. |
| Observation path (NF-23) | Verified on a real **window**: router-selected backend, validated frames, dims = live window rect, pause on minimize with no frame, restore revalidated, `process_exited` on exit. |
| Live game (NF-23) | **BLOCKED**: Freedom Fighters' own launcher prints `SteamAPI_Init failed` and exits; Steam's own launch dies 2 s later; re-measured in the NF-23 verification pass. No real-game frame exists on this host. |
| Scope rule carried forward | No game launch in this phase unless NF-23 can observe one; no input; no external drive; no force-kill; gate ≥2 GB free and ≤92 % used. |

## 2. What already exists (read from source, not summaries)

| Component | Current capability | What NF-24 may NOT rebuild |
|---|---|---|
| `novaforge/state.py` (NF-16) | Profile-declared HUD regions, five HUD statuses (`recognized/absent/ambiguous/unreadable/unsupported`), interface summary, geometric entities, per-field `unknown` list, `quality` coverage, profile match/refusal, session-scoped changes with observation ids | the perception, OCR, entity or profile machinery — NF-24 extends the model, it does not add a second reader |
| `novaforge/state_service.py` (NF-16) | Newest validated frame only; refuses paused/ended observation, stale frame, superseded observation, concurrent analysis, timeout; bounded history; one event per reading | the freshness/supersession/pause gates — they are load-bearing (F-06/NF-23) |
| `novaforge/objectives.py` (NF-17) | Profile rules over state fields, explicit objective text, progress counters, completion/failure texts, conflicts, terminal corrections, evidence refs with observation ids | the objective interpreter or its rule model — NF-24 adds one evidence channel, not a second interpreter |
| `novaforge/knowledge.py` / `knowledge_service.py` (NF-18) | Evidence/provenance per record, contradiction handling, revalidation, state ingestion (`state_candidates`) | the knowledge store or its provenance rules |
| `perception` (Phase 21) | `SceneRepresentation` with `text: OcrText` (confidence when the reader reports one), objects, provider states | OCR/detection |

**Measured baseline (NF-16 report; to be re-measured in this phase):** ~1.2 s per reading (CPU OCR), 8-state
history, no model load. Confidence is `None` because the shipped Windows OCR reader reports none.

## 3. Gaps NF-24 must close

| ID | Gap | Evidence |
|---|---|---|
| **G-01** | **No scene classification.** The state says which HUD fields were read and whether a menu/dialog element is present, but never states the screen's kind (menu, loading, gameplay, pause, death, victory, defeat, unknown) or its evidence. | NF-16 report §5/§8; `GameState` has no scene field |
| **G-02** | **Transitions are unconfirmed.** A single low-confidence frame can flip a HUD value or interface state and the change is reported immediately; there is no N-of-M confirmation, no pending/contradicted state, and no distinction between an observed and an inferred transition. | NF-16 report §8 (“Changes … each carries … inferred: False”); no temporal validator in `state_service.py` |
| **G-03** | **Measured confidence is dropped.** `HudReading.confidence` exists but is never populated even when the OCR record carries a confidence; `GameState.confidence/uncertainty` are always `None`. | `_read_field` never sets `confidence`; NF-16 report §9 |
| **G-04** | **A repeated reading of the same observation is not distinguished from a new one**, so it can look like a transition and inflate history. | `state_service._analyze` only compares `observation_id` for supersession |
| **G-05** | **Objectives cannot consume a completion screen.** NF-17 accepts explicit text/counters only; a profile-verified victory/defeat scene is not admissible completion evidence even when the profile declares it. | `ObjectiveRule` has `completion_texts`/`failure_texts`, no scene channel |
| **G-06** | **Knowledge does not record the scene**, so the one durable fact about “what screen this was” has no provenance record. | `state_candidates` emits HUD/interface candidates only |
| **G-07** | **No live-game state evidence is possible on this host** (NF-23 blocker). The phase's live requirement therefore cannot be earned; it must be reported, not simulated. | NF-23 report §9/§14 |

Deliberately **not** in scope: action planning/execution (NF-19–NF-22), a semantic object detector, commercial
game profiles, bar/meter/map measurement, live gameplay input, and any second perception stack.

## 4. Implementation steps

1. **`novaforge/scene.py` (new, small):** `SceneKind` (`menu/loading/gameplay/pause/death/victory/defeat/unknown`),
   `SceneRule` (profile-declared: scene, optional HUD field, required text patterns), `SceneAssessment`
   (`kind`, `source`, `status` = `observed|inferred|unknown`, `confidence` only when measured, `evidence`,
   `alternatives`, `contradictions`), `assess_scene(...)` pure classifier, and `SceneTracker` implementing
   N-of-M confirmation (`confirmations`, default 2) with pending/confirmed/contradicted outcomes.
   Classification order: profile rules over recognized fields → unambiguous generic text cue (marked
   **inferred**, quoted) → `unknown`. A profile rule firing is `observed`; two rules naming different scenes is
   a contradiction, not a guess.
2. **`state.py`:** `SCHEMA_VERSION = "nf24.1"`; `GameProfile.scene_rules`; `GameState.scene` +
   `GameState.transition`; populate `HudReading.confidence` from the OCR record(s) inside the region when the
   reader reports one; `GameState.confidence` = mean of measured field confidences (else `None`);
   `unsupported`/`limitations` statements updated to say what is still not measured.
3. **`state_service.py`:** own a `SceneTracker`; ignore a repeated `observation_id` for transition purposes
   (reported as `tracking.reason`); reset the tracker on a new session; attach the transition to the state
   (`dataclasses.replace`), append a `scene_changed` change only when a transition is **confirmed**; include
   the scene in the analyzed event; restore the tracker from the persisted last state so a reload does not
   re-confirm a transition from scratch.
4. **`objectives.py`:** `EvidenceKind.SCENE`; `ObjectiveRule.scene_completion` / `scene_failure`; admissible
   only when the rule declares the scene AND the state's transition confirms it, with the evidence ref carrying
   the scene, confidence and observation id. A missing completion indicator stays “not complete”. No rule may be
   satisfied by a scene it did not declare.
5. **`knowledge.py`:** one `scene:` candidate per state whose scene is not `unknown`, carrying the observation
   id and `OBSERVED`/`INFERRED` method to match the assessment.
6. **UI:** one scene line (kind, confirmed/pending, evidence) in the existing state card; no second status
   vocabulary.
7. **Tests:** `tests/test_novaforge_nf24.py` (scene rules/unknown/contradiction, temporal confirmation,
   duplicate observations, OCR failure, confidence propagation, objective scene completion, knowledge
   provenance, service integration) plus updates to the NF-16 schema pin.
8. **Measurement:** scene classification and transition cost per reading (µs), end-to-end reading latency
   against the NF-16 baseline, memory, and the OCR-failure path.
9. **Reports:** this plan's closing section, `docs/NOVAFORGE_NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md`,
   and a current-results section in `docs/NOVAFORGE_VERIFICATION.md` in its existing format.

## 5. Risks and controls

| Risk | Control |
|---|---|
| Scene classification fabricated from weak text | Profile rules are declared and require the field to be `recognized`; generic cues are marked **inferred** with the cue quoted; no cue ⇒ `unknown`. |
| Temporary confirmation suppressing real transitions | Confirmation counts *consecutive agreeing* observations and is configurable; the raw assessment is always reported, only the *confirmed* scene waits. Contradictions reset the streak and are recorded. |
| Confidence invented on a host whose OCR reports none | `confidence` stays `None` unless the perception record carries one; `quality` remains explicitly heuristic. |
| Duplicate readings inflating transitions | Same `observation_id` never advances the tracker, and a non-contiguous observation `sequence` drops the unconfirmed run instead of counting across it. |
| A cue naming a screen from ordinary words | Cues are whole-word phrases, never bare screen nouns (`"defeat"` in an objective line would otherwise name the defeat screen). |
| Objective complete without evidence | Completion needs the rule's declared text **or** its declared, confirmed scene; absence of an indicator is never completion. |
| Live requirement quietly converted into a fixture claim | The report separates live (blocked) from fixture results in its own sections; no fixture is labelled live. |
| Unrelated tree changes | Only NF-24 modules/tests/docs are touched; `git status` before/after is recorded; no commit. |

## 6. Measurable acceptance criteria

* **Scene:** a profile rule classifies its declared screen as `observed` with evidence quoting the field and
  text; a screen with no rule and no unambiguous cue is `unknown`; conflicting rules are a recorded
  contradiction with `unknown` (never a guess).
* **Temporal:** one observation of a new scene is `pending`, not `confirmed`; the configured number of
  consecutive agreeing observations confirms it; an intervening contradiction resets the streak; a repeated
  observation id changes nothing.
* **Honesty:** every non-recognized field remains `unknown`/`unsupported` with a reason; `confidence` is `None`
  when nothing measured it; an OCR failure yields `unreadable` fields and an `unknown` scene.
* **Objectives:** a state that displays no objective indicator marks nothing complete; a declared, confirmed
  completion scene marks completion with a scene evidence ref carrying the observation id; an undeclared scene
  proves nothing.
* **Provenance:** the knowledge record for a scene cites the observation id and the method
  (`observed`/`inferred`).
* **Quality:** new tests pass; NF-16/NF-17/NF-18 and NF-23 suites stay green; both mypy views clean; API
  reference in sync if the API changed; measured latency stays in the same envelope as NF-16's baseline.
* **Live:** if (and only if) NF-23 can observe a real game, at least two distinct visible states or one
  meaningful transition are captured, interpreted and recorded with evidence. Otherwise the blocker is stated
  with the exact measured failure.

## 7. Completed work (as built)

| Step | Status | Where |
|---|---|---|
| 1. `novaforge/scene.py` — kinds, validated rules, assessment, N-of-M tracker | **done** | `scene.py` (~510 lines): `SceneKind`, `SceneRule`, `SceneAssessment`, `SceneTracker`, `assess_scene` |
| 2. State model — schema bump, `scene_rules`, `scene`/`transition`, measured confidence | **done** | `state.py`: `SCHEMA_VERSION = "nf24.1"`, `GameProfile.scene_rules` (validated against declared fields), `GameState.scene`/`.transition`, `confidence`/`uncertainty` from the scene's measured mean, `scene_changed` change kind, honest limitations |
| 3. Service — own tracker, session reset, persistence/restore, event + status | **done** | `state_service.py`: `scene_confirmations` (default 2), tracker passed to the builder, restored from the store, reset on a new session, `scene`/`transition` on the analyzed event, `scene` + `counts.scene_transitions` in `status()` |
| 4. Objectives — `EvidenceKind.SCENE` and the attribution rule | **done, shape changed** (see §8.2) | `objectives.py`: `_scene_outcome`, scene evidence on a sole displayed candidate, single remaining objective closed by a confirmed outcome screen, `unattributed` note otherwise |
| 5. Knowledge — one scene candidate with provenance | **done, narrower** | `knowledge.py`: `situation:scene:<kind>` **only** for a confirmed transition, method `observed`/`inferred`, provenance line |
| 6. UI — one scene line in the state card | **done** | `novaforge.js`: `stateSceneText()` exported for the Node test, `Scene` fact row, scene in history rows |
| 7. Tests + schema pin | **done** | `tests/test_novaforge_nf24.py` (57 tests incl. a Node group; 41 at the first pass, 16 added by the verification pass — §10), `tests/test_novaforge_state.py` pin → `nf24.1` |
| 8. Measurement | **done** | recorded frames 939.1–979.3 ms; live window 734.6–738.9 ms; NF-16 baseline 868.9–922.1 ms; objective 0.058 ms; knowledge 12.9 ms (report §10) |
| 9. Reports | **done** | this plan, `docs/NOVAFORGE_NF24_REAL_GAME_STATE_UNDERSTANDING_REPORT.md`, `docs/NOVAFORGE_VERIFICATION.md` Appendix B |

Six defects were found and fixed during implementation and verification (parameter shadowing in the
classifier, a duplicate flag no caller passed, a first confirmation missing from the change list, wrong
alternatives for multi-cue frames, one mypy typing defect, and a victory screen closing several objectives at
once). Each is pinned; details and evidence are in the report's §11. The verification pass then found six
more (five with new pins, one a cost-model fix) — report §15.1, plan §10.

## 8. Deliberate deviations from this plan

### 8.1 Per-field confidence is not fabricated (was step 2)

The shipped OCR reader constructs every `OcrText` with `confidence=None`, so a `HudReading.confidence` could
only be invented from geometry. The **state-level** measured mean (NF-21's `scene_confidence`) is reported
instead, and stays `None` with its reason when nothing was measured. Pinned in
`StateSceneTests.test_confidence_is_the_measured_mean_or_nothing` and its negative twin.

### 8.2 The scene channel lives on the state, not on the objective rule (was step 4)

The plan proposed `ObjectiveRule.scene_completion` / `scene_failure`. A rule-level flag would let a profile
attach any screen to any objective it liked; the real ambiguity is about attribution, not declaration. The
scene is declared by the perception profile (`scene_rules`) and a confirmed `victory`/`defeat` screen is
admissible objective evidence **only when exactly one objective can own it** — one displayed candidate, or one
remaining objective that is no longer displayed. More than one ⇒ `unattributed` and no status change. Pinned by
`test_two_current_objectives_are_never_closed_by_one_banner` and
`test_two_displayed_objectives_are_never_closed_by_one_banner`.

### 8.3 Knowledge records confirmations only (was step 5)

A single frame's scene is an observation, not a durable fact about a game's flow, so `scene:` records are
written **only** when the transition was confirmed — that is also what keeps a steady victory screen from
re-writing the same record every frame (measured: knowledge candidates 3 → 4 → 3 over three recorded frames).

## 9. Outcome

A full **PASS** requires the live half; the delivered verdict is **PARTIAL — PASS on fixture- and
recorded-frame-verified state understanding and objective/knowledge integration, BLOCKED on live-game state
interpretation**, with the blocker measured three ways and the deterministic evidence reported separately.
NF-25 has not been started.

## 10. Verification pass (after the phase was first reported)

NF-24 was re-checked against its own objectives, with new adversarial inputs rather than a re-run of the same
cases. Six defects were found and fixed — five of them pinned by new tests (57 in the suite now, up from 41),
the sixth being a behaviour-preserving cost fix:

1. **Substring cue/rule matching** fabricated screens — `"game over"` inside `"game overlay"`, and the bare
   nouns `"victory"`/`"defeat"` matched an objective line (`"Defeat the boss"`) that would then have *failed the
   objective* once confirmed. Cues and rule patterns are now whole-word phrase patterns, and a bare screen noun
   is no longer a cue at all (a one-word banner belongs to a profile rule over a declared region).
2. **An unbounded contradiction log** (one string per candidate flip, for the life of the process) is now
   capped at `MAX_TRACKED_CONTRADICTIONS = 8`, oldest first.
3. **A run could bridge a capture interruption** — frames 1 and 5 agreeing confirmed a transition across three
   observations that were never captured. `SceneTracker.observe(..., sequence=)` drops the unconfirmed run on a
   non-contiguous sequence, records why and marks the transition `interrupted`; both travel in the persisted
   payload so a restart resumes the sequence rather than starting fresh.
4. **Objective C was incomplete** — a profile could not declare required observation capabilities and a rule
   had no confidence requirement. `GameProfile.required_capabilities` (validated against
   `OBSERVATION_CAPABILITIES`) now refuses a profile whose OCR requirement is unmet, and `SceneRule.min_confidence`
   holds a rule back on an unmeasured or low-confidence reading — a weaker generic cue may not subsequently
   decide the same claim.
5. **The panel hid the reason** for an unknown screen (`unknown (unknown)`) and said nothing about a run that had
   restarted after a gap; `stateSceneText` now shows both.
6. **Rule patterns were compiled per frame** while the design (and the N-07 fix) says the phrases are compiled
   once. `_cue_pattern` is now `lru_cache`d, so one pattern serves cues and profile rules for the life of the
   process — no behaviour change, and the 57-test suite runs 0.83 s → 0.31 s.

Test/type results for the pass (all green): NF-24 suite **57 passed**; state/objectives/knowledge/observation/
NF-23 **277 passed**; UI/API/perception **279 passed + 94 subtests**; novaforge/planning/verification **248
passed + 53 subtests**; `mypy src` and `mypy src --platform win32` both clean (452 files). No route changed, so
`docs/API.md` is unchanged. The live-game criterion was **not** re-attempted (nothing about the blocker changed)
and remains **BLOCKED**; the verdict stays **PARTIAL**.

Full evidence, including which defects matter for honesty, is in the report's §15.
