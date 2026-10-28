"""NF-23 — real-game compatibility and observation: the observation ladder.

The ladder's whole reason to exist is that a stage may only be true from its OWN
evidence: a discovered title is not a running process, a running process is not
an identified window, an identified window is not a captured frame, and a frame
is not an accepted observation. These tests pin that separation, the
read-only embedding rows (measured, never required), and the eight live states
the panel is allowed to show.

Nothing in this file claims a real game was observed; the live evidence for
NF-23 lives in ``docs/NOVAFORGE_NF23_REAL_GAME_COMPATIBILITY_REPORT.md``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

try:
    from fastapi.testclient import TestClient  # noqa: F401 - requires httpx
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]

from test_web_api import _IsolatedApiTestCase

from novacontrol.novaforge.compat import (
    EMBEDDING_EMBEDDED,
    EMBEDDING_REFUSED,
    CompatibilityClass,
    CompatibilityLog,
    CompatibilityRecord,
)
from novacontrol.novaforge.launcher import (
    ALREADY_RUNNING,
    AMBIGUOUS_MATCH,
    INSTALLED_AND_LAUNCHABLE,
    NOT_FOUND_ON_INTERNAL_STORAGE,
)
from novacontrol.novaforge.manager import (
    EMBEDDING_LADDER_STAGES,
    OBSERVATION_LADDER_STAGES,
    NovaForgeManager,
)
from novacontrol.novaforge.state_service import GameStateService
from novacontrol.persistence import JsonStateStore

REPO = Path(__file__).resolve().parent.parent
PANEL_JS = str(REPO / "src" / "novacontrol" / "web" / "static" / "js" / "novaforge.js")
INDEX_HTML = REPO / "src" / "novacontrol" / "web" / "static" / "index.html"
GAME = "Shadow Fight Arena"


# ── seams ────────────────────────────────────────────────────────────────


async def _capture(path: str) -> dict[str, Any]:  # pragma: no cover - never called
    return {"fresh": True, "path": path}


async def _geometry(title: str) -> Any | None:  # pragma: no cover - never called
    return None


async def _window_lines() -> list[str] | None:  # pragma: no cover - never called
    return None


class _Catalog:
    """A catalog whose answer is fixed, and whose calls are counted."""

    def __init__(self, resolution: dict[str, Any] | None = None, *, explode: bool = False):
        self.resolution = resolution
        self.explode = explode
        self.calls = 0

    async def resolve(self, query: str) -> dict[str, Any]:
        self.calls += 1
        if self.explode:
            raise RuntimeError("the catalog is unreadable")
        return dict(self.resolution or {})


class _Launcher:
    """The launch service as the manager uses it: a catalog and one owned row."""

    def __init__(
        self,
        resolution: dict[str, Any] | None = None,
        *,
        owned: Any | None = None,
        explode: bool = False,
    ):
        self.catalog = _Catalog(resolution, explode=explode)
        self._owned = owned

    def owned(self) -> Any | None:
        return self._owned


class _ObserverStub:
    """The observer's own status, as the ladder reads it."""

    def __init__(self, status: dict[str, Any]):
        self._status = status

    def status(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._status))


def entry(
    *,
    title: str = GAME,
    status: str = INSTALLED_AND_LAUNCHABLE,
    launchable: bool = True,
    running: dict[str, Any] | None = None,
    reason: str = "Verified launchable installation.",
) -> dict[str, Any]:
    return {
        "title": title,
        "status": status,
        "reason": reason,
        "artifact": {
            "key": "steam:123",
            "name": title,
            "source": "steam",
            "launch_kind": "steam",
            "launch_target": "123",
            "executable_exists": True,
            "launchable": launchable,
        },
        "running": running,
        "candidates": [],
    }


def running_launcher(*, window: str = GAME) -> _Launcher:
    """A title whose process probe found a running process with a window."""
    running = {
        "pid": 4321,
        "name": "game.exe",
        "start_time": "2026-10-27T09:00:00Z",
        "window": window,
    }
    return _Launcher(
        {
            "status": ALREADY_RUNNING,
            "entry": entry(status=ALREADY_RUNNING, running=running),
            "candidates": [],
        }
    )


def observer_status(
    *,
    state: str = "idle",
    last: dict[str, Any] | None = None,
    success: dict[str, Any] | None = None,
    counts: dict[str, int] | None = None,
    backend: str = "",
    pause: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "available": True,
        "state": state,
        "session_id": "obs-test" if target else "",
        "target": target,
        "backend": backend,
        "last_observation": last,
        "last_success": success,
        "counts": counts or {"observed": 0, "rejected": 0, "unavailable": 0, "attempts": 0},
        "pause": pause or {"code": "", "detail": "", "user_requested": False},
        "reason": "",
    }


def observed_record(*, fresh: bool = True) -> dict[str, Any]:
    return {
        "observation_id": "obs-1",
        "seq": 1,
        "status": "observed",
        "captured_at": "2026-10-27T10:00:00+00:00",
        "reason_code": "ok",
        "reason": "",
        "backend": "printwindow",
        "source": "window_surface",
        "validation": {
            "capture_ok": True,
            "target_visible": True,
            "fresh": fresh,
            "content_ok": True,
            "livable": fresh,
            "content": "valid",
            "reason": "",
        },
    }


