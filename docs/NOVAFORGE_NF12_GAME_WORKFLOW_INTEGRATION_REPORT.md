# NovaForge NF-12 — Game Discovery & Launch Workflow Integration

Date: 2026-10-23
Host: this laptop (Windows 11, 15.37 GB visible memory, Steam on internal fixed
drives `E:\STEAM` and `D:\SteamLibrary`, Google Play Games installed)

NF-11 built the catalog and the verified launch lifecycle. NF-12 makes them
**reachable**: a Game Library tab in the NovaForge panel, a small HTTP surface,
`launch <game>` / `is <game> installed` in chat, and the existing
`/command/plan` → `/command/execute` approval door. No new discovery logic, no
second automation stack, no capture change.

The phase's rule is unchanged and now visible in the UI: **a discovered row is
not a launch test.** Only `tested: true` means this process actually started and
verified the game.

---

## 1. What was integrated, and where

| Layer | File | What it contributes |
| --- | --- | --- |
| Request vocabulary | `src/novacontrol/novaforge/launcher.py` | `game_request()` (launch / status / attach shapes, chains refused), `row_matches()` (search), `GameLaunchService.status_payload()` (owned claim, history, memory gate) |
| Workflow | `src/novacontrol/application.py` | `game_library`, `game_library_resolve`, `plan_game_launch`, `execute_game_launch`, `close_game_launch`, `attach_game`; the chat intercept; `plan_command_or_game`; the game branch in `execute_command` |
| HTTP | `src/novacontrol/api/app.py` | `GET /novaforge/library`, `GET /novaforge/library/resolve`, `POST /novaforge/library/launch`, `POST /novaforge/library/close`, `POST /novaforge/library/attach`; `/command/plan` now plans game launches through the library |
| Surface registry | `src/novacontrol/api/models.py`, `api/route_consumers.py`, `docs/API.md` | 5 new declared routes (328 total), each with its frontend consumer; docs regenerated |
| UI | `web/static/index.html`, `js/novaforge.js`, `nc-os.css` | the **Game Library** tab: search, rows with evidence/status/reason, preview → approve launch, close, attach, and four separate launch facts |
| Tests | `tests/test_novaforge_library.py`, `tests/test_novaforge_library_ui.py`, one derived assertion in `tests/test_novaforge_ui.py` | the API/chat/token contract, the Node-executed translations, the tab contract |

**J.A.R.V.I.S. reuse is unchanged from NF-11.** Every platform fact still comes
from `LocalDesktopRunner`: Steam manifests, Start Menu index, process identity,
memory, `launch_game_target`, `close_game_window`. NF-12 adds no seam; it adds
callers. The `GameLaunchService` behind `NovaForgeManager.game_launcher` is the
only object that launches or closes anything.

---

## 2. User-visible behavior

### Game Library tab (`NovaForge → Game Library`)

- Reads `/novaforge/library` when the tab opens and on **Refresh**; typing
  filters through the same endpoint (`q`), so the panel never filters a stale
  local list.
- Each row shows: canonical title, status chip, `launch-tested` chip when the
  service's own history says so, the launch source and target (e.g.
  `steam · 1446890`), and the reason when it is not launchable.
- `Preview Launch` is offered only for `INSTALLED_AND_LAUNCHABLE`. It calls the
  launch route **without** a token: the server resolves the name, runs the
  memory gate and returns a plan with a single-use token. The card states
  "nothing has started yet", lists the four steps, the resource measurement and
  the target.
- `Approve & Launch` sends that token with the exact command; the response is
  rendered as four separate facts (requested / process / window / verified) plus
  the process identity, and observation is reported separately.
- `Close Test-Launched Game` (also on the owned row) posts to the close route;
  the button is enabled only while `owned` is non-null. A game that was already
  running is only ever `Attach`ed — never closed.
- Counts, limitations and the `Close` label all come from the response; the
  panel owns no list of its own.

### Chat (`/ask`)

- `launch <title>` → route `game_launch`, a **plan** with a token; the existing
  chat command renderer draws it and its Approve button executes through
  `/command/execute`, which dispatches an approved game launch to the verified
  service.
- `check whether <title> is installed`, `is <title> installed`, `do i have
  <title>` → route `game_library`, answered from the catalog row.
