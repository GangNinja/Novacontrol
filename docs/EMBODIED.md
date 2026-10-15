# Embodied agents and controlled environments (Phase 25)

NovaControl can plan and it can act on this machine. Phase 25 adds the case where
acting means *playing*: an agent that reads an environment's state, decides what to
do, sends one bounded input, looks at what happened, and either carries on or
changes its mind — inside an environment this build owns and can therefore
reproduce.

This document is the guide to that package: what it does, what it refuses to do,
where each part lives, and how to drive it.

- Package: `src/novacontrol/embodied/` (9 Python files including `__init__.py`, 5,705 lines in the current tree)
- Tests: `tests/test_embodied.py` (122 tests + 84 subtests)
- Benchmark: `scripts/benchmark_embodied.py` (9 self-checking scenarios)
- Report: [PHASE25_REPORT.md](PHASE25_REPORT.md)

## The loop

```
observe the environment          →  adapter.observe()          structured, with provenance
interpret the reading            →  perception.facts_from_observation()
update the world model           →  world.observe()             Phase 22, observed facts only
identify the objective           →  the session's Objective
select an action                 →  tactical.choose() → Phase 24 GoalManager.plan()
check preconditions and safety   →  the plan's fresh-state gate + the permission layer
send the input                   →  manager.execute_step()      THE ONE DOOR
observe the result               →  adapter.observe()           a SECOND, fresh reading
verify                           →  effect_observed() + agent.verdict()
update memory and trajectory     →  TacticalMemory + SessionState.trajectory
recover or replan                →  agent._recover()            bounded, and a refusal is never retried
```

The loop never raises to report an outcome. An environment that breaks, a
controller that refuses and a planner that declines are all *session statuses with
a reason*, because "the run threw" is not something an operator can act on.

## The one input door

Every environment input in this build leaves through `EmbodiedManager.execute_step`,
reached as the `environment_input` plan action. It is deliberately LAST in the
application's step-handler table, so every gate above it (the privacy gate on an
external tool, the approval gate on a command) still runs first. The door itself
enforces, in this order:

1. the session exists, and the step names an input;
2. the session is not paused, not finished, and not awaiting revalidation;
3. the environment is registered and **allowlisted by name** (`embodied.environments`);
4. the environment is OPERATIONAL right now;
5. the input is paced by `action_interval_ms`;
6. the duration is clamped to `embodied.max_input_ms` and the objective's own ceiling;
7. the input is sent, and the result is audited on the shared automation trail;
8. a timeout, a refusal and a failure each raise a *distinct* error and stop the
   attempt — none of them is re-sent here, because deciding whether an input landed
   is the loop's job, after a fresh reading.

A restored session cannot skip step 2: a restart marks it INTERRUPTED with
`needs_revalidation`, and the only thing that clears that flag is a fresh
structured reading arriving from a live environment.

## Environment contract

An adapter declares what it is (`EnvironmentInfo`: id, kind, status, allowed,
actions, observations, capabilities) and answers six calls: `connect`, `status`,
`observe`, `available_actions`, `send_input`, `release_inputs`, `shutdown`.

