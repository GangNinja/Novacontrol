# NovaForge NF-24 — Real-Game State Understanding and Objective Recognition — Verification Report

**Phase:** NF-24. **Tagline:** Observe. Decide. Act. Verify. Evolve.
**Companion documents:** `docs/NOVAFORGE_NF24_IMPLEMENTATION_PLAN.md` (this phase's plan, updated with the
completed work and its two deliberate deviations), `docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md`
(the observation path this phase consumes, and the blocker it inherits), `docs/NOVAFORGE_NF16_GAME_STATE_UNDERSTANDING_REPORT.md`,
`docs/NOVAFORGE_NF17_OBJECTIVE_PROGRESS_REPORT.md`, `docs/NOVAFORGE_NF18_GAME_KNOWLEDGE_MEMORY_REPORT.md`,
`docs/NOVAFORGE_NF21_OUTCOME_VERIFICATION_RECOVERY_REPORT.md`, `docs/NOVAFORGE_VERIFICATION.md` (Appendix B
carries this phase's current results in that document's own format).
Earlier reports are **not** edited here; where an earlier limitation is now resolved, it is named as resolved
and the earlier text is left standing.

---

## 1. Executive summary

NF-24 turns "the pipeline can read HUD fields from a frame" into "the pipeline can say **what screen this is**,
**whether a change to it is a fact yet**, and **which objective that closes** — with the evidence each claim
rests on".

**Delivered, with evidence in this document**

| Item | Result |
|---|---|
| Scene model (`menu/loading/gameplay/pause/death/victory/defeat/unknown`) | `novacontrol/novaforge/scene.py`; a scene is `observed` only from a **declared profile rule over a recognized field**, `inferred` from an unambiguous generic cue (quoted), otherwise `unknown` — never a guess |
| Temporal confirmation | `SceneTracker`, N-of-M consecutive agreeing observations (default **2**), pending/confirmed/contradicted/duplicate outcomes, contradictions recorded; a repeated observation id can never count twice |
| State model | `GameState.scene` + `GameState.transition`, `GameProfile.scene_rules`, `SCHEMA_VERSION` **nf16.1 → nf24.1**; measured `confidence`/`uncertainty` (mean of the provider confidences a scene actually measured, `None` when none was) |
| NF-17 integration | `EvidenceKind.SCENE`; a confirmed victory/defeat screen closes an objective **only when attribution is unambiguous** (one displayed candidate, or one remaining objective that is no longer displayed) — never a crowd of them |
| NF-18 integration | a confirmed screen becomes a `situation` record (`scene:<kind>`) whose evidence method is `observed` or `inferred`; an unconfirmed frame records nothing |
| Panel | one `Scene` fact row (`kind (observed via profile_rule) · confirmed … · pending 1/2 — reason`), plus the scene in history rows; a Node-driven test of the real `novaforge.js` pins the sentence |
| Tests | `tests/test_novaforge_nf24.py` — **57 tests** in the current suite (41 at the first pass; the verification pass added 16 — see §15), including a Node case group against the real panel module |
| **Live real-game state interpretation** | **BLOCKED**, with the blocker re-measured under changed conditions (Steam running): the only safe installed offline title's launcher still prints `SteamAPI_Init failed` and exits rc=1 in 0.5 s. Full evidence in §9. No real-game frame or state is claimed. |
| Live evidence that *was* obtainable | a **real window** (the disposable harness, explicitly not a game) read live through the product's own services: two fresh observations, `unknown` scene with its reason, `transition: none`, 735–739 ms — the negative the phase requires (§9.2) |
| Recorded-frame evidence | three consecutive frames of the captured `GAME MENU` window: `menu` **observed via `game-menu@1`**, `pending → confirmed → steady`, HUD read `SINGLE PLAYER OPTIONS QUIT GAME CREDITS`, 939–979 ms (§9.3) |

**Final verdict: PARTIAL — PASS on scene/transition/objective/knowledge integration, BLOCKED on
live-game state interpretation.**

Every deterministic criterion is satisfied and regression-pinned, three implementation defects and one
over-permissive attribution rule were found by this phase's own tests and fixed (§11), and the one criterion
that needs a real game window is reported with a *measured root cause* that persists when the environment
changed (§9.1) — not with a guess, and not by substituting a fixture. Per §13 of the phase brief this is
therefore not a full PASS: `Live-game state interpretation has been demonstrated` does not hold on this host.

---

## 2. Initial findings from NF-13A–NF-23 (reviewed, not assumed)

Read from source and from the prior reports, then re-measured where the report's claim was the thing this
phase would depend on.

