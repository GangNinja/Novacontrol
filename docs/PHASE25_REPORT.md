# Phase 25 — Embodied / Game Agent

**Verdict: PASS**, with the limitations in §15 recorded explicitly. Every mandatory
acceptance criterion in the phase specification is satisfied by code that runs, and the
evidence for each is named below. Phase 26 is named as deferred (§18) and nothing from
it is claimed.

The phase's flow, as implemented:

```
OBSERVE THE ENVIRONMENT      → EnvironmentAdapter.observe()          (environment.py, simulation.py)
INTERPRET THE CURRENT STATE  → facts_from_observation()              (perception.py)
UPDATE THE WORLD MODEL       → world.observe()                       (Phase 22, observed facts only)
IDENTIFY THE OBJECTIVE       → SessionState.objective                (models.py)
SELECT AN ACTION             → tactical.choose() → Phase 24 plan()    (tactical.py → goal_manager.py)
CHECK PRECONDITIONS/SAFETY   → the plan's fresh-state gate + self.risk (Phase 8/24)
EXECUTE THROUGH A CONTROLLER → EmbodiedManager.execute_step()        (manager.py — THE ONE DOOR)
OBSERVE THE RESULT           → a SECOND, fresh observe()             (agent.py:_fresh_observation)
VERIFY WHETHER IT WORKED     → effect_observed() + verdict()         (perception.py, agent.py)
UPDATE MEMORY AND TRAJECTORY → TacticalMemory + trajectory entries   (tactical.py, models.py)
RECOVER OR REPLAN            → _recover(), bounded                    (agent.py)
→ completed | budget_exhausted | environment_failed | agent_error | denied | stuck | cancelled | interrupted
```

---

## 1. Scope and implemented capabilities

NovaControl can now act **inside** an environment that answers back. A session is
created from a measurable objective, driven through a bounded loop, and reports an
ending that distinguishes a met objective from an exhausted budget from an
environment that stopped answering from a refusal.

Implemented and verified (§11, §12, §14):

- **The cycle above, end to end**, with each stage in the module named above and the
  decision itself taken by Phase 24's goal manager, never by a private loop.
- **One input door** (`EmbodiedManager.execute_step`), reached as the
  `environment_input` plan action, which enforces: the session exists and is live, the
  step names an input, the environment is registered **and allowlisted by name**, the
  environment is OPERATIONAL, the pace, the duration clamp, the audit record and the
  read-back. A pause, a finish and a missing revalidation each refuse with their own
  error, and nothing is sent.
- **A controlled environment that really runs**: `GridWorldSimulation`, a deterministic
  grid world with walls, an agent, one collectible target, structured readings, fogged
  readings (unlabeled regions only) and four named fault injections (`timeout`, `drift`,
  `reject`, `freeze`) declared by the scenario that asks for them.
- **Seven named scenarios** (`open-east`, `detour`, `drifting`, `refusing`, `frozen`,
  `timing-out`, `fogged`), each a configuration of the allowed `gridworld` family and so
  its own environment id (`gridworld:detour`).
- **Observation honesty**: `basis` (`structured`/`visual`/`predicted`/`unknown`) and
  `freshness` (`fresh`/`stale`/`failed`), where only a fresh structured reading may
  authorize a state-dependent action, and a label no evidence supports stays empty.
- **Three-valued verification** of both the action (`True`/`False`/`None`, where `None`
  is never a success) and the objective (a verdict needs a fresh reading).
- **Bounded recovery** with a remembered blockage, a refusal that ends the session
  rather than being retried, and a timeout that is re-observed rather than re-sent.
- **Sessions as data**: lifecycle, budgets clamped by configuration, a bounded
  trajectory, an evaluation whose unmeasured rates are `None`, persistence through the
  shared JSON state store, and a boot restore that marks unfinished work INTERRUPTED and
  refuses to send anything until it has been revalidated.
- **Five surfaces and an audit trail**: 10 routes, **18** capability rows on the ONE
  registry (this phase's own table is their single source; the figure of 8 this document first
  gave did not match the shipped table and was corrected by the Phases 21–26 audit, which
  measured 18 on a real application; `status()["app"]["embodied"]` and
  `GET /embodied/status` read the same table), 13 typed events on the ONE bus, and every
  input appended to the shared automation audit trail.

## 2. Existing architecture reused

Nothing was forked. Phase 25 composes what already exists:

| Existing piece | How Phase 25 uses it |
|---|---|
| Phase 4 execution | The application's `WorkflowExecutor` runs every step; this package only plans and reads |
| Phase 5 tools | The `environment_input` action is declared on the ONE permission layer; a step reaches it the same way any other tool does |
| Phase 8 reliability | `self.risk` decides what an input may do; the plan's verification ordering and the recovery ceilings are the existing ones |
| Phase 9 registry/bus | `register_embodied_capabilities` writes the ONE `CapabilityRegistry`; announces go through the ONE `EventBus` via an injected observer seam |
| Phase 13 audit | The shared `AutomationAuditLog` the device controllers write to — one `EmbodiedActionResult` per input, exactly once |
| Phase 14 governor | Nothing here loads a model, so the governor is never asked to; no GPU is required |
| Phase 22 world model | Observations are ingested as **observed** facts (provenance intact), and the plan's fresh-state gate reads them back |
| Phase 24 planning | `GoalManager.create/decompose/plan/execute` is the action selector; one goal per session, replanned per cycle |
| Shared persistence | `JsonStateStore`, one key (`embodied_sessions`) holding every session |

## 3. Modules created or modified

Created — `src/novacontrol/embodied/` (9 Python files including `__init__.py`, 5,705 lines in the current tree):