- `attach to the game that is already running` → route `game_library`, attach
  result (never ownership).
- The gate is the catalog: a launch/status phrase is claimed only when the name
  is a candidate title or alias. "open calculator", "start the server" and
  every other application launch keep the desktop plan they had.

### HTTP

| Route | Behavior |
| --- | --- |
| `GET /novaforge/library?q=` | rows + counts + `owned` + history + memory + observation capability |
| `GET /novaforge/library/resolve?name=` | one row's status, launchability and gate; starts nothing |
| `POST /novaforge/library/launch` | no token = preview; token = execute exactly that plan |
| `POST /novaforge/library/close` | graceful close of `owned`, exit confirmed |
| `POST /novaforge/library/attach` | report a running instance, no ownership |
| `POST /command/plan` / `/command/execute` | game phrases use the same plan/token/verified path |

---

## 3. Status semantics and failure handling

Catalog statuses are NF-11's vocabulary, unchanged:

`INSTALLED_AND_LAUNCHABLE`, `ALREADY_RUNNING`,
`INSTALLED_BUT_LAUNCH_FAILED`, `NOT_FOUND_ON_INTERNAL_STORAGE`,
`REQUIRES_LAUNCHER_OR_RUNTIME`, `AMBIGUOUS_MATCH`, `NOT_TESTED`.

Two plan/execution-only states are added, and they are not catalog statuses:

- `LAUNCH_REFUSED` — the resource gate refused before a token existed.
- `waiting_for_approval` — a preview; nothing has started.

Execution stages are independent booleans: `launch_requested`,
`process_started`, `window_detected`, `launch_verified`. A process with no window
is reported as `INSTALLED_BUT_LAUNCH_FAILED` with `launch_verified: false`, and
is still tracked as this service's own so it can be closed. A failed command
carries its cause (`shop`-refused, exception text) in the summary and the
execution result, never as a success.

Failure mapping:

| Condition | Result |
| --- | --- |
| unknown / expired / replayed token | HTTP 403, nothing starts |
| ambiguous name | plan with no token and both candidates |
| not installed | plan with no token and the reason |
| runtime/emulator title | plan with no token and the runtime named |
| memory gate refused | no token, measurement quoted |
| no process appears | `process_started: false`, launch failed |
| process, no window | verified false, still owned |
| another game still owned | plan is `ANOTHER_GAME_OWNED` with no token |
| claim's process already exited | the stale claim is cleared and the plan proceeds |
| close not confirmed | `SHUTDOWN_TIMEOUT`, claim kept, never a kill |
| user-owned running game | `NO_OWNED_GAME` on close, never touched |

Observation is reported from the manager's own capture capability with
`implied_by_launch: false`. A verified launch says nothing about frames.

---

## 4. Live verification on this host

All of the following ran against a fresh server on `127.0.0.1:8002` built from
this working tree. Memory was the binding constraint throughout: 2.37–3.08 GB
free, 80–85 % used, which passes the configured gate (≥2.0 GB, ≤92 %) but is not
GTA V territory.

### Catalog

```
GET /novaforge/library                 (first read of the session)
12 discovered titles
  INSTALLED_AND_LAUNCHABLE 3
  ALREADY_RUNNING 1            (Freedom Fighters, pid 28600, window "Freedom Fighters")
  NOT_FOUND_ON_INTERNAL_STORAGE 5
  REQUIRES_LAUNCHER_OR_RUNTIME 3
q=freedom → 1 of 12, ALREADY_RUNNING
```

By the time the panel was checked in the browser, Freedom Fighters had been
closed by the operator, so the same read reported **4 installed · launchable**
and no running title. That is the catalog being live, not a discrepancy: a
running row is a process fact, not a stored status.

The 12th discovered title (`Supermarket Together`, Steam 2709570) is real but
outside the brief's inventory; it is listed rather than hidden. GTA V and
Supermarket Together are **launchable, not launch-tested**.

### Chat

```
/ask "check whether Freedom Fighters is installed"
  route game_library
  "Freedom Fighters: already running. Already running as Freedom (pid 28600) with
   window 'Freedom Fighters'. NovaForge attaches instead of launching a duplicate."

/ask "launch Shadow Fight Arena"
  route game_launch, status waiting_for_approval, token minted
  4 steps, memory 3.08 GB free / 80.0 % used
  no process started
```