| Prior finding | Status at NF-24 start | How it was checked |
|---|---|---|
| NF-15 observation path: validated frames from a real window, pause on minimize with **no** frame, restore revalidated, `process_exited` on exit | **Holds** | NF-23 §7; re-exercised live in §9.2 (two fresh observations of a real window, backend `printwindow`, dims = live rect) |
| NF-23 F-06: a paused/ended observation must not produce a state reading | **Holds (load-bearing for NF-24)** | `state_service._pause_refusal`; NF-24's own service tests run only in `observing` |
| NF-16: HUD fields are read only from profile-declared regions; five statuses; unknown fields listed with a reason | **Holds** | `state.py` source; §9.3 recorded-frame read (2/2 declared fields recognized) |
| NF-16 §9: "no confidence is reported because no component measures one" | **Partially resolved** | the *scene* now carries the provider confidences that **were** measured (`scene.confidence`, NF-21's `scene_confidence`), and the state reports that mean. On this host the shipped Windows OCR reader reports none, so the measured value is still `None` — and the payload says why (§5) |
| NF-16: changes are observed diffs, `inferred=False` | **Extended, not replaced** | a `scene_changed` entry joins the list only on a **confirmed** transition, and carries `inferred=True` when the scene was inferred from a cue (§4) |
| NF-17: completion needs an explicit completion text or a counter reaching a declared target; disappearance is not completion | **Holds, extended by one evidence channel** | §6; NF-17's evaluation is still 32/32 with **0 false completions** after the change |
| NF-18: every record has evidence + provenance; contradictions are not silently overwritten | **Holds** | §6; NF-18 evaluation 51/51 |
| NF-21: `NOT_VERIFIABLE` is a real verdict | **Holds, unchanged** | no NF-24 code path can promote a scene into a verified *action outcome*: the scene channel is objective-level evidence, and NF-21's verifier still reads its own plan/step evidence (§8) |
| NF-23 §9/§14: no real game window can exist on this host | **Still true** | re-measured with Steam running (§9.1) |

**Not resolved by this phase (carried forward honestly):** no commercial-game profile exists (one screenshot
of one menu is not a layout this build can claim), so scene rules exist for the two fixture layouts and the
`GAME MENU` fixture only; gameplay cannot be inferred from a generic cue (deliberate — see §5.2); per-field
confidence stays `None` on a host whose OCR reports none.

---

## 3. Files and components changed

| File | Change |
|---|---|
| `src/novacontrol/novaforge/scene.py` (**new**, ~510 lines) | `SceneKind`, `SceneRule` (declared: scene + HUD field + text patterns, validated), `SceneAssessment` (`observed|inferred|unknown`, evidence/alternatives/contradictions, confidence only when measured), `SceneTracker` (N-of-M confirmation, duplicate detection, contradiction bookkeeping, restore), `assess_scene(...)` |
| `src/novacontrol/novaforge/state.py` | `SCHEMA_VERSION` → `nf24.1`; `GameProfile.scene_rules` (+ validation that a rule names a field the profile declares, + payload); `GameState.scene`/`GameState.transition` (+ payload); `build_game_state(scene_tracker=…)` computes the assessment, asks the tracker, appends `scene_changed` on a confirmation, reports the measured confidence/uncertainty, and states the remaining limits; scene rules added to the `nc-forge-mission@1` (victory/defeat banner) and `game-menu@1` (menu row) fixtures |
| `src/novacontrol/novaforge/state_service.py` | owns the `SceneTracker` (`scene_confirmations`, default 2); resets it on a session change; passes it to the builder; persists the tracker's last transition and **restores** it on boot; adds `scene`, `scene_status`, `scene_source`, `scene_confirmed`, `transition` to the analyzed event; adds a `scene` block and `counts.scene_transitions` to `status()`; honest scene limitations |
| `src/novacontrol/novaforge/objectives.py` | `EvidenceKind.SCENE`; `_scene_outcome(state)` (only the frame that CONFIRMED an outcome screen carries it); the scene joins a candidate's completion/failure evidence **only when it is the sole candidate**; a single remaining objective no longer displayed is closed/failed by the confirmed screen; more than one current objective with a confirmed outcome screen is recorded as `unattributed` and nothing is closed |
| `src/novacontrol/novaforge/knowledge.py` | one `situation` candidate `scene:<kind>` for a **confirmed** scene transition (content `{scene, after}`, evidence method `observed`/`inferred`, provenance "NF-24 confirmed scene transition"); `scene` counts as outcome evidence for an objective's `verified` flag |
| `src/novacontrol/web/static/js/novaforge.js` | `stateSceneText()` (kind, how it was read, confidence when measured, alternatives, confirmed scene, pending x/y with the reason, contradiction), a `Scene` fact row, the scene in history rows, and the export the Node test drives |
| `tests/test_novaforge_nf24.py` (**new**, 57 tests) | rules/classification/contradiction, whole-word cue matching, tracker semantics (incl. sequence gaps and the bounded contradiction log), state payload + change list + confidence, service persistence/session reset/events, objective scene evidence (completed/failed/ambiguous/stale), knowledge candidates, panel sentence (Node) |
| `tests/test_novaforge_state.py` | the schema pin moved to `nf24.1` with the reason in the assertion's comment |
| `docs/NOVAFORGE_NF24_IMPLEMENTATION_PLAN.md` | updated with completed work (§7) and the two deliberate deviations (§8.1, §8.2) |

**No API route was added or changed**, so `docs/API.md` is byte-identical and
`python scripts/generate_api_reference.py --check` reports *in docs\API.md: in sync*.

---

## 4. Architecture and data flow

```
NF-23 capture ─► NF-15 observer (validated frame only)
                    │
                    ▼
            GameStateService.analyze()                 ← refuses paused/ended/stale/superseded
                    │ new observation (deduplicated by observation_id)
                    ▼
            perception scene (real OCR + regions; no model)
                    │
                    ▼
            build_game_state()
                 ├─ HUD readings (declared regions, 5 statuses)
                 ├─ assess_scene(hud, text, profile)     ← rules → cue → unknown
                 ├─ SceneTracker.observe(scene, observation_id)
                 ├─ confidence = scene.confidence (measured mean, else None)
                 └─ changes (+ scene_changed only when confirmed)
                    │
        ┌───────────┼───────────────────────────┐
        ▼           ▼                           ▼
  novaforge.state.analyzed   ObjectiveService.observe   KnowledgeService.record_state
        (event)              → EvidenceKind.SCENE       → situation:scene:<kind>
                                 (one-candidate rule)      (confirmed only)
```

One pipeline, one tracker, one assessment per observation. The tracker is state the *service* owns (so a run
spans frames), while the assessment is a pure function of the frame (so a panel can always show what this
frame looked like even while a change is pending).

---

## 5. State representation and evidence model

### 5.1 What a reading now contains

| Field | Meaning | When it is absent/unknown |
|---|---|---|
| `scene.kind` | the named screen | `unknown` — a real answer, with a reason in `scene.evidence` |
| `scene.status` | `observed` (declared rule over a recognized field) \| `inferred` (generic cue) \| `unknown` | — |
| `scene.source` | `profile_rule` \| `text_cue` \| `none` | — |
| `scene.evidence` | quotes the field + text that fired the rule, or the cue, or the reason nothing did | — |
| `scene.alternatives` / `scene.contradictions` | two rules/cues naming different screens | empty when unambiguous |
| `scene.confidence` | mean of the confidences the perception providers measured | `None` on a host whose OCR reports none (this one) |
| `transition.state` | `none` \| `pending` \| `confirmed` \| `contradicted` \| `duplicate` | `none` when no tracker is attached (with the reason) |
| `transition.confirmed` / `candidate` / `confirmations` / `required` / `previous` / `reason` / `contradictions` | the stable answer, the candidate run, and why | — |
| `confidence` / `uncertainty` | the measured scene mean and `1 - mean` | `None` when nothing was measured |
| `changes[kind="scene_changed"]` | a **confirmed** transition, `inferred` when the scene came from a cue | absent otherwise |

### 5.2 Honest boundaries (deliberate, and pinned)

* **Gameplay has no generic cue.** "Absence of a menu" is not evidence of gameplay, and words like "health"
  appear on menus. Gameplay is therefore only ever `observed` through a profile rule, and no commercial
  profile exists — so this build can say "menu/loading/victory/defeat/pause/death" from cues and cannot claim
  "gameplay" without a declared layout.
* **Generic cues name a screen only when exactly one scene is named.** `"NEW GAME"` plus `"MISSION COMPLETE"`
  in one frame is two screens: the answer is `unknown` with both alternatives and a contradiction, not a pick.
* **A rule must name a declared field.** `GameProfile.__post_init__` refuses a scene rule over a field the
  profile does not declare: a scene read from free text would be a guess wearing a field's clothes.
* **A first confirmation has no previous scene** and is still recorded as a change (`unknown → victory`), with
  the pre-confirmation assessment always visible in `scene`.
* **Cues are whole words, and a bare screen noun is not a cue** (§15.1 N-07): `"game over"` must not match
  inside `"game overlay"`, and `"Defeat the boss"` — an objective line — must never name the DEFEAT screen.

### 5.3 Confidence: what is measured and what is not

`GameState.confidence` is the perception scene's own measured mean (NF-21's `scene_confidence`: the mean over
the provider confidences that were **measured**, `None` if none were). Two consequences, both stated in the
payload: when the reader reports per-line confidences the state reports their mean and the limitation sentence
says it is *not* a calibrated probability that the state is correct; when it reports none (this machine's
Windows OCR) the state reports `None` and the older NF-16 sentence stands. Per-**field** confidence is not
populated — see §5.3.1 for why that is a deliberate deviation from the plan.