# ── the ladder ───────────────────────────────────────────────────────────


class ObservationLadderTests(unittest.TestCase):
    """Ten stages, each decided from its own evidence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _manager(
        self,
        *,
        launcher: Any | None = None,
        capture: Any = _capture,
        status: dict[str, Any] | None = None,
    ) -> NovaForgeManager:
        manager = NovaForgeManager(
            data_dir=self.root,
            capture=capture,
            geometry=_geometry,
            window_lines=_window_lines,
        )
        if launcher is not None:
            manager.game_launcher = launcher
        if status is not None:
            manager.observer = _ObserverStub(status)  # type: ignore[assignment]
        return manager

    def _ladder(self, manager: NovaForgeManager, title: str = "") -> dict[str, Any]:
        return asyncio.run(manager.observation_ladder(title))

    def _stage(self, ladder: dict[str, Any], stage: str) -> dict[str, Any]:
        return next(row for row in ladder["stages"] if row["stage"] == stage)

    def test_every_stage_is_present_in_order_and_embedding_is_marked(self) -> None:
        ladder = self._ladder(self._manager())
        self.assertEqual(len(OBSERVATION_LADDER_STAGES), 10)
        self.assertEqual([row["stage"] for row in ladder["stages"]], list(OBSERVATION_LADDER_STAGES))
        self.assertEqual(ladder["stage_order"], list(OBSERVATION_LADDER_STAGES))
        self.assertEqual(ladder["embedding_stages"], list(EMBEDDING_LADDER_STAGES))
        self.assertEqual(set(ladder["stages"][0]), {"stage", "state", "detail", "evidence"})

    def test_a_discovered_launchable_title_claims_no_process_and_no_window(self) -> None:
        launcher = _Launcher(
            {"status": INSTALLED_AND_LAUNCHABLE, "entry": entry(), "candidates": []}
        )
        ladder = self._ladder(self._manager(launcher=launcher), GAME)
        self.assertEqual(self._stage(ladder, "discovered")["state"], "true")
        self.assertEqual(self._stage(ladder, "launchable")["state"], "true")
        # The anti-inference rule: neither of these may be borrowed from above.
        self.assertEqual(self._stage(ladder, "process_started")["state"], "false")
        self.assertEqual(self._stage(ladder, "window_identified")["state"], "unknown")
        self.assertEqual(self._stage(ladder, "frame_acquired")["state"], "unknown")
        self.assertEqual(ladder["held_at"], "process_started")

    def test_a_running_process_without_a_window_is_not_an_identified_window(self) -> None:
        launcher = _Launcher(
            {
                "status": ALREADY_RUNNING,
                "entry": entry(
                    status=ALREADY_RUNNING,
                    running={"pid": 4321, "name": "game.exe", "start_time": "2026-10-27T09:00:00Z"},
                ),
                "candidates": [],
            }
        )
        ladder = self._ladder(self._manager(launcher=launcher), GAME)
        self.assertEqual(self._stage(ladder, "process_started")["state"], "true")
        self.assertEqual(self._stage(ladder, "process_started")["evidence"]["pid"], 4321)
        self.assertEqual(self._stage(ladder, "window_identified")["state"], "unknown")
        self.assertEqual(ladder["held_at"], "window_identified")

    def test_a_probed_window_identifies_it_but_still_acquires_no_frame(self) -> None:
        running = {
            "pid": 4321,
            "name": "game.exe",
            "start_time": "2026-10-27T09:00:00Z",
            "window": GAME,
        }
        launcher = _Launcher(
            {"status": ALREADY_RUNNING, "entry": entry(status=ALREADY_RUNNING, running=running),
             "candidates": []}
        )
        ladder = self._ladder(self._manager(launcher=launcher), GAME)
        self.assertEqual(self._stage(ladder, "window_identified")["state"], "true")
        self.assertEqual(self._stage(ladder, "window_identified")["evidence"]["window"], GAME)
        # A window is not a frame: acquisition stays untried, not true.
        self.assertEqual(self._stage(ladder, "backend_selected")["state"], "unknown")
        self.assertEqual(self._stage(ladder, "frame_acquired")["state"], "unknown")
        self.assertEqual(ladder["held_at"], "backend_selected")

    def test_an_ambiguous_title_is_discovered_but_never_launchable(self) -> None:
        launcher = _Launcher(
            {
                "status": AMBIGUOUS_MATCH,
                "entry": None,
                "candidates": [
                    {"title": "Shadow Fight Arena", "status": INSTALLED_AND_LAUNCHABLE},
                    {"title": "Shadow Fight Arena HD", "status": INSTALLED_AND_LAUNCHABLE},
                ],
            }
        )
        ladder = self._ladder(self._manager(launcher=launcher), GAME)
        discovered = self._stage(ladder, "discovered")
        self.assertEqual(discovered["state"], "true")
        self.assertEqual(len(discovered["evidence"]["candidates"]), 2)
        launchable = self._stage(ladder, "launchable")
        self.assertEqual(launchable["state"], "false")
        self.assertIn("never guessed", launchable["detail"])
        # The ambiguity is where the ladder is held: an operator has to name the
        # edition, and nothing below this row may be claimed until they do.
        self.assertEqual(ladder["held_at"], "launchable")

    def test_a_title_that_is_not_installed_is_not_discovered(self) -> None:
        launcher = _Launcher(
            {
                "status": NOT_FOUND_ON_INTERNAL_STORAGE,
                "entry": entry(
                    title="Nope", status=NOT_FOUND_ON_INTERNAL_STORAGE, launchable=False,
                    reason="No installed title matches that name.",
                ),
                "candidates": [],
            }
        )
        ladder = self._ladder(self._manager(launcher=launcher), "Nope")
        self.assertEqual(self._stage(ladder, "discovered")["state"], "false")
        self.assertEqual(self._stage(ladder, "launchable")["state"], "false")
        self.assertEqual(ladder["held_at"], "discovered")

    def test_an_unreadable_catalog_is_a_gap_not_a_verdict(self) -> None:
        launcher = _Launcher(explode=True)
        ladder = self._ladder(self._manager(launcher=launcher), GAME)
        self.assertEqual(self._stage(ladder, "discovered")["state"], "unknown")
        self.assertIn("could not be read", self._stage(ladder, "discovered")["detail"])
        self.assertEqual(self._stage(ladder, "launchable")["state"], "unknown")

    def test_no_wired_catalog_says_so_instead_of_claiming_launch_is_impossible(self) -> None:
        ladder = self._ladder(self._manager(), GAME)
        self.assertEqual(self._stage(ladder, "discovered")["state"], "not_wired")
        self.assertEqual(self._stage(ladder, "launchable")["state"], "not_wired")
        self.assertEqual(ladder["held_at"], "discovered")

    def test_nothing_is_resolved_without_a_title_or_a_bound_target(self) -> None:
        """The catalog probe spawns a process; an empty question never runs it."""
        launcher = _Launcher({"status": INSTALLED_AND_LAUNCHABLE, "entry": entry(), "candidates": []})
        ladder = self._ladder(self._manager(launcher=launcher))
        self.assertEqual(launcher.catalog.calls, 0)
        self.assertEqual(self._stage(ladder, "discovered")["state"], "unknown")
        self.assertEqual(self._stage(ladder, "launchable")["state"], "unknown")

    def test_a_bound_observation_reports_its_own_stages_as_true(self) -> None:
        status = observer_status(
            state="observing",
            target={
                "title": GAME,
                "game": GAME,
                "handle": 17185,
                "pid": 4321,
                "process": "game.exe",
                "start_time": "2026-10-27T09:00:00Z",
                "identity": "pid:4321:2026-10-27T09:00:00Z",
            },
            last=observed_record(),
            success={
                "observation_id": "obs-1",
                "seq": 1,
                "at": "2026-10-27T10:00:00+00:00",
                "width": 1920,
                "height": 1080,
                "backend": "printwindow",
                "source": "window_surface",
                "capture_ms": 8.5,
            },
            counts={"observed": 5, "rejected": 1, "unavailable": 0, "attempts": 6},
            backend="printwindow",
        )
        launcher = running_launcher()
        ladder = self._ladder(self._manager(launcher=launcher, status=status))
        # The bound title is what the catalog was asked about (once).
        self.assertEqual(launcher.catalog.calls, 1)
        self.assertEqual(self._stage(ladder, "process_started")["state"], "true")
        self.assertEqual(self._stage(ladder, "window_identified")["state"], "true")
        self.assertEqual(self._stage(ladder, "backend_selected")["state"], "true")
        self.assertEqual(self._stage(ladder, "frame_acquired")["state"], "true")
        self.assertEqual(self._stage(ladder, "frame_validated")["state"], "true")
        accepted = self._stage(ladder, "observation_accepted")
        self.assertEqual(accepted["state"], "true")
        self.assertEqual(accepted["evidence"]["observed"], 5)
        # Every observation-path stage is true, so nothing is holding it.
        self.assertEqual(ladder["held_at"], "")
        self.assertTrue(ladder["accepted"])
        self.assertEqual(ladder["target"]["title"], GAME)

    def test_a_rejected_frame_is_never_an_accepted_observation(self) -> None:
        rejected = {
            "seq": 2,
            "status": "rejected",
            "captured_at": "2026-10-27T10:00:01+00:00",
            "reason_code": "capture_rejected",
            "reason": "The frame is blank; nothing was painted.",
            "backend": "",
            "validation": {
                "capture_ok": True,
                "target_visible": True,
                "fresh": False,
                "content_ok": False,
                "livable": False,
                "content": "blank",
                "reason": "The frame is blank; nothing was painted.",
                # A refused read still names the backend that served it.
                "backend": "printwindow",
            },
        }
        status = observer_status(
            state="observing",
            last=rejected,
            counts={"observed": 0, "rejected": 3, "unavailable": 0, "attempts": 3},
        )
        ladder = self._ladder(
            self._manager(launcher=running_launcher(), status=status), GAME
        )
        # The backend DID serve the read, so it is selected: the ladder is held
        # at the frame, not at the backend that produced it.
        self.assertEqual(self._stage(ladder, "backend_selected")["state"], "true")
        self.assertEqual(self._stage(ladder, "frame_acquired")["state"], "false")
        self.assertEqual(self._stage(ladder, "frame_validated")["state"], "false")
        self.assertEqual(self._stage(ladder, "observation_accepted")["state"], "false")
        self.assertFalse(ladder["accepted"])
        self.assertEqual(ladder["held_at"], "frame_acquired")
        self.assertEqual(
            self._stage(ladder, "frame_validated")["evidence"]["content"], "blank"
        )

    def test_a_stale_validated_frame_keeps_its_stages_and_the_observation_holds_where_it_stopped(
        self,
    ) -> None:
        status = observer_status(
            state="stopped",
            last=observed_record(fresh=False),
            success={
                "observation_id": "obs-9",
                "seq": 9,
                "at": "2026-10-27T09:00:00+00:00",
                "width": 800,
                "height": 500,
                "backend": "printwindow",
            },
            counts={"observed": 9, "rejected": 0, "unavailable": 0, "attempts": 9},
            backend="printwindow",
        )
        ladder = self._ladder(self._manager(status=status))
        # The frame WAS acquired and WAS validated; only its freshness lapsed, so
        # both stages stay true and the record is what carries that.
        self.assertEqual(self._stage(ladder, "frame_validated")["state"], "true")
        self.assertEqual(self._stage(ladder, "observation_accepted")["state"], "true")

    def test_a_later_refusal_does_not_lend_its_content_to_the_validated_stage(self) -> None:
        """Evidence belongs to the record it describes. After a success, a later
        blank refusal is evidence for THAT refusal: putting its content on the
        validated row described a frame the refusal never produced."""
        refused = {
            "seq": 4,
            "status": "rejected",
            "captured_at": "2026-10-27T10:00:04+00:00",
            "reason_code": "capture_rejected",
            "reason": "The frame is blank; nothing was painted.",
            "backend": "printwindow",
            "validation": {
                "capture_ok": True,
                "target_visible": True,
                "fresh": False,
                "content_ok": False,
                "livable": False,
                "content": "blank",
                "reason": "The frame is blank; nothing was painted.",
                "backend": "printwindow",
            },
        }
        status = observer_status(
            state="observing",
            last=refused,
            success={
                "observation_id": "obs-1",
                "seq": 1,
                "at": "2026-10-27T10:00:00+00:00",
                "width": 800,
                "height": 500,
                "backend": "printwindow",
            },
            counts={"observed": 1, "rejected": 1, "unavailable": 0, "attempts": 2},
            backend="printwindow",
        )
        ladder = self._ladder(self._manager(status=status))
        row = self._stage(ladder, "frame_validated")
        self.assertEqual(row["state"], "true")
        self.assertNotIn("content", row["evidence"])
        self.assertNotIn("limitations", row["evidence"])
        # A frame WAS acquired and validated earlier, so those stages stay true;
        # the point is only that the later refusal's content is not theirs.
        self.assertEqual(self._stage(ladder, "frame_acquired")["state"], "true")
        self.assertEqual(
            self._stage(ladder, "backend_selected")["state"], "true"
        )

    def test_capture_that_is_not_wired_stops_the_ladder_at_the_backend(self) -> None:
        status = observer_status(
            state="observing",
            target={
                "title": GAME,
                "handle": 17185,
                "pid": 4321,
                "process": "game.exe",
                "start_time": "2026-10-27T09:00:00Z",
                "identity": "pid:4321:2026-10-27T09:00:00Z",
            },
        )
        ladder = self._ladder(
            self._manager(capture=None, launcher=running_launcher(), status=status)
        )
        row = self._stage(ladder, "backend_selected")
        self.assertEqual(row["state"], "not_wired")
        self.assertEqual(row["detail"], "No display-capture seam is wired.")
        self.assertEqual(self._stage(ladder, "window_identified")["state"], "true")
        self.assertEqual(ladder["held_at"], "backend_selected")


class LadderEmbeddingTests(unittest.TestCase):
    """Embedding is MEASURED from the host's history and never holds the ladder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _manager(self) -> NovaForgeManager:
        return NovaForgeManager(
            data_dir=self.root,
            capture=_capture,
            geometry=_geometry,
            window_lines=_window_lines,
        )

    def _record(
        self,
        *,
        game: str = GAME,
        classification: CompatibilityClass = CompatibilityClass.UNSUPPORTED,
        embedding: str = EMBEDDING_REFUSED,
        reason_code: str = "fullscreen_presentation_unsupported",
        reason: str = "The window is a borderless/fullscreen presentation surface.",
        window_mode: str = "borderless_fullscreen",
    ) -> None:
        log = CompatibilityLog(JsonStateStore(self.root))
        log.record(
            CompatibilityRecord(
                game=game,
                classification=classification,
                reason_code=reason_code,
                reason=reason,
                window_mode=window_mode,
                embedding=embedding,
            )
        )

    def _ladder(self, title: str = GAME) -> dict[str, Any]:
        return asyncio.run(self._manager().observation_ladder(title))

    def _stage(self, ladder: dict[str, Any], stage: str) -> dict[str, Any]:
        return next(row for row in ladder["stages"] if row["stage"] == stage)

    def test_no_record_is_unknown_and_nothing_attempted(self) -> None:
        ladder = self._ladder()
        compatible = self._stage(ladder, "embedding_compatible")
        self.assertEqual(compatible["state"], "unknown")
        self.assertEqual(compatible["evidence"]["records"], 0)
        embedded = self._stage(ladder, "embedded")
        self.assertEqual(embedded["state"], "false")
        self.assertIn("Nothing has attempted", embedded["detail"])

    def test_an_unsupported_window_is_reported_from_the_hosts_own_record(self) -> None:
        self._record()
        ladder = self._ladder()
        compatible = self._stage(ladder, "embedding_compatible")
        self.assertEqual(compatible["state"], "false")
        self.assertEqual(compatible["evidence"]["classification"], "UNSUPPORTED")
        self.assertEqual(
            compatible["evidence"]["reason_code"], "fullscreen_presentation_unsupported"
        )
        self.assertEqual(compatible["evidence"]["window_mode"], "borderless_fullscreen")
        self.assertEqual(self._stage(ladder, "embedded")["state"], "false")
        self.assertEqual(self._stage(ladder, "embedded")["evidence"]["embedding"], "refused")

    def test_an_embedded_window_is_reported_as_embedded(self) -> None:
        self._record(
            classification=CompatibilityClass.SUPPORTED,
            embedding=EMBEDDING_EMBEDDED,
            reason_code="embedded",
            reason="The window was embedded in the host.",
            window_mode="windowed",
        )
        ladder = self._ladder()
        self.assertEqual(self._stage(ladder, "embedding_compatible")["state"], "true")
        self.assertEqual(self._stage(ladder, "embedded")["state"], "true")

    def test_embedding_never_holds_the_observation_path(self) -> None:
        """External capture is a supported path: an embed is not the gate."""
        self._record()
        ladder = self._ladder()
        self.assertNotIn(ladder["held_at"], EMBEDDING_LADDER_STAGES)
        # With no catalog wired either, the FIRST observation-path stage is the
        # one reported — never the embedding rows in between.
        self.assertEqual(ladder["held_at"], "discovered")

    def test_a_recorded_window_title_matches_the_requested_game(self) -> None:
        self._record(game="Shadow Fight Arena — Windowed")
        ladder = self._ladder(GAME)
        self.assertEqual(self._stage(ladder, "embedding_compatible")["state"], "false")

    def test_another_games_record_is_not_borrowed(self) -> None:
        self._record(game="Some Other Game")
        ladder = self._ladder(GAME)
        self.assertEqual(self._stage(ladder, "embedding_compatible")["state"], "unknown")
        self.assertEqual(self._stage(ladder, "embedding_compatible")["evidence"]["records"], 1)

    def test_an_unreadable_history_is_a_gap_not_an_error(self) -> None:
        # A parent that is a regular FILE makes the store unusable on every OS.
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        manager = NovaForgeManager(
            data_dir=blocker,
            capture=_capture,
            geometry=_geometry,
            window_lines=_window_lines,
        )
        ladder = asyncio.run(manager.observation_ladder(GAME))
        self.assertEqual(self._stage(ladder, "embedding_compatible")["state"], "unknown")