`env.observe()` returns an `Observation` whose `basis` says WHERE the reading came
from — `structured` (the environment's own state: a measurement), `visual`
(inferred from pixels: a claim), `predicted` (a projection), `unknown` — and whose
`freshness` says how much it can still be leaned on (`fresh`, `stale`, `failed`).
Only a fresh, structured reading may authorize a state-dependent action;
`to_world_observation` keeps the provenance when the facts reach the world model.

The shipped environment is `gridworld`: a deterministic grid with walls, an agent
and one collectible target, with structured readings, optional fogged readings
(unlabeled regions only) and four named faults — `timeout`, `drift`, `reject`,
`freeze` — that a scenario declares so its own name is the whole truth about it.

Named scenarios: `open-east`, `detour`, `drifting`, `refusing`, `frozen`,
`timing-out`, `fogged`. A scenario is *configuration*, so a differently configured
world is its own environment id (`gridworld:detour`) and is not a mutation of the
one somebody else may be driving. A scenario's faults always survive an explicit
`faults` mapping: a caller can ask for more trouble, never less.

## Verification, and the three-valued answer

Two different things are verified, and conflating them is how an embodied agent
lies:

- **The action**: did the effect the input declared actually appear? `effect_observed`
  answers `True` (a fresh structured reading shows every declared fact), `False`
  (the reading could report the fact and it was something else), or `None` (the
  check could not be made). `None` is never rounded up to a success.
- **The objective**: `agent.verdict(observation)` sets `met=True` only when a fresh
  reading shows **every** success condition holding; otherwise it reports what was
  observed and what is still missing.

A session reports `completed` only from a `met` verdict. Running out of actions,
hitting the seconds budget, exhausting the recovery ceiling, an environment that
will not answer and a stuck position are all their own endings with their own
reasons — and `Evaluation.objective_completion_rate` is `None`, not `0.0`, when no
verdict was ever reached.

## Recovery

`_recover` is bounded by `objective.max_replans + 1` attempts and always records
*why* the previous attempt did not work, so the next choice is a different one:

- a blocked direction is remembered per position (`TacticalMemory`), which is what
  turns "the way east failed" into "go around";
- repeating an attempt already made is refused as a new attempt (counted as
  `repeated_actions`) rather than retried forever;
- a **refusal** is an answer, not a speed bump: the permission layer saying no ends
  the session as `denied`, and it is never retried through;
- a timed-out input is never blindly re-sent — the state is re-read first;
- when every route from a cell is known blocked the session ends as `stuck`, which
  is not an exhausted budget: no budget ran out, the environment did.

## Sessions, persistence and restart

A session holds its environment, objective, budgets, verdict, trajectory and
evaluation. Creating one runs **nothing**; the first reading is what starts it.

Sessions and the environment inputs are recorded as operational metadata only: the
action, where it came from, what was expected, what was seen, and the outcome. No
frame body, no prompt, no reasoning trace — pinned by a test that scans the record.

The registry (`EmbodiedManager._sessions`) is the authority on a session's status,
because that is what the input door, the live-session ceiling, the list surface and
the boot restore read. Every transition — including the ones the *loop* makes mid-run,
such as clearing `needs_revalidation` — is mirrored into it.

On boot, sessions are restored as INTERRUPTED with `needs_revalidation=True`, and a
finished session is not restored at all: history is not work.

A restored session's **world is rebuilt from the record itself**: the environment id this
manager minted (`gridworld:detour`, or `gridworld:open-east:drift2` for a fault budget)
is read back — family, scenario, faults — and the same world is registered again, so a
session that comes back can actually be revalidated and driven. Reporting a session as
restored while it died with "environment … is no longer registered" was a real defect found
by driving a restart. A configuration this build cannot rebuild is **stated on the record**
(its reason says so, the record stays readable and cancellable, and running it is refused)
rather than being silently pointed at some other world.

## Budgets

Every ceiling exists twice: a configuration value that may ask for less, and a hard
bound it can never exceed.

| Setting | Default | Hard ceiling |
| --- | --- | --- |
| `embodied.max_sessions` | 1 | 4 |
| `embodied.max_actions` | 40 | 400 |
| `embodied.max_seconds` | 120 | 1800 |
| `embodied.max_replans` | 3 | 20 |
| `embodied.max_plans` | 40 | 400 |
| `embodied.max_input_ms` | 500 | 2000 |
| `embodied.action_interval_ms` | 50 | 2000 |
| `embodied.environments` | `["gridworld"]` | — |

The action count is **what the door sent**, not what the loop intended, and the
budget is checked against that figure before each attempt — so a 2-input budget
sends two inputs.

## API

All routes are API/CLI surfaces; there is no web panel for this phase yet, and the
contract says so rather than inventing a control the frontend does not have.

| Route | What it does |
| --- | --- |
| `GET /embodied/status` | Allowlisted environments with live availability, scenarios, session counts, ceilings, the input door's scope, the boot restore report, the capability table |
| `GET /embodied/overview` | What the phase ships and what it refuses to claim |
| `GET /embodied/environments` | Each adapter with the capabilities it declares and whether each was VERIFIED |
| `GET /embodied/sessions` | Every session with its lifecycle state and a summary — a row carries no objective, verdict, observation or trajectory; those are the single-session view |
| `POST /embodied/sessions` | Record a session from an objective (runs nothing; 422 when the objective is unmeasurable) |
| `GET /embodied/sessions/{id}` | The record: objective, status, verdict, trajectory, evaluation |
| `POST /embodied/sessions/{id}/run` | Drive the bounded loop |
| `POST /embodied/sessions/{id}/pause` | Pause it; what it holds is released first |
| `POST /embodied/sessions/{id}/resume` | Resume it as needing revalidation |
| `POST /embodied/sessions/{id}/cancel` | Cancel it; the record stays readable |

An objective is `{request, success_conditions: ["env.target.collected=true"], …}`. A
condition that is not a `key=value` check, or an objective with no conditions at
all, is refused with 422 *before* anything runs — "get better at the game" is not an
objective this phase can verify.

## What this phase does NOT do

- No real game or window adapter ships. `embodied.screen_environment` is reported
  UNAVAILABLE with that reason; authorizing one needs an owner-permitted
  environment and is not claimed.
- No object identity is invented. With no vision-language provider wired, a region
  stays a region: `embodied.semantic_perception` reports the absence, `EntityRead.named`
  is false, and a low-confidence semantic claim is kept as unconfirmed.
- Nothing is learned from sessions, and nothing generalizes to a game this build has
  never seen — both are Phase 26 territory and are reported as such.
- No batching: one input per cycle, no macro actions.
- No model is loaded, no GPU is required, and the trajectory stores no frames or
  prompts.

## Measured on this machine

`PYTHONPATH=src python scripts/benchmark_embodied.py` — 9/9 scenarios matched their
declared outcome:

| Scenario | Ending | met | inputs | recoveries | refusals | failures | stale | run ms | ms/input |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| open-east | completed | yes | 4 | 0 | 0 | 0 | 0 | 182 | 45.4 |
| detour | completed | yes | 8 | 2 | 0 | 2 | 0 | 400 | 50.0 |
| drifting | completed | yes | 8 | 2 | 0 | 2 | 0 | 399 | 49.9 |
| refusing | completed | yes | 8 | 2 | **2** | 0 | 0 | 397 | 49.6 |
| timing-out | completed | yes | 8 | 2 | 0 | 2 | 0 | 397 | 49.6 |
| frozen | budget_exhausted | no | **0** | 4 | 0 | 0 | 4 | 3.2 | — |
| fogged | stuck | no | **0** | 0 | 0 | 0 | 0 | 2.3 | — |
| tiny-budget | budget_exhausted | no | 2 | 0 | 0 | 0 | 0 | 79 | 39.6 |
| corridor-20 | completed | yes | 21 | 0 | 0 | 0 | 0 | 1052 | 50.1 |

The configured pace is 50 ms between inputs, and the measurement includes it:
subtracting `(inputs - 1) × 50 ms` leaves **≈2.5 ms of loop work per input** on the
20-cell corridor and ≈7.9 ms on the 4-input baseline (which carries the one-off goal
creation). A session record is 10 KB after 4 inputs and 31 KB after 21, and
persisting one costs 0.8–2.0 ms. Boot is ≈228 ms. The baseline repeated 10×:
min 193.1 ms, median 195.8 ms, max 198.6 ms, stdev 1.5 ms.

## Running it

```bash
PYTHONPATH=src python -m pytest tests/test_embodied.py -q
PYTHONPATH=src python scripts/benchmark_embodied.py --json
PYTHONPATH=src python -m uvicorn novacontrol.api.app:create_app --factory --port 8000
curl -s -X POST localhost:8000/embodied/sessions \
  -H 'content-type: application/json' \
  -d '{"objective":{"request":"collect the target","success_conditions":["env.target.collected=true"]}}'
curl -s -X POST localhost:8000/embodied/sessions/<id>/run
```