| Module | Lines | Responsibility |
|---|---|---|
| `models.py` | 777 | The contracts: observations, entities, actions, results, objectives, verdicts, trajectories, sessions, evaluations, `validate_objective`, `EmbodiedActionResult` |
| `environment.py` | 236 | The adapter protocol, `EnvironmentRegistry` (allowlist by family, refuse by name), connect/release reports |
| `simulation.py` | 716 | `GridWorldSimulation`, `Faults`, the seven scenarios, `adapter_for` |
| `perception.py` | 364 | `facts_from_observation` (observed vs uncertain), `to_world_observation`, `effect_observed`, `same_value` |
| `tactical.py` | 399 | `read_position/target/collected`, `TacticalMemory`, `choose`, `tasks_for` (the Phase 24 tasks a choice becomes) |
| `agent.py` | 1,226 | `EmbodiedAgent`: the cycle, the plane, the recovery, the verdict, the trajectory, the events |
| `manager.py` | 1,176 | The session registry, the ONE input door, persistence/restore (including rebuilding a restored session's configured world), status |
| `capabilities.py` | 407 | The 18-row capability table and its live availability probes |
| `__init__.py` | 332 | `overview()`, the lifecycle tables, the event vocabulary, the deferrals |

Modified: `application.py` (build the manager, declare the permission, the step handler,
the 10 routes, boot restore, stop, status, capabilities), `api/app.py` (the handlers),
`api/models.py` (10 `ApiRoute` rows = 248 routes), `api/route_consumers.py` (10
consumers = 248), `core/config.py` (`EmbodiedSettings` + 6 hard ceilings),
`core/events.py` (13 event types with declared payload fields),
`docs/API.md` (regenerated), `planning/capabilities.py` (see §13).

## 4. Environment and observation contracts

`adapter.observe()` returns an `Observation` carrying the environment's own `structured`
state when it has one, entities with a `basis` and an optional `confidence`, a
`freshness`, an age, the objective's progress and its provenance. `facts_from_observation`
splits that reading into **observed** (only the environment's own state, and only a
labeled entity with real support) and **uncertain** (a region from a frame, a
low-confidence semantic claim). A region with no label becomes
`env.region.<id>.present/.x/.y` — a fact that exists, and that can never satisfy a
precondition by itself.

The fogged scenario is the proof: a reading with `structured={}`, `basis=visual`, whose
entities are all `named=False`, produces no observed facts at all, and the agent
therefore reports `stuck` with the reason rather than navigating a guess (§14, fogged).

## 5. The observe → act loop

`EmbodiedAgent.run()` refuses to start without a planner (a session with no Phase 24 goal
manager finishes as `agent_error` saying so, rather than falling back to a private loop),
revalidates first when the session needs it, connects, then iterates:

`_sync_sent_count` → the seconds budget → the action budget → `_cycle`, where `_cycle`
takes ONE fresh reading (re-read once when the first comes back stale), ingests it into
the world model, records the observation, checks the objective's verdict, chooses, and
either sends one input or answers `stuck`. The input count is taken from the **door**
before anything is decided, because those two checks are what stop the loop.

## 6. Action selection method

`tactical.choose()` turns a fresh structured reading into one of three things:
`interact` when standing on the target, a `move` in the direction that closes the
Manhattan distance, or `none` **with a reason** (a stale or unfogged reading, a missing
position, every direction from this cell blocked). It answers in the order
east → north → south → west, skipping a direction this position is known to have failed,
which is what makes the detour work. That choice becomes a Phase 24 `Task` with the
declared `expected` facts, the preconditions the environment reported, a
`VerificationSpec` and a `StepEffect`, and is planned, ranked, authorized and executed by
Phase 24 — the choice is this phase's, the decision to run it is not.

## 7. State and prediction integration

Every observation is ingested into Phase 22 as an observed fact with its provenance and
scope (`environment_id`), so the plan's fresh-state gate reads exactly what the agent
read. Nothing is predicted: this phase has no projection, and the honest boundary is that
a fact that cannot be read is not assumed — the fogged and frozen scenarios are the two
ends of that (no state at all, and state that is no longer current).

## 8. Execution, verification and recovery

Execution is Phase 24's, through the door described in §1. Verification is two checks in
one order: the action's declared effect against a **fresh** reading, and the objective's
conditions against a fresh reading. Neither rounds up: a reading that cannot report a
fact answers `None`, and `None` is never a success.

Recovery is bounded by `max_replans + 1` and always names why the previous attempt did
not work. A blocked direction is remembered per position; a repeated attempt is counted
and not retried; a refusal (`denied`) ends the session; a timeout is re-observed; an
exhausted recovery ceiling ends the session as `budget_exhausted` with the reason.

## 9. Safety and resource controls

- The allowlist is by **name and family**: an environment not in `embodied.environments`
  is refused even when an adapter is registered for it.
- The `environment_input` action is **declared** on the permission layer (LOW risk,
  `environment:control`, reversible, not destructive, no confirmation) rather than
  derived from its name.
- The door refuses a paused, finished or un-revalidated session with a distinct
  `PermissionError`, which Phase 24 records as a DENIED step where the refusal happened.
- Every ceiling exists twice (configuration and a hard bound, §EMBODIED.md).
- The trajectory is bounded, and what it stores is operational metadata only — pinned by
  a test that scans the serialized record for `prompt`, `reasoning`, `chain_of_thought`,
  `frame` and `screenshot`.
- Nothing loads a model, downloads anything, or requires a GPU.

## 10. API, GUI and CLI changes

10 routes (§EMBODIED.md), registered in all five contract places: the handler in
`api/app.py`, the `ApiRoute` row in `api/models.py`, the consumer in
`api/route_consumers.py`, the regenerated `docs/API.md` and the tests.
**248 routes = 248 consumers**, `docs/API.md` in sync, 248 subtests green in
`test_api_reference_sync.py`.

No GUI change and no CLI command: this phase has no web panel, and `route_consumers.py`
declares each route `no-render` with the reason, rather than putting a control in the
contract that the frontend does not have. `status()["app"]["embodied"]` carries the
session counts and ceilings for the operator surfaces that do exist.

## 11. Automated test results

| Check | Command | Result |
|---|---|---|
| Embodied contracts + workflows | `PYTHONPATH=src python -m pytest tests/test_embodied.py -q` | **122 passed, 84 subtests passed** |
| Planning (cross-phase truth fix) | `pytest tests/test_goal_planning.py -q` | **90 passed** |
| API surface + reference sync | `pytest tests/test_api_reference_sync.py tests/test_api.py -q` | **8 passed, 248 subtests passed** |
| Full suite, seven disjoint slices | `PYTHONPATH=src python -m pytest <slice> -q` × 7 | **3,901 passed, 12 skipped, 2,296 subtests passed**; every slice exits 0. Five `test_rlhf.py::ApplicationRLHFTests` cases failed in the slice run **because the Phase 14 resource governor refused on a memory-starved machine** (a background uvicorn plus the parallel pytest run left <500 MB free: "the estimate needs 135818342 bytes but only 0 bytes are usable"); with the server stopped the class is **12 passed / exit 0**, so it is an artifact of the measurement host, not of this phase |
| Types, Linux view | `python -m mypy src` | **Success: no issues found in 399 source files** |
| Types, Windows view | `python -m mypy src --platform win32` | **Success: no issues found in 399 source files** |
| API reference | `python scripts/generate_api_reference.py --check` | **docs/API.md: in sync** |
| Benchmark | `PYTHONPATH=src python scripts/benchmark_embodied.py` | **9/9 scenarios matched their declared outcome**, exit 0 |
| Live server | real `uvicorn` on `127.0.0.1:8001`, isolated `data/` | **16/16 scripted HTTP legs** (§12) |

The 12 skips are one environmental cause (`playwright not installed`); nothing is
skipped for a reason related to this phase.

`tests/test_embodied.py` has two halves. The first pins the **contracts**: objective
measurability (four refusals), the terminal-status set, evaluation rates that are `None`
until something measured them, observation provenance (a region is never a measurement, a
low-confidence "enemy" stays unconfirmed), `effect_observed`'s three answers including
"a reading that cannot report the fact is unknown, not a pass", the registry's
refuse-by-name, the simulation's bounds and its five faults, the tactical choice and its
tasks, the door's seven refusals, the registry's pause/resume/cancel/restore rules, the
capability table's live availability, the config clamps, and the overview's own
vocabulary. The second drives the **real application over HTTP** (`create_app()` against a
temporary data directory) through workflows A–L: the full cycle, refusals counted as
refusals, the detour, the API's refusals with their codes, pause/resume/cancel, budget
honesty, a frozen environment, a fogged one, a timed-out input, the drifting environment,
the event lifecycle with its declared payload fields, the three status surfaces, the audit
trail and a restart.

**The new pins are mutation-tested**: reverting the transition recording, the state
mirror, the refusal counter and the budget sync each makes the suite fail (1 + 9 + 1 + 1
tests respectively), and so do the three added by the final verification pass — publishing
whole records on the sessions list (1 test), dropping the restore-time rebuild (2 tests)
and pointing an unbuildable configuration at the default world (1 test). The verification
audit then added two more, both watched to fail as well: the environment echoing the
requested duration instead of measuring it (1 test) and the input door dropping its clamp
(1 test). Each mutation was applied to the real source, watched to fail, and restored
byte-for-byte. A pin that cannot fail is not a pin.

### Final verification audit (re-run against the tree this report describes)

`tests/test_embodied.py` (**122 passed, 84 subtests**), `tests/test_goal_planning.py` and
`tests/test_agentic_rl.py` (**265 passed, 6 subtests**) and `tests/test_web_api.py`
(**65 passed, 34 subtests**) and `tests/test_api_reference_sync.py tests/test_api.py`
(**8 passed, 248 subtests**) all exit 0, `python scripts/generate_api_reference.py --check`
reports **docs/API.md: in sync**, both mypy views report
**Success: no issues found in 399 source files**, `scripts/benchmark_embodied.py` reports
**9/9 scenarios matched their declared outcome** (exit 0, baseline repeated 5×: min
187.9 ms, median 194.4 ms, max 200.1 ms, stdev 4.5 ms), and `ruff check
src/novacontrol/embodied/ --select F401,F841,F811` is clean. That set is the affected
surface: the package is imported only by `application.py`, the benchmark and its own tests,
and the HTTP workflows run inside `tests/test_embodied.py`. The phase pass's full-suite
slices, the live-`uvicorn` HTTP legs, the restart run and the 161-check sweep were **not**
re-run in this audit — their results in §11 and §12 stand as the phase run reported them,
and the only code changed since is described in §13.

## 12. Real user-level workflow results

A real `uvicorn` server was started from an isolated working directory (its own `data/`)
and driven over HTTP with a scripted workflow. All 16 legs passed:

1. `GET /embodied/status` — allowlist `["gridworld"]`, seven scenarios, ceilings, the
   input door's scope, the capability table, `restored` report.
2. `GET /embodied/environments` — `gridworld` operational, six capabilities, five
   verified, `semantic_object_recognition` UNSUPPORTED with its reason.
3. `GET /embodied/overview` — the cycle, the lifecycle tables, 13 event types, the
   honesty flags, `deferred`.
4. `POST /embodied/sessions` — creates, `actions_taken: 0` (creating runs nothing).
5. `POST /embodied/sessions/{id}/run` — `completed` in 4 inputs, verdict met with
   `env.target.collected=true` observed, completion rate 1.0, 4.0 inputs per success.
6. `GET /embodied/sessions/{id}` — the trajectory reads connect → observe → decide → act →
   observe → … → verify, indexes dense, no frame and no prompt in the body.
7. `POST /embodied/sessions/{id}/run` again — **422**, "already completed".
8. `POST /embodied/sessions` with `{"request": "get better at the game"}` — **422**,
   naming the missing observable condition.
9. `POST /embodied/sessions/{id}/run` for an unknown id — **404**.
10. `GET /embodied/sessions` — one completed session, `live: 0`.
11. Scenario `detour` — completed in 8 inputs with 2 recoveries (the wall is remembered).

The restart path was then driven as its own scripted run, because a restart is the one
thing an in-process test cannot fully stage. Two boots over one `data/` directory:

- **Boot 1** created and COMPLETED one session (history) and left a second session live
  in the configured world `gridworld:open-east`.
- **Boot 2** restored exactly that one (`restored: 1`, `skipped: 1`, `interrupted:
  [sess-…]`), rebuilt `gridworld:open-east` on its own registry, and **13/13** restore
  checks passed: the session is listed as `interrupted`, carries a reason, is flagged
  `needs_revalidation`, holds nothing and had sent nothing — and running it over HTTP
  returns 200 and `completed` with the flag cleared and a fresh reading taken.
- The same boot then passed a **161-check** endpoint sweep (all 10 routes) covering every
  scenario's declared ending, the API refusals with their codes, the lifecycle moves,
  the 404s, the operator surfaces and their body-leak scans.
12. Scenario `refusing` — completed with **2 refusals and 0 failures**.
13. Scenario `frozen` — `budget_exhausted`, **0 inputs sent**, 4 stale readings,
    reason "the recovery ceiling was reached (4/4): the observation is stale".
14. Scenario `fogged` — `stuck`, 0 inputs, reason "no structured state (regions only)".
15. Pause → run (**422**, "this session is paused") → resume (`created`,
    `needs_revalidation: true`) → run → **completed** (the revalidation really happened).
16. Cancel → `cancelled`, `live: 0`, the record still readable, a new session creatable at
    once — and the audit trail holds one row per input, refusals included.

`GET /status` carries the same counts under `app.embodied`.

## 13. Defects found and fixed

**Eleven defects, all fixed** — eight found by driving the loop (the workflows, the
benchmark and the restart run, not by reading the code) and three by the final verification
audit (§11):

1. **A pause, a resume and a cancel were never recorded.** `pause`/`resume`/`cancel` built
   the new state and returned it without writing it into the registry — so a pause was
   cosmetic (the door still saw a live session and would send an input), a cancelled
   session kept its live slot, the status was lost on restart, and the door's own PAUSED
   rule was unreachable. All three now go through one `_transition` that stores and
   persists.
2. **The loop's own state changes never reached the registry.** Fixing (1) exposed this:
   `resume` marks a session as needing revalidation, the loop *does* revalidate it — but
   only in the agent's copy, while the door reads the registry, so every input of a
   resumed session was refused and the session reported `stuck` with "a required action
   was not authorized". An `on_state` seam now mirrors every transition into the registry,
   keeping the door's own counters authoritative.
3. **An environment's refusal was reported as a malfunction.** `Evaluation.actions_rejected`
   was a permanent zero while two refused inputs were counted as failures — the same
   action described wrongly, and the retry path invisible. The door now tells the loop how
   many inputs the environment refused, and the loop reports a rejection as a rejection.
4. **The shared audit trail recorded nothing for embodied inputs.** The seam asked the
   injected audit object for `append`, which the application's `AuditLogger` does not have,
   so the call silently no-opped while `overview()` claimed "every input is written to the
   shared automation audit trail". The manager now takes an `AutomationAuditLog` (in memory
   by default, exactly like the device controllers) and appends one `EmbodiedActionResult`
   per input, whatever the ending.