### Launch → verify → close (library route)

```
preview   waiting_for_approval, token, 2.51 GB free / 83.7 % used
approve   LAUNCH_VERIFIED
          SFA, pid 25520, start 2026-10-23T01:43:30.5955071Z,
          window "Shadow Fight Arena"
          stages: requested/process/window/verified = true ×4
          observation.available = true, implied_by_launch = false
close     CLOSED — "exited after a graceful close (SFA, pid 25520)"
catalog   Shadow Fight Arena INSTALLED_AND_LAUNCHABLE, tested=true, owned=null
probe     Get-Process SFA → empty
```

### Launch through the unified command door

```
POST /command/plan   route game_launch, waiting_for_approval, token
POST /command/execute LAUNCH_VERIFIED — SFA, pid 30816, window verified
POST /novaforge/library/close → CLOSED (pid 30816), exit confirmed
replay the consumed token → HTTP 403, no second launch
```

### The ownership gate (added after the first live pass)

The first live pass revealed that a plan could be minted for a second game while
an earlier test-owned instance was still alive; execution would then refuse (or,
worse, a caller would see a server error). The preview now asks the service for
its **live** claim first (`owned_claim`, which also clears a record whose process
exited on its own):

```
while SFA (pid 23468) was owned:
  POST /novaforge/library/launch {"command":"launch GTA V"}
  → ANOTHER_GAME_OWNED, token absent
    "Shadow Fight Arena was started by this service (pid 23468) and has not
     been confirmed closed. Close it before launching another game."
close → CLOSED (pid 23468), exit confirmed, owned = null
catalog → tested=true, no process left
```

### The panel, in a real browser

On the running server, the NovaForge panel showed **10 tabs** including Game
Library. Opening it rendered **12 rows** with counts
`12 of 12 discovered titles · 4 installed · launchable · 5 not found · 3 needs a
runtime`, the close button disabled (nothing owned), and 4 stated limitations.
`Preview Launch` on Shadow Fight Arena rendered the plan, the four steps, the
gate (`2.69 GB free, 83% used`), `Approve & Launch`, and every stage as false.
Approving through the button produced `LAUNCH_VERIFIED`, SFA pid 33168, all four
stages true, and enabled the Close button; closing reported `CLOSED` with the
pid and released ownership. **No console messages or errors** were produced.

No keyboard or mouse input was sent to any game, and no game was left running.

---

## 5. Automated tests

```
PYTHONPATH=src python -m pytest tests/test_novaforge_library.py -q
  40 passed, 19 subtests passed

PYTHONPATH=src python -m pytest tests/test_novaforge_library_ui.py -q
  13 passed

PYTHONPATH=src python -m pytest tests/test_novaforge_launcher.py \
    tests/test_novaforge_library.py tests/test_novaforge_library_ui.py -q
  105 passed, 22 subtests passed

PYTHONPATH=src python -m pytest tests/test_application.py tests/test_desktop.py \
    tests/test_jarvis_desktop_vision_fixes.py -q
  125 passed, 49 subtests passed  (2:11)

PYTHONPATH=src python -m pytest tests/test_web_api.py tests/test_web_platform.py \
    tests/test_route_shape_contract.py tests/test_novaforge_api.py \
    tests/test_novaforge_ui.py tests/test_novaforge_library.py \
    tests/test_novaforge_library_ui.py -q
  347 passed, 627 subtests passed  (2:18)

PYTHONPATH=src python -m mypy src
  Success: no issues found in 436 source files

PYTHONPATH=src python -m mypy src --platform win32
  Success: no issues found in 436 source files

python scripts/generate_api_reference.py --check
  docs/API.md: in sync   (328 routes)

node --check src/novacontrol/web/static/js/novaforge.js
  clean
```

The first combined run (before the new UI file existed) produced one failure: the
panel test that pinned the tab count at a literal `9`, while NF-12 legitimately
adds a tenth tab. The assertion now derives the expected count from the number
of `.forge-view` elements, so a view without a tab (or a tab without a view)
still fails and the next phase does not have to edit a magic number.
`test_novaforge_ui.py` then ran **125 passed / 75 subtests**, and the final
combined group above is the clean run.