#### 5.3.1 Deviation 1 — per-field confidence is not fabricated

The plan proposed filling `HudReading.confidence` from the OCR records inside a region. The shipped reader
(`perception/engine.py`) constructs every `OcrText` with `confidence=None`, so such a field could only be
filled by inventing a number from geometry. The state-level measured mean is reported instead, and the plan
now records this deviation rather than the report pretending the field is filled.

#### 5.3.2 Deviation 2 — the scene channel is not declared per objective rule

The plan proposed `ObjectiveRule.scene_completion` / `scene_failure`. Implemented instead: the **scene itself**
is declared (by the perception profile's `scene_rules`), and a confirmed outcome screen is admissible objective
evidence only under an attribution rule that is stronger than a per-rule flag — exactly one objective may be
closed by it (§6). A rule-level flag would have let a profile attach a scene to whichever objective it liked;
the attribution rule is about the *state*, which is where the ambiguity actually lives.

---

## 6. Supported game profiles and verified capabilities

| Perception profile | Scene rules declared | Verified how |
|---|---|---|
| `nc-forge-mission@1` (fixture scenario, 1280×720) | `victory` on `notification` (`mission complete`, `mission accomplished`); `defeat` on `notification` (`mission failed`, `mission over`) | deterministic fixtures: completed / failed / two-objective ambiguity / stale banner |
| `game-menu@1` (GAME MENU fixture window, 2880×1800) | `menu` on `menu` (`single player`, `new game`, `load game`, `quit`) | **recorded real frame**: OCR read `SINGLE PLAYER OPTIONS QUIT GAME CREDITS` → `menu` **observed** (§9.3) |
| `nc-forge-tick@1`, `nc-forge-harness@1` | **none** (their fields are a tick counter and a harness state word, which name no screen) | live harness window → `unknown` with the reason (§9.2) |

No commercial-game profile was added, and the phase brief's warning is respected: two declared fixture layouts
are not "a large number of unverified profiles", and no file claims a commercial title's HUD.

**Objective/knowledge capability verified:** a confirmed victory screen completes the single current objective
with a `scene` evidence ref (observation id + confirmation sentence); a confirmed defeat fails it; two current
objectives are left `NOT_VERIFIABLE` with an `unattributed` note; a victory screen that stays up adds no new
event and no re-dated evidence; a confirmed scene produces exactly one `situation` knowledge candidate on the
confirming frame and none on a steady frame (§7, §10).

---

## 7. Automated tests and exact results

All commands run from the repository root with `PYTHONPATH=src` and the project's `.venv` (Python 3.13,
Windows 11).