class LadderApiTests(_IsolatedApiTestCase):
    """The HTTP surface: the ladder rides the capabilities payload."""

    def _isolated_application(self, **kwargs: Any):
        app = super()._isolated_application(**kwargs)
        self.launcher = _Launcher(
            {"status": INSTALLED_AND_LAUNCHABLE, "entry": entry(), "candidates": []}
        )
        manager = NovaForgeManager(
            data_dir=Path(self._tmp.name),
            capture=_capture,
            geometry=_geometry,
            window_lines=_window_lines,
        )
        manager.game_launcher = self.launcher
        app.novaforge = manager
        return app

    def test_capabilities_carry_the_ten_stage_ladder(self) -> None:
        response = self._client.get("/novaforge/capabilities")
        self.assertEqual(response.status_code, 200)
        capabilities = response.json()["capabilities"]
        ladder = capabilities["observation_ladder"]
        self.assertEqual([row["stage"] for row in ladder["stages"]], list(OBSERVATION_LADDER_STAGES))
        self.assertEqual(ladder["embedding_stages"], list(EMBEDDING_LADDER_STAGES))
        self.assertEqual(ladder["held_at"], "discovered")
        self.assertEqual(self.launcher.catalog.calls, 0)

    def test_a_title_query_resolves_against_the_catalog(self) -> None:
        response = self._client.get(f"/novaforge/capabilities?title={GAME}")
        self.assertEqual(response.status_code, 200)
        ladder = response.json()["capabilities"]["observation_ladder"]
        self.assertEqual(self.launcher.catalog.calls, 1)
        self.assertEqual(ladder["target"]["title"], GAME)
        self.assertEqual(ladder["stages"][0]["state"], "true")


