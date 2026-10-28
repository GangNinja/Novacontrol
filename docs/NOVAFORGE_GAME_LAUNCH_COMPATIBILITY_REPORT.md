# NovaForge NF-11 — Game Discovery, Launch Verification and Cleanup

Date: 2026-10-23
Host: this laptop (Windows, 15.37 GB visible physical memory, Steam on internal
fixed drives `E:\STEAM` and `D:\SteamLibrary`, Google Play Games installed)

This phase answers one question end to end: *can NovaControl be told a game's name
and actually start it, prove it started, and put it back?*

Scope boundaries held for the whole run: no universal-capture change, no
installation, no update, no DRM/anti-cheat/account interaction, no gameplay
input, no concurrent launches, no access to the disconnected WD My Passport, and
no commit or push.

---

## 1. What was built

`src/novacontrol/novaforge/launcher.py` (new) contains two objects and nothing
else:

| Object | Answers | Evidence it uses |
| --- | --- | --- |
| `GameCatalog` | "Is it here, and is it running?" | Steam `appmanifest_*.acf` rows whose install directory and executable exist; Start Menu / App Paths shortcuts as *resolution evidence for a candidate*; operator-configured game roots (one level deep, bounded, internal-fixed-drives only); running processes with pid + start time + window + executable path |
| `GameLaunchService` | "Did **my** launch work, and can I close it?" | the process/window that appeared *after* the launch command, keyed by (pid, start time), closed by window title (WM_CLOSE) and confirmed exited |

The split is deliberate: a catalog row is evidence of an *installation*, a
process record is evidence of a *run*, and only the identity this service started
is evidence of *ownership*.

### J.A.R.V.I.S. capabilities reused (nothing duplicated)

Every platform fact comes from the existing `LocalDesktopRunner`
(`src/novacontrol/desktop/controller.py`):

| Seam | Already existed | Used for |
| --- | --- | --- |
| `installed_steam_games()` | yes | Steam installs (same reader `launch <game>` resolves through) |
| `installed_app_targets()` | yes | the Start Menu / App Paths index `open <app>` already uses |
| `game_processes(names)` | yes | running-instance detection and launch verification |
| `memory_snapshot()` | yes | the resource gate before every launch |
| `launch_game_target(kind, target)` | yes | Steam deep link (`steam://rungameid/<appid>`) or a verified `.exe`/`.lnk` |
| `close_game_window(title)` | yes | graceful WM_CLOSE (the STOP_APP path) |
| `game_processes_under(directories)` | **added** | processes running from inside a verified install directory |

`game_processes_under` is the one extension, and the live run is why it exists
(§4). It is the same `Get-Process` probe with the executable path as the filter.
No second automation stack, no new launcher, no bypass of the permission layer:
`NovaForgeManager` receives the service as its `game_launcher` seam and reports
`game_launch` availability from it.

---

## 2. Discovered inventory on internal storage

Discovery counts at the end of the run (`GameCatalog.describe()`):

```
INSTALLED_AND_LAUNCHABLE 3   NOT_FOUND_ON_INTERNAL_STORAGE 5
REQUIRES_LAUNCHER_OR_RUNTIME 3   ALREADY_RUNNING 1
```

| # | Candidate title | Status | Evidence |
| --- | --- | --- | --- |
| 1 | GTA V | `INSTALLED_AND_LAUNCHABLE` | Steam app 3240220 "Grand Theft Auto V Enhanced"; install `e:\steam\steamapps\common\Grand Theft Auto V Enhanced`; `GTA5_Enhanced.exe` present |
| 2 | Call of Duty | `NOT_FOUND_ON_INTERNAL_STORAGE` | no Steam manifest, shortcut, or configured root matches any alias |
| 3 | Hill Climb Racing | `REQUIRES_LAUNCHER_OR_RUNTIME` | Android title; this build has no Android runtime integration |
| 4 | Clash of Clans | `REQUIRES_LAUNCHER_OR_RUNTIME` | a Start Menu `.lnk` exists under `…\Programs\Google Play Games\` — that starts the Android runtime and its account flow, not the game (see §5) |
| 5 | Cosmic Coliseum | `NOT_FOUND_ON_INTERNAL_STORAGE` | no matching artifact |
| 6 | Tekken 8 | `NOT_FOUND_ON_INTERNAL_STORAGE` | no matching artifact |
| 7 | Free Fire | `REQUIRES_LAUNCHER_OR_RUNTIME` | `…\Programs\Google Play Games\Free Fire MAX x NARUTO.lnk` — same runtime rule |
| 8 | Shadow Fight Arena | `INSTALLED_AND_LAUNCHABLE` — **launch-tested** | Steam app 1446890; install `e:\steam\steamapps\common\Shadow Fight Arena`; executable `SFA.exe` (no title fragment matches it) |
| 9 | Freedom Fighters | `ALREADY_RUNNING` (not tested, not closed) | Steam app 1347780; running as `Freedom`, pid 28600, start 2026-10-22T18:40:51Z, window "Freedom Fighters", `E:\STEAM\steamapps\common\Freedom_Fighters\Freedom.exe` |
| 10 | Hollow Knight | `NOT_FOUND_ON_INTERNAL_STORAGE` | no matching artifact |
| 11 | Quarantine Zone | `NOT_FOUND_ON_INTERNAL_STORAGE` | no matching artifact |

One further Steam title is discoverable and was not tested because it is outside
the brief's inventory: **Supermarket Together** (Steam app 2709570,
`D:\SteamLibrary\steamapps\common\Supermarket Together`). It is listed so the
catalog is not silently truncated.

Steam's `Steamworks Common Redistributables` (app 228980) is a runtime
prerequisite, not a game, and is excluded from the catalog.

Installation evidence, per the phase's rule, is never a name: it is a manifest
whose install directory **and** executable exist, a shortcut whose target file
exists, or an executable inside a configured root. A row whose install is gone
still reports *why* rather than disappearing.

---

## 3. The launch test that passed

`Shadow Fight Arena` was chosen because it is small enough to be safe on this
machine; it was the only game launched in this phase.

Lifecycle: **discover → launch → verify → record → close → confirm exit**.

```
memory gate     ok (free 3.85 GB, used 75.0 %, total 15.37 GB)
launch          steam://rungameid/1446890 issued; shell pid 27468 (not the game)
verified        process SFA, pid 23564, start 2026-10-22T21:16:58.0193373Z,
                window "Shadow Fight Arena" (verified: true)