5. **The action budget was checked against a stale count.** The count was synced inside the
   cycle while the budget was checked before it, so a 2-input budget sent **three** inputs
   and then reported two. The count is now taken from the door before each decision.
6. **Two duplicate/contradictory declarations.** `EmbodiedManager.capability_rows()` was a
   second, never-called 7-row capability table (deleted — the registry is fed by
   `capabilities.py`), and Phase 24's `planning.game_agent` "FUTURE" row still said "Phase
   25 owns it" while Phase 25 now ships `embodied.*` rows on the same registry — one thing
   reported as both "not built" and "available" (the row is deleted, and the two tests that
   pinned it now pin its absence and point at where the capability lives).

7. **A restored session could be reported RESTORED and yet be impossible to run.** The
   record stores the CONFIGURED environment (`gridworld:open-east`) while a new process has
   only minted the default one, so after a restart `run` died with
   `EnvironmentRefused: environment 'gridworld:open-east' is no longer registered` — a 500
   for a session the boot report had just called restored. Found by the two-boot restart
   run (§12), not by a test: the existing restore pins checked the *record* (status,
   reason, flags) and never that the session could still be driven. The rebuild now reads
   the scenario and the fault budget back off the record's own key — this module's own
   encoding, so the decoder is deterministic — and registers the same world the session was
   created in; a configuration this build cannot rebuild returns `None` and the reason is
   stated on the record instead of the session being quietly pointed at some other world.
