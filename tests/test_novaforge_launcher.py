"""Game discovery, launch verification and cleanup — deterministic tests.

No real game is launched and no display is touched: every platform fact (Steam
manifests, shortcuts, processes, memory, launch, close) is a fake, so these tests
run the same on any machine and in CI.

The contract under test is the phase's honesty rule: a catalog row is evidence
of an INSTALLATION, a process record is evidence of a RUN, and only the process
identity this service started is evidence of OWNERSHIP.
"""

from __future__ import annotations

import unittest

from novacontrol.novaforge.launcher import (
    ALREADY_RUNNING,
    AMBIGUOUS_MATCH,
    CANDIDATE_INVENTORY,
    INSTALLED_AND_LAUNCHABLE,
    INSTALLED_BUT_LAUNCH_FAILED,
    NOT_FOUND_ON_INTERNAL_STORAGE,
    REQUIRES_LAUNCHER_OR_RUNTIME,
    GameCatalog,
    GameLaunchService,
    _blank_artifact,
    _is_internal_fixed_drive,
    _process_patterns,
    candidate_title,
    normalise,
)

FIXED = "C:\\Games"


def fixed_path(_path) -> bool:
    """Every test path is internal; the drive rule has its own test."""
    return True


class FakeMachine:
    """A fake Windows runner: installs, processes, memory, launches, closes."""

    def __init__(
        self,
        *,
        steam: tuple[dict, ...] = (),
        shortcuts: dict[str, str] | None = None,
        processes: tuple[dict, ...] = (),
        free_gb: float | None = 8.0,
        used_percent: float | None = 40.0,
        spawn: bool = True,
        spawn_start_time: str = "2026-10-23T10:00:00Z",
        spawn_pid: int = 5001,
        window_after: str = "Freedom Fighters",
        closes: bool = True,
        spawn_name: str | None = None,
        spawn_path: str = "",
    ) -> None:
        self.steam_rows = steam
        self.shortcut_rows = shortcuts or {}
        self.process_rows = list(processes)
        self.free_gb = free_gb
        self.used_percent = used_percent
        self.spawn = spawn
        self.spawn_start_time = spawn_start_time
        self.spawn_pid = spawn_pid
        self.window_after = window_after
        self.closes = closes
        self.spawn_name = spawn_name
        self.spawn_path = spawn_path
        self.launch_calls: list[dict] = []
        self.close_calls: list[str] = []
        self.probe_calls: int = 0

    # -- seams -------------------------------------------------------------

    def steam_games(self):
        return list(self.steam_rows)

    def shortcuts(self):
        return dict(self.shortcut_rows)

    async def processes(self, names):
        self.probe_calls += 1
        return [dict(row) for row in self.process_rows]

    async def processes_under(self, directories):
        roots = [str(item).lower().rstrip("\\") for item in directories]
        return [
            dict(row)
            for row in self.process_rows
            if any(
                str(row.get("path") or "").lower().startswith(root + "\\")
                for root in roots
            )
        ]

    async def memory(self):
        snapshot: dict = {"available": True, "source": "fake"}
        if self.free_gb is not None:
            snapshot["free_gb"] = self.free_gb
        if self.used_percent is not None:
            snapshot["used_percent"] = self.used_percent
        return snapshot

    async def launch(self, *, kind, target, name=""):
        self.launch_calls.append({"kind": kind, "target": target, "name": name})
        if self.spawn:
            self.process_rows.append(
                {
                    "pid": self.spawn_pid,
                    "name": self.spawn_name
                    or (
                        target.split("\\")[-1].removesuffix(".exe")
                        if kind == "path"
                        else name
                    ),
                    "start_time": self.spawn_start_time,
                    "window": self.window_after,
                    "path": self.spawn_path or (target if kind == "path" else ""),
                }
            )
        return {"adapter": "local-desktop", "action": "game_launch", "shell_pid": 999}

    async def close_window(self, title):
        self.close_calls.append(title)
        if self.closes:
            self.process_rows = [row for row in self.process_rows if row.get("window") != title]
        return {"action": "stop_app", "stopped": True}

    # -- assembly ----------------------------------------------------------

    def catalog(self, **kwargs) -> GameCatalog:
        return GameCatalog(
            steam_games=self.steam_games,
            shortcuts=self.shortcuts,
            processes=self.processes,
            internal_path=fixed_path,
            processes_under=self.processes_under,
            **kwargs,
        )

    def service(self, **kwargs) -> GameLaunchService:
        options = {
            "poll_interval": 0.0,
            "startup_timeout": 1.0,
            "shutdown_timeout": 1.0,
            "window_timeout": 0.0,
        }
        options.update(kwargs)
        return GameLaunchService(
            self.catalog(),
            launch=self.launch,
            processes=self.processes,
            processes_under=self.processes_under,
            close_window=self.close_window,
            memory=self.memory,
            **options,
        )