close           WM_CLOSE on "Shadow Fight Arena" → CLOSED
exit confirmed  pid 23564 gone; post-exit probe for "SFA" returned []
ownership       released (owned = None) only after the exit was confirmed
history         LAUNCH_VERIFIED, then CLOSED — both carrying the launch record
```

- It was **not** already running before the test (the pre-launch probe was empty).
- Exactly one instance was started.
- No input of any kind was sent; the game was stopped at its initial screen.
- The close was a graceful window request, never a process kill.
- The next launch was gated on that confirmed exit (the service refuses to start
  another game while a record it owns is alive).

Startup verification is process **and** window: a process alone is reported as
`INSTALLED_BUT_LAUNCH_FAILED` with "no window appeared", because the phase treats
a successful command as insufficient evidence.

---

## 4. Failures found by the live run, and their fixes

The first attempt at the same game reported `INSTALLED_BUT_LAUNCH_FAILED`
("no new matching process appeared within 90s"). The game **had** started — `SFA`,
pid 11752, window "Shadow Fight Arena" — and stayed running until it was found by
a later catalog read and closed (WM_CLOSE; exit confirmed within 1 s). Three real
defects were behind it:

1. **Process identities were guessed from the title.** `SFA.exe` shares no word
   with "Shadow Fight Arena", so the name probe could never see it. Fixed with
   `game_processes_under`: a process running from inside the verified install
   directory is evidence, and it is how the launch is verified now. Space-stripped
   patterns (`shadowfightarena`) were added for the same reason.
2. **A crash handler stood in for the game.** The crashed/closed attempt left
   `crashpad_handler.exe` in the install directory; the next catalog read answered
   `ALREADY_RUNNING` from that helper, and a verification poll would have called
   its spawn a successful start. Known install helpers (`crashpad_handler`,
   `UnityCrashHandler*`, `steamwebhelper`, `gameoverlayui64`, `steam`,
   `WerFault`, redistributables) are now excluded from both answers.
3. **The Start Menu index was listed as a games library.** The first discovery
   run reported 142 "installed games", including *Control Panel*, *Run*,
   *Registry Editor*, and "system information" matched against pid 4 (System).
   A shortcut is now resolution evidence for a candidate title; it is never its
   own discovered game.

A fourth, smaller correction: a `.lnk` inside an Android runtime's folder
(Google Play Games, BlueStacks, LDPlayer, GameLoop, MuMu, Nox) is reported as
`REQUIRES_LAUNCHER_OR_RUNTIME` with the runtime named, instead of claiming a
launchable installation it cannot start.

---

## 5. Not tested, and why

| Title | Result | Reason |
| --- | --- | --- |
| GTA V | **NOT_TESTED** (`INSTALLED_AND_LAUNCHABLE`) | free memory was 2.45–3.85 GB during the batch. That passes the configured gate (≥2 GB free, ≤92 % used) but is far below a safe working set for this title; launching it would have been a resource gamble on the machine running the batch, which the phase's safeguards forbid. It remains launchable and is the next candidate when the machine has headroom. |
| Clash of Clans, Free Fire | **NOT_TESTED** (`REQUIRES_LAUNCHER_OR_RUNTIME`) | their only entry points are Google Play Games shortcuts; starting one opens the Android runtime and its account flow — an interaction this task must not drive. |
| Hill Climb Racing | **NOT_TESTED** (`REQUIRES_LAUNCHER_OR_RUNTIME`) | Android title, no runtime integration in this build. |
| Call of Duty, Cosmic Coliseum, Tekken 8, Hollow Knight, Quarantine Zone | **NOT_TESTED** (`NOT_FOUND_ON_INTERNAL_STORAGE`) | no verified installation on internal fixed storage; nothing was downloaded or moved. |
| Freedom Fighters | **NOT_TESTED / attached only** (`ALREADY_RUNNING`) | running before the test began (pid 28600, started 18:40). It is the operator's, so it was attached for status and never launched again or closed. |
| Supermarket Together | **NOT_TESTED** | outside the brief's inventory; listed for completeness only. |

`ALREADY_RUNNING` is never a launch result: a game that was already up is
reported and attached, and a duplicate is refused rather than started.

---

## 6. Resource safeguards exercised

- The gate runs before every launch and reads the same `LocalDesktopRunner`
  memory probe the rest of the application uses: minimum 2.0 GB free and at most
  92 % used, configurable through `NOVACONTROL_GAME_MIN_FREE_GB` /
  `NOVACONTROL_GAME_MAX_MEM_PERCENT`.
- It was live-refused at the start of the session: *"only 1.8 GB free (minimum
  2.0 GB)"*, and no launch command was issued. Testing resumed only after the
  machine recovered to 2.58 GB.
- One game at a time; a second launch is refused while a test-owned process is
  alive; a launch that produces no verified start is not retried.
- A shutdown that does not complete is `SHUTDOWN_TIMEOUT` with "No process was
  killed; stop the batch" — never a forced termination, and no further launches.
- No process was killed in this phase. Every close was WM_CLOSE on a window.

---

## 7. Automated tests

```
PYTHONPATH=src python -m pytest tests/test_novaforge_launcher.py -q
  52 passed, 3 subtests passed