> **Historical record.** The table below is the **first pass**, exactly as it ran. The suite has since grown
> from 41 to 57 tests; §15 records the verification pass, its new groups and the current counts. Numbers here
> are left standing on purpose.

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge_nf24.py -q` | **41 passed** (includes a Node group against the real `novaforge.js`; skipped cleanly if node were absent) |
| `python -m pytest tests/test_novaforge_nf24.py tests/test_novaforge_state.py -q` | **97 passed** |
| `python -m pytest tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py -q` | **222 passed** |
| `python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_api.py tests/test_novaforge_nf23.py tests/test_novaforge_observation.py -q` | **283 passed, 94 subtests passed** |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_closed_loop.py tests/test_novaforge_gameplay.py tests/test_novaforge_planning.py -q` | **237 passed, 33 subtests passed** |
| `python -m pytest tests/test_novaforge_verification.py tests/test_novaforge_autonomy.py tests/test_web_api.py -q` | **296 passed, 120 subtests passed** |
| `python -m pytest tests/test_novaforge_nf24.py tests/test_novaforge_objectives.py tests/test_novaforge_verification.py -q` (after the attribution tightening) | **211 passed, 27 subtests passed** |
| `python -m mypy src` | **Success: no issues found in 452 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 452 source files** |
| `python scripts/generate_api_reference.py --check` | **docs\API.md: in sync** |
| `python scripts/novaforge_nf16_state_eval.py` | **fixtures 20/20 (accuracy 1.0)**; recorded frames 3/3 analyzed, 8/8 declared fields recognized, 868.9–922.1 ms |
| `python scripts/novaforge_nf17_objective_eval.py` | **32/32 (accuracy 1.0, false completions 0)**; service 7/7 guarantees; 0.058 ms avg |
| `python scripts/novaforge_nf18_knowledge_eval.py` | **51/51**; 0 verified-without-evidence, 0 verified questions |

**What each test group actually pins (not just counts)**

* **Classification** — a declared rule over a recognized field is `observed` with the field and matched text in
  the evidence; two firing rules are `unknown` + alternatives + a contradiction; a generic cue is `inferred`
  and quotes itself; no rule and no cue is `unknown` *with the reason*; unavailable OCR is named as the reason.
* **Temporal** — one frame is `pending` (1/2, confirmed `unknown`); two consecutive agreeing frames confirm;
  `required=1` confirms immediately; the same observation id read twice is `duplicate` and does not advance;
  an unreadable frame neither confirms nor resets; a candidate that flips is `contradicted` and restarts at 1;
  a persisted run is restored and a replay after restart is still a duplicate.
* **State** — `scene`/`transition` travel in `to_dict()`; without a tracker the transition says so; a confirmed
  change appears **once** in `changes` (and a steady screen never does); a cue-based change is `inferred`;
  confidence is `None` when nothing measured it and the measured mean when one did.
* **Service** — two readings are needed before `status()["scene"]["confirmed"]` moves; a restart continues the
  run from the persisted transition; a **new session** does not inherit the confirmed screen; `reset()` forgets
  the run too; the analyzed event carries `scene`/`transition` and never a reserved envelope key.
* **Objectives** — completed by a confirmed victory (with a `scene` evidence ref naming the observation),
  failed by a confirmed defeat, **not** closed when two objectives are current, **not** re-dated by a stale
  banner, and not closed when the banner was merely pending.
* **Knowledge** — `scene:victory` is stored as `situation` with `method=observed` on the confirming frame;
  nothing is stored for an unconfirmed frame; an inferred cue stores `method=inferred`.
* **Panel** — the real module's sentence for unread / observed / pending / contradictory states, including
  `pending 1/2` with the reason and the alternatives of an ambiguous screen.

**Tests were not weakened.** No existing assertion was relaxed: the only pre-existing test touched is the
schema pin (`nf16.1 → nf24.1`), which is the deliberate consequence of adding two fields, and its comment says
so.

---

## 8. Type-check and API/documentation validation

* Both mypy views clean (452 files) after one real typing defect was fixed (§11.5).
* `docs/API.md` unchanged and in sync — no route, request model or response model was added.
* Payload compatibility: `scene`/`transition` are **additive**; every existing consumer reads its own keys.
  The verification/planning/autonomy suites (296 tests) pass unchanged against the new payload, and NF-21's
  verifier was not touched, so an unverifiable action outcome is still `NOT_VERIFIABLE`.

---

## 9. Live verification — what was measured, and the exact blocker

### 9.1 The real-game criterion: BLOCKED, re-measured under changed conditions

The environment changed between NF-23 and this phase: **Steam was running** during this pass
(`steam` PID 4740, `SteamService` 19220, eight `steamwebhelper` processes), and the resource gate was open
(**3.41 GB free, 77.8 % used**). Both facts remove the two most plausible environmental explanations for
NF-23's blocker.