def steam_row(
    name: str = "Freedom Fighters",
    app_id: str = "1234",
    install: str = "C:\\Games\\Freedom Fighters",
    exe: str = "C:\\Games\\Freedom Fighters\\Freedom.exe",
) -> dict:
    return {
        "app_id": app_id,
        "name": name,
        "install_dir": install,
        "install_exists": "true",
        "executable": exe,
        "library": "C:\\Program Files (x86)\\Steam\\steamapps",
    }


def running(name: str = "Freedom", pid: int = 28600, start: str = "2026-10-22T20:00:00Z") -> dict:
    return {
        "pid": pid,
        "name": name,
        "start_time": start,
        "window": "Freedom Fighters",
        "path": "C:\\Games\\Freedom Fighters\\Freedom.exe",
    }


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    """A request resolves to one title, an ambiguity, or nothing."""

    async def test_exact_and_alias_requests_resolve(self) -> None:
        catalog = FakeMachine(steam=[steam_row()]).catalog()
        for query in ("Freedom Fighters", "freedom fighters", "  FREEDOM FIGHTERS  "):
            with self.subTest(query=query):
                self.assertEqual((await catalog.resolve(query))["status"], INSTALLED_AND_LAUNCHABLE)

    async def test_an_alias_resolves_to_the_canonical_title(self) -> None:
        catalog = FakeMachine(
            steam=[steam_row("Grand Theft Auto V", "3240220", "C:\\Games\\GTAV", "C:\\Games\\GTAV\\GTA5.exe")]
        ).catalog()
        resolved = await catalog.resolve("gta 5")
        self.assertEqual(resolved["entry"]["title"], "GTA V")

    async def test_an_ambiguous_title_is_refused_not_guessed(self) -> None:
        catalog = FakeMachine(
            steam=[
                steam_row("Freedom Fighters", "111", "C:\\Games\\FF1", "C:\\Games\\FF1\\ff.exe"),
                steam_row("Freedom Fighters Remastered", "222", "C:\\Games\\FF2", "C:\\Games\\FF2\\ff.exe"),
            ]
        ).catalog()
        resolved = await catalog.resolve("freedom fighters")
        self.assertEqual(resolved["status"], AMBIGUOUS_MATCH)
        self.assertIsNone(resolved["entry"])
        self.assertEqual(len(resolved["candidates"]), 2)

    async def test_a_short_key_never_matches_a_longer_word(self) -> None:
        """'supermarket together' must not match 'ark' (the recorded bug)."""
        catalog = FakeMachine(steam=[steam_row("ARK", "346110", "C:\\Games\\ARK", "C:\\Games\\ARK\\ark.exe")]).catalog()
        self.assertEqual((await catalog.resolve("supermarket together"))["status"],
                         NOT_FOUND_ON_INTERNAL_STORAGE)

    async def test_an_unknown_title_is_not_found_with_a_reason(self) -> None:
        resolved = await FakeMachine().catalog().resolve("hogwarts legacy")
        self.assertEqual(resolved["status"], NOT_FOUND_ON_INTERNAL_STORAGE)
        self.assertIn("No installed title matches", resolved["entry"]["reason"])

    async def test_a_blank_request_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            await FakeMachine().catalog().resolve("   ")

    async def test_a_mobile_candidate_needs_a_runtime_this_build_lacks(self) -> None:
        resolved = await FakeMachine().catalog().resolve("Clash of Clans")
        self.assertEqual(resolved["status"], REQUIRES_LAUNCHER_OR_RUNTIME)
        self.assertIn("Android", resolved["entry"]["reason"])


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Discovery reports what the disk and the process table actually say."""

    async def test_a_steam_install_is_launchable_by_app_id(self) -> None:
        row = row_for(await FakeMachine(steam=[steam_row()]).catalog().entries(), "Freedom Fighters")
        self.assertEqual(row["status"], INSTALLED_AND_LAUNCHABLE)
        self.assertEqual(row["artifact"]["launch_kind"], "steam")
        self.assertEqual(row["artifact"]["launch_target"], "1234")

    async def test_a_steam_row_whose_directory_is_missing_is_not_called_verified(self) -> None:
        broken = steam_row(install="E:\\SteamLibrary\\missing", exe="")
        broken["executable"] = ""
        row = row_for(await FakeMachine(steam=[broken]).catalog().entries(), "Freedom Fighters")
        # The deep link still exists, so this is launchable — but the row says
        # what is wrong instead of implying the files are there.
        self.assertFalse(row["artifact"]["executable_exists"])
        self.assertEqual(row["artifact"]["executable"], "")
        self.assertEqual(row["status"], INSTALLED_AND_LAUNCHABLE)

    async def test_a_runtime_store_shortcut_needs_a_runtime_not_a_launch(self) -> None:
        """A .lnk inside Google Play Games starts the runtime, not the game."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            folder = Path(temp) / "Start Menu" / "Google Play Games"
            folder.mkdir(parents=True)
            link = folder / "Clash of Clans.lnk"
            link.write_bytes(b"")
            catalog = GameCatalog(
                steam_games=lambda: (),
                shortcuts=lambda: {"Clash of Clans": str(link)},
                processes=lambda names: _async(()),
                internal_path=fixed_path,
            )
            row = row_for(await catalog.entries(), "Clash of Clans")
        self.assertEqual(row["status"], REQUIRES_LAUNCHER_OR_RUNTIME)
        self.assertFalse(row["artifact"]["launchable"])
        self.assertIn("Android runtime", row["reason"])

    async def test_a_shortcut_whose_target_is_gone_is_not_an_installation(self) -> None:
        catalog = GameCatalog(
            steam_games=lambda: (),
            shortcuts=lambda: {"Hollow Knight": "C:\\Games\\Hollow\\hollow.exe"},
            processes=lambda names: _async(()),
            internal_path=fixed_path,
        )
        # The path does not exist on this test host, so nothing is discovered.
        entries = await catalog.entries()
        hollow = row_for(entries, "Hollow Knight")
        self.assertEqual(hollow["status"], NOT_FOUND_ON_INTERNAL_STORAGE)

    async def test_the_start_menu_index_is_not_listed_as_a_games_library(self) -> None:
        """A shortcut is resolution evidence for a candidate, never its own row.

        Live run on the laptop: listing the 155 Start Menu shortcuts produced
        "Control Panel", "Run" and "Registry Editor" as discovered GAMES, and
        "system information" matched pid 4 (System) as an already-running title.
        """
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            link = Path(temp) / "Control Panel.lnk"
            link.write_bytes(b"")
            catalog = GameCatalog(
                steam_games=lambda: (),
                shortcuts=lambda: {"Control Panel": str(link)},
                processes=lambda names: _async(()),
                internal_path=fixed_path,
            )
            titles = {row["title"] for row in await catalog.entries()}
        self.assertNotIn("Control Panel", titles)
        self.assertEqual(titles, {row.title for row in CANDIDATE_INVENTORY})

    async def test_a_steam_redistributable_is_not_a_discovered_game(self) -> None:
        machine = FakeMachine(
            steam=[steam_row("Steamworks Common Redistributables", "228980", "C:\\Games\\redist", "")]
        )
        self.assertNotIn(
            "Steamworks Common Redistributables",
            {row["title"] for row in await machine.catalog().entries()},
        )
        resolved = await machine.catalog().resolve("steamworks common redistributables")
        self.assertEqual(resolved["status"], NOT_FOUND_ON_INTERNAL_STORAGE)

    async def test_an_already_running_game_is_reported_as_running(self) -> None:
        machine = FakeMachine(steam=[steam_row()], processes=(running(),))
        row = row_for(await machine.catalog().entries(), "Freedom Fighters")
        self.assertEqual(row["status"], ALREADY_RUNNING)
        self.assertEqual(row["running"]["pid"], 28600)
        self.assertIn("Already running", row["reason"])

    async def test_a_configured_root_on_a_refused_drive_is_not_scanned(self) -> None:
        def refuse(_path) -> bool:
            return False

        catalog = GameCatalog(
            steam_games=lambda: (),
            shortcuts=lambda: {},
            processes=lambda names: _async(()),
            configured_dirs=("E:\\External\\Games",),
            internal_path=refuse,
        )
        self.assertEqual(catalog.configured_artifacts(), [])

    async def test_a_configured_root_discovered_executable_is_launchable_by_path(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            root = Path(temp)
            game = root / "Quarantine Zone"
            game.mkdir()
            (game / "QuarantineZone.exe").write_bytes(b"MZ")
            catalog = GameCatalog(
                steam_games=lambda: (),
                shortcuts=lambda: {},
                processes=lambda names: _async(()),
                configured_dirs=(root,),
                internal_path=fixed_path,
            )
            row = row_for(await catalog.entries(), "Quarantine Zone")
            self.assertEqual(row["title"], "Quarantine Zone")
            self.assertEqual(row["artifact"]["launch_kind"], "path")
            self.assertTrue(row["artifact"]["executable_exists"])

    async def test_the_process_probe_is_called_once_for_the_whole_catalog(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        await machine.catalog().entries()
        self.assertEqual(machine.probe_calls, 1, "one probe, not one per title")

    async def test_the_catalog_says_a_row_is_not_a_launch_test(self) -> None:
        payload = await FakeMachine(steam=[steam_row()]).catalog().describe()
        self.assertTrue(all(row["tested"] is False for row in payload["entries"]))
        self.assertTrue(any("not a launch test" in item for item in payload["limitations"]))
        self.assertEqual(payload["candidate_inventory"][0], "GTA V")

    async def test_the_inventory_covers_the_brief(self) -> None:
        titles = {row.title for row in CANDIDATE_INVENTORY}
        for expected in (
            "GTA V", "Call of Duty", "Hill Climb Racing", "Clash of Clans",
            "Cosmic Coliseum", "Tekken 8", "Free Fire", "Shadow Fight Arena",
            "Freedom Fighters", "Hollow Knight", "Quarantine Zone",
        ):
            self.assertIn(expected, titles)
        self.assertIsNotNone(candidate_title("cod"))
        self.assertIsNone(candidate_title("a game nobody named"))


class LaunchTests(unittest.IsolatedAsyncioTestCase):
    """Launch is gated, owned, verified — and never a duplicate."""

    async def test_a_verified_launch_records_the_exact_process(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        result = await service.launch("Freedom Fighters")
        self.assertTrue(result["launched"], result.get("reason"))
        self.assertEqual(result["launch"]["pid"], 5001)
        self.assertEqual(result["launch"]["start_time"], "2026-10-23T10:00:00Z")
        self.assertTrue(result["launch"]["owned"])
        self.assertEqual(result["launch"]["window"], "Freedom Fighters")
        self.assertEqual(machine.launch_calls[0]["kind"], "steam")
        self.assertEqual(machine.launch_calls[0]["target"], "1234")

    async def test_a_missing_process_is_a_failed_launch_not_a_success(self) -> None:
        machine = FakeMachine(steam=[steam_row()], spawn=False)
        result = await machine.service().launch("Freedom Fighters")
        self.assertFalse(result["launched"])
        self.assertEqual(result["status"], INSTALLED_BUT_LAUNCH_FAILED)
        self.assertIn("no new matching process", result["reason"])
        self.assertIsNone(machine.service().owned)

    async def test_a_refused_launch_command_is_reported_with_its_cause(self) -> None:
        class Broken(FakeMachine):
            async def launch(self, *, kind, target, name=""):
                raise ValueError("Steam deep-link launch requires Windows.")

        result = await Broken(steam=[steam_row()]).service().launch("Freedom Fighters")
        self.assertFalse(result["launched"])
        self.assertIn("requires Windows", result["reason"])

    async def test_an_already_running_game_is_not_launched_again(self) -> None:
        machine = FakeMachine(steam=[steam_row()], processes=(running(),))
        service = machine.service()
        result = await service.launch("Freedom Fighters")
        self.assertFalse(result["launched"])
        self.assertEqual(result["status"], ALREADY_RUNNING)
        self.assertEqual(machine.launch_calls, [], "a running game must not spawn a duplicate")
        attached = await service.adopt("Freedom Fighters")
        self.assertTrue(attached["attached"])
        self.assertFalse(attached["owned"])

    async def test_a_second_launch_is_refused_while_the_first_is_running(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        with self.assertRaises(RuntimeError) as caught:
            await service.launch("Freedom Fighters")
        self.assertIn("still running", str(caught.exception))
        self.assertEqual(len(machine.launch_calls), 1)

    async def test_a_stale_ownership_record_does_not_block_the_next_launch(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        machine.process_rows.clear()  # the game exited on its own
        again = await service.launch("Freedom Fighters")
        self.assertTrue(again["launched"], again.get("reason"))

    async def test_low_memory_refuses_the_launch_with_the_measurement(self) -> None:
        machine = FakeMachine(steam=[steam_row()], free_gb=0.6)
        result = await machine.service().launch("Freedom Fighters")
        self.assertFalse(result["launched"])
        self.assertIn("0.6 GB free", result["reason"])
        self.assertEqual(machine.launch_calls, [])

    async def test_high_memory_pressure_refuses_the_launch(self) -> None:
        machine = FakeMachine(steam=[steam_row()], free_gb=6.0, used_percent=96.0)
        result = await machine.service().launch("Freedom Fighters")
        self.assertFalse(result["launched"])
        self.assertIn("96%", result["reason"])
        self.assertEqual(machine.launch_calls, [])

    async def test_an_unreadable_memory_probe_does_not_block_a_launch(self) -> None:
        machine = FakeMachine(steam=[steam_row()], free_gb=None, used_percent=None)
        result = await machine.service().launch("Freedom Fighters")
        self.assertTrue(result["launched"], result.get("reason"))
        self.assertFalse(result["memory"]["available"])
        self.assertTrue(result["memory"]["ok"])

    async def test_an_ambiguous_request_launches_nothing(self) -> None:
        machine = FakeMachine(
            steam=[
                steam_row("Freedom Fighters", "111", "C:\\Games\\FF1", "C:\\Games\\FF1\\ff.exe"),
                steam_row("Freedom Fighters Remastered", "222", "C:\\Games\\FF2", "C:\\Games\\FF2\\ff.exe"),
            ]
        )
        result = await machine.service().launch("freedom fighters")
        self.assertFalse(result["launched"])
        self.assertEqual(result["status"], AMBIGUOUS_MATCH)
        self.assertEqual(machine.launch_calls, [])

    async def test_a_not_installed_title_launches_nothing(self) -> None:
        machine = FakeMachine()
        result = await machine.service().launch("Hollow Knight")
        self.assertFalse(result["launched"])
        self.assertEqual(result["status"], NOT_FOUND_ON_INTERNAL_STORAGE)
        self.assertEqual(machine.launch_calls, [])

    async def test_an_abbreviated_executable_is_verified_from_the_install_dir(self) -> None:
        """The live failure this pins: Shadow Fight Arena installs SFA.exe.

        No fragment of "Shadow Fight Arena" matches ``SFA``, so the name probe
        found nothing and a real launch was reported as failed. The install
        directory is the identity that survives an abbreviation.
        """
        machine = FakeMachine(
            steam=[steam_row("Shadow Fight Arena", "1446890", "C:\\Games\\SFA", "")],
            spawn_name="SFA",
            spawn_path="C:\\Games\\SFA\\SFA.exe",
            window_after="Shadow Fight Arena",
        )
        result = await machine.service().launch("Shadow Fight Arena")
        self.assertTrue(result["launched"], result.get("reason"))
        self.assertTrue(result["launch"]["verified"])
        self.assertEqual(result["launch"]["process_name"], "SFA")
        self.assertEqual(result["launch"]["window"], "Shadow Fight Arena")
        self.assertEqual(machine.launch_calls[0]["target"], "1446890")

    async def test_a_launch_test_marks_the_catalog_row(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        before = await service.catalog_payload()
        self.assertFalse(row_for(before["entries"], "Freedom Fighters")["tested"])
        await service.launch("Freedom Fighters")
        after = await service.catalog_payload()
        row = row_for(after["entries"], "Freedom Fighters")
        self.assertTrue(row["tested"])
        self.assertEqual(row["launch_status"], "LAUNCH_VERIFIED")

    async def test_a_crash_handler_is_not_the_game_starting(self) -> None:
        """A crashed launch leaves crashpad_handler.exe behind, not a game.

        Live run: after a failed attempt the leftover handler made the next
        catalog read answer ALREADY_RUNNING, and a fresh attempt would have
        called the handler's spawn a verified start.
        """
        machine = FakeMachine(
            steam=[steam_row("Shadow Fight Arena", "1446890", "C:\\Games\\SFA", "")],
            spawn_name="crashpad_handler",
            spawn_path="C:\\Games\\SFA\\crashpad_handler.exe",
            window_after="",
        )
        self.assertEqual(
            (await machine.catalog().resolve("Shadow Fight Arena"))["status"],
            INSTALLED_AND_LAUNCHABLE,
            "a helper must not make a game look like it is running",
        )
        result = await machine.service().launch("Shadow Fight Arena")
        self.assertFalse(result["launched"], "a helper is not a verified start")
        self.assertEqual(result["status"], INSTALLED_BUT_LAUNCH_FAILED)

    async def test_a_process_with_no_window_is_not_a_verified_launch(self) -> None:
        machine = FakeMachine(steam=[steam_row()], window_after="")
        service = machine.service()
        result = await service.launch("Freedom Fighters")
        self.assertTrue(result["launched"])
        self.assertFalse(result["launch"]["verified"])
        self.assertEqual(result["status"], INSTALLED_BUT_LAUNCH_FAILED)
        self.assertIn("no window", result["reason"])
        # It stays tracked as this service's own, so the batch cannot launch over it.
        self.assertIsNotNone(service.owned)
        with self.assertRaises(RuntimeError):
            await service.launch("Freedom Fighters")

    async def test_a_path_launch_passes_the_verified_executable(self) -> None:
        machine = FakeMachine(
            shortcuts={"Freedom Fighters": "C:\\Games\\Freedom Fighters\\Freedom.exe"}
        )
        # The shortcut target must exist for discovery; create it for the test.
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            exe = Path(temp) / "Freedom.exe"
            exe.write_bytes(b"MZ")
            machine.shortcut_rows = {"Freedom Fighters": str(exe)}
            result = await machine.service().launch("Freedom Fighters")
            self.assertTrue(result["launched"], result.get("reason"))
            self.assertEqual(machine.launch_calls[0]["kind"], "path")
            self.assertEqual(machine.launch_calls[0]["target"], str(exe))


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """Close only what this service started, and confirm the identity exited."""

    async def test_a_graceful_close_confirms_the_process_exited(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        outcome = await service.close()
        self.assertTrue(outcome["closed"])
        self.assertEqual(outcome["status"], "CLOSED")
        self.assertEqual(machine.close_calls, ["Freedom Fighters"])
        self.assertIsNone(service.owned, "the ownership claim is released only on confirmed exit")

    async def test_a_close_that_times_out_is_reported_and_claims_nothing(self) -> None:
        machine = FakeMachine(steam=[steam_row()], closes=False)
        service = machine.service()
        await service.launch("Freedom Fighters")
        outcome = await service.close()
        self.assertFalse(outcome["closed"])
        self.assertEqual(outcome["status"], "SHUTDOWN_TIMEOUT")
        self.assertIn("still running", outcome["reason"])
        self.assertIn("No process was killed", outcome["reason"])
        self.assertIsNotNone(service.owned, "an unconfirmed exit keeps the claim")
        # And the batch must stop: another launch is refused for the same reason.
        with self.assertRaises(RuntimeError):
            await service.launch("Freedom Fighters")

    async def test_closing_with_nothing_owned_says_so(self) -> None:
        service = FakeMachine().service()
        outcome = await service.close()
        self.assertFalse(outcome["closed"])
        self.assertEqual(outcome["status"], "NO_OWNED_GAME")

    async def test_a_windowless_game_cannot_be_closed_gracefully(self) -> None:
        machine = FakeMachine(steam=[steam_row()], window_after="")
        service = machine.service()
        await service.launch("Freedom Fighters")
        outcome = await service.close()
        self.assertFalse(outcome["closed"])
        self.assertEqual(outcome["status"], "NO_WINDOW")
        self.assertEqual(machine.close_calls, [], "nothing was asked to close without a window")

    async def test_a_mismatched_launch_id_closes_nothing(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        outcome = await service.close(launch_id="launch-999")
        self.assertEqual(outcome["status"], "NOT_OWNED")
        self.assertEqual(machine.close_calls, [])

    async def test_a_pid_reuse_is_not_the_same_process(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        # Same pid, different start time: a different process that reused it.
        machine.process_rows = [
            {"pid": 5001, "name": "Freedom", "start_time": "2026-10-23T11:00:00Z",
             "window": "Something Else", "path": ""}
        ]
        self.assertFalse(await service._process_alive(service.owned))
        # So the launch gate no longer treats the old claim as live.
        await service.launch("Freedom Fighters")

    async def test_attaching_to_the_running_game_needs_no_title(self) -> None:
        machine = FakeMachine(steam=[steam_row()], processes=(running(),))
        attached = await machine.service().adopt("")
        self.assertTrue(attached["attached"])
        self.assertFalse(attached["owned"])
        self.assertEqual(attached["entry"]["title"], "Freedom Fighters")
        self.assertEqual(machine.close_calls, [])

    async def test_attaching_with_nothing_running_says_so(self) -> None:
        attached = await FakeMachine(steam=[steam_row()]).service().adopt("")
        self.assertFalse(attached["attached"])
        self.assertIn("nothing to attach to", attached["reason"])

    async def test_a_user_owned_running_game_is_never_closed(self) -> None:
        machine = FakeMachine(steam=[steam_row()], processes=(running(),))
        service = machine.service()
        await service.adopt("Freedom Fighters")
        outcome = await service.close()
        self.assertEqual(outcome["status"], "NO_OWNED_GAME")
        self.assertEqual(machine.close_calls, [])

    async def test_history_records_the_launch_and_the_close(self) -> None:
        machine = FakeMachine(steam=[steam_row()])
        service = machine.service()
        await service.launch("Freedom Fighters")
        await service.close()
        statuses = [row["status"] for row in service.history()]
        self.assertIn("LAUNCH_VERIFIED", statuses)
        self.assertIn("CLOSED", statuses)


class DriveRuleTests(unittest.TestCase):
    def test_a_relative_path_is_never_evidence(self) -> None:
        self.assertFalse(_is_internal_fixed_drive("Games\\Freedom\\Freedom.exe"))

    def test_a_unc_path_is_refused(self) -> None:
        self.assertFalse(_is_internal_fixed_drive("\\\\server\\share\\game.exe"))

    def test_an_empty_path_is_refused(self) -> None:
        self.assertFalse(_is_internal_fixed_drive(""))


def row_for(rows, title: str) -> dict:
    """The catalog row for one title (the inventory rows come first)."""
    for row in rows:
        if row["title"] == title:
            return row
    raise AssertionError(f"no catalog row for {title!r}: {[row['title'] for row in rows]}")


def _async(value):
    async def _inner():
        return value

    return _inner()


class ProcessPatternTests(unittest.TestCase):
    def test_a_concatenated_process_name_is_matched(self) -> None:
        patterns = _process_patterns("Shadow Fight Arena", _blank_artifact("Shadow Fight Arena"))
        self.assertIn("shadowfightarena", patterns, "a process name is one token")

    def test_the_title_itself_is_always_a_pattern(self) -> None:
        self.assertIn("Freedom Fighters", _process_patterns("Freedom Fighters", _blank_artifact("Freedom Fighters")))


class NameNormalisationTests(unittest.TestCase):
    def test_punctuation_and_case_do_not_change_identity(self) -> None:
        self.assertEqual(normalise("  Tom Clancy's  GHOST RECON! "), "tom clancy s ghost recon")

    def test_candidate_aliases_resolve(self) -> None:
        self.assertEqual(candidate_title("freefire").title, "Free Fire")
        self.assertEqual(candidate_title("tk8"), None)
        self.assertEqual(candidate_title("tekken").title, "Tekken 8")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