PYTHONPATH=src python -m pytest tests/test_application.py tests/test_desktop.py \
    tests/test_jarvis_desktop_vision_fixes.py tests/test_novaforge.py \
    tests/test_novaforge_api.py -q
  233 passed, 49 subtests passed

PYTHONPATH=src python -m mypy src
  Success: no issues found in 436 source files
```

`tests/test_novaforge_launcher.py` is fully deterministic — every platform fact is
injected, so no test launches a game, reads the registry, or needs a display. It
covers:

- exact, alias and partial-name resolution; ambiguous matches refused, never guessed;
  the recorded `ark` / `supermarket together` substring trap;
- missing or inaccessible executables; a shortcut whose target is gone;
  a runtime-store shortcut reported as needing a runtime;
- the disconnected-drive rule: removable, network, UNC, relative and empty paths
  are all refused, and a configured root on a refused drive is not scanned;
- an already-running game detected, a duplicate launch prevented, and attachment
  that takes no ownership;
- an abbreviated executable (`SFA.exe`) verified from the install directory, and a
  crash handler explicitly *not* counted as the game starting;
- a process with no window is not a verified launch;
- launch failure handling (no process, refused command, low/high/unreadable memory);
- process identity by (pid, start time), pid reuse, and ownership;
- graceful close with confirmed exit, `SHUTDOWN_TIMEOUT` blocking the batch,
  `NO_WINDOW`, `NOT_OWNED`, and a user-owned game that is never closed;
- the catalog row marked `tested` only by a real launch record.

Not run in this phase: the full repository suite (≈2,250 tests). The groups above
are the ones this change touches.

---

## 8. Confirmations

- **External drive:** the disconnected WD My Passport was not accessed, mounted,
  scanned or launched from. Discovery refuses every drive that is not
  `DRIVE_FIXED`, and refuses UNC paths; the two Steam libraries it did read
  (`E:\STEAM`, `D:\SteamLibrary`) are internal fixed disks.
- **No gameplay input** was sent — no keystrokes, clicks, or controller events.
- **No installation, update, download, move or uninstall** was performed.
- **No commit and no push** were made; pre-existing working-tree changes were left
  untouched.
- **No game was left running by this phase.** The one leftover instance from the
  first failed attempt (`SFA`, pid 11752) was closed with WM_CLOSE and its exit
  confirmed; the verified launch (pid 23564) exited after its own close.

---

## 9. Remaining issues and next actions

1. **Memory headroom is the binding constraint on this laptop.** GTA V is
   installed and launchable but was not tested because free memory stayed between
   1.8 and 3.9 GB. Closing applications and re-running the same launch flow is
   all that is needed.
2. **A `.lnk` is launched, not resolved.** The Start Menu shortcut path verifies
   that the link exists, not what it points at. Resolving the link target (and
   rejecting one that does not exist) would tighten that source further.
3. **The web/chat surface is unchanged.** This phase adds the discovery and
   lifecycle service behind `NovaForgeManager`'s `game_launch` seam. The existing
   JARVIS `launch <game>` path still performs launches; the catalog is not yet
   exposed as its own HTTP route or chat intent.
4. **Other launchers are not integrated.** Only Steam metadata is read; Epic and
   EA tiles are visible through their shortcuts but are not parsed as installs.

Nothing in this phase touches universal game capture, and no capture claim is
made here: a verified game window is not a verified live capture.