1. **The product's own launch path.** `GameCatalog` inventory (12 rows): four are `INSTALLED_AND_LAUNCHABLE`
   — GTA V (**excluded by instruction**), *Shadow Fight Arena* and *Supermarket Together* (**online
   multiplayer**, excluded by scope), and *Freedom Fighters* (single-player, offline, the phase's target).
   `GameLaunchService.launch("Freedom Fighters")` → **`INSTALLED_BUT_LAUNCH_FAILED`** after 122.45 s:
   *"Started 'Freedom Fighters' but no new matching process appeared within 120s."*
2. **Steam's own log, for the same attempt** (`E:\steam\logs\console_log.txt`):
   `19:22:21 Game process added : AppID 1347780 "…\Freedom_Fighters\Launcher.exe", ProcID 17768` then
   `19:22:23 Game process removed: … ProcID 17768`. The title's launcher started and died **2 s later, with
   no window** — under Steam, not under this product.
3. **The launcher itself, run directly from its own directory (the NF-23 F-07 fix's behaviour):**
   `Launcher.exe` prints exactly `SteamAPI_Init failed` and exits **rc=1 in 0.5 s**. Steam being resident
   changed nothing, so this is the title's own Steam-API initialisation failing on this host.
4. **No other offline title is installed.** `E:\STEAM\steamapps\common` contains `Poppy Playtime`,
   `Aurora Hills Chapter 1` and `Quarantine Zone The Last Check Demo` as **empty directories (0 bytes)** —
   not installed — and none of them is in the product's catalog anyway.

Nothing was killed, nothing was launched twice, and no process was left behind (`Get-Process` shows no
`Freedom`/`Launcher` after the attempts). **Therefore no real-game frame exists on this host, so no live-game
state interpretation is claimed.** Per the phase brief, the blocker is stated rather than disguised.

### 9.2 Live evidence that WAS obtainable — a real window, explicitly not a game

The negative the phase demands ("invalid or ambiguous screens must not become invented states") was verified
**live**, through the product's own services (`NovaForgeManager` → its wired `GameStateService`), on a
disposable harness window (title `NC NF23 LIFECYCLE PROBE`, 840×600, backend `printwindow`):

| Reading | Observation | Scene | Transition | Processing | Wall |
|---|---|---|---|---|---|
| 1 | `obs-d839db4fecf4:0003` | `unknown` (source `none`) — *"no profile matched, so no scene rule exists for this screen and no unambiguous cue was read"* | `none` | 738.9 ms | 742.1 ms |
| 2 | `obs-d839db4fecf4:0004` | `unknown` (same reason) | `none` | 734.6 ms | 738.1 ms |

`status()["scene"]` reads `confirmed: unknown`, `candidate: unknown`, `confirmations: 0` — no scene, no
transition, and no objective. This is the state service refusing to name a screen it cannot read, on live
frames, exactly as designed.

**A real integration property found here:** with a 0.5 s observation interval **every** reading was refused as
`observation_superseded` (measured twice, 768.0 ms / 744.2 ms): perception takes ~740 ms, so a newer frame
always replaced the one being read and the service discarded the result rather than attributing it. At 3.0 s
both readings succeeded. The honest reading of that pair is that an observation interval must exceed the
perception pass for a state to be attributable at all — the service's supersession guard is doing its job, and
this is recorded so nobody "fixes" it by relaxing the guard.

### 9.3 Recorded-frame evidence — real pixels, real OCR, still not a live game

Three consecutive frames of this repository's captured `GAME MENU` window (2880×1800, session
`3f84fe424db9479c9133cf93328dd365`), read by the shipped `GameStateService` (real perception engine, real
Windows OCR, real `game-menu@1` profile):

| Frame | Scene | Transition | HUD | Processing |
|---|---|---|---|---|
| `000001.png` | `menu` **observed via `profile_rule`** | `pending` (1/2, confirmed `unknown`) | `title` = `GAME MENU`, `menu` = `SINGLE PLAYER OPTIONS QUIT GAME CREDITS` | 979.3 ms |
| `000002.png` | `menu` observed | **`confirmed`** (`previous: ""`) | same | 939.1 ms |
| `000003.png` | `menu` observed | `none` (steady, 0/2) | same | 945.2 ms |

Knowledge candidates per frame: **3 → 4 → 3** — the confirming frame adds exactly one `situation:scene:menu`
record, and the steady frame adds none. Objectives: none (the `game-menu` profile declares no objective rule,
and no line named itself). Scene confidence: `None` (the reader reported none), with that sentence in the
payload.

This is **recorded-frame** evidence. It is not a live game and is not reported as one.

---

## 10. Performance and resource measurements

| Measurement | Value | Source |
|---|---|---|
| Scene classification + tracker cost | **not separately measurable at this resolution** — it is pure Python over ≤ a few dozen text rows and adds no I/O; the end-to-end reading time is unchanged within noise against the NF-16 baseline (see next row) | `tests/test_novaforge_nf24.py`, §9.3 |
| Reading latency, 2880×1800 recorded frame | **939.1 / 945.2 / 979.3 ms** (min / steady / max; mean 954.5) | `.nc24_tmp/measure_recorded.log` |
| Reading latency, 840×600 live window | **734.6 / 738.9 ms** (two readings; wall 738.1 / 742.1) | `.nc24_tmp/live_window_state_probe.log` |
| NF-16 baseline (same script, this pass) | 868.9–922.1 ms | `python scripts/novaforge_nf16_state_eval.py` |
| Objective interpretation | 0.058 ms avg, 0.185 ms max | NF-17 evaluation |
| Knowledge recording | 12.858 ms avg, 30.113 ms max (8 records) | NF-18 evaluation |
| Model load | **none** — the reading path is OCR + connected components | `state.latency.model_load_note` |
| Capture (live window) | backend `printwindow`, dims = live rect (840×600) for both observed frames | live probe log |
| Memory gate | free **3.41 GB**, used **77.8 %** before the launch attempts; the gate is re-read before every attempt | live probe log |
| Bounded resources | one analysis at a time; one perception worker; ≤8 states of history; tracker state is O(1) (a counter, a kind, one id, ≤ handful of notes); no queue is introduced | `state_service.py` |

**Targets (established from this baseline, not invented):** a reading must stay under the NF-16 envelope
(~0.9–1.2 s on this CPU-only path at 2880×1800) — met at 0.94–0.98 s; scene classification must add no
measurable I/O or model load — met (no model is loaded, no file is opened beyond the frame already read); and
the tracker must be O(1) in memory — met by construction and pinned by the duplicate/restore tests.

---

## 11. Regressions found and fixes applied

All five were found by this phase's own new tests or by the verification pass, and each is now pinned.

| # | Defect | Symptom | Fix |
|---|---|---|---|
| **N-01** | `assess_scene` reassigned its `text` parameter inside the profile-rule loop | any reading whose profile rules did **not** fire crashed in the generic-cue fallback (`AttributeError: 'str' object has no attribute 'get'`) — i.e. every "no profile rule matched" frame, which is the common case for a real window | the loop local is `reading_text`; the parameter is never reused |
| **N-02** | `SceneTracker.observe` had a `duplicate` flag that no caller ever passed | reading the same observation twice **confirmed** a scene from a single frame | the observation id itself is the duplicate test (`observation_id == last_observation_id`), so no caller can forget it |
| **N-03** | `scene_changed` required a non-empty `transition.previous` | the **first** confirmation (from `unknown`) never appeared in the change list — the one transition that matters most on a fresh run | the condition is the confirmation; `before` is `""` and the detail says the scene was unknown |
| **N-04** | the alternatives of a multi-cue frame were recomputed by re-scanning cue text (`any(cue in quote …)`) | `"NEW GAME"` + `"MISSION COMPLETE"` reported the wrong alternatives | `_generic_cues` returns its hit map; alternatives are the scenes that actually fired, in a stable order |
| **N-05** | mypy: `getattr(profile, "scene_rules", ())` passed a tuple as a `bool`-typed default | both mypy views failed | direct attribute access (`profile.scene_rules`) |
| **N-06** (found in the verification pass, not by a failing test) | a confirmed victory screen closed **every** displayed candidate objective | two objectives on one victory screen were both marked COMPLETED by one banner — the exact guesswork the phase forbids | the scene channel applies only when exactly **one** candidate is displayed; otherwise the diagnostics record `none was closed by it` and the objectives keep their real status. New test `test_two_displayed_objectives_are_never_closed_by_one_banner` |

The objective tightening (N-06) was made **before** the final test runs recorded in §7, and the NF-17
evaluation (32/32, 0 false completions) was re-run afterwards.

---

## 12. Remaining limitations and external blockers

1. **No real-game window exists on this host** (external, measured): Freedom Fighters' launcher fails
   `SteamAPI_Init` with Steam running; every other installed offline title is either online-multiplayer,
   excluded by instruction (GTA V), or an empty directory. NF-24's live-game state interpretation is therefore
   **unverified**, not passed.
2. **No commercial-game profile.** Scene rules exist for two fixture layouts and the `GAME MENU` fixture. A
   commercial title takes the generic path, which can name a menu/loading/outcome screen from a cue and cannot
   name gameplay.
3. **Per-field confidence is `None`** on this host: the shipped OCR reader reports no per-line confidence, and
   a geometric proxy would be an invented number.
4. **Gameplay is never inferred** from a generic cue (deliberate; §5.2).
5. **Confirmation is frame-based.** With the default 2 confirmations, a transition needs two consecutive
   agreeing *observed* frames; at a 3 s interval that is ~3 s of latency, and an interval shorter than the
   perception pass makes readings superseded (§9.2). Both are properties of the honest path, not bugs.
6. **Scene evidence for objectives is limited to outcome screens** (`victory`/`defeat`). A pause or menu screen
   closes nothing, and no screen can close a *list* of objectives.
7. **Live objective/knowledge behaviour on a real game** is unverified for the same reason as (1); the fixture
   and recorded-frame evidence is deterministic but is not a substitute, as §13 of the brief requires.

---

## 13. Exact commands for reproducing verification

```bash
# from the repository root, Git Bash on Windows, with the project's .venv
export PYTHONPATH=src

# NF-24's own suite (57 tests now; 41 at the first pass — includes the Node panel cases)
python -m pytest tests/test_novaforge_nf24.py -q

# the NF-16/17/18 surfaces it extends
python -m pytest tests/test_novaforge_state.py tests/test_novaforge_objectives.py \
               tests/test_novaforge_knowledge.py -q

# the observation path it consumes, the API surface and the panel
python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_api.py \
               tests/test_novaforge_nf23.py tests/test_novaforge_observation.py -q

# the downstream consumers (planning, verification, autonomy, web)
python -m pytest tests/test_novaforge.py tests/test_novaforge_closed_loop.py \
               tests/test_novaforge_gameplay.py tests/test_novaforge_planning.py -q
python -m pytest tests/test_novaforge_verification.py tests/test_novaforge_autonomy.py \
               tests/test_web_api.py -q

# type checks and the API reference
python -m mypy src
python -m mypy src --platform win32
python scripts/generate_api_reference.py --check

# the phase evaluations (deterministic fixtures + recorded frames)
python scripts/novaforge_nf16_state_eval.py
python scripts/novaforge_nf17_objective_eval.py
python scripts/novaforge_nf18_knowledge_eval.py

# recorded-frame measurement of the scene path (real OCR, no game)
python .nc24_tmp/measure_recorded.py

# live NON-GAME window check (spawns and closes its own disposable window)
python .nc24_tmp/live_window_state_probe.py

# the live-game attempt (refuses if the gate is closed; launches nothing if the title is running)
python .nc24_tmp/live_game_state_probe.py
```

Scratch evidence (untracked, as in earlier phases): `.nc24_tmp/measure_recorded.log`,
`.nc24_tmp/live_window_state_probe.log`, `.nc24_tmp/live_game_state_probe.log`.

---

## 14. Acceptance checklist and final verdict

| Criterion (phase brief §13) | Status | Evidence |
|---|---|---|
| Uses the verified NF-23 observation pipeline | **PASS** | `manager.state` reads only the NF-15 observer's newest validated frame; supersession still refuses (§9.2) |
| State results grounded in traceable visual evidence | **PASS** | every scene carries `field`/`text`/cue or the reason none applied; every objective evidence ref carries the observation id (§5, §7) |
| Uncertain and unsupported states represented honestly | **PASS** | `unknown` with a reason (live, §9.2); `alternatives` + contradictions; `None` confidence with its sentence |
| State transitions temporally validated | **PASS** | N-of-M, pending/confirmed/contradicted/duplicate, restart/session semantics (§7) |
| Objective recognition integrated with NF-17 | **PASS** | `EvidenceKind.SCENE`, single-attribution rule, `unattributed` diagnostics; NF-17 eval 32/32, 0 false completions |
| Evidence provenance preserved through NF-18–NF-21 | **PASS** | `situation:scene:<kind>` with observed/inferred method; objective evidence travels; NF-21's verifier untouched and 296 downstream tests green |
| Regression tests cover the new functionality; earlier behaviour intact | **PASS** | 41 new tests + 222/283/237/296 existing tests green; schema pin updated deliberately |
| Relevant type checks and integration tests pass | **PASS** | both mypy views clean; API reference in sync |
| Live-game state interpretation demonstrated if a safe supported game and valid conditions exist | **BLOCKED** | no real game can start on this host; re-measured with Steam running (§9.1) |
| Report distinguishes live verification from simulation | **PASS** | §9.1 blocked, §9.2 live-non-game, §9.3 recorded-frame — three separate sections |
| No known, fixable, in-scope defect remains | **PASS** | eleven defects/over-permissive behaviours found and fixed (§11, §15.1), each pinned |

**Final verdict: PARTIAL.**

* **PASS** — scene understanding, temporal confirmation, the state payload and panel, objective scene
  evidence with honest attribution, knowledge provenance, and the refusal behaviour on a live window. No known
  fixable in-scope defect remains.
* **BLOCKED** — live-game state interpretation. The only safe installed offline title cannot start on this
  host with the product's own launcher, with Steam itself, or by direct execution (measured three ways in
  §9.1), so no real-game frame could be interpreted. This is an external blocker, not a code defect.