8. **The sessions list published whole records.** `GET /embodied/sessions` returned
   `SessionState.to_dict()` per row, so every row embedded `last_observation.structured`
   and the full `trajectory` — while the documented contract
   ([EMBODIED.md](EMBODIED.md)) is "lifecycle state and a summary" with the record on the
   single-session view, and the status surface is already pinned content-free. Found by the
   endpoint sweep's body-leak scan. Rows now carry the lifecycle fields only
   (`_EMBODIED_SESSION_ROW_FIELDS`), and the pin asserts both halves: no `objective`,
   `verdict`, `last_observation`, `trajectory` or `evaluation` on a row, and all of them on
   the view.

Also fixed while finishing: the verification harness itself asserted the wrong direction for
`released` after a pause (the field is `True` when nothing is held, so the check as written
could only pass for a session still holding its input); the check now asserts `True`, which
is what the sweep observed and what the manager does. And seven mypy errors in the new
package (a possibly-`None` tuple index, four `Any | None` collaborator accesses and two
un-narrowed optionals) were narrowed at the source rather than suppressed.

Found by the final verification audit, which read the package against its own claims:

9. **A policy refusal was reported as a budget that ran out.** When the approval layer
   refused the plan itself (a `PermissionError` out of Phase 24's planning), the loop
   finished the session as `budget_exhausted` — "the budget ran out" for a session whose
   budget was untouched — and counted the refusal under `invalid_actions`. A refusal is an
   answered request, not a malformed action and not an exhausted allowance: the loop now
   reads the run's own `denied` status, records the attempt as `actions_rejected`, writes
   the refusal into the trajectory with its reason, and ends the session `DENIED` with
   **0 inputs sent**. Pinned by `test_a_policy_refusal_ends_the_session_as_denied_with_nothing_sent`
   and `test_a_refusal_is_a_rejection_and_never_a_retry` in `WorkflowMDenialTests`, which
   raise the risk of the input's own permission scope to CRITICAL and drive the whole path
   over HTTP (`denied`, 1 rejection, 0 invalid, 0 failures, and a
   second `run` refused with 422). Before this, the ending, the counters and the reason
   for a policy refusal had **no test at all**.
10. **A pin that could not fail.** `test_a_duration_is_clamped_to_the_ceiling` asserted
   `result.duration_ms <= 2000` after handing in a ten-million-millisecond input — but
   `ActionResult.duration_ms` is the elapsed time the environment *measured* (0.12 ms in
   this run), so the assertion held with the clamp deleted. It was proven vacuous by
   removing the clamp entirely: the old assertion still passes. Replaced by two pins that
   can fail, each mutation-tested: the environment reports a **measurement, never the
   number it was handed** (`test_the_reported_duration_is_measured_not_echoed`), and the
   bound on a request is enforced where the input leaves — the door's clamp, read back off
   the session's `last_action` (`test_the_duration_handed_to_the_environment_is_clamped`).
11. **A second ceiling, and code that outlived its use.** `simulation.py` carried its own
   `HARD_MAX_INPUT_MS` — a second definition, exported from a second module, of the same
   2000 ms ceiling `manager.py` already owns — plus a `_clamp` whose result was discarded
   on every path, which is precisely why (10) could not see it. Both are gone; the module
   now states that the budget is bounded and enforced at the input door and is not
   re-derived. The same read-through removed three dead variables (`facts` in
   `tactical.tasks_for`, a `verified` read of a run whose status the loop deliberately does
   not consult, the unused clamped duration) and six unused imports; `ruff check
   src/novacontrol/embodied/ --select F401,F841,F811` is now clean and both mypy views stay
   clean.

## 14. Benchmark methodology and metrics

`scripts/benchmark_embodied.py` builds a REAL `NovaControlApplication` over a temporary
data directory and drives nine workflows through the real manager, then **checks its own
expectations** (ending status, verdict, input-count bounds, minimum recoveries, refusals,
failures, stale readings, and "the audit trail holds exactly what was sent"). A scenario
that stops behaving exits non-zero. The only substitution is that Ollama/cloud probing is
disabled for the run, so the measurement is of the embodied loop and not of a network
timeout; no model takes part in a session either way.

| Scenario | Ending | met | inputs | attempts | observations | recoveries | refusals | failures | stale | trajectory | run ms | ms/input | record B | save ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| open-east | completed | yes | 4 | 4 | 5 | 0 | 0 | 0 | 0 | 15 | 182 | 45.4 | 10,112 | 0.75 |
| detour | completed | yes | 8 | 8 | 9 | 2 | 0 | 2 | 0 | 29 | 400 | 50.0 | 16,746 | 1.06 |
| drifting | completed | yes | 8 | 8 | 9 | 2 | 0 | 2 | 0 | 29 | 399 | 49.9 | 16,744 | 1.18 |
| refusing | completed | yes | 8 | 8 | 9 | 2 | **2** | **0** | 0 | 29 | 397 | 49.6 | 16,932 | 1.91 |
| timing-out | completed | yes | 8 | 8 | 9 | 2 | 0 | 2 | 0 | 29 | 397 | 49.6 | 16,898 | 1.61 |
| frozen | budget_exhausted | no | **0** | 0 | 4 | 4 | 0 | 0 | 4 | 9 | 3.2 | — | 6,908 | 1.67 |
| fogged | stuck | no | **0** | 0 | 1 | 0 | 0 | 0 | 0 | 3 | 2.3 | — | 5,300 | 2.25 |
| tiny-budget | budget_exhausted | no | 2 | 2 | 2 | 0 | 0 | 0 | 0 | 7 | 79 | 39.6 | 7,064 | 1.73 |
| corridor-20 | completed | yes | 21 | 21 | 22 | 0 | 0 | 0 | 0 | 66 | 1,052 | 50.1 | 30,733 | 2.04 |

Reading the numbers honestly:

- The configured pace is 50 ms between inputs and the wall clock includes it:
  `(inputs − 1) × 50 ms` is 1,000 of the corridor's 1,052 ms, leaving **≈2.5 ms of loop
  work per input**; the 4-input baseline leaves ≈7.9 ms per input because it carries the
  one-off goal creation.
- Boot is ≈228 ms. The baseline repeated 10×: **min 193.1 ms, median 195.8 ms, max
  198.6 ms, stdev 1.5 ms** — the loop is deterministic, and the variance is the 50 ms pace
  plus scheduling noise.
- A session record is ~10 KB after 4 inputs and ~31 KB after 21, and persisting one costs
  0.8–2.0 ms (the store rewrites the whole sessions payload — one key, bounded by
  `max_sessions`).
- No scenario sent an input when the reading was stale, and no scenario claimed a
  completion its conditions did not support.

## 15. Known limitations

- **One environment.** The grid world is deliberately small: one agent, one target, four
  moves, no other actors. It is enough to exercise the loop and every failure mode, and it
  is not a game.
- **A deterministic simulation only.** No real game, no window driving, no screen
  reading — §16.
- **The tactical layer is a policy, not a planner.** `choose()` closes a Manhattan
  distance and remembers blockages; it has no notion of cost, timing or an opponent, and
  a maze needing real search would defeat it (it reports `stuck` rather than flailing).
- **One goal per session.** Replanning reuses that goal, so Phase 24's replan ceiling is
  the session's real bound; a session cannot invent a sub-goal tree.
- **The pace is a configured sleep**, not a measured controller latency: this phase owns a
  simulation, and a real adapter's latency would have to be measured on that adapter.
- **Persistence rewrites every session** on each input, which is fine at
  `max_sessions: 4` and would not be at hundreds.
- **The record size grows with the trajectory** (~450 B per entry), bounded by
  `max_trajectory`.
- **No CLI command and no web panel** (§10).

## 16. Mock and provider-dependent capabilities

| Capability | State | Availability here |
|---|---|---|
| 13 rows — `embodied.session_lifecycle`, `environment_adapter`, `gridworld_environment`, `structured_observation`, `world_model_update`, `tactical_navigation`, `action_selection`, `precondition_safety`, `input_gating`, `action_verification`, `outcome_verification`, `recovery_replanning`, `trajectory_evaluation` | IMPLEMENTED | AVAILABLE (probed live from the manager) |
| `embodied.screen_environment` | FUTURE | UNAVAILABLE — "no screen/game adapter ships in this build" |
| `embodied.semantic_perception` | PROVIDER_DEPENDENT | UNAVAILABLE — "no environment adapter reports semantic perception, so regions stay regions" |
| `embodied.batching`, `embodied.learning_from_sessions`, `embodied.generalization` | FUTURE | UNAVAILABLE — a later phase |

There is **no mock environment** in the shipping path: `EnvironmentStatus.MOCK` exists and
a connecting adapter that reports itself as a mock is refused by `_connect` and by
`revalidate`, so a mock can be used in a test but never presented as a working
environment. The benchmark and test doubles (a step object, an in-memory store) are data,
not environments.

## 17. Compatibility with Phases 15–24

- **Phase 15 evaluation**: nothing is recorded automatically; a session's own `Evaluation`
  is the record, and the trajectory it is built from is bounded.
- **Phase 22/23/24**: the world model is read and written as observed facts, Phase 24 is
  the only action selector, and Phase 23's rule book is not consulted by this phase at all
  (an embodied session makes no prediction).
- **Phase 8**: the same permission layer, the same verification ordering, the same retry
  ceilings — a refusal is never retried through.
- **Phase 13**: the same audit trail as the device controllers, joined through an
  `AutomationAuditResult`-shaped row.
- **Cross-phase truth fix**: Phase 24's `planning.game_agent` deferral row was removed and
  its two test pins updated (§13.6).
- **Tests that could have been broken**: `test_goal_planning.py` (updated for the row
  removal), `test_api_reference_sync.py`/`test_api.py` (248 routes in sync),
  `test_web_platform.py`'s contract tests (untouched, green in the full suite).

## 18. Explicitly deferred Phase 26

Named in `embodied.DEFERRED_PHASES` and in `overview()["deferred"]`: Phase 26
(generalization, ARC and intelligence evaluation), a real game or window adapter, learning
a policy from recorded sessions, and batching several inputs into one authorized macro
action. Each of the first two has a capability row reporting the absence with its reason.

## 19. Remaining failures and unverified requirements

- **CI is not run.** Everything in §11 is local to this Windows machine. This repository
  has previously been broken by a defect only Linux CI could see (a path split on a
  literal separator, and a store constructor that only raised on Linux), so CI green is
  the one acceptance item this report does **not** claim.
- **No real environment was driven.** The phase's own specification allows a controlled
  game or simulation; the simulation is what was tested, end to end, and no claim is made
  about driving a real game.
- **No vision provider was wired**, so the visual path was exercised through the fogged
  scenario's regions only — the "regions stay regions" rule is verified, not semantic
  perception.
- **The audit trail is in-memory by default**, like the device controllers'. That is
  parity, not a missing feature; a deployment that needs the rows on disk attaches the
  same sink the Phase 13 logger uses.
- **Timing was measured once per scenario** (plus a 10× repeat of the baseline); the
  2.5–7.9 ms/input figures are single-machine measurements, not a benchmark suite. The
  audit re-ran the benchmark (9/9, baseline stdev 4.5 ms over 5 runs) rather than
  re-deriving the per-scenario table.
- **The audit re-ran the affected set, not the whole suite** (§11): the phase pass's
  seven-slice full-suite run, the live-`uvicorn` legs, the two-boot restart run and the
  161-check endpoint sweep are reported as that pass observed them. Nothing in §13 items
  9–11 changed a route, a schema, a status or an ending that those runs assert, and the
  workflow suite that drives the same paths in-process is green.

## 20. Final acceptance checklist and verdict

| Acceptance criterion | Evidence | Status |
|---|---|---|
| Observe the environment | §4, §5; `ObservationProvenanceTests`; workflow A; live legs 5–6 | ✅ |
| Interpret the current state | §4; observed-vs-uncertain tests; fogged scenario reports no state | ✅ |
| Update the world model | §7; `to_world_observation`; live leg 6 trajectory entries | ✅ |
| Identify the current objective | §5; the session's `Objective`, refused when unmeasurable (422) | ✅ |
| Select an action through Phase 24 | §6; `GoalManager.create/decompose/plan`; no-planner refusal test | ✅ |
| Check preconditions and safety | §9; the fresh-state gate + `self.risk`; the door's seven refusals | ✅ |
| Execute inputs through an approved controller | §1, §9; the ONE door; audit rows = inputs sent | ✅ |
| Observe the result | §5, §8; a second fresh reading after every input | ✅ |
| Verify whether the action and the objective worked | §8; three-valued `effect_observed`; verdict tests; live leg 5 | ✅ |
| Update memory and trajectory | §5; `TacticalMemory`; bounded trajectory; no frames/prompts stored | ✅ |
| Recover or replan when necessary | §8; detour/drifting/refusing/frozen scenarios; benchmark | ✅ |
| A controlled game or simulation environment | §4; the grid world, seven scenarios, five faults | ✅ |
| Integrated with the existing architecture, not standalone | §2, §17; every collaborator is an existing subsystem | ✅ |
| Relevant user-facing workflows executed successfully | §12; 16/16 live HTTP legs, plus 13/13 restart checks and a 161-check route sweep; benchmark 9/9 self-checked | ✅ |
| Genuine defects found and fixed | §13: eleven defects, each fixed and pinned, with the pins mutation-tested as §11 lists | ✅ |
| Regression, type and API checks completed | §11: 3,901 passed + the 5 host-memory cases re-verified passing / 12 skipped, both mypy views, API in sync; the audit re-ran the affected set (122 + 265 + 65 passed), both mypy views and the benchmark 9/9 | ✅ |
| Documentation matches the final code | this file, [EMBODIED.md](EMBODIED.md), `STATUS.md`, `DEVELOPMENT_LOG.md`, `README.md`, `API.md` | ✅ |
| No Phase 26 functionality claimed | §18; `embodied.generalization` UNAVAILABLE with its reason | ✅ |

**Verdict: PASS.** The phase ships a real, measured, end-to-end embodied agent over an
environment this build owns, integrated with the planning, permission, world-model, event
and audit machinery that already existed. The two items that are *not* closed are named
above rather than glossed: CI has not run on this tree, and no real game or window was
driven — the phase specification allows a controlled simulation, and that is what is
claimed.
