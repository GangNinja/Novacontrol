# NovaForge NF-25 — Real-Game Objective Execution and Progress Validation

**Status:** **complete — PARTIAL verdict.** The task layer, its preflight gates, its evidence judge and the
fixture eval are implemented and verified; the live-game half is **BLOCKED / NOT APPROVED** for the same two
reasons NF-23 and NF-24 recorded: no safe installed offline title can start on this host
(`SteamAPI_Init failed`), and the brief requires the operator's explicit approval for a live gameplay test
before any input is sent. Results: `docs/NOVAFORGE_NF25_REAL_GAME_OBJECTIVE_EXECUTION_REPORT.md`;
current verification: `docs/NOVAFORGE_VERIFICATION.md` Appendix C.

**Rule this phase exists to enforce:** a dispatched input is not a game response, a game response is not
progress, progress is not completion, and a simulated run is not a live one.

## 1. Dependency status at phase start

| Dependency | Established by | State |
|---|---|---|
| Bounded input execution (press/release/hold/move, deadlines, input release) | NF-19 `GameControlService`, manager gates | **verified** (717 control tests + downstream suites green) |
| Evidence-grounded planning | NF-20 `GamePlanService` (plan → approval → execute) | **verified** (planning suite green) |
| Outcome verification and recovery | NF-21 `GameVerificationService` | **verified**; `not_verifiable` is a real answer |
| Autonomous orchestration | NF-22 `AutonomousSessionController` (phases, limits, recovery, per-cycle records) | **verified** on fixtures/simulation |
| Structured state understanding | NF-24 `GameState.scene` / `SceneTracker` | **verified on fixtures and recorded frames**; live-game interpretation **BLOCKED** |
| Real-game state from a live commercial title | NF-23 measurement | **BLOCKED** — no launchable, safe, offline title on this host |

NF-25 therefore builds on verified **interfaces**, and does not claim to have verified them against a real game.

## 2. Existing components NF-25 reuses (no new loop, no new planner)