**NF-25 was not begun.** Per §15 of the brief, NF-24's implementation, regression testing and reporting stop
here, and the live-game criterion is handed forward as a blocker rather than a claim.

---

## 15. Verification pass (second) — re-checked against the phase objectives

Run after the phase was first reported, with the same brief: verify NF-24 against its own objectives, find and
fix defects, run the phase and dependency suites, and record what remains unverified. **The live-game result was
not re-attempted** (no environment change occurred, so the §9.1 measurement stands); everything else was
re-exercised against inputs the first pass never tried.

### 15.1 Defects found and fixed in this pass

| # | Defect | Symptom | Fix |
|---|---|---|---|
| **N-07** | generic cues and rule patterns were matched as **substrings** | `"game over"` matched inside `"game overlay"`; worse, the bare nouns `"victory"`/`"defeat"` were cues, so an on-screen **objective line** `"Defeat the boss"` named the DEFEAT screen — two consecutive such frames confirmed it and the objective channel then **failed the objective**. A false defeat caused by reading the mission text | phrases are compiled once as whole-word patterns (`(?<!\w)…(?!\w)`, case-insensitive) for cues **and** rule patterns; bare screen nouns are removed from the cue table, because a banner that says only that word is a profile's job (a profile declares the region it was verified in). Pins: `test_a_cue_must_be_the_whole_words_it_names`, `test_a_mission_instruction_does_not_name_a_defeat_screen`, `test_a_pattern_matches_whole_words_only` |
| **N-08** | `SceneTracker.contradictions` was **unbounded** | one note per candidate flip, cleared only by a confirmation or reset — a noisy HUD accumulates strings for the life of the process | `MAX_TRACKED_CONTRADICTIONS = 8`, trimmed oldest-first in `_transition` and on `restore`. Pin: `test_the_contradiction_log_stays_bounded` (40 flips ⇒ exactly 8, newest kept) |
| **N-09** | a run counted **frames**, so it could bridge a capture interruption | observations 1 and 5 (2–4 never captured) agreeing confirmed a transition across three unseen frames — a change asserted from a run that was not consecutive. The persisted run had the same hole after a restart | `observe(..., sequence=)` carries the observation's own sequence; a non-contiguous sequence drops the unconfirmed run, says why, and marks the transition `interrupted`. `SceneTransition.sequence`/`interrupted` persist and restore, so the first frame after a restart is judged as the interruption it is. Pins: `test_a_sequence_gap_restarts_the_run_instead_of_confirming_across_it`, `test_a_restored_run_notices_that_capture_resumed_elsewhere`, `test_a_capture_gap_cannot_confirm_a_transition_across_it` (through `build_game_state`) |
| **N-10** | Objective C was **incomplete**: a profile could not declare what the pipeline must provide, and a rule had **no confidence requirement** | a profile whose rules depend on reading text could not say so (a missing OCR engine produced a generic "unknown" instead of naming the unmet requirement), and a rule fired over a reading with no measured confidence — the weakest possible evidence producing the strongest claim | `GameProfile.required_capabilities` (validated against the new `OBSERVATION_CAPABILITIES` vocabulary) and `SceneRule.min_confidence`. `assess_scene` refuses a profile that requires OCR when OCR is known missing (`source: "capability"`), and a rule that matched but **failed its floor** leaves the screen `unknown` — a generic cue may not decide a claim that stronger evidence was just rejected for (pinned: `…holds_a_rule_back_until_the_reading_is_measured`, `test_a_profile_requiring_ocr_is_refused_when_ocr_is_missing`, `test_a_profile_refuses_a_capability_the_build_cannot_provide`) |
| **N-11** | the panel showed an unknown screen as `unknown (unknown)` | the reason the screen could not be named — the one thing an operator needs — was rendered nowhere, and a run restarted after a capture gap looked like an ordinary pending run | `stateSceneText` appends the first evidence line for an unknown screen, and names the interruption beside the pending count. Pins: `test_an_unknown_screen_shows_the_reason_it_could_not_be_named`, `test_a_capture_interruption_is_named_beside_the_pending_run` (Node, real `novaforge.js`) |
| **N-12** | profile rules recompiled their phrase pattern **on every frame**, although the design and the fix for N-07 describe the phrases as compiled once | a rule loop compiled the same handful of regexes per rule per observation — negligible in absolute terms, but the documented invariant was not the implemented one | `_cue_pattern` is `lru_cache(maxsize=256)`d, so a phrase is compiled once per process for cues **and** rule patterns (suite time for the 57 tests fell 0.83 s → 0.31 s). No behaviour change; the whole-word property is unchanged because the cached object **is** the same pattern |

