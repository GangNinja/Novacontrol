"""NF-12 — the game library wired into the user-facing workflow.

Three layers are pinned here, each at the level it is actually used:

* **Request shape** — ``game_request`` recognises launch/status/attach phrasing
  and refuses a CHAIN, because a multi-clause command belongs to the desktop
  planner that keeps cross-clause context.
* **The HTTP surface** — ``/novaforge/library`` and the launch/close/attach
  routes are driven through the REAL ``create_app()`` against a REAL
  ``GameLaunchService`` whose every platform fact is a fake. A preview mints a
  token and starts nothing; the token executes exactly once; a replayed or
  missing token is a 403 and nothing starts.
* **The chat path** — ``/ask`` answers a status question from the catalog and
  returns an approval-gated PLAN for a launch, while every ordinary
  "open <app>" still travels the desktop plan it always did.

No test launches a game, reads the registry or needs a display.
"""

from __future__ import annotations

import unittest

from test_novaforge_launcher import FakeMachine, running, steam_row
from test_web_api import _IsolatedApiTestCase

from novacontrol.novaforge.launcher import (
    ALREADY_RUNNING,
    AMBIGUOUS_MATCH,
    CANDIDATE_INVENTORY,
    INSTALLED_AND_LAUNCHABLE,
    NOT_FOUND_ON_INTERNAL_STORAGE,
    candidate_title,
    game_request,
    row_matches,
)


class GameRequestShapeTests(unittest.TestCase):
    """What counts as a game request — and what deliberately does not."""

    def test_launch_phrasing_names_the_title(self) -> None:
        for text, name in (
            ("launch GTA V", "GTA V"),
            ("Launch freedom fighters", "freedom fighters"),
            ("open tekken 8", "tekken 8"),
            ("start hollow knight", "hollow knight"),
            ("play gta online", "gta online"),
            ("launch shadow fight arena please", "shadow fight arena"),
            ("launch the game Free Fire", "Free Fire"),
        ):
            with self.subTest(text=text):
                self.assertEqual(game_request(text), ("launch", name))

    def test_status_phrasing_is_recognised(self) -> None:
        for text, name in (
            ("check whether Freedom Fighters is installed", "Freedom Fighters"),
            ("check if tekken 8 is installed.", "tekken 8"),
            ("is hollow knight installed?", "hollow knight"),
            ("do i have quarantine zone", "quarantine zone"),
            ("is clash of clans on this pc", "clash of clans"),
        ):
            with self.subTest(text=text):
                self.assertEqual(game_request(text), ("status", name))

    def test_attach_request_needs_no_name(self) -> None:
        self.assertEqual(
            game_request("attach to the game that is already running"), ("attach", "")
        )
        self.assertEqual(
            game_request("connect to the game that's already running"), ("attach", "")
        )

    def test_a_chain_belongs_to_the_desktop_planner(self) -> None:
        """Half a sentence must never be answered by the game path."""
        for text in (
            "open steam and launch gta v",
            "launch gta v and take a screenshot",
            "open chrome and search for cats and dogs",
        ):
            with self.subTest(text=text):
                self.assertEqual(game_request(text), ("", ""))

    def test_ordinary_application_speech_has_no_catalog_title(self) -> None:
        """The SHAPE parser is deliberately permissive; the catalog is the gate.

        ``game_request`` only reads the sentence ("open X" is a launch shape).
        What keeps "open calculator" on the desktop path is ``candidate_title``:
        the application claims the request only when X is one of the catalog's
        titles. Pinning both halves here stops either from drifting.
        """
        for text in ("open calculator", "start the server", "play music", "open the settings"):
            with self.subTest(text=text):
                kind, name = game_request(text)
                self.assertEqual(kind, "launch")
                self.assertIsNone(candidate_title(name))
        self.assertIsNotNone(candidate_title("gta 5"))

    def test_row_search_matches_titles_and_aliases(self) -> None:
        row = {"title": "GTA V", "query_names": ["GTA V", "gta 5", "grand theft auto v"]}
        self.assertTrue(row_matches(row, "gta"))
        self.assertTrue(row_matches(row, "grand theft"))
        self.assertFalse(row_matches(row, "tekken"))
        self.assertTrue(row_matches(row, ""), "an empty search lists everything")

    def test_the_candidate_inventory_is_still_the_reference_vocabulary(self) -> None:
        """The chat gate keys on this list, so it must not silently shrink."""
        titles = {item.title for item in CANDIDATE_INVENTORY}
        self.assertIn("GTA V", titles)
        self.assertIn("Freedom Fighters", titles)
        self.assertEqual(len(titles), 11)