class StatePauseRefusalTests(unittest.TestCase):
    """F-06: a PAUSED pipeline cannot supply a current state reading.

    Measured before the fix: the target window was destroyed, the observer paused
    with ``window_missing``, and ``analyze()`` still produced a state from the
    frame that was younger than the state service's own five-second window — a
    live-looking reading for a window that no longer existed. A pause is not a
    freshness question: the pipeline is no longer reading the target.
    """

    def _service(self, status: dict[str, Any]) -> GameStateService:
        # No perception seam is wired on purpose: the pause check must decide
        # before anything is asked to read pixels.
        return GameStateService(observer=_ObserverStub(status))  # type: ignore[arg-type]

    def _success(self) -> dict[str, Any]:
        return {
            "observation_id": "obs-1",
            "seq": 1,
            "at": "2026-10-27T10:00:00+00:00",
            "width": 1920,
            "height": 1080,
            "backend": "printwindow",
        }

    def test_a_paused_observation_refuses_a_state_reading(self) -> None:
        status = observer_status(
            state="paused",
            last=observed_record(),
            success=self._success(),
            pause={
                "code": "target_minimized",
                "detail": "The target window is minimized.",
                "user_requested": False,
            },
            counts={"observed": 3, "rejected": 0, "unavailable": 1, "attempts": 4},
        )
        outcome = asyncio.run(self._service(status).analyze())
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observation_paused")
        self.assertIn("target_minimized", outcome.reason)
        self.assertIsNone(outcome.state)

    def test_a_window_that_vanished_is_a_pause_not_a_live_reading(self) -> None:
        status = observer_status(
            state="paused",
            last=observed_record(),
            success=self._success(),
            pause={
                "code": "window_missing",
                "detail": "No open window matches 'A Game'.",
                "user_requested": False,
            },
        )
        outcome = asyncio.run(self._service(status).analyze())
        self.assertEqual(outcome.reason_code, "observation_paused")
        self.assertIn("window_missing", outcome.reason)

    def test_a_pause_the_operator_asked_for_is_reported_as_one(self) -> None:
        status = observer_status(
            state="paused",
            last=observed_record(),
            success=self._success(),
            pause={"code": "user", "detail": "", "user_requested": True},
        )
        outcome = asyncio.run(self._service(status).analyze())
        self.assertEqual(outcome.reason_code, "observation_paused")
        self.assertIn("by request", outcome.reason)

    def test_an_ended_observation_is_a_record_not_a_reading(self) -> None:
        """Measured: after the observed process exited, a state was still read."""
        status = observer_status(
            state="stopped",
            last=observed_record(),
            success=self._success(),
            counts={"observed": 4, "rejected": 0, "unavailable": 1, "attempts": 5},
        )
        status["reason"] = "process_exited"
        outcome = asyncio.run(self._service(status).analyze())
        self.assertFalse(outcome.analyzed)
        self.assertEqual(outcome.reason_code, "observation_inactive")
        self.assertIn("process_exited", outcome.reason)
        self.assertIn("Start observing again", outcome.reason)

    def test_a_failed_observation_is_not_a_live_reading_either(self) -> None:
        status = observer_status(
            state="failed", last=observed_record(), success=self._success()
        )
        outcome = asyncio.run(self._service(status).analyze())
        self.assertEqual(outcome.reason_code, "observation_inactive")

    def test_an_observing_pipeline_is_never_refused_by_the_pause_check(self) -> None:
        status = observer_status(
            state="observing", last=observed_record(), success=self._success()
        )
        outcome = asyncio.run(self._service(status).analyze())
        # Whatever else it decides (a stale frame, a missing frame file), it is
        # NOT a pause: the check must only speak for a pipeline that stopped.
        self.assertNotEqual(outcome.reason_code, "observation_paused")
        self.assertNotEqual(outcome.reason_code, "observation_inactive")