N-07 and N-09 are the two that matter for the phase's honesty rules: one fabricated a **state** from an
instruction, the other a **transition** from a run that never existed. N-12 is a documentation-vs-code gap in
the cost model, not in any answer.

### 15.2 Tests and checks run in this pass (all green)

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge_nf24.py -q` | **57 passed** (41 → 57; 16 new, all listed above) |
| `python -m pytest tests/test_novaforge_state.py tests/test_novaforge_objectives.py tests/test_novaforge_knowledge.py tests/test_novaforge_observation.py tests/test_novaforge_nf23.py -q` | **277 passed** |
| `python -m pytest tests/test_novaforge_ui.py tests/test_novaforge_api.py tests/test_perception.py -q` | **279 passed, 94 subtests passed** |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_planning.py tests/test_novaforge_verification.py -q` | **248 passed, 53 subtests passed** |
| `python -m mypy src` | **Success: no issues found in 452 source files** |
| `python -m mypy src --platform win32` | **Success: no issues found in 452 source files** |

No route, request model or response model changed in this pass, so `docs/API.md` is still in sync (no new
field is exposed on the wire except the additive `transition.sequence` / `transition.interrupted`, which travel
inside the existing nested object).

### 15.3 What this pass did NOT change

1. **Live-game state interpretation is still BLOCKED.** The blocker was not re-attempted because nothing about
   it changed; §9.1 remains the current measurement, and no real-game frame or state is claimed anywhere.