class LibraryApiFixture(_IsolatedApiTestCase):
    """Real application + real service; only the platform facts are fakes."""

    def setUp(self) -> None:
        self.machine = FakeMachine(steam=[steam_row()])
        self.service = self.machine.service()
        super().setUp()

    def _isolated_application(self, **kwargs: object):
        app = super()._isolated_application(**kwargs)
        # NoopDesktopRunner has none of the launch seams, so the app builds no
        # service; install this one explicitly. The capability stays truthful
        # because the manager reads the same object.
        app.games = self.service
        app.novaforge.game_launcher = self.service
        return app

    # -- helpers -----------------------------------------------------------

    def _library(self, query: str = "") -> dict:
        response = self._client.get("/novaforge/library", params={"q": query})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _plan(self, command: str) -> dict:
        response = self._client.post("/novaforge/library/launch", json={"command": command})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _approve(self, plan: dict) -> dict:
        return self._client.post(
            "/novaforge/library/launch",
            json={"command": plan["command"], "approval_token": plan["approval"]["token"]},
        ).json()

    def _row(self, payload: dict, title: str) -> dict:
        for row in payload["entries"]:
            if row["title"] == title:
                return row
        raise AssertionError(f"no row for {title}: {[r['title'] for r in payload['entries']]}")


class CatalogSurfaceTests(LibraryApiFixture):
    def test_the_library_lists_installed_games_with_their_evidence(self) -> None:
        body = self._library()
        self.assertTrue(body["available"])
        row = self._row(body, "Freedom Fighters")
        self.assertEqual(row["status"], INSTALLED_AND_LAUNCHABLE)
        self.assertEqual(row["artifact"]["source"], "steam")
        self.assertEqual(row["artifact"]["launch_target"], "1234")
        self.assertFalse(row["tested"], "discovery is not a launch test")
        self.assertIn(INSTALLED_AND_LAUNCHABLE, body["counts"])
        self.assertTrue(body["limitations"])
        self.assertTrue(body["memory"]["ok"])

    def test_search_filters_without_inventing_a_row(self) -> None:
        body = self._library("freedom")
        self.assertTrue(body["entries"])
        self.assertTrue(all("freedom" in row["title"].lower() for row in body["entries"]))
        self.assertLessEqual(body["matched"], body["total"])
        self.assertEqual(self._library("nothing-like-this")["entries"], [])

    def test_the_unread_owned_claim_is_reported_separately(self) -> None:
        body = self._library()
        self.assertIsNone(body["owned"])
        self.assertEqual(body["history"], [])

    def test_a_missing_launch_service_is_stated_not_faked(self) -> None:
        """A host with no seams says so instead of returning an empty library."""
        self._nova.games = None
        body = self._library()
        self.assertFalse(body["available"])
        self.assertEqual(body["entries"], [])
        self.assertIn("No game catalog", body["reason"])

    def test_resolve_answers_without_launching(self) -> None:
        body = self._client.get(
            "/novaforge/library/resolve", params={"name": "freedom fighters"}
        ).json()
        self.assertEqual(body["status"], INSTALLED_AND_LAUNCHABLE)
        self.assertTrue(body["launchable"])
        self.assertEqual(self.machine.launch_calls, [])
        unknown = self._client.get(
            "/novaforge/library/resolve", params={"name": "hogwarts legacy"}
        ).json()
        self.assertEqual(unknown["status"], NOT_FOUND_ON_INTERNAL_STORAGE)
        self.assertFalse(unknown["launchable"])

    def test_a_runtime_only_title_is_reported_with_its_reason(self) -> None:
        body = self._library("clash")
        row = self._row(body, "Clash of Clans")
        self.assertEqual(row["status"], "REQUIRES_LAUNCHER_OR_RUNTIME")
        self.assertIn("Android", row["reason"])