# ── the eight live states, as the panel decides them ─────────────────────

LIVE_STATE_NEEDLE = r"""
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const observed = (v) => ({
  status: 'observed', captured_at: '2026-10-27T10:00:00Z', reason_code: 'ok', reason: '',
  validation: Object.assign({ capture_ok: true, target_visible: true, fresh: true,
    content_ok: true, livable: true, content: 'valid', reason: '' }, v || {}),
});
const refused = (code, v) => ({
  status: 'unavailable', captured_at: '2026-10-27T10:00:00Z', reason_code: code,
  reason: 'recorded refusal',
  validation: Object.assign({ capture_ok: true, target_visible: true, fresh: false,
    content_ok: false, livable: false, content: 'unverified', reason: 'verdict reason' }, v || {}),
});
const cases = {
  unread: L.observationLiveState(null),
  unwired: L.observationLiveState({ available: false, state: 'idle' }),
  live: L.observationLiveState({ state: 'observing', last_observation: observed() }),
  staticScene: L.observationLiveState({ state: 'observing',
    last_observation: observed({ content: 'repeated' }) }),
  darkScene: L.observationLiveState({ state: 'observing',
    last_observation: observed({ content: 'dark' }) }),
  pausedByUser: L.observationLiveState({ state: 'paused', pause: { user_requested: true },
    last_observation: observed() }),
  minimized: L.observationLiveState({ state: 'paused',
    pause: { code: 'target_minimized', detail: 'The window is minimized.', user_requested: false },
    last_observation: observed() }),
  hidden: L.observationLiveState({ state: 'paused',
    pause: { code: 'target_hidden', user_requested: false } }),
  staleVerdict: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_rejected', { fresh: false }) }),
  staleRecord: L.observationLiveState({ state: 'stopped', last_observation: observed() }),
  blank: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_rejected', { content: 'blank' }) }),
  screenFallback: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_rejected', { content: 'not_target', source: 'screen' }) }),
  backendFailed: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_failed', { capture_ok: false }) }),
  timedOut: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_timeout', { capture_ok: false }) }),
  noFrame: L.observationLiveState({ state: 'observing',
    last_observation: refused('target_unreadable', { capture_ok: false, content: 'unverified' }) }),
  targetNotVisible: L.observationLiveState({ state: 'observing',
    last_observation: refused('capture_rejected', { target_visible: false, content: 'not_target' }) }),
  idleNoRead: L.observationLiveState({ state: 'idle' }),
  windowMissing: L.observationLiveState({ state: 'paused',
    pause: { code: 'window_missing', detail: 'No open window matches the title.', user_requested: false },
    last_observation: { status: 'unavailable', captured_at: '2026-10-27T10:00:02Z',
      reason_code: 'target_missing', reason: 'No open window matches the title.', validation: {} } }),
  identityUnavailable: L.observationLiveState({ state: 'paused',
    pause: { code: 'target_identity_unavailable', user_requested: false },
    last_observation: { status: 'unavailable', captured_at: '2026-10-27T10:00:02Z',
      reason_code: 'target_missing', reason: 'The identity could not be confirmed.', validation: {} } }),
  replaced: L.observationLiveState({ state: 'paused',
    pause: { code: 'target_replaced', detail: 'The window now belongs to pid 999, not 5151.',
      user_requested: false } }),
  exitedNoFrame: L.observationLiveState({ state: 'stopped',
    last_observation: { status: 'unavailable', captured_at: '2026-10-27T10:00:03Z',
      reason_code: 'process_exited', reason: 'Process 5151 exited; observation stopped.',
      validation: {} } }),
};
const ladder = {
  headlineHeld: L.observationLadderHeadline({ held_at: 'window_identified', accepted: false }),
  headlineAccepted: L.observationLadderHeadline({ accepted: true, held_at: '' }),
  headlineMissing: L.observationLadderHeadline(null),
  rows: L.observationLadderRows({
    stages: [
      { stage: 'discovered', state: 'true', detail: 'catalog' },
      { stage: 'embedding_compatible', state: 'false', detail: 'fullscreen' },
      { stage: 'embedded', state: 'false', detail: 'refused' },
      { stage: 'frame_validated', state: 'unknown', detail: '' },
      { stage: 'backend_selected', state: 'not_wired', detail: '' },
    ],
    embedding_stages: ['embedding_compatible', 'embedded'],
  }),
};
console.log(JSON.stringify({ cases, ladder }));
"""


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class ObservationLiveStateTests(unittest.TestCase):
    """One mapping, eight labels, and no stage borrowing another's state."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(LIVE_STATE_NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), PANEL_JS],
            capture_output=True, timeout=60, check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        cls.data = json.loads(stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def _case(self, name: str) -> dict[str, Any]:
        return self.data["cases"][name]

    def test_a_validated_frame_while_running_is_the_only_live_state(self) -> None:
        live = self._case("live")
        self.assertEqual(live["state"], "live_valid")
        self.assertEqual(live["label"], "LIVE AND VALID")

    def test_a_stationary_or_dark_scene_that_validates_is_still_live(self) -> None:
        for name in ("staticScene", "darkScene"):
            case = self._case(name)
            self.assertEqual(case["state"], "live_valid", name)
            self.assertEqual(case["label"], "LIVE AND VALID", name)

    def test_an_old_record_is_never_shown_as_live(self) -> None:
        for name in ("pausedByUser", "staleRecord", "staleVerdict"):
            case = self._case(name)
            self.assertEqual(case["state"], "stale_frame", name)
            self.assertEqual(case["label"], "STALE FRAME", name)

    def test_a_minimized_or_hidden_target_is_its_own_state(self) -> None:
        # `windowMissing`/`identityUnavailable` are pauses that recorded NO
        # frame; before the verification pass they fell through to INVALID FRAME
        # — a claim about pixels that were never read.
        for name in ("minimized", "hidden", "targetNotVisible", "windowMissing",
                     "identityUnavailable"):
            case = self._case(name)
            self.assertEqual(case["state"], "target_not_visible", name)
            self.assertEqual(case["label"], "TARGET HIDDEN OR MINIMIZED", name)
        self.assertEqual(self._case("minimized")["detail"], "The window is minimized.")

    def test_a_failed_capture_is_a_backend_error_not_a_target_state(self) -> None:
        # `noFrame` is the measured NF-23 refusal shape: no backend produced a
        # frame, so there is no frame to identify. Reading its empty content as
        # "not_target" is how the panel said "the frame is not the target"
        # about a frame that never existed.
        for name in ("backendFailed", "timedOut", "noFrame"):
            case = self._case(name)
            self.assertEqual(case["state"], "backend_error", name)
            self.assertEqual(case["label"], "BACKEND ERROR", name)

    def test_a_frame_of_the_wrong_thing_is_an_identity_mismatch(self) -> None:
        for name in ("screenFallback", "replaced"):
            case = self._case(name)
            self.assertEqual(case["state"], "identity_mismatch", name)
            self.assertEqual(case["label"], "TARGET IDENTITY MISMATCH", name)
        self.assertEqual(self._case("windowMissing")["detail"],
                         "No open window matches the title.")

    def test_a_blank_frame_is_invalid_and_unverified(self) -> None:
        case = self._case("blank")
        self.assertEqual(case["state"], "invalid_frame")
        self.assertEqual(case["label"], "INVALID OR UNVERIFIED FRAME")

    def test_an_unwired_host_says_capture_is_unavailable(self) -> None:
        case = self._case("unwired")
        self.assertEqual(case["state"], "capture_unavailable")
        self.assertEqual(case["label"], "CAPTURE UNAVAILABLE")

    def test_nothing_read_and_nothing_started_are_not_live(self) -> None:
        # A stop record carries no frame either: "nothing was read" is the
        # honest answer, not an invalid frame and not a live one.
        for name in ("unread", "idleNoRead", "exitedNoFrame"):
            case = self._case(name)
            self.assertEqual(case["state"], "not_observed", name)
            self.assertEqual(case["label"], "NOT OBSERVED YET", name)

    def test_every_state_carries_its_own_label_and_no_state_is_borrowed(self) -> None:
        labels = {name: case["label"] for name, case in self.data["cases"].items()}
        self.assertEqual(len(set(labels.values())), 8, labels)
        self.assertEqual([name for name, label in labels.items() if label == "LIVE AND VALID"],
                         ["live", "staticScene", "darkScene"])

    def test_the_ladder_rows_name_embedding_as_unsupported_without_gating_capture(self) -> None:
        rows = {row["stage"]: row for row in self.data["ladder"]["rows"]}
        self.assertEqual(rows["embedding_compatible"]["text"], "EMBEDDING UNSUPPORTED")
        self.assertTrue(rows["embedding_compatible"]["embedding"])
        self.assertTrue(rows["embedded"]["embedding"])
        self.assertFalse(rows["discovered"]["embedding"])
        self.assertEqual(rows["backend_selected"]["text"], "not wired")
        self.assertEqual(rows["frame_validated"]["text"], "unknown")

    def test_the_ladder_headline_names_the_stage_that_is_actually_holding(self) -> None:
        ladder = self.data["ladder"]
        self.assertEqual(ladder["headlineHeld"], "Held at: Window identified.")
        self.assertEqual(ladder["headlineAccepted"], "An observation has been accepted through this path.")
        self.assertEqual(ladder["headlineMissing"], "The observation ladder has not been read.")


# ── surface pins ─────────────────────────────────────────────────────────


class LadderSurfaceTests(unittest.TestCase):
    """The card has a place for the ladder, and the ladder is not on the poll."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        cls.js = (REPO / "src" / "novacontrol" / "web" / "static" / "js" / "novaforge.js").read_text(
            encoding="utf-8"
        )

    def test_the_observation_card_has_a_ladder_block(self) -> None:
        self.assertIn('id="forgeObservationLadder"', self.html)
        self.assertIn('id="forgeObservationHistory"', self.html)
        self.assertLess(
            self.html.index('id="forgeObservationLadder"'),
            self.html.index('id="forgeObservationHistory"'),
        )

    def test_the_ladder_state_mapping_is_exported_once(self) -> None:
        self.assertIn("observationLiveState: observationLiveState,", self.js)
        self.assertIn("observationLadderRows: observationLadderRows,", self.js)
        self.assertEqual(self.js.count("function observationLiveState("), 1)
        self.assertEqual(self.js.count("function observationLadderRows("), 1)

    def test_the_poll_never_reads_the_ladder(self) -> None:
        start = self.js.index("async function loadObservationStatus()")
        end = self.js.index("async function observationCommand(")
        self.assertNotIn("loadObservationLadder", self.js[start:end])

    def test_the_refresh_button_re_reads_both_the_status_and_the_ladder(self) -> None:
        self.assertIn('byId("forgeObservationRefresh").addEventListener("click", function () {',
                      self.js)
        self.assertIn("loadObservationLadder();", self.js)


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