2. **No commercial-game profile was added.** Objective C forbids padding the profile count, and no real game can
   be measured on this host to justify one.
3. **Per-field confidence is still `None`**, so a rule's `min_confidence` floor is a *refusal* guard on this host
   (an unmeasured reading never clears a floor) rather than a tuned threshold.
4. **No test was weakened, skipped or deselected**; the only edited assertion is the profile payload dictionary
   in `test_a_profiles_scene_rules_travel_in_its_payload`, which now also pins `min_confidence` and
   `required_capabilities` because the payload genuinely gained them.

### 15.4 Verdict after the verification pass

Unchanged: **PARTIAL — PASS on scene/transition/objective/knowledge integration, BLOCKED on live-game state
interpretation.** The pass raised confidence in the deterministic half (two fabricated-claim defects removed,
both now impossible to reintroduce without failing a test) and left the one genuinely unverifiable criterion
where it was: no safe supported game can start on this host.

## 16. NF-23…NF-26 consolidated acceptance (2026-10-28)

Re-audited with NF-23, NF-25 and NF-26. Full evidence:
`docs/NOVAFORGE_NF23_NF26_CONSOLIDATED_ACCEPTANCE_REPORT.md`; findings:
`docs/NOVAFORGE_NF23_NF26_REMEDIATION_REGISTER.md`.

**Verdict unchanged: PARTIAL** — the declared commercial-game interpretation remains absent and is the
in-scope requirement this phase cannot honestly claim; the live blocker (B-01) is unchanged.

**What this pass added as evidence.** A live session over the disposable harness window attached a
checkable objective through `set_objective` (`WindowTitleVerifier`, `supported: true`) and asked the
independent verifier for a verdict. Against a window whose title did **not** satisfy the objective, it
returned:

```
outcome: verified_failure   achieved: false   decided: true   method: window_title
reason: Read 'NC FORGE HARNESS - idle' from window <h> (pid <p>): not satisfied because the
        title contains 'READY'.
```

That is the phase's safety rule demonstrated on a live surface: the objective was not achieved, the
system said so with the reading it used, and it did not infer success from the absence of a failure
signal. As a **negative control**, it shows success is not reported before the objective is met.

**One semantic checked because it is a trap.** The verdict carries `verified: true` even when the outcome
is `verified_failure` (`verified` = the question was decided). Every NovaForge consumer tested reads the
explicit `status`/`outcome` instead (`tasks.py`, `autonomy.py`, `verification.OutcomeStatus`), and no
NovaForge verdict is adapted into `StandardResult.verified`; recorded as an observation, not a defect.

No interpretation accuracy is claimed: no commercial HUD was read in this pass, and the retention list in
§12 stands.