| Need | Reused component | How NF-25 uses it |
|---|---|---|
| observe → decide → act → verify | `autonomy.AutonomousSessionController` | NF-25 drives it; it keeps owning phases, budgets, recovery and per-cycle records |
| planning | `planner.GamePlanService` (inside NF-22's gateway) | unchanged; NF-25 only refuses a start whose evidence cannot justify a plan |
| input | `control.GameControlService` (inside NF-22's gateway) | unchanged; NF-25 never injects input itself |
| verification | `verification.GameVerificationService` (inside NF-22's gateway) | NF-25 reads the verdict and its evidence refs; it never re-decides the outcome |
| state | `state_service.GameStateService` (NF-24 scene payload) | read once for the initial-state gate and recorded as the task's opening evidence |
| task text/target/budgets | `models.MissionConfig` | the task spec **contains** a validated `MissionConfig` rather than inventing a second objective model |

## 3. What NF-25 adds

### 3.1 `novaforge/tasks.py` — a bounded real-game task

* `TaskSpec` — the task definition: id, `MissionConfig` (objective, target title, budget), game label,
  success/failure conditions, permitted action kinds, action/cycle/time budgets, observation freshness bound,
  recovery limit, required evidence tags, allowed initial scenes.
* `TaskSpec.errors()` — rejects a task that cannot be executed honestly: no objective, no success condition, a
  vague goal ("play well", "finish the game"), no evidence requirement, an unknown action kind, or a budget
  above the NF-22 ceilings (refused, never silently clamped).
* `TaskReport` — the mission-level record: verdict, reason, pinned identity, opening evidence
  (`observation_id`, `state_id`, scene), dispatch/response/progress counters, the objective's stage
  ladder, evidence, limitations.
* `task_stages(run)` — the nine stages of Objective E answered **separately** (`objective_identified`,
  `initial_state_verified`, `plan_generated`, `plan_approved`, `action_requested`, `input_dispatched`,
  `game_response_observed`, `progress_evidence_recorded`, `completion_verified`). A later stage is never
  inferred from an earlier one, and `completion_verified` can only be set by `judge_task`.
* `dispatched_kinds(run)` — the action kinds the run actually dispatched (labelled, `ok: true` steps),
  plus the count it could not attribute. The task's `permitted_actions` are enforced as a **claim gate**:
  a run that dispatched anything outside them cannot be judged `verified_completion`.
* `judge_task(spec, run)` — the ONE judge: a run may only become `verified_completion` when its phase is
  `COMPLETED`, its outcome is `objective_completed`, and the required evidence is actually present; a run that
  dispatched input but never established an effect is `outcome_unknown`, never completion.
* `TaskRunner` — the supervisor: preflight gates (spec, approval, session, target identity, control policy,
  initial state freshness/known scene, resource gate, duplicate run), start through the NF-22 controller, wait,
  then judge. It adds no plan, no input path and no verification of its own.

### 3.2 Preflight gates (each one a refusal with its own code)

| Gate | Code | Why it must refuse |
|---|---|---|
| spec invalid | `invalid_task` | a task without a verifiable success condition is not a task |
| operator approval absent | `approval_required` | NF-25 never sends input a person has not approved |
| unknown session / unpinned target | `unknown_session`, `target_unverified` | never act on a window nobody validated |
| title does not match the task | `target_mismatch` | the wrong game is not the task's game |
| action kind not permitted by the control policy | `unsupported_action` | the task cannot ask for input the build cannot bound |
| initial state unavailable / unknown screen | `initial_state_unavailable`, `initial_state_unknown` | no plan may be built on a guess |
| initial state stale for the task's freshness bound | `initial_state_stale` | an old reading is not the current state |
| initial scene not in the task's allowed set | `initial_state_mismatch` | e.g. a task that starts from a menu cannot start in a death screen |
| resource gate refuses | `resource_gate_refused` | the brief's own launch gate, read from the governor |
| another task is already running on the session | `already_running` | two supervisors on one window is how input races |

### 3.3 Cancellation and the ledger

A CONFIRMED cancellation ends the task in the runner's ledger (report finished, verdict `cancelled`) —
otherwise the session stays refused `already_running` forever. Three cases are kept apart: a cancel the
supervisor confirms; a cancel it does not confirm (the ledger follows the supervisor without claiming a
stop, verdict `outcome_unknown` with the supervisor's code); and a cancel that **ended another way**
(`cancelled_after_completion` / `cancelled_after_ending`) — the run's own ending is the judge's to read,
so a cancel path never rewrites it into "cancelled" and never claims a completion it did not gate.

### 3.4 Verdict mapping (NF-22 outcome → NF-25 verdict)

`objective_completed` → `verified_completion` **only if** the evidence gate AND the action-policy gate pass,
else `outcome_unknown` (`evidence_missing`/`no_evidence`) or `safety_stop` (`action_policy_exceeded`);
`objective_failed` → `objective_failed`; `session_cancelled` → `cancelled`; `safety_stop` → `safety_stop`;
`dependency_failure` → `dependency_unavailable`; `observation_unavailable` → `interrupted`;
`session_timed_out` → `interrupted` when input was dispatched, else `outcome_unknown`;
`resource_limit_reached` → `progress_only` when verified progress exists, else `outcome_unknown`;
`outcome_unknown` → `outcome_unknown`.

## 4. Safety boundaries

Unchanged from NF-23/NF-24 and enforced at the same places: no GTA V, no external drive, no force-kill, no
duplicate instance, no injection/memory/rendering hooks, no display-mode changes, no online multiplayer, no
input to an unvalidated window, 2 GB free RAM / < 92 % used gate before a launch, and graceful shutdown only.
NF-25 adds no new input path: every action still travels NF-22 → NF-20 → NF-19 → the Guardian.

## 5. Implementation steps

1. `tasks.py` with `TaskSpec`, `TaskReport`, `TaskVerdict`, `judge_task`, `task_progress`, `TaskRunner`,
   `AutonomyTaskGateway` (the adapter to `AutonomousSessionController` + manager readers).
2. Manager factory `NovaForgeManager.tasks()` returning a `TaskRunner` wired to this manager's own services, so
   the phase is reachable from the product rather than being a library island. No API route is added, so the
   documented API surface is unchanged.
3. Tests (`tests/test_novaforge_nf25.py`): spec validation, every preflight gate, verdict mapping (dispatch ≠
   response ≠ progress ≠ completion), evidence gate (missing evidence keeps completion unverified), duplicate
   execution prevention, cancellation, resource refusal, simulation-vs-live labelling, and the adapter's limit
   translation.
4. Report + `docs/NOVAFORGE_VERIFICATION.md` appendix C.

## 6. Measurable acceptance criteria

| Criterion | Target | Basis |
|---|---|---|
| Preflight latency (spec + gates, no capture) | ≤ 50 ms | pure validation plus one state read already taken by the app |
| Judge latency (one run payload) | ≤ 20 ms | pure function over ≤ 40 cycles |
| cycle budget enforced | never above `AutonomyLimits` ceilings | NF-22 refuses a wider bound |
| dispatch accounting | `actions_dispatched` counted per input operation, never per plan | NF-22's own counter |
| completion evidence | 100 % of `verified_completion` verdicts carry ≥ 1 accepted evidence ref and every required tag | `judge_task` |
| action policy | 0 `verified_completion` verdicts after a dispatch outside `permitted_actions` (including an unattributable step) | `dispatched_kinds` + `judge_task` |
| stage ladder | all nine stages reported separately for every judged run | `task_stages` |
| session freed after a cancel | a confirmed cancellation leaves no active task on the session | `TaskRunner.cancel` |
| false completion | **0** in tests | dispatch-only, progress-only and missing-evidence fixtures must all stay unverified |
| duplicate execution | **0** concurrent tasks per session | preflight `already_running` |

## 7. Live-game test procedure (requires approval; not run in this pass)

Proposed task (for when a safe offline title is launchable): start from the game's own main menu, objective
"open the options menu and return to the main menu", permitted actions `key_press`/`key_release` only, 4 actions,
60 s, 3 cycles, observation freshness 5 s, recovery 1, required evidence `scene:menu`, `verification` — verified
completion only from a fresh observation after the last action.

Before it may run: identify the process/window, confirm the capture backend supports it, check the resource
gate, confirm the game is not already running, present the plan to the operator and receive explicit approval.
**No approval was requested or granted in this pass, and no input was sent.**

## 8. Risks and controls

* **Claiming completion from a dispatched input** — controlled by `judge_task` requiring the run's terminal
  outcome *and* evidence; pinned by tests that start from dispatch-only fixtures.
* **Driving a second supervisor** — `TaskRunner` holds no loop; it calls the NF-22 controller and the manager's
  own services.
* **A task whose evidence tag can never appear** — the report lists the evidence it actually found, so a
  mismatched requirement fails honestly instead of passing silently.
* **Simulation mistaken for live evidence** — every report carries `evidence_source` from the run and an
  explicit `simulated` flag, and the report's live section is separate from the fixture sections.