class LaunchPlanAndExecuteTests(LibraryApiFixture):
    def test_a_preview_mints_a_token_and_starts_nothing(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        self.assertEqual(plan["route"], "game_launch")
        self.assertEqual(plan["status"], "waiting_for_approval")
        self.assertTrue(plan["approval"]["token"])
        self.assertFalse(plan["approval"]["approved"])
        self.assertEqual(plan["target"], "Freedom Fighters")
        self.assertTrue(plan["steps"])
        self.assertEqual(self.machine.launch_calls, [], "planning must not launch")

    def test_an_approved_plan_launches_once_and_reports_every_stage(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        result = self._approve(plan)
        self.assertEqual(result["status"], "LAUNCH_VERIFIED")
        self.assertEqual(len(self.machine.launch_calls), 1)
        self.assertEqual(self.machine.launch_calls[0]["kind"], "steam")
        self.assertEqual(self.machine.launch_calls[0]["target"], "1234")
        self.assertTrue(result["stages"]["launch_requested"])
        self.assertTrue(result["stages"]["process_started"])
        self.assertTrue(result["stages"]["window_detected"])
        self.assertTrue(result["stages"]["launch_verified"])
        self.assertEqual(result["execution_results"][0]["status"], "completed")
        self.assertIn("pid", result["game"]["launch"])

    def test_the_token_is_single_use(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        self._approve(plan)
        replay = self._client.post(
            "/novaforge/library/launch",
            json={"command": plan["command"], "approval_token": plan["approval"]["token"]},
        )
        self.assertEqual(replay.status_code, 403, replay.text)
        self.assertEqual(len(self.machine.launch_calls), 1, "a replayed token launched again")

    def test_execution_without_a_token_is_refused(self) -> None:
        response = self._client.post("/command/execute", json={"command": "launch Freedom Fighters"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.machine.launch_calls, [])

    def test_a_process_without_a_window_is_not_a_verified_launch(self) -> None:
        self.machine.window_after = ""
        plan = self._plan("launch Freedom Fighters")
        result = self._approve(plan)
        self.assertTrue(result["stages"]["process_started"])
        self.assertFalse(result["stages"]["window_detected"])
        self.assertFalse(result["stages"]["launch_verified"])
        self.assertEqual(result["status"], "INSTALLED_BUT_LAUNCH_FAILED")
        self.assertEqual(result["execution_results"][0]["status"], "failed")
        # It is still this service's own process, so it can be closed.
        self.assertIsNotNone(self.service.owned)

    def test_a_launch_that_produces_no_process_says_exactly_that(self) -> None:
        self.machine.spawn = False
        plan = self._plan("launch Freedom Fighters")
        result = self._approve(plan)
        self.assertFalse(result["stages"]["process_started"])
        self.assertFalse(result["stages"]["launch_verified"])
        self.assertIn("no new matching process", result["summary"])

    def test_a_refused_launch_command_is_reported_with_its_cause(self) -> None:
        async def broken(**_: object) -> dict:
            raise ValueError("Steam deep-link launch requires Windows.")

        self.machine.launch = broken  # type: ignore[method-assign]
        self.service = self.machine.service()
        self._nova.games = self.service
        self._nova.novaforge.game_launcher = self.service
        plan = self._plan("launch Freedom Fighters")
        result = self._approve(plan)
        self.assertFalse(result["stages"]["launch_verified"])
        self.assertIn("requires Windows", result["summary"])

    def test_the_resource_gate_refuses_before_a_token_is_minted(self) -> None:
        self.machine.free_gb = 0.6
        plan = self._plan("launch Freedom Fighters")
        self.assertEqual(plan["status"], "LAUNCH_REFUSED")
        self.assertNotIn("token", plan["approval"])
        self.assertIn("0.6 GB free", plan["summary"])
        self.assertEqual(self.machine.launch_calls, [])

    def test_an_ambiguous_name_prepares_nothing(self) -> None:
        self.machine.steam_rows = [
            steam_row("Freedom Fighters", "111", "C:\\Games\\FF1", "C:\\Games\\FF1\\ff.exe"),
            steam_row(
                "Freedom Fighters Remastered", "222", "C:\\Games\\FF2", "C:\\Games\\FF2\\ff.exe"
            ),
        ]
        plan = self._plan("launch freedom fighters")
        self.assertEqual(plan["status"], AMBIGUOUS_MATCH)
        self.assertNotIn("token", plan["approval"])
        self.assertEqual(len(plan["candidates"]), 2)
        self.assertEqual(self.machine.launch_calls, [])

    def test_a_missing_title_is_not_found_and_nothing_starts(self) -> None:
        plan = self._plan("launch hollow knight")
        self.assertEqual(plan["status"], NOT_FOUND_ON_INTERNAL_STORAGE)
        self.assertNotIn("token", plan["approval"])
        self.assertIn("No verified installation", plan["summary"])

    def test_a_running_game_is_attached_not_launched_again(self) -> None:
        self.machine.process_rows = [running()]
        plan = self._plan("launch Freedom Fighters")
        self.assertEqual(plan["status"], ALREADY_RUNNING)
        self.assertNotIn("token", plan["approval"])
        self.assertEqual(self.machine.launch_calls, [])
        attached = self._client.post(
            "/novaforge/library/attach", json={"name": "Freedom Fighters"}
        ).json()
        self.assertTrue(attached["attached"])
        self.assertFalse(attached["owned"])

    def test_a_tracked_instance_blocks_the_next_launch_plan(self) -> None:
        """One game at a time: the PREVIEW refuses, so no token is minted."""
        self.machine.steam_rows = [
            steam_row(),
            steam_row(
                "Grand Theft Auto V", "3240220", "C:\\Games\\GTAV", "C:\\Games\\GTAV\\GTA5.exe"
            ),
        ]
        self._approve(self._plan("launch Freedom Fighters"))
        blocked = self._plan("launch GTA V")
        self.assertEqual(blocked["status"], "ANOTHER_GAME_OWNED")
        self.assertNotIn("token", blocked["approval"])
        self.assertIn("not been confirmed closed", blocked["summary"])
        self.assertEqual(len(self.machine.launch_calls), 1)

    def test_a_claim_whose_process_exited_does_not_block_the_next_plan(self) -> None:
        self.machine.steam_rows = [
            steam_row(),
            steam_row(
                "Grand Theft Auto V", "3240220", "C:\\Games\\GTAV", "C:\\Games\\GTAV\\GTA5.exe"
            ),
        ]
        self._approve(self._plan("launch Freedom Fighters"))
        self.machine.process_rows.clear()  # it exited on its own
        next_plan = self._plan("launch GTA V")
        self.assertEqual(next_plan["status"], "waiting_for_approval")
        self.assertTrue(next_plan["approval"]["token"])

    def test_capture_unavailable_is_reported_as_unavailable(self) -> None:
        """A verified launch must not borrow the capture capability's answer."""
        self._nova.novaforge.capture_supported = False
        plan = self._plan("launch Freedom Fighters")
        self.assertFalse(plan["observation"]["available"])
        self.assertTrue(plan["observation"]["reason"])
        result = self._approve(plan)
        self.assertTrue(result["stages"]["launch_verified"])
        self.assertFalse(result["observation"]["available"])
        self.assertFalse(result["observation"]["implied_by_launch"])

    def test_observation_is_never_implied_by_a_verified_launch(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        result = self._approve(plan)
        self.assertTrue(result["stages"]["launch_verified"])
        self.assertFalse(result["observation"]["implied_by_launch"])
        self.assertIn("not a verified capture", result["observation"]["note"])
        # The availability flag is the capability's own reading, not a constant.
        expected = bool(self._nova.novaforge.capabilities()["capture"]["available"])
        self.assertEqual(result["observation"]["available"], expected)


class CleanupApiTests(LibraryApiFixture):
    def test_close_uses_a_graceful_window_close_and_confirms_exit(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        self._approve(plan)
        outcome = self._client.post("/novaforge/library/close", json={}).json()
        self.assertEqual(outcome["status"], "CLOSED")
        self.assertTrue(outcome["closed"])
        self.assertEqual(self.machine.close_calls, ["Freedom Fighters"])
        self.assertIsNone(self.service.owned, "the claim is released only on confirmed exit")
        self.assertIsNone(self._library()["owned"])

    def test_a_shutdown_timeout_is_reported_and_never_escalates_to_a_kill(self) -> None:
        self.machine.closes = False
        plan = self._plan("launch Freedom Fighters")
        self._approve(plan)
        outcome = self._client.post("/novaforge/library/close", json={}).json()
        self.assertEqual(outcome["status"], "SHUTDOWN_TIMEOUT")
        self.assertFalse(outcome["closed"])
        self.assertIn("No process was killed", outcome["summary"])
        self.assertIsNotNone(self.service.owned)

    def test_a_user_owned_running_game_is_never_closed(self) -> None:
        self.machine.process_rows = [running()]
        self._client.post("/novaforge/library/attach", json={"name": "Freedom Fighters"})
        outcome = self._client.post("/novaforge/library/close", json={}).json()
        self.assertEqual(outcome["status"], "NO_OWNED_GAME")
        self.assertEqual(self.machine.close_calls, [])

    def test_closing_a_different_launch_id_is_refused(self) -> None:
        plan = self._plan("launch Freedom Fighters")
        self._approve(plan)
        outcome = self._client.post(
            "/novaforge/library/close", json={"launch_id": "launch-999"}
        ).json()
        self.assertEqual(outcome["status"], "NOT_OWNED")
        self.assertEqual(self.machine.close_calls, [])


class UnifiedCommandDoorTests(LibraryApiFixture):
    """/command/plan and /command/execute keep ONE approval surface for games."""

    def test_command_plan_routes_a_game_to_the_verified_plan(self) -> None:
        plan = self._client.post(
            "/command/plan", json={"command": "launch Freedom Fighters"}
        ).json()
        self.assertEqual(plan["route"], "game_launch")
        self.assertTrue(plan["approval"]["token"])
        self.assertEqual(self.machine.launch_calls, [])

    def test_command_execute_runs_the_approved_game_launch(self) -> None:
        plan = self._client.post(
            "/command/plan", json={"command": "launch Freedom Fighters"}
        ).json()
        result = self._client.post(
            "/command/execute",
            json={"command": plan["command"], "approval_token": plan["approval"]["token"]},
        ).json()
        self.assertEqual(result["status"], "LAUNCH_VERIFIED")
        self.assertEqual(len(self.machine.launch_calls), 1)

    def test_an_ordinary_application_still_plans_as_a_desktop_command(self) -> None:
        plan = self._client.post("/command/plan", json={"command": "open calculator"}).json()
        self.assertNotEqual(plan.get("route"), "game_launch")
        self.assertTrue(plan["approval"]["token"])


class ChatRoutingTests(LibraryApiFixture):
    """/ask answers a game question from the catalog, never by guessing."""

    def _ask(self, text: str) -> dict:
        response = self._client.post("/ask", json={"request": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_launch_in_chat_returns_a_plan_and_starts_nothing(self) -> None:
        body = self._ask("launch Freedom Fighters")
        self.assertEqual(body["route"], "game_launch")
        self.assertEqual(body["data"]["status"], "waiting_for_approval")
        self.assertTrue(body["data"]["approval"]["token"])
        self.assertEqual(self.machine.launch_calls, [])

    def test_a_chat_plan_can_be_approved_through_the_command_door(self) -> None:
        body = self._ask("launch Freedom Fighters")
        plan = body["data"]
        result = self._client.post(
            "/command/execute",
            json={"command": plan["command"], "approval_token": plan["approval"]["token"]},
        ).json()
        self.assertEqual(result["status"], "LAUNCH_VERIFIED")

    def test_a_status_question_is_answered_from_the_catalog_row(self) -> None:
        body = self._ask("check whether Freedom Fighters is installed")
        self.assertEqual(body["route"], "game_library")
        self.assertEqual(body["data"]["status"], INSTALLED_AND_LAUNCHABLE)
        self.assertIn("installed and launchable", body["summary"].lower())
        self.assertEqual(self.machine.launch_calls, [])

    def test_a_missing_game_is_reported_as_missing(self) -> None:
        body = self._ask("is hollow knight installed")
        self.assertEqual(body["data"]["status"], NOT_FOUND_ON_INTERNAL_STORAGE)
        self.assertIn("not found on internal storage", body["summary"].lower())

    def test_ordinary_application_launches_are_untouched(self) -> None:
        """The name gate keeps non-game launches on the desktop path."""
        body = self._ask("open calculator")
        self.assertEqual(body["route"], "desktop_automation")
        self.assertNotIn("game", body["data"].get("route", ""))
        self.assertEqual(self.machine.launch_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