Coverage added:

- request shapes: launch/status/attach phrasing, polite suffixes, the
  `the game` prefix, chains refused, ordinary app speech having no catalog title;
- listing/search, missing service stated honestly, resolve without launching;
- preview mints a token and launches nothing; the token executes exactly once;
  replay and missing token are 403; ambiguous/runtime/missing/low-memory plans
  carry no token and start nothing;
- process-without-window is not verified but stays owned; no-process failure;
  a refused launch command keeps its cause;
- stages and observation separation; observation availability equals the
  manager's capture capability;
- close confirms exit and releases the claim; timeout keeps the claim and never
  kills; attach never closes; a foreign `launch_id` is refused;
- a tracked instance blocks the next preview with no token, a claim whose
  process exited does not, and capture-unavailable is reported as unavailable
  on both the plan and the verified result;
- `/command/plan` + `/command/execute` share the token; ordinary applications
  still plan as desktop commands;
- chat launch returns a plan, chat status answers from the row, chat "open
  calculator" stays on the desktop route;
- UI: the four stage sentences, status labels/tones for unknown states, the
  memory-gate wording, the tab/ids, the library routes, approval sending the
  server token, and observation rendered separately.

Every automated test in this phase is **mocked**: Steam manifests, shortcuts,
processes, memory, the launch command and the window close are fixtures
(`FakeMachine` from the NF-11 suite), so no test launches a game, reads the
registry or needs a display. The live evidence in §4 is separate and was run by
hand against a real server.

Not run: the full repository suite (≈2,250 tests) in one pass. The groups above
are the ones this change touches.

---

## 6. Constraints and confirmations

- **Memory:** every preview and launch measured it; the gate is the existing
  `NovaForge`/`GameLaunchService` threshold (2.0 GB free, 92 % used), fully
  configurable with `NOVACONTROL_GAME_MIN_FREE_GB` /
  `NOVACONTROL_GAME_MAX_MEM_PERCENT`. GTA V was **not** launched.
- **External drive:** the disconnected WD My Passport was not accessed, mounted
  or scanned. Discovery continues to refuse non-`DRIVE_FIXED` paths and UNC
  paths; the libraries read were `E:\STEAM` and `D:\SteamLibrary`.
- **No gameplay input:** no key, click or controller event was sent in this
  phase. The browser test clicked only the NovaControl UI.
- **No installation, update, move or download** was performed.
- **No commit or push** was made. Pre-existing working-tree changes were left
  intact; the only edits are the files listed in §1 and this report.
- **One game at a time:** each of the four test launches was closed and its exit
  confirmed before the next request was made, and the preview now refuses a
  second game while a claim is live (verified against GTA V). `Get-Process SFA`
  returned nothing afterwards, and `owned` was `null`.
- **Capture is not claimed.** The panel reports capture availability and says a
  verified launch is not a verified frame. No capture test was run here.

---

## 7. What is deliberately not integrated

- **Non-inventory titles in chat.** Chat claims a game request only when the
  name is one of the candidate inventory's titles or aliases. `launch
  Supermarket Together` still plans a desktop command; it is fully available in
  the Game Library tab and through `/novaforge/library/*`. Extending the chat
  vocabulary means widening that gate and adding tests, not a new resolver.
- **Epic/EA/other launchers.** Still only Steam manifests are parsed; those
  titles are reported as needing a launcher or runtime this build does not
  integrate.
- **Android titles.** No emulator integration; the row says so.
- **GTA V launch.** Installed and launchable, not tested (resource headroom).
- **Game Adaptation Engine.** Not started. This phase ends at "the game is
  running, and here is the process and window identity".

---

## 8. Next phase input

The next phase can now assume a **verified running instance** with a stable
`(pid, start_time, window)` identity, an ownership claim that survives until a
confirmed exit, and a surface that refuses to imply capture. The honest next
step is capture compatibility per launched title: can a validated frame be
produced from *that* window by any declared backend, with the observed limits
(focused/unfocused/minimized) recorded per game and reported separately from
launch success. The Game Adaptation Engine should begin only after that
compatibility table exists, and it should consume this service rather than
launch anything itself.
